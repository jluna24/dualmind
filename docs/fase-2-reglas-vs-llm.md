# DualMind — Fase 2: reglas (ESP32) vs. LLM (Bedrock)

> Documento de diseño de la Fase 2. Estado: **implementado** reglas + worker + interfaz (2026-11-08); pendiente reporte de comparación y segundo modelo.
> Contexto previo: Fase 1 terminada — ESP32 + DHT22 → IoT Core → DynamoDB `dualmind-telemetry` + CloudWatch, todo con OpenTofu (`infra/`).

---

## 1. Objetivo

Con la **misma entrada** (temperatura y humedad del DHT22), dos "mentes" toman la misma decisión y se comparan:

| Mente | Dónde vive | Naturaleza |
|---|---|---|
| **Reglas** | En el firmware del ESP32 (edge) | Determinista, umbrales fijos, costo 0 |
| **LLM** | Un *worker* fuera del micro que llama a Bedrock | Probabilística, razona con contexto |

La decisión combina dos preguntas:

1. **Confort** — ¿cómo está el cuarto *ahora*? (solo necesita el minuto actual)
2. **Riesgo de moho / condensación** — ¿qué pasó en las *últimas horas*? (necesita historial)

La primera es el caso "fácil" (umbrales claros); la segunda es el "difícil" (patrones en el tiempo), donde se espera ver más diferencia entre las dos mentes.

---

## 2. Arquitectura

```
┌──────────────────────── ESP32 (edge) ───────────────────────┐
│ DHT22 → resumen por minuto → REGLAS → mensaje con "rules"   │
└──────────────────────────────┬──────────────────────────────┘
                               │ MQTT/TLS (sin cambios)
                               ▼
                IoT Core ── regla SELECT * ──► DynamoDB dualmind-telemetry
                               │                    ▲            │
                               └─► CloudWatch       │ UpdateItem │ Query (últimas 12 h)
                                                    │  "llm"     ▼
                                       ┌──────── LLM worker (cada 10 min) ───────┐
                                       │ arma contexto → Bedrock Converse →      │
                                       │ respuesta estructurada → guarda "llm"   │
                                       └─────────────────────────────────────────┘
```

Puntos clave:

- **No hay Lambda.** Las reglas corren en el ESP32 y el LLM en un worker programado.
- **La infraestructura de la Fase 1 casi no cambia.** La regla IoT ya hace `SELECT *`, así que el campo `rules` del mensaje se guarda solo en DynamoDB.
- **Un ítem por minuto lo tiene todo:** lecturas + decisión de las reglas + (cada 10 min) decisión del LLM. La comparación se hace sobre el mismo ítem.

---

## 3. Reglas en el ESP32: análisis

### Ventajas
- **Sin Lambda ni costo:** cero infraestructura nueva para las reglas.
- **Funciona sin internet:** la decisión se toma aunque se caiga la red (útil si después el ESP32 actúa sobre algo, p. ej. un relevador).
- **Latencia ~0:** la decisión sale en el mismo mensaje que la lectura.
- **Narrativa clara para el proyecto:** *edge determinista vs. nube probabilística*.

### Desventajas y cómo mitigarlas

| Riesgo | Mitigación |
|---|---|
| El riesgo de moho necesita **horas de historial**, y el ESP32 lo pierde al reiniciarse | Contador de minutos húmedos en RAM, guardado en **NVS cada 10 min** junto con su hora (epoch). Al arrancar se restaura si el último guardado tiene < 15 min; si no, empieza en 0 y se marca `history_reset: true` |
| Cambiar umbrales obliga a **reflashear** | Aceptable en un proyecto de pocos días. A futuro: umbrales por MQTT (tópico de configuración) u OTA |
| Las reglas ven lecturas cada 2.5 s; el LLM solo resúmenes | Las reglas **solo usan el resumen por minuto** (avg) y su contador, igual que lo que recibe el LLM |
| El mensaje crece | +~150 bytes; sigue muy por debajo de 5 KB (1 mensaje facturable) |

---

## 4. Dónde vive el LLM: opciones

| Opción | Pros | Contras | Veredicto |
|---|---|---|---|
| **Raspberry Pi 5** (script Python + timer de systemd) | Ya existe, siempre encendida, gratis, fácil de depurar por SSH | Necesita credenciales AWS en la Pi; si la Pi se apaga no hay evaluaciones | ✅ **Recomendada para empezar** |
| ESP32 llama directo a Bedrock | Todo en un lugar | Firma SigV4, credenciales AWS en el micro, respuestas grandes, poca memoria | ❌ No |
| PC con Windows | Cero setup | No está siempre encendida | ❌ Solo para pruebas manuales |
| **AgentCore Runtime** (Fase 3) | Nativo de AWS, observabilidad, Memory | Más piezas; es justo lo que toca en la Fase 3 | ⏭️ **Destino final** |

**Recomendación:** empezar con el worker en la **Pi** y, en la Fase 3, mover la misma lógica a **AgentCore Runtime** (con Memory para el contexto). El worker se escribe pensando en esa migración: una función `evaluate(context) -> decision` sin dependencias de la Pi.

### Credenciales del worker en la Pi

| Opción | Detalle |
|---|---|
| **A. Usuario IAM dedicado** `dualmind-llm-worker` (recomendada por simplicidad) | Política mínima: `dynamodb:Query` + `dynamodb:UpdateItem` sobre la tabla, `bedrock:InvokeModel` sobre el/los perfiles de inferencia elegidos. El usuario y su política en OpenTofu; **la access key se crea aparte por CLI** para que el secreto no quede en el estado. Borrarla en la Fase 5 |
| B. IoT credentials provider | La Pi usa su propio certificado X.509 para obtener credenciales temporales vía *role alias*. Sin llaves de larga duración; más configuración |

---

## 5. Contrato de decisión (común a las dos mentes)

Mismo formato y mismos valores posibles, para poder medir coincidencias:

| Campo | Valores |
|---|---|
| `comfort` | `ok`, `frio`, `caluroso`, `seco`, `humedo` |
| `mold_risk` | `bajo`, `medio`, `alto` |
| `action` | `ninguna`, `ventilar`, `ventilador`, `deshumidificar`, `humidificar`, `calentar` |
| `reason` | Texto corto (≤ 120 caracteres) |

### En el mensaje del ESP32 (campo opcional nuevo, contrato sigue en `v: 1`)

```json
{
  "v": 1, "device_id": "esp32-01", "msg_id": "esp32-01-9-000123", "ts": 1794123491649,
  "type": "telemetry", "window_s": 60,
  "readings": { "temp_c": {"avg": 26.82, "min": 26.6, "max": 27.1},
                "hum_pct": {"avg": 63.47, "min": 63, "max": 63.6} },
  "samples": { "ok": 24, "err": 0 },
  "rules": {
    "rules_v": 1,
    "comfort": "caluroso", "mold_risk": "bajo", "action": "ventilador",
    "reason": "T 26.8 > 26",
    "dew_point_c": 19.3, "humid_min": 0, "history_reset": false
  }
}
```

### Lo que agrega el worker al mismo ítem (UpdateItem, solo cada 10 min)

```json
"llm": {
  "model": "us.amazon.nova-micro-v1:0", "prompt_v": 1,
  "comfort": "caluroso", "mold_risk": "bajo", "action": "ventilar",
  "reason": "...",
  "latency_ms": 820, "tokens_in": 1480, "tokens_out": 95,
  "evaluated_at": 1794123500000
}
```

---

## 6. Reglas (v1, umbrales tentativos)

> Umbrales definidos para una recámara (decisiones 1, 2, 4 y 5 de la sección 11 resueltas el 2026-11-07).

**Entradas:** `T` = `temp_c.avg`, `HR` = `hum_pct.avg` del minuto; `humid_min` = minutos consecutivos con HR ≥ 70 %.

> **Decidido (2026-11-07):** recámara en Ensenada, B.C. Hora local con TZ `PST8PDT,M3.2.0,M11.1.0`. Noche = 23:00–07:00.

### Confort (primera que aplique)
| Estado | Día (07–23) | Noche (23–07) |
|---|---|---|
| `caluroso` | `T > 26` | `T > 24` |
| `frio` | `T < 20` | `T < 18` |
| `humedo` | `HR > 60` | `HR > 60` |
| `seco` | `HR < 30` | `HR < 30` |
| `ok` | si no | si no |

### Riesgo de moho
- `humid_min` se incrementa si `HR ≥ 70`, se reinicia a 0 si `HR < 65` (histéresis para no oscilar).
- `< 120 min` → `bajo` · `120–359 min` → `medio` · `≥ 360 min` → `alto`

### Punto de rocío (informativo, fórmula de Magnus)
```
γ = ln(HR/100) + (17.62 · T) / (243.12 + T)
Td = 243.12 · γ / (17.62 − γ)
```

### Acción (primera que aplique)
1. `mold_risk = alto` → `deshumidificar`
2. `mold_risk = medio` o `comfort = humedo` → `ventilar`
3. `comfort = caluroso` → `ventilador`
4. `comfort = frio` → `calentar`
5. `comfort = seco` → `humidificar`
6. si no → `ninguna`

---

## 7. LLM worker

### Ciclo (cada 10 min, minutos fijos: :00, :10, :20…)
1. Leer el ítem más reciente de `dualmind-telemetry`. **Si tiene más de 3 min de antigüedad, no evaluar** (ESP32 apagado) y registrar el salto.
2. Consultar las **últimas 12 h** y resumirlas por hora: avg/min/max de T y HR por hora (~12 filas). Esto mantiene el prompt chico (~1,500 tokens).
3. Llamar a Bedrock **Converse** con salida estructurada (una *tool* con JSON Schema y los enums de la sección 5) para que la respuesta siempre sea válida.
4. Guardar `llm` en el **mismo ítem** (`device_id` + `ts`) con modelo, versión de prompt, latencia y tokens.

### Qué recibe el LLM (y qué no)
- **Sí:** lecturas del minuto actual, serie por hora de las últimas 12 h, contexto del lugar (p. ej. "recámara", "oficina"; ver decisión abierta 1), objetivo (confort + moho) y valores permitidos.
- **No:** los umbrales de las reglas ni la decisión de las reglas. La idea es ver si llega a conclusiones parecidas por su cuenta.

### Prompt (borrador, `prompt_v: 1`)
> Eres un asistente de monitoreo ambiental de interiores. Con las lecturas actuales y el historial por hora de un sensor de temperatura y humedad ubicado en {lugar}, evalúa (1) el confort actual y (2) el riesgo de moho o condensación según la persistencia de humedad. Responde usando únicamente la herramienta `report_decision` con los valores permitidos. La razón debe ser breve y concreta.

### Modelos
- Empezar con **Nova Micro** (`us.amazon.nova-micro-v1:0`).
- Después, con los mismos datos, **Claude Haiku 4.5** (`us.anthropic.claude-haiku-4-5-20251001-v1:0`). Comparar dos LLMs entre sí también aporta al proyecto.

---

## 8. Comparación (qué se mide)

Sobre los ítems que tienen `rules` **y** `llm`:

| Métrica | Cómo |
|---|---|
| Coincidencia por campo | % de ítems donde `comfort`, `mold_risk` y `action` coinciden |
| Matriz de confusión | Reglas (filas) vs. LLM (columnas) por campo |
| Casos interesantes | Lista de desacuerdos con lecturas y ambas razones |
| Estabilidad del LLM | ¿Cambia de opinión entre evaluaciones con lecturas casi iguales? |
| Latencia y costo | `latency_ms` y tokens del LLM (las reglas: ~0 ms, 0 USD) |

Primera versión: un script local (Python) que consulta DynamoDB y genera un reporte. Opcional después: métrica `DualMind/agreement` en CloudWatch.

---

## 9. Costos estimados (por semana)

| Componente | Uso | USD/semana |
|---|---|---|
| Reglas en ESP32 | — | 0.00 |
| Worker en la Pi | — | 0.00 |
| Bedrock Nova Micro | ~1,000 llamadas × ~1,500 in / ~150 out tokens | ≈ 0.07 |
| Bedrock Haiku 4.5 (si se prueba) | igual | ≈ 2.30 |
| DynamoDB (consultas de 12 h + UpdateItem) | ~1,000 ciclos | < 0.05 |
| Fase 1 (sin cambios) | | ≈ 0.05 – 0.75 |

Con Nova Micro la Fase 2 cabe holgada en el presupuesto. Con Haiku conviene correrlo solo unos días o bajar la frecuencia.

---

## 10. Cambios de infraestructura (OpenTofu, `infra/`)

- **Ninguno para las reglas** (la regla IoT ya guarda `SELECT *`).
- **Nuevos para el worker:** usuario IAM `dualmind-llm-worker` + política mínima (sección 4). La access key, por CLI.
- **Opcional:** métricas de CloudWatch para las decisiones (p. ej. contar `action != ninguna`).
- **Acceso a modelos:** confirmar en Bedrock que Nova Micro (y Haiku, si se usa) están habilitados en us-east-2 vía perfil `us.`. Nova Micro ya se probó en la Fase 0.
- *Por decidir:* si esta parte va en OpenTofu o en Pulumi.

---

## 11. Decisiones abiertas

| # | Pregunta | Por qué importa |
|---|---|---|
| 1 | ¿**Dónde está el sensor**? | ✅ Recámara en Ensenada, B.C. |
| 2 | ¿Dónde corre el worker del LLM? | ✅ Raspberry Pi, junto con la interfaz web (un solo servicio) |
| 3 | ¿Credenciales del worker? | ✅ Usuario IAM `dualmind-llm-worker` (A), key solo en la Pi |
| 4 | ¿Modelo inicial? | ✅ Nova Micro vía perfil de inferencia de aplicación; Haiku 4.5 después |
| 5 | ¿IaC del worker? | ✅ OpenTofu (`infra/llm.tf`) |

---

## 12. Plan de implementación (orden sugerido)

1. **Reglas en el firmware:** confort, contador de humedad con persistencia en NVS, punto de rocío, acción; campo `rules` en el mensaje. Verificar en serial y en DynamoDB.
2. **Infra del worker:** usuario/política IAM en IaC + access key por CLI; confirmar acceso al modelo.
3. **Worker en la Pi:** venv Python 3.13 + boto3; `evaluate()` con Converse + tool de salida estructurada; timer de systemd cada 10 min; logs locales.
4. **Comparación:** script de reporte (coincidencias, matriz de confusión, desacuerdos, latencia, tokens).
5. **Segundo modelo:** repetir unos días con Haiku 4.5 y comparar.
6. **Fase 3:** migrar `evaluate()` a AgentCore Runtime; contexto vía AgentCore Memory.

---

## 13. Implementación (2026-11-08)

| Paso | Estado | Dónde |
|---|---|---|
| 1. Reglas en el firmware | ✅ | `esp32/include/rules.h`, `esp32/src/rules.cpp`, `esp32/src/main.cpp` |
| 2. Infra del worker | ✅ | `infra/llm.tf` (+ access key por CLI, solo en la Pi) |
| 3. Worker en la Pi | ✅ | `pi/dualmind_pi/{llm,context,store}.py` |
| 4. Interfaz web en la Pi | ✅ (no estaba en el plan original) | `pi/dualmind_pi/app.py`, `pi/dualmind_pi/static/index.html` → `http://192.168.0.109:8000` |
| 5. Reporte de comparación | ⬜ | — |
| 6. Segundo modelo (Haiku 4.5) | ⬜ | — |

### Diferencias respecto al diseño

- **Campo `rules`:** además de lo previsto incluye `period` (`dia`/`noche`) y `local_hour`. La zona horaria es `PST8PDT,M3.2.0,M11.1.0` (America/Tijuana); `ts` sigue en UTC.
- **`history_reset`:** se pone en `true` cuando el contador no se pudo restaurar de NVS (guardado ausente o de hace ≥ 15 min) y se limpia cuando HR baja de 65 %, porque desde ahí el conteo vuelve a ser exacto.
- **Modo de prueba del contador:** `build_flags = -DMOLD_STEP_MIN=30` hace que cada minuto cuente como 30.
- **Modelo:** el worker usa el **perfil de inferencia de aplicación** `dualmind-nova-micro` (copia de `us.amazon.nova-micro-v1:0`, con tags `project`/`model`) como `modelId`, para ver el gasto por modelo. La política IAM cubre el perfil de aplicación, el de sistema y el modelo base en us-east-1/us-east-2/us-west-2. La descripción del perfil solo admite `[0-9a-zA-Z:.]` separados por espacio, `_` o `-`.
- **Worker + interfaz en un solo servicio** (FastAPI/uvicorn) en vez de un timer de systemd: caché en memoria de 12 h con refresco incremental cada 20 s, evaluación programada a :00:20, :10:20… (20 s de margen para que llegue el minuto) y botón manual con límite de 30 s. Si el último dato tiene > 3 min no evalúa y muestra el error en la interfaz.
- **Campo `llm`:** además de lo previsto lleva `model_id`, `temperature` (0.3) y `trigger` (`scheduled` | `manual`). Si dos evaluaciones caen en el mismo minuto, la última sobrescribe.
- **Contexto del LLM:** lugar ("recámara en Ensenada… se duerme ahí por la noche"), hora local y día de la semana, lectura del minuto y resumen por hora local de 12 h (~890 tokens de entrada). No recibe umbrales ni la decisión de las reglas.
- **Servicio de usuario** (`systemctl --user` + `loginctl enable-linger`) porque `sudo` en la Pi pide contraseña. Logs: `journalctl _SYSTEMD_USER_UNIT=dualmind.service`.

### Primeros resultados

| Hora (local) | Origen | Lectura | Reglas | LLM (Nova Micro) |
|---|---|---|---|---|
| 00:07 | manual | T 26.1 °C, HR ~64 % | caluroso / bajo / ventilador | caluroso / **medio** / **ventilar** — 764 ms, 890/45 tokens |
| 00:10 | programado | igual | caluroso / bajo / ventilador | caluroso / **medio** / **ventilar** — 604 ms, 891/45 tokens |

El LLM ve riesgo de moho con HR ~64 % sostenida; las reglas no, porque su contador solo suma con HR ≥ 70 %.

### Problema detectado: vocabulario de las acciones

El esquema de `report_decision` solo lista los valores permitidos, sin definirlos. `ventilar` (renovar el aire con el exterior: ventana, puerta o extractor; baja la humedad si afuera está más seco) y `ventilador` (mover el aire interior; solo mejora la sensación térmica, no cambia T ni HR) pueden parecerle sinónimos al modelo, y entonces un desacuerdo no siempre es de criterio. **Siguiente paso:** `prompt_v: 2` con una descripción por valor de `action` y `comfort`, y el reporte separado por `prompt_v`.

Limitación conocida: ninguna mente conoce las condiciones exteriores. En Ensenada el aire nocturno suele ser húmedo, así que "ventilar" puede no ayudar.
