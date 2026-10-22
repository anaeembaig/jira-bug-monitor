"""Register the MCP server with Claude Desktop and Claude Code (CLI)."""
from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

from .config import ROOT

NAME = "jira-bug-monitor"


def desktop_config_path() -> Path:
    system = platform.system()
    if system == "Windows":
        return Path(os.environ["APPDATA"]) / "Claude" / "claude_desktop_config.json"
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    return Path.home() / ".config" / "Claude" / "claude_desktop_config.json"


def server_entry(session: str) -> dict:
    return {
        "command": sys.executable,
        "args": ["-m", "bugmonitor", "mcp"],
        "env": {"PYTHONPATH": str(ROOT), "BUGMON_SESSION": session},
    }


def install_desktop() -> str:
    path = desktop_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {}
    if path.exists():
        shutil.copy2(path, path.with_suffix(".json.bak"))
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    data.setdefault("mcpServers", {})[NAME] = server_entry("claude-desktop")
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return f"Claude Desktop: added to {path} (restart Claude Desktop to load it)"


def install_cli() -> str:
    claude = shutil.which("claude")
    if not claude:
        return ("Claude Code CLI: 'claude' not found on PATH. Run this later:\n  claude mcp add "
                f"{NAME} --scope user -e PYTHONPATH=\"{ROOT}\" -e BUGMON_SESSION=claude-cli -- "
                f"\"{sys.executable}\" -m bugmonitor mcp")
    subprocess.run([claude, "mcp", "remove", NAME, "--scope", "user"],
                   capture_output=True, check=False)
    r = subprocess.run(
        [claude, "mcp", "add", NAME, "--scope", "user",
         "-e", f"PYTHONPATH={ROOT}", "-e", "BUGMON_SESSION=claude-cli",
         "--", sys.executable, "-m", "bugmonitor", "mcp"],
        capture_output=True, text=True, check=False,
    )
    if r.returncode != 0:
        return f"Claude Code CLI: registration failed: {r.stderr.strip() or r.stdout.strip()}"
    return "Claude Code CLI: registered for all projects (scope: user)"


def main() -> None:
    print(install_desktop())
    print(install_cli())
