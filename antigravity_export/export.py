"""High-level orchestrator: pull everything from Antigravity into a folder tree.

Default output layout:

    <output>/
      _MASTER_INDEX.md
      knowledge/<item>/...              (copied verbatim)
      brain_artifacts/<cascade-id>/...  (copied verbatim — conversation scratch)
      conversations/<Project>/_INDEX.md
      conversations/<Project>/<Title (Mon DD, YYYY)>/<same>.md
      conversations/<Project>/<Title (Mon DD, YYYY)>/<same>.json

``layout``, ``naming``, ``file_format``, and ``detail`` in ``ExportOptions``
change the structure and contents — see the CLI ``--help`` or the README.
"""

from __future__ import annotations

import json
import os
import shutil
import urllib.parse
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from . import api
from .discover import Instance
from .naming import (
    DETAIL_CHOICES,
    FORMAT_CHOICES,
    LAYOUT_CHOICES,
    NAMING_CHOICES,
    build_name,
    group_key,
    unique,
    _sanitize,
)
from .render import steps_to_markdown


def default_gemini_dir() -> Path:
    """~/.gemini/antigravity — identical layout on macOS, Linux, and Windows."""
    return Path.home() / ".gemini" / "antigravity"


@dataclass
class ExportOptions:
    output_dir: Path
    gemini_dir: Path
    include_knowledge: bool = True
    include_artifacts: bool = True
    include_conversations: bool = True
    project_filter: Optional[str] = None
    wipe: bool = True
    naming: str = "title-date"
    layout: str = "project"
    file_format: str = "both"
    detail: str = "full"
    apply_timestamps: bool = True

    def __post_init__(self) -> None:
        if self.naming not in NAMING_CHOICES:
            raise ValueError(f"naming must be one of {NAMING_CHOICES}")
        if self.layout not in LAYOUT_CHOICES:
            raise ValueError(f"layout must be one of {LAYOUT_CHOICES}")
        if self.file_format not in FORMAT_CHOICES:
            raise ValueError(f"file_format must be one of {FORMAT_CHOICES}")
        if self.detail not in DETAIL_CHOICES:
            raise ValueError(f"detail must be one of {DETAIL_CHOICES}")


def _apply_timestamps(path: Path, iso_time: str) -> None:
    if not iso_time:
        return
    try:
        clean_iso = iso_time.split(".")[0] + "+00:00"
        dt = datetime.fromisoformat(clean_iso)
        ts = dt.timestamp()
        os.utime(path, (ts, ts))
    except Exception:
        pass


def project_from_workspaces(info: dict) -> str:
    """Public wrapper: derive a friendly project name from a trajectory summary."""
    return _project_from_workspaces(info)


def _project_from_workspaces(info: dict) -> str:
    ws_list = info.get("workspaces") or []
    if not ws_list:
        return "Playground"
    uri = ws_list[0].get("workspaceFolderAbsoluteUri", "")
    decoded = urllib.parse.unquote(uri).replace("file:///", "")
    return decoded.rstrip("/").split("/")[-1] if decoded else "Playground"


def _copy_children(
    src: Path,
    dst: Path,
    skip: Callable[[Path], bool] = lambda p: False,
    on_item: Optional[Callable[[int, int, str], None]] = None,
) -> int:
    if not src.exists():
        return 0
    dst.mkdir(parents=True, exist_ok=True)
    targets = [p for p in src.iterdir() if p.is_dir() and not skip(p)]
    total = len(targets)
    for i, child in enumerate(targets, 1):
        target = dst / child.name
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(child, target)
        if on_item:
            on_item(i, total, child.name)
    return total


def _has_visible_content(path: Path) -> bool:
    try:
        for entry in path.iterdir():
            if not entry.name.startswith("."):
                return True
        sys_gen = path / ".system_generated" / "logs"
        return sys_gen.exists()
    except OSError:
        return False


def _group_heading(layout: str, key: str) -> str:
    if layout == "date":
        return f"Conversations — {key}" if key else "Conversations"
    if layout == "flat":
        return "Conversations"
    return key  # project layout: use project name as heading


ProgressFn = Callable[[str, str, int, int], None]


def _noop_progress(phase: str, label: str, current: int, total: int) -> None:
    pass


def run_export(
    instance: Instance,
    opts: ExportOptions,
    log: Callable[[str], None] = print,
    progress: Optional[ProgressFn] = None,
) -> Dict[str, int]:
    """Run the full export.

    ``progress`` (optional) is called with ``(phase, label, current, total)`` at key
    points so GUIs can render a progress bar + dynamic status label. Phases emitted:
    ``"init"``, ``"knowledge"``, ``"artifacts"``, ``"conversations"``,
    ``"finalizing"``, ``"done"``. ``current`` is 0 when a phase starts and may equal
    ``total`` when it ends; ``total`` is 0 if the count isn't known yet.
    """
    p = progress or _noop_progress

    out = opts.output_dir
    p("init", "Preparing output folder...", 0, 0)
    if opts.wipe and out.exists():
        log(f"[0/4] Wiping {out}")
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)

    stats = {"knowledge": 0, "artifacts": 0, "conversations": 0}

    # 1. KNOWLEDGE
    if opts.include_knowledge:
        log("[1/4] Knowledge items")
        p("knowledge", "Copying knowledge items...", 0, 0)

        def _on_knowledge(i: int, total: int, name: str) -> None:
            p("knowledge", f"Knowledge: {name}", i, total)

        stats["knowledge"] = _copy_children(
            opts.gemini_dir / "knowledge", out / "knowledge", on_item=_on_knowledge
        )
        log(f"      {stats['knowledge']} exported")

    # 2. BRAIN ARTIFACTS
    if opts.include_artifacts:
        log("[2/4] Brain artifacts")
        p("artifacts", "Copying brain artifacts...", 0, 0)

        def _on_artifact(i: int, total: int, name: str) -> None:
            p("artifacts", f"Artifact: {name[:40]}", i, total)

        stats["artifacts"] = _copy_children(
            opts.gemini_dir / "brain",
            out / "brain_artifacts",
            skip=lambda p_: p_.name == "tempmediaStorage" or not _has_visible_content(p_),
            on_item=_on_artifact,
        )
        log(f"      {stats['artifacts']} exported")

    # 3. CONVERSATIONS
    groups: Dict[str, List[Tuple[str, dict]]] = defaultdict(list)
    if opts.include_conversations:
        log("[3/4] Conversations")
        p("conversations", "Fetching conversation list...", 0, 0)
        summaries = api.list_trajectories(instance)
        log(f"      {len(summaries)} found in API")

        for cid, info in summaries.items():
            project = _project_from_workspaces(info)
            if opts.project_filter and opts.project_filter.lower() not in project.lower():
                continue
            created = info.get("createdTime", "")
            gk = group_key(project, created, opts.layout)
            groups[gk].append((cid, {
                "title": info.get("summary") or f"Conversation {cid[:8]}",
                "created": created,
                "modified": info.get("lastModifiedTime", ""),
                "step_count": info.get("stepCount", 0),
                "project": project,
            }))

        conv_dir = out / "conversations"
        conv_dir.mkdir(parents=True, exist_ok=True)

        total_convos = sum(len(v) for v in groups.values())
        done_convos = 0

        for gk in sorted(groups.keys()):
            convos = groups[gk]
            convos.sort(key=lambda x: x[1].get("created", "zzz"))
            group_dir = conv_dir if gk == "" else conv_dir / _sanitize(gk, 80)
            group_dir.mkdir(parents=True, exist_ok=True)
            if gk:
                log(f"      {gk}/  ({len(convos)} conversations)")
            else:
                log(f"      (flat, {len(convos)} conversations)")

            used_names: List[str] = []
            for i, (cid, info) in enumerate(convos, 1):
                title = (info["title"] or "").strip()
                base = build_name(title, info["created"], cid, opts.naming)
                fname = unique(used_names, base)
                used_names.append(fname)

                done_convos += 1
                short = title[:60] + ("..." if len(title) > 60 else "")
                p(
                    "conversations",
                    f"[{done_convos}/{total_convos}] {short}",
                    done_convos,
                    total_convos,
                )
                log(f"        [{i}/{len(convos)}] {title[:50]}")

                try:
                    steps = api.get_trajectory_steps(instance, cid)
                    conv_folder = group_dir / fname
                    conv_folder.mkdir(parents=True, exist_ok=True)

                    if opts.file_format in ("both", "markdown"):
                        md = steps_to_markdown(
                            title, cid, steps,
                            info["created"], info["modified"], info["step_count"],
                            detail=opts.detail,
                        )
                        md_path = conv_folder / f"{fname}.md"
                        md_path.write_text(md, encoding="utf-8")
                        if opts.apply_timestamps:
                            _apply_timestamps(md_path, info["created"])

                    if opts.file_format in ("both", "json"):
                        json_path = conv_folder / f"{fname}.json"
                        json_path.write_text(
                            json.dumps(steps, ensure_ascii=False), encoding="utf-8"
                        )
                        if opts.apply_timestamps:
                            _apply_timestamps(json_path, info["created"])

                    if opts.apply_timestamps:
                        _apply_timestamps(conv_folder, info["created"])

                    info["fname"] = fname
                    stats["conversations"] += 1
                except Exception as e:
                    log(f"          ERROR: {e}")

            # Per-group index
            idx: List[str] = [
                f"# {_group_heading(opts.layout, gk)}\n\n",
                f"**Conversations:** {len(convos)}\n\n",
                "| # | Date | Title | Steps |\n|---|------|-------|-------|\n",
            ]
            for i, (_, info) in enumerate(convos, 1):
                dt = (info["created"] or "?")[:10]
                safe_title = info["title"].replace("|", "&#124;")
                fname = info.get("fname", "")
                if fname and opts.file_format in ("both", "markdown"):
                    link = f"{urllib.parse.quote(fname)}/{urllib.parse.quote(fname)}.md"
                    idx.append(f"| {i} | {dt} | [{safe_title}]({link}) | {info['step_count']} |\n")
                else:
                    idx.append(f"| {i} | {dt} | {safe_title} | {info['step_count']} |\n")
            (group_dir / "_INDEX.md").write_text("".join(idx), encoding="utf-8")

    # 4. MASTER INDEX
    log("[4/4] Master index")
    p("finalizing", "Writing master index...", 0, 0)
    total_files = sum(len(fs) for _, _, fs in os.walk(out))
    total_bytes = sum(
        (Path(r) / f).stat().st_size for r, _, fs in os.walk(out) for f in fs
    )

    master: List[str] = [
        "# Antigravity Brain Export\n\n",
        f"**Exported:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n",
        (
            f"**Total:** {stats['conversations']} conversations | "
            f"{stats['knowledge']} knowledge items | "
            f"{stats['artifacts']} brain artifacts | "
            f"{total_files} files | {round(total_bytes / 1048576, 1)} MB\n\n---\n\n"
        ),
    ]
    if opts.include_conversations:
        heading = {
            "project": "Conversations by Project",
            "date":    "Conversations by Month",
            "flat":    "Conversations",
        }[opts.layout]
        master.append(f"## {heading}\n\n")
        if opts.layout == "flat":
            total = sum(len(v) for v in groups.values())
            master.append(f"[All conversations](conversations/_INDEX.md) — {total} total\n")
        else:
            col = {"project": "Project", "date": "Month"}[opts.layout]
            master.append(f"| {col} | Conversations | Date Range |\n|---|:---:|---|\n")
            for gk in sorted(groups.keys()):
                convos = groups[gk]
                dates = [c[1]["created"][:10] for c in convos if c[1]["created"]]
                dr = f"{min(dates)} to {max(dates)}" if dates else "?"
                master.append(
                    f"| [{gk}](conversations/{_sanitize(gk, 80)}/_INDEX.md) | "
                    f"{len(convos)} | {dr} |\n"
                )
    if opts.include_knowledge:
        master.append("\n## Knowledge Items\n\n")
        ki_out = out / "knowledge"
        if ki_out.exists():
            for d in sorted(p.name for p in ki_out.iterdir() if p.is_dir()):
                master.append(f"- [{d}](knowledge/{d}/)\n")
    if opts.include_artifacts:
        master.append(
            f"\n## Brain Artifacts\n\n{stats['artifacts']} conversation artifact directories\n"
        )

    (out / "_MASTER_INDEX.md").write_text("".join(master), encoding="utf-8")

    summary = (
        f"Done — {stats['conversations']} conversations, {total_files} files, "
        f"{round(total_bytes / 1048576, 1)} MB"
    )
    log("\n" + summary)
    log(f"Output: {out}")
    p("done", summary, 1, 1)
    return stats


def export_single_conversation(
    instance: Instance,
    cascade_id: str,
    summary: dict,
    opts: ExportOptions,
    log: Callable[[str], None] = print,
    progress: Optional[ProgressFn] = None,
) -> Path:
    """Export one conversation into ``opts.output_dir`` and return its folder path.

    Uses the same naming/format/detail/timestamp rules as the bulk exporter but
    skips project grouping, knowledge copy, artifacts copy, wiping, and indexes.
    """
    p = progress or _noop_progress

    title = (summary.get("summary") or f"Conversation {cascade_id[:8]}").strip() or "Untitled"
    created = summary.get("createdTime", "")
    modified = summary.get("lastModifiedTime", "")
    step_count = summary.get("stepCount", 0)

    out = opts.output_dir
    out.mkdir(parents=True, exist_ok=True)
    fname = build_name(title, created, cascade_id, opts.naming)
    conv_folder = out / fname
    conv_folder.mkdir(parents=True, exist_ok=True)

    p("conversations", f"Fetching: {title[:60]}", 0, 1)
    log(f"Fetching steps for '{title[:60]}'...")
    steps = api.get_trajectory_steps(instance, cascade_id)

    p("conversations", f"Writing: {title[:60]}", 1, 1)

    if opts.file_format in ("both", "markdown"):
        md = steps_to_markdown(
            title, cascade_id, steps, created, modified, step_count, detail=opts.detail
        )
        md_path = conv_folder / f"{fname}.md"
        md_path.write_text(md, encoding="utf-8")
        if opts.apply_timestamps:
            _apply_timestamps(md_path, created)

    if opts.file_format in ("both", "json"):
        json_path = conv_folder / f"{fname}.json"
        json_path.write_text(json.dumps(steps, ensure_ascii=False), encoding="utf-8")
        if opts.apply_timestamps:
            _apply_timestamps(json_path, created)

    if opts.apply_timestamps:
        _apply_timestamps(conv_folder, created)

    done_msg = f"Exported '{title[:60]}' to {conv_folder}"
    log(done_msg)
    p("done", done_msg, 1, 1)
    return conv_folder
