"""DualMind en la Raspberry Pi: interfaz web + worker del LLM en un solo servicio.

- Refresca la telemetría de DynamoDB cada REFRESH_S segundos (caché en memoria).
- Evalúa con el LLM cada INTERVAL_MIN minutos (:00, :10, ...) y con el botón.
- Sirve la página en / y la API en /api/*.
- Transmite la cámara USB (MJPEG) en /api/camera.mjpg, encendida solo mientras alguien la ve.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import boto3
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response, StreamingResponse

from .camera import Camera
from .context import DEFAULT_PLACE, build_context
from .llm import evaluate
from .store import TelemetryStore

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("dualmind")

# --- Configuración (variables de entorno, ver dualmind.env) -----------------
TABLE = os.environ.get("DUALMIND_TABLE", "dualmind-telemetry")
DEVICE_ID = os.environ.get("DUALMIND_DEVICE_ID", "esp32-01")
MODEL_ID = os.environ["DUALMIND_MODEL_ID"]  # ARN del perfil de inferencia
MODEL_NAME = os.environ.get("DUALMIND_MODEL_NAME", "nova-micro")
PLACE = os.environ.get("DUALMIND_PLACE", DEFAULT_PLACE)
TZ = ZoneInfo(os.environ.get("DUALMIND_TZ", "America/Tijuana"))
INTERVAL_MIN = int(os.environ.get("DUALMIND_INTERVAL_MIN", "10"))
HISTORY_H = int(os.environ.get("DUALMIND_HISTORY_H", "12"))
REFRESH_S = int(os.environ.get("DUALMIND_REFRESH_S", "20"))
STALE_S = 180          # no evaluar si el último dato tiene más de 3 min
MANUAL_COOLDOWN_S = 30  # límite del botón
# Quién ejecuta la mente LLM: "bedrock" (aquí, en la Pi) o "agentcore" (Fase 3)
ENGINE = os.environ.get("DUALMIND_ENGINE", "bedrock")
AGENT_RUNTIME_ARN = os.environ.get("DUALMIND_AGENT_RUNTIME_ARN", "")
if ENGINE not in ("bedrock", "agentcore"):
    raise ValueError(f"DUALMIND_ENGINE inválido: {ENGINE!r}")
if ENGINE == "agentcore" and not AGENT_RUNTIME_ARN:
    raise ValueError("DUALMIND_ENGINE=agentcore requiere DUALMIND_AGENT_RUNTIME_ARN")

session = boto3.Session()  # AWS_PROFILE / región desde el entorno
store = TelemetryStore(session.resource("dynamodb").Table(TABLE), DEVICE_ID, keep_hours=HISTORY_H + 1)
bedrock = session.client("bedrock-runtime")
agentcore = session.client("bedrock-agentcore") if ENGINE == "agentcore" else None
camera = Camera(
    os.environ.get("DUALMIND_CAMERA_DEVICE", "/dev/video0"),
    size=os.environ.get("DUALMIND_CAMERA_SIZE", "1280x720"),
    fps=int(os.environ.get("DUALMIND_CAMERA_FPS", "30")),
)

state = {"last_manual": 0.0, "next_scheduled": None, "last_error": None, "last_skip": None}
eval_lock = asyncio.Lock()


def _run_evaluation(trigger: str) -> dict:
    latest = store.latest()
    if latest is None:
        raise RuntimeError("Sin telemetría en caché")
    age_s = time.time() - latest["ts"] / 1000
    if age_s > STALE_S:
        raise RuntimeError(f"Último dato con {age_s / 60:.0f} min de antigüedad (¿ESP32 apagado?)")
    if ENGINE == "agentcore":
        latest, llm = _invoke_agent(trigger)
    else:
        history = store.since(latest["ts"] - HISTORY_H * 3600 * 1000)
        ctx = build_context(latest, history, PLACE, TZ, HISTORY_H)
        llm = evaluate(ctx, MODEL_ID, MODEL_NAME, client=bedrock)
        llm["trigger"] = trigger
        llm["engine"] = "bedrock"
        store.save_llm(int(latest["ts"]), llm)
    rules = latest.get("rules", {})
    log.info("LLM %s/%s [%s] comfort=%s mold=%s action=%s (%d ms, %d/%d tok) | reglas %s/%s/%s",
             ENGINE, llm["model"], trigger, llm["comfort"], llm["mold_risk"], llm["action"],
             llm["latency_ms"], llm["tokens_in"], llm["tokens_out"],
             rules.get("comfort"), rules.get("mold_risk"), rules.get("action"))
    return llm


def _invoke_agent(trigger: str) -> tuple[dict, dict]:
    """El agente de AgentCore lee DynamoDB, llama al LLM y guarda `llm`; aquí solo se dispara."""
    t0 = time.perf_counter()
    resp = agentcore.invoke_agent_runtime(
        agentRuntimeArn=AGENT_RUNTIME_ARN,
        runtimeSessionId=f"dualmind-{DEVICE_ID}-{uuid.uuid4().hex}",  # mínimo 33 caracteres
        payload=json.dumps({"device_id": DEVICE_ID, "trigger": trigger}).encode(),
        contentType="application/json",
        accept="application/json",
    )
    body = json.loads(resp["response"].read())
    if "error" in body:
        raise RuntimeError(f"AgentCore: {body['error']}")
    ts, llm = int(body["ts"]), body["llm"]
    store.cache_llm(ts, llm)
    log.info("AgentCore respondió en %d ms (agente %s ms)",
             round((time.perf_counter() - t0) * 1000), llm.get("agent_ms"))
    latest = next((it for it in reversed(store.since(ts)) if int(it["ts"]) == ts), store.latest())
    return latest, llm


async def evaluate_now(trigger: str) -> dict:
    async with eval_lock:
        await asyncio.to_thread(store.refresh)
        try:
            llm = await asyncio.to_thread(_run_evaluation, trigger)
            state["last_error"] = None
            return llm
        except Exception as e:  # se muestra en la interfaz
            state["last_error"] = f"{datetime.now(TZ):%H:%M} {trigger}: {e}"
            log.warning("Evaluación %s fallida: %s", trigger, e)
            raise


def _next_slot(now: datetime) -> datetime:
    """Siguiente :00/:10/:20... + 20 s (para que ya haya llegado el minuto)."""
    slot = now.replace(minute=now.minute // INTERVAL_MIN * INTERVAL_MIN, second=20, microsecond=0)
    while slot <= now:
        slot += timedelta(minutes=INTERVAL_MIN)
    return slot


async def refresher():
    while True:
        try:
            await asyncio.to_thread(store.refresh)
        except Exception as e:
            log.warning("Refresh fallido: %s", e)
        await asyncio.sleep(REFRESH_S)


async def scheduler():
    while True:
        nxt = _next_slot(datetime.now(TZ))
        state["next_scheduled"] = int(nxt.timestamp() * 1000)
        await asyncio.sleep(max(1, (nxt - datetime.now(TZ)).total_seconds()))
        try:
            await evaluate_now("scheduled")
        except Exception:
            state["last_skip"] = int(time.time() * 1000)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    n = await asyncio.to_thread(store.refresh)
    log.info("Caché inicial: %d ítems (%d h)", n, HISTORY_H)
    tasks = [asyncio.create_task(refresher()), asyncio.create_task(scheduler())]
    yield
    for t in tasks:
        t.cancel()
    camera.stop()


app = FastAPI(title="DualMind", lifespan=lifespan)
STATIC = Path(__file__).parent / "static"


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


def _slim(it: dict) -> dict:
    return {k: it.get(k) for k in ("ts", "msg_id", "readings", "samples", "rules", "llm")}


@app.get("/api/state")
def api_state():
    latest = store.latest()
    last_llm = next((it for it in reversed(store.since(0)) if "llm" in it), None)
    return {
        "now": int(time.time() * 1000),
        "latest": _slim(latest) if latest else None,
        "last_llm": _slim(last_llm) if last_llm else None,
        "model": MODEL_NAME,
        "interval_min": INTERVAL_MIN,
        "next_scheduled": state["next_scheduled"],
        "last_error": state["last_error"],
        "cooldown_s": max(0, round(MANUAL_COOLDOWN_S - (time.time() - state["last_manual"]))),
    }


@app.get("/api/history")
def api_history(hours: int = 12):
    since = int(time.time() * 1000) - min(hours, HISTORY_H) * 3600 * 1000
    return [
        {"ts": it["ts"], "t": it["readings"]["temp_c"]["avg"], "h": it["readings"]["hum_pct"]["avg"]}
        for it in store.since(since)
        if "readings" in it
    ]


@app.get("/api/evaluations")
def api_evaluations(limit: int = 20):
    items = [_slim(it) for it in store.since(0) if "llm" in it]
    return list(reversed(items))[:limit]


@app.post("/api/analyze")
async def api_analyze():
    wait = MANUAL_COOLDOWN_S - (time.time() - state["last_manual"])
    if wait > 0:
        raise HTTPException(429, f"Espera {wait:.0f} s antes de otro análisis")
    state["last_manual"] = time.time()
    try:
        llm = await evaluate_now("manual")
    except Exception as e:
        raise HTTPException(502, str(e))
    latest = store.latest()
    return {"llm": llm, "rules": (latest or {}).get("rules"), "ts": (latest or {}).get("ts")}


# --- Cámara -----------------------------------------------------------------
BOUNDARY = "dualmindframe"
NO_CACHE = {"Cache-Control": "no-store", "X-Accel-Buffering": "no"}


@app.get("/api/camera.mjpg")
async def camera_stream():
    async def frames():
        camera.acquire()
        try:
            last, waited = 0, 0.0
            while True:
                frame, seq = camera.frame()
                if frame is not None and seq != last:
                    last, waited = seq, 0.0
                    yield (f"--{BOUNDARY}\r\nContent-Type: image/jpeg\r\n"
                           f"Content-Length: {len(frame)}\r\n\r\n").encode() + frame + b"\r\n"
                else:
                    await asyncio.sleep(0.01)
                    waited += 0.01
                    if waited > 20:  # cámara sin cuadros: cerrar y que el navegador reintente
                        return
        finally:
            camera.release()

    return StreamingResponse(frames(), media_type=f"multipart/x-mixed-replace; boundary={BOUNDARY}",
                             headers=NO_CACHE)


@app.get("/api/camera.jpg")
async def camera_snapshot():
    camera.acquire()
    try:
        for _ in range(500):  # hasta 5 s para el primer cuadro
            frame, _seq = camera.frame()
            if frame is not None:
                return Response(frame, media_type="image/jpeg", headers=NO_CACHE)
            await asyncio.sleep(0.01)
    finally:
        camera.release()
    raise HTTPException(503, camera.error or "La cámara no respondió")


@app.get("/api/camera/status")
def camera_status():
    return camera.status()
