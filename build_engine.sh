#!/usr/bin/env bash
set -Eeuo pipefail

umask 022

ENV_FILE="/home/chai/Desktop/Codes/CRK-model/.env"
REBOOT_FLAG="/tmp/crk_model_engine_build_reboot.flag"

# 직접 실행하여 성공한 입력 크기
YOLO_IMGSZ="${YOLO_IMGSZ:-480}"

BUILT_NEW_ENGINE=0


log() {
  echo "[BUILD] $*"
}


error() {
  echo "[BUILD][ERROR] $*" >&2
}


fail() {
  error "$*"
  rm -f "$REBOOT_FLAG"
  exit 1
}


on_error() {
  local exit_code=$?
  local line_number="${BASH_LINENO[0]:-unknown}"

  error "build failed at line ${line_number}, exit code=${exit_code}"
  rm -f "$REBOOT_FLAG"

  exit "$exit_code"
}


trap on_error ERR


# 이전 실행에서 남아 있을 수 있는 재부팅 flag 제거
rm -f "$REBOOT_FLAG"


# ------------------------------------------------------------------
# 1. 환경변수 검사
# ------------------------------------------------------------------

: "${MODEL_DIR:?MODEL_DIR is not defined}"
: "${VENV_ACTIVATE:?VENV_ACTIVATE is not defined}"
: "${MODELS_DIR:?MODELS_DIR is not defined}"
: "${PT_FILE:?PT_FILE is not defined}"
: "${ENGINE_FILE:?ENGINE_FILE is not defined}"


log "MODEL_DIR=$MODEL_DIR"
log "MODELS_DIR=$MODELS_DIR"
log "PT_FILE=$PT_FILE"
log "ENGINE_FILE=$ENGINE_FILE"
log "ENV_FILE=$ENV_FILE"
log "YOLO_IMGSZ=$YOLO_IMGSZ"


# ------------------------------------------------------------------
# 2. 파일 및 디렉터리 검사
# ------------------------------------------------------------------

[ -d "$MODEL_DIR" ] ||
  fail "model directory not found: $MODEL_DIR"

[ -d "$MODELS_DIR" ] ||
  fail "models directory not found: $MODELS_DIR"

[ -f "$VENV_ACTIVATE" ] ||
  fail "venv activate file not found: $VENV_ACTIVATE"

[ -f "$PT_FILE" ] ||
  fail "pt model file not found: $PT_FILE"

[ -f "$ENV_FILE" ] ||
  fail ".env file not found: $ENV_FILE"

case "$PT_FILE" in
  *.pt)
    ;;
  *)
    fail "PT_FILE must have .pt extension: $PT_FILE"
    ;;
esac


# ------------------------------------------------------------------
# 3. 가상환경 실행 파일 설정
# ------------------------------------------------------------------

VENV_BIN_DIR="$(dirname "$VENV_ACTIVATE")"
PYTHON_BIN="$VENV_BIN_DIR/python"
YOLO_BIN="$VENV_BIN_DIR/yolo"

[ -x "$PYTHON_BIN" ] ||
  fail "venv python is not executable: $PYTHON_BIN"

[ -x "$YOLO_BIN" ] ||
  fail "venv yolo is not executable: $YOLO_BIN"


# 시스템 Python 패키지가 가상환경 패키지보다 먼저 로드되는 문제 방지
unset PYTHONPATH
unset PYTHONHOME

log "Python executable: $PYTHON_BIN"
log "YOLO executable: $YOLO_BIN"


# ------------------------------------------------------------------
# 4. Python, CUDA, TensorRT 및 의존성 검사
# ------------------------------------------------------------------

log "check Python dependencies, CUDA and TensorRT"

"$PYTHON_BIN" - <<'PY'
import sys

try:
    import numpy
    import cv2
    import sympy
    import torch
    import tensorrt as trt
except Exception as exc:
    print(f"[BUILD][ERROR] dependency import failed: {exc}", file=sys.stderr)
    raise

print("[BUILD] python:", sys.executable)
print("[BUILD] numpy:", numpy.__version__, numpy.__file__)
print("[BUILD] cv2:", cv2.__file__)
print("[BUILD] sympy:", sympy.__version__, sympy.__file__)
print("[BUILD] torch:", torch.__version__)
print("[BUILD] torch CUDA:", torch.version.cuda)
print("[BUILD] CUDA available:", torch.cuda.is_available())
print("[BUILD] CUDA device count:", torch.cuda.device_count())
print("[BUILD] TensorRT:", trt.__version__, trt.__file__)

if not torch.cuda.is_available():
    raise SystemExit(
        "[BUILD][ERROR] CUDA is not available. "
        "TensorRT engine export requires CUDA."
    )

if torch.cuda.device_count() < 1:
    raise SystemExit("[BUILD][ERROR] no CUDA device detected")

print("[BUILD] CUDA device:", torch.cuda.get_device_name(0))
PY


# ------------------------------------------------------------------
# 5. 기존 engine 확인 및 YOLO export
# ------------------------------------------------------------------

GENERATED_ENGINE="${PT_FILE%.pt}.engine"

if [ -s "$ENGINE_FILE" ]; then
  log "engine file already exists; skip YOLO export"
  log "existing engine: $ENGINE_FILE"
else
  if [ -f "$ENGINE_FILE" ]; then
    log "remove empty or invalid engine file: $ENGINE_FILE"
    rm -f "$ENGINE_FILE"
  fi

  # export 결과가 다른 경로에 생성되는 경우 기존 파일과 혼동하지 않도록 제거
  if [ "$GENERATED_ENGINE" != "$ENGINE_FILE" ] &&
     [ -f "$GENERATED_ENGINE" ]; then
    log "remove stale generated engine: $GENERATED_ENGINE"
    rm -f "$GENERATED_ENGINE"
  fi

  mkdir -p "$(dirname "$ENGINE_FILE")"

  cd "$MODEL_DIR"

  log "start YOLO TensorRT export"
  log "command: yolo export model=$PT_FILE format=engine imgsz=$YOLO_IMGSZ"

  env -u PYTHONPATH -u PYTHONHOME \
    "$YOLO_BIN" export \
    model="$PT_FILE" \
    format=engine \
    imgsz="$YOLO_IMGSZ"

  if [ ! -s "$GENERATED_ENGINE" ]; then
    fail "generated engine file not found or empty: $GENERATED_ENGINE"
  fi

  if [ "$GENERATED_ENGINE" != "$ENGINE_FILE" ]; then
    log "move generated engine"
    log "source: $GENERATED_ENGINE"
    log "target: $ENGINE_FILE"

    mv -f "$GENERATED_ENGINE" "$ENGINE_FILE"
  fi

  if [ ! -s "$ENGINE_FILE" ]; then
    fail "final engine file not found or empty: $ENGINE_FILE"
  fi

  BUILT_NEW_ENGINE=1

  log "YOLO TensorRT export completed"
  ls -lh "$ENGINE_FILE"
fi


# ------------------------------------------------------------------
# 6. .env 경로 계산
# ------------------------------------------------------------------

MODEL_DIR_REAL="$(realpath -m "$MODEL_DIR")"
ENGINE_FILE_REAL="$(realpath -m "$ENGINE_FILE")"

if [[ "$ENGINE_FILE_REAL" == "$MODEL_DIR_REAL/"* ]]; then
  ENGINE_ENV_PATH="$(realpath --relative-to="$MODEL_DIR_REAL" "$ENGINE_FILE_REAL")"
else
  # engine이 프로젝트 외부에 있으면 절대경로 사용
  ENGINE_ENV_PATH="$ENGINE_FILE_REAL"
fi

log "engine path for .env: $ENGINE_ENV_PATH"


# ------------------------------------------------------------------
# 7. .env 백업 및 MODEL__VISION__YOLO_MODEL_PATH 갱신
# ------------------------------------------------------------------

ENV_BACKUP="${ENV_FILE}.bak"

cp -a "$ENV_FILE" "$ENV_BACKUP"

log ".env backup created: $ENV_BACKUP"

if grep -q '^MODEL__VISION__YOLO_MODEL_PATH=' "$ENV_FILE"; then
  sed -i \
    "s|^MODEL__VISION__YOLO_MODEL_PATH=.*|MODEL__VISION__YOLO_MODEL_PATH=${ENGINE_ENV_PATH}|" \
    "$ENV_FILE"
else
  printf '\nMODEL__VISION__YOLO_MODEL_PATH=%s\n' \
    "$ENGINE_ENV_PATH" >> "$ENV_FILE"
fi

UPDATED_ENV_VALUE="$(
  grep '^MODEL__VISION__YOLO_MODEL_PATH=' "$ENV_FILE" | tail -n 1
)"

EXPECTED_ENV_VALUE="MODEL__VISION__YOLO_MODEL_PATH=${ENGINE_ENV_PATH}"

if [ "$UPDATED_ENV_VALUE" != "$EXPECTED_ENV_VALUE" ]; then
  fail ".env update verification failed"
fi

log ".env updated successfully"
log "$UPDATED_ENV_VALUE"


# ------------------------------------------------------------------
# 8. 새 engine 생성 시에만 재부팅 flag 생성
# ------------------------------------------------------------------

if [ "$BUILT_NEW_ENGINE" -eq 1 ]; then
  touch "$REBOOT_FLAG"
  chmod 600 "$REBOOT_FLAG"

  log "new engine was created"
  log "reboot flag created: $REBOOT_FLAG"
else
  log "engine build was skipped; reboot flag was not created"
fi


log "build process completed successfully"
