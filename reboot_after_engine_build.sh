#!/usr/bin/env bash
set -euo pipefail

REBOOT_FLAG="/tmp/crk_model_engine_build_reboot.flag"

if [ -f "$REBOOT_FLAG" ]; then
  echo "[BUILD][REBOOT] engine build completed. reboot now."
  rm -f "$REBOOT_FLAG"
  sleep 3
  systemctl reboot
else
  echo "[BUILD][REBOOT] reboot flag not found. skip reboot."
fi
