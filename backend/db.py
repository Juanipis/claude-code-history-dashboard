"""Backup persistente en SQLite + capa de consultas del dashboard.

Por qué SQLite: Claude Code BORRA las transcripciones .jsonl tras
`cleanupPeriodDays` (default 30 días). Si sólo leyéramos los .jsonl, perderíamos
todo lo viejo. Aquí hacemos lo contrario: en cada sync VOLCAMOS los .jsonl vivos
a SQLite, así que aunque mañana Claude borre el archivo, la sesión (con sus
tokens, herramientas, prompts y costo) SIGUE en nuestra base y se marca como
"archivada". La base es por tanto un superconjunto durable que sólo crece.

Fuentes que ingiere (ver parser.py):
  - .jsonl vivos        → sesiones con detalle completo (source='jsonl')
  - history.jsonl       → prompts de sesiones ya purgadas (source='history')
El dashboard lee SIEMPRE de SQLite, nunca directo del disco de Claude.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import functools
from collections import defaultdict
from datetime import datetime, timedelta, date

from pricing import cost_for, family_for_model, PRICING_TABLE
import parser as P

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "history.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id      TEXT PRIMARY KEY,
    project         TEXT,
    cwd             TEXT,
    git_branch      TEXT,
    title           TEXT,
    agent_name      TEXT,
    version         TEXT,
    start           TEXT,
    end             TEXT,
    models          TEXT,   -- json array
    user_prompts    INTEGER DEFAULT 0,
    assistant_msgs  INTEGER DEFAULT 0,
    tool_calls      INTEGER DEFAULT 0,
    input_tokens    INTEGER DEFAULT 0,
    output_tokens   INTEGER DEFAULT 0,
    cache_write_5m  INTEGER DEFAULT 0,
    cache_write_1h  INTEGER DEFAULT 0,
    cache_read      INTEGER DEFAULT 0,
    tools           TEXT,   -- json obj
    commands        TEXT,   -- json obj
    files_touched   TEXT,   -- json array
    first_prompt    TEXT,
    prompt_samples  TEXT,   -- json array
    source          TEXT,   -- 'jsonl' | 'history'
    is_pruned       INTEGER DEFAULT 0,
    file_mtime      REAL DEFAULT 0,
    first_synced    TEXT,
    last_synced     TEXT
);
CREATE TABLE IF NOT EXISTS prompts (
    session_id  TEXT,
    project     TEXT,
    timestamp   TEXT,
    display     TEXT
);
CREATE INDEX IF NOT EXISTS idx_prompts_session ON prompts(session_id);
CREATE TABLE IF NOT EXISTS recaps (
    session_id   TEXT PRIMARY KEY,
    recap        TEXT,
    generated_at TEXT,
    model        TEXT,
    source       TEXT    -- 'ai' (CLI) | 'heuristic'
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

_TOKEN_COLS = ["input_tokens", "output_tokens", "cache_write_5m",
               "cache_write_1h", "cache_read"]


def _now() -> str:
    # Hora local sin microsegundos.
    return datetime.now().replace(microsecond=0).isoformat()


def _synchronized(fn):
    """Serializa el acceso a la conexión SQLite compartida entre hilos.

    Una sola sqlite3.Connection no es segura para uso concurrente; el hilo de
    fondo (sync) y los requests (lecturas) compiten. El lock reentrante evita
    los 500 intermitentes por 'recursive use of cursors' / 'database is locked'.
    """
    @functools.wraps(fn)
    def wrapper(self, *a, **kw):
        with self._lock:
            return fn(self, *a, **kw)
    return wrapper


class HistoryDB:
    def __init__(self, root_path: str, db_path: str = DB_PATH):
        self.root_path = root_path.rstrip("/")
        self.root_name = os.path.basename(self.root_path)
        self.db_path = os.path.abspath(db_path)
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        # busy_timeout: si otro proceso (p.ej. el MCP) está escribiendo, espera
        # en vez de fallar con 'database is locked'.
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        self.last_sync: str | None = None

    # ------------------------------------------------------------------ #
    # SYNC: disco de Claude → SQLite
    # ------------------------------------------------------------------ #
    @_synchronized
    def sync(self) -> dict:
        cur = self.conn.cursor()
        now = _now()
        seen_jsonl = set()

        # 1) Volcar .jsonl vivos (detalle completo).
        for f in P.discover_files(self.root_name):
            sid = os.path.splitext(os.path.basename(f))[0]
            mtime = os.path.getmtime(f)
            row = cur.execute(
                "SELECT file_mtime, source FROM sessions WHERE session_id=?", (sid,)
            ).fetchone()
            seen_jsonl.add(sid)
            # Sólo re-parsear si cambió el archivo (sync incremental barato).
            if row and row["source"] == "jsonl" and abs((row["file_mtime"] or 0) - mtime) < 0.5:
                continue
            s = P.parse_file(f, self.root_path)
            if s:
                self._upsert_session(cur, s.as_row(), now)

        # 2) Marcar como purgadas las sesiones jsonl cuyo archivo ya no existe.
        pruned_now = 0
        for r in cur.execute(
            "SELECT session_id FROM sessions WHERE source='jsonl' AND is_pruned=0"
        ).fetchall():
            if r["session_id"] not in seen_jsonl:
                cur.execute(
                    "UPDATE sessions SET is_pruned=1, last_synced=? WHERE session_id=?",
                    (now, r["session_id"]),
                )
                pruned_now += 1

        # 3) history.jsonl → tabla prompts + sesiones que NUNCA tuvieron .jsonl.
        hist = P.parse_history(self.root_path)
        cur.execute("DELETE FROM prompts")  # se reconstruye completa cada sync
        by_sid = defaultdict(list)
        for h in hist:
            cur.execute(
                "INSERT INTO prompts (session_id, project, timestamp, display) VALUES (?,?,?,?)",
                (h["session_id"], h["project"],
                 h["timestamp"].isoformat() if h["timestamp"] else None, h["display"]),
            )
            if h["session_id"]:
                by_sid[h["session_id"]].append(h)

        recovered = 0
        existing = {r["session_id"] for r in
                    cur.execute("SELECT session_id FROM sessions").fetchall()}
        for sid, items in by_sid.items():
            if sid in existing:
                continue  # ya tenemos detalle rico de esta sesión
            items.sort(key=lambda x: x["timestamp"] or datetime.min.replace(tzinfo=None))
            ts = [i["timestamp"] for i in items if i["timestamp"]]
            row = {
                "session_id": sid,
                "project": items[0]["project"],
                "cwd": items[0]["cwd"],
                "git_branch": "", "title": "", "agent_name": "", "version": "",
                "start": ts[0].isoformat() if ts else None,
                "end": ts[-1].isoformat() if ts else None,
                "models": [], "user_prompts": len(items), "assistant_msgs": 0,
                "tool_calls": 0, "input_tokens": 0, "output_tokens": 0,
                "cache_write_5m": 0, "cache_write_1h": 0, "cache_read": 0,
                "tools": {}, "commands": {}, "files_touched": [],
                "first_prompt": items[0]["display"],
                "prompt_samples": [i["display"] for i in items[:20]],
                "source": "history", "is_pruned": 1, "file_mtime": 0,
            }
            self._upsert_session(cur, row, now)
            recovered += 1

        self.conn.commit()
        self.last_sync = now
        self._set_meta("last_sync", now)
        return {
            "ok": True, "last_sync": now,
            "total_sessions": self._count("SELECT COUNT(*) FROM sessions"),
            "jsonl_sessions": self._count("SELECT COUNT(*) FROM sessions WHERE source='jsonl'"),
            "pruned_sessions": self._count("SELECT COUNT(*) FROM sessions WHERE is_pruned=1"),
            "history_only": self._count("SELECT COUNT(*) FROM sessions WHERE source='history'"),
            "newly_pruned_this_sync": pruned_now,
            "recovered_from_history": recovered,
        }

    @_synchronized
    def sync_one(self, session_id: str):
        """Re-sync incremental de UNA sesión (para sesiones activas en vivo)."""
        path = P.jsonl_path_for(self.root_name, session_id)
        if not path:
            return
        s = P.parse_file(path, self.root_path)
        if s:
            cur = self.conn.cursor()
            self._upsert_session(cur, s.as_row(), _now())
            self.conn.commit()

    def _upsert_session(self, cur, row: dict, now: str):
        # Preservar first_synced si ya existía.
        prev = cur.execute(
            "SELECT first_synced FROM sessions WHERE session_id=?", (row["session_id"],)
        ).fetchone()
        first_synced = prev["first_synced"] if prev and prev["first_synced"] else now
        cur.execute("""
            INSERT OR REPLACE INTO sessions (
                session_id, project, cwd, git_branch, title, agent_name, version,
                start, end, models, user_prompts, assistant_msgs, tool_calls,
                input_tokens, output_tokens, cache_write_5m, cache_write_1h, cache_read,
                tools, commands, files_touched, first_prompt, prompt_samples,
                source, is_pruned, file_mtime, first_synced, last_synced
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            row["session_id"], row["project"], row["cwd"], row["git_branch"],
            row["title"], row["agent_name"], row["version"], row["start"], row["end"],
            json.dumps(row["models"]), row["user_prompts"], row["assistant_msgs"],
            row["tool_calls"], row["input_tokens"], row["output_tokens"],
            row["cache_write_5m"], row["cache_write_1h"], row["cache_read"],
            json.dumps(row["tools"]), json.dumps(row["commands"]),
            json.dumps(row["files_touched"]), row["first_prompt"],
            json.dumps(row["prompt_samples"]), row["source"], row["is_pruned"],
            row["file_mtime"], first_synced, now,
        ))

    # ------------------------------------------------------------------ #
    # Helpers de lectura
    # ------------------------------------------------------------------ #
    def _count(self, q) -> int:
        return self.conn.execute(q).fetchone()[0]

    def _set_meta(self, k, v):
        self.conn.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (k, v))
        self.conn.commit()

    @staticmethod
    def _row_cost(r) -> float:
        models = json.loads(r["models"] or "[]")
        model = models[0] if models else None
        return cost_for(model, r["input_tokens"], r["output_tokens"],
                        r["cache_write_5m"], r["cache_write_1h"], r["cache_read"])

    @staticmethod
    def _total_tokens(r) -> int:
        return sum(r[c] for c in _TOKEN_COLS)

    def _all(self):
        return self.conn.execute("SELECT * FROM sessions").fetchall()

    # ------------------------------------------------------------------ #
    # KPIs / consultas
    # ------------------------------------------------------------------ #
    @_synchronized
    def overview(self) -> dict:
        rows = self._all()
        detail = [r for r in rows if r["source"] == "jsonl"]
        models = defaultdict(int)
        days = set()
        starts, ends = [], []
        tok = {c: 0 for c in _TOKEN_COLS}
        total_cost = 0.0
        for r in rows:
            if r["start"]:
                starts.append(r["start"]); days.add(r["start"][:10])
            if r["end"]:
                ends.append(r["end"])
        for r in detail:
            for c in _TOKEN_COLS:
                tok[c] += r[c]
            total_cost += self._row_cost(r)
            for m in json.loads(r["models"] or "[]"):
                if m and m != "<synthetic>":
                    models[m] += 1
        return {
            "root_path": self.root_path,
            "root_name": self.root_name,
            "last_sync": self.conn.execute(
                "SELECT value FROM meta WHERE key='last_sync'").fetchone() and
                self.conn.execute("SELECT value FROM meta WHERE key='last_sync'").fetchone()[0],
            "total_sessions": len(rows),
            "detail_sessions": len(detail),
            "pruned_sessions": sum(1 for r in rows if r["is_pruned"]),
            "history_only_sessions": sum(1 for r in rows if r["source"] == "history"),
            "total_projects": len(set(r["project"] for r in rows)),
            "total_user_prompts": sum(r["user_prompts"] for r in rows),
            "total_assistant_msgs": sum(r["assistant_msgs"] for r in rows),
            "total_tool_calls": sum(r["tool_calls"] for r in rows),
            "input_tokens": tok["input_tokens"],
            "output_tokens": tok["output_tokens"],
            "cache_write_tokens": tok["cache_write_5m"] + tok["cache_write_1h"],
            "cache_write_5m": tok["cache_write_5m"],
            "cache_write_1h": tok["cache_write_1h"],
            "cache_read_tokens": tok["cache_read"],
            "total_tokens": sum(tok.values()),
            "total_cost": round(total_cost, 2),
            "days_active": len(days),
            "first_activity": min(starts) if starts else None,
            "last_activity": max(ends) if ends else None,
            "models": sorted(models.items(), key=lambda x: -x[1]),
            "pricing_table": PRICING_TABLE,
        }

    @_synchronized
    def by_project(self) -> list[dict]:
        agg = defaultdict(lambda: {
            "project": "", "sessions": 0, "detail_sessions": 0, "pruned": 0,
            "user_prompts": 0, "assistant_msgs": 0, "tool_calls": 0,
            "total_tokens": 0, "cost": 0.0, "first": None, "last": None,
        })
        for r in self._all():
            a = agg[r["project"]]
            a["project"] = r["project"]
            a["sessions"] += 1
            if r["source"] == "jsonl":
                a["detail_sessions"] += 1
            if r["is_pruned"]:
                a["pruned"] += 1
            a["user_prompts"] += r["user_prompts"]
            a["assistant_msgs"] += r["assistant_msgs"]
            a["tool_calls"] += r["tool_calls"]
            a["total_tokens"] += self._total_tokens(r)
            a["cost"] += self._row_cost(r)
            if r["start"] and (a["first"] is None or r["start"] < a["first"]):
                a["first"] = r["start"]
            if r["end"] and (a["last"] is None or r["end"] > a["last"]):
                a["last"] = r["end"]
        out = list(agg.values())
        for a in out:
            a["cost"] = round(a["cost"], 2)
        out.sort(key=lambda x: -x["total_tokens"])
        return out

    @_synchronized
    def timeseries(self, granularity: str = "day") -> list[dict]:
        buckets = defaultdict(lambda: {
            "period": "", "sessions": 0, "user_prompts": 0, "assistant_msgs": 0,
            "tool_calls": 0, "input_tokens": 0, "output_tokens": 0,
            "cache_write_tokens": 0, "cache_read_tokens": 0,
            "total_tokens": 0, "cost": 0.0, "projects": set(),
        })
        for r in self._all():
            if not r["start"]:
                continue
            d = date.fromisoformat(r["start"][:10])
            if granularity == "day":
                key = d.isoformat()
            elif granularity == "week":
                iso = d.isocalendar()
                monday = d - timedelta(days=d.weekday())
                key = f"{iso[0]}-W{iso[1]:02d} ({monday.isoformat()})"
            elif granularity == "month":
                key = f"{d.year}-{d.month:02d}"
            else:
                key = d.isoformat()
            b = buckets[key]
            b["period"] = key
            b["sessions"] += 1
            b["user_prompts"] += r["user_prompts"]
            b["assistant_msgs"] += r["assistant_msgs"]
            b["tool_calls"] += r["tool_calls"]
            b["input_tokens"] += r["input_tokens"]
            b["output_tokens"] += r["output_tokens"]
            b["cache_write_tokens"] += r["cache_write_5m"] + r["cache_write_1h"]
            b["cache_read_tokens"] += r["cache_read"]
            b["total_tokens"] += self._total_tokens(r)
            b["cost"] += self._row_cost(r)
            b["projects"].add(r["project"])
        out = []
        for b in buckets.values():
            b["projects"] = sorted(b["projects"])
            b["projects_count"] = len(b["projects"])
            b["cost"] = round(b["cost"], 2)
            out.append(b)
        out.sort(key=lambda x: x["period"])
        return out

    @_synchronized
    def sessions_list(self) -> list[dict]:
        out = []
        for r in self.conn.execute(
            "SELECT * FROM sessions ORDER BY (start IS NULL), start DESC"
        ).fetchall():
            out.append(self._summary(r))
        return out

    def _summary(self, r) -> dict:
        models = json.loads(r["models"] or "[]")
        tools = json.loads(r["tools"] or "{}")
        commands = json.loads(r["commands"] or "{}")
        files = json.loads(r["files_touched"] or "[]")
        return {
            "session_id": r["session_id"],
            "project": r["project"],
            "cwd": r["cwd"],
            "git_branch": r["git_branch"],
            "title": r["title"] or (r["first_prompt"] or "")[:80] or "(sin título)",
            "agent_name": r["agent_name"],
            "version": r["version"],
            "start": r["start"], "end": r["end"],
            "duration_min": self._duration(r["start"], r["end"]),
            "models": models,
            "user_prompts": r["user_prompts"],
            "assistant_msgs": r["assistant_msgs"],
            "tool_calls": r["tool_calls"],
            "input_tokens": r["input_tokens"],
            "output_tokens": r["output_tokens"],
            "cache_write_tokens": r["cache_write_5m"] + r["cache_write_1h"],
            "cache_read_tokens": r["cache_read"],
            "total_tokens": self._total_tokens(r),
            "cost": round(self._row_cost(r), 4),
            "top_tools": sorted(tools.items(), key=lambda x: -x[1])[:10],
            "commands": sorted(commands.items(), key=lambda x: -x[1]),
            "files_touched": files[:50],
            "files_touched_count": len(files),
            "first_prompt": r["first_prompt"],
            "source": r["source"],
            "is_pruned": bool(r["is_pruned"]),
            "first_synced": r["first_synced"],
        }

    @staticmethod
    def _duration(start, end) -> float:
        if not (start and end):
            return 0.0
        try:
            a = datetime.fromisoformat(start)
            b = datetime.fromisoformat(end)
            return round((b - a).total_seconds() / 60, 1)
        except Exception:
            return 0.0

    @_synchronized
    def session_detail(self, session_id: str) -> dict | None:
        r = self.conn.execute(
            "SELECT * FROM sessions WHERE session_id=?", (session_id,)
        ).fetchone()
        if not r:
            return None
        d = self._summary(r)
        d["transcript"] = self._transcript(r)
        d["recap"] = self._recap(r)
        rec = self.conn.execute(
            "SELECT recap, generated_at, model, source FROM recaps WHERE session_id=?",
            (session_id,)).fetchone()
        d["ai_recap"] = dict(rec) if rec else None
        return d

    def _transcript(self, r) -> list[dict]:
        # Si el .jsonl sigue vivo, reconstruimos el hilo completo.
        path = P.jsonl_path_for(self.root_name, r["session_id"])
        if path:
            return self._transcript_from_jsonl(path)
        # Si fue purgada: sólo tenemos los prompts del usuario (history.jsonl).
        msgs = []
        for p in self.conn.execute(
            "SELECT timestamp, display FROM prompts WHERE session_id=? ORDER BY timestamp",
            (r["session_id"],)).fetchall():
            msgs.append({"role": "user", "kind": "message", "command": None,
                         "timestamp": p["timestamp"], "model": None,
                         "text": p["display"], "thinking": "", "tool_uses": [],
                         "output_tokens": 0})
        return msgs

    def _transcript_from_jsonl(self, path) -> list[dict]:
        msgs = []
        try:
            fh = open(path, encoding="utf-8", errors="replace")
        except OSError:
            return msgs
        with fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                t = d.get("type")
                if t not in ("user", "assistant"):
                    continue
                msg = d.get("message", {})
                content = msg.get("content")
                text = P.text_from_content(content)
                tool_uses, thinking = [], ""
                if isinstance(content, list):
                    for b in content:
                        if isinstance(b, dict):
                            if b.get("type") == "tool_use":
                                inp = b.get("input", {}) or {}
                                fp = (inp.get("file_path") or inp.get("command")
                                      or inp.get("path") or "")
                                tool_uses.append({"name": b.get("name", "?"),
                                                  "detail": str(fp)[:200]})
                            elif b.get("type") == "thinking":
                                thinking = (b.get("thinking", "") or "")[:1500]
                is_tool_result = isinstance(content, list) and any(
                    isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
                cmd = P.clean_command(text)
                kind = "message"
                if t == "user":
                    if is_tool_result:
                        kind = "tool_result"
                    elif cmd:
                        kind = "command"
                    elif P.is_meta_prompt(text):
                        kind = "meta"
                usage = msg.get("usage") or {}
                msgs.append({
                    "role": t, "kind": kind, "command": cmd,
                    "timestamp": d.get("timestamp"), "model": msg.get("model"),
                    "text": text[:8000], "thinking": thinking, "tool_uses": tool_uses,
                    "output_tokens": usage.get("output_tokens", 0) or 0,
                })
        return msgs

    def _recap(self, r) -> dict:
        tools = json.loads(r["tools"] or "{}")
        commands = json.loads(r["commands"] or "{}")
        files = json.loads(r["files_touched"] or "[]")
        samples = json.loads(r["prompt_samples"] or "[]")
        bullets = []
        if r["is_pruned"] and r["source"] == "history":
            bullets.append("⚠️ Sesión purgada por Claude Code (transcripción borrada). "
                           "Sólo se recuperaron los prompts desde history.jsonl.")
        if commands:
            cmds = ", ".join(f"/{c}" for c, _ in sorted(commands.items(), key=lambda x: -x[1])[:6])
            bullets.append(f"Comandos/skills invocados: {cmds}")
        if tools:
            tl = ", ".join(f"{n} ×{c}" for n, c in sorted(tools.items(), key=lambda x: -x[1])[:6])
            bullets.append(f"Herramientas más usadas: {tl}")
        if files:
            sample = sorted(os.path.basename(f) for f in files)[:8]
            bullets.append(f"{len(files)} archivo(s) leídos/editados (ej.: {', '.join(sample)})")
        if r["user_prompts"]:
            bullets.append(f"{r['user_prompts']} instrucción(es) del usuario en la sesión")
        return {
            "title": r["title"] or (r["first_prompt"] or "")[:80],
            "objective": (r["first_prompt"] or "")[:500],
            "bullets": bullets,
            "prompt_samples": samples[:12],
        }

    # ------------------------------------------------------------------ #
    # Recaps generados por IA (vía CLI de claude)
    # ------------------------------------------------------------------ #
    @_synchronized
    def save_recap(self, session_id: str, recap: str, model: str, source: str = "ai"):
        self.conn.execute(
            "INSERT OR REPLACE INTO recaps VALUES (?,?,?,?,?)",
            (session_id, recap, _now(), model, source))
        self.conn.commit()

    @_synchronized
    def get_session_cwd(self, session_id: str) -> str | None:
        r = self.conn.execute(
            "SELECT cwd FROM sessions WHERE session_id=?", (session_id,)).fetchone()
        return r["cwd"] if r else None

    @_synchronized
    def session_is_resumable(self, session_id: str) -> bool:
        return P.jsonl_path_for(self.root_name, session_id) is not None

    # ------------------------------------------------------------------ #
    # Consultas temporales para el MCP (por día / rango / búsqueda)
    # ------------------------------------------------------------------ #
    @staticmethod
    def _day(s: str | None) -> str | None:
        return s[:10] if s else None

    def _overlapping_rows(self, d0: str, d1: str):
        """Sesiones cuyo intervalo [start, end] intersecta [d0, d1] (días ISO).

        Incluye sesiones que: iniciaron en el rango, terminaron en el rango, o
        lo abarcan (empezaron antes y siguen/terminan después). Una sesión sin
        end usa su start como fin.
        """
        rows = []
        for r in self._all():
            sd = self._day(r["start"])
            if not sd:
                continue
            ed = self._day(r["end"]) or sd
            if sd <= d1 and ed >= d0:  # intersección de intervalos
                rows.append(r)
        rows.sort(key=lambda r: r["start"] or "")
        return rows

    @_synchronized
    def day_context(self, day: str) -> dict:
        """Qué pasó un día concreto (YYYY-MM-DD), con clasificación por sesión."""
        rows = self._overlapping_rows(day, day)
        sessions = []
        agg = {"sessions": 0, "user_prompts": 0, "tool_calls": 0,
               "total_tokens": 0, "cost": 0.0}
        projects = set()
        for r in rows:
            sd, ed = self._day(r["start"]), (self._day(r["end"]) or self._day(r["start"]))
            started = sd == day
            ended = ed == day
            if started and ended:
                relation = "iniciada y terminada ese día"
            elif started:
                relation = "iniciada ese día (continuó después)"
            elif ended:
                relation = "terminada ese día (venía de antes)"
            else:
                relation = "en curso ese día (empezó antes y siguió después)"
            rec = self._recap(r)
            sessions.append({
                "session_id": r["session_id"], "project": r["project"],
                "title": r["title"] or (r["first_prompt"] or "")[:80] or "(sin título)",
                "relation": relation, "started": started, "ended": ended,
                "start": r["start"], "end": r["end"],
                "user_prompts": r["user_prompts"], "tool_calls": r["tool_calls"],
                "total_tokens": self._total_tokens(r),
                "cost": round(self._row_cost(r), 4),
                "models": json.loads(r["models"] or "[]"),
                "is_pruned": bool(r["is_pruned"]),
                "objective": rec["objective"], "did": rec["bullets"],
                "prompt_samples": rec["prompt_samples"],
            })
            agg["sessions"] += 1
            agg["user_prompts"] += r["user_prompts"]
            agg["tool_calls"] += r["tool_calls"]
            agg["total_tokens"] += self._total_tokens(r)
            agg["cost"] += self._row_cost(r)
            projects.add(r["project"])
        agg["cost"] = round(agg["cost"], 2)
        agg["projects"] = sorted(projects)
        return {"day": day, "summary": agg, "sessions": sessions}

    @_synchronized
    def range_context(self, d0: str, d1: str, label: str = "") -> dict:
        """Resumen agregado de un rango de días [d0, d1] (semana, mes, etc.)."""
        rows = self._overlapping_rows(d0, d1)
        by_project = defaultdict(lambda: {"sessions": 0, "tokens": 0, "cost": 0.0,
                                          "prompts": 0})
        by_day = defaultdict(int)
        agg = {"sessions": 0, "user_prompts": 0, "tool_calls": 0,
               "total_tokens": 0, "cost": 0.0}
        sess = []
        for r in rows:
            p = by_project[r["project"]]
            p["sessions"] += 1
            p["tokens"] += self._total_tokens(r)
            p["cost"] = round(p["cost"] + self._row_cost(r), 2)
            p["prompts"] += r["user_prompts"]
            by_day[self._day(r["start"])] += 1
            agg["sessions"] += 1
            agg["user_prompts"] += r["user_prompts"]
            agg["tool_calls"] += r["tool_calls"]
            agg["total_tokens"] += self._total_tokens(r)
            agg["cost"] += self._row_cost(r)
            sess.append({
                "session_id": r["session_id"], "project": r["project"],
                "title": r["title"] or (r["first_prompt"] or "")[:80] or "(sin título)",
                "start": r["start"], "end": r["end"],
                "total_tokens": self._total_tokens(r),
                "user_prompts": r["user_prompts"], "is_pruned": bool(r["is_pruned"]),
            })
        agg["cost"] = round(agg["cost"], 2)
        return {
            "label": label or f"{d0} → {d1}", "from": d0, "to": d1,
            "summary": agg,
            "by_project": [{"project": k, **v} for k, v in
                           sorted(by_project.items(), key=lambda x: -x[1]["tokens"])],
            "by_day": [{"day": k, "sessions": v} for k, v in sorted(by_day.items())],
            "sessions": sorted(sess, key=lambda x: x["start"] or ""),
        }

    @_synchronized
    def search(self, query: str, limit: int = 25) -> list[dict]:
        """Busca en títulos, primer prompt y prompts recuperados."""
        like = f"%{query}%"
        ids = set()
        for r in self.conn.execute(
            "SELECT session_id FROM sessions WHERE title LIKE ? OR first_prompt LIKE ? "
            "OR prompt_samples LIKE ?", (like, like, like)).fetchall():
            ids.add(r["session_id"])
        for r in self.conn.execute(
            "SELECT DISTINCT session_id FROM prompts WHERE display LIKE ?", (like,)).fetchall():
            if r["session_id"]:
                ids.add(r["session_id"])
        out = []
        for sid in ids:
            r = self.conn.execute(
                "SELECT * FROM sessions WHERE session_id=?", (sid,)).fetchone()
            if not r:
                continue
            out.append({
                "session_id": r["session_id"], "project": r["project"],
                "title": r["title"] or (r["first_prompt"] or "")[:80] or "(sin título)",
                "start": r["start"], "end": r["end"],
                "first_prompt": r["first_prompt"],
                "total_tokens": self._total_tokens(r), "is_pruned": bool(r["is_pruned"]),
            })
        out.sort(key=lambda x: x["start"] or "", reverse=True)
        return out[:limit]
