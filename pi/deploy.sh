#!/usr/bin/env bash
# Copia el código a la Pi, instala dependencias y reinicia el servicio de usuario.
# Uso (Git Bash, desde DualMind/pi): ./deploy.sh
# Requisito una sola vez en la Pi: loginctl enable-linger jluna
set -euo pipefail
PI=${PI:-jluna@192.168.0.109}

ssh "$PI" "mkdir -p ~/dualmind ~/.config/systemd/user"
tar --exclude='__pycache__' -czf - dualmind_pi requirements.txt dualmind.env dualmind.service \
  | ssh "$PI" "tar -xzf - -C ~/dualmind"
ssh "$PI" bash -s <<'REMOTE'
set -euo pipefail
cd ~/dualmind
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt
cp dualmind.service ~/.config/systemd/user/dualmind.service
systemctl --user daemon-reload
systemctl --user enable dualmind
systemctl --user restart dualmind
sleep 4
systemctl --user --no-pager --lines=10 status dualmind
REMOTE
