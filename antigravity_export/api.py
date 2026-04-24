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


def list_trajectories(instance: Instance) -> Dict[str, Any]:
    return post(instance, "GetAllCascadeTrajectories", {}).get("trajectorySummaries", {})


def get_trajectory_steps(instance: Instance, cascade_id: str) -> list:
    return post(
        instance,
        "GetCascadeTrajectorySteps",
        {"cascadeId": cascade_id, "startIndex": 0, "endIndex": 999_999},
    ).get("steps", [])
