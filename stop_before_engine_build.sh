#!/usr/bin/env bash
set -euo pipefail

echo "[BUILD][STOP] stop CRK related systemd services"

SERVICE_LIST=(
  "/etc/systemd/system/crk-model.service"
  "/etc/systemd/system/edge-environment.service"
  "/etc/systemd/system/crk-camera.service"
  "/etc/systemd/system/crk-payment.service"
  "/etc/systemd/system/crk-io-board.service"
)

for svc in "${SERVICE_LIST[@]}"; do
  echo "[BUILD][STOP] try stop: $svc"
  systemctl stop "$svc" 2>/dev/null || true
done

echo "[BUILD][STOP] kill leftover CRK runtime processes"

PROJECT_DIRS=(
  "/home/chai/Desktop/Codes/Edge_Environment"
  "/home/chai/Desktop/Codes/CRK-CAMERA"
  "/home/chai/Desktop/Codes/CRK-IO-BOARD"
  "/home/chai/Desktop/Codes/CRK-PAYMENT"
  "/home/chai/Desktop/Codes/CRK-model"
)

CMD_REGEX='(npm|node|python|python3|uv|model-service|model-services|yolo)'

kill_matching_processes() {
  local sig="$1"

  while read -r pid; do
    [ -z "$pid" ] && continue

    # 자기 자신과 부모 shell은 제외
    if [ "$pid" = "$$" ] || [ "$pid" = "$PPID" ]; then
      continue
    fi

    if [ ! -d "/proc/$pid" ]; then
      continue
    fi

    local cwd=""
    local cmd=""

    cwd="$(readlink -f "/proc/$pid/cwd" 2>/dev/null || true)"
    cmd="$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true)"

    [ -z "$cwd" ] && continue
    [ -z "$cmd" ] && continue

    for dir in "${PROJECT_DIRS[@]}"; do
      if [[ "$cwd" == "$dir"* ]]; then
        echo "[BUILD][STOP] kill -$sig pid=$pid cwd=$cwd cmd=$cmd"
        kill "-$sig" "$pid" 2>/dev/null || true
        break
      fi
    done
  done < <(pgrep -f "$CMD_REGEX" || true)
}

# 1차 정상 종료
kill_matching_processes TERM

sleep 3

# 남아 있으면 강제 종료
kill_matching_processes KILL

echo "[BUILD][STOP] remaining related processes:"
ps aux | grep -E "Edge_Environment|CRK-CAMERA|CRK-IO-BOARD|CRK-PAYMENT|CRK-model|model-service|model-services|npm run start|node server/index.js|uv run src/main.py" | grep -v grep || true

echo "[BUILD][STOP] done"
