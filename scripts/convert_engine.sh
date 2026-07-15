#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="${PROJECT_ROOT:-$(cd "${SCRIPT_DIR}/.." && pwd)}"
PT_FILE="${PT_FILE:-0204_morning.pt}"
IMGSZ="${IMGSZ:-480}"
MODELS_DIR="${MODELS_DIR:-${PROJECT_ROOT}/models}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
INPUT_PATH="${MODELS_DIR}/${PT_FILE}"
OUTPUT_PATH="${INPUT_PATH%.pt}.engine"

echo "=========================================="
echo "TensorRT engine export"
echo "=========================================="
echo "Project root: ${PROJECT_ROOT}"
echo "Input model : ${INPUT_PATH}"
echo "Image size  : ${IMGSZ}"
echo "Output file : ${OUTPUT_PATH}"
echo "Owner       : CRK-model Python TensorRT service"
echo "=========================================="

if [[ ! -f "${INPUT_PATH}" ]]; then
    echo "ERROR: input model not found: ${INPUT_PATH}" >&2
    ls -la "${MODELS_DIR}" || true
    exit 1
fi

if ! command -v yolo >/dev/null 2>&1; then
    echo "ERROR: yolo CLI not found in PATH" >&2
    exit 1
fi

if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
    echo "ERROR: python command not found: ${PYTHON_BIN}" >&2
    exit 1
fi

if ! "${PYTHON_BIN}" - <<'PY'
import sys

try:
    import torch
except Exception as exc:
    print(f"ERROR: failed to import torch: {type(exc).__name__}: {exc}", file=sys.stderr)
    sys.exit(1)

cuda_version = getattr(torch.version, "cuda", None)
cuda_available = bool(torch.cuda.is_available())
print(f"Torch version : {getattr(torch, '__version__', 'unknown')}")
print(f"CUDA version  : {cuda_version}")
print(f"CUDA available: {cuda_available}")

if cuda_version is None or not cuda_available:
    print(
        "ERROR: TensorRT engine export requires Jetson CUDA-enabled torch. "
        "Create the venv with --system-site-packages or run scripts/install_jetson_torch.sh.",
        file=sys.stderr,
    )
    sys.exit(2)

# Jetson torch wheels are built against NumPy 1.x - export fails mid-run
# with "Downgrade to 'numpy<2'" if pip pulled NumPy 2 into this venv
# (typically via ultralytics auto-install of onnx during a previous export).
import numpy

print(f"NumPy version : {numpy.__version__}")
if numpy.__version__.startswith("2."):
    print(
        "ERROR: NumPy 2.x detected in this venv. Fix with:\n"
        '  uv pip install onnx onnxslim "numpy>=1.24.0,<2.0.0"\n'
        "(installing export deps together with the pin keeps the resolver "
        "from re-upgrading NumPy).",
        file=sys.stderr,
    )
    sys.exit(3)
PY
then
    exit 1
fi

# Block ultralytics runtime auto-install: it pip-installs missing export
# deps (onnx/onnxslim) on the fly and can silently upgrade NumPy to 2.x,
# breaking Jetson torch. setup_jetson.sh preinstalls these with the pin.
export YOLO_AUTOINSTALL=false

yolo export \
    model="${INPUT_PATH}" \
    format=engine \
    device=0 \
    half=True \
    imgsz="${IMGSZ}"

if [[ ! -f "${OUTPUT_PATH}" ]]; then
    echo "ERROR: export did not produce ${OUTPUT_PATH}" >&2
    exit 1
fi

# Post-check: fail loudly if anything bumped NumPy during export
"${PYTHON_BIN}" - <<'PY'
import sys
import numpy

if numpy.__version__.startswith("2."):
    print(
        f"WARNING: NumPy was upgraded to {numpy.__version__} during export. "
        'Restore with: uv pip install "numpy>=1.24.0,<2.0.0" --force-reinstall',
        file=sys.stderr,
    )
    sys.exit(4)
PY

echo "=========================================="
echo "Export complete"
echo "=========================================="
echo "Engine file : ${OUTPUT_PATH}"
du -h "${OUTPUT_PATH}"
