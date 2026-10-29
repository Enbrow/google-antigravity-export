"""Command-line entry point for `google-antigravity-export`."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .naming import DETAIL_CHOICES, FORMAT_CHOICES, LAYOUT_CHOICES, NAMING_CHOICES


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="google-antigravity-export",
        description=(
            "Export your Google Antigravity Brain (knowledge items, cascade artifacts, "
            "and full conversation transcripts) to clean markdown. Works on macOS, "
            "Windows, and Linux. Antigravity must be running so its local "
            "language-server API is reachable."
        ),
    )
    p.add_argument("--version", action="version", version=f"google-antigravity-export {__version__}")
    p.add_argument(
        "-o", "--output",
        type=Path,
        default=Path.cwd() / "AntigravityExport",
        help="Destination folder (default: ./AntigravityExport)",
    )
    p.add_argument(
        "--gemini-dir",
        type=Path,
        default=None,
        help="Path to .gemini/antigravity (default: ~/.gemini/antigravity)",
    )
    p.add_argument("--csrf", help="Override the auto-detected CSRF token")
    p.add_argument("--port", type=int, help="Override the auto-detected API port")
    p.add_argument("--no-knowledge", action="store_true", help="Skip knowledge items")
    p.add_argument("--no-artifacts", action="store_true", help="Skip brain artifacts")
    p.add_argument("--no-conversations", action="store_true", help="Skip conversations")
    p.add_argument(
        "--project",
        help="Only export conversations whose workspace name contains this substring",
    )
    p.add_argument(
        "--no-wipe",
        action="store_true",
        help="Don't delete the output folder before writing (default: wipe first)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Detect process/token/port and print them; don't export anything",
    )
    native = p.add_mutually_exclusive_group()
    native.add_argument(
        "--native-export", type=Path, metavar="ARCHIVE.zip",
        help="Back up native SQLite sessions to an importable ZIP (does not need the live API)",
    )
    native.add_argument(
        "--native-import", type=Path, metavar="ARCHIVE.zip",
        help="Preview additive native-session import; specify --apply to perform it",
    )
    p.add_argument("--native-id", action="append", default=[], metavar="UUID",
                   help="Include only this conversation in --native-export (repeatable)")
    p.add_argument("--apply", action="store_true",
                   help="Apply --native-import after checking that Antigravity is closed")
    p.add_argument("-v", "--verbose", action="store_true", help="Verbose discovery output")
    p.add_argument(
        "--gui",
        action="store_true",
        help=(
            "Launch the desktop GUI instead of running headless. "
            "Requires the [gui] extra: pip install google-antigravity-export[gui]"
        ),
    )
    p.add_argument(
        "--web",
        action="store_true",
        help=(
            "Launch the local web UI in your default browser. Zero extra "
            "dependencies — runs a tiny stdlib HTTP server on 127.0.0.1."
        ),
    )
    p.add_argument(
        "--web-port",
        type=int,
        default=None,
        help="Pin the --web server to a specific port (default: pick a free one).",
    )
    p.add_argument(
        "--no-open",
        action="store_true",
        help="With --web, don't auto-open the browser.",
    )

    styling = p.add_argument_group("output style")
    styling.add_argument(
        "--naming",
        choices=NAMING_CHOICES,
        default="title-date",
        help=(
            "Conversation folder/file names. "
            "title-date (default) = 'Title (Apr 24, 2026)'; "
            "date-title = '2026-04-24 - Title'; "
            "iso-title = '2026-04-24_Title_With_Underscores'; "
            "title-only = just the title; "
            "id-title = 'e92bc17e_Title' (short cascade id prefix)."
        ),
    )
    styling.add_argument(
        "--layout",
        choices=LAYOUT_CHOICES,
        default="project",
        help=(
            "Folder grouping. "
            "project (default) = conversations/<Project>/...; "
            "flat = conversations/<Title>/... (no grouping); "
            "date = conversations/YYYY-MM/..."
        ),
    )
    styling.add_argument(
        "--format",
        dest="file_format",
        choices=FORMAT_CHOICES,
        default="both",
        help="Write markdown, JSON, or both per conversation (default: both).",
    )
    styling.add_argument(
        "--detail",
        choices=DETAIL_CHOICES,
        default="full",
        help=(
            "How much to render into each markdown file. "
            "full (default) = user+agent+expandable trace (thinking, tools, commands); "
            "chat = user+agent only, no trace; "
            "thinking = user+agent+thinking, no tool noise."
        ),
    )
    styling.add_argument(
        "--no-apply-timestamps",
        action="store_true",
        help=(
            "Don't set file/folder mtime to the conversation's creation date. "
            "(Default: apply, so sorting by 'Date modified' in your file manager "
            "matches conversation order.)"
        ),
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    # Force UTF-8 on stdout/stderr so the output works on Windows consoles too.
    for stream in (sys.stdout, sys.stderr):
        reconf = getattr(stream, "reconfigure", None)
        if reconf:
            try:
                reconf(encoding="utf-8")
            except Exception:
                pass

    if args.native_export or args.native_import:
        from .native import NativeMigrationError, create_native_archive, import_native_archive
        storage = (args.gemini_dir or (Path.home() / ".gemini" / "antigravity")).expanduser()
        try:
            if args.native_export:
                if args.apply:
                    print("ERROR: --apply is only valid with --native-import", file=sys.stderr)
                    return 2
                result = create_native_archive(storage, args.native_export, args.native_id)
            else:
                if args.native_id:
                    print("ERROR: --native-id is only valid with --native-export", file=sys.stderr)
                    return 2
                result = import_native_archive(args.native_import, storage, apply=args.apply)
            import json
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        except Exception as exc:
            print(f"Native migration failed: {exc}", file=sys.stderr)
            return 2
    if args.apply or args.native_id:
        print("ERROR: --apply/--native-id require a native command", file=sys.stderr)
        return 2

    from .discover import DiscoveryError, discover
    from .export import ExportOptions, default_gemini_dir, run_export

    if args.web:
        from .web import run_web
        return run_web(port=args.web_port, open_browser=not args.no_open)

    if args.gui:
        # Lazy import so CLI-only users never pay for the GUI dep.
        try:
            from .gui import run_gui
        except SystemExit:
            raise
        except ImportError as e:
            print(
                "Cannot launch GUI: customtkinter is not installed.\n"
                "Install the GUI extra:\n\n"
                "  pip install google-antigravity-export[gui]\n\n"
                f"(underlying error: {e})",
                file=sys.stderr,
            )
            return 3
        return run_gui()

    print(f"google-antigravity-export v{__version__}")
    print("Discovering Antigravity language-server...")

    try:
        instance = discover(
            csrf_override=args.csrf,
            port_override=args.port,
            verbose=args.verbose,
        )
    except DiscoveryError as e:
        print(f"\nERROR: {e}", file=sys.stderr)
        return 2

    print(f"  -> pid {instance.pid}, port {instance.port}, csrf {instance.csrf[:8]}...")

    if args.dry_run:
        print("\n(--dry-run) Skipping export.")
        return 0

    gemini = args.gemini_dir or default_gemini_dir()
    if not gemini.exists():
        print(
            f"\nWARNING: Antigravity data dir not found at {gemini}. "
            "Knowledge/artifact copy will be skipped.",
            file=sys.stderr,
        )

    opts = ExportOptions(
        output_dir=args.output.resolve(),
        gemini_dir=gemini,
        include_knowledge=not args.no_knowledge,
        include_artifacts=not args.no_artifacts,
        include_conversations=not args.no_conversations,
        project_filter=args.project,
        wipe=not args.no_wipe,
        naming=args.naming,
        layout=args.layout,
        file_format=args.file_format,
        detail=args.detail,
        apply_timestamps=not args.no_apply_timestamps,
    )

    run_export(instance, opts)
    return 0


if __name__ == "__main__":
    sys.exit(main())
