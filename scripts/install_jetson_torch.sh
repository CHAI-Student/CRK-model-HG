#!/usr/bin/env bash

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
VENV_PATH="${VENV_PATH:-${PROJECT_ROOT}/.venv}"
PYTHON_BIN="${PYTHON_BIN:-python3.10}"
JETSON_TORCH_INDEX_URL="${JETSON_TORCH_INDEX_URL:-https://pypi.jetson-ai-lab.io/jp6/cu126}"
JETSON_TORCH_VERSION="${JETSON_TORCH_VERSION:-2.8.0}"
JETSON_TORCHVISION_VERSION="${JETSON_TORCHVISION_VERSION:-0.23.0}"
JETSON_TORCH_WHEEL_URL="${JETSON_TORCH_WHEEL_URL:-https://pypi.jetson-ai-lab.io/jp6/cu126/+f/62a/1beee9f2f1470/torch-2.8.0-cp310-cp310-linux_aarch64.whl}"
JETSON_TORCHVISION_WHEEL_URL="${JETSON_TORCHVISION_WHEEL_URL:-https://pypi.jetson-ai-lab.io/jp6/cu126/+f/5e2/327c6ee4f97cf/torchvision-0.23.0-cp310-cp310-linux_aarch64.whl}"
INSTALL_CUDSS_IF_NEEDED="${INSTALL_CUDSS_IF_NEEDED:-1}"

print_ok() {
    echo -e "${GREEN}OK${NC} $1"
}

print_warn() {
    echo -e "${YELLOW}WARN${NC} $1"
}

print_err() {
    echo -e "${RED}ERROR${NC} $1"
}

print_note() {
    echo -e "${BLUE}$1${NC}"
}

download_from_index() {
    python -m pip download \
        --no-deps \
        --dest "${WHEEL_DIR}" \
        --index-url "${JETSON_TORCH_INDEX_URL}" \
        "torch==${JETSON_TORCH_VERSION}" \
        "torchvision==${JETSON_TORCHVISION_VERSION}"
}

download_from_direct_urls() {
    python -m pip download \
        --no-deps \
        --dest "${WHEEL_DIR}" \
        "${JETSON_TORCH_WHEEL_URL}" \
        "${JETSON_TORCHVISION_WHEEL_URL}"
}

install_cudss_if_needed() {
    local import_log
    import_log="$(mktemp)"

    if python - <<'PY' >"${import_log}" 2>&1
import torch

print(f"PyTorch: {torch.__version__}")
print(f"CUDA version: {torch.version.cuda}")
print(f"CUDA available: {torch.cuda.is_available()}")

if torch.version.cuda is None:
    raise SystemExit("Installed torch is still CPU-only. Verify the Jetson wheel source.")

if not torch.cuda.is_available():
    raise SystemExit("Installed torch can import, but CUDA is still unavailable.")
PY
    then
        cat "${import_log}"
        rm -f "${import_log}"
        return 0
    fi

    if [[ "${INSTALL_CUDSS_IF_NEEDED}" == "1" ]] && grep -q "libcudss.so" "${import_log}"; then
        print_warn "PyTorch import requires libcudss.so. Installing nvidia-cudss-cu12 into the venv."
        python -m pip install --no-cache-dir --force-reinstall nvidia-cudss-cu12

        if python - <<'PY'
import torch

print(f"PyTorch: {torch.__version__}")
print(f"CUDA version: {torch.version.cuda}")
print(f"CUDA available: {torch.cuda.is_available()}")

if torch.version.cuda is None:
    raise SystemExit("Installed torch is still CPU-only. Verify the Jetson wheel source.")

if not torch.cuda.is_available():
    raise SystemExit("Installed torch can import, but CUDA is still unavailable.")
PY
        then
            rm -f "${import_log}"
            return 0
        fi
    fi

    cat "${import_log}"
    rm -f "${import_log}"
    return 1
}

if [[ ! -f /etc/nv_tegra_release ]]; then
    print_err "This helper must be run on a Jetson device."
    exit 1
fi

if [[ ! -d "${VENV_PATH}" ]]; then
    print_err "Virtual environment not found: ${VENV_PATH}"
    print_err "Create it first with: uv venv --system-site-packages --python ${PYTHON_BIN} ${VENV_PATH}"
    exit 1
fi

source "${VENV_PATH}/bin/activate"
# The wheel import checks below need the same Jetson runtime linker paths that
# the service process uses at startup.
. "${PROJECT_ROOT}/scripts/jetson_env.sh"

if ! command -v python >/dev/null 2>&1; then
    print_err "Python is not available after activating ${VENV_PATH}."
    exit 1
fi

if ! ldconfig -p | grep -q "libcusparseLt"; then
    print_warn "libcusparseLt was not found in the system linker cache."
    print_warn "NVIDIA's Jetson PyTorch docs require cuSPARSELT on newer builds."
    print_note "If torch import later fails, install cuSPARSELT first:"
    print_note "  wget raw.githubusercontent.com/pytorch/pytorch/5c6af2b583709f6176898c017424dc9981023c28/.ci/docker/common/install_cusparselt.sh"
    print_note "  export CUDA_VERSION=12.6"
    print_note "  bash ./install_cusparselt.sh"
fi

print_warn "Removing any existing torch, torchvision, and torchaudio packages from the venv"
python -m pip uninstall -y torch torchvision torchaudio >/dev/null 2>&1 || true

WHEEL_DIR="$(mktemp -d)"
DOWNLOAD_LOG="$(mktemp)"
trap 'rm -rf "${WHEEL_DIR}"; rm -f "${DOWNLOAD_LOG}"' EXIT

print_warn "Downloading Jetson-compatible PyTorch wheels from ${JETSON_TORCH_INDEX_URL}"
if ! download_from_index >"${DOWNLOAD_LOG}" 2>&1; then
    cat "${DOWNLOAD_LOG}"

    if grep -Eq "Name or service not known|Temporary failure in name resolution|Failed to establish a new connection" "${DOWNLOAD_LOG}"; then
        print_err "DNS resolution failed while contacting ${JETSON_TORCH_INDEX_URL}"
        print_note "Check Jetson networking first:"
        print_note "  ping -c 1 8.8.8.8"
        print_note "  getent hosts pypi.jetson-ai-lab.io"
    else
        print_warn "Index download failed. Falling back to direct wheel URLs."
    fi

    print_warn "Trying direct wheel URLs"
    if ! download_from_direct_urls; then
        print_err "Direct wheel download also failed."
        exit 1
    fi
else
    cat "${DOWNLOAD_LOG}"
fi

print_warn "Installing Jetson-compatible PyTorch wheels into ${VENV_PATH}"
python -m pip install --no-cache-dir --force-reinstall \
    "${WHEEL_DIR}"/torch-*.whl \
    "${WHEEL_DIR}"/torchvision-*.whl

if ! install_cudss_if_needed; then
    print_err "Jetson torch installation completed, but import/initialization still failed."
    exit 1
fi

print_ok "Jetson-compatible PyTorch is installed and CUDA is visible"
