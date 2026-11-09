"""Cámara USB (Logitech C922) en tiempo real, bajo demanda.

La C922 entrega MJPEG, así que ffmpeg solo copia los cuadros (`-c:v copy`, sin
recodificar): la Pi casi no gasta CPU. Un único lector abre el dispositivo (solo
un proceso puede usarlo a la vez) y reparte el último cuadro a todos los
navegadores. ffmpeg arranca con el primer espectador y se detiene IDLE_S
segundos después de que se va el último: sin nadie viendo, la cámara está apagada.
"""

from __future__ import annotations

import collections
import logging
import subprocess
import threading
import time

log = logging.getLogger("dualmind.camera")

SOI, EOI = b"\xff\xd8", b"\xff\xd9"


class Camera:
    def __init__(self, device: str, size: str = "1280x720", fps: int = 30, idle_s: float = 10.0):
        self.device, self.size, self.fps, self.idle_s = device, size, fps, idle_s
        self._lock = threading.Lock()
        self._frame: bytes | None = None
        self._seq = 0
        self._frame_at = 0.0
        self._viewers = 0
        self._last_viewer_at = 0.0
        self._thread: threading.Thread | None = None
        self._proc: subprocess.Popen | None = None
        self.error: str | None = None

    # --- espectadores -------------------------------------------------------
    def acquire(self) -> None:
        with self._lock:
            self._viewers += 1
            self._last_viewer_at = time.monotonic()
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name="camera", daemon=True)
                self._thread.start()

    def release(self) -> None:
        with self._lock:
            self._viewers = max(0, self._viewers - 1)
            self._last_viewer_at = time.monotonic()

    def frame(self) -> tuple[bytes | None, int]:
        with self._lock:
            return self._frame, self._seq

    def status(self) -> dict:
        with self._lock:
            return {
                "on": self._proc is not None and self._proc.poll() is None,
                "viewers": self._viewers,
                "frame_age_s": round(time.monotonic() - self._frame_at, 1) if self._frame else None,
                "size": self.size,
                "fps": self.fps,
                "error": self.error,
            }

    def stop(self) -> None:
        with self._lock:
            self._viewers = 0
            self._last_viewer_at = 0.0
        self._kill()

    # --- lector ----------------------------------------------------------------
    def _wanted(self) -> bool:
        with self._lock:
            return self._viewers > 0 or time.monotonic() - self._last_viewer_at < self.idle_s

    def _kill(self) -> None:
        p = self._proc
        if p and p.poll() is None:
            p.terminate()
            try:
                p.wait(3)
            except subprocess.TimeoutExpired:
                p.kill()

    def _run(self) -> None:
        while self._wanted():
            try:
                self._capture()
            except Exception as e:  # noqa: BLE001 - se muestra en la interfaz
                self.error = str(e)
                log.warning("Cámara: %s", e)
            if self._wanted():
                time.sleep(3)  # dispositivo desconectado u ocupado: reintentar
        with self._lock:
            self._frame = None
        log.info("Cámara apagada (sin espectadores)")

    def _capture(self) -> None:
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
            "-fflags", "nobuffer", "-flags", "low_delay",
            "-f", "v4l2", "-input_format", "mjpeg",
            "-video_size", self.size, "-framerate", str(self.fps),
            "-i", self.device,
            "-c:v", "copy", "-f", "mjpeg", "-",
        ]
        log.info("Cámara encendida: %s %s@%d", self.device, self.size, self.fps)
        self._proc = proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        errs: collections.deque[str] = collections.deque(maxlen=5)
        threading.Thread(
            target=lambda: [errs.append(l.decode(errors="replace").strip()) for l in proc.stderr],
            daemon=True,
        ).start()
        buf = b""
        idle_check = time.monotonic()
        try:
            while True:
                chunk = proc.stdout.read1(65536)
                if not chunk:
                    break
                buf += chunk
                # Un cuadro MJPEG de webcam = JPEG completo de SOI a EOI (sin EXIF).
                while True:
                    start = buf.find(SOI)
                    end = buf.find(EOI, start + 2) if start >= 0 else -1
                    if end < 0:
                        if start > 0:
                            buf = buf[start:]
                        break
                    frame, buf = buf[start:end + 2], buf[end + 2:]
                    with self._lock:
                        self._frame, self._frame_at = frame, time.monotonic()
                        self._seq += 1
                    self.error = None
                if len(buf) > 4_000_000:  # flujo corrupto: descartar
                    buf = b""
                now = time.monotonic()
                if now - idle_check > 1:
                    idle_check = now
                    if not self._wanted():
                        break
        finally:
            self._kill()
            self._proc = None
        if proc.returncode not in (0, None, -15) and self._wanted():
            raise RuntimeError("; ".join(errs) or f"ffmpeg terminó con código {proc.returncode}")
