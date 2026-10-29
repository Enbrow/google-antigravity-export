# Native Antigravity 2.x session migration

The regular Markdown + JSON export is for reading and archiving, **not** for re-importing
into the Antigravity IDE. The native migration mode saves the underlying SQLite
conversation database files together with matching native summary rows and
per-conversation Brain artifacts into one portable ZIP.

## Web UI

Launch the tool as usual with `google-antigravity-export --web` and open the **Export**
tab. At the bottom is **Native session backup & import (Antigravity 2.x)**:

1. On the source computer, enter a local destination ZIP path and click **Create ZIP**.
2. Move that private ZIP to the target computer using your trusted file-transfer method.
3. On the target, enter the ZIP's full local path and choose **Preview import**.
4. Verify the proposed new sessions and the list of existing UUIDs being skipped.
5. **Completely quit Antigravity** on the target, then click **Import missing sessions**
   and confirm the operation. The web tool can remain open while Antigravity is closed.
6. Restart Antigravity and confirm that the imported conversations display.

The web UI rejects an import if the set of newly imported IDs changed since preview.

## CLI (macOS, Windows, Linux)

Export all available indexed native sessions:

    google-antigravity-export --native-export ~/Desktop/AntigravityNative.zip

Export only one session (repeat `--native-id` to select multiple):

    google-antigravity-export --native-export ~/Desktop/Selected.zip --native-id 11111111-1111-4111-8111-111111111111

Safely **preview** an import — this is the default, and does not modify data:

    google-antigravity-export --native-import ~/Downloads/AntigravityNative.zip

Apply only missing sessions (requires Antigravity to be completely stopped):

    google-antigravity-export --native-import ~/Downloads/AntigravityNative.zip --apply

Use `--gemini-dir /path/to/antigravity` to choose an isolated storage directory
for testing; this is strongly recommended before importing real data. A dry run
still validates ZIP checksums and compares every conversation ID.

## Local artifact path portability

Starting with 0.2.1, the importer attempts to adapt references to files stored
inside the imported session's Brain folder. It only modifies newly inserted
session records and generated transcript sidecars; the artifact files themselves
are copied unchanged. Existing destination conversations are never modified.

For Protobuf safety, the importer only adjusts references if the replacement
can preserve the original UTF-8 byte length, using redundant POSIX path
separators if necessary. Other mappings remain untouched and are reported as
unmapped prefixes. This is best-effort for macOS/Linux; external project file
paths and Windows-specific paths may still require manual handling.

## Safety guarantees and limitations

- **Add-only**: if a UUID is present in the target summary index, session file,
  Brain directory, or annotation file, it is **skipped**, not overwritten or merged.
- **Full validation before applying**: every ZIP entry must have an expected safe
  path and a matching SHA-256 digest. Native SQLite databases are checked for
  integrity and session UUID consistency before installing.
- **Process guard**: the importer refuses to run while the Antigravity executable
  or its language server is active. Close all IDE windows and background agents.
- **Automatic index snapshot**: a timestamped backup of the original destination
  `conversation_summaries.db` and `agyhub_summaries_proto.pb` is saved in
  `~/.gemini/antigravity-import-backups/` for the default storage path.
  Session files already on the target are never removed or changed.
- **Failure recovery**: if an import raises an error while installing, the importer
  restores the original target indexes and removes only files it created.
  Empty new directories are removed; pre-existing directories are never wiped.
- **Portable data**: native ZIPs contain session SQLite snapshots, corresponding
  summary records and optional Brain/annotation files. Installation IDs,
  login credentials, the user's Antigravity application configuration, global
  project registries and unrelated project source folders are **not** copied.
- **Projects may need remapping**: the source's project IDs and workspace paths
  may not exist on the target, especially across macOS and Linux.
- **Version compatibility is not guaranteed**: the 2.x native schema is
  undocumented and may change. For normal live storage, the native ZIP records
  the installed Antigravity version. Preview reports both source and target.
  Importing a **newer** source into an older target (or across major versions)
  is deliberately blocked, while other version differences are flagged.
  Upgrade the target through its normal, tested update procedure before retrying.
  If the version cannot be detected, import is not blocked by this check;
  validate compatibility manually. A session being indexed does not guarantee
  that its prior workspace, tools, or agent runtime can be resumed seamlessly.
- **Search index**: Antigravity's full-text FTS index is not forcibly rewritten.
  Imported sessions are added to the native conversation summary and binary
  summary index; text search availability may depend on Antigravity rebuilding
  its FTS index after restart.

## Restore an earlier target state

Keep the automatically generated pre-import index snapshot, along with an
independent full backup of the target storage. If rollback is needed:

1. Quit Antigravity completely.
2. Identify exactly which new UUIDs were imported from the CLI/Web report.
3. Restore both summary index files from the pre-import snapshot using SQLite's
   backup API for the database (do not copy over a live SQLite database).
4. Remove only the newly imported UUID session files and Brain/annotation data.
   Never remove a UUID that already existed before the import.
5. Restart Antigravity, then compare original UUID and step counts.

Do not upload private ZIPs or credential-bearing Antigravity state to a public
GitHub repository.

## Verification conducted on Ubuntu ARM64

- A snapshot of the real 4-session Ubuntu storage was preserved **before tests**.
- All 4 native conversations exported to a portable ZIP.
- Isolated test target: 3 existing sessions + 1 missing; dry-run detected the
  missing session, apply restored it (70 steps), original database hashes and
  summary BLOBs remained identical, and a second import skipped all 4.
- 12 synthetic regression tests passed, including simulated partial-write
  failure rollback, corrupted ZIP rejection, running-app protection, duplicate
  UUID handling and end-to-end index consistency.
- Live IDE interaction/continuation after cross-device import remains a separate
  acceptance test; this module has not been certified by Google.
