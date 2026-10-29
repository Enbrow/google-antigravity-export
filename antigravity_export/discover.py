"""Locate the running Antigravity language-server process and its local API.

Antigravity spawns a background `language_server_<platform>` process that hosts
a local HTTP API for its own IDE. We piggy-back on that API by:

1. Finding the process (matches `language_server_*` on macOS, Linux, Windows).
2. Parsing `--csrf_token <uuid>` out of its command line.
3. Enumerating TCP ports it's listening on (typically 2–4 ports).
4. Probing each port with a cheap authenticated request to find the one
   that actually serves the conversation API (the others are for other features).
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Iterable, List, Optional

import psutil

LANGUAGE_SERVER_PREFIX = "language_server"
CSRF_CMDLINE_RE = re.compile(r"--csrf_token[=\s]+([0-9a-fA-F-]{36})")


@dataclass
class Instance:
    pid: int
    process_name: str
    csrf: str
    port: int

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/exa.language_server_pb.LanguageServerService"


class DiscoveryError(RuntimeError):
    """Raised when the Antigravity language-server can't be located."""


def _iter_language_server_procs() -> Iterable[psutil.Process]:
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            name = (proc.info.get("name") or "").lower()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        # Strip exe suffix for Windows
        stem = name[:-4] if name.endswith(".exe") else name
        if stem.startswith(LANGUAGE_SERVER_PREFIX):
            yield proc


def _cmdline(proc: psutil.Process) -> List[str]:
    try:
        return proc.cmdline() or []
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return []


def _extract_csrf(cmdline: List[str]) -> Optional[str]:
    # cmdline may show `--csrf_token <uuid>` as two args or one with `=`
    for i, arg in enumerate(cmdline):
        if arg == "--csrf_token" and i + 1 < len(cmdline):
            candidate = cmdline[i + 1]
            if CSRF_CMDLINE_RE.match(f"--csrf_token {candidate}"):
                return candidate
        if arg.startswith("--csrf_token="):
            candidate = arg.split("=", 1)[1]
            if CSRF_CMDLINE_RE.match(f"--csrf_token={candidate}"):
                return candidate
    joined = " ".join(cmdline)
    m = CSRF_CMDLINE_RE.search(joined)
    return m.group(1) if m else None


def _listening_ports(proc: psutil.Process) -> List[int]:
    try:
        conns = proc.net_connections(kind="inet")
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return []
    ports: List[int] = []
    for c in conns:
        if c.status == psutil.CONN_LISTEN and c.laddr:
            addr = c.laddr.ip
            if addr in ("127.0.0.1", "::1", "0.0.0.0", "::"):
                ports.append(c.laddr.port)
    # De-dupe while preserving order
    return list(dict.fromkeys(ports))


def _probe_port(port: int, csrf: str, timeout: float = 3.0) -> bool:
    url = (
        f"http://127.0.0.1:{port}/exa.language_server_pb.LanguageServerService/"
        "GetAllCascadeTrajectories"
    )
    req = urllib.request.Request(
        url,
        data=b"{}",
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Connect-Protocol-Version": "1",
            "X-Codeium-Csrf-Token": csrf,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                return False
            # An empty conversation index is a valid response on newer
            # Antigravity builds: GetAllCascadeTrajectories can return {} even
            # while SQLite-backed local conversations remain accessible.
            # Still require an HTTP 200 from this precise RPC endpoint.
            head = resp.read(4096)
            if b'"trajectorySummaries"' in head:
                return True
            try:
                return json.loads(head) == {}
            except (json.JSONDecodeError, UnicodeDecodeError):
                return False
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
        return False


def discover(
    csrf_override: Optional[str] = None,
    port_override: Optional[int] = None,
    verbose: bool = False,
) -> Instance:
    """Return a ready-to-use Instance, or raise DiscoveryError."""

    # If both are overridden, trust the caller and verify.
    if csrf_override and port_override:
        if _probe_port(port_override, csrf_override):
            return Instance(pid=-1, process_name="(override)", csrf=csrf_override, port=port_override)
        raise DiscoveryError(
            f"Provided CSRF+port ({port_override}) did not respond to GetAllCascadeTrajectories."
        )

    procs = list(_iter_language_server_procs())
    if not procs:
        raise DiscoveryError(
            "No language_server process found. Is Antigravity running? "
            "Open the app and try again."
        )

    errors: List[str] = []
    for proc in procs:
        cmdline = _cmdline(proc)
        if not cmdline:
            continue
        name = proc.info.get("name") or "language_server"
        csrf = csrf_override or _extract_csrf(cmdline)
        if not csrf:
            errors.append(f"pid={proc.pid}: no --csrf_token in cmdline")
            continue

        candidate_ports: List[int] = []
        if port_override:
            candidate_ports = [port_override]
        else:
            candidate_ports = _listening_ports(proc)
            if not candidate_ports:
                errors.append(f"pid={proc.pid}: no listening ports")
                continue

        if verbose:
            print(f"  pid={proc.pid} name={name} csrf={csrf[:8]}... ports={candidate_ports}")

        for port in candidate_ports:
            if _probe_port(port, csrf):
                if verbose:
                    print(f"    port {port}: OK")
                return Instance(pid=proc.pid, process_name=name, csrf=csrf, port=port)
            elif verbose:
                print(f"    port {port}: no response")

        errors.append(
            f"pid={proc.pid}: none of ports {candidate_ports} answered the API"
        )

    raise DiscoveryError(
        "Found language_server process(es) but could not reach the API.\n"
        + "\n".join(f"  - {e}" for e in errors)
        + "\n\nMake sure Antigravity is fully loaded (open the app and wait for the UI)."
    )
