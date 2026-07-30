#!/usr/bin/env bash

set -Eeuo pipefail

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

print_note() {
    echo -e "${BLUE}$1${NC}"
}

# set -e로 종료될 때 조용히 사라지지 않게 한다. 2026-07-30 회귀: 5단계가 단계
# 제목만 찍고 종료됐는데(① 실패한 파이프라인을 변수에 대입 ② 미정의 함수 호출)
# 아무 메시지가 없어 원인 파악이 늦었다.
trap 'rc=$?; print_err "setup_jetson.sh aborted at line ${LINENO} (exit ${rc})"; exit ${rc}' ERR

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

    # 이름 변경(model-service-hg → model-service) 전에 설치된 훅도 잡아야
    # 중복 삽입되지 않는다 — 접두 이름이 아니라 'Jetson runtime hook'으로 검사.
    if grep -Fq 'Jetson runtime hook' "${activate_path}"; then
        print_ok "Jetson activation hook already installed"
        return
    fi

    printf '%s' "${hook_block}" >> "${activate_path}"
    print_ok "Installed Jetson activation hook into .venv/bin/activate"
}

# Jetson torch는 NumPy 1.x로 빌드돼 있어 2.x가 들어오면 import가 깨진다.
# 의존성 해석이 NumPy를 올릴 수 있는 지점(프로젝트 의존성 설치, Jetson 휠 설치)
# **뒤마다** 호출한다 — 한 번만 검사하면 이후 단계가 다시 올려놓는다
# (2026-07-30: torch 단계를 뒤로 옮기면서 이 가드가 앞에만 남아 numpy 2.2.6이 통과).
ensure_numpy1() {
    local version
    version="$(python -c 'import numpy; print(numpy.__version__)' 2>/dev/null || true)"

    if [[ -z "${version}" ]]; then
        print_err "NumPy is not importable inside the venv."
        exit 1
    fi

    if [[ "${version}" == 2.* ]]; then
        print_warn "NumPy ${version} detected. Reinstalling NumPy 1.x for Jetson compatibility."
        uv pip install --force-reinstall "numpy>=1.24.0,<2.0.0"
        version="$(python -c 'import numpy; print(numpy.__version__)' 2>/dev/null || true)"
        if [[ "${version}" == 2.* || -z "${version}" ]]; then
            print_err "Failed to pin NumPy 1.x (now: ${version:-import failed})."
            exit 1
        fi
    fi

    print_ok "NumPy ${version}"
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
        "polars>=0.20.0"

    # --no-deps 필수: 둘 다 torch를 의존성으로 선언한다. 의존성을 해석하게 두면
    # PyPI의 x86 기준 CUDA 빌드(cu130 등)를 venv에 끌어와 JetPack torch를 가린다
    # (2026-07-30 실기: 드라이버 12.6 vs torch cu130 → 기동 불가).
    uv pip install --no-deps \
        "ultralytics>=8.0.0,<9.0.0" \
        "ultralytics-thop>=2.0.18"

    # TensorRT engine export deps (scripts/convert_engine.sh). NumPy 핀과 한
    # 명령으로 설치해야 resolver가 NumPy를 2.x로 올리지 않는다 — 미리 안 깔면
    # 첫 `yolo export` 때 ultralytics auto-install이 onnx를 설치하며 NumPy 2를
    # 끌어와 Jetson torch("Downgrade to 'numpy<2'")를 깨뜨린다.
    uv pip install onnx onnxslim "numpy>=1.24.0,<2.0.0"
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

print_step "1/10" "Checking Jetson prerequisites"

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

print_step "2/10" "Checking uv"

if ! command -v uv >/dev/null 2>&1; then
    print_warn "uv not found. Installing to ~/.local/bin"
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="${HOME}/.local/bin:${HOME}/.cargo/bin:${PATH}"
else
    print_ok "uv detected: $(uv --version)"
fi

print_step "3/10" "Preparing virtual environment"

if [[ -d "${VENV_PATH}" && "${FORCE_RECREATE_VENV}" == "1" ]]; then
    print_warn "Removing existing .venv because FORCE_RECREATE_VENV=1"
    rm -rf "${VENV_PATH}"
fi

if [[ ! -d "${VENV_PATH}" ]]; then
    # --seed: venv 안에 pip를 심는다. 없으면 활성화 상태에서 `pip list`가 조용히
    # 시스템 pip로 떨어져 **venv 밖 패키지 목록**을 보여준다 — 2026-07-30 실기에서
    # "pip list는 torch 2.8.0인데 import는 2.13.0+cu130" 오진의 원인이었다.
    uv venv --seed --system-site-packages --python "${PYTHON_BIN}" "${VENV_PATH}"
    print_ok "Created .venv with system site packages (pip seeded)"
else
    print_ok "Reusing existing .venv"
fi

source "${VENV_PATH}/bin/activate"
# Load the runtime paths in the current shell as well, so the validation steps
# below exercise the same CUDA/TensorRT environment that normal runtime uses.
. "${PROJECT_ROOT}/scripts/jetson_env.sh"

# 순서 주의: 의존성 설치가 **먼저**다. 반대로 하면(구 순서) torch 검증이 통과한
# 뒤에 의존성 설치가 venv로 PyPI torch를 끌어와 검증 결과를 무효로 만든다 —
# 2026-07-30 실기 사고: 시스템 torch로 검증을 통과한 뒤 ultralytics-thop의 의존성
# 해석이 torch 2.13.0+cu130(CUDA 13 빌드)을 venv에 설치해, 드라이버 12.6에서
# "Nvidia driver ... too old (found version 12060)"로 기동 불가.
print_step "4/10" "Installing project dependencies"

install_project_packages
print_ok "Project dependencies installed"

ensure_numpy1

print_step "5/10" "Ensuring Jetson-compatible torch"

# torch가 설치돼 있지 않아도 이 함수는 성공해야 한다 — 실패한 파이프라인을
# 변수에 대입하면 set -euo pipefail이 그 자리에서 스크립트를 죽인다.
torch_report() {
    python - <<'PY' 2>/dev/null || true
import torch

print(f"{torch.__version__} cuda={torch.version.cuda} available={torch.cuda.is_available()}")
print(torch.__file__)
PY
}

torch_cuda_ok() {
    python -c "import torch; assert torch.cuda.is_available()" >/dev/null 2>&1
}

TORCH_INFO="$(torch_report)"

if torch_cuda_ok; then
    print_ok "PyTorch can see CUDA: $(printf '%s\n' "${TORCH_INFO}" | head -1)"
    print_note "  imported from $(printf '%s\n' "${TORCH_INFO}" | tail -1)"
else
    print_warn "PyTorch cannot see CUDA. Current import: $(printf '%s\n' "${TORCH_INFO}" | head -1)"
    if [[ -z "${TORCH_INFO}" ]]; then
        print_warn "  torch is not importable at all — the Jetson wheel installer will run."
    fi

    # venv 안의 torch가 정상 동작하는 외부 torch(JetPack dist-packages 또는
    # 사용자 사이트)를 가리고 있을 수 있다 — 의존성 해석이 PyPI 휠을 끌어온
    # 경우가 그렇다. 휠 재다운로드 전에 먼저 venv 로컬 torch를 걷어내고
    # 외부 torch로 되돌려 본다 (네트워크 불필요, 2026-07-30 실기 복구 절차).
    VENV_SITE="$(python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])' 2>/dev/null || true)"
    if [[ -n "${VENV_SITE}" && -d "${VENV_SITE}/torch" ]]; then
        print_warn "torch exists inside the venv (${VENV_SITE}/torch) and shadows the outer install. Removing it."
        uv pip uninstall torch torchvision torchaudio >/dev/null 2>&1 || true
        # torch를 끌어온 유일한 패키지를 의존성 없이 복구
        uv pip install --no-deps "ultralytics-thop>=2.0.18" >/dev/null 2>&1 || true
    fi

    if torch_cuda_ok; then
        TORCH_INFO="$(torch_report)"
        print_ok "Outer PyTorch restored: $(printf '%s\n' "${TORCH_INFO}" | head -1)"
        print_note "  imported from $(printf '%s\n' "${TORCH_INFO}" | tail -1)"
    elif [[ "${INSTALL_JETSON_TORCH}" != "1" ]]; then
        print_err "PyTorch cannot see CUDA and INSTALL_JETSON_TORCH=0."
        exit 1
    else
        "${PROJECT_ROOT}/scripts/install_jetson_torch.sh"
    fi
fi

# 휠 설치가 NumPy를 2.x로 올렸을 수 있다 — torch 단계 뒤에 반드시 재검사한다.
ensure_numpy1

print_step "6/10" "Preparing runtime configuration"

if [[ ! -f "${PROJECT_ROOT}/.env" ]]; then
    cp "${PROJECT_ROOT}/.env.example" "${PROJECT_ROOT}/.env"
    print_warn "Created .env from .env.example. Review MODEL__VISION__YOLO_MODEL_PATH before running."
else
    print_ok ".env already present"
fi

# models/는 .gitignore 대상이라 새 클론에는 없다. find가 없는 디렉터리에서 1을
# 반환하고 pipefail이 그것을 대입에 실어 나르면 스크립트가 여기서 종료된다.
if [[ -d "${PROJECT_ROOT}/models" ]]; then
    ENGINE_COUNT="$(find "${PROJECT_ROOT}/models" -maxdepth 1 -name '*.engine' | wc -l | tr -d ' ' || true)"
else
    ENGINE_COUNT=0
fi
if [[ "${ENGINE_COUNT}" == "0" ]]; then
    print_warn "No .engine file found under models/. Update .env after copying your engine file."
else
    print_ok "Detected ${ENGINE_COUNT} engine file(s) under models/"
    find "${PROJECT_ROOT}/models" -maxdepth 1 -name '*.engine' -print
fi

print_step "7/10" "Verifying imports inside the venv"

python <<'PY'
import os

from crk_model.core.config import Settings

settings = Settings.from_env()
engine = os.environ.get(
    "MODEL__VISION__YOLO_MODEL_PATH", "models/set9_doorfas_0323_imbal.engine"
)
print(f"Resolved engine path: {engine}")
print(f"Cabinet type: {settings.cabinet_type} / camera layout: {settings.camera_layout}")
print(f"Batch size: {settings.batch_size} / prefetch: {settings.prefetch_depth}")
PY

python <<'PY'
import fastapi
import numpy
import torch

print(f"FastAPI: {fastapi.__version__}")
print(f"NumPy: {numpy.__version__}")
print(f"PyTorch: {torch.__version__} (built for CUDA {torch.version.cuda})")
# 어느 경로의 torch가 import됐는지 반드시 남긴다 — venv 안/JetPack dist-packages/
# 사용자 사이트(~/.local) 중 어디인지가 장애 진단의 첫 갈림길이다.
print(f"PyTorch origin: {torch.__file__}")
print(f"CUDA available: {torch.cuda.is_available()}")

if numpy.__version__.startswith("2."):
    raise SystemExit(
        f"NumPy {numpy.__version__} is installed, but Jetson torch is built for 1.x.\n"
        "  Something re-resolved NumPy after the pin. Fix with:\n"
        "    uv pip install --force-reinstall 'numpy>=1.24.0,<2.0.0'"
    )

if torch.version.cuda is None:
    raise SystemExit("PyTorch is still CPU-only. Verify the Jetson wheel source.")

if not torch.cuda.is_available():
    # 대표적 원인: torch의 빌드 CUDA가 드라이버보다 새것 (PyPI 휠이 섞였을 때).
    # 그 경우 torch가 "Nvidia driver ... too old (found version NNNNN)"을 던지는데,
    # NNNNN은 드라이버 CUDA(예: 12060 = 12.6)이지 드라이버 자체의 문제가 아니다.
    raise SystemExit(
        "PyTorch imports but CUDA is unavailable.\n"
        f"  built for CUDA {torch.version.cuda}, imported from {torch.__file__}\n"
        "  If the build CUDA is newer than this Jetson's driver, a PyPI wheel got\n"
        "  mixed in. Remove it from the venv and fall back to the JetPack build:\n"
        "    uv pip uninstall torch torchvision torchaudio\n"
        "    uv pip install --no-deps 'ultralytics-thop>=2.0.18'\n"
        "  Note: inside a uv-created venv, `pip list` may report the *system*\n"
        "  packages; trust `torch.__file__` above instead."
    )
PY

print_step "8/10" "Verifying entry points"

if command -v model-service >/dev/null 2>&1; then
    print_ok "model-service entry point is available"
else
    print_err "model-service entry point is not available after install"
    exit 1
fi

for cli in label-session analyze-sessions render-session; do
    if command -v "${cli}" >/dev/null 2>&1; then
        print_ok "${cli} entry point is available"
    else
        print_warn "${cli} entry point is missing (진단 CLI — 서비스 기동에는 무관)"
    fi
done

if pytest --version >/dev/null 2>&1; then
    print_ok "pytest is available"
else
    print_err "pytest is not available after install"
    exit 1
fi

print_step "9/10" "Installing activation hook"

install_activation_hook

print_step "10/10" "Done"

echo -e "${BLUE}Recommended runtime commands${NC}"
echo "  source .venv/bin/activate"
echo "  cp refrg.env.example .env      # 냉장 기기 (냉동은 freezer.env.example)"
echo "  model-service"
echo ""
echo -e "${BLUE}Verification${NC}"
echo "  curl -s http://localhost:8002/api/health"
echo "  pytest -q"
echo ""
echo -e "${BLUE}Optional uv commands without re-sync${NC}"
echo "  uv run --no-sync model-service"
echo "  uv run --no-sync pytest -q"
