#!/usr/bin/env bash
# Starts the dashboard. Requires `uv` (https://github.com/astral-sh/uv).
set -e
cd "$(dirname "$0")"

# First run? Kick off the interactive onboarding so the user picks a folder.
if [ ! -f "config.json" ] && [ -z "${CLAUDE_ROOT_PATH:-}" ]; then
  echo "No config.json found — running the setup wizard first."
  echo
  uv run python setup.py
  echo
fi

PORT="${PORT:-$(uv run python -c "import sys; sys.path.insert(0,'backend'); import config; print(config.PORT)" 2>/dev/null || echo 8765)}"
echo "▶  Claude History Dashboard → http://localhost:${PORT}"
uv run --with fastapi --with uvicorn --with pydantic uvicorn app:app --app-dir backend --host 127.0.0.1 --port "${PORT}"
