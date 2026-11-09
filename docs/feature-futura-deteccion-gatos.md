# DualMind — Feature futura: detección de gatos en la cámara

> Documento de análisis. Estado: **propuesta, no implementado** (análisis del 2026-11-08).
> Contexto: la Pi ya transmite la cámara USB (Logitech C922, MJPEG) en `/api/camera.mjpg` desde `pi/dualmind_pi/camera.py`. Relacionado con la Fase 4 (cámara + modelo multimodal).

---

## 1. Objetivo

Detectar cuándo hay un gato en cuadro, registrar los eventos (hora de entrada y salida, confianza) y, si se quiere, mostrarlos en la interfaz.

**Esfuerzo estimado:** unos **1 día** para un prototipo funcional y **2–3 días** si se quiere pulido (interfaz, eventos, ajustes de umbral). Puede correr todo en la Pi, sin costo de AWS.

---

## 2. Lo que ya existe a favor

- `camera.py` ya guarda en memoria el último cuadro JPEG (`camera.frame()`): el detector solo tiene que leerlo cada cierto tiempo.
- "Gato" ya viene como una de las 80 clases (la 15) de los modelos preentrenados con COCO. **No hay que entrenar nada.**
- La Raspberry Pi 5 (8 GB) corre un detector chico (YOLO nano o EfficientDet‑Lite0) a unos **5–15 cuadros por segundo** a 320–640 px solo con CPU. Para saber si hay un gato, 1–2 cuadros por segundo bastan.

---

## 3. Opciones

| Opción | Esfuerzo | Costo | Notas |
|---|---|---|---|
| **Detector local (recomendado)**: YOLO11n en ONNX o NCNN, o EfficientDet‑Lite0 en TFLite | ~1 día | 0 USD | Ultralytics (YOLO) tiene licencia **AGPL**. Si esto se ofrecerá a clientes, usar EfficientDet‑Lite0 o MobileNet‑SSD con OpenCV DNN (Apache) |
| Amazon Rekognition `DetectLabels` | ~medio día | ~0.001 USD por imagen | A 1 imagen por segundo son unos **86 USD al día**: acaba el presupuesto de 20 USD en horas |
| Bedrock multimodal (Nova Lite) en cada cuadro | ~medio día | igual de caro y más lento | Usarlo solo **después** de que el detector local vea un gato, por ejemplo para describir qué está haciendo |

### Arquitectura recomendada (híbrida)

```
cuadro JPEG ──► ¿hubo movimiento? (diferencia entre cuadros, casi gratis)
                     │ sí
                     ▼
               detector local (clase "cat", umbral ~0.4)
                     │ gato en 2–3 cuadros seguidos
                     ▼
               evento "gato visto" en store.py
                     │ opcional, 1 foto por evento
                     ▼
               Bedrock multimodal: "¿qué está haciendo el gato?"
```

Esto sigue la idea de DualMind: el detector es la parte **determinista** y el LLM la **probabilística**.

---

## 4. Cambios necesarios

1. **Cámara siempre encendida (o por horario).** Hoy la cámara solo se prende cuando alguien la está viendo. Hay que agregar un "espectador interno" que llame a `camera.acquire()` mientras la detección esté activa.
2. **Decodificar cuadros.** Hoy ffmpeg no decodifica (`-c:v copy`), y por eso casi no usa CPU. El detector sí necesita decodificar: muestreando 1–2 cuadros por segundo con `cv2.imdecode` sobre el JPEG, la carga es baja (aprox. un núcleo al 20–40 %). Conviene analizar a 640x360.
3. **Código nuevo:**
   - `pi/dualmind_pi/detector.py`, de unas 80–120 líneas
   - dependencias: `onnxruntime` u `opencv-python-headless`, más el archivo del modelo (6–10 MB)
   - un hilo que guarde eventos del tipo "gato visto de tal hora a tal hora, confianza 0.87" en `store.py`
   - un endpoint `/api/cats` y, opcional, cajas dibujadas sobre la imagen en la interfaz

---

## 5. Riesgos y limitaciones

- **Poca luz / noche:** la C922 pierde mucha calidad y suben los falsos negativos.
- **Poses difíciles:** un gato de espaldas, enroscado o muy lejos a veces no se detecta. Mitigación: umbral ~0.4 y exigir que aparezca en 2–3 cuadros seguidos.
- **Reconocer a un gato en particular** (no a cualquier gato) es otro proyecto: hay que juntar fotos y ajustar un modelo, unos días más.
- **Licencia:** revisar AGPL de Ultralytics antes de usarlo en algo para clientes.

---

## 6. Siguientes pasos (cuando se retome)

- [ ] Elegir modelo: EfficientDet‑Lite0 (Apache) o YOLO11n (AGPL)
- [ ] Prototipo de `detector.py` leyendo `camera.frame()` a 1–2 cuadros por segundo
- [ ] Guardar eventos en `store.py` y exponer `/api/cats`
- [ ] Medir CPU y temperatura de la Pi con la cámara siempre encendida
- [ ] (Opcional) Descripción con Bedrock, una foto por evento, registrando tokens y costo
