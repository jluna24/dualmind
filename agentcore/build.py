"""Empaqueta el agente para AgentCore Runtime (despliegue directo de código).

AgentCore corre en Linux ARM64, así que las dependencias se bajan como wheels
aarch64 para Python 3.13 aunque esto se ejecute en Windows.

Uso (desde DualMind/):  python agentcore/build.py
Salida: agentcore/dist/agent.zip  (lo sube `tofu apply`, ver infra/agentcore.tf)

El zip es determinista (fechas fijas y orden alfabético): si el código no
cambia, el hash no cambia y OpenTofu no crea una versión nueva del Runtime.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PI_PKG = HERE.parent / "pi" / "dualmind_pi"
BUILD = HERE / "build"
ZIP = HERE / "dist" / "agent.zip"
SHARED = ["__init__.py", "context.py", "llm.py", "store.py"]  # sin app.py ni camera.py
PLATFORMS = ["manylinux2014_aarch64", "manylinux_2_17_aarch64", "manylinux_2_28_aarch64"]


def main() -> None:
    shutil.rmtree(BUILD, ignore_errors=True)
    BUILD.mkdir(parents=True)

    cmd = [sys.executable, "-m", "pip", "install", "-q", "--target", str(BUILD),
           "--python-version", "3.13", "--implementation", "cp", "--only-binary=:all:",
           "-r", str(HERE / "requirements.txt")]
    for p in PLATFORMS:
        cmd += ["--platform", p]
    subprocess.check_call(cmd)

    shutil.copy2(HERE / "agent.py", BUILD / "agent.py")
    (BUILD / "dualmind_pi").mkdir(exist_ok=True)
    for name in SHARED:
        shutil.copy2(PI_PKG / name, BUILD / "dualmind_pi" / name)

    ZIP.parent.mkdir(exist_ok=True)
    files = sorted(
        f for f in BUILD.rglob("*")
        if f.is_file() and "__pycache__" not in f.parts and f.suffix != ".pyc"
    )
    with zipfile.ZipFile(ZIP, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            info = zipfile.ZipInfo(f.relative_to(BUILD).as_posix(), date_time=(2026, 1, 1, 0, 0, 0))
            info.external_attr = 0o644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, f.read_bytes())
    print(f"{ZIP} ({ZIP.stat().st_size / 1e6:.1f} MB, {len(files)} archivos)")


if __name__ == "__main__":
    main()
