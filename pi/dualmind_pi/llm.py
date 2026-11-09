"""Mente probabilística: evaluate(context) -> decision.

No depende de la Raspberry Pi ni de DynamoDB: recibe el contexto ya armado
(ver context.py) y llama a Bedrock Converse. Así se puede mover tal cual a
AgentCore Runtime en la Fase 3.
"""

from __future__ import annotations

import time
from typing import Any

import boto3

PROMPT_V = 2

# prompt_v 2: cada valor lleva su definición, para que el LLM no confunda
# "ventilar" (renovar aire con el exterior) con "ventilador" (mover aire interior).
COMFORT_DESC = {
    "ok": "temperatura y humedad dentro de lo cómodo para una recámara",
    "frio": "temperatura por debajo de lo cómodo (prioritario sobre la humedad)",
    "caluroso": "temperatura por encima de lo cómodo (prioritario sobre la humedad)",
    "seco": "temperatura cómoda pero humedad relativa demasiado baja",
    "humedo": "temperatura cómoda pero humedad relativa demasiado alta",
}
MOLD_RISK_DESC = {
    "bajo": "la humedad no se ha mantenido alta el tiempo suficiente para favorecer moho",
    "medio": "humedad alta sostenida durante horas; vigilar",
    "alto": "humedad muy alta sostenida durante muchas horas o condensación probable",
}
ACTIONS_DESC = {
    "ninguna": "no hace falta hacer nada",
    "ventilar": "abrir ventanas o puertas para renovar el aire con el exterior; solo baja "
                "la humedad si el aire de afuera es más seco (no se conocen las condiciones exteriores)",
    "ventilador": "encender un ventilador que mueve el aire interior; mejora la sensación de calor "
                  "pero no cambia la temperatura ni la humedad",
    "deshumidificar": "usar un deshumidificador para bajar la humedad relativa",
    "humidificar": "usar un humidificador para subir la humedad relativa",
    "calentar": "usar calefacción para subir la temperatura",
}
COMFORT = list(COMFORT_DESC)
MOLD_RISK = list(MOLD_RISK_DESC)
ACTIONS = list(ACTIONS_DESC)


def _enum_desc(title: str, desc: dict[str, str]) -> str:
    return title + ". Valores: " + "; ".join(f"{k} = {v}" for k, v in desc.items())

SYSTEM_PROMPT = (
    "Eres un asistente de monitoreo ambiental de interiores. Con las lecturas "
    "actuales y el historial por hora de un sensor de temperatura y humedad, "
    "evalúa (1) el confort actual de las personas en ese lugar y (2) el riesgo "
    "de moho o condensación según la persistencia de la humedad. Elige una sola "
    "acción recomendada. Responde únicamente usando la herramienta "
    "report_decision con los valores permitidos. La razón debe ser breve, "
    "concreta y en español (máximo 120 caracteres)."
)

TOOL = {
    "toolSpec": {
        "name": "report_decision",
        "description": "Reporta la evaluación del ambiente con valores cerrados.",
        "inputSchema": {
            "json": {
                "type": "object",
                "properties": {
                    "comfort": {"type": "string", "enum": COMFORT,
                                "description": _enum_desc("Confort actual", COMFORT_DESC)},
                    "mold_risk": {"type": "string", "enum": MOLD_RISK,
                                  "description": _enum_desc("Riesgo de moho/condensación", MOLD_RISK_DESC)},
                    "action": {"type": "string", "enum": ACTIONS,
                               "description": _enum_desc("Acción recomendada", ACTIONS_DESC)},
                    "reason": {"type": "string", "maxLength": 120,
                               "description": "Razón breve"},
                },
                "required": ["comfort", "mold_risk", "action", "reason"],
            }
        },
    }
}


def _fmt(v: float | None, nd: int = 1) -> str:
    return "—" if v is None else f"{v:.{nd}f}"


def build_prompt(ctx: dict[str, Any]) -> str:
    cur = ctx["current"]
    t, h = cur["temp_c"], cur["hum_pct"]
    lines = [
        f"Lugar: {ctx['place']}",
        f"Hora local: {ctx['local_time']} ({ctx['weekday']})",
        "",
        "Lectura actual (último minuto):",
        f"- Temperatura: {_fmt(t['avg'])} °C (min {_fmt(t['min'])}, max {_fmt(t['max'])})",
        f"- Humedad relativa: {_fmt(h['avg'])} % (min {_fmt(h['min'])}, max {_fmt(h['max'])})",
        "",
        f"Historial por hora, últimas {ctx['history_hours']} h (hora local):",
        "hora | T avg/min/max °C | HR avg/min/max % | minutos con datos",
    ]
    for r in ctx["hourly"]:
        lines.append(
            f"{r['hour']} | {_fmt(r['t_avg'])}/{_fmt(r['t_min'])}/{_fmt(r['t_max'])}"
            f" | {_fmt(r['h_avg'], 0)}/{_fmt(r['h_min'], 0)}/{_fmt(r['h_max'], 0)} | {r['n']}"
        )
    if not ctx["hourly"]:
        lines.append("(sin historial)")
    return "\n".join(lines)


def _validate(raw: dict[str, Any]) -> dict[str, str]:
    out = {}
    for field, allowed in (("comfort", COMFORT), ("mold_risk", MOLD_RISK), ("action", ACTIONS)):
        val = str(raw.get(field, "")).strip().lower()
        if val not in allowed:
            raise ValueError(f"{field} fuera del contrato: {val!r}")
        out[field] = val
    out["reason"] = str(raw.get("reason", "")).strip()[:120]
    return out


def evaluate(
    ctx: dict[str, Any],
    model_id: str,
    model_name: str,
    client=None,
    temperature: float = 0.3,
) -> dict[str, Any]:
    """Llama al LLM y regresa el campo `llm` listo para guardar."""
    client = client or boto3.client("bedrock-runtime")
    t0 = time.perf_counter()
    resp = client.converse(
        modelId=model_id,
        system=[{"text": SYSTEM_PROMPT}],
        messages=[{"role": "user", "content": [{"text": build_prompt(ctx)}]}],
        toolConfig={"tools": [TOOL], "toolChoice": {"tool": {"name": "report_decision"}}},
        inferenceConfig={"maxTokens": 300, "temperature": temperature},
    )
    latency_ms = round((time.perf_counter() - t0) * 1000)

    content = resp["output"]["message"]["content"]
    tool_use = next((c["toolUse"] for c in content if "toolUse" in c), None)
    if tool_use is None:
        raise ValueError(f"El modelo no usó la herramienta: {content!r}")
    decision = _validate(tool_use["input"])

    usage = resp.get("usage", {})
    return {
        "model": model_name,
        "model_id": model_id,
        "prompt_v": PROMPT_V,
        "temperature": temperature,
        **decision,
        "latency_ms": latency_ms,
        "tokens_in": usage.get("inputTokens", 0),
        "tokens_out": usage.get("outputTokens", 0),
        "evaluated_at": int(time.time() * 1000),
    }
