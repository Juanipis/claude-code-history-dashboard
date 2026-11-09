"""Servidor MCP local del historial de Claude Code (transporte stdio).

Lo lanza Claude Code bajo demanda (no necesita estar siempre activo). Lee el
mismo backup SQLite del dashboard y lo sincroniza al arrancar, así que funciona
aunque el dashboard web no esté corriendo.

Registro (ver instrucciones en el dashboard o el README):
    claude mcp add claude-history -s user \
      -e CLAUDE_ROOT_PATH=<tu-carpeta> \
      -- uv run --with mcp python <ruta>/backend/mcp_server.py

Pensado para responder preguntas como:
  - "¿Qué hice el 22 de abril?"  -> what_did_i_do("2026-04-22")
  - "¿Y esa semana?"             -> summarize_week("2026-04-22")
  - "¿Y en abril?"               -> summarize_month("2026-04")
Una sesión cuenta para un día si lo INICIÓ, lo TERMINÓ, o estuvo EN CURSO ese
día (empezó antes y siguió/terminó después).
"""

from __future__ import annotations

import os
import sys
import time
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp.server.fastmcp import FastMCP
import config
from db import HistoryDB
from pricing import PRICING_TABLE, _BASE

ROOT_PATH = config.ROOT_PATH

mcp = FastMCP(config.MCP_NAME)
db = HistoryDB(ROOT_PATH)
db.sync()
_last_sync = time.time()


def _refresh(min_age: float = 30.0):
    """Re-sincroniza disco→SQLite si pasó suficiente tiempo (barato/incremental)."""
    global _last_sync
    if time.time() - _last_sync > min_age:
        try:
            db.sync()
            _last_sync = time.time()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# Formateadores (markdown legible para Claude Code)
# --------------------------------------------------------------------------- #
def _fmt_tokens(n: int) -> str:
    n = n or 0
    if n >= 1e9: return f"{n/1e9:.2f}B"
    if n >= 1e6: return f"{n/1e6:.2f}M"
    if n >= 1e3: return f"{n/1e3:.1f}K"
    return str(n)


def _fmt_session_block(s: dict) -> str:
    head = f"### {s['title']}  ·  _{s['project']}_"
    if s.get("relation"):
        head += f"  ·  ({s['relation']})"
    if s.get("is_pruned"):
        head += "  ⚠️ purgada (sólo prompts recuperados)"
    lines = [head]
    span = ""
    if s.get("start"):
        span = f"{s['start'][:16].replace('T',' ')}"
        if s.get("end"):
            span += f" → {s['end'][:16].replace('T',' ')}"
    meta = f"- {span}  ·  {s.get('user_prompts',0)} prompts"
    if s.get("tool_calls") is not None:
        meta += f"  ·  {s.get('tool_calls',0)} tool calls"
    if s.get("total_tokens"):
        meta += f"  ·  {_fmt_tokens(s['total_tokens'])} tokens"
    if s.get("models"):
        meta += f"  ·  {', '.join(m.replace('claude-','') for m in s['models'])}"
    lines.append(meta)
    if s.get("objective"):
        lines.append(f"- **Objetivo:** {s['objective']}")
    for b in s.get("did", []):
        lines.append(f"- {b}")
    if s.get("prompt_samples"):
        lines.append("- **Algunos prompts del usuario:**")
        for p in s["prompt_samples"][:5]:
            lines.append(f"  - {p}")
    lines.append(f"- _session_id_: `{s['session_id']}`")
    return "\n".join(lines)


def _disclaimer() -> str:
    return ("\n\n> Nota: el costo es estimación equivalente-API (con suscripción no "
            "pagas por token). Sesiones marcadas ⚠️ fueron purgadas por Claude Code "
            "(cleanupPeriodDays); de ellas sólo se recuperaron los prompts.")


# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #
@mcp.tool()
def what_did_i_do(day: str) -> str:
    """Resume qué se hizo con Claude Code en un día concreto.

    Incluye sesiones que iniciaron, terminaron O estuvieron en curso ese día
    (empezaron antes y siguieron después). Útil para "¿qué hice el 22 de abril?".

    Args:
        day: fecha en formato ISO YYYY-MM-DD (ej. "2026-04-22").
    """
    _refresh()
    try:
        date.fromisoformat(day)
    except ValueError:
        return f"Fecha inválida '{day}'. Usa el formato YYYY-MM-DD (ej. 2026-04-22)."
    ctx = db.day_context(day)
    s = ctx["summary"]
    if not ctx["sessions"]:
        return f"No hay actividad de Claude Code registrada el {day} en {db.root_name}."
    header = (f"## Actividad del {day}\n"
              f"**{s['sessions']} sesión(es)** · {s['user_prompts']} prompts · "
              f"{s['tool_calls']} tool calls · {_fmt_tokens(s['total_tokens'])} tokens · "
              f"~${s['cost']} (est. API)\n"
              f"Proyectos: {', '.join(s['projects'])}\n")
    body = "\n\n".join(_fmt_session_block(x) for x in ctx["sessions"])
    return header + "\n" + body + _disclaimer()


@mcp.tool()
def summarize_week(date_in_week: str) -> str:
    """Resume la SEMANA (lunes a domingo) que contiene la fecha dada.

    Args:
        date_in_week: cualquier fecha ISO YYYY-MM-DD dentro de la semana.
    """
    _refresh()
    try:
        d = date.fromisoformat(date_in_week)
    except ValueError:
        return f"Fecha inválida '{date_in_week}'. Usa YYYY-MM-DD."
    monday = d - timedelta(days=d.weekday())
    sunday = monday + timedelta(days=6)
    return _format_range(monday.isoformat(), sunday.isoformat(),
                         f"Semana {monday.isoformat()} → {sunday.isoformat()}")


@mcp.tool()
def summarize_month(month: str) -> str:
    """Resume un MES completo.

    Args:
        month: mes en formato YYYY-MM (ej. "2026-04").
    """
    _refresh()
    try:
        y, m = map(int, month.split("-"))
        d0 = date(y, m, 1)
        d1 = date(y + (m == 12), (m % 12) + 1, 1) - timedelta(days=1)
    except Exception:
        return f"Mes inválido '{month}'. Usa YYYY-MM (ej. 2026-04)."
    return _format_range(d0.isoformat(), d1.isoformat(), f"Mes {month}")


@mcp.tool()
def summarize_range(start: str, end: str) -> str:
    """Resume un rango de fechas arbitrario [start, end] (inclusive).

    Args:
        start: fecha ISO YYYY-MM-DD de inicio.
        end: fecha ISO YYYY-MM-DD de fin.
    """
    _refresh()
    try:
        date.fromisoformat(start); date.fromisoformat(end)
    except ValueError:
        return "Fechas inválidas. Usa YYYY-MM-DD para start y end."
    return _format_range(start, end, f"{start} → {end}")


def _format_range(d0: str, d1: str, label: str) -> str:
    ctx = db.range_context(d0, d1, label)
    s = ctx["summary"]
    if not s["sessions"]:
        return f"No hay actividad de Claude Code entre {d0} y {d1} en {db.root_name}."
    out = [f"## {label}",
           f"**{s['sessions']} sesiones** · {s['user_prompts']} prompts · "
           f"{s['tool_calls']} tool calls · {_fmt_tokens(s['total_tokens'])} tokens · "
           f"~${s['cost']} (est. API)\n",
           "### Por proyecto"]
    for p in ctx["by_project"]:
        out.append(f"- **{p['project']}**: {p['sessions']} sesiones, "
                   f"{p['prompts']} prompts, {_fmt_tokens(p['tokens'])} tokens, ~${p['cost']}")
    out.append("\n### Sesiones (cronológico)")
    for x in ctx["sessions"]:
        tag = " ⚠️" if x["is_pruned"] else ""
        when = (x["start"] or "")[:10]
        out.append(f"- `{when}` _{x['project']}_ — {x['title']}{tag} "
                   f"(`{x['session_id']}`)")
    return "\n".join(out) + _disclaimer()


@mcp.tool()
def search_sessions(query: str, limit: int = 25) -> str:
    """Busca sesiones por texto en títulos, primer prompt o prompts del usuario.

    Args:
        query: texto a buscar (ej. "RiskCanvas", "permisos", "deploy").
        limit: máximo de resultados (default 25).
    """
    _refresh()
    res = db.search(query, limit)
    if not res:
        return f"Sin resultados para '{query}'."
    out = [f"## {len(res)} resultado(s) para '{query}'"]
    for r in res:
        tag = " ⚠️purgada" if r["is_pruned"] else ""
        out.append(f"- `{(r['start'] or '')[:10]}` _{r['project']}_ — **{r['title']}**{tag}\n"
                   f"  {(r['first_prompt'] or '')[:160]}\n  (`{r['session_id']}`)")
    return "\n".join(out)


@mcp.tool()
def session_detail(session_id: str) -> str:
    """Detalle de una sesión: objetivo, qué se hizo, métricas y prompts.

    Args:
        session_id: el id de la sesión (lo devuelven las otras herramientas).
    """
    _refresh()
    d = db.session_detail(session_id)
    if not d:
        return f"No se encontró la sesión {session_id}."
    r = d["recap"]
    out = [f"## {d['title']}  ·  _{d['project']}_",
           f"{(d['start'] or '')[:16].replace('T',' ')} → {(d['end'] or '')[:16].replace('T',' ')} "
           f"· {d['duration_min']} min · {d['user_prompts']} prompts · {d['tool_calls']} tools · "
           f"{_fmt_tokens(d['total_tokens'])} tokens · ~${d['cost']} (est. API)",
           f"Modelos: {', '.join(m.replace('claude-','') for m in d['models']) or '—'}"]
    if d["is_pruned"]:
        out.append("⚠️ Sesión purgada: sólo prompts recuperados de history.jsonl.")
    if r["objective"]:
        out.append(f"\n**Objetivo:** {r['objective']}")
    if r["bullets"]:
        out.append("\n**Qué se hizo:**")
        out += [f"- {b}" for b in r["bullets"]]
    if d.get("ai_recap"):
        out.append(f"\n**Recap IA:**\n{d['ai_recap']['recap']}")
    if r["prompt_samples"]:
        out.append("\n**Prompts del usuario:**")
        out += [f"- {p}" for p in r["prompt_samples"]]
    if d.get("files_touched"):
        out.append(f"\n**Archivos ({d['files_touched_count']}):** "
                   + ", ".join(os.path.basename(f) for f in d["files_touched"][:15]))
    return "\n".join(out)


@mcp.tool()
def list_projects() -> str:
    """Lista los subproyectos con sus métricas agregadas."""
    _refresh()
    out = ["## Proyectos"]
    for p in db.by_project():
        out.append(f"- **{p['project']}**: {p['sessions']} sesiones "
                   f"({p['pruned']} purgadas), {p['user_prompts']} prompts, "
                   f"{_fmt_tokens(p['total_tokens'])} tokens, ~${p['cost']} · "
                   f"{(p['first'] or '')[:10]} → {(p['last'] or '')[:10]}")
    return "\n".join(out)


@mcp.tool()
def overview() -> str:
    """Visión global del historial de Claude Code (KPIs)."""
    _refresh()
    o = db.overview()
    return (f"## {o['root_name']} — historial de Claude Code\n"
            f"- **{o['total_sessions']} sesiones** ({o['detail_sessions']} con detalle, "
            f"{o['history_only_sessions']} recuperadas tras purga)\n"
            f"- {o['total_user_prompts']} prompts · {o['total_tool_calls']} tool calls\n"
            f"- {_fmt_tokens(o['total_tokens'])} tokens (cache read: {_fmt_tokens(o['cache_read_tokens'])})\n"
            f"- ~${o['total_cost']} estimado (equivalente-API; con suscripción NO es tu factura)\n"
            f"- {o['days_active']} días activos: {(o['first_activity'] or '')[:10]} → "
            f"{(o['last_activity'] or '')[:10]}\n"
            f"- Modelos: {', '.join(f'{m}({c})' for m,c in o['models'])}")


@mcp.tool()
def active_sessions() -> str:
    """Sesiones de Claude Code ABIERTAS ahora mismo (en vivo: busy/idle)."""
    import parser as P
    live = P.live_sessions(ROOT_PATH)
    if not live:
        return "No hay sesiones de Claude Code activas en este momento."
    out = ["## Sesiones activas ahora"]
    for a in live:
        out.append(f"- **{a['status'].upper()}** _{a['project']}_ — {a.get('name') or '(sin nombre)'} "
                   f"· pid {a['pid']} · act. {(a.get('updated_at') or '')[:16].replace('T',' ')} "
                   f"(`{a['session_id']}`)")
    return "\n".join(out)


@mcp.tool()
def current_models() -> str:
    """Lista los modelos actuales de Claude y los precios usados para estimar costo.

    Referencia para tenerlos siempre presentes (fuente: skill claude-api).
    """
    cur = {
        "claude-fable-5": "Fable 5 — el más potente (tier sobre Opus)",
        "claude-opus-4-8": "Opus 4.8 — Opus más capaz, autónomo (recomendado por defecto)",
        "claude-opus-4-7": "Opus 4.7",
        "claude-opus-4-6": "Opus 4.6",
        "claude-sonnet-4-6": "Sonnet 4.6 — mejor balance velocidad/inteligencia",
        "claude-haiku-4-5": "Haiku 4.5 — el más rápido y barato",
    }
    out = ["## Modelos Claude actuales", *[f"- `{k}` — {v}" for k, v in cur.items()],
           "\n## Precios usados (USD / 1M tokens)",
           "| Familia | input | output | cache 5m | cache 1h | cache read |",
           "|---|---|---|---|---|---|"]
    for p in PRICING_TABLE:
        out.append(f"| {p['family']} | ${p['input']} | ${p['output']} | "
                   f"${p['cache_write_5m']} | ${p['cache_write_1h']} | ${p['cache_read']} |")
    out.append("\n> Reglas de cache: write 5m = 1.25× input · write 1h = 2× input · read = 0.1× input.")
    return "\n".join(out)


if __name__ == "__main__":
    mcp.run()  # stdio por defecto
