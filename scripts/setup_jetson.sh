#!/usr/bin/env bash
# CRK-model-HG Jetson 환경 준비 (1회).
#
# CRK-model의 Jetson 관행 준용:
# - venv는 --system-site-packages: JetPack이 제공하는 CUDA/TensorRT/torch/
#   OpenCV/numpy(<2)를 그대로 사용한다.
# - ultralytics를 PyPI 전체 의존성으로 설치하지 않는다 (CPU torch 오염 방지).
#   이미 CRK-model .venv 또는 system-site에 있으면 그것을 쓴다.
set -euo pipefail
cd "$(dirname "$0")/.."

if ! command -v uv >/dev/null 2>&1; then
  echo "uv가 필요합니다: https://docs.astral.sh/uv/" >&2
  exit 1
fi

uv venv --system-site-packages --python python3.10 .venv
# shellcheck disable=SC1091
source .venv/bin/activate

# 코어는 의존성 0 — 서버 어댑터 의존성만 추가
uv pip install --no-deps -e .
uv pip install "fastapi>=0.100.0" "uvicorn[standard]>=0.23.0"

# ultralytics가 system-site에 없으면 CRK-model 관행대로 --no-deps 설치
python - <<'EOF' || uv pip install --no-deps "ultralytics>=8.0.0,<9.0.0" "ultralytics-thop>=2.0.18"
import ultralytics  # noqa: F401
EOF

echo ""
echo "setup 완료. 실행:"
echo "  source .venv/bin/activate"
echo "  MODEL__VISION__YOLO_MODEL_PATH=models/siyeon_best.engine model-service-hg"
