#!/usr/bin/env python3
"""Interactive onboarding for the Claude History Dashboard.

Detects your Claude Code activity, lets you pick which folder to analyze, and
writes a git-ignored `config.json` the dashboard and MCP server read from.

Run it with:

    python3 setup.py
    # or, without installing anything:
    uv run python setup.py

Re-run any time to change your settings. It only writes `config.json`; it never
touches your Claude Code data (~/.claude is read-only to this tool).
"""

from __future__ import annotations

import json
import os
import sys
from collections import Counter

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(REPO_DIR, "config.json")
CLAUDE_DIR = os.path.expanduser("~/.claude")
HISTORY_FILE = os.path.join(CLAUDE_DIR, "history.jsonl")
SESSIONS_DIR = os.path.join(CLAUDE_DIR, "sessions")
PROJECTS_DIR = os.path.join(CLAUDE_DIR, "projects")
HOME = os.path.expanduser("~")


# --------------------------------------------------------------------------- #
# Pretty printing helpers
# --------------------------------------------------------------------------- #
def _c(code: str, text: str) -> str:
    if not sys.stdout.isatty():
        return text
    return f"\033[{code}m{text}\033[0m"


def bold(t: str) -> str:
    return _c("1", t)


def dim(t: str) -> str:
    return _c("2", t)


def cyan(t: str) -> str:
    return _c("36", t)


def green(t: str) -> str:
    return _c("32", t)


def yellow(t: str) -> str:
    return _c("33", t)


def hr() -> None:
    print(dim("─" * 64))


def ask(prompt: str, default: str | None = None) -> str:
    suffix = f" {dim('[' + default + ']')}" if default is not None else ""
    try:
        raw = input(f"{prompt}{suffix} ").strip()
    except EOFError:
        raw = ""
    return raw or (default or "")


# --------------------------------------------------------------------------- #
# Discover the real project paths Claude Code has seen
# --------------------------------------------------------------------------- #
def _collect_cwds() -> list[str]:
    """Gather real absolute working directories from Claude Code's own records.

    Best source is ~/.claude/history.jsonl (a global prompt log with a real
    `project` path per entry). We also read live-session records and, as a
    fallback, the first `cwd` line of each project transcript.
    """
    cwds: list[str] = []

    # 1) history.jsonl — one JSON object per line, with a `project` field.
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                p = d.get("project")
                if p:
                    cwds.append(p)

    # 2) live session records.
    if os.path.isdir(SESSIONS_DIR):
        for name in os.listdir(SESSIONS_DIR):
            if not name.endswith(".json"):
                continue
            try:
                d = json.load(open(os.path.join(SESSIONS_DIR, name),
                                   encoding="utf-8", errors="replace"))
            except Exception:
                continue
            if d.get("cwd"):
                cwds.append(d["cwd"])

    # 3) Fallback: first cwd line of one transcript per project dir.
    if not cwds and os.path.isdir(PROJECTS_DIR):
        for d in os.listdir(PROJECTS_DIR):
            full = os.path.join(PROJECTS_DIR, d)
            if not os.path.isdir(full):
                continue
            for f in os.listdir(full):
                if not f.endswith(".jsonl"):
                    continue
                try:
                    with open(os.path.join(full, f), encoding="utf-8",
                              errors="replace") as fh:
                        for line in fh:
                            try:
                                obj = json.loads(line)
                            except json.JSONDecodeError:
                                continue
                            if obj.get("cwd"):
                                cwds.append(obj["cwd"])
                                break
                except OSError:
                    pass
                break
    return cwds


def _rank_candidate_roots(cwds: list[str]) -> list[tuple[str, int]]:
    """Rank ancestor folders by how many distinct projects live under them.

    A folder like ~/Projects that contains many repos naturally floats to the
    top, which is usually the most useful "root" to analyze.
    """
    unique_cwds = sorted(set(cwds))
    counter: Counter[str] = Counter()
    for cwd in unique_cwds:
        parts = os.path.normpath(cwd).split(os.sep)
        # Walk ancestors from home downward; skip "/" and home itself here
        # (home is offered separately as the "everything" option).
        acc = os.sep
        for part in parts:
            if not part:
                continue
            acc = os.path.join(acc, part)
            if acc == HOME or not acc.startswith(HOME + os.sep):
                continue
            # Only count this ancestor if some cwd is strictly below it.
            if any(c == acc or c.startswith(acc + os.sep) for c in unique_cwds):
                counter[acc] += 1
    # Rank: more projects first, then shallower path (closer to a workspace).
    ranked = sorted(counter.items(),
                    key=lambda kv: (-kv[1], kv[0].count(os.sep), kv[0]))
    # Drop ancestors that only ever hold exactly one project *and* whose child
    # is itself in the list (keeps the menu focused on real workspaces + repos).
    return ranked


# --------------------------------------------------------------------------- #
# Main flow
# --------------------------------------------------------------------------- #
def choose_root() -> str:
    print(bold("\nWhich folder's Claude Code history do you want to analyze?"))
    print(dim("Every project under it shows up as a separate 'sub-project'.\n"))

    cwds = _collect_cwds()
    options: list[tuple[str, str]] = []  # (path, label)

    # Always offer "everything" (home directory).
    options.append((HOME, f"All my Claude Code projects  {dim('(' + HOME + ')')}"))

    if cwds:
        ranked = _rank_candidate_roots(cwds)
        for path, count in ranked[:10]:
            noun = "project" if count == 1 else "projects"
            options.append((path, f"{path}  {dim('(' + str(count) + ' ' + noun + ')')}"))
    else:
        print(yellow("  (No Claude Code history detected yet under ~/.claude — "
                     "that's fine, you can type a path manually.)\n"))

    for i, (_, label) in enumerate(options, 1):
        marker = green("→") if i == 1 else " "
        print(f"  {marker} {bold(str(i))}. {label}")
    print(f"    {bold('c')}. Enter a custom path")
    print()

    while True:
        choice = ask(cyan("Pick a number (or 'c'):"), "1")
        if choice.lower() == "c":
            custom = ask("Absolute path to analyze:")
            custom = os.path.expanduser(custom.strip())
            if custom and os.path.isabs(custom):
                if not os.path.isdir(custom):
                    print(yellow(f"  Note: {custom} doesn't exist yet — using it anyway."))
                return custom.rstrip("/")
            print(yellow("  Please enter an absolute path (e.g. /Users/you/Projects)."))
            continue
        if choice.isdigit() and 1 <= int(choice) <= len(options):
            return options[int(choice) - 1][0].rstrip("/")
        print(yellow("  Invalid choice, try again."))


def main() -> int:
    hr()
    print(bold("  Claude History Dashboard — setup"))
    hr()

    existing = {}
    if os.path.exists(CONFIG_FILE):
        try:
            existing = json.load(open(CONFIG_FILE, encoding="utf-8"))
            print(dim(f"Found existing config.json (root: "
                      f"{existing.get('root_path', '?')}). Values shown as "
                      f"defaults."))
        except Exception:
            pass

    if not os.path.isdir(CLAUDE_DIR):
        print(yellow(f"\n⚠️  {CLAUDE_DIR} not found. Is Claude Code installed and "
                     "has it run at least once on this machine?"))
        print(dim("You can still continue and set a path manually.\n"))

    root = choose_root()

    print()
    port = ask(cyan("Port for the dashboard web UI:"),
               str(existing.get("port", 8765)))
    sync = ask(cyan("Background re-sync interval in seconds:"),
               str(existing.get("sync_every", 30)))

    print()
    print(dim("Advanced (press Enter to accept defaults):"))
    mcp_name = ask(cyan("  MCP server name (for `claude mcp add`):"),
                   existing.get("mcp_name", "claude-history"))
    service_label = ask(cyan("  macOS autostart service label:"),
                        existing.get("service_label", "com.claude-history.dashboard"))

    cfg = {
        "root_path": root,
        "port": int(port) if str(port).isdigit() else 8765,
        "sync_every": int(sync) if str(sync).isdigit() else 30,
        "mcp_name": mcp_name or "claude-history",
        "service_label": service_label or "com.claude-history.dashboard",
    }

    with open(CONFIG_FILE, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)
        fh.write("\n")

    hr()
    print(green(bold("✓ Saved config.json")))
    print(f"  root_path     {cfg['root_path']}")
    print(f"  port          {cfg['port']}")
    print(f"  sync_every    {cfg['sync_every']}s")
    print(f"  mcp_name      {cfg['mcp_name']}")
    print(f"  service_label {cfg['service_label']}")
    hr()
    print(bold("\nNext steps:"))
    print(f"  1. Start the dashboard:   {cyan('./run.sh')}")
    print(f"     Then open:             {cyan('http://localhost:' + str(cfg['port']))}")
    print(f"  2. (optional) Register the MCP server for natural-language queries")
    print(f"     inside Claude Code — the exact command is on the dashboard's")
    print(f"     {bold('MCP & Service')} tab (with a copy button).")
    print()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nCancelled.")
        raise SystemExit(130)
