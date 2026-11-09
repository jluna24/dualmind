"""Arma el contexto del LLM a partir de los ítems de telemetría (funciones puras)."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

# Mismo texto para la Pi y el agente de AgentCore (afecta al prompt).
DEFAULT_PLACE = (
    "recámara en Ensenada, Baja California, México (clima costero con influencia "
    "marina); se duerme ahí por la noche"
)

WEEKDAYS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]


def _r(v: float | None, nd: int = 2) -> float | None:
    return None if v is None else round(v, nd)


def hourly_summary(items: list[dict[str, Any]], tz: ZoneInfo) -> list[dict[str, Any]]:
    """Agrupa los resúmenes por minuto en filas por hora local (más antigua primero)."""
    buckets: dict[datetime, dict[str, list[float]]] = {}
    for it in items:
        rd = it.get("readings", {})
        t = rd.get("temp_c", {})
        h = rd.get("hum_pct", {})
        if t.get("avg") is None or h.get("avg") is None:
            continue
        local = datetime.fromtimestamp(it["ts"] / 1000, tz)
        key = local.replace(minute=0, second=0, microsecond=0)
        b = buckets.setdefault(key, {"t": [], "tmin": [], "tmax": [], "h": [], "hmin": [], "hmax": []})
        b["t"].append(t["avg"])
        b["tmin"].append(t.get("min", t["avg"]))
        b["tmax"].append(t.get("max", t["avg"]))
        b["h"].append(h["avg"])
        b["hmin"].append(h.get("min", h["avg"]))
        b["hmax"].append(h.get("max", h["avg"]))

    rows = []
    for key in sorted(buckets):
        b = buckets[key]
        n = len(b["t"])
        rows.append({
            "hour": key.strftime("%d %H:00"),
            "t_avg": _r(sum(b["t"]) / n), "t_min": _r(min(b["tmin"])), "t_max": _r(max(b["tmax"])),
            "h_avg": _r(sum(b["h"]) / n), "h_min": _r(min(b["hmin"])), "h_max": _r(max(b["hmax"])),
            "n": n,
        })
    return rows


def build_context(
    latest: dict[str, Any],
    history: list[dict[str, Any]],
    place: str,
    tz: ZoneInfo,
    history_hours: int = 12,
) -> dict[str, Any]:
    local = datetime.fromtimestamp(latest["ts"] / 1000, tz)
    return {
        "place": place,
        "local_time": local.strftime("%Y-%m-%d %H:%M"),
        "weekday": WEEKDAYS[local.weekday()],
        "current": latest["readings"],
        "history_hours": history_hours,
        "hourly": hourly_summary(history, tz),
    }
