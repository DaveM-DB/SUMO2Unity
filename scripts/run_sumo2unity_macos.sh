#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NATIVE_BINARY="${ROOT_DIR}/dist/Sumo2UnityTool-macos-arm64"
VENV_PYTHON="${ROOT_DIR}/.venv-macos/bin/python"

if [[ -z "${SUMO_HOME:-}" ]]; then
  if command -v sumo >/dev/null 2>&1; then
    SUMO_BIN="$(command -v sumo)"
    export SUMO_HOME="$(cd "$(dirname "${SUMO_BIN}")/.." && pwd)"
  elif [[ -d "${HOME}/Sumo/sumo" ]]; then
    export SUMO_HOME="${HOME}/Sumo/sumo"
    export PATH="${SUMO_HOME}/bin:${PATH}"
  fi
fi

if [[ -z "${SUMO_HOME:-}" ]]; then
  echo "SUMO_HOME is not set and SUMO was not found."
  echo "Install SUMO, then export SUMO_HOME or add SUMO's bin directory to PATH."
  exit 1
fi

export PYTHONPATH="${SUMO_HOME}/tools:${PYTHONPATH:-}"

if [[ -x "${NATIVE_BINARY}" && "${SUMO2UNITY_USE_SOURCE:-0}" != "1" ]]; then
  exec "${NATIVE_BINARY}" --sumocfg "${ROOT_DIR}/Scenario/Sumo2Unity.sumocfg" --results-dir "${ROOT_DIR}/Results" "$@"
fi

if [[ ! -x "${VENV_PYTHON}" ]]; then
  echo "Missing ${VENV_PYTHON}."
  echo "Run: make macos-setup"
  exit 1
fi

exec "${VENV_PYTHON}" "${ROOT_DIR}/tools/sumo2unity_tool.py" --sumocfg "${ROOT_DIR}/Scenario/Sumo2Unity.sumocfg" --results-dir "${ROOT_DIR}/Results" "$@"
