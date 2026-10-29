"""Thin HTTP client for the Antigravity local language-server API."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Dict

from .discover import Instance


class ApiError(RuntimeError):
    pass


def post(instance: Instance, endpoint: str, body: Dict[str, Any], timeout: float = 120.0) -> Dict[str, Any]:
    url = f"{instance.base_url}/{endpoint}"
    payload = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Connect-Protocol-Version": "1",
            "X-Codeium-Csrf-Token": instance.csrf,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        raise ApiError(f"{endpoint} returned HTTP {e.code}: {e.read()[:200]!r}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ApiError(f"{endpoint} failed: {e}") from e

    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise ApiError(f"{endpoint} returned non-JSON body: {raw[:200]!r}") from e


def _local_sqlite_trajectories(existing: Dict[str, Any]) -> Dict[str, Any]:
    """Recover local conversations omitted from the language-server's index.

    Newer Antigravity builds store conversations as individual SQLite databases
    under ~/.gemini/antigravity/conversations/. The list API can omit older
    conversations even though GetCascadeTrajectorySteps still accepts their IDs.
    Read only the minimal metadata; conversation steps continue to use the API.
    """
    import sqlite3
    from datetime import datetime, timezone
    from pathlib import Path

    directory = Path.home() / ".gemini" / "antigravity" / "conversations"
    if not directory.is_dir():
        return existing

    for db_path in directory.glob("*.db"):
        try:
            # mode=ro avoids accidentally creating or modifying the database.
            db_uri = db_path.resolve().as_uri() + "?mode=ro"
            with sqlite3.connect(db_uri, uri=True, timeout=1) as conn:
                rows = conn.execute(
                    "SELECT cascade_id FROM trajectory_meta WHERE cascade_id IS NOT NULL"
                ).fetchall()
                step_count = conn.execute("SELECT COUNT(*) FROM steps").fetchone()[0]
            created = datetime.fromtimestamp(
                db_path.stat().st_mtime, timezone.utc
            ).isoformat().replace("+00:00", "Z")
            for (cascade_id,) in rows:
                if not cascade_id or cascade_id in existing:
                    continue
                existing[cascade_id] = {
                    "summary": f"Recovered conversation {cascade_id[:8]}",
                    "stepCount": step_count,
                    "createdTime": created,
                    "lastModifiedTime": created,
                }
        except (OSError, sqlite3.Error, ValueError):
            # A partial, locked or differently-shaped DB must not block export.
            continue
    return existing


def list_trajectories(instance: Instance) -> Dict[str, Any]:
    summaries = post(instance, "GetAllCascadeTrajectories", {}).get("trajectorySummaries", {})
    return _local_sqlite_trajectories(summaries)


def get_trajectory_steps(instance: Instance, cascade_id: str) -> list:
    return post(
        instance,
        "GetCascadeTrajectorySteps",
        {"cascadeId": cascade_id, "startIndex": 0, "endIndex": 999_999},
    ).get("steps", [])
