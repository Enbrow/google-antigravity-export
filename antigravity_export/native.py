"""Native Antigravity 2.x conversation migration (offline, additive, stdlib only).

The readable Markdown/JSON export is not an import format. This module archives
the native SQLite sessions and matching summary rows. Existing destination IDs
are never replaced; imports require Antigravity to be closed and a verified
pre-import snapshot is created before any target mutation.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .rebase import rebase_new_session_brain_links

FORMAT = "antigravity-native-sessions-v1"
ID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
MAX_UNPACKED = 4 * 1024 ** 3
MAX_FILES = 100000


class NativeMigrationError(RuntimeError):
    """Invalid archive, incompatible storage or unsafe migration."""


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _version_for_storage(storage: Path) -> Optional[str]:
    """Record the installed Antigravity version for normal per-user storage."""
    default = Path.home() / ".gemini" / "antigravity"
    if storage.resolve() != default.resolve():
        return None  # an isolated/test data directory is not the installed IDE
    if sys.platform == "darwin":
        import plistlib
        for path in (Path("/Applications/Antigravity.app/Contents/Info.plist"),
                     Path.home() / "Applications/Antigravity.app/Contents/Info.plist"):
            if path.is_file():
                try:
                    with path.open("rb") as f:
                        return plistlib.load(f).get("CFBundleShortVersionString")
                except (OSError, ValueError):
                    continue
    if sys.platform.startswith("linux"):
        for path in (Path("/opt/antigravity/app/product.json"),
                     Path.home() / ".local/share/antigravity/app/product.json"):
            if path.is_file():
                try:
                    return json.loads(path.read_text()).get("version")
                except (OSError, ValueError):
                    continue
    return None


def _version_numbers(value: Optional[str]) -> tuple:
    if not value:
        return ()
    matches = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:[-+].*)?", value)
    return tuple(map(int, matches.groups())) if matches else ()


def _db_read(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise NativeMigrationError(f"Missing SQLite database: {path}")
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _sha256(src) -> str:
    h = hashlib.sha256()
    for b in iter(lambda: src.read(1024 * 1024), b""):
        h.update(b)
    return h.hexdigest()


def _checksum(path: Path) -> str:
    with path.open("rb") as f:
        return _sha256(f)


def _add_streaming_file(z: zipfile.ZipFile, file: Path, arcname: str) -> dict:
    """Hash the exact bytes written, even if the source changes during backup."""
    total = 0
    digest = hashlib.sha256()
    with file.open("rb") as src, z.open(arcname, "w") as dest:
        for part in iter(lambda: src.read(1024 * 1024), b""):
            dest.write(part)
            total += len(part)
            digest.update(part)
    return {"path": arcname, "size": total, "sha256": digest.hexdigest()}


def _json_bytes(obj: Any) -> bytes:
    return (json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _encode_row(row: sqlite3.Row) -> Dict[str, Any]:
    return {k: ({"$bytes": base64.b64encode(v).decode("ascii")} if isinstance(v, bytes) else v)
            for k, v in dict(row).items()}


def _decode_row(row: Dict[str, Any]) -> Dict[str, Any]:
    return {k: (base64.b64decode(v["$bytes"], validate=True) if
                 isinstance(v, dict) and set(v) == {"$bytes"} else v)
            for k, v in row.items()}


def _check_id(cid: str) -> None:
    if not isinstance(cid, str) or not ID.fullmatch(cid):
        raise NativeMigrationError("Invalid conversation UUID in archive")


def _verify_session_db(path: Path, cid: str) -> int:
    with _db_read(path) as conn:
        if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise NativeMigrationError(f"Session database failed quick_check: {cid}")
        row = conn.execute("SELECT cascade_id FROM trajectory_meta LIMIT 1").fetchone()
        if not row or row[0] != cid:
            raise NativeMigrationError(f"Session database ID mismatch: {cid}")
        return int(conn.execute("SELECT COUNT(*) FROM steps").fetchone()[0])


def _copy_sqlite_snapshot(source: Path, target: Path) -> None:
    with _db_read(source) as source_conn:
        with sqlite3.connect(target) as target_conn:
            source_conn.backup(target_conn)


def _running_antigravity() -> List[str]:
    """Fail closed on real Antigravity processes, not on a shell's arguments."""
    if sys.platform == "win32":
        try:
            cmd = subprocess.check_output(["tasklist", "/fo", "csv", "/nh"], text=True)
            return [line.split(",", 1)[0] for line in cmd.splitlines()
                    if re.search(r'"(?:antigravity|language_server)[^"]*"', line, re.I)]
        except (OSError, subprocess.SubprocessError):
            raise NativeMigrationError("Unable to check running Antigravity processes")
    try:
        cmd = subprocess.check_output(["ps", "-axo", "comm="], text=True)
    except (OSError, subprocess.SubprocessError) as exc:
        raise NativeMigrationError("Unable to inspect running processes") from exc
    found = []
    for line in cmd.splitlines():
        name = Path(line.strip()).name.lower()
        if name in ("antigravity", "antigravity ide", "language_server", "language_server_macos",
                    "language_server_linux_x64", "language_server_linux_arm64",
                    "language_server_windows_x64.exe", "antigravity.exe"):
            found.append(name)
    return found


def _proto_varint(i: int) -> bytes:
    r = bytearray()
    while i >= 128:
        r.append((i & 127) | 128)
        i >>= 7
    r.append(i)
    return bytes(r)


def _proto_bytes(n: int, b: bytes) -> bytes:
    return _proto_varint((n << 3) | 2) + _proto_varint(len(b)) + b


def _read_varint(raw: bytes, pos: int):
    value = shift = 0
    for _ in range(10):
        if pos >= len(raw):
            raise NativeMigrationError("Truncated Protobuf index")
        b = raw[pos]
        pos += 1
        value |= (b & 127) << shift
        if b < 128:
            return value, pos
        shift += 7
    raise NativeMigrationError("Malformed Protobuf varint")


def _proto_fields(raw: bytes):
    pos = 0
    while pos < len(raw):
        tag, pos = _read_varint(raw, pos)
        field, wire = tag >> 3, tag & 7
        if not field:
            raise NativeMigrationError("Invalid Protobuf field")
        if wire == 2:
            length, pos = _read_varint(raw, pos)
            value = raw[pos:pos + length]
            if len(value) != length:
                raise NativeMigrationError("Truncated Protobuf field")
            pos += length
        elif wire == 0:
            value, pos = _read_varint(raw, pos)
        elif wire in (1, 5):
            size = 8 if wire == 1 else 4
            value = raw[pos:pos + size]
            if len(value) != size:
                raise NativeMigrationError("Truncated fixed-width Protobuf field")
            pos += size
        else:
            raise NativeMigrationError(f"Unsupported Protobuf wire type {wire}")
        yield field, wire, value


def _proto_ids(raw: bytes) -> set:
    ids = set()
    for field, wire, payload in _proto_fields(raw):
        if field != 1 or wire != 2:
            continue
        for subfield, subwire, subpayload in _proto_fields(payload):
            if subfield == 1 and subwire == 2:
                try:
                    ids.add(subpayload.decode("utf-8"))
                except UnicodeDecodeError as exc:
                    raise NativeMigrationError("Invalid Protobuf conversation ID") from exc
    return ids


def _add_proto(original: bytes, rows: Dict[str, Dict[str, Any]], new_ids: List[str]) -> bytes:
    ids = _proto_ids(original)
    result = bytearray(original)
    for cid in new_ids:
        if cid in ids:
            continue
        raw = _decode_row(rows[cid]).get("raw_summary")
        if not raw:
            continue
        if not isinstance(raw, bytes):
            raise NativeMigrationError("Summary raw_summary must be a blob")
        result += _proto_bytes(1, _proto_bytes(1, cid.encode()) + _proto_bytes(2, raw))
        ids.add(cid)
    return bytes(result)


def create_native_archive(storage: Path, destination: Path,
                          selected_ids: Optional[List[str]] = None) -> Dict[str, Any]:
    """Generate a portable ZIP, preserving full SQLite sessions and summary blobs."""
    storage = storage.expanduser().resolve()
    destination = destination.expanduser().resolve()
    if destination.exists():
        raise NativeMigrationError(f"Refusing to overwrite existing archive: {destination}")
    ids = set(selected_ids or [])
    for cid in ids:
        _check_id(cid)
    with _db_read(storage / "conversation_summaries.db") as index:
        rows = {r["conversation_id"]: _encode_row(r)
                for r in index.execute("SELECT * FROM conversation_summaries")}
    if ids - set(rows):
        raise NativeMigrationError(f"Missing indexed conversations: {len(ids - set(rows))}")
    if ids:
        rows = {k: v for k, v in rows.items() if k in ids}
    rows = {k: v for k, v in rows.items()
            if ID.fullmatch(k) and (storage / "conversations" / f"{k}.db").is_file()}
    if not rows:
        raise NativeMigrationError("No indexed native session database found")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.with_name(destination.name + f".tmp-{os.getpid()}")
    if staging.exists():
        raise NativeMigrationError("Another archive creation appears to be in progress")
    files = []
    conversations = []
    try:
        with tempfile.TemporaryDirectory(prefix="ag-native-snapshot-") as temp:
            with zipfile.ZipFile(staging, "x", zipfile.ZIP_DEFLATED, compresslevel=6) as out:
                for cid, row in sorted(rows.items()):
                    _check_id(cid)
                    db = Path(temp) / f"{cid}.db"
                    source_db = storage / "conversations" / f"{cid}.db"
                    if source_db.is_symlink():
                        raise NativeMigrationError("Symlink session databases are not supported")
                    _copy_sqlite_snapshot(source_db, db)
                    steps = _verify_session_db(db, cid)
                    zpath = f"conversations/{cid}.db"
                    files.append(_add_streaming_file(out, db, zpath))
                    brain = storage / "brain" / cid
                    brain_files = 0
                    if brain.is_symlink():
                        raise NativeMigrationError("Symlinks are not allowed in session artifacts")
                    if brain.is_dir():
                        for file in sorted(brain.rglob("*")):
                            if file.is_symlink():
                                raise NativeMigrationError("Symlinks are not allowed in session artifacts")
                            if not file.is_file():
                                continue
                            relative = file.relative_to(storage).as_posix()
                            files.append(_add_streaming_file(out, file, relative))
                            brain_files += 1
                    annotation = storage / "annotations" / f"{cid}.pbtxt"
                    if annotation.is_symlink():
                        raise NativeMigrationError("Symlink annotations are not supported")
                    if annotation.is_file():
                        relative = f"annotations/{cid}.pbtxt"
                        files.append(_add_streaming_file(out, annotation, relative))
                    conversations.append({"id": cid, "title": row.get("title", ""),
                                          "steps": steps, "brain_files": brain_files})
                index_bytes = _json_bytes(rows)
                out.writestr("index.json", index_bytes)
                files.append({"path": "index.json", "size": len(index_bytes),
                              "sha256": hashlib.sha256(index_bytes).hexdigest()})
                manifest = {"format": FORMAT, "created_at": datetime.now(timezone.utc).isoformat(),
                            "source_antigravity_version": _version_for_storage(storage),
                            "source_storage_root": str(storage),
                            "conversations": conversations, "files": files,
                            "notes": "Native SQLite sessions; import only into compatible Antigravity 2.x"}
                out.writestr("manifest.json", _json_bytes(manifest))
        os.chmod(staging, 0o600)
        os.replace(staging, destination)
    finally:
        if staging.exists():
            staging.unlink()
    return {"archive": str(destination), "conversations": len(conversations),
            "files": len(files) + 1, "size": destination.stat().st_size}


def _load_archive(archive: Path) -> tuple[dict, dict]:
    if not archive.is_file():
        raise NativeMigrationError(f"Archive not found: {archive}")
    with zipfile.ZipFile(archive, "r") as z:
        info = z.infolist()
        if len(info) > MAX_FILES or sum(i.file_size for i in info) > MAX_UNPACKED:
            raise NativeMigrationError("Archive exceeds safe file-count/size limits")
        names = [i.filename for i in info]
        if len(names) != len(set(names)) or any(i.is_dir() for i in info):
            raise NativeMigrationError("Duplicate or directory ZIP entries are not supported")
        for i in info:
            p = i.filename
            if (p.startswith("/") or "\\" in p or ".." in Path(p).parts
                    or ((i.external_attr >> 16) & 0o170000) == 0o120000):
                raise NativeMigrationError(f"Unsafe ZIP entry: {p}")
        manifest = json.loads(z.read("manifest.json"))
        if manifest.get("format") != FORMAT:
            raise NativeMigrationError("Not a supported native Antigravity ZIP archive")
        files = manifest.get("files", [])
        expected = {r["path"]: r for r in files}
        if len(files) != len(expected):
            raise NativeMigrationError("Repeated file paths in manifest")
        if set(names) != set(expected) | {"manifest.json"}:
            raise NativeMigrationError("Archive contents do not match its manifest")
        for filename, record in expected.items():
            actual = z.getinfo(filename)
            if actual.file_size != record["size"]:
                raise NativeMigrationError(f"Size mismatch: {filename}")
            with z.open(filename) as stream:
                if _sha256(stream) != record["sha256"]:
                    raise NativeMigrationError(f"Checksum mismatch: {filename}")
        rows = json.loads(z.read("index.json"))
        source_ids = [item["id"] for item in manifest.get("conversations", [])]
        if len(source_ids) != len(set(source_ids)) or set(source_ids) != set(rows):
            raise NativeMigrationError("Conversation IDs do not match index.json")
        for cid in source_ids:
            _check_id(cid)
            if f"conversations/{cid}.db" not in expected:
                raise NativeMigrationError(f"Missing native session database: {cid}")
            if rows[cid].get("conversation_id") != cid:
                raise NativeMigrationError(f"Summary mismatch: {cid}")
        for filename in expected:
            if filename == "index.json":
                continue
            if not any(filename == f"conversations/{cid}.db"
                       or filename == f"annotations/{cid}.pbtxt"
                       or filename.startswith(f"brain/{cid}/") for cid in source_ids):
                raise NativeMigrationError(f"File does not belong to any session: {filename}")
        return manifest, rows


def plan_native_import(archive: Path, storage: Path) -> Dict[str, Any]:
    archive = archive.expanduser().resolve()
    storage = storage.expanduser().resolve()
    manifest, rows = _load_archive(archive)
    with _db_read(storage / "conversation_summaries.db") as index:
        cols = {r[1] for r in index.execute("PRAGMA table_info(conversation_summaries)")}
        required = {r[1] for r in index.execute("PRAGMA table_info(conversation_summaries)")
                    if r[3] and r[4] is None}
        existing = {r[0] for r in index.execute("SELECT conversation_id FROM conversation_summaries")}
    imported = []
    skipped = []
    for rec in manifest["conversations"]:
        cid = rec["id"]
        incoming = set(rows[cid])
        if not required <= incoming or "raw_summary" not in cols:
            raise NativeMigrationError("Source and target summary schemas are incompatible")
        existing_paths = [storage / "conversations" / f"{cid}.db",
                          storage / "brain" / cid,
                          storage / "annotations" / f"{cid}.pbtxt"]
        item = {"id": cid, "title": rec.get("title") or f"Conversation {cid[:8]}",
                "steps": rec.get("steps", 0)}
        if cid in existing or any(p.exists() for p in existing_paths):
            skipped.append(item)
        else:
            imported.append(item)
    source_version = manifest.get("source_antigravity_version")
    target_version = _version_for_storage(storage)
    compatible = True
    warnings = ["Imported workspace/project associations may need remapping on the new machine.",
                "Close Antigravity before applying; never import live sessions."]
    src_nums, dst_nums = _version_numbers(source_version), _version_numbers(target_version)
    if src_nums and dst_nums and src_nums != dst_nums:
        warnings.append(f"Antigravity version differs: source {source_version}, target {target_version}.")
        if src_nums[0] != dst_nums[0] or src_nums > dst_nums:
            compatible = False
            warnings.append("Import blocked: the target app is older or is a different major version.")
    return {"archive": str(archive), "storage": str(storage),
            "add": imported, "skip": skipped, "total": len(rows),
            "source_version": source_version, "target_version": target_version,
            "compatible": compatible, "warnings": warnings}


def _write_exclusive(destination: Path, source, created: Optional[List[Path]] = None) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("xb") as target:
        # Only track files after we have successfully obtained exclusive creation.
        # This prevents a failed race against an existing file from deleting it.
        if created is not None:
            created.append(destination)
        shutil.copyfileobj(source, target, length=1024 * 1024)
    os.chmod(destination, 0o600)


def import_native_archive(archive: Path, storage: Path, apply: bool = False) -> Dict[str, Any]:
    """Default is preview. When applying, never replace existing sessions."""
    archive = archive.expanduser().resolve()
    storage = storage.expanduser().resolve()
    plan = plan_native_import(archive, storage)
    plan["applied"] = False
    if not apply or not plan["add"]:
        return plan
    if not plan["compatible"]:
        raise NativeMigrationError("Target Antigravity version is older than the archive source; upgrade the target or use a compatible source")
    active = _running_antigravity()
    if active:
        raise NativeMigrationError(f"Close Antigravity before importing (running: {', '.join(active)})")

    _manifest, rows = _load_archive(archive)
    add_ids = [r["id"] for r in plan["add"]]
    index_path = storage / "conversation_summaries.db"
    proto_path = storage / "agyhub_summaries_proto.pb"
    backups = storage.parent / "antigravity-import-backups"
    backups.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(backups, 0o700)
    snapshot = backups / f"preimport-{_timestamp()}-{os.getpid()}"
    snapshot.mkdir(mode=0o700)
    snapshot_index = snapshot / "conversation_summaries.db"
    _copy_sqlite_snapshot(index_path, snapshot_index)
    if proto_path.exists():
        shutil.copy2(proto_path, snapshot / proto_path.name)
    original_proto = proto_path.read_bytes() if proto_path.exists() else b""
    merged_proto = _add_proto(original_proto, rows, add_ids)

    before_ids = set()
    with _db_read(index_path) as index:
        before_ids = {r[0] for r in index.execute("SELECT conversation_id FROM conversation_summaries")}
    created = []
    modified_proto = False
    index_committed = False

    try:
        with zipfile.ZipFile(archive) as z, tempfile.TemporaryDirectory(prefix="ag-import-") as stage:
            staged = Path(stage)
            for cid in add_ids:
                with z.open(f"conversations/{cid}.db") as f:
                    local_db = staged / f"{cid}.db"
                    _write_exclusive(local_db, f)
                _verify_session_db(local_db, cid)

            for record in _manifest["files"]:
                filename = record["path"]
                if filename == "index.json":
                    continue
                if not any(filename == f"conversations/{cid}.db"
                           or filename == f"annotations/{cid}.pbtxt"
                           or filename.startswith(f"brain/{cid}/") for cid in add_ids):
                    continue
                target = storage / filename
                # refuse if a concurrent operation modified the destination
                if target.exists():
                    raise NativeMigrationError(f"Target appeared during import: {target}")
                source = staged / Path(filename).name if filename.startswith("conversations/") else None
                if source:
                    with source.open("rb") as f:
                        _write_exclusive(target, f, created)
                else:
                    with z.open(filename) as f:
                        _write_exclusive(target, f, created)

        # Cross-machine file links may still point at the source user's Brain
        # directory. Rebase *new* sessions only; never mutate existing UUIDs.
        plan["artifact_path_rebase"] = [
            rebase_new_session_brain_links(
                storage, cid, _manifest.get("source_storage_root")
            ) for cid in add_ids
        ]

        if merged_proto != original_proto:
            temp_proto = proto_path.with_name(proto_path.name + f".new-{os.getpid()}")
            try:
                with temp_proto.open("xb") as f:
                    f.write(merged_proto)
                    f.flush()
                    os.fsync(f.fileno())
                os.chmod(temp_proto, 0o600)
                os.replace(temp_proto, proto_path)
                modified_proto = True
            finally:
                if temp_proto.exists():
                    temp_proto.unlink()

        with sqlite3.connect(index_path) as target:
            target.execute("BEGIN IMMEDIATE")
            columns = [r[1] for r in target.execute("PRAGMA table_info(conversation_summaries)")]
            for cid in add_ids:
                decoded = _decode_row(rows[cid])
                fields = [col for col in columns if col in decoded]
                values = [decoded[col] for col in fields]
                sql = ("INSERT INTO conversation_summaries (" +
                       ",".join('"' + c.replace('"','""') + '"' for c in fields) +
                       ") VALUES (" + ",".join("?" for _ in fields) + ")")
                target.execute(sql, values)
            target.commit()
            index_committed = True

        with _db_read(index_path) as target:
            after = {r[0] for r in target.execute("SELECT conversation_id FROM conversation_summaries")}
            if after != before_ids | set(add_ids):
                raise NativeMigrationError("Post-import conversation index mismatch")
            if target.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise NativeMigrationError("Post-import SQLite integrity check failed")
        plan.update({"applied": True, "backup": str(snapshot),
                     "inserted": len(add_ids), "preserved_existing": len(before_ids)})
        return plan

    except Exception:
        # Undo only files this run created. Never delete or overwrite preexisting sessions.
        if index_committed:
            # The index snapshot was captured via SQLite's online backup API.
            with _db_read(snapshot_index) as original:
                with sqlite3.connect(index_path) as target:
                    original.backup(target)
        if modified_proto:
            saved = snapshot / proto_path.name
            if saved.exists():
                shutil.copy2(saved, proto_path)
            elif proto_path.exists():
                proto_path.unlink()
        for file in reversed(created):
            if file.is_file():
                file.unlink()
        for cid in add_ids:
            folder = storage / "brain" / cid
            if folder.is_dir():
                # Remove empty directories only. A different writer may have
                # created additional files; never recursively wipe them.
                directories = [p for p in folder.rglob("*") if p.is_dir()]
                for subdir in sorted(directories, key=lambda p: len(p.parts), reverse=True):
                    try:
                        subdir.rmdir()
                    except OSError:
                        pass
                try:
                    folder.rmdir()
                except OSError:
                    pass
        raise
