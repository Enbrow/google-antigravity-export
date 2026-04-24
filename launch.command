#!/usr/bin/env bash
# Antigravity Export - macOS/Linux double-click launcher.
# Opens the local web UI in your default browser.

set -e
cd "$(dirname "$0")"

if command -v python3 >/dev/null 2>&1; then
  exec python3 -m antigravity_export --web
elif command -v python >/dev/null 2>&1; then
  exec python -m antigravity_export --web
else
  cat <<'EOF'

Python is not installed, or not on PATH.

Install Python 3.9+:
  macOS:   brew install python   (or https://www.python.org/downloads/)
  Linux:   sudo apt install python3    (or your distro equivalent)

Then:
  pip install "google-antigravity-export @ git+https://github.com/winters27/google-antigravity-export.git"

EOF
  read -n 1 -s -r -p "Press any key to close..."
  exit 1
fi
