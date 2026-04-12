PYTHONPATH := $(shell pwd)

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "}; {printf "%-12s %s\n", $$1, $$2}'

install: ## Install dependencies into a uv-managed venv
	uv sync

dev-install: ## Install dev dependencies too
	uv sync --all-groups

run: ## Run the kasten CLI (pass args after --, e.g. make run -- status)
	uv run kasten

sync: ## Incremental sync from notes.vcoeur.com
	uv run kasten sync

sync-full: ## Full export-based rebuild
	uv run kasten sync --full

test: ## Run pytest
	uv run pytest; RET=$$?; if [ $$RET -eq 5 ]; then exit 0; else exit $$RET; fi

coverage: ## Run pytest with line-coverage report
	uv run pytest --cov=app --cov-report=term-missing --cov-report=html

lint: ## Ruff lint + format check
	uv run ruff check .
	uv run ruff format --check .

format: ## Ruff auto-fix + format
	uv run ruff check --fix .
	uv run ruff format .

tool-install: ## Install kasten as a global editable command via uv tool (one-time)
	uv tool install --force --reinstall --editable .

tool-uninstall: ## Remove the global kasten command
	uv tool uninstall kasten-manager

.PHONY: help install dev-install run sync sync-full test coverage lint format tool-install
