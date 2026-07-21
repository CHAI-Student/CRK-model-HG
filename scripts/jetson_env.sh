#!/usr/bin/env bash

# Source this file from a Jetson shell to restore the CUDA/TensorRT runtime
# paths that the model service expects. setup_jetson.sh installs an activation
# hook so this runs automatically after `source .venv/bin/activate`.

_model_service_prepend_path() {
    local var_name="$1"
    local candidate="$2"

    if [[ -z "${candidate}" ]]; then
        return
    fi

    if [[ ! -d "${candidate}" && ! -f "${candidate}" ]]; then
        return
    fi

    local current_value="${!var_name:-}"
    case ":${current_value}:" in
        *":${candidate}:"*) ;;
        *)
            if [[ -n "${current_value}" ]]; then
                export "${var_name}=${candidate}:${current_value}"
            else
                export "${var_name}=${candidate}"
            fi
            ;;
    esac
}

_model_service_source_jetson_env() {
    if [[ ! -f /etc/nv_tegra_release ]]; then
        return 0
    fi

    local script_dir project_root python_version site_dir
    script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    project_root="$(dirname "${script_dir}")"

    export MODEL_SERVICE_JETSON_ENV_READY=1
    export CUDA_HOME="${CUDA_HOME:-/usr/local/cuda}"
    export CUDA_PATH="${CUDA_PATH:-/usr/local/cuda}"

    if [[ -n "${VIRTUAL_ENV:-}" ]]; then
        _model_service_prepend_path PATH "${VIRTUAL_ENV}/bin"
        _model_service_prepend_path LD_LIBRARY_PATH "${VIRTUAL_ENV}/lib"
    fi

    _model_service_prepend_path PATH "${HOME}/.local/bin"
    _model_service_prepend_path PATH "${HOME}/.cargo/bin"
    _model_service_prepend_path PATH "/usr/local/cuda/bin"

    _model_service_prepend_path LD_LIBRARY_PATH "/usr/local/cuda/lib64"
    _model_service_prepend_path LD_LIBRARY_PATH "/usr/local/cuda/compat"
    _model_service_prepend_path LD_LIBRARY_PATH "/usr/lib/aarch64-linux-gnu"
    _model_service_prepend_path LD_LIBRARY_PATH "/lib/aarch64-linux-gnu"
    _model_service_prepend_path LD_LIBRARY_PATH "/usr/lib/aarch64-linux-gnu/tegra"
    _model_service_prepend_path LD_LIBRARY_PATH "/usr/lib/aarch64-linux-gnu/nvidia"
    _model_service_prepend_path LD_LIBRARY_PATH "/usr/lib/aarch64-linux-gnu/nvidia/current"

    python_version="$(
        "${VIRTUAL_ENV:-${project_root}/.venv}/bin/python" -c \
            'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' \
            2>/dev/null || true
    )"

    if [[ -n "${python_version}" ]]; then
        # Search both the venv and common system package roots because Jetson
        # often mixes system-provided TensorRT bindings with wheel-provided CUDA
        # libraries under `site-packages/nvidia/*/lib`.
        for site_dir in \
            "${VIRTUAL_ENV:-${project_root}/.venv}/lib/python${python_version}/site-packages" \
            "/usr/local/lib/python${python_version}/dist-packages" \
            "/usr/lib/python${python_version}/dist-packages"; do
            _model_service_prepend_path LD_LIBRARY_PATH "${site_dir}/tensorrt_libs"
            if [[ -d "${site_dir}/nvidia" ]]; then
                while IFS= read -r -d '' lib_dir; do
                    _model_service_prepend_path LD_LIBRARY_PATH "${lib_dir}"
                done < <(find "${site_dir}/nvidia" -mindepth 2 -maxdepth 2 -type d -name lib -print0 2>/dev/null)
            fi
        done
    fi
}

_model_service_source_jetson_env "$@"
