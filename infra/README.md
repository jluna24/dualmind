# DualMind — infraestructura (OpenTofu)

Todo lo de AWS de la Fase 1 se gestiona desde aquí: thing, certificado, política
IoT, regla IoT, rol IAM y tabla DynamoDB. **No cambiar estos recursos desde la
consola**: el siguiente `apply` revierte los cambios.

## Llave y CSR del dispositivo (una sola vez, fuera de OpenTofu)

La llave privada se genera localmente y nunca entra al estado de OpenTofu.
Desde la carpeta `DualMind/` (Git Bash):

```bash
openssl ecparam -name prime256v1 -genkey -noout -out esp32/certs/private.pem.key
MSYS_NO_PATHCONV=1 openssl req -new -key esp32/certs/private.pem.key -subj "/CN=dualmind-esp32-01" -out infra/device.csr
```

(`MSYS_NO_PATHCONV=1` evita que Git Bash convierta `/CN=...` en una ruta de Windows.)

`device.csr` no es secreto. `private.pem.key` sí (está en `.gitignore`).

## Uso

```powershell
cd infra
tofu init
tofu plan -out='plan.tfplan'
tofu apply plan.tfplan
```

El `apply` escribe el certificado firmado en `esp32/certs/device.pem.crt`.
Luego se regenera el header del firmware y se flashea:

```powershell
cd ..\esp32
& "$env:USERPROFILE\.platformio\penv\Scripts\python.exe" tools\gen_aws_certs.py (tofu -chdir=..\infra output -raw iot_endpoint)
& "$env:USERPROFILE\.platformio\penv\Scripts\pio.exe" run -t upload
```

## Fase 2: LLM (`llm.tf`)

- Perfil de inferencia de aplicación por modelo (`var.llm_models`), salida `llm_profile_arns` → va en `DUALMIND_MODEL_ID` de `pi/dualmind.env`.
- Usuario IAM `dualmind-llm-worker` con política mínima (DynamoDB Query/GetItem/UpdateItem sobre la tabla, `bedrock:InvokeModel` sobre los perfiles y el modelo base en sus 3 regiones).
- La access key **no** está en OpenTofu: se creó por CLI y vive solo en la Pi (ver `pi/README.md`).

## Limpieza al final del proyecto

Primero borrar la access key del worker (creada fuera de OpenTofu), si no el usuario no se puede borrar:

```powershell
aws iam list-access-keys --user-name dualmind-llm-worker
aws iam delete-access-key --user-name dualmind-llm-worker --access-key-id <ID>
tofu destroy
```
