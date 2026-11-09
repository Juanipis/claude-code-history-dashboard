"""Parser del historial de Claude Code — 3 fuentes de datos.

Claude Code persiste información en varias ubicaciones de ~/.claude. Este parser
las combina para dar la imagen MÁS COMPLETA posible (y honesta) del uso:

1. ~/.claude/projects/<ruta>/<sessionId>.jsonl
   Transcripción completa por sesión: mensajes, modelo, tokens (message.usage),
   herramientas, comandos. Es el detalle rico — PERO Claude Code BORRA estos
   archivos automáticamente tras `cleanupPeriodDays` (default 30 días). Por eso
   sesiones viejas desaparecen de aquí.

2. ~/.claude/history.jsonl
   Log GLOBAL de prompts del usuario: {display, timestamp, project, sessionId}.
   Sobrevive aunque la sesión .jsonl ya haya sido purgada. Nos permite saber que
   una sesión EXISTIÓ y qué se pidió, recuperando historial perdido.

3. ~/.claude/sessions/<pid>.json
   Registro de sesiones VIVAS: {pid, sessionId, cwd, status, updatedAt, name}.
   Permite detección en tiempo real de sesiones activas (status busy/idle) y
   verificar si el proceso sigue vivo (os.kill(pid, 0)).

NOTA: nunca recorremos los árboles de proyecto reales → no se tocan node_modules,
.venv, etc. Sólo leemos el almacén central de ~/.claude.
"""

from __future__ import annotations

import json
import os
import glob
import signal
from collections import defaultdict
from datetime import datetime, timezone
from dataclasses import dataclass, field

from pricing import cost_for, family_for_model

CLAUDE_DIR = os.path.expanduser("~/.claude")
CLAUDE_PROJECTS = os.path.join(CLAUDE_DIR, "projects")
HISTORY_FILE = os.path.join(CLAUDE_DIR, "history.jsonl")
SESSIONS_DIR = os.path.join(CLAUDE_DIR, "sessions")


# --------------------------------------------------------------------------- #
# Utilidades
# --------------------------------------------------------------------------- #
def parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


def ts_from_ms(ms) -> datetime | None:
    try:
        return datetime.fromtimestamp(ms / 1000, timezone.utc)
    except Exception:
        return None


def project_key(cwd: str, root_path: str) -> str:
    if not cwd:
        return "(desconocido)"
    if cwd == root_path:
        return "(raíz)"
    if cwd.startswith(root_path + "/"):
        rel = cwd[len(root_path) + 1:]
        return rel.split("/")[0]
    return "(externo)"


def text_from_content(content) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    out = []
    for block in content:
        if not isinstance(block, dict):
            continue
        t = block.get("type")
        if t == "text":
            out.append(block.get("text", ""))
        elif t == "tool_result":
            c = block.get("content")
            if isinstance(c, str):
                out.append(c)
            elif isinstance(c, list):
                for cc in c:
                    if isinstance(cc, dict) and cc.get("type") == "text":
                        out.append(cc.get("text", ""))
    return "\n".join(out).strip()


def clean_command(text: str) -> str | None:
    if "<command-name>" in text:
        try:
            seg = text.split("<command-name>", 1)[1].split("</command-name>", 1)[0]
            return seg.strip().lstrip("/")
        except Exception:
            return None
    return None


def is_meta_prompt(text: str) -> bool:
    if not text:
        return True
    s = text.lstrip()
    return (s.startswith("<command-") or s.startswith("<local-command")
            or s.startswith("Caveat:") or s.startswith("<system-reminder"))


# --------------------------------------------------------------------------- #
# Sesión (transcripción .jsonl)
# --------------------------------------------------------------------------- #
@dataclass
class Session:
    session_id: str
    file: str
    project: str = "(desconocido)"
    cwd: str = ""
    git_branch: str = ""
    title: str = ""
    agent_name: str = ""
    version: str = ""
    start: datetime | None = None
    end: datetime | None = None
    models: set = field(default_factory=set)
    user_prompts: int = 0
    assistant_msgs: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_5m: int = 0
    cache_write_1h: int = 0
    cache_read: int = 0
    cost: float = 0.0
    tools: dict = field(default_factory=lambda: defaultdict(int))
    commands: dict = field(default_factory=lambda: defaultdict(int))
    files_touched: set = field(default_factory=set)
    first_prompt: str = ""
    user_prompt_samples: list = field(default_factory=list)
    file_mtime: float = 0.0

    @property
    def cache_write(self) -> int:
        return self.cache_write_5m + self.cache_write_1h

    @property
    def total_tokens(self) -> int:
        return (self.input_tokens + self.output_tokens
                + self.cache_write + self.cache_read)

    def as_row(self) -> dict:
        """Forma serializable para persistir en SQLite."""
        return {
            "session_id": self.session_id,
            "project": self.project,
            "cwd": self.cwd,
            "git_branch": self.git_branch,
            "title": self.title,
            "agent_name": self.agent_name,
            "version": self.version,
            "start": self.start.isoformat() if self.start else None,
            "end": self.end.isoformat() if self.end else None,
            "models": sorted(m for m in self.models if m and m != "<synthetic>"),
            "user_prompts": self.user_prompts,
            "assistant_msgs": self.assistant_msgs,
            "tool_calls": self.tool_calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_write_5m": self.cache_write_5m,
            "cache_write_1h": self.cache_write_1h,
            "cache_read": self.cache_read,
            "tools": dict(sorted(self.tools.items(), key=lambda x: -x[1])),
            "commands": dict(sorted(self.commands.items(), key=lambda x: -x[1])),
            "files_touched": sorted(self.files_touched),
            "first_prompt": self.first_prompt,
            "prompt_samples": self.user_prompt_samples[:20],
            "source": "jsonl",
            "is_pruned": 0,
            "file_mtime": self.file_mtime,
        }


def parse_file(path: str, root_path: str) -> Session | None:
    sid = os.path.splitext(os.path.basename(path))[0]
    s = Session(session_id=sid, file=path)
    try:
        s.file_mtime = os.path.getmtime(path)
        fh = open(path, "r", encoding="utf-8", errors="replace")
    except OSError:
        return None

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

            if d.get("cwd") and not s.cwd:
                s.cwd = d["cwd"]
                s.project = project_key(d["cwd"], root_path)
            if d.get("gitBranch") and not s.git_branch:
                s.git_branch = d["gitBranch"]
            if d.get("version"):
                s.version = d["version"]

            ts = parse_ts(d.get("timestamp"))
            if ts:
                if s.start is None or ts < s.start:
                    s.start = ts
                if s.end is None or ts > s.end:
                    s.end = ts

            if t == "ai-title":
                s.title = d.get("aiTitle", s.title)
            elif t == "agent-name":
                s.agent_name = d.get("agentName") or d.get("name") or s.agent_name
            elif t == "user":
                msg = d.get("message", {})
                content = msg.get("content")
                text = text_from_content(content)
                cmd = clean_command(text)
                if cmd:
                    s.commands[cmd] += 1
                is_tool_result = isinstance(content, list) and any(
                    isinstance(b, dict) and b.get("type") == "tool_result"
                    for b in content
                )
                if not is_tool_result and not is_meta_prompt(text) and not cmd:
                    s.user_prompts += 1
                    if not s.first_prompt:
                        s.first_prompt = text[:500]
                    if len(s.user_prompt_samples) < 40:
                        s.user_prompt_samples.append(text[:300])
            elif t == "assistant":
                msg = d.get("message", {})
                model = msg.get("model")
                if model:
                    s.models.add(model)
                s.assistant_msgs += 1
                content = msg.get("content")
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "tool_use":
                            s.tools[block.get("name", "?")] += 1
                            s.tool_calls += 1
                            inp = block.get("input", {}) or {}
                            fp = (inp.get("file_path") or inp.get("path")
                                  or inp.get("notebook_path"))
                            if fp:
                                s.files_touched.add(fp)
                usage = msg.get("usage") or {}
                it = usage.get("input_tokens", 0) or 0
                ot = usage.get("output_tokens", 0) or 0
                cr = usage.get("cache_read_input_tokens", 0) or 0
                cc = usage.get("cache_creation") or {}
                c5 = cc.get("ephemeral_5m_input_tokens", 0) or 0
                c1 = cc.get("ephemeral_1h_input_tokens", 0) or 0
                # Si no viene el desglose, todo el cache_creation se trata como 5m.
                if not c5 and not c1:
                    c5 = usage.get("cache_creation_input_tokens", 0) or 0
                s.input_tokens += it
                s.output_tokens += ot
                s.cache_write_5m += c5
                s.cache_write_1h += c1
                s.cache_read += cr
                s.cost += cost_for(model, it, ot, c5, c1, cr)

    if s.assistant_msgs == 0 and s.user_prompts == 0:
        return None
    return s


def discover_files(root_name: str) -> list[str]:
    files = []
    for d in glob.glob(os.path.join(CLAUDE_PROJECTS, f"*{root_name}*")):
        if os.path.isdir(d):
            files.extend(glob.glob(os.path.join(d, "*.jsonl")))
    return files


# --------------------------------------------------------------------------- #
# Fuente 2: history.jsonl (prompts globales — recupera sesiones purgadas)
# --------------------------------------------------------------------------- #
def parse_history(root_path: str) -> list[dict]:
    """Devuelve prompts del usuario relacionados al root, desde history.jsonl."""
    rows = []
    if not os.path.exists(HISTORY_FILE):
        return rows
    for line in open(HISTORY_FILE, encoding="utf-8", errors="replace"):
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        project = d.get("project") or ""
        if not project.startswith(root_path):
            continue
        display = d.get("display") or ""
        if display.startswith("/"):  # comandos como /voice, /clear
            continue
        rows.append({
            "session_id": d.get("sessionId"),
            "project": project_key(project, root_path),
            "cwd": project,
            "timestamp": ts_from_ms(d.get("timestamp")),
            "display": display[:500],
        })
    return rows


# --------------------------------------------------------------------------- #
# Fuente 3: registro de sesiones vivas
# --------------------------------------------------------------------------- #
def pid_alive(pid: int) -> bool:
    """¿Sigue vivo el proceso? PermissionError = existe pero es de otro usuario."""
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except Exception:
        return False


def live_sessions(root_path: str) -> list[dict]:
    """Sesiones actualmente abiertas (proceso vivo) cuyo cwd cae bajo el root."""
    out = []
    if not os.path.isdir(SESSIONS_DIR):
        return out
    for f in glob.glob(os.path.join(SESSIONS_DIR, "*.json")):
        try:
            d = json.load(open(f, encoding="utf-8", errors="replace"))
        except Exception:
            continue
        pid = d.get("pid")
        cwd = d.get("cwd") or ""
        if not pid or not pid_alive(pid):
            continue
        if not (cwd == root_path or cwd.startswith(root_path)):
            continue
        out.append({
            "pid": pid,
            "session_id": d.get("sessionId"),
            "project": project_key(cwd, root_path),
            "cwd": cwd,
            "status": d.get("status"),          # busy | idle
            "kind": d.get("kind"),
            "name": d.get("name"),
            "started_at": ts_from_ms(d.get("startedAt")).isoformat() if d.get("startedAt") else None,
            "updated_at": ts_from_ms(d.get("updatedAt")).isoformat() if d.get("updatedAt") else None,
            "version": d.get("version"),
        })
    return out


def jsonl_path_for(root_name: str, session_id: str) -> str | None:
    """Ubica el .jsonl vivo de una sesión por id (para re-sync incremental)."""
    for d in glob.glob(os.path.join(CLAUDE_PROJECTS, f"*{root_name}*")):
        p = os.path.join(d, f"{session_id}.jsonl")
        if os.path.exists(p):
            return p
    return None
