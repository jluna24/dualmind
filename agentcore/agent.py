"""Agente DualMind en AgentCore Runtime (Fase 3, paso 1).

Hace lo mismo que el worker de la Pi, pero dentro de AgentCore:
lee las últimas horas de DynamoDB, arma el contexto, llama a Bedrock con
`evaluate()` y guarda el campo `llm` en el mismo ítem. Reutiliza tal cual
`context.py`, `llm.py` y `store.py` de `pi/dualmind_pi` (build.py los copia).

Payload de entrada (JSON): {"device_id": "esp32-01", "trigger": "scheduled"|"manual"}
Respuesta: {"ts": <ts del ítem evaluado>, "llm": {...}} o {"error": "..."}
"""

from __future__ import annotations

import logging
import os
import time
from zoneinfo import ZoneInfo

import boto3
from bedrock_agentcore.runtime import BedrockAgentCoreApp

from dualmind_pi.context import DEFAULT_PLACE, build_context
from dualmind_pi.llm import evaluate
from dualmind_pi.store import TelemetryStore

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("dualmind.agent")

TABLE = os.environ.get("DUALMIND_TABLE", "dualmind-telemetry")
DEVICE_ID = os.environ.get("DUALMIND_DEVICE_ID", "esp32-01")
MODEL_ID = os.environ["DUALMIND_MODEL_ID"]
MODEL_NAME = os.environ.get("DUALMIND_MODEL_NAME", "nova-micro")
PLACE = os.environ.get("DUALMIND_PLACE", DEFAULT_PLACE)
TZ = ZoneInfo(os.environ.get("DUALMIND_TZ", "America/Tijuana"))
HISTORY_H = int(os.environ.get("DUALMIND_HISTORY_H", "12"))
STALE_S = 180

ddb = boto3.resource("dynamodb")
bedrock = boto3.client("bedrock-runtime")
app = BedrockAgentCoreApp()


@app.entrypoint
def invoke(payload: dict) -> dict:
    t0 = time.perf_counter()
    device_id = payload.get("device_id", DEVICE_ID)
    trigger = payload.get("trigger", "manual")

    store = TelemetryStore(ddb.Table(TABLE), device_id, keep_hours=HISTORY_H + 1)
    store.refresh()
    latest = store.latest()
    if latest is None:
        return {"error": f"Sin telemetría de {device_id} en las últimas {HISTORY_H} h"}
    age_s = time.time() - latest["ts"] / 1000
    if age_s > STALE_S:
        return {"error": f"Último dato con {age_s / 60:.0f} min de antigüedad (¿ESP32 apagado?)"}

    history = store.since(latest["ts"] - HISTORY_H * 3600 * 1000)
    ctx = build_context(latest, history, PLACE, TZ, HISTORY_H)
    llm = evaluate(ctx, MODEL_ID, MODEL_NAME, client=bedrock)
    llm["trigger"] = trigger
    llm["engine"] = "agentcore"
    llm["agent_ms"] = round((time.perf_counter() - t0) * 1000)
    store.save_llm(int(latest["ts"]), llm)

    log.info("LLM %s [%s] comfort=%s mold=%s action=%s (modelo %d ms, agente %d ms, %d/%d tok)",
             MODEL_NAME, trigger, llm["comfort"], llm["mold_risk"], llm["action"],
             llm["latency_ms"], llm["agent_ms"], llm["tokens_in"], llm["tokens_out"])
    return {"ts": int(latest["ts"]), "llm": llm}


if __name__ == "__main__":
    app.run()
