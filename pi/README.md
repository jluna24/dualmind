# DualMind — Raspberry Pi (interfaz + worker del LLM)

Un solo servicio (FastAPI) que:

- mantiene en memoria las últimas 12 h de `dualmind-telemetry` (refresco incremental cada 20 s),
- evalúa con el LLM cada 10 min (:00:20, :10:20, …) y con el botón de la interfaz (límite 30 s),
- guarda la decisión en el campo `llm` del mismo ítem (`trigger`: `scheduled` | `manual`),
- sirve la interfaz en `http://192.168.0.109:8000`.

| Archivo | Qué hace |
|---|---|
| `dualmind_pi/llm.py` | `evaluate(context) -> decision`: Bedrock Converse con salida forzada (`report_decision`). Portátil a AgentCore Runtime |
| `dualmind_pi/context.py` | Arma el contexto: lectura actual + resumen por hora local |
| `dualmind_pi/store.py` | Caché de DynamoDB y `UpdateItem` del campo `llm` |
| `dualmind_pi/app.py` | API, programador e interfaz |
| `dualmind_pi/camera.py` | Cámara USB C922 en vivo (`/api/camera.mjpg`, `/api/camera.jpg`): MJPEG sin recodificar, solo encendida mientras alguien la ve (se apaga 10 s después) |
| `dualmind.env` | Configuración (sin secretos). `DUALMIND_MODEL_ID` = salida `llm_profile_arns` de OpenTofu |

## Credenciales

Perfil `dualmind-llm-worker` en `~/.aws/credentials` de la Pi (permisos 600). La key se
creó por CLI y se envió por SSH sin guardarse en la PC:

```bash
aws iam create-access-key --user-name dualmind-llm-worker \
  --query 'AccessKey.[AccessKeyId,SecretAccessKey]' --output text | ssh jluna@192.168.0.109 '...'
```

## Despliegue

```bash
cd pi && ./deploy.sh      # copia, instala en .venv y reinicia el servicio
```

Servicio de usuario (no requiere sudo; una sola vez: `loginctl enable-linger jluna`):

```bash
ssh jluna@192.168.0.109 'systemctl --user status dualmind'
ssh jluna@192.168.0.109 'journalctl _SYSTEMD_USER_UNIT=dualmind.service -f'   # --user no tiene journal propio aquí
```
