#!/bin/bash
# Postgres를 Windows 폴더로 백업한다. WSL이나 Docker 볼륨이 망가져도 Task가 남게 하기 위해서다.
# 복구: pg_restore -U pao -d pao --clean --if-exists < 백업파일 (컨테이너 안에서)
set -euo pipefail

DEST=${PAO_BACKUP_DIR:-/mnt/c/Users/zcb16/Documents/personal-agent-os-backups}
KEEP_DAYS=${PAO_BACKUP_KEEP_DAYS:-14}
COMPOSE="docker compose -f $HOME/projects/personal-agent-os/infra/docker-compose.yml"
FILE="$DEST/pao-$(date +%Y%m%d-%H%M).dump"

mkdir -p "$DEST"
# 도중에 실패한 파일이 정상 백업처럼 남지 않게 임시 이름으로 받고 확인한 뒤 옮긴다.
$COMPOSE exec -T postgres pg_dump -U pao -d pao -Fc > "$FILE.tmp"
$COMPOSE exec -T postgres pg_restore --list < "$FILE.tmp" > /dev/null
mv "$FILE.tmp" "$FILE"
find "$DEST" -name 'pao-*.dump' -mtime +"$KEEP_DAYS" -delete
echo "backup ok: $FILE ($(du -h "$FILE" | cut -f1))"
