# Claude History Dashboard

A local web app to explore **all of your Claude Code history**: KPIs, token
metrics, estimated cost, daily / weekly / monthly breakdowns, **live active
sessions**, a full searchable session list with transcripts and recaps, and a
**durable SQLite backup** that keeps your sessions even after Claude Code deletes
them.

Point it at any folder — a single project, a whole workspace, or your entire
home directory — and it analyzes every Claude Code session under it. Everything
runs **100% locally**; nothing leaves your machine.

> Works on macOS and Linux. The optional "run in the background" autostart
> service is macOS-only (launchd); everything else is cross-platform.

---

## ⚠️ Why this exists: Claude Code deletes old sessions

Claude Code stores conversations in `~/.claude/projects/.../<id>.jsonl` and, on
startup, **automatically deletes any that exceed `cleanupPeriodDays` (default 30
days)**. That's why it can feel like you've used it far more than your history
shows — the old sessions are simply gone.

This dashboard mitigates that in two ways:

1. **Partial recovery** from `~/.claude/history.jsonl` — a global log of your
   *prompts* that survives the cleanup. Even when the full transcript is gone,
   we recover that a session existed, when, and what you asked.
2. **Its own durable SQLite backup** (`data/history.db`): every sync **dumps**
   the live `.jsonl` files into the database. Even if Claude deletes the file
   tomorrow, the session (tokens, tools, files, prompts, cost) **stays in your
   database** and gets flagged as *pruned/archived*. The database only grows —
   you never lose anything again.

> **Tip:** to keep full transcripts around longer, raise `cleanupPeriodDays` in
> `~/.claude/settings.json` (e.g. `{"cleanupPeriodDays": 365}`). The dashboard
> already backs up what it sees, but it can only back up what still exists when
> it runs.

---

## Quick start

**Requirements**

- [`uv`](https://github.com/astral-sh/uv) — the only hard dependency. It manages
  Python and the packages for you (no `pip install`, no virtualenv juggling).
- No Node / `npm install`: the frontend uses Tailwind and Chart.js via CDN.
- For the **AI recap** feature: the `claude` CLI on your `PATH`, authenticated
  (it uses your local subscription/key).

**Install & run**

```bash
git clone https://github.com/Juanipis/claude-code-history-dashboard.git
cd claude-code-history-dashboard
./run.sh
```

On the **first run** an interactive wizard starts automatically. It detects your
Claude Code projects and lets you pick which folder to analyze:

```
Which folder's Claude Code history do you want to analyze?

  → 1. All my Claude Code projects  (/Users/you)
    2. /Users/you/Projects            (44 projects)
    3. /Users/you/Projects/my-app     (12 projects)
    ...
    c. Enter a custom path
```

Pick one, accept the defaults, and the dashboard opens at
**http://localhost:8765**.

You can re-run the wizard any time to change your settings:

```bash
uv run python setup.py
```

---

## Configuration

The wizard writes a **git-ignored `config.json`** at the repo root. You can edit
it by hand too:

```json
{
  "root_path": "/Users/you/Projects",
  "port": 8765,
  "sync_every": 30,
  "mcp_name": "claude-history",
  "service_label": "com.claude-history.dashboard"
}
```

| Key | What it does | Default |
|---|---|---|
| `root_path` | Folder to analyze. Every project under it is a "sub-project". Use your home dir to include **everything**. | `~` |
| `port` | Port for the web UI. | `8765` |
| `sync_every` | Background re-sync interval (seconds). | `30` |
| `mcp_name` | Name used when registering the MCP server. | `claude-history` |
| `service_label` | launchd label for the macOS autostart service. | `com.claude-history.dashboard` |

**Environment variables override `config.json`**, which is handy for one-off
runs:

```bash
PORT=9000 ./run.sh                                  # different port
SYNC_EVERY=10 ./run.sh                              # sync more often
CLAUDE_ROOT_PATH=/Users/you/Projects ./run.sh       # analyze another folder
```

---

## 💵 About the "cost" number (be honest with yourself)

The cost shown is an **API-equivalent estimate** using official pay-per-token
prices (see `backend/pricing.py`):

| Family | input | output | cache write 5m | cache write 1h | cache read |
|---|---|---|---|---|---|
| Opus 4.x | $5 | $25 | $6.25 | $10 | $0.50 |
| Sonnet 4.x | $3 | $15 | $3.75 | $6 | $0.30 |
| Haiku 4.5 | $1 | $5 | $1.25 | $2 | $0.10 |

**On a subscription (Pro / Max / Team / Enterprise) you do NOT pay per token** —
you pay a fixed seat price and usage counts against *rate limits*, not dollars.
So the number is **volume priced at API rates**, useful for comparing
projects/periods — **it is not your bill**. Totals are dominated by `cache_read`
(Claude Code re-reads the cached context every turn, at 0.1× input), which is why
the token count is huge but cheap per unit.

---

## Where the data comes from (3 sources, all under `~/.claude`)

We never walk your real project trees → **`node_modules`, `.venv`, `dist/`,
etc. are never touched.** We only read Claude's central store:

| Source | What it provides |
|---|---|
| `~/.claude/projects/<path>/<id>.jsonl` | Full transcript: messages, model, **tokens** (`message.usage`, with 5m/1h cache split), tools, commands. Pruned after 30 days. |
| `~/.claude/history.jsonl` | Global log of your prompts. Survives the prune → recovers deleted sessions. |
| `~/.claude/sessions/<pid>.json` | Registry of **live** sessions (`pid`, `status` busy/idle, `cwd`). Powers real-time detection. |

---

## What it shows

- **Overview** — honesty note (pruned sessions, cost, cache), global KPIs, charts
  (activity/tokens per day, tokens & cost per project, token distribution
  input/output/cache 5m/1h/read, models), and the pricing table used.
- **Projects** — per sub-project: sessions, how many pruned, prompts, tools,
  tokens, cost, date range.
- **Timeline** — daily / weekly / monthly breakdown, stacked token + cost chart
  and table.
- **Sessions**
  - **Active sessions now** (top, live, refreshes every 4s): open Claude sessions
    with a **WORKING** (busy) / **idle** badge and tokens/prompts/tools/cost
    updating in real time. A pulsing green dot in the header signals global
    activity.
  - Searchable cards for every session (filter by project, hide pruned). Pruned
    ones get a dashed border and a label.
  - Opening a session shows:
    - **Recap**: instant heuristic brief (goal, commands, tools, files, prompts)
      **+ a "✨ Generate AI recap" button** that runs
      `claude --resume <id> -p "/recap…"` and saves the result.
    - **Transcript**: the user ↔ Claude thread (or just prompts if pruned).
    - **Metrics**: token breakdown, cost, tools, files.

The **↻ Sync** button forces a re-sync; a background thread also syncs every
`sync_every` seconds (detects prunes and picks up new prompts).

### The AI recap button

In a session's detail, **"✨ Generate AI recap"** runs on the backend:

```
claude --resume <sessionId> -p "<recap prompt>"
```

in the session's original `cwd`, resuming its full context and asking for a
goal / what-was-done / status summary. The result is **cached in SQLite** (table
`recaps`) and shown again without recomputing. Notes:

- It's **explicit** (a button), not automatic, because it **consumes tokens**.
- **Pruned sessions can't be resumed** (no `.jsonl`): for those only the
  heuristic recap from recovered prompts is available.

---

## 🔌 MCP server for Claude Code

There's a **local MCP server** (`backend/mcp_server.py`, stdio transport) that
connects this history to Claude Code so you can **ask in natural language** and
Claude queries your sessions. Examples:

> "What did I do on April 22nd?" · "Summarize my week of April 20th" ·
> "What did I work on in May in project X?"

A session counts toward a day if it **started**, **ended**, or was **in
progress** that day (started earlier and continued/ended later).

**Register it (once, user scope):** the exact command is on the dashboard's
**MCP & Service** tab (with a copy button). It looks like this:

```bash
claude mcp add claude-history -s user \
  -e CLAUDE_ROOT_PATH=/Users/you/Projects \
  -- uv run --with mcp python /absolute/path/to/backend/mcp_server.py
```

Verify / remove: `claude mcp list` · `claude mcp remove claude-history -s user`.

The MCP reads the same SQLite backup and **syncs on startup**, so it works even
when the web dashboard isn't running. Tools:

| Tool | For |
|---|---|
| `what_did_i_do(day)` | what happened on a day (started / ended / in progress) |
| `summarize_week(date_in_week)` | week summary (Mon–Sun) |
| `summarize_month(month)` | month summary (`YYYY-MM`) |
| `summarize_range(start, end)` | arbitrary range summary |
| `search_sessions(query)` | search sessions by text |
| `session_detail(session_id)` | one session's detail |
| `list_projects()` / `overview()` | per-project / global metrics |
| `active_sessions()` | sessions open right now (busy/idle) |
| `current_models()` | current Claude models + reference prices |

---

## ⚙️ Run in the background (autostart, macOS)

On the **MCP & Service** tab there are buttons to **install/enable** and
**uninstall** the dashboard as a background service. On macOS it installs a
LaunchAgent (`~/Library/LaunchAgents/<service_label>.plist`) with `RunAtLoad` +
`KeepAlive`: it starts **at login** and stays alive. Logs go to
`data/service.log`.

> Once the service is active you don't need `./run.sh` — the service owns the
> port. Running both at once will conflict; use one or the other.

---

## Current models

`backend/pricing.py` covers all current families (Opus 4.x, Sonnet 4.x,
Haiku 4.5, Fable 5) and the MCP exposes `current_models()` to look them up. When
Anthropic ships new models, just update `_BASE` in `pricing.py` and the list in
`current_models()`.

---

## Project structure

```
claude-code-history-dashboard/
├── run.sh                 # starts the dashboard (runs setup.py on first run)
├── setup.py               # interactive onboarding wizard → writes config.json
├── config.json            # your settings (git-ignored, per-machine)
├── pyproject.toml
├── data/history.db        # SQLite backup (auto, git-ignored)
├── backend/
│   ├── config.py          # config layer: env > config.json > defaults
│   ├── app.py             # FastAPI API + sync thread + AI recap + service + frontend
│   ├── db.py              # SQLite: durable backup + queries (thread-safe)
│   ├── parser.py          # reads the 3 sources under ~/.claude
│   ├── pricing.py         # official prices (API-equivalent estimate)
│   ├── mcp_server.py      # MCP server (stdio) for Claude Code
│   └── service.py         # launchd autostart (macOS)
└── static/
    ├── index.html         # dashboard (Tailwind + Chart.js via CDN)
    └── app.js
```

## API

| Endpoint | Description |
|----------|-------------|
| `GET /api/overview` | global KPIs + pricing table |
| `GET /api/projects` | per-sub-project metrics |
| `GET /api/timeseries/{day\|week\|month}` | time series |
| `GET /api/sessions` | list (includes pruned) |
| `GET /api/sessions/{id}` | detail: recap + transcript + metrics |
| `GET /api/active` | **live sessions in real time** (busy/idle + metrics) |
| `POST /api/sessions/{id}/recap?force=true` | generate AI recap via the CLI |
| `POST /api/reload` | force disk → SQLite re-sync |
| `GET /api/mcp/info` | MCP install command + tools |
| `GET /api/service/status` | autostart status (launchd) |
| `POST /api/service/install` · `POST /api/service/uninstall` | enable / disable autostart |

---

## Privacy

Everything is local. The app reads only `~/.claude` (read-only), stores its
backup in `data/history.db` on your machine, and serves the UI on
`127.0.0.1`. The only outbound call is the **optional** AI recap, which invokes
your local `claude` CLI. No telemetry, no external services.

## License

[MIT](LICENSE) — do whatever you want, no warranty.

## Contributing

Issues and PRs welcome. This started as a personal tool, so expect some rough
edges. If Anthropic ships new models, updating `backend/pricing.py` is the most
common contribution.

---

Made by [Juanipis](https://github.com/Juanipis) · [juanipis.xyz](https://juanipis.xyz)
