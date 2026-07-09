#!/usr/bin/env bash

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_ROOT"

VENV_PATH="${PROJECT_ROOT}/.venv"
PYTHON_BIN="${PYTHON_BIN:-python3.10}"
FORCE_RECREATE_VENV="${FORCE_RECREATE_VENV:-0}"
INSTALL_JETSON_TORCH="${INSTALL_JETSON_TORCH:-1}"

print_step() {
    echo -e "\n${YELLOW}[$1] $2${NC}"
}

print_ok() {
    echo -e "${GREEN}OK${NC} $1"
}

print_warn() {
    echo -e "${YELLOW}WARN${NC} $1"
}

print_err() {
    echo -e "${RED}ERROR${NC} $1"
}

install_activation_hook() {
    local activate_path hook_block
    activate_path="${VENV_PATH}/bin/activate"

    if [[ ! -f "${activate_path}" ]]; then
        print_err "Activation script not found: ${activate_path}"
        exit 1
    fi

    # Persist the Jetson runtime linker setup across future shells so users only
    # need `source .venv/bin/activate` before starting the service.
    hook_block=$'\n# model-service Jetson runtime hook\nif [ -n "${VIRTUAL_ENV:-}" ] && [ -f "${VIRTUAL_ENV}/../scripts/jetson_env.sh" ]; then\n    . "${VIRTUAL_ENV}/../scripts/jetson_env.sh"\nfi\n'

    if grep -Fq 'model-service Jetson runtime hook' "${activate_path}"; then
        print_ok "Jetson activation hook already installed"
        return
    fi

    printf '%s' "${hook_block}" >> "${activate_path}"
    print_ok "Installed Jetson activation hook into .venv/bin/activate"
}

install_user_launcher() {
    bash "${PROJECT_ROOT}/scripts/install_model_service_launcher.sh"
}

install_project_packages() {
    uv pip install --no-deps -e .

    uv pip install \
        "fastapi>=0.100.0" \
        "uvicorn[standard]>=0.23.0" \
        "pydantic>=2.0.0" \
        "pydantic-settings>=2.0.0" \
        "python-multipart>=0.0.6" \
        "httpx>=0.24.0" \
        "aiohttp>=3.8.0" \
        "numpy>=1.24.0,<2.0.0" \
        "pillow>=10.0.0" \
        "pyyaml>=6.0.0" \
        "requests>=2.23.0" \
        "scipy>=1.4.1" \
        "matplotlib>=3.3.0" \
        "psutil>=5.8.0" \
        "polars>=0.20.0" \
        "ultralytics-thop>=2.0.18"

    uv pip install --no-deps "ultralytics>=8.0.0,<9.0.0"
    uv pip install \
        "pytest>=7.0.0" \
        "pytest-asyncio>=0.21.0" \
        "pytest-cov>=4.0.0" \
        "ruff>=0.1.0"

    if python -c "import cv2" >/dev/null 2>&1; then
        print_ok "OpenCV available"
    else
        print_warn "OpenCV not found from system packages. Installing opencv-python-headless."
        uv pip install "opencv-python-headless>=4.8.0"
    fi
}

print_step "1/9" "Checking Jetson prerequisites"

if [[ ! -f /etc/nv_tegra_release ]]; then
    print_err "This script must be run on a Jetson device."
    exit 1
fi

if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
    print_err "Python interpreter not found: ${PYTHON_BIN}"
    exit 1
fi

PYTHON_VERSION="$(${PYTHON_BIN} -c 'import sys; print("{}.{}".format(sys.version_info.major, sys.version_info.minor))')"
if [[ "${PYTHON_VERSION}" != "3.10" ]]; then
    print_warn "Expected Python 3.10 on Jetson, found ${PYTHON_VERSION}."
fi
print_ok "Python ${PYTHON_VERSION}"

if command -v nvcc >/dev/null 2>&1; then
    print_ok "CUDA detected: $(nvcc --version | grep release | awk '{print $5}' | tr -d ',')"
else
    print_warn "nvcc not found in PATH. CUDA may still be installed, but PATH should be checked."
fi

if python3 -c "import tensorrt" >/dev/null 2>&1; then
    print_ok "TensorRT Python package detected"
else
    print_warn "TensorRT Python package not found. Engine loading will fail until it is available."
fi

if python3 -c "import torch; assert torch.cuda.is_available()" >/dev/null 2>&1; then
    print_ok "System PyTorch can see CUDA"
else
    print_warn "System PyTorch is missing or CUDA is not available."
    print_warn "The setup will try to install a Jetson-compatible torch wheel into .venv."
fi

print_step "2/9" "Checking uv"

if ! command -v uv >/dev/null 2>&1; then
    print_warn "uv not found. Installing to ~/.local/bin"
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="${HOME}/.local/bin:${HOME}/.cargo/bin:${PATH}"
else
    print_ok "uv detected: $(uv --version)"
fi

print_step "3/9" "Preparing virtual environment"

if [[ -d "${VENV_PATH}" && "${FORCE_RECREATE_VENV}" == "1" ]]; then
    print_warn "Removing existing .venv because FORCE_RECREATE_VENV=1"
    rm -rf "${VENV_PATH}"
fi

if [[ ! -d "${VENV_PATH}" ]]; then
    uv venv --system-site-packages --python "${PYTHON_BIN}" "${VENV_PATH}"
    print_ok "Created .venv with system site packages"
else
    print_ok "Reusing existing .venv"
fi

source "${VENV_PATH}/bin/activate"
# Load the runtime paths in the current shell as well, so the validation steps
# below exercise the same CUDA/TensorRT environment that normal runtime uses.
. "${PROJECT_ROOT}/scripts/jetson_env.sh"

print_step "4/9" "Ensuring Jetson-compatible torch"

if python -c "import torch; assert torch.cuda.is_available()" >/dev/null 2>&1; then
    print_ok "Current venv PyTorch can see CUDA"
else
    if [[ "${INSTALL_JETSON_TORCH}" != "1" ]]; then
        print_err "PyTorch inside .venv cannot see CUDA and INSTALL_JETSON_TORCH=0."
        exit 1
    fi

    "${PROJECT_ROOT}/scripts/install_jetson_torch.sh"
fi

print_step "5/9" "Installing project dependencies"

install_project_packages
print_ok "Project dependencies installed without replacing Jetson torch"

NUMPY_VERSION="$(python -c 'import numpy; print(numpy.__version__)')"
if [[ "${NUMPY_VERSION}" == 2.* ]]; then
    print_warn "NumPy ${NUMPY_VERSION} detected. Reinstalling NumPy 1.x for Jetson compatibility."
    uv pip install "numpy>=1.24.0,<2.0.0" --force-reinstall
    NUMPY_VERSION="$(python -c 'import numpy; print(numpy.__version__)')"
fi
print_ok "NumPy ${NUMPY_VERSION}"

print_step "6/9" "Preparing runtime configuration"

if [[ ! -f "${PROJECT_ROOT}/.env" ]]; then
    cp "${PROJECT_ROOT}/.env.example" "${PROJECT_ROOT}/.env"
    print_warn "Created .env from .env.example. Review MODEL__VISION__YOLO_MODEL_PATH before running."
else
    print_ok ".env already present"
fi

ENGINE_COUNT="$(find "${PROJECT_ROOT}/models" -maxdepth 1 -name '*.engine' 2>/dev/null | wc -l | tr -d ' ')"
if [[ "${ENGINE_COUNT}" == "0" ]]; then
    print_warn "No .engine file found under models/. Update .env after copying your engine file."
else
    print_ok "Detected ${ENGINE_COUNT} engine file(s) under models/"
    find "${PROJECT_ROOT}/models" -maxdepth 1 -name '*.engine' -print
fi

print_step "7/9" "Verifying imports inside the venv"

python <<'PY'
from model_service.core.config import Settings

settings = Settings()
print(f"Resolved engine path: {settings.yolo_model_path}")
print(f"Resolved host/port: {settings.host}:{settings.port}")
PY

python <<'PY'
import fastapi
import numpy
import torch

print(f"FastAPI: {fastapi.__version__}")
print(f"NumPy: {numpy.__version__}")
print(f"PyTorch: {torch.__version__}")
print(f"CUDA version: {torch.version.cuda}")
print(f"CUDA available: {torch.cuda.is_available()}")

if torch.version.cuda is None:
    raise SystemExit("PyTorch is still CPU-only inside .venv.")

if not torch.cuda.is_available():
    raise SystemExit("PyTorch can import, but CUDA is still unavailable inside .venv.")
PY

print_step "8/9" "Verifying entry points"

if model-service --help >/dev/null 2>&1; then
    print_ok "model-service entry point is available"
else
    print_err "model-service entry point is not available after install"
    exit 1
fi

if pytest --version >/dev/null 2>&1; then
    print_ok "pytest is available"
else
    print_err "pytest is not available after install"
    exit 1
fi

print_step "9/11" "Installing activation hook"

install_activation_hook

print_step "10/11" "Installing user launcher"

install_user_launcher

print_step "11/11" "Done"

echo -e "${BLUE}Recommended runtime commands${NC}"
echo "  model-service"
echo "  pytest services/model/tests/test_fastapi_imports.py -q"
echo ""
echo -e "${BLUE}Manual venv activation remains available${NC}"
echo "  source .venv/bin/activate"
echo ""
echo -e "${BLUE}Optional uv commands without re-sync${NC}"
echo "  uv run --no-sync model-service"
echo "  uv run --no-sync pytest services/model/tests -q"
