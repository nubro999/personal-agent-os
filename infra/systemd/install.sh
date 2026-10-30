#!/bin/bash
# systemd 사용자 유닛을 설치하고 켠다. 다시 실행해도 된다.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
dest="$HOME/.config/systemd/user"
mkdir -p "$dest"
for unit in "$here"/*.service "$here"/*.timer; do
  [ -e "$unit" ] && install -m 644 "$unit" "$dest/"
done
systemctl --user daemon-reload
systemctl --user enable --now pao-agent.service pao-api.service pao-2325-tunnel.service
for timer in "$here"/*.timer; do
  [ -e "$timer" ] && systemctl --user enable --now "$(basename "$timer")"
done
systemctl --user --no-pager status pao-agent.service | head -5
