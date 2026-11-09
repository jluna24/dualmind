"""Genera include/aws_certs.h a partir de los PEM en certs/.

Uso (desde la carpeta esp32/):
    python tools/gen_aws_certs.py <endpoint-iot>

El .h resultante contiene la llave privada: NO se comparte ni se sube a git.
"""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
CERTS = ROOT / "certs"
OUT = ROOT / "include" / "aws_certs.h"

FILES = {
    "AWS_ROOT_CA": "AmazonRootCA1.pem",
    "AWS_DEVICE_CERT": "device.pem.crt",
    "AWS_PRIVATE_KEY": "private.pem.key",
}


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit("Uso: python tools/gen_aws_certs.py <endpoint-iot>")
    endpoint = sys.argv[1]

    parts = [
        "// Generado por tools/gen_aws_certs.py. NO compartir ni subir a git.",
        "#pragma once",
        "",
        f'#define AWS_IOT_ENDPOINT "{endpoint}"',
        "",
    ]
    for name, filename in FILES.items():
        pem = (CERTS / filename).read_text().strip()
        parts.append(f'static const char {name}[] = R"PEM(\n{pem}\n)PEM";\n')

    OUT.write_text("\n".join(parts), newline="\n")
    print(f"Escrito {OUT}")


if __name__ == "__main__":
    main()
