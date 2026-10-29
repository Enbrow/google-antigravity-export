"""Rebase imported local Brain-file references without corrupting Protobuf lengths.

Antigravity stores absolute file paths inside serialized conversation BLOBs and
generated transcript logs. For a newly copied session, only those paths that
point to that session's Brain directory are eligible for repair. Other paths
(such as unrelated project/workspace files) remain unchanged.
"""

from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path
from typing import Optional


def rebase_new_session_brain_links(
    storage: Path, conversation_id: str, source_storage_root: Optional[str] = None
) -> dict:
    """Rebase paths on a session that the importer *just created*.

    Protobuf messages embed byte lengths. A replacement is only safe when
    the path prefix remains the same UTF-8 length. If the target is shorter,
    redundant forward slashes pad it without altering filesystem resolution.
    Otherwise the path is left untouched, and the report records a warning.
    This routine must never be called on a pre-existing destination UUID.
    """
    report = {
        "id": conversation_id, "rewritten_references": 0,
        "updated_generated_files": 0, "unmapped_source_prefixes": 0,
    }
    if os.name != "posix":
        return report  # Windows path mapping is not yet supported.

    db_path = storage / "conversations" / (conversation_id + ".db")
    if not db_path.is_file():
        raise FileNotFoundError("Imported conversation database missing: " + str(db_path))

    target = (storage / "brain" / conversation_id).as_posix().encode("utf-8") + b"/"
    raw = db_path.read_bytes()
    source_roots = set(
        re.findall(
            rb"/(?:Users|home)/[^/\x00]{1,80}/\.gemini/antigravity/brain/"
            + re.escape(conversation_id.encode("ascii")) + rb"/",
            raw,
        )
    )
    if source_storage_root and source_storage_root.startswith("/"):
        candidate = (source_storage_root.rstrip("/") + "/brain/" + conversation_id + "/").encode()
        if candidate in raw:
            source_roots.add(candidate)

    pairs = []
    for old in sorted(source_roots):
        if os.path.normpath(old.decode("utf-8")) == os.path.normpath(target.decode("utf-8")):
            continue
        if len(old) < len(target):
            report["unmapped_source_prefixes"] += 1
            continue
        replacement = target + b"/" * (len(old) - len(target))
        assert len(replacement) == len(old)
        pairs.append((old, replacement))

    if not pairs:
        return report

    with sqlite3.connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        table_names = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )]
        for table in table_names:
            safe_table = '"' + table.replace('"', '""') + '"'
            columns = [r[1] for r in conn.execute("PRAGMA table_info(" + safe_table + ")")]
            for col in columns:
                safe_col = '"' + col.replace('"', '""') + '"'
                for rowid, value in conn.execute(
                    "SELECT rowid," + safe_col + " FROM " + safe_table
                ):
                    if not isinstance(value, (str, bytes)):
                        continue
                    output = value
                    for old, replacement in pairs:
                        needle = old if isinstance(value, bytes) else old.decode("utf-8")
                        repl = replacement if isinstance(value, bytes) else replacement.decode("utf-8")
                        report["rewritten_references"] += output.count(needle)
                        output = output.replace(needle, repl)
                    if output == value:
                        continue
                    original_bytes = value.encode() if isinstance(value, str) else value
                    changed_bytes = output.encode() if isinstance(output, str) else output
                    if len(original_bytes) != len(changed_bytes):
                        raise ValueError("Refusing length-changing Protobuf alteration")
                    conn.execute(
                        "UPDATE " + safe_table + " SET " + safe_col + "=? WHERE rowid=?",
                        (output, rowid),
                    )
        if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("SQLite integrity check failed after artifact path repair")

    # Sidecar logs may be read by the IDE while resuming a conversation.
    # Never modify an agent-created HTML/JS artifact itself.
    generated = storage / "brain" / conversation_id / ".system_generated"
    if generated.is_dir():
        for file in generated.rglob("*"):
            if file.is_symlink():
                raise ValueError("Symlink found in imported generated sidecar")
            if not file.is_file():
                continue
            original = file.read_bytes()
            updated = original
            for old, replacement in pairs:
                updated = updated.replace(old, replacement)
            if updated == original:
                continue
            if len(updated) != len(original):
                raise ValueError("Refusing length-changing generated log alteration")
            file.write_bytes(updated)
            report["updated_generated_files"] += 1

    return report
