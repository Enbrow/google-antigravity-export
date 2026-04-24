"""Conversation folder/file naming and output-layout helpers."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Iterable, Optional

NAMING_CHOICES = ("title-date", "date-title", "iso-title", "title-only", "id-title")
LAYOUT_CHOICES = ("project", "flat", "date")
FORMAT_CHOICES = ("both", "markdown", "json")
DETAIL_CHOICES = ("full", "chat", "thinking")


def _sanitize(s: str, max_len: int = 120) -> str:
    return re.sub(r'[\\/:*?"<>|\n\r]', "_", s).strip()[:max_len]


def _parse_iso_date(iso: str) -> Optional[datetime]:
    if not iso:
        return None
    try:
        return datetime.strptime(iso[:10], "%Y-%m-%d")
    except ValueError:
        return None


def build_name(title: str, created_iso: str, cascade_id: str, naming: str) -> str:
    """Return the bare (unsanitized-length-wise) folder/file name for a conversation."""
    title = (title or "Untitled").strip() or "Untitled"
    dt = _parse_iso_date(created_iso)

    if naming == "title-date":
        date_str = dt.strftime("%b %d, %Y") if dt else (created_iso[:10] or "Unknown Date")
        return _sanitize(f"{title} ({date_str})")

    if naming == "date-title":
        date_str = dt.strftime("%Y-%m-%d") if dt else (created_iso[:10] or "0000-00-00")
        return _sanitize(f"{date_str} - {title}")

    if naming == "iso-title":
        date_str = dt.strftime("%Y-%m-%d") if dt else (created_iso[:10] or "0000-00-00")
        slug = re.sub(r"\s+", "_", title)
        slug = re.sub(r"[^A-Za-z0-9_\-]", "", slug).strip("_") or "Untitled"
        return _sanitize(f"{date_str}_{slug}")

    if naming == "title-only":
        return _sanitize(title)

    if naming == "id-title":
        short = (cascade_id or "").split("-")[0][:8] or "noid"
        return _sanitize(f"{short}_{title}")

    raise ValueError(f"Unknown naming scheme: {naming!r}")


def group_key(project: str, created_iso: str, layout: str) -> str:
    """Return the conversations/<grouping-folder> name (or '' for flat layout)."""
    if layout == "flat":
        return ""
    if layout == "date":
        return created_iso[:7] if created_iso else "undated"
    # 'project'
    return project or "Playground"


def unique(existing: Iterable[str], candidate: str) -> str:
    """Return a name that isn't in `existing`, appending ' (2)', ' (3)', ... as needed."""
    seen = set(existing)
    if candidate not in seen:
        return candidate
    n = 2
    while True:
        alt = f"{candidate} ({n})"
        if alt not in seen:
            return alt
        n += 1
