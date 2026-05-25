PYTHON ?= python3
VENV := .venv-macos
VENV_PYTHON := $(VENV)/bin/python
VENV_PIP := $(VENV)/bin/pip
PYINSTALLER_CONFIG_DIR := $(CURDIR)/.pyinstaller
SUMO_TOOLS := $(shell if [ -n "$$SUMO_HOME" ]; then printf "%s/tools" "$$SUMO_HOME"; elif command -v sumo >/dev/null 2>&1; then cd "$$(dirname "$$(command -v sumo)")/.." && printf "%s/tools" "$$(pwd)"; elif [ -d "$$HOME/Sumo/sumo/tools" ]; then printf "%s/Sumo/sumo/tools" "$$HOME"; fi)

.PHONY: macos-setup run-macos build-macos-arm64 clean-macos

macos-setup:
	$(PYTHON) -m venv $(VENV)
	$(VENV_PIP) install --upgrade pip
	$(VENV_PIP) install -r requirements-macos.txt

run-macos:
	./scripts/run_sumo2unity_macos.sh

build-macos-arm64:
	test "$$(uname -m)" = "arm64"
	test -n "$(SUMO_TOOLS)"
	PYINSTALLER_CONFIG_DIR="$(PYINSTALLER_CONFIG_DIR)" PYTHONPATH="$(SUMO_TOOLS)" $(VENV_PYTHON) -m PyInstaller --onefile --name Sumo2UnityTool-macos-arm64 --paths "$(SUMO_TOOLS)" tools/sumo2unity_tool.py

clean-macos:
	rm -rf .pyinstaller build dist Sumo2UnityTool-macos-arm64.spec
