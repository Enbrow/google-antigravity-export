"""Sleek desktop GUI (customtkinter) for google-antigravity-export.

Two tabs:

* **Export** — configure and run the bulk export (knowledge, artifacts,
  conversations) to a folder on disk.
* **Browse** — live, in-memory browser of your Antigravity conversations. List
  them sorted by date / title / project, filter by substring, click to read the
  decoded transcript, and export a single conversation without touching the
  others.

Optional — install with the ``[gui]`` extra::

    pip install google-antigravity-export[gui]

Launch with::

    google-antigravity-export --gui
"""

from __future__ import annotations

import queue
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog
from typing import Dict, List, Optional

try:
    import customtkinter as ctk
except ImportError as e:  # pragma: no cover
    raise SystemExit(
        "The GUI requires customtkinter. Install the optional extra:\n\n"
        "  pip install google-antigravity-export[gui]\n"
    ) from e

import re

from . import __version__, api
from .discover import DiscoveryError, Instance, discover
from .export import (
    ExportOptions,
    default_gemini_dir,
    export_single_conversation,
    project_from_workspaces,
    run_export,
)
from .naming import DETAIL_CHOICES, FORMAT_CHOICES, LAYOUT_CHOICES, NAMING_CHOICES
from .render import _extract_user_text, steps_to_markdown


APP_TITLE = "Google Antigravity Export"
ACCENT = "#7C5CFF"
ACCENT_HOVER = "#6949E5"
OK_GREEN = "#2EC27E"
ERR_RED = "#E5484D"
MUTED = "#8a8a92"
PANEL = "#1e1e24"
PANEL_DARK = "#141419"

NAMING_LABELS = {
    "title-date":  "Title (Apr 24, 2026)   — default",
    "date-title":  "2026-04-24 - Title",
    "iso-title":   "2026-04-24_Title_Underscores",
    "title-only":  "Title only",
    "id-title":    "e92bc17e_Title",
}
LAYOUT_LABELS = {
    "project": "By Project   — default",
    "flat":    "Flat (no grouping)",
    "date":    "By Month (YYYY-MM)",
}
FORMAT_LABELS = {
    "both":     "Markdown + JSON   — default",
    "markdown": "Markdown only",
    "json":     "JSON only",
}
DETAIL_LABELS = {
    "full":     "Full — user, agent, thinking, tools   — default",
    "chat":     "Chat — user and agent only",
    "thinking": "Thinking — user, agent, thinking only",
}

SORT_OPTIONS = (
    "Date  (newest first)",
    "Date  (oldest first)",
    "Title  (A → Z)",
    "Project  (A → Z)",
)

ALL_PROJECTS = "(All projects)"


def _label_to_key(mapping: dict, label: str) -> str:
    for k, v in mapping.items():
        if v == label:
            return k
    return next(iter(mapping))


def _open_folder(path: Path) -> None:
    p = str(path)
    if sys.platform.startswith("win"):
        subprocess.Popen(["explorer", p])
    elif sys.platform == "darwin":
        subprocess.Popen(["open", p])
    else:
        subprocess.Popen(["xdg-open", p])


def _short_date(iso: str) -> str:
    return (iso or "")[:10] or "????-??-??"


def _build_preview_markdown(rec: "ConvRecord", steps: list) -> str:
    """Build the in-app viewer markdown — the face-value conversation only.

    Shows user messages (USER_INPUT) and every visible agent reply: both
    planner-response text (`modifiedResponse` / `response`) and user-facing
    notifications (NOTIFY_USER). Thinking, tool calls, file edits, and other
    trace steps are hidden from the preview so the reader stays skimmable.
    """
    out: List[str] = [f"# {rec.title}", ""]
    meta_bits = []
    if rec.created:
        meta_bits.append(_short_date(rec.created))
    if rec.project:
        meta_bits.append(rec.project)
    meta_bits.append(f"{rec.step_count} steps")
    if meta_bits:
        out.append("*" + "  ·  ".join(meta_bits) + "*")
    out.append("")
    out.append("---")

    any_user = False
    any_agent = False
    for step in steps:
        t = step.get("type", "")
        if t == "CORTEX_STEP_TYPE_USER_INPUT":
            text = _extract_user_text(step)
            if text:
                any_user = True
                out.append("")
                out.append("## 👤 USER")
                out.append("")
                out.append(text.strip())
        elif t == "CORTEX_STEP_TYPE_PLANNER_RESPONSE":
            pr = step.get("plannerResponse", {})
            reply = (
                pr.get("modifiedResponse")
                or pr.get("response")
                or pr.get("text", "")
            )
            if reply and reply.strip():
                any_agent = True
                out.append("")
                out.append("## 🤖 AGENT")
                out.append("")
                out.append(reply.strip())
        elif t == "CORTEX_STEP_TYPE_NOTIFY_USER":
            nu = step.get("notifyUser", {})
            content = (nu.get("notificationContent", "") or "").strip()
            if content:
                any_agent = True
                out.append("")
                out.append("## 🤖 AGENT")
                out.append("")
                out.append(content)

    if not (any_user or any_agent):
        out.append("")
        out.append("*No user messages or agent replies were found in this conversation.*")
    return "\n".join(out)


@dataclass
class ConvRecord:
    cid: str
    title: str
    project: str
    created: str
    modified: str
    step_count: int
    summary: dict  # raw summary from API (for export_single_conversation)


class ExportApp(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()

        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("dark-blue")

        self.title(APP_TITLE)
        self.geometry("1320x940")
        self.minsize(1040, 740)

        self._log_queue: "queue.Queue[str]" = queue.Queue()
        self._instance: Optional[Instance] = None
        self._output_path: Optional[Path] = None
        self._export_thread: Optional[threading.Thread] = None

        self._records: List[ConvRecord] = []
        self._filtered: List[ConvRecord] = []
        self._selected_cid: Optional[str] = None
        # Per-cid references so selection can be updated in place (no rebuild).
        self._conv_rows: Dict[str, ctk.CTkFrame] = {}
        self._conv_buttons: Dict[str, ctk.CTkButton] = {}
        self._viewer_steps: Optional[list] = None  # cached steps for selected

        # Shared state vars (shared between Export and Browse tabs)
        self.output_var = ctk.StringVar(value=str(Path.cwd() / "AntigravityExport"))
        self.inc_conversations = ctk.BooleanVar(value=True)
        self.inc_knowledge = ctk.BooleanVar(value=True)
        self.inc_artifacts = ctk.BooleanVar(value=True)
        self.project_var = ctk.StringVar(value=ALL_PROJECTS)
        self.apply_ts_var = ctk.BooleanVar(value=True)
        self.wipe_var = ctk.BooleanVar(value=True)
        self.naming_var = ctk.StringVar(value=NAMING_LABELS["title-date"])
        self.layout_var = ctk.StringVar(value=LAYOUT_LABELS["project"])
        self.format_var = ctk.StringVar(value=FORMAT_LABELS["both"])
        self.detail_var = ctk.StringVar(value=DETAIL_LABELS["full"])

        # Browse-tab vars
        self.sort_var = ctk.StringVar(value=SORT_OPTIONS[0])
        self.filter_var = ctk.StringVar(value="")
        self.filter_var.trace_add("write", lambda *_: self._apply_browse_filter())

        self._build_header()
        self._build_tabs()
        self._build_progress()
        self._build_log()
        self._build_footer()

        self.after(100, self._pump_log)
        self.after(200, self._start_detection)

    # ─── header ─────────────────────────────────────────────────────────────

    def _build_header(self) -> None:
        header = ctk.CTkFrame(self, corner_radius=0, fg_color="#1a1a1f")
        header.pack(side="top", fill="x")

        title = ctk.CTkLabel(header, text=APP_TITLE,
                             font=ctk.CTkFont(size=20, weight="bold"))
        title.pack(side="left", padx=20, pady=14)

        version = ctk.CTkLabel(header, text=f"v{__version__}", text_color=MUTED,
                               font=ctk.CTkFont(size=12))
        version.pack(side="left", pady=14)

        self.status_dot = ctk.CTkLabel(header, text="●", text_color="#bfbfc7",
                                       font=ctk.CTkFont(size=16))
        self.status_dot.pack(side="right", padx=(0, 20), pady=14)
        self.status_label = ctk.CTkLabel(header, text="Detecting Antigravity…",
                                         text_color="#bfbfc7",
                                         font=ctk.CTkFont(size=13))
        self.status_label.pack(side="right", padx=(0, 8), pady=14)

        self.refresh_btn = ctk.CTkButton(
            header, text="↻ Refresh", width=90,
            fg_color="transparent", border_width=1, border_color="#3a3a42",
            hover_color="#2a2a30", command=self._start_detection,
        )
        self.refresh_btn.pack(side="right", padx=(0, 12), pady=10)

    # ─── tabs ───────────────────────────────────────────────────────────────

    def _build_tabs(self) -> None:
        self.tabs = ctk.CTkTabview(
            self, fg_color="transparent",
            segmented_button_selected_color=ACCENT,
            segmented_button_selected_hover_color=ACCENT_HOVER,
        )
        self.tabs.pack(side="top", fill="both", expand=True, padx=16, pady=(12, 0))
        self.tabs.add("Export")
        self.tabs.add("Browse")
        self._build_export_tab(self.tabs.tab("Export"))
        self._build_browse_tab(self.tabs.tab("Browse"))

    # ─── Export tab ─────────────────────────────────────────────────────────

    def _build_export_tab(self, parent: ctk.CTkFrame) -> None:
        container = ctk.CTkScrollableFrame(parent, fg_color="transparent")
        container.pack(side="top", fill="both", expand=True)

        self._section(container, "Output")
        out_row = ctk.CTkFrame(container, fg_color="transparent")
        out_row.pack(fill="x", pady=(0, 14))
        ctk.CTkEntry(out_row, textvariable=self.output_var, height=36).pack(
            side="left", fill="x", expand=True, padx=(0, 8))
        ctk.CTkButton(out_row, text="Browse…", width=100,
                      command=self._choose_output).pack(side="left")

        self._section(container, "What to export")
        scope = ctk.CTkFrame(container, fg_color="transparent")
        scope.pack(fill="x", pady=(0, 14))
        for text, var in (
            ("Conversations",   self.inc_conversations),
            ("Knowledge items", self.inc_knowledge),
            ("Brain artifacts", self.inc_artifacts),
        ):
            ctk.CTkCheckBox(scope, text=text, variable=var,
                            hover_color=ACCENT_HOVER, fg_color=ACCENT
                            ).pack(side="left", padx=(0, 20))

        # Project dropdown — populated after detection.
        proj_row = ctk.CTkFrame(container, fg_color="transparent")
        proj_row.pack(fill="x", pady=(0, 14))
        ctk.CTkLabel(proj_row, text="Include projects:", width=170,
                     anchor="w").pack(side="left")
        self.project_menu = ctk.CTkOptionMenu(
            proj_row, variable=self.project_var, values=[ALL_PROJECTS],
            fg_color=PANEL, button_color=ACCENT, button_hover_color=ACCENT_HOVER,
            dropdown_fg_color=PANEL,
        )
        self.project_menu.pack(side="left", fill="x", expand=True)

        self._section(container, "Output style")
        grid = ctk.CTkFrame(container, fg_color="transparent")
        grid.pack(fill="x", pady=(0, 14))
        grid.grid_columnconfigure(1, weight=1)

        def _opt_row(row: int, label: str, var: ctk.StringVar, labels: dict) -> None:
            ctk.CTkLabel(grid, text=label, anchor="w", width=170
                         ).grid(row=row, column=0, sticky="w", pady=4)
            ctk.CTkOptionMenu(
                grid, variable=var, values=list(labels.values()),
                fg_color=PANEL, button_color=ACCENT,
                button_hover_color=ACCENT_HOVER, dropdown_fg_color=PANEL,
            ).grid(row=row, column=1, sticky="ew", pady=4)

        _opt_row(0, "Naming convention:", self.naming_var, NAMING_LABELS)
        _opt_row(1, "Folder layout:",     self.layout_var, LAYOUT_LABELS)
        _opt_row(2, "File format:",       self.format_var, FORMAT_LABELS)
        _opt_row(3, "Detail level:",      self.detail_var, DETAIL_LABELS)

        ctk.CTkCheckBox(container,
                        text="Stamp file dates to conversation creation time",
                        variable=self.apply_ts_var,
                        hover_color=ACCENT_HOVER, fg_color=ACCENT
                        ).pack(anchor="w", pady=(0, 4))
        ctk.CTkCheckBox(container,
                        text="Wipe output folder before writing",
                        variable=self.wipe_var,
                        hover_color=ACCENT_HOVER, fg_color=ACCENT
                        ).pack(anchor="w", pady=(0, 8))

    # ─── Browse tab ─────────────────────────────────────────────────────────

    def _build_browse_tab(self, parent: ctk.CTkFrame) -> None:
        # Toolbar (sort + filter)
        toolbar = ctk.CTkFrame(parent, fg_color="transparent")
        toolbar.pack(side="top", fill="x", pady=(0, 8))

        ctk.CTkLabel(toolbar, text="Sort:", text_color=MUTED
                     ).pack(side="left", padx=(0, 6))
        ctk.CTkOptionMenu(
            toolbar, variable=self.sort_var, values=list(SORT_OPTIONS),
            fg_color=PANEL, button_color=ACCENT,
            button_hover_color=ACCENT_HOVER, dropdown_fg_color=PANEL,
            width=180, command=lambda _: self._apply_browse_filter(),
        ).pack(side="left", padx=(0, 16))

        ctk.CTkLabel(toolbar, text="Filter:", text_color=MUTED
                     ).pack(side="left", padx=(0, 6))
        ctk.CTkEntry(
            toolbar, textvariable=self.filter_var, height=30,
            placeholder_text="type to filter by title or project…",
        ).pack(side="left", fill="x", expand=True, padx=(0, 8))

        self.browse_count_lbl = ctk.CTkLabel(
            toolbar, text="0 conversations", text_color=MUTED,
            font=ctk.CTkFont(size=11),
        )
        self.browse_count_lbl.pack(side="right")

        # Split: list (left) | viewer (right)
        split = ctk.CTkFrame(parent, fg_color="transparent")
        split.pack(side="top", fill="both", expand=True)

        left = ctk.CTkFrame(split, fg_color=PANEL_DARK, corner_radius=8)
        left.pack(side="left", fill="y", padx=(0, 8))
        left.configure(width=360)
        left.pack_propagate(False)

        self.list_frame = ctk.CTkScrollableFrame(
            left, fg_color="transparent", label_text="",
        )
        self.list_frame.pack(fill="both", expand=True, padx=6, pady=6)

        right = ctk.CTkFrame(split, fg_color="transparent")
        right.pack(side="left", fill="both", expand=True)

        # Viewer header
        head = ctk.CTkFrame(right, fg_color=PANEL, corner_radius=8)
        head.pack(side="top", fill="x")
        self.viewer_title = ctk.CTkLabel(
            head, text="Select a conversation to read",
            font=ctk.CTkFont(size=14, weight="bold"), anchor="w",
        )
        self.viewer_title.pack(side="left", padx=12, pady=(10, 0), fill="x", expand=True)
        self.viewer_meta = ctk.CTkLabel(
            head, text="", text_color=MUTED, anchor="w",
            font=ctk.CTkFont(size=11),
        )
        self.viewer_meta.pack(side="left", padx=12, pady=(0, 10))

        self.export_one_btn = ctk.CTkButton(
            head, text="Export this conversation", width=210, height=32,
            fg_color=ACCENT, hover_color=ACCENT_HOVER,
            font=ctk.CTkFont(size=12, weight="bold"),
            command=self._export_selected,
            state="disabled",
        )
        self.export_one_btn.pack(side="right", padx=10, pady=10)

        # Viewer body — roomy reader with markdown rendering via text tags.
        self.viewer_box = ctk.CTkTextbox(
            right, fg_color=PANEL_DARK, text_color="#e4e4eb",
            font=ctk.CTkFont(family="Segoe UI", size=14), wrap="word",
            padx=18, pady=14,
        )
        self.viewer_box.pack(fill="both", expand=True, pady=(8, 0))
        self._setup_viewer_tags()
        self.viewer_box.configure(state="disabled")

        # Claim the mousewheel when the cursor is over the viewer. CTk's
        # ScrollableFrame uses bind_all for <MouseWheel>, which steals wheel
        # events from sibling widgets — this makes the viewer scroll snap
        # back instead of moving. Binding directly on the inner tk.Text and
        # returning "break" stops the event from bubbling up.
        inner = self.viewer_box._textbox  # type: ignore[attr-defined]
        def _on_wheel(event):
            inner.yview_scroll(int(-event.delta / 60), "units")
            return "break"
        inner.bind("<MouseWheel>", _on_wheel)
        inner.bind("<Shift-MouseWheel>", _on_wheel)

    def _setup_viewer_tags(self) -> None:
        # CTkTextbox wraps a tk.Text and blocks font= on tag_config so its own
        # scaling can own the font. Reach the inner tk.Text to apply per-tag
        # fonts directly.
        inner = self.viewer_box._textbox  # type: ignore[attr-defined]
        UI = "Segoe UI"
        MONO = "Consolas"
        inner.tag_config("h1",     foreground="#cfc2ff",
                         font=(UI, 20, "bold"), spacing1=10, spacing3=8)
        inner.tag_config("h2",     foreground=ACCENT,
                         font=(UI, 16, "bold"), spacing1=12, spacing3=6)
        inner.tag_config("h3",     foreground="#bfbfc7",
                         font=(UI, 13, "bold"), spacing1=8, spacing3=4)
        inner.tag_config("user",   foreground="#7ad0ff",
                         font=(UI, 14, "bold"), spacing1=14, spacing3=6)
        inner.tag_config("agent",  foreground="#9cf0c4",
                         font=(UI, 14, "bold"), spacing1=14, spacing3=6)
        inner.tag_config("body",
                         font=(UI, 14), spacing3=4, lmargin1=4, lmargin2=4)
        inner.tag_config("bold",
                         font=(UI, 14, "bold"))
        inner.tag_config("italic", foreground="#bfbfc7",
                         font=(UI, 14, "italic"))
        inner.tag_config("code_inline", foreground="#ffcf8f",
                         font=(MONO, 13), background="#22222a")
        inner.tag_config("code_block",  foreground="#dcdce3",
                         font=(MONO, 12), background="#101015",
                         lmargin1=14, lmargin2=14, spacing1=6, spacing3=6)
        inner.tag_config("quote",  foreground=MUTED,
                         font=(UI, 13, "italic"), lmargin1=16, lmargin2=16)
        inner.tag_config("rule",   foreground="#2a2a30")
        inner.tag_config("li",
                         font=(UI, 14), lmargin1=18, lmargin2=32, spacing3=2)
        inner.tag_config("meta",   foreground=MUTED,
                         font=(UI, 11), spacing3=10)

    # ─── progress + log + footer ────────────────────────────────────────────

    def _build_progress(self) -> None:
        wrap = ctk.CTkFrame(self, fg_color="transparent")
        wrap.pack(side="top", fill="x", padx=16, pady=(8, 0))

        row = ctk.CTkFrame(wrap, fg_color="transparent")
        row.pack(fill="x")
        self.progress_phase = ctk.CTkLabel(
            row, text="Idle", anchor="w",
            font=ctk.CTkFont(size=12, weight="bold"), text_color="#bfbfc7",
        )
        self.progress_phase.pack(side="left")
        self.progress_count = ctk.CTkLabel(
            row, text="", anchor="e",
            font=ctk.CTkFont(size=11), text_color=MUTED,
        )
        self.progress_count.pack(side="right")

        self.progress_bar = ctk.CTkProgressBar(
            wrap, height=10, progress_color=ACCENT, fg_color=PANEL,
        )
        self.progress_bar.pack(fill="x", pady=(6, 2))
        self.progress_bar.set(0)

        self.progress_detail = ctk.CTkLabel(
            wrap, text="", anchor="w",
            font=ctk.CTkFont(size=11), text_color=MUTED,
        )
        self.progress_detail.pack(fill="x", pady=(0, 4))

    def _build_log(self) -> None:
        log_wrap = ctk.CTkFrame(self, fg_color="transparent")
        log_wrap.pack(side="top", fill="x", padx=16, pady=(0, 8))
        ctk.CTkLabel(
            log_wrap, text="Log", anchor="w",
            font=ctk.CTkFont(size=11, weight="bold"), text_color=MUTED,
        ).pack(anchor="w", pady=(6, 4))
        self.log_box = ctk.CTkTextbox(
            log_wrap, fg_color=PANEL_DARK, text_color="#dcdce3",
            font=ctk.CTkFont(family="Consolas", size=11), wrap="word",
            height=90,
        )
        self.log_box.pack(fill="x")
        self.log_box.configure(state="disabled")

    def _build_footer(self) -> None:
        footer = ctk.CTkFrame(self, fg_color="#1a1a1f", corner_radius=0, height=64)
        footer.pack(side="bottom", fill="x")
        footer.pack_propagate(False)

        self.open_btn = ctk.CTkButton(
            footer, text="Open output folder", width=180, height=36,
            fg_color="transparent", border_width=1, border_color="#3a3a42",
            hover_color="#2a2a30", command=self._open_output, state="disabled",
        )
        self.open_btn.pack(side="left", padx=14, pady=14)

        self.run_btn = ctk.CTkButton(
            footer, text="Export", width=160, height=36,
            fg_color=ACCENT, hover_color=ACCENT_HOVER,
            font=ctk.CTkFont(size=14, weight="bold"),
            command=self._run_export, state="disabled",
        )
        self.run_btn.pack(side="right", padx=14, pady=14)

    def _section(self, parent, text: str) -> None:
        ctk.CTkLabel(
            parent, text=text.upper(), anchor="w",
            font=ctk.CTkFont(size=11, weight="bold"), text_color=MUTED,
        ).pack(anchor="w", pady=(4, 6))

    # ─── log / status plumbing ─────────────────────────────────────────────

    def _log(self, msg: str) -> None:
        self._log_queue.put(msg)

    def _pump_log(self) -> None:
        try:
            while True:
                msg = self._log_queue.get_nowait()
                self.log_box.configure(state="normal")
                self.log_box.insert("end", msg + "\n")
                self.log_box.see("end")
                self.log_box.configure(state="disabled")
        except queue.Empty:
            pass
        self.after(100, self._pump_log)

    def _set_status(self, ok: bool, text: str) -> None:
        self.status_dot.configure(text="●", text_color=OK_GREEN if ok else ERR_RED)
        self.status_label.configure(text=text)
        self.run_btn.configure(state="normal" if ok else "disabled")

    # ─── detection + summary loading ───────────────────────────────────────

    def _start_detection(self) -> None:
        self._set_status(False, "Detecting…")
        self.status_dot.configure(text_color="#bfbfc7")
        threading.Thread(target=self._detect_worker, daemon=True).start()

    def _detect_worker(self) -> None:
        try:
            instance = discover()
        except DiscoveryError as e:
            self._instance = None
            self.after(0, lambda: self._set_status(False, "Antigravity not reachable"))
            self._log(f"Detection failed: {e}")
            return
        self._instance = instance
        text = f"Connected — pid {instance.pid}, port {instance.port}"
        self.after(0, lambda: self._set_status(True, text))
        self._log(f"Detected {instance.process_name} (pid={instance.pid}, port={instance.port}).")
        # Kick off summary fetch so both tabs can populate.
        threading.Thread(target=self._load_summaries_worker, daemon=True).start()

    def _load_summaries_worker(self) -> None:
        if not self._instance:
            return
        try:
            self._log("Loading conversation list…")
            summaries = api.list_trajectories(self._instance)
        except Exception as e:
            self._log(f"Failed to load conversations: {e}")
            return

        records: List[ConvRecord] = []
        for cid, info in summaries.items():
            title = (info.get("summary") or f"Conversation {cid[:8]}").strip() or "Untitled"
            records.append(ConvRecord(
                cid=cid,
                title=title,
                project=project_from_workspaces(info) or "Playground",
                created=info.get("createdTime", "") or "",
                modified=info.get("lastModifiedTime", "") or "",
                step_count=info.get("stepCount", 0) or 0,
                summary=info,
            ))
        self._records = records
        self._log(f"Loaded {len(records)} conversations.")
        self.after(0, self._on_summaries_loaded)

    def _on_summaries_loaded(self) -> None:
        # Populate project dropdown with distinct projects (+ counts).
        counts: Dict[str, int] = {}
        for r in self._records:
            counts[r.project] = counts.get(r.project, 0) + 1
        projects = sorted(counts.keys(), key=lambda s: s.lower())
        values = [ALL_PROJECTS] + [f"{p}  ({counts[p]})" for p in projects]
        self.project_menu.configure(values=values)
        if self.project_var.get() not in values:
            self.project_var.set(ALL_PROJECTS)

        self._apply_browse_filter()

    # ─── browse list ────────────────────────────────────────────────────────

    def _sorted_records(self) -> List[ConvRecord]:
        key = self.sort_var.get()
        recs = list(self._records)
        if key == SORT_OPTIONS[0]:  # Date newest first
            recs.sort(key=lambda r: r.created or "", reverse=True)
        elif key == SORT_OPTIONS[1]:  # Date oldest first
            recs.sort(key=lambda r: r.created or "zzz")
        elif key == SORT_OPTIONS[2]:  # Title A-Z
            recs.sort(key=lambda r: r.title.lower())
        elif key == SORT_OPTIONS[3]:  # Project A-Z, then date desc
            recs.sort(key=lambda r: (r.project.lower(), -(len(r.created or ""))))
            recs.sort(key=lambda r: (r.project.lower(), r.created or ""), reverse=False)
        return recs

    def _apply_browse_filter(self) -> None:
        needle = self.filter_var.get().strip().lower()
        recs = self._sorted_records()
        if needle:
            recs = [
                r for r in recs
                if needle in r.title.lower() or needle in r.project.lower()
            ]
        self._filtered = recs
        self._rebuild_browse_list()

    def _rebuild_browse_list(self) -> None:
        for child in self.list_frame.winfo_children():
            child.destroy()
        self._conv_rows.clear()
        self._conv_buttons.clear()

        self.browse_count_lbl.configure(
            text=f"{len(self._filtered)} of {len(self._records)} conversations"
        )

        for rec in self._filtered:
            self._add_list_row(rec)

    def _add_list_row(self, rec: ConvRecord) -> None:
        selected = (rec.cid == self._selected_cid)
        row = ctk.CTkFrame(
            self.list_frame,
            fg_color=ACCENT if selected else PANEL,
            corner_radius=6,
        )
        row.pack(fill="x", pady=2, padx=0)

        inner = ctk.CTkButton(
            row, text="", anchor="w",
            fg_color="transparent", hover_color=ACCENT_HOVER,
            corner_radius=6, height=48,
            command=lambda r=rec: self._on_select(r),
        )
        # Render multi-line label via compound text.
        title = rec.title if len(rec.title) <= 60 else rec.title[:57] + "…"
        sub = f"{_short_date(rec.created)}  ·  {rec.project}  ·  {rec.step_count} steps"
        inner.configure(text=f"{title}\n{sub}",
                        font=ctk.CTkFont(size=12))
        inner.pack(fill="x", padx=4, pady=4)
        self._conv_rows[rec.cid] = row
        self._conv_buttons[rec.cid] = inner

    def _update_selection_styles(self, prev_cid: Optional[str], new_cid: str) -> None:
        """Swap selection highlight in place — avoids rebuilding the list,
        which caused stale mousewheel bindings and laggy scroll on the viewer."""
        if prev_cid and prev_cid in self._conv_rows:
            try:
                self._conv_rows[prev_cid].configure(fg_color=PANEL)
            except Exception:
                pass
        if new_cid in self._conv_rows:
            try:
                self._conv_rows[new_cid].configure(fg_color=ACCENT)
            except Exception:
                pass

    # ─── viewer ────────────────────────────────────────────────────────────

    def _on_select(self, rec: ConvRecord) -> None:
        prev = self._selected_cid
        self._selected_cid = rec.cid
        # In-place selection swap (cheap). Full rebuild was causing scroll-
        # bounce in the viewer because CTkScrollableFrame rebinds mousewheel
        # on every child-destroy/create cycle.
        self._update_selection_styles(prev, rec.cid)

        self.viewer_title.configure(text=rec.title)
        self.viewer_meta.configure(
            text=f"{_short_date(rec.created)}   ·   {rec.project}   ·   "
                 f"{rec.step_count} steps   ·   id {rec.cid[:8]}"
        )
        self._set_viewer_plain("Loading conversation…")
        self.export_one_btn.configure(state="disabled")
        threading.Thread(
            target=self._load_conversation_worker, args=(rec,), daemon=True
        ).start()

    def _load_conversation_worker(self, rec: ConvRecord) -> None:
        if not self._instance:
            return
        try:
            steps = api.get_trajectory_steps(self._instance, rec.cid)
        except Exception as e:
            self.after(0, lambda: self._set_viewer_plain(f"Failed to load: {e}"))
            return
        if self._selected_cid != rec.cid:
            return
        md = _build_preview_markdown(rec, steps)
        self._viewer_steps = steps
        self.after(0, lambda: self._render_markdown(md))
        self.after(0, lambda: self.export_one_btn.configure(state="normal"))

    def _set_viewer_plain(self, text: str) -> None:
        self.viewer_box.configure(state="normal")
        self.viewer_box.delete("1.0", "end")
        self.viewer_box.insert("1.0", text, ("body",))
        self.viewer_box.configure(state="disabled")

    def _render_markdown(self, md: str) -> None:
        tb = self.viewer_box
        tb.configure(state="normal")
        tb.delete("1.0", "end")

        # Collect (text, tag-tuple) chunks first, then flush in batches grouped
        # by tag. Fewer tb.insert() calls = drastically less layout churn,
        # which is what was making the 40-KB preview scroll-bounce.
        chunks: List[tuple] = []
        in_code = False
        for line in md.split("\n"):
            if line.startswith("```"):
                in_code = not in_code
                chunks.append(("\n", ()))
                continue
            if in_code:
                chunks.append((line + "\n", ("code_block",)))
                continue

            stripped = line.rstrip()
            if stripped == "":
                chunks.append(("\n", ()))
                continue
            if stripped == "---":
                chunks.append(("─" * 60 + "\n", ("rule",)))
                continue
            if stripped.startswith("# "):
                chunks.append((stripped[2:] + "\n", ("h1",)))
                continue
            if stripped.startswith("## 👤"):
                chunks.append(("👤  " + stripped[4:].lstrip() + "\n", ("user",)))
                continue
            if stripped.startswith("## 🤖"):
                chunks.append(("🤖  " + stripped[4:].lstrip() + "\n", ("agent",)))
                continue
            if stripped.startswith("## "):
                chunks.append((stripped[3:] + "\n", ("h2",)))
                continue
            if stripped.startswith("### "):
                chunks.append((stripped[4:] + "\n", ("h3",)))
                continue
            if stripped.startswith("> "):
                chunks.append((stripped[2:] + "\n", ("quote",)))
                continue
            if stripped.startswith("*") and stripped.endswith("*") and not stripped.startswith("**"):
                chunks.append((stripped.strip("*") + "\n", ("meta",)))
                continue
            if stripped.startswith("- ") or stripped.startswith("* "):
                self._collect_inline(chunks, "  •  " + stripped[2:] + "\n", "li")
                continue
            self._collect_inline(chunks, line + "\n", "body")

        # Merge consecutive same-tag chunks then insert.
        merged: List[tuple] = []
        for text, tag in chunks:
            if merged and merged[-1][1] == tag:
                merged[-1] = (merged[-1][0] + text, tag)
            else:
                merged.append((text, tag))
        for text, tag in merged:
            tb.insert("end", text, tag)

        tb.yview_moveto(0.0)
        tb.configure(state="disabled")

    _INLINE_RE = re.compile(r"(\*\*[^*]+\*\*|`[^`]+`)")

    def _collect_inline(self, chunks: list, text: str, base_tag: str) -> None:
        pos = 0
        for m in self._INLINE_RE.finditer(text):
            if m.start() > pos:
                chunks.append((text[pos:m.start()], (base_tag,)))
            token = m.group(0)
            if token.startswith("`"):
                chunks.append((token.strip("`"), ("code_inline",)))
            else:
                chunks.append((token.strip("*"), ("bold", base_tag)))
            pos = m.end()
        if pos < len(text):
            chunks.append((text[pos:], (base_tag,)))

    # ─── export actions ────────────────────────────────────────────────────

    def _build_opts(self) -> ExportOptions:
        # Strip "  (count)" suffix from project dropdown selection.
        project_choice = self.project_var.get()
        if project_choice == ALL_PROJECTS:
            project_filter = None
        else:
            project_filter = project_choice.split("  (")[0]

        out = Path(self.output_var.get()).expanduser().resolve()
        self._output_path = out

        return ExportOptions(
            output_dir=out,
            gemini_dir=default_gemini_dir(),
            include_knowledge=self.inc_knowledge.get(),
            include_artifacts=self.inc_artifacts.get(),
            include_conversations=self.inc_conversations.get(),
            project_filter=project_filter,
            wipe=self.wipe_var.get(),
            naming=_label_to_key(NAMING_LABELS, self.naming_var.get()),
            layout=_label_to_key(LAYOUT_LABELS, self.layout_var.get()),
            file_format=_label_to_key(FORMAT_LABELS, self.format_var.get()),
            detail=_label_to_key(DETAIL_LABELS, self.detail_var.get()),
            apply_timestamps=self.apply_ts_var.get(),
        )

    def _choose_output(self) -> None:
        initial = Path(self.output_var.get()).parent
        if not initial.exists():
            initial = Path.home()
        chosen = filedialog.askdirectory(initialdir=str(initial),
                                         title="Choose output folder")
        if chosen:
            self.output_var.set(chosen)

    def _open_output(self) -> None:
        if self._output_path and self._output_path.exists():
            _open_folder(self._output_path)

    def _run_export(self) -> None:
        if not self._instance:
            self._log("Cannot export — Antigravity not detected. Click ↻ Refresh.")
            return
        if self._export_thread and self._export_thread.is_alive():
            return

        opts = self._build_opts()

        self.run_btn.configure(state="disabled", text="Exporting…")
        self.open_btn.configure(state="disabled")
        self.export_one_btn.configure(state="disabled")
        self._reset_progress()
        self._log("")
        self._log(f"Starting export to {opts.output_dir}")

        def worker() -> None:
            try:
                run_export(self._instance, opts,
                           log=self._log, progress=self._on_progress)
                self.after(0, self._export_done)
            except Exception as e:
                self._log(f"ERROR: {e}")
                self.after(0, lambda: self.run_btn.configure(state="normal", text="Export"))
                self.after(0, lambda: self.progress_phase.configure(text="Failed", text_color=ERR_RED))

        self._export_thread = threading.Thread(target=worker, daemon=True)
        self._export_thread.start()

    def _export_selected(self) -> None:
        if not self._instance or not self._selected_cid:
            return
        rec = next((r for r in self._records if r.cid == self._selected_cid), None)
        if not rec:
            return
        if self._export_thread and self._export_thread.is_alive():
            return

        opts = self._build_opts()
        # For a single-conversation export, don't wipe the output dir and don't
        # force a full project tree — just drop the one conversation folder in.
        opts.wipe = False
        self._output_path = opts.output_dir

        self.run_btn.configure(state="disabled")
        self.open_btn.configure(state="disabled")
        self.export_one_btn.configure(state="disabled", text="Exporting…")
        self._reset_progress()
        self._log("")
        self._log(f"Exporting single conversation: {rec.title[:60]}")

        def worker() -> None:
            try:
                export_single_conversation(
                    self._instance, rec.cid, rec.summary, opts,
                    log=self._log, progress=self._on_progress,
                )
                self.after(0, self._single_export_done)
            except Exception as e:
                self._log(f"ERROR: {e}")
                self.after(0, lambda: self.export_one_btn.configure(
                    state="normal", text="Export this conversation"))
                self.after(0, lambda: self.run_btn.configure(state="normal"))
                self.after(0, lambda: self.progress_phase.configure(
                    text="Failed", text_color=ERR_RED))

        self._export_thread = threading.Thread(target=worker, daemon=True)
        self._export_thread.start()

    def _export_done(self) -> None:
        self.run_btn.configure(state="normal", text="Export")
        self.open_btn.configure(state="normal")
        self.export_one_btn.configure(
            state="normal" if self._selected_cid else "disabled")

    def _single_export_done(self) -> None:
        self.run_btn.configure(state="normal")
        self.open_btn.configure(state="normal")
        self.export_one_btn.configure(state="normal", text="Export this conversation")

    # ─── progress ───────────────────────────────────────────────────────────

    _PHASE_TITLES = {
        "init":          "Preparing",
        "knowledge":     "Knowledge items",
        "artifacts":     "Brain artifacts",
        "conversations": "Conversations",
        "finalizing":    "Finalizing",
        "done":          "Complete",
    }

    def _on_progress(self, phase: str, label: str, current: int, total: int) -> None:
        self.after(0, self._apply_progress, phase, label, current, total)

    def _apply_progress(self, phase: str, label: str, current: int, total: int) -> None:
        title = self._PHASE_TITLES.get(phase, phase.title())
        self.progress_phase.configure(
            text=title,
            text_color=OK_GREEN if phase == "done" else ACCENT,
        )
        self.progress_detail.configure(text=label)
        if phase == "done":
            self.progress_bar.set(1.0)
            self.progress_count.configure(text="")
            return
        if total and total > 0:
            frac = max(0.0, min(1.0, current / total))
            self.progress_bar.set(frac)
            self.progress_count.configure(text=f"{current}/{total}")
        else:
            self.progress_bar.set(0.05)
            self.progress_count.configure(text="")

    def _reset_progress(self) -> None:
        self.progress_phase.configure(text="Idle", text_color="#bfbfc7")
        self.progress_detail.configure(text="")
        self.progress_count.configure(text="")
        self.progress_bar.set(0)


def run_gui() -> int:
    app = ExportApp()
    app.mainloop()
    return 0
