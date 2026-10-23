#!/usr/bin/env bash
# One-time setup on macOS or Linux. Run from this folder: ./setup.sh
set -euo pipefail
cd "$(dirname "$0")"
python3 -c "import sys; assert sys.version_info >= (3,11), 'Python 3.11+ required'"
[ -d .venv ] || python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip -q
.venv/bin/python -m pip install -r requirements.txt -q
[ -f config.toml ] || cp bugmonitor/config.example.toml config.toml
.venv/bin/python -m bugmonitor install-claude
echo
echo "Setup done. Next: open config.toml, add your Jira site, email, and API token."
echo "Then run ./start.sh"
