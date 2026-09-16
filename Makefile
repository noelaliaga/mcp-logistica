PYTHON ?= python3
VENV   ?= .venv
BIN    := $(VENV)/bin
DB     ?= data/wms.sqlite
WRITE_MODE ?= off
LOG    ?= logs/tool_calls.jsonl

.PHONY: install seed test lint typecheck format serve report check clean

install:  ## create .venv and install the package with dev tools (PYTHON=python3.12 to pick one)
	@$(PYTHON) -c 'import sys; v = sys.version.split()[0]; sys.exit(0 if sys.version_info >= (3, 11) else f"Python >= 3.11 required, $(PYTHON) is {v}. Try: make install PYTHON=python3.12")'
	$(PYTHON) -m venv $(VENV)
	$(BIN)/python -m pip install --upgrade pip
	$(BIN)/python -m pip install -e '.[dev]'

seed:  ## (re)create the synthetic demo database at $(DB)
	$(BIN)/wms-seed --db $(DB) --force

test:
	$(BIN)/pytest

lint:  ## ruff lint + format check + mypy strict
	$(BIN)/ruff check .
	$(BIN)/ruff format --check .
	$(BIN)/mypy

typecheck:
	$(BIN)/mypy

format:
	$(BIN)/ruff format .
	$(BIN)/ruff check --fix .

serve:  ## run the MCP server on stdio (WRITE_MODE=off|dry_run|on)
	WMS_DB_PATH=$(DB) WMS_WRITE_MODE=$(WRITE_MODE) $(BIN)/wms-mcp

report:  ## summarize a tool-call log written with WMS_TOOL_LOG
	$(BIN)/wms-report $(LOG)

check: lint test

clean:
	rm -rf .mypy_cache .pytest_cache .ruff_cache build dist src/*.egg-info
