"""Autostart del dashboard como servicio de fondo (launchd / macOS).

Genera un LaunchAgent en ~/Library/LaunchAgents que arranca el dashboard al
iniciar sesión y lo mantiene vivo (KeepAlive). El frontend expone botones para
instalar / desinstalar / ver estado, así que el usuario controla esto
explícitamente desde la UI.

Sólo macOS (launchd). En otros SO se devuelve un error informativo.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import plistlib

import config

LABEL = config.SERVICE_LABEL
HOME = os.path.expanduser("~")
PLIST_PATH = os.path.join(HOME, "Library", "LaunchAgents", f"{LABEL}.plist")
PROJECT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
BACKEND_DIR = os.path.join(PROJECT_DIR, "backend")
LOG_DIR = os.path.join(PROJECT_DIR, "data")


def is_macos() -> bool:
    return sys.platform == "darwin"


def _uv() -> str | None:
    return shutil.which("uv") or ("/opt/homebrew/bin/uv"
                                  if os.path.exists("/opt/homebrew/bin/uv") else None)


def _program_args(port: int) -> list[str]:
    uv = _uv()
    return [
        uv, "run", "--with", "fastapi", "--with", "uvicorn", "--with", "pydantic",
        "uvicorn", "app:app", "--app-dir", BACKEND_DIR,
        "--host", "127.0.0.1", "--port", str(port),
    ]


def _env(port: int, root_path: str) -> dict:
    uv = _uv() or ""
    extra_path = os.pathsep.join(filter(None, [
        os.path.dirname(uv), os.path.join(HOME, ".local", "bin"),
        "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin",
    ]))
    return {
        "PATH": extra_path,
        "CLAUDE_ROOT_PATH": root_path,
        "PORT": str(port),
        "HOME": HOME,
    }


def status(port: int) -> dict:
    installed = os.path.exists(PLIST_PATH)
    loaded = False
    if is_macos() and installed:
        try:
            r = subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=10)
            loaded = LABEL in r.stdout
        except Exception:
            loaded = False
    return {
        "supported": is_macos(),
        "label": LABEL,
        "installed": installed,
        "loaded": loaded,
        "plist_path": PLIST_PATH,
        "log_path": os.path.join(LOG_DIR, "service.log"),
        "uv_found": bool(_uv()),
        "project_dir": PROJECT_DIR,
    }


def install(port: int, root_path: str) -> dict:
    if not is_macos():
        return {"ok": False, "error": "El autostart sólo está soportado en macOS (launchd)."}
    if not _uv():
        return {"ok": False, "error": "No se encontró `uv`. Instálalo o ajusta el PATH."}
    os.makedirs(LOG_DIR, exist_ok=True)
    os.makedirs(os.path.dirname(PLIST_PATH), exist_ok=True)
    plist = {
        "Label": LABEL,
        "ProgramArguments": _program_args(port),
        "WorkingDirectory": PROJECT_DIR,
        "EnvironmentVariables": _env(port, root_path),
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": os.path.join(LOG_DIR, "service.log"),
        "StandardErrorPath": os.path.join(LOG_DIR, "service.log"),
        "ProcessType": "Background",
    }
    with open(PLIST_PATH, "wb") as f:
        plistlib.dump(plist, f)
    # Recargar por si ya existía.
    subprocess.run(["launchctl", "unload", PLIST_PATH], capture_output=True, text=True)
    r = subprocess.run(["launchctl", "load", "-w", PLIST_PATH],
                       capture_output=True, text=True, timeout=20)
    if r.returncode != 0:
        return {"ok": False, "error": (r.stderr or r.stdout or "launchctl load falló").strip(),
                "plist_path": PLIST_PATH}
    return {"ok": True, "plist_path": PLIST_PATH,
            "note": "Instalado. Arrancará al iniciar sesión y se mantiene vivo. "
                    "Puedes cerrar la terminal / ./run.sh: el servicio lo reemplaza."}


def uninstall() -> dict:
    if not is_macos():
        return {"ok": False, "error": "Sólo macOS."}
    if os.path.exists(PLIST_PATH):
        subprocess.run(["launchctl", "unload", "-w", PLIST_PATH], capture_output=True, text=True)
        try:
            os.remove(PLIST_PATH)
        except OSError as e:
            return {"ok": False, "error": str(e)}
    return {"ok": True, "note": "Servicio desinstalado (el dashboard ya no arranca al iniciar sesión)."}
