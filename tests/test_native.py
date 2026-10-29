"""Native import/export tests with fully synthetic Antigravity SQLite fixtures."""

import hashlib
import json
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from antigravity_export import native


FIRST = "11111111-1111-4111-8111-111111111111"
SECOND = "22222222-2222-4222-8222-222222222222"


def make_storage(storage: Path, cid: str, name: str):
    (storage / "conversations").mkdir(parents=True, exist_ok=True)
    (storage / "brain" / cid).mkdir(parents=True, exist_ok=True)
    (storage / "annotations").mkdir(parents=True, exist_ok=True)

    main = sqlite3.connect(storage / "conversation_summaries.db")
    main.execute("""CREATE TABLE IF NOT EXISTS conversation_summaries (
        conversation_id TEXT PRIMARY KEY, title TEXT NOT NULL,
        preview TEXT NOT NULL DEFAULT '', step_count INTEGER NOT NULL DEFAULT 0,
        last_modified_time TEXT NOT NULL, workspace_uris TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT '',
        project_id TEXT NOT NULL DEFAULT '', raw_summary BLOB
    )""")
    raw = f"summary:{name}".encode()
    main.execute("INSERT INTO conversation_summaries (conversation_id,title,step_count,last_modified_time,workspace_uris,raw_summary) VALUES (?,?,?,?,?,?)",
                 (cid, name, 2, "2026-10-30T00:00:00Z", "[]", raw))
    main.commit()
    main.close()

    session = sqlite3.connect(storage / "conversations" / f"{cid}.db")
    session.execute("CREATE TABLE trajectory_meta (trajectory_id TEXT, cascade_id TEXT)")
    session.execute("INSERT INTO trajectory_meta VALUES (?,?)", (f"t-{cid}", cid))
    session.execute("CREATE TABLE steps (idx INTEGER)")
    session.executemany("INSERT INTO steps VALUES (?)", [(1,), (2,)])
    session.commit()
    session.close()

    (storage / "brain" / cid / "task.md").write_text(f"Sample {name}", encoding="utf8")
    (storage / "annotations" / f"{cid}.pbtxt").write_text("annotation:test", encoding="utf8")
    pb = storage / "agyhub_summaries_proto.pb"
    pb.write_bytes(pb.read_bytes() if pb.exists() else b"" +
                   native._proto_bytes(1, native._proto_bytes(1, cid.encode()) +
                                       native._proto_bytes(2, raw)))


class NativeMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.source = root / "source"
        self.target = root / "destination"
        self.archive = root / "source.zip"
        make_storage(self.source, FIRST, "source-only")
        make_storage(self.target, SECOND, "destination-only")
        self.before = (self.target / "conversations" / f"{SECOND}.db").read_bytes()
        native.create_native_archive(self.source, self.archive)

    def tearDown(self):
        self.temp.cleanup()

    def test_preview_is_read_only(self):
        p = native.import_native_archive(self.archive, self.target)
        self.assertFalse(p["applied"])
        self.assertEqual(len(p["add"]), 1)
        self.assertEqual(len(p["skip"]), 0)
        self.assertFalse((self.target / "conversations" / f"{FIRST}.db").exists())

    def test_incremental_import_and_second_attempt(self):
        p = native.import_native_archive(self.archive, self.target, apply=True)
        self.assertTrue(p["applied"])
        self.assertEqual(p["inserted"], 1)
        self.assertEqual(p["preserved_existing"], 1)
        self.assertTrue(Path(p["backup"]).is_dir())
        self.assertEqual((self.target / "conversations" / f"{SECOND}.db").read_bytes(), self.before)
        self.assertEqual(native._verify_session_db(self.target / "conversations" / f"{FIRST}.db", FIRST), 2)
        self.assertEqual((self.target / "brain" / FIRST / "task.md").read_text(), "Sample source-only")
        self.assertEqual((self.target / "annotations" / f"{FIRST}.pbtxt").read_text(), "annotation:test")
        self.assertIn(FIRST, native._proto_ids((self.target / "agyhub_summaries_proto.pb").read_bytes()))
        con = sqlite3.connect(self.target / "conversation_summaries.db")
        self.assertEqual(con.execute("SELECT count(*) FROM conversation_summaries").fetchone()[0], 2)
        con.close()
        p2 = native.import_native_archive(self.archive, self.target, apply=True)
        self.assertEqual(len(p2["skip"]), 1)
        self.assertEqual(len(p2["add"]), 0)
        self.assertFalse(p2["applied"])

    def test_never_replace_existing_session_even_with_same_uuid(self):
        make_storage(self.target, FIRST, "destination-has-different-first")
        original = (self.target / "conversations" / f"{FIRST}.db").read_bytes()
        p = native.import_native_archive(self.archive, self.target, apply=True)
        self.assertEqual(len(p["skip"]), 1)
        self.assertFalse(p["applied"])
        self.assertEqual((self.target / "conversations" / f"{FIRST}.db").read_bytes(), original)

    def test_error_on_corrupted_archive(self):
        # Tamper with the checksummed native session, without changing manifest.
        bad = self.archive.parent / "corrupt.zip"
        with zipfile.ZipFile(self.archive) as src, zipfile.ZipFile(bad, "w") as dst:
            for name in src.namelist():
                content = src.read(name)
                if name == f"conversations/{FIRST}.db":
                    content += b"tampered"
                dst.writestr(name, content)
        with self.assertRaises(native.NativeMigrationError):
            native.import_native_archive(bad, self.target, apply=True)
        self.assertFalse((self.target / "conversations" / f"{FIRST}.db").exists())

    def test_running_process_prevents_live_write(self):
        with patch.object(native, "_running_antigravity", return_value=["language_server"]):
            with self.assertRaisesRegex(native.NativeMigrationError, "Close Antigravity"):
                native.import_native_archive(self.archive, self.target, apply=True)
        self.assertFalse((self.target / "conversations" / f"{FIRST}.db").exists())

    def test_rollback_after_partial_copy(self):
        actual = native._write_exclusive
        def fail_after_copy(dest, source, created=None):
            actual(dest, source, created)
            if created is not None:
                raise OSError("Simulated transfer failure")
        with patch.object(native, "_write_exclusive", side_effect=fail_after_copy):
            with self.assertRaisesRegex(OSError, "Simulated"):
                native.import_native_archive(self.archive, self.target, apply=True)
        self.assertFalse((self.target / "conversations" / f"{FIRST}.db").exists())
        self.assertEqual((self.target / "conversations" / f"{SECOND}.db").read_bytes(), self.before)
        con = sqlite3.connect(self.target / "conversation_summaries.db")
        self.assertEqual(con.execute("SELECT COUNT(*) FROM conversation_summaries").fetchone()[0], 1)
        con.close()

    def test_no_overwrite_original_archive(self):
        with self.assertRaises(native.NativeMigrationError):
            native.create_native_archive(self.source, self.archive)


if __name__ == "__main__":
    unittest.main()
