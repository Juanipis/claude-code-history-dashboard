"""Servidor del dashboard de historial de Claude Code.

Arquitectura:
  - db.HistoryDB: backup en SQLite (superconjunto durable) + consultas.
  - Hilo de fondo: re-sincroniza disco→SQLite cada SYNC_EVERY s (detecta sesiones
    purgadas y recupera prompts de history.jsonl).
  - /api/active: lee el registro de sesiones VIVAS (~/.claude/sessions) en tiempo
    real y re-sincroniza la(s) sesión(es) activa(s) para tokens al instante.
  - /api/sessions/{id}/recap (POST): genera un recap con IA ejecutando el CLI
    `claude --resume <id> -p "..."` (usa tu auth/suscripción local).

Arranque:  ./run.sh   (o: uv run uvicorn app:app --app-dir backend --port 8765)
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import config
import parser as P
import service
from db import HistoryDB

ROOT_PATH = config.ROOT_PATH
PORT = config.PORT
STATIC_DIR = os.path.join(os.path.dirname(__file__), "..", "static")
BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
SYNC_EVERY = config.SYNC_EVERY  # segundos

app = FastAPI(title="Claude History Dashboard")

print(f"[claude-history] analyzing root: {ROOT_PATH}  ·  port: {PORT}  ·  "
      f"sync every {SYNC_EVERY}s")
if not config.CONFIGURED:
    print("[claude-history] no config.json found — using defaults. "
          "Run `python3 setup.py` to configure interactively.")

db = HistoryDB(ROOT_PATH)
_lock = threading.Lock()


def _sync():
    with _lock:
        return db.sync()


# Sync inicial + hilo de fondo.
_sync()


def _bg_loop():
    while True:
        time.sleep(SYNC_EVERY)
        try:
            _sync()
        except Exception as e:  # nunca matar el hilo
            print("sync error:", e)


threading.Thread(target=_bg_loop, daemon=True).start()


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #
@app.get("/api/overview")
def overview():
    return db.overview()


@app.get("/api/projects")
def projects():
    return db.by_project()


@app.get("/api/timeseries/{granularity}")
def timeseries(granularity: str):
    if granularity not in ("day", "week", "month"):
        raise HTTPException(400, "granularity debe ser day|week|month")
    return db.timeseries(granularity)


@app.get("/api/sessions")
def sessions():
    return db.sessions_list()


@app.get("/api/active")
def active():
    """Sesiones vivas en tiempo real, con sus métricas frescas."""
    live = P.live_sessions(ROOT_PATH)
    enriched = []
    for s in live:
        sid = s.get("session_id")
        if sid:
            with _lock:
                db.sync_one(sid)  # refresca tokens al instante
            detail = db.session_detail(sid)
            if detail:
                s["title"] = detail["title"]
                s["total_tokens"] = detail["total_tokens"]
                s["cost"] = detail["cost"]
                s["user_prompts"] = detail["user_prompts"]
                s["tool_calls"] = detail["tool_calls"]
                s["models"] = detail["models"]
                s["last_tools"] = detail["top_tools"][:5]
                s["first_prompt"] = detail["first_prompt"]
        enriched.append(s)
    # busy primero, luego por updated_at desc
    enriched.sort(key=lambda x: (x.get("status") != "busy",
                                 x.get("updated_at") or ""), reverse=False)
    return enriched


@app.get("/api/sessions/{session_id}")
def session_detail(session_id: str):
    d = db.session_detail(session_id)
    if not d:
        raise HTTPException(404, "sesión no encontrada")
    return d


@app.post("/api/reload")
def reload_store():
    return _sync()


# --- MCP + servicio en segundo plano --------------------------------------- #
def _mcp_command() -> str:
    uv = shutil.which("uv") or "uv"
    script = os.path.join(BACKEND_DIR, "mcp_server.py")
    return (f'claude mcp add {config.MCP_NAME} -s user '
            f'-e CLAUDE_ROOT_PATH={ROOT_PATH} '
            f'-- {uv} run --with mcp python {script}')


@app.get("/api/mcp/info")
def mcp_info():
    """Datos e instrucciones para registrar el MCP local en Claude Code."""
    return {
        "name": config.MCP_NAME,
        "scope": "user",
        "transport": "stdio",
        "install_command": _mcp_command(),
        "remove_command": f"claude mcp remove {config.MCP_NAME} -s user",
        "list_command": "claude mcp list",
        "server_path": os.path.join(BACKEND_DIR, "mcp_server.py"),
        "tools": [
            ["what_did_i_do(day)", "qué se hizo un día (inició/terminó/en curso)"],
            ["summarize_week(date_in_week)", "resumen de la semana (lun–dom)"],
            ["summarize_month(month)", "resumen de un mes (YYYY-MM)"],
            ["summarize_range(start, end)", "resumen de un rango arbitrario"],
            ["search_sessions(query)", "buscar sesiones por texto"],
            ["session_detail(session_id)", "detalle de una sesión"],
            ["list_projects()", "métricas por subproyecto"],
            ["overview()", "KPIs globales"],
            ["active_sessions()", "sesiones abiertas ahora (busy/idle)"],
            ["current_models()", "modelos Claude actuales + precios"],
        ],
        "example_prompts": [
            "¿Qué hice el 22 de abril?",
            "Resume mi semana del 20 de abril",
            "¿En qué trabajé en mayo en RiskCanvas?",
            "Busca sesiones sobre permisos de Power Pages",
        ],
    }


@app.get("/api/service/status")
def service_status():
    return service.status(PORT)


@app.post("/api/service/install")
def service_install():
    return service.install(PORT, ROOT_PATH)


@app.post("/api/service/uninstall")
def service_uninstall():
    return service.uninstall()


RECAP_PROMPT = (
    "Genera un /recap de ESTA sesión para un dashboard de historial. "
    "Resume de forma concisa y en español: (1) el objetivo, (2) qué se hizo y los "
    "cambios clave, (3) el estado actual / qué quedó pendiente. "
    "Usa viñetas. No incluyas preámbulo ni preguntes nada; sólo el resumen."
)


class RecapResult(BaseModel):
    ok: bool
    recap: str | None = None
    error: str | None = None
    cached: bool = False


@app.post("/api/sessions/{session_id}/recap", response_model=RecapResult)
def generate_recap(session_id: str, force: bool = False):
    """Genera (o devuelve cacheado) un recap con IA vía el CLI de claude.

    Ejecuta `claude --resume <id> -p "<prompt>"` en el cwd de la sesión, usando
    tu autenticación local (suscripción/clave). Consume tokens. Es un botón
    explícito, no automático.
    """
    detail = db.session_detail(session_id)
    if not detail:
        raise HTTPException(404, "sesión no encontrada")

    if not force and detail.get("ai_recap"):
        return RecapResult(ok=True, recap=detail["ai_recap"]["recap"], cached=True)

    if not db.session_is_resumable(session_id):
        return RecapResult(ok=False, error=(
            "La sesión fue purgada por Claude Code (sin transcripción .jsonl), "
            "así que no se puede reanudar con `claude --resume`. Sólo está el "
            "recap heurístico a partir de los prompts recuperados."))

    claude_bin = shutil.which("claude")
    if not claude_bin:
        return RecapResult(ok=False, error="No se encontró el binario `claude` en el PATH.")

    cwd = db.get_session_cwd(session_id) or ROOT_PATH
    try:
        proc = subprocess.run(
            [claude_bin, "--resume", session_id, "-p", RECAP_PROMPT],
            cwd=cwd if os.path.isdir(cwd) else None,
            capture_output=True, text=True, timeout=240,
        )
    except subprocess.TimeoutExpired:
        return RecapResult(ok=False, error="El recap tardó demasiado (timeout 240s).")
    except Exception as e:
        return RecapResult(ok=False, error=f"Error ejecutando claude: {e}")

    out = (proc.stdout or "").strip()
    if proc.returncode != 0 or not out:
        return RecapResult(ok=False, error=(
            (proc.stderr or "").strip()[:500] or "El CLI no devolvió contenido."))

    db.save_recap(session_id, out, "cli-default", "ai")
    return RecapResult(ok=True, recap=out)


# --- Frontend estático ----------------------------------------------------- #
@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
