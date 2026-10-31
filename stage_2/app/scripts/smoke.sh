#!/usr/bin/env bash
# Smoke-тест compose (T17): поднимает стек, добавляет каналы из smoke_channels.txt,
# ждёт доставки постов в эмулятор, проверяет мягкое удаление, гасит стек.
#
# Использование: scripts/smoke.sh [--keep]   (--keep не удаляет volume postgres)
set -euo pipefail

cd "$(dirname "$0")/.."

CHANNELS_FILE="scripts/smoke_channels.txt"
HEALTH_TIMEOUT=120
POSTS_TIMEOUT=180
KEEP=0
[[ "${1:-}" == "--keep" ]] && KEEP=1

[[ -f "$CHANNELS_FILE" ]] || { echo "нет файла $CHANNELS_FILE" >&2; exit 1; }

channels_json=$(python3 - "$CHANNELS_FILE" <<'EOF'
import json
import sys

items = []
for raw in open(sys.argv[1], encoding="utf-8"):
    line = raw.strip()
    if not line or line.startswith("#"):
        continue
    url, name, channel_id = (line.split("|") + ["", ""])[:3]
    item = {"channel_url": url, "channel_name": name}
    if channel_id.strip():
        item["channel_id"] = int(channel_id)
    items.append(item)
if not items:
    sys.exit("no channels in file")
print(json.dumps(items, ensure_ascii=False))
EOF
)

channel_names=$(awk -F'|' '!/^\s*$/ && !/^\s*#/ {print $2}' "$CHANNELS_FILE")
first_channel=$(printf '%s\n' "$channel_names" | head -n 1)

emulator_logs() {
  docker compose logs emulator 2>/dev/null || true
}

post_count() {
  emulator_logs | grep -c "\"channel_id\": \"${1}\"" || true
}

all_delivered() {
  printf '%s\n' "$channel_names" | while read -r name; do
    if (( $(post_count "$name") < 1 )); then
      return 1
    fi
  done
}

echo "==> docker compose up -d --build"
docker compose up -d --build

cleanup() {
  echo "==> docker compose down"
  if [[ "$KEEP" == "1" ]]; then
    docker compose down
  else
    docker compose down -v
  fi
}
trap cleanup EXIT

echo "==> ожидание /health (${HEALTH_TIMEOUT}s)..."
deadline=$(( $(date +%s) + HEALTH_TIMEOUT ))
until curl -fsS http://localhost:8000/health >/dev/null 2>&1; do
  if (( $(date +%s) > deadline )); then
    echo "health не поднялся за ${HEALTH_TIMEOUT}s" >&2
    docker compose logs api crawler migrate >&2 || true
    exit 1
  fi
  sleep 2
done
echo "health: ok"

echo "==> POST /watch_channels"
curl -fsS -X POST http://localhost:8000/watch_channels \
  -H 'Content-Type: application/json' \
  -d "$channels_json"
echo

echo "==> ожидание постов в эмуляторе (${POSTS_TIMEOUT}s)..."
deadline=$(( $(date +%s) + POSTS_TIMEOUT ))
until all_delivered; do
  if (( $(date +%s) > deadline )); then
    echo "не все каналы получили посты за ${POSTS_TIMEOUT}s" >&2
    exit 1
  fi
  sleep 5
done

echo "==> сводка по каналам:"
status=0
printf '%s\n' "$channel_names" | while read -r name; do
  count=$(post_count "$name")
  echo "  ${name}: ${count} пост(ов)"
  if (( count < 1 )); then
    status=1
    exit 1
  fi
done || status=1

echo "==> DELETE первого канала (${first_channel})"
curl -fsS -X DELETE "http://localhost:8000/watch_channels/${first_channel}"
echo
active_state=$(curl -fsS 'http://localhost:8000/watch_channels?active=all' \
  | python3 -c "
import json, sys
name = sys.argv[1]
rows = json.load(sys.stdin)
row = next((r for r in rows if r['channel_name'] == name), None)
print(None if row is None else row['is_active'])
" "$first_channel")
if [[ "$active_state" != "False" ]]; then
  echo "канал ${first_channel} не деактивирован (is_active=${active_state})" >&2
  exit 1
fi
echo "  ${first_channel}: is_active=false ✓"

if [[ "$status" != "0" ]]; then
  echo "SMOKE FAILED" >&2
  exit 1
fi
echo "SMOKE OK"
