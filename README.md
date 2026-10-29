# google-antigravity-export

> **Export and archive your Google Antigravity conversations, Brain knowledge items, and cascade artifacts as clean markdown.** Works on macOS, Windows, and Linux. Ships with a point-and-click local web app, a desktop GUI, and a full CLI — pick whichever you like.

![platform: macOS | Windows | Linux](https://img.shields.io/badge/platform-macOS%20%7C%20Windows%20%7C%20Linux-6949E5)
![python: 3.9+](https://img.shields.io/badge/python-3.9%2B-blue)
![license: PolyForm Noncommercial 1.0.0](https://img.shields.io/badge/license-PolyForm%20Noncommercial%201.0.0-informational)

Google [Antigravity](https://antigravity.google/) — the Gemini-powered agentic IDE — stores your entire conversation history in opaque protobuf (`.pb`) files that normal tools can't open. If you've tried to back up, grep, commit to git, or feed those chats into another model, you already know: the files are unreadable, and `protoc --decode_raw` just spits out unlabeled field numbers.

**This tool fixes that.** It uses Antigravity's own local HTTP API — the one its UI talks to — to pull out every conversation, knowledge item, and cascade artifact, then writes them into a clean folder tree of markdown + JSON you can actually use. No reverse-engineered protobuf schemas, no telemetry, no cloud uploads. Everything stays on your machine.

---

## Table of contents

- [Features](#features)
- [Supported platforms](#supported-platforms)
- [Install](#install)
- [Quick start (web UI — recommended)](#quick-start-web-ui--recommended)
- [Quick start (CLI)](#quick-start-cli)
- [Quick start (desktop GUI)](#quick-start-desktop-gui)
- [Output layout](#output-layout)
- [Customizing the export](#customizing-the-export)
- [How it works](#how-it-works)
- [Troubleshooting](#troubleshooting)
- [FAQ](#faq)
- [License](#license)

## Features

- **Zero-config** — auto-detects the running Antigravity `language_server` process, its CSRF token, and the correct local API port every time Antigravity restarts.
- **Full conversation history** — every user turn, agent reply, thinking trace, file edit, shell command + output, file view, search, task boundary, MCP tool call. Raw JSON saved alongside every markdown file for lossless re-processing.
- **Knowledge items + brain artifacts** — copies your `.gemini/antigravity/knowledge/` folder and every per-conversation scratchpad (implementation plans, task files, walkthroughs).
- **Five naming conventions, three folder layouts, three detail levels, markdown-or-JSON format toggle** — see [Customizing the export](#customizing-the-export).
- **Three frontends, one engine** — a zero-dependency local web UI (opens in your browser), a CustomTkinter desktop GUI, and a headless CLI.
- **Cross-platform** — single Python package, identical behavior on macOS, Windows, and Linux.
- **No outbound network** — all API calls go to `127.0.0.1`. Nothing about your conversations leaves your machine.

## Supported platforms

| OS | CLI | GUI | Notes |
|---|:---:|:---:|---|
| **macOS** (Intel + Apple Silicon) | yes | yes | detects `language_server_macos` |
| **Windows** 10 / 11 | yes | yes | detects `language_server_windows_x64.exe` |
| **Linux** (Ubuntu, Debian, Fedora, Arch, ...) | yes | yes | detects `language_server_linux_x64` |

Python 3.9 or newer required. On Linux the GUI uses tkinter — install `python3-tk` (Debian/Ubuntu) or `tk` (Arch/Fedora) if it isn't already there.

## Install

```bash
pip install "google-antigravity-export @ git+https://github.com/winters27/google-antigravity-export.git"
```

That's it. Then jump to the [web-UI quick start](#quick-start-web-ui--recommended).

### First time installing a Python tool? Follow these steps.

**1. Install Python 3.9 or newer.**

- **Windows** — grab the installer from [python.org/downloads](https://www.python.org/downloads/). On the first screen, **tick "Add python.exe to PATH"** before clicking Install.
- **macOS** — `brew install python` (install [Homebrew](https://brew.sh) first if needed) or the installer from [python.org/downloads](https://www.python.org/downloads/).
- **Linux** — usually pre-installed. Check with `python3 --version`. If missing: `sudo apt install python3 python3-pip` (Debian/Ubuntu), `sudo dnf install python3 python3-pip` (Fedora), `sudo pacman -S python python-pip` (Arch).

**2. Paste the install command above into a terminal.**

On macOS or Linux, use `pip3` if `pip` is not found. If neither works, `python3 -m pip install ...` always works.

**3. Verify:**

```bash
google-antigravity-export --version
```

If "command not found", open a fresh terminal and try again.

### Also want the desktop GUI?

Add `[gui]` to the install command to pull in [customtkinter](https://github.com/TomSchimansky/CustomTkinter):

```bash
pip install "google-antigravity-export[gui] @ git+https://github.com/winters27/google-antigravity-export.git"
```

On **Linux only**, also install tkinter from your distro: `sudo apt install python3-tk` (Debian/Ubuntu), `sudo dnf install python3-tkinter` (Fedora), or `sudo pacman -S tk` (Arch). Windows and macOS already bundle it.

### Advanced

Use [pipx](https://pipx.pypa.io/) instead of pip for an isolated install (`pipx install ...` — same URL). Once published to PyPI the URL collapses to `pip install google-antigravity-export`.

## Quick start (web UI — recommended)

Make sure Antigravity is running, then:

```bash
google-antigravity-export --web
```

A browser tab opens at `http://127.0.0.1:<port>/`. Pick an output folder, tick what to include, click **Start export**. When you're done, close the tab — the server shuts itself down (or click the ⏻ button to kill it immediately).

### Prefer not to touch a terminal?

Clone or download this repo, then double-click:

- **Windows** — `launch.bat`
- **macOS / Linux** — `launch.command` *(on Linux, run `chmod +x launch.command` once first)*

Both launchers do the same thing as the command above. The package still needs to be installed (see [Install](#install)).

### What's in the UI

- **Live Antigravity detection chip** — green = connected, click ↻ to re-detect.
- **Native folder picker** — the **Browse…** button opens the real OS file dialog.
- **Export tab** — all the same knobs as the CLI (naming, layout, format, detail, scope).
- **Browse tab** — read past conversations with fully rendered markdown before you export.
- **Keyboard shortcuts** — `/` filter, `J`/`K` navigate, `Esc` clear, `R` re-detect, `Q` quit.

Nothing leaves your machine — all traffic is between your browser and `127.0.0.1`.

## Native session backup and additive import (Antigravity 2.x)

Need to move sessions to a different Antigravity installation while keeping the
destination's existing sessions intact? Use the new **Native session backup &
import** panel in `--web`, or the `--native-export` / `--native-import` CLI
commands. Native ZIP files preserve SQLite conversations and summary metadata;
the regular Markdown/JSON export is unchanged and remains a readable archive.

**Imports are preview-only by default**, and require `--apply` plus Antigravity
being closed before any data is written. All existing conversation UUIDs are
skipped without overwriting the target. The importer makes an automatic
pre-import index snapshot.

See [Native session migration](NATIVE_MIGRATION.md) for complete safety
details, commands and cross-platform limitations.

---

## Quick start (CLI)

1. Open Antigravity and wait for the UI to finish loading.
2. Run:

```bash
google-antigravity-export
```

The tool finds the language-server process, pulls the CSRF token out of its command line, probes its listening ports to find the API, and writes everything to `./AntigravityExport/` in the current working directory. Use `-o /some/path` to write elsewhere.

The shorter `antigravity-export` command is registered as an alias — both invocations are identical.

### Common CLI examples

```bash
google-antigravity-export --output ~/Desktop/MyExport       # pick output folder
google-antigravity-export --dry-run                         # verify detection, don't export
google-antigravity-export --project AnimeSite               # only one workspace
google-antigravity-export --no-artifacts --no-knowledge     # conversations only
google-antigravity-export --detail chat --format markdown   # clean chat-only markdown
google-antigravity-export --csrf <uuid> --port 60130        # manual override
google-antigravity-export -v                                # verbose discovery
```

Full flag list: `google-antigravity-export --help`.

## Quick start (desktop GUI)

If you prefer a native desktop window over a browser tab, install the `[gui]` extra (see [Install](#install)) and run:

```bash
google-antigravity-export --gui
```

You get a single-window dark-themed UI with:

- **Live detection banner** — green when Antigravity is connected, red otherwise, with a ↻ Refresh button.
- **Output folder picker** — native folder dialog (Finder / Explorer / file-chooser).
- **Scope checkboxes** — conversations, knowledge items, brain artifacts; optional project filter.
- **Style dropdowns** — naming convention, folder layout, file format, detail level (defaults match the CLI).
- **Live progress bar with dynamic phase labels** — "Knowledge items", "Brain artifacts", "Conversations 42/98 — Building A Dynamic Anime Roadmap", "Finalizing", "Complete" — so you always know what's happening even during long exports.
- **Live log pane** — the same progress output the CLI prints, streamed while the export runs.
- **Open output folder** — one-click reveal when the export finishes.

No launchers, no browser, no second process — one native window.

## Output layout

With defaults, `./AntigravityExport/` looks like this:

```
AntigravityExport/
├── _MASTER_INDEX.md                       ← overview + per-project counts
├── knowledge/                             ← your knowledge items (verbatim)
│   └── <item_name>/…
├── brain_artifacts/                       ← per-conversation scratch (verbatim)
│   └── <cascade-uuid>/…
└── conversations/
    └── <Project>/
        ├── _INDEX.md                      ← table of conversations for this workspace
        └── <Title (Apr 24, 2026)>/
            ├── <Title (Apr 24, 2026)>.md  ← rich markdown transcript
            └── <Title (Apr 24, 2026)>.json ← raw step data (nothing lost)
```

Each conversation markdown renders user messages and agent replies as top-level sections, with an expandable **Agent Workflow Trace** containing the step-by-step reasoning, file edits, commands, file views, searches, and task boundaries.

## Customizing the export

Defaults produce the layout above. Mix and match these to reshape it:

### `--naming` — conversation folder/file names

| Value | Example |
|---|---|
| `title-date` *(default)* | `Building A Dynamic Anime Roadmap (Apr 21, 2026)` |
| `date-title` | `2026-04-21 - Building A Dynamic Anime Roadmap` |
| `iso-title` | `2026-04-21_Building_A_Dynamic_Anime_Roadmap` (filesystem-safest) |
| `title-only` | `Building A Dynamic Anime Roadmap` |
| `id-title` | `e92bc17e_Building A Dynamic Anime Roadmap` (short cascade ID prefix) |

### `--layout` — folder grouping

| Value | Structure |
|---|---|
| `project` *(default)* | `conversations/<Project>/<Title>/…` |
| `flat` | `conversations/<Title>/…` (no grouping — good if you use one workspace) |
| `date` | `conversations/YYYY-MM/<Title>/…` (grouped by calendar month) |

### `--format` — what each conversation writes

| Value | Files |
|---|---|
| `both` *(default)* | `<name>.md` and `<name>.json` |
| `markdown` | markdown only |
| `json` | raw JSON only |

### `--detail` — how much to render into markdown

| Value | Contents |
|---|---|
| `full` *(default)* | user + agent + expandable workflow trace (thinking, edits, commands, searches) |
| `chat` | user + agent only — clean, readable transcript |
| `thinking` | user + agent + thinking — no tool/command noise |

### `--no-apply-timestamps`

By default the tool stamps file and folder mtime to each conversation's creation date, so sorting by "Date modified" in Explorer / Finder matches conversation order. Use this flag to leave mtimes alone.

### Combined example

ISO filenames, grouped by month, markdown-only, clean chat view:

```bash
google-antigravity-export --naming iso-title --layout date --format markdown --detail chat
```

The same controls are available as dropdowns and checkboxes in the GUI.

## How it works

Antigravity spawns a `language_server_<platform>` child process that hosts a local HTTP API on loopback. Antigravity's own UI talks to that API using a CSRF token passed on the command line. This tool does the same thing:

1. Enumerate processes via `psutil`, match any beginning with `language_server_`.
2. Parse `--csrf_token <uuid>` out of the matched process's command line.
3. List TCP ports that process is listening on (usually 2–4 of them).
4. Probe each one with `POST /exa.language_server_pb.LanguageServerService/GetAllCascadeTrajectories`; the port that answers 200 with a `trajectorySummaries` body is the conversation API.
5. Call `GetAllCascadeTrajectories` for the index, then `GetCascadeTrajectorySteps` for each cascade, and render the step stream to markdown.

All traffic stays on `127.0.0.1`. No outbound network, no authentication bypass — just reading your own data from your own machine.

## Troubleshooting

**`No language_server process found`**
Antigravity isn't running, or is still starting. Open the app, wait for the UI to fully load, and re-run. In the GUI, click ↻ Refresh.

**`Found language_server process(es) but could not reach the API`**
Antigravity hasn't finished startup, or another process is binding the expected port. Wait a few seconds and retry. If it persists, inspect the running process yourself and pass `--csrf <uuid>` and `--port <n>` manually.

**`Permission denied` reading process info on Linux**
`psutil` needs to read `/proc/<pid>` entries owned by the same user. Run as the user that launched Antigravity.

**GUI won't launch on Linux: `ModuleNotFoundError: _tkinter`**
Install tkinter from your distro: `sudo apt install python3-tk` (Debian/Ubuntu) or `sudo pacman -S tk` (Arch) or `sudo dnf install python3-tkinter` (Fedora).

**Paths differ**
Knowledge items and brain artifacts are expected under `~/.gemini/antigravity/`. If yours live elsewhere, pass `--gemini-dir /path/to/antigravity`.

## FAQ

**Q: Is this against Google's or Antigravity's terms of service?**
It uses the same local API that Antigravity's own UI uses, on your own machine, on your own data. No telemetry, no uploads, no auth bypass — just reading what's already on your disk. That said, no lawyer has reviewed this. Use at your own discretion.

**Q: Why can't I just open the `.pb` files directly?**
Antigravity's `.pb` files are Google protocol buffers in an unpublished schema. `protoc --decode_raw` gives you unlabeled field numbers; without the `.proto` definitions, there's no way to turn those back into meaningful text. The local API returns the same data but already structured and named.

**Q: Will the CSRF token stay the same between runs?**
No — Antigravity regenerates it every time its language-server starts. That's why auto-detection matters; never hard-code the token.

**Q: Does this work if Antigravity isn't running?**
No. The tool depends on the local API, which only exists while Antigravity's language-server process is alive. Open the app first, then run the export.

**Q: Can I export conversations from a specific workspace only?**
Yes: `--project <name>` (CLI) or type the workspace name into the "Project filter" box in the GUI. Matching is case-insensitive substring.

**Q: What if I have hundreds of conversations and it takes a while?**
That's normal for very large Brains — each `GetCascadeTrajectorySteps` call fetches every step of a conversation (sometimes thousands). You only need to run the export once; re-runs overwrite the output folder unless you pass `--no-wipe`.

**Q: Can I automate / schedule this?**
Yes. The CLI is headless. Wire it into cron (macOS/Linux) or Task Scheduler (Windows). You'll need Antigravity running when the job fires.

**Q: Does this work with Gemini CLI / Gemini Code Assist / Codeium / Windsurf?**
This is specific to Google **Antigravity**. The local-API approach is structurally similar to what those other tools expose, but the process names, endpoints, and data shapes differ — nothing here is tested against them.

**Q: Where does Antigravity store its data on disk?**
`~/.gemini/antigravity/` on all three operating systems. Conversations live in `conversations/*.pb`, knowledge in `knowledge/<item>/`, per-conversation scratch in `brain/<uuid>/`.

## License

[PolyForm Noncommercial License 1.0.0](./LICENSE) — free for any non-commercial use (personal, research, education, internal company tooling). Selling it, or selling a service built on top of it, requires a separate license.
