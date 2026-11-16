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
# 2325 DB: 서버 맥과 짝을 지었으면(db_relay.py pair) 중계를, 아니면 이 PC 의 SSM 터널을 쓴다
if [ -f "$HOME/.config/db-relay/client.json" ]; then
  db_unit=pao-2325-relay.service
  systemctl --user disable --now pao-2325-tunnel.service 2>/dev/null || true
else
  db_unit=pao-2325-tunnel.service
  systemctl --user disable --now pao-2325-relay.service 2>/dev/null || true
fi
systemctl --user enable --now pao-agent.service pao-api.service "$db_unit"
for timer in "$here"/*.timer; do
  [ -e "$timer" ] && systemctl --user enable --now "$(basename "$timer")"
done
systemctl --user --no-pager status pao-agent.service | head -5
