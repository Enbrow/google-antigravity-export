"""Safe canonical Brain-file references in native Antigravity SQLite sessions.

The Antigravity UI matches canonical artifact snapshot URIs against paths stored
in protobuf step payloads. Padding a shorter target path with additional slashes
does resolve on POSIX, but the React artifact URI match fails. Instead, rewrite
each protobuf string and update every enclosing length-delimited field.
"""

from __future__ import annotations

import os
import re
import sqlite3
import stat
from pathlib import Path
from typing import Optional


class UnsafeProtobufRewrite(ValueError):
    """A serialized payload cannot be safely rewritten without its schema."""


def _varint_at(data: bytes, pos: int) -> tuple[int, int]:
    number = 0
    for shift in range(0, 70, 7):
        if pos >= len(data):
            raise UnsafeProtobufRewrite("Truncated protobuf varint")
        byte = data[pos]
        pos += 1
        number |= (byte & 0x7F) << shift
        if byte < 0x80:
            return number, pos
    raise UnsafeProtobufRewrite("Oversized protobuf varint")


def _varint(value: int) -> bytes:
    if value < 0:
        raise UnsafeProtobufRewrite("Negative protobuf length")
    result = bytearray()
    while value >= 0x80:
        result.append((value & 0x7F) | 0x80)
        value >>= 7
    result.append(value)
    return bytes(result)


def rewrite_protobuf_uris(
    data: bytes, substitutions: list[tuple[bytes, bytes]], depth: int = 0
) -> tuple[bytes, int]:
    """Rewrite literal URI/path bytes inside a protobuf message.

    Unknown field bytes and field order are retained exactly. When a wire-type
    2 child contains a target path, first attempt recursive protobuf parsing.
    Otherwise treat the child as a UTF-8 string and adjust the parent's encoded
    length. Never edit unrecognized non-text binary data in place.
    """
    if depth > 32:
        raise UnsafeProtobufRewrite("Protobuf message nesting too deep")
    pos = 0
    result = bytearray()
    count = 0
    while pos < len(data):
        begin = pos
        tag, pos = _varint_at(data, pos)
        field, wire = tag >> 3, tag & 7
        if not 1 <= field < (1 << 29):
            raise UnsafeProtobufRewrite("Invalid protobuf field number")
        if wire == 2:
            size, pos = _varint_at(data, pos)
            end = pos + size
            if end > len(data):
                raise UnsafeProtobufRewrite("Truncated protobuf length-delimited value")
            value = data[pos:end]
            if not any(old in value for old, _ in substitutions):
                result.extend(data[begin:end])
            else:
                try:
                    updated, hits = rewrite_protobuf_uris(value, substitutions, depth + 1)
                except UnsafeProtobufRewrite:
                    try:
                        value.decode("utf-8")
                    except UnicodeDecodeError as exc:
                        raise UnsafeProtobufRewrite(
                            "Path appears in non-UTF8 binary data"
                        ) from exc
                    updated = value
                    hits = 0
                    for old, new in substitutions:
                        hits += updated.count(old)
                        updated = updated.replace(old, new)
                if hits == 0 or any(old in updated for old, _ in substitutions):
                    raise UnsafeProtobufRewrite("Could not safely rewrite nested path")
                result.extend(_varint(tag))
                result.extend(_varint(len(updated)))
                result.extend(updated)
                count += hits
            pos = end
        elif wire == 0:
            _, pos = _varint_at(data, pos)
            result.extend(data[begin:pos])
        elif wire in (1, 5):
            pos += 8 if wire == 1 else 4
            if pos > len(data):
                raise UnsafeProtobufRewrite("Truncated protobuf fixed-width value")
            result.extend(data[begin:pos])
        else:
            raise UnsafeProtobufRewrite("Unsupported protobuf wire type")
    return bytes(result), count


def _rewrite_field(value: str | bytes, pairs: list[tuple[bytes, bytes]]) -> tuple[str | bytes, int]:
    if isinstance(value, str):
        updated = value
        count = 0
        for old, new in pairs:
            src, dest = old.decode("utf-8"), new.decode("utf-8")
            count += updated.count(src)
            updated = updated.replace(src, dest)
        return updated, count
    if not any(old in value for old, _ in pairs):
        return value, 0
    try:
        updated, count = rewrite_protobuf_uris(value, pairs)
    except UnsafeProtobufRewrite:
        try:
            decoded = value.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise UnsafeProtobufRewrite(
                "Unrecognized non-text SQLite BLOB with artifact link"
            ) from exc
        if any(ord(ch) < 32 and ch not in "\t\n\r" for ch in decoded):
            raise UnsafeProtobufRewrite("Unrecognized binary SQLite BLOB with artifact link")
        updated = value
        count = 0
        for old, new in pairs:
            count += updated.count(old)
            updated = updated.replace(old, new)
    if any(old in updated for old, _ in pairs):
        raise UnsafeProtobufRewrite("Unrepaired artifact URI in SQLite BLOB")
    return updated, count


def rebase_brain_artifact_links(
    storage: Path, conversation_id: str, source_storage_root: Optional[str] = None,
    update_generated: bool = True
) -> dict:
    """Canonicalize one session's Brain references after it is safely backed up.

    Caller must ensure the IDE is closed and the target session is newly
    imported, or deliberately invoke this on a selected existing session after
    making a complete verified backup. This function does not touch other UUIDs.
    """
    report = {
        "id": conversation_id,
        "rewritten_references": 0,
        "updated_generated_files": 0,
        "unmapped_source_prefixes": 0,
    }
    if os.name != "posix":
        return report

    storage = storage.expanduser().resolve()
    db_path = storage / "conversations" / (conversation_id + ".db")
    if not db_path.is_file() or db_path.is_symlink():
        raise FileNotFoundError("Missing or unsafe conversation database: " + str(db_path))
    target = (storage / "brain" / conversation_id).as_posix().encode("utf-8") + b"/"
    raw = db_path.read_bytes()

    # Match conventional absolute paths from the source computer, including
    # paths previously padded with repeated slashes by older importer versions.
    pattern = (
        rb"/(?:Users|home)/[^/\x00]{1,100}/+\.gemini/antigravity/brain/"
        + re.escape(conversation_id.encode("ascii")) + rb"/"
    )
    old_roots = set(re.findall(pattern, raw))
    if source_storage_root and source_storage_root.startswith("/"):
        candidate = (source_storage_root.rstrip("/") + "/brain/" + conversation_id + "/").encode()
        if candidate in raw:
            old_roots.add(candidate)

    substitutions = []
    for old in sorted(old_roots, key=len, reverse=True):
        if old == target:
            continue
        substitutions.append((old, target))
    if not substitutions:
        return report

    with sqlite3.connect(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        tables = [r[0] for r in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )]
        for table in tables:
            safe_table = '"' + table.replace('"', '""') + '"'
            cols = [r[1] for r in connection.execute("PRAGMA table_info(" + safe_table + ")")]
            for col in cols:
                safe_col = '"' + col.replace('"', '""') + '"'
                for rowid, value in connection.execute(
                    "SELECT rowid, " + safe_col + " FROM " + safe_table
                ):
                    if not isinstance(value, (str, bytes)):
                        continue
                    if not any((old.decode("utf-8") if isinstance(value, str) else old) in value
                               for old, _ in substitutions):
                        continue
                    updated, count = _rewrite_field(value, substitutions)
                    report["rewritten_references"] += count
                    if updated != value:
                        connection.execute(
                            "UPDATE " + safe_table + " SET " + safe_col + "=? WHERE rowid=?",
                            (updated, rowid),
                        )
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise UnsafeProtobufRewrite("SQLite quick_check failed after rewriting paths")

    generated = storage / "brain" / conversation_id / ".system_generated"
    if update_generated and generated.is_dir():
        for file in sorted(generated.rglob("*")):
            if file.is_symlink():
                raise UnsafeProtobufRewrite("Symlinks are not permitted in generated files")
            if not file.is_file():
                continue
            original = file.read_bytes()
            updated = original
            for old, new in substitutions:
                updated = updated.replace(old, new)
            if updated == original:
                continue
            mode = stat.S_IMODE(file.stat().st_mode)
            temporary = file.with_name(file.name + ".ag-rebase-tmp")
            if temporary.exists():
                raise UnsafeProtobufRewrite("Unexpected temporary file: " + str(temporary))
            try:
                with temporary.open("xb") as stream:
                    stream.write(updated)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.chmod(temporary, mode)
                os.replace(temporary, file)
            finally:
                if temporary.exists():
                    temporary.unlink()
            report["updated_generated_files"] += 1
    return report


# Compatibility with v0.2.1 caller and tests.
rebase_new_session_brain_links = rebase_brain_artifact_links
