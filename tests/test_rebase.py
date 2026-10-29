"""Regression tests for native Brain artifact link portability."""

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from antigravity_export.native import _proto_bytes
from antigravity_export.rebase import rebase_new_session_brain_links

CID = "77777777-7777-4777-8777-777777777777"


class BrainPathRebaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ag-rebase-")
        self.storage = Path(self.temp.name) / "target"
        self.conversations = self.storage / "conversations"
        self.brain = self.storage / "brain" / CID
        self.messages = self.brain / ".system_generated" / "messages"
        self.conversations.mkdir(parents=True)
        self.messages.mkdir(parents=True)

    def tearDown(self):
        self.temp.cleanup()

    def make_db(self, url: str):
        db = sqlite3.connect(self.conversations / (CID + ".db"))
        db.execute("CREATE TABLE trajectory_meta (trajectory_id TEXT, cascade_id TEXT)")
        db.execute("INSERT INTO trajectory_meta VALUES (?,?)", ("t", CID))
        db.execute("CREATE TABLE steps (idx INTEGER PRIMARY KEY, step_payload BLOB)")
        db.execute("INSERT INTO steps VALUES (?,?)", (1, _proto_bytes(1, url.encode("utf-8"))))
        db.commit()
        db.close()

    def test_rebase_protobuf_and_logs_preserves_original_html(self):
        source_root = "/Users/long_descriptive_mac_username/.gemini/antigravity"
        original_url = source_root + "/brain/" + CID + "/artifact.html"
        unrelated = "/Users/long_descriptive_mac_username/Documents/untouched.txt"
        self.make_db("file://" + original_url + " and " + unrelated)
        (self.messages / "last.json").write_text(
            '{"artifact":"file://' + original_url + '"}', encoding="utf-8"
        )
        html = self.brain / "artifact.html"
        html.write_text("<html><body>hello</body></html>", encoding="utf-8")
        original_html = html.read_bytes()

        report = rebase_new_session_brain_links(self.storage, CID, source_root)
        self.assertEqual(report["unmapped_source_prefixes"], 0)
        self.assertGreaterEqual(report["rewritten_references"], 1)
        self.assertEqual(report["updated_generated_files"], 1)
        db = sqlite3.connect(self.conversations / (CID + ".db"))
        value = db.execute("SELECT step_payload FROM steps").fetchone()[0]
        self.assertNotIn(source_root.encode(), value)
        self.assertIn(str(self.brain).encode(), value)
        self.assertIn(unrelated.encode(), value)
        self.assertEqual(db.execute("PRAGMA quick_check").fetchone()[0], "ok")
        db.close()
        self.assertEqual(html.read_bytes(), original_html)
        self.assertNotIn(source_root, (self.messages / "last.json").read_text())
        again = rebase_new_session_brain_links(self.storage, CID, source_root)
        self.assertEqual(again["rewritten_references"], 0)

    def test_longer_target_path_rewrites_nested_protobuf_lengths(self):
        source_root = "/Users/x/.gemini/antigravity"
        uri = "file://" + source_root + "/brain/" + CID + "/artifact.html"
        dbfile = self.conversations / (CID + ".db")
        self.conversations.mkdir(exist_ok=True)
        with sqlite3.connect(dbfile) as db:
            db.execute("CREATE TABLE steps (idx INTEGER PRIMARY KEY, step_payload BLOB)")
            wrapped = _proto_bytes(1, _proto_bytes(2, _proto_bytes(5, uri.encode())))
            db.execute("INSERT INTO steps VALUES (1, ?)", (wrapped,))
        report = rebase_new_session_brain_links(self.storage, CID, source_root)
        self.assertEqual(report["rewritten_references"], 1)
        self.assertEqual(report["unmapped_source_prefixes"], 0)
        with sqlite3.connect(dbfile) as db:
            data = db.execute("SELECT step_payload FROM steps").fetchone()[0]
            self.assertEqual(db.execute("PRAGMA quick_check").fetchone()[0], "ok")
        self.assertIn(str(self.brain).encode(), data)
        self.assertNotIn(source_root.encode(), data)
        self.assertEqual(len(data), len(wrapped) + len(str(self.brain)) - len(source_root + "/brain/" + CID))

    def test_rejects_unrecognized_binary_blob(self):
        from antigravity_export.rebase import UnsafeProtobufRewrite, _rewrite_field
        source = b"file:///Users/x/.gemini/antigravity/brain/" + CID.encode() + b"/"
        target = (self.brain.as_posix() + "/").encode()
        with self.assertRaises(UnsafeProtobufRewrite):
            _rewrite_field(b"\xff\x00" + source + b"artifact.html", [(source, target)])

    def test_old_padded_linux_path_becomes_canonical(self):
        padded = str(self.brain).replace("/.gemini/", "//.gemini/")
        # The temporary directory may not be inside .gemini; add one using
        # a realistic Brain root fixture for the Linux import format.
        self.make_db("file:///home/ubuntu//.gemini/antigravity/brain/" + CID + "/artifact.html")
        dbfile = self.conversations / (CID + ".db")
        report = rebase_new_session_brain_links(
            self.storage, CID,
            "/home/ubuntu//.gemini/antigravity"
        )
        self.assertEqual(report["rewritten_references"], 1)
        with sqlite3.connect(dbfile) as db:
            payload = db.execute("SELECT step_payload FROM steps").fetchone()[0]
        self.assertIn(str(self.brain).encode(), payload)
        self.assertNotIn(b"file:///home/ubuntu//.gemini", payload)


if __name__ == "__main__":
    unittest.main()
