"""Precios de la API de Claude (USD por 1M de tokens), fuente: skill claude-api.

Verificado contra la referencia oficial (junio 2026). Reglas de cache (prompt-caching):
  - cache write 5m = 1.25× input
  - cache write 1h = 2.00× input
  - cache read     = 0.10× input

⚠️ IMPORTANTE — honestidad sobre el costo:
Estos precios son los de la **API pay-per-token**. Si usas Claude Code con una
**suscripción** (Pro / Max / Team / Enterprise), NO pagas por token: pagas una
cuota fija por asiento y el uso cuenta contra límites de tasa, no contra dólares.
Por eso el "costo" del dashboard es una *estimación equivalente-API* (lo que
costaría si fuera pay-per-token), no tu factura real.
"""

# input, output por millón. Las tarifas de cache se derivan de input.
_BASE = {
    "opus":   {"input": 5.0, "output": 25.0},
    "sonnet": {"input": 3.0, "output": 15.0},
    "haiku":  {"input": 1.0, "output": 5.0},
    "fable":  {"input": 10.0, "output": 50.0},
}


def family_for_model(model: str | None) -> str | None:
    if not model:
        return None
    m = model.lower()
    for fam in ("opus", "sonnet", "haiku", "fable"):
        if fam in m:
            return fam
    return None


def rates_for(model: str | None) -> dict:
    """Devuelve las 5 tarifas (por 1M) para un modelo, o ceros si desconocido."""
    fam = family_for_model(model)
    base = _BASE.get(fam)
    if not base:
        return {"input": 0.0, "output": 0.0, "cache_write_5m": 0.0,
                "cache_write_1h": 0.0, "cache_read": 0.0}
    i = base["input"]
    return {
        "input": i,
        "output": base["output"],
        "cache_write_5m": i * 1.25,
        "cache_write_1h": i * 2.0,
        "cache_read": i * 0.10,
    }


def cost_for(model: str | None, input_t: int, output_t: int,
             cache_write_5m_t: int, cache_write_1h_t: int, cache_read_t: int) -> float:
    r = rates_for(model)
    return (
        input_t / 1e6 * r["input"]
        + output_t / 1e6 * r["output"]
        + cache_write_5m_t / 1e6 * r["cache_write_5m"]
        + cache_write_1h_t / 1e6 * r["cache_write_1h"]
        + cache_read_t / 1e6 * r["cache_read"]
    )


# Tabla para mostrar en la UI.
PRICING_TABLE = [
    {"family": "Opus 4.x", **rates_for("opus")},
    {"family": "Sonnet 4.6", **rates_for("sonnet")},
    {"family": "Haiku 4.5", **rates_for("haiku")},
]
