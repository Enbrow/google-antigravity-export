"""Render Antigravity conversation steps as rich markdown.

Antigravity's API returns a flat list of `steps`, each tagged by `type`
(`CORTEX_STEP_TYPE_*`). We walk them and group user/agent turns plus an
expandable "agent workflow trace" containing thinking, tool calls,
edits, commands, file views, and searches.
"""

from __future__ import annotations

import json
import urllib.parse
from typing import List

# Keep rendered artifacts readable but not huge.
MAX_FILE_CONTENT = 1000
MAX_COMMAND_OUTPUT = 1000
MAX_COMMAND_STATUS_OUTPUT = 800

EXT_LANG = {
    "ts": "typescript", "tsx": "tsx", "js": "javascript", "jsx": "jsx",
    "py": "python", "rs": "rust", "go": "go", "rb": "ruby", "java": "java",
    "c": "c", "cpp": "cpp", "cs": "csharp", "css": "css", "scss": "scss",
    "json": "json", "yaml": "yaml", "yml": "yaml", "toml": "toml",
    "md": "markdown", "html": "html", "xml": "xml", "sh": "bash",
    "ps1": "powershell", "sql": "sql",
}


def _extract_user_text(step: dict) -> str:
    ui = step.get("userInput", {})
    items = ui.get("items", [])
    parts: List[str] = []
    for wrapper in items:
        item = wrapper.get("item", wrapper) if isinstance(wrapper, dict) else {}
        recipe = item.get("recipe", {})
        if recipe.get("title"):
            parts.append(f"**Workflow:** `/{recipe['title']}`")
        text = item.get("text", "")
        if isinstance(text, dict) and text.get("text"):
            parts.append(text["text"])
        elif isinstance(text, str) and text:
            parts.append(text)
        tb = item.get("textBlock", {})
        if tb and tb.get("content"):
            parts.append(tb["content"])
        ctx = item.get("context", {})
        active_doc = ctx.get("activeDocumentUri", "") if isinstance(ctx, dict) else ""
        if active_doc:
            parts.append(f"*Active file: `{active_doc}`*")
    return "\n\n".join(parts)


def _extract_code_action(step: dict) -> str:
    ca = step.get("codeAction", {})
    spec = ca.get("actionSpec", {})
    lines: List[str] = []
    for key, label in (("createFile", "Create"), ("editFile", "Edit"), ("multiEditFile", "Multi-edit")):
        action = spec.get(key, {})
        if not action:
            continue
        target = action.get("targetFile", "")
        desc = action.get("description", "")
        instr = action.get("instruction", "")
        if target:
            lines.append(f"**{label}:** `{target}`")
        if desc:
            lines.append(f"*{desc}*")
        if instr:
            lines.append(f"\n{instr[:500]}")
    return "\n".join(lines)


def _extract_command(step: dict) -> str:
    rc = step.get("runCommand", {})
    cmd = rc.get("commandLine", "")
    cwd = rc.get("cwd", "")
    co = rc.get("combinedOutput", {}) or {}
    output = co.get("full", co.get("delta", ""))
    exit_code = rc.get("exitCode", "")

    lines: List[str] = []
    if cwd:
        lines.append(f"*cwd: `{cwd}`*")
    if cmd:
        lines.append(f"```\n{cmd}\n```")
    if output:
        if len(output) > MAX_COMMAND_OUTPUT:
            output = output[:MAX_COMMAND_OUTPUT] + "\n...(truncated)"
        lines.append(
            f"<details><summary>Output (exit: {exit_code})</summary>\n\n```\n{output}\n```\n</details>"
        )
    return "\n".join(lines)


def _extract_command_status(step: dict) -> str:
    cs = step.get("commandStatus", {})
    output = cs.get("combined", cs.get("delta", ""))
    if not output:
        return ""
    if len(output) > MAX_COMMAND_STATUS_OUTPUT:
        output = output[:MAX_COMMAND_STATUS_OUTPUT] + "\n...(truncated)"
    return f"*Status: {cs.get('status', '')}, exit: {cs.get('exitCode', '')}*\n```\n{output}\n```"


def _extract_view_file(step: dict) -> str:
    vf = step.get("viewFile", {})
    uri = vf.get("absolutePathUri", "")
    content = vf.get("content", "")
    start = vf.get("startLine", "")
    end = vf.get("endLine", "")
    path = urllib.parse.unquote(uri.replace("file:///", ""))

    lines: List[str] = []
    if path:
        range_str = f" (L{start}-{end})" if start else ""
        lines.append(f"**File:** `{path}`{range_str}")
    if content:
        if len(content) > MAX_FILE_CONTENT:
            content = content[:MAX_FILE_CONTENT] + "\n...(truncated)"
        ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
        lang = EXT_LANG.get(ext, "")
        lines.append(f"```{lang}\n{content}\n```")
    return "\n".join(lines)


def _extract_search(step: dict) -> str:
    gs = step.get("grepSearch", {})
    query = gs.get("query", "")
    results = gs.get("results", []) or []
    lines: List[str] = []
    if query:
        lines.append(f"**Search:** `{query}`")
    for r in results[:10]:
        rp = r.get("relativePath", "") if isinstance(r, dict) else str(r)
        if rp:
            lines.append(f"- `{rp}`")
    if len(results) > 10:
        lines.append(f"*...and {len(results) - 10} more*")
    return "\n".join(lines)


def steps_to_markdown(
    title: str,
    cascade_id: str,
    steps: list,
    created: str = "",
    modified: str = "",
    step_count: int = 0,
    detail: str = "full",
) -> str:
    """Convert API steps to a human-readable markdown document.

    ``detail`` controls how much gets rendered:
      * ``full`` (default) — user turns, agent replies, plus an expandable
        "Agent Workflow Trace" with thinking, file edits, commands, views,
        searches, task boundaries, MCP calls, and errors.
      * ``chat`` — only user turns and agent replies. No trace at all.
      * ``thinking`` — user turns, agent replies, and an expandable trace
        containing thinking only (no tool calls, commands, or file edits).
    """
    render_trace = detail in ("full", "thinking")
    render_tools = detail == "full"

    out: List[str] = []
    out.append(f"# {title}\n")
    out.append("| Field | Value |")
    out.append("|-------|-------|")
    out.append(f"| Cascade ID | `{cascade_id}` |")
    if created:
        out.append(f"| Created | {created} |")
    if modified:
        out.append(f"| Last Modified | {modified} |")
    out.append(f"| Steps | {step_count or len(steps)} |")
    out.append("\n---\n")

    # Turn-based layout: USER -> AGENT -> (collapsible trace) -> ---
    # This keeps the human dialogue at face value and buries the long internal
    # trace inside a <details> drawer after the agent's reply.
    user_md = ""
    agent_chunks: List[str] = []
    trace_items: List[str] = []

    def flush_turn() -> None:
        nonlocal user_md, agent_chunks, trace_items
        if not (user_md or agent_chunks or trace_items):
            return
        if user_md:
            out.append(f"## 👤 USER\n\n{user_md}\n")
        if agent_chunks:
            out.append("## 🤖 AGENT\n")
            out.extend(agent_chunks)
        if render_trace and trace_items:
            out.append("\n<details><summary><b>🛠️ Agent Workflow Trace (Expand)</b></summary>\n")
            out.extend(trace_items)
            out.append("\n</details>\n")
        out.append("\n---\n")
        user_md = ""
        agent_chunks = []
        trace_items = []

    for i, step in enumerate(steps):
        t = step.get("type", "")

        if t == "CORTEX_STEP_TYPE_USER_INPUT":
            flush_turn()
            user_md = _extract_user_text(step) or ""

        elif t == "CORTEX_STEP_TYPE_NOTIFY_USER":
            nu = step.get("notifyUser", {})
            content = nu.get("notificationContent", "")
            uris = nu.get("reviewAbsoluteUris", []) or []
            if content or uris:
                parts: List[str] = []
                if content:
                    parts.append(content)
                if uris:
                    parts.append("\n**Artifacts for review:**")
                    for u in uris:
                        parts.append(f"- `{u}`")
                agent_chunks.append("\n".join(parts) + "\n")

        elif t == "CORTEX_STEP_TYPE_PLANNER_RESPONSE":
            pr = step.get("plannerResponse", {})
            # The agent's visible reply text lives in `modifiedResponse`
            # (if the user edited) or `response`. Historically some builds
            # exposed it as `text`; fall back to that for safety.
            reply = pr.get("modifiedResponse") or pr.get("response") or pr.get("text", "")
            if reply:
                agent_chunks.append(reply.rstrip() + "\n")
            if render_trace:
                thinking = pr.get("thinking", "")
                if thinking:
                    quoted = "\n".join(f"> {l}" for l in thinking.split("\n"))
                    trace_items.append(f"\n### 🤔 Thinking (Step {i})\n\n{quoted}\n")

        elif render_tools and t == "CORTEX_STEP_TYPE_TASK_BOUNDARY":
            tb = step.get("taskBoundary", {})
            if tb:
                lines = [
                    f"\n### 🎯 TASK: {tb.get('taskName', '?')} (Step {i})",
                    f"- **Mode:** {tb.get('mode', '?')}",
                    f"- **Status:** {tb.get('taskStatus', '?')}",
                ]
                if tb.get("taskSummary"):
                    lines.append(f"- **Summary:** {tb['taskSummary']}")
                lines.append("")
                trace_items.append("\n".join(lines))

        elif render_tools and t == "CORTEX_STEP_TYPE_CODE_ACTION":
            text = _extract_code_action(step)
            if text:
                trace_items.append(f"\n### 💻 CODE EDIT (Step {i})\n\n{text}\n")

        elif render_tools and t == "CORTEX_STEP_TYPE_RUN_COMMAND":
            text = _extract_command(step)
            if text:
                trace_items.append(f"\n### ⚡ COMMAND (Step {i})\n\n{text}\n")

        elif render_tools and t == "CORTEX_STEP_TYPE_COMMAND_STATUS":
            text = _extract_command_status(step)
            if text:
                trace_items.append(f"\n### 🔄 COMMAND OUTPUT (Step {i})\n\n{text}\n")

        elif render_tools and t == "CORTEX_STEP_TYPE_VIEW_FILE":
            text = _extract_view_file(step)
            if text:
                trace_items.append(f"\n### 👀 VIEW FILE (Step {i})\n\n{text}\n")

        elif render_tools and t == "CORTEX_STEP_TYPE_GREP_SEARCH":
            text = _extract_search(step)
            if text:
                trace_items.append(f"\n### 🔍 SEARCH (Step {i})\n\n{text}\n")

        elif render_tools and t == "CORTEX_STEP_TYPE_FIND":
            fd = step.get("find", {})
            pattern = fd.get("pattern", "")
            search_dir = fd.get("searchDirectory", "")
            results = fd.get("results", []) or []
            if pattern or results:
                lines = [f"\n### 📂 FIND (Step {i})"]
                if search_dir:
                    lines.append(f"**Dir:** `{search_dir}`")
                if pattern:
                    lines.append(f"**Pattern:** `{pattern}`")
                for r in results[:10]:
                    lines.append(f"- `{r.get('relativePath', r) if isinstance(r, dict) else r}`")
                lines.append("")
                trace_items.append("\n".join(lines))

        elif render_tools and t == "CORTEX_STEP_TYPE_LIST_DIRECTORY":
            ld = step.get("listDirectory", {})
            path = ld.get("directoryPath", "")
            if path:
                trace_items.append(f"\n### 📁 LIST DIRECTORY (Step {i})\n\n**Path:** `{path}`\n")

        elif render_tools and t == "CORTEX_STEP_TYPE_MCP_TOOL":
            mcp = step.get("mcpTool", {})
            if mcp:
                trace_items.append(f"\n### 🔧 MCP TOOL (Step {i})\n\n**Tool:** `{mcp.get('toolName', '?')}`\n")

        elif render_tools and t == "CORTEX_STEP_TYPE_ERROR_MESSAGE":
            em = step.get("errorMessage", {})
            if em:
                msg = em.get("message", json.dumps(em, ensure_ascii=False)[:300])
                trace_items.append(f"\n### ⚠️ ERROR (Step {i})\n\n> {msg}\n")

    flush_turn()
    return "\n".join(out)
