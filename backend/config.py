"""Central configuration for the Claude History Dashboard.

Resolution order for every setting (first hit wins):

    1. Environment variable  (e.g. CLAUDE_ROOT_PATH, PORT, SYNC_EVERY)
    2. `config.json` at the repo root (written by `setup.py`, git-ignored)
    3. Built-in default

This lets each user keep their own machine-specific setup in a git-ignored
`config.json` while the published code ships with safe, generic defaults.

Run `python3 setup.py` (or `uv run python setup.py`) to generate `config.json`
interactively — it detects your Claude Code projects and lets you pick the
folder to analyze.
"""

from __future__ import annotations

import json
import os

_REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_FILE = os.path.join(_REPO_DIR, "config.json")


def _load_file() -> dict:
    try:
        with open(CONFIG_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
            return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


_FILE = _load_file()


def _get(key: str, default, env: str | None = None):
    if env:
        val = os.environ.get(env)
        if val not in (None, ""):
            return val
    val = _FILE.get(key)
    if val not in (None, ""):
        return val
    return default


# The folder whose Claude Code history you want to analyze. Every project under
# it becomes a "sub-project" in the dashboard. Point it at your home directory
# (the default) to include ALL of your Claude Code activity.
ROOT_PATH: str = os.path.expanduser(
    str(_get("root_path", "~", env="CLAUDE_ROOT_PATH"))
)

PORT: int = int(_get("port", 8765, env="PORT"))
SYNC_EVERY: int = int(_get("sync_every", 30, env="SYNC_EVERY"))  # seconds

# Name used when registering the local MCP server with Claude Code
# (`claude mcp add <name> ...`).
MCP_NAME: str = str(_get("mcp_name", "claude-history", env="MCP_NAME"))

# launchd label for the macOS autostart service.
SERVICE_LABEL: str = str(
    _get("service_label", "com.claude-history.dashboard", env="SERVICE_LABEL")
)

# True when a config.json exists (i.e. the user has run the onboarding).
CONFIGURED: bool = bool(_FILE)
