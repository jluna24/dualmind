# DualMind — Fase 3: de Bedrock directo a AgentCore

> Estado (2026-11-08): **Paso 1 en producción** — la Pi invoca a AgentCore desde las 17:20. Pasos 2–4 en diseño.
> Contexto previo: Fase 2 — reglas en el ESP32 vs. LLM (Nova Micro vía Bedrock Converse) en un worker en la Pi. Ver `fase-2-reglas-vs-llm.md`.

---

## 1. Qué cambia

**Antes (Fase 2):** la Pi hace todo.
```
Pi (cada 10 min): Query DynamoDB → build_context → Converse → UpdateItem "llm"
     └─ access key de dualmind-llm-worker con DynamoDB + Bedrock
```

**Después (Fase 3):** la Pi solo dispara y muestra.
```
Pi (cada 10 min o botón) ──InvokeAgentRuntime──► AgentCore Runtime "dualmind_agent"
                                                   ├─ Query DynamoDB (últimas 12 h)
                                                   ├─ (paso 2) Memory: decisiones y patrones previos
                                                   ├─ Converse → Bedrock
                                                   └─ UpdateItem "llm" (engine: "agentcore")
```

- `context.py`, `llm.py` y `store.py` se reutilizan **sin cambios**: `agentcore/build.py` los copia al zip.
- La Pi sigue siendo el programador y la interfaz web: sin Lambda ni EventBridge por ahora.
- El motor se elige con `DUALMIND_ENGINE=bedrock|agentcore` en `pi/dualmind.env`. Volver atrás = cambiar la variable y `./deploy.sh`.
- Cada decisión guarda `engine`, para separar en el reporte lo evaluado en la Pi y en AgentCore.

---

## 2. Pasos

| Paso | Qué | Estado |
|---|---|---|
| 0 | `prompt_v: 2`: definición de cada valor de `comfort`, `mold_risk` y `action` en el esquema de la herramienta | ✅ desplegado en la Pi |
| 1 | Runtime con la misma lógica (lift & shift) | ✅ aplicado y en uso por la Pi (`DUALMIND_ENGINE=agentcore`) |
| 1b | Observability: *Transaction Search* + instrumentación OTEL (`aws-opentelemetry-distro`) | ⬜ |
| 2 | Memory: decisiones como eventos + estrategia de resumen de largo plazo → `prompt_v: 3` | ⬜ |
| 3 | Gateway + target OpenAPI de clima exterior (p. ej. Open-Meteo, sin llave) + Policy | ⬜ opcional |
| 4 | Evaluations en línea (LLM como juez) | ⬜ opcional, cuesta tokens |

---

## 3. Paso 1 — Runtime

### Empaquetado: despliegue directo de código (sin contenedor)

- `agentcore/agent.py`: `BedrockAgentCoreApp` + `@app.entrypoint`. Recibe `{"device_id", "trigger"}`. Regresa `{"ts", "llm"}` o `{"error"}`.
- `agentcore/build.py`: baja wheels **Linux ARM64 / Python 3.13** aunque corra en Windows. Copia el agente y los 3 módulos compartidos. Genera `agentcore/dist/agent.zip` (~21 MB).
- El zip es **determinista** (fechas fijas, orden alfabético): si el código no cambia, no se crea una versión nueva del Runtime.

### Infra (`infra/agentcore.tf`)

| Recurso | Detalle |
|---|---|
| `aws_s3_bucket.agent_code` | `dualmind-agentcore-code-<cuenta>`, privado, versionado; guarda 3 versiones anteriores |
| `aws_s3_object.agent_zip` | `dualmind_agent/agent.zip`; el Runtime apunta a su `version_id` |
| `aws_iam_role.agent_runtime` | `dualmind-agentcore-runtime`, confía en `bedrock-agentcore.amazonaws.com` (limitado a la cuenta) |
| `aws_iam_role_policy.agent_runtime` | DynamoDB (Query/GetItem/UpdateItem), `bedrock:InvokeModel` (mismos ARN que el worker), lectura del zip, logs, X-Ray, métricas, token de workload identity |
| `aws_bedrockagentcore_agent_runtime.dualmind` | `dualmind_agent`, `PYTHON_3_13`, red pública, HTTP, sesión inactiva 60 s / vida máx. 300 s, modelo por `var.agent_model` |
| `aws_iam_user_policy.llm_worker` (cambio) | La Pi gana `bedrock-agentcore:InvokeAgentRuntime` sobre el Runtime |

El endpoint `DEFAULT` lo crea AgentCore solo y apunta a la última versión.

### Cómo desplegar

```bash
python agentcore/build.py            # desde DualMind/
cd infra && tofu plan -out=plan.tfplan && tofu apply plan.tfplan
tofu output agent_runtime_arn
```

### Probar antes de cambiar la Pi

```bash
aws bedrock-agentcore invoke-agent-runtime \
  --agent-runtime-arn <ARN> \
  --runtime-session-id dualmind-esp32-01-prueba-manual-000000001 \
  --payload '{"device_id":"esp32-01","trigger":"manual"}' \
  --cli-binary-format raw-in-base64-out out.json && cat out.json
```

Logs: `/aws/bedrock-agentcore/runtimes/<runtime-id>-DEFAULT`.

### Cambiar la Pi a AgentCore

En `pi/dualmind.env`: `DUALMIND_ENGINE=agentcore` y `DUALMIND_AGENT_RUNTIME_ARN=<ARN>`. Después, `./deploy.sh`.

### Criterio de éxito

- Con el mismo modelo y el mismo prompt (`prompt_v: 2`), las decisiones de `engine: agentcore` deben parecerse a las de `engine: bedrock`. La temperatura es 0.3, así que puede haber variación.
- Latencia total razonable. El primer arranque (*cold start*) con una sesión nueva por invocación es el costo esperado.

---

### Resultado (2026-11-08)

- `tofu apply`: 8 recursos nuevos, 1 cambio, sin errores. El Runtime quedó `READY` en 21 s, versión 1, sin el problema de propagación de IAM.
- Primera invocación desde la CLI: **4.2 s en total**, con cold start de la microVM. Dentro del agente: 682 ms, de los cuales 503 ms son de Bedrock. Lo demás es arranque y la consulta a DynamoDB.
- Desde la Pi: 3.5 s en total, 695 ms dentro del agente. El cold start se paga en cada invocación porque cada una abre una sesión nueva.
- Logs en `/aws/bedrock-agentcore/runtimes/<runtime-id>-DEFAULT`. El agente usa el rol de ejecución (`Found credentials from IAM Role: execution_role`): no hay llaves.
- Las decisiones quedan en DynamoDB con `engine: agentcore`.

## 4. Paso 2 — Memory (diseño)

- La **telemetría cruda se queda en DynamoDB**: es dato determinista, no "memoria" del agente.
- **Memory** guarda lo que el agente concluye:
  - Eventos de corto plazo: cada decisión (`actor_id = esp32-01`, `session_id` por día).
  - Estrategia de largo plazo tipo *summary*: patrones como "la HR sube a ~70 % entre 3 y 6 am".
- El prompt agrega una sección "lo que recuerdas" → `prompt_v: 3`.
- **Ojo con la comparación:** el LLM tendría más contexto que las reglas. Es lo que se quiere medir, pero hay que tener antes 1–2 días de `prompt_v: 2` como base.

## 5. Paso 3 — Gateway (diseño)

- Un `aws_bedrockagentcore_gateway` con un `gateway_target` OpenAPI hacia una API de clima. El agente gana la herramienta "condiciones exteriores", y así "ventilar" deja de ser una adivinanza.
- Con eso, el agente pasa de una sola llamada con `toolChoice` forzado a un bucle de herramientas. Es otra "mente": guardarla con `prompt_v` o `agent_v` distinto.
- Policy (`aws_bedrockagentcore_policy_engine`) limita qué herramientas y parámetros puede usar.

---

## 6. Costo esperado

| Pieza | Uso | USD/semana |
|---|---|---|
| Runtime | 144 invocaciones/día × ~2 s; el CPU no se cobra mientras espera a Bedrock | < 0.10 |
| S3 | ~100 MB con versiones | < 0.01 |
| Bedrock (Nova Micro) | igual que en la Fase 2 | < 0.05 |
| Memory / Observability (pasos siguientes) | cientos de eventos / trazas | < 0.50 |

## 7. Pendientes por confirmar

- Si EventBridge Scheduler puede invocar el Runtime directamente: la Pi dejaría de ser el programador.
- Si en despliegue directo de código la instrumentación OTEL es automática o hay que agregar `aws-opentelemetry-distro`.
