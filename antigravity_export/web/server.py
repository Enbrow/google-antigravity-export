"""Local web UI server — stdlib only.

Spins up an HTTP server on 127.0.0.1:<random-port>, opens the browser, and
serves a single-page UI that talks to a small JSON API.

Routes:
  GET  /                         → index.html
  GET  /static/<file>            → static asset (css, js)
  GET  /api/status               → { connected, pid, port, count }
  POST /api/detect               → re-run discovery, returns status
  GET  /api/conversations        → [ { cid, title, project, created, ... } ]
  GET  /api/conversations/<cid>  → { title, turns: [ { role, markdown } ] }
  POST /api/export               → SSE stream of { type, ... } events
  POST /api/export/<cid>         → SSE stream of { type, ... } events
  POST /api/open-folder          → open given path in OS file manager
  POST /api/shutdown             → stop the server (browser tab closed)
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import traceback
import zipfile
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import unquote

from .. import api as ag_api
from ..discover import DiscoveryError, Instance, discover
from ..export import (
    ExportOptions,
    default_gemini_dir,
    export_single_conversation,
    project_from_workspaces,
    run_export,
)
from ..native import (
    NativeMigrationError, create_native_archive,
    plan_native_import, import_native_archive,
)
from ..render import _extract_user_text

STATIC_DIR = Path(__file__).parent / "static"


# ─── shared state ──────────────────────────────────────────────────────────

class _State:
    def __init__(self) -> None:
        self.instance: Optional[Instance] = None
        self.records: List[Dict[str, Any]] = []
        self.last_output: Optional[Path] = None
        self.lock = threading.Lock()
        # Heartbeat tracking — the browser pings /api/heartbeat every few
        # seconds. If it stops (tab closed, laptop slept, etc.) we let the
        # server exit on its own so there's no orphan process.
        self.first_heartbeat_at: Optional[float] = None
        self.last_heartbeat_at: Optional[float] = None


state = _State()
shutdown_event = threading.Event()


# ─── discovery ─────────────────────────────────────────────────────────────

def _refresh_from_instance(inst: Instance) -> Dict[str, Any]:
    summaries = ag_api.list_trajectories(inst)
    recs: List[Dict[str, Any]] = []
    for cid, info in summaries.items():
        title = (info.get("summary") or f"Conversation {cid[:8]}").strip() or "Untitled"
        recs.append({
            "cid": cid,
            "title": title,
            "project": project_from_workspaces(info) or "Playground",
            "created": info.get("createdTime") or "",
            "modified": info.get("lastModifiedTime") or "",
            "stepCount": info.get("stepCount", 0) or 0,
            "_summary": info,
        })
    state.instance = inst
    state.records = recs
    return {
        "connected": True,
        "pid": inst.pid,
        "port": inst.port,
        "count": len(recs),
    }


def run_discovery() -> Dict[str, Any]:
    try:
        inst = discover()
    except DiscoveryError as e:
        return {"connected": False, "error": str(e)}
    try:
        return _refresh_from_instance(inst)
    except Exception as e:
        return {"connected": False, "error": f"Discovery OK but list failed: {e}"}


# ─── turn-based preview for the chat viewer ────────────────────────────────

def _build_turns(steps: list) -> List[Dict[str, str]]:
    """Collapse consecutive agent messages; keep every user turn distinct."""
    turns: List[Dict[str, str]] = []
    buf_role: Optional[str] = None
    buf_parts: List[str] = []

    def flush() -> None:
        nonlocal buf_role, buf_parts
        if buf_role and buf_parts:
            turns.append({"role": buf_role, "markdown": "\n\n".join(buf_parts).strip()})
        buf_role = None
        buf_parts = []

    for step in steps:
        t = step.get("type", "")
        if t == "CORTEX_STEP_TYPE_USER_INPUT":
            text = _extract_user_text(step)
            if text and text.strip():
                flush()
                buf_role = "user"
                buf_parts = [text.strip()]
                flush()
        elif t == "CORTEX_STEP_TYPE_PLANNER_RESPONSE":
            pr = step.get("plannerResponse", {}) or {}
            reply = pr.get("modifiedResponse") or pr.get("response") or pr.get("text", "")
            if reply and reply.strip():
                if buf_role != "agent":
                    flush()
                    buf_role = "agent"
                buf_parts.append(reply.strip())
        elif t == "CORTEX_STEP_TYPE_NOTIFY_USER":
            nu = step.get("notifyUser", {}) or {}
            content = (nu.get("notificationContent", "") or "").strip()
            if content:
                if buf_role != "agent":
                    flush()
                    buf_role = "agent"
                buf_parts.append(content)
    flush()
    return turns


# ─── export options from JSON ──────────────────────────────────────────────

def _build_opts(data: dict) -> ExportOptions:
    out = Path(data.get("output") or (Path.cwd() / "AntigravityExport")).expanduser().resolve()
    return ExportOptions(
        output_dir=out,
        gemini_dir=default_gemini_dir(),
        include_knowledge=bool(data.get("includeKnowledge", True)),
        include_artifacts=bool(data.get("includeArtifacts", True)),
        include_conversations=bool(data.get("includeConversations", True)),
        project_filter=(data.get("projectFilter") or None),
        wipe=bool(data.get("wipe", True)),
        naming=data.get("naming", "title-date"),
        layout=data.get("layout", "project"),
        file_format=data.get("format", "both"),
        detail=data.get("detail", "full"),
        apply_timestamps=bool(data.get("applyTimestamps", True)),
    )


def _open_folder(path: Path) -> None:
    p = str(path)
    try:
        if sys.platform.startswith("win"):
            subprocess.Popen(["explorer", p])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", p])
        else:
            subprocess.Popen(["xdg-open", p])
    except Exception:
        pass


def _pick_folder_native(initial: Optional[str] = None) -> Optional[str]:
    """Pop the OS's native 'choose folder' dialog via tkinter (stdlib).

    Runs on a fresh thread because tkinter wants a clean Tcl interp per call
    and we're invoked from the HTTP handler thread. Returns the chosen path,
    or ``None`` if the user cancels / tkinter isn't available.
    """
    result: Dict[str, Optional[str]] = {"path": None}

    def worker() -> None:
        try:
            import tkinter as tk
            from tkinter import filedialog
        except Exception:
            return
        try:
            root = tk.Tk()
            root.withdraw()
            try:
                root.attributes("-topmost", True)
            except Exception:
                pass
            # Force dialog to front on Windows
            try:
                root.update()
            except Exception:
                pass
            kw: Dict[str, Any] = {"title": "Choose output folder", "mustexist": False}
            if initial and Path(initial).expanduser().exists():
                kw["initialdir"] = str(Path(initial).expanduser())
            path = filedialog.askdirectory(**kw)
            try:
                root.destroy()
            except Exception:
                pass
            result["path"] = path or None
        except Exception as e:
            print(f"Folder picker failed: {e}", file=sys.stderr)

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(timeout=300)  # user has 5 minutes to pick
    return result["path"]


# ─── HTTP handler ──────────────────────────────────────────────────────────

_CTYPES = {
    ".html": "text/html; charset=utf-8",
    ".css":  "text/css; charset=utf-8",
    ".js":   "application/javascript; charset=utf-8",
    ".svg":  "image/svg+xml",
    ".png":  "image/png",
    ".ico":  "image/x-icon",
}


class Handler(BaseHTTPRequestHandler):
    # Silence stderr noise — we log via log events.
    def log_message(self, *_args, **_kwargs) -> None:  # noqa: D401
        return

    # ── helpers ────────────────────────────────────────────────────────────

    def _send_json(self, obj: Any, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path) -> None:
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            self.send_error(404)
            return
        ctype = _CTYPES.get(path.suffix.lower(), "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0) or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def _start_sse(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

    def _sse_send(self, obj: dict) -> bool:
        """Send one SSE event. Returns False if the client disconnected."""
        try:
            line = f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode("utf-8")
            self.wfile.write(line)
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError, OSError):
            return False

    # ── routing ────────────────────────────────────────────────────────────

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]

        if path in ("/", "/index.html"):
            self._send_file(STATIC_DIR / "index.html")
            return

        if path.startswith("/static/"):
            rel = path[len("/static/"):].lstrip("/")
            target = (STATIC_DIR / rel).resolve()
            try:
                target.relative_to(STATIC_DIR.resolve())
            except ValueError:
                self.send_error(403)
                return
            self._send_file(target)
            return

        if path == "/api/status":
            inst = state.instance
            self._send_json({
                "connected": inst is not None,
                "pid": inst.pid if inst else None,
                "port": inst.port if inst else None,
                "count": len(state.records),
                "defaultOutput": str(Path.cwd() / "AntigravityExport"),
                "defaultGeminiDir": str(default_gemini_dir()),
                "lastOutput": str(state.last_output) if state.last_output else None,
            })
            return

        if path == "/api/conversations":
            with state.lock:
                items = [{k: v for k, v in r.items() if k != "_summary"}
                         for r in state.records]
            self._send_json(items)
            return

        if path.startswith("/api/conversations/"):
            cid = unquote(path[len("/api/conversations/"):])
            rec = next((r for r in state.records if r["cid"] == cid), None)
            if not rec or not state.instance:
                self._send_json({"error": "Not found"}, 404)
                return
            try:
                steps = ag_api.get_trajectory_steps(state.instance, cid)
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
                return
            turns = _build_turns(steps)
            self._send_json({
                "title":     rec["title"],
                "project":   rec["project"],
                "created":   rec["created"],
                "stepCount": rec["stepCount"],
                "cid":       cid,
                "turns":     turns,
            })
            return

        self.send_error(404)

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        data = self._read_json_body()

        if path == "/api/detect":
            self._send_json(run_discovery())
            return

        if path in ("/api/native/export", "/api/native/plan", "/api/native/apply"):
            # Browser pages on other origins must not be able to trigger local
            # filesystem writes via a cross-site request.
            origin = self.headers.get("Origin")
            if origin and origin != "http://" + self.headers.get("Host", ""):
                self._send_json({"error": "Cross-origin native migration request blocked"}, 403)
                return
            requested_path = str(data.get("path") or "").strip()
            if not requested_path:
                self._send_json({"error": "Please specify the local ZIP file path"}, 400)
                return
            try:
                src = Path(requested_path).expanduser()
                storage = default_gemini_dir()
                if path == "/api/native/export":
                    if not src.name.lower().endswith(".zip"):
                        raise NativeMigrationError("Native archive file must end in .zip")
                    result = create_native_archive(storage, src)
                elif path == "/api/native/plan":
                    result = plan_native_import(src, storage)
                else:
                    if not isinstance(data.get("expectedAddIds"), list):
                        raise NativeMigrationError("Preview the import before applying")
                    preview = plan_native_import(src, storage)
                    expected = sorted(data["expectedAddIds"])
                    if expected != sorted(r["id"] for r in preview["add"]):
                        raise NativeMigrationError("Import plan changed. Preview again before applying")
                    result = import_native_archive(src, storage, apply=True)
                self._send_json(result)
            except (NativeMigrationError, OSError, ValueError, KeyError, TypeError,
                    zipfile.BadZipFile) as e:
                self._send_json({"error": str(e)}, 400)
            return

        if path == "/api/export":
            self._stream_export_all(data)
            return

        if path.startswith("/api/export/"):
            cid = unquote(path[len("/api/export/"):])
            self._stream_export_single(cid, data)
            return

        if path == "/api/open-folder":
            raw = data.get("path") or ""
            if raw:
                p = Path(raw)
                if p.exists():
                    _open_folder(p)
            self._send_json({"ok": True})
            return

        if path == "/api/pick-folder":
            chosen = _pick_folder_native(data.get("initial") or None)
            self._send_json({"path": chosen or ""})
            return

        if path == "/api/shutdown":
            self._send_json({"ok": True})
            shutdown_event.set()
            # schedule the actual server shutdown off-thread so this response
            # can flush before the server dies.
            threading.Thread(target=lambda: (time.sleep(0.2), self.server.shutdown()),
                             daemon=True).start()
            return

        if path == "/api/heartbeat":
            now = time.time()
            if state.first_heartbeat_at is None:
                state.first_heartbeat_at = now
            state.last_heartbeat_at = now
            self._send_json({"ok": True})
            return

        self.send_error(404)

    # ── SSE handlers ───────────────────────────────────────────────────────

    def _stream_export_all(self, data: dict) -> None:
        if not state.instance:
            self._send_json({"error": "Not connected"}, 400)
            return
        try:
            opts = _build_opts(data)
        except Exception as e:
            self._send_json({"error": f"Bad options: {e}"}, 400)
            return

        self._start_sse()
        alive = {"v": True}

        def send(obj: dict) -> None:
            if alive["v"] and not self._sse_send(obj):
                alive["v"] = False

        def log(msg: str) -> None:
            send({"type": "log", "message": msg})

        def progress(phase: str, label: str, current: int, total: int) -> None:
            send({"type": "progress", "phase": phase, "label": label,
                  "current": current, "total": total})

        send({"type": "start", "output": str(opts.output_dir)})
        try:
            run_export(state.instance, opts, log=log, progress=progress)
            state.last_output = opts.output_dir
            send({"type": "done", "output": str(opts.output_dir)})
        except Exception as e:
            traceback.print_exc()
            send({"type": "error", "message": str(e)})

    def _stream_export_single(self, cid: str, data: dict) -> None:
        rec = next((r for r in state.records if r["cid"] == cid), None)
        if not rec or not state.instance:
            self._send_json({"error": "Not found"}, 404)
            return
        try:
            opts = _build_opts(data)
        except Exception as e:
            self._send_json({"error": f"Bad options: {e}"}, 400)
            return
        # single-conversation exports never wipe.
        opts.wipe = False

        self._start_sse()
        alive = {"v": True}

        def send(obj: dict) -> None:
            if alive["v"] and not self._sse_send(obj):
                alive["v"] = False

        def log(msg: str) -> None:
            send({"type": "log", "message": msg})

        def progress(phase: str, label: str, current: int, total: int) -> None:
            send({"type": "progress", "phase": phase, "label": label,
                  "current": current, "total": total})

        send({"type": "start", "output": str(opts.output_dir), "single": True})
        try:
            export_single_conversation(
                state.instance, cid, rec["_summary"], opts,
                log=log, progress=progress,
            )
            state.last_output = opts.output_dir
            send({"type": "done", "output": str(opts.output_dir), "single": True})
        except Exception as e:
            traceback.print_exc()
            send({"type": "error", "message": str(e)})


# ─── launcher ──────────────────────────────────────────────────────────────

def _pick_port() -> int:
    """Ask the OS for a free port on 127.0.0.1."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _idle_watchdog(httpd: ThreadingHTTPServer, idle_timeout: float) -> None:
    """Shut the server down if the browser stops heartbeating.

    Only engages AFTER the first heartbeat — so opening the CLI but never
    visiting the URL doesn't trip the timeout. Once the browser has checked
    in once, we require it to keep checking in.
    """
    while not shutdown_event.is_set():
        time.sleep(2.0)
        first = state.first_heartbeat_at
        last  = state.last_heartbeat_at
        if first is None:
            continue
        if last is not None and (time.time() - last) > idle_timeout:
            print(f"\nNo browser heartbeat for {idle_timeout:.0f}s — shutting down.")
            shutdown_event.set()
            threading.Thread(target=httpd.shutdown, daemon=True).start()
            return


def run_web(
    port: Optional[int] = None,
    open_browser: bool = True,
    idle_timeout: float = 30.0,
) -> int:
    if not STATIC_DIR.is_dir():
        print(f"Static dir missing: {STATIC_DIR}", file=sys.stderr)
        return 2

    port = port or _pick_port()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"

    print("─" * 60)
    print(f"  Antigravity Export UI")
    print(f"  → {url}")
    print(f"  Press Ctrl-C to stop, or just close the browser tab.")
    print("─" * 60)

    # Kick off discovery in a thread so startup feels instant.
    threading.Thread(target=run_discovery, daemon=True).start()

    # Idle watchdog so a closed tab auto-exits the server.
    threading.Thread(
        target=_idle_watchdog, args=(httpd, idle_timeout), daemon=True,
    ).start()

    if open_browser:
        threading.Thread(
            target=lambda: (time.sleep(0.35), webbrowser.open(url)),
            daemon=True,
        ).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping…")
    finally:
        httpd.server_close()
    return 0
