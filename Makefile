# FALCON developer task runner.
# Common commands for setup, testing, linting, and running the agents.

VENV := .venv
PY := $(VENV)/bin/python
PIP := $(VENV)/bin/pip

.PHONY: help install test lint format run-commander run-follower clean

help:
	@echo "FALCON make targets:"
	@echo "  make install        Create a virtualenv and install all dependencies"
	@echo "  make test           Run the pytest suite"
	@echo "  make lint           Check formatting (black) and lint (flake8)"
	@echo "  make format          Auto-format the code with black"
	@echo "  make run-commander  Run the Commander agent (available from Phase 2)"
	@echo "  make run-follower   Run the Follower agent (available from Phase 2)"
	@echo "  make clean          Remove the virtualenv and caches"

install:
	python3 -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements-dev.txt

test:
	$(PY) -m pytest tests/ -v

lint:
	$(PY) -m black --check .
	$(PY) -m flake8 .

format:
	$(PY) -m black .

# The agent entry points land in Phase 2 (FALCON-022 / FALCON-023).
# These targets are wired now so the workflow is stable from day one.
run-commander:
	$(PY) -m src.agents.commander

run-follower:
	$(PY) -m src.agents.follower

clean:
	rm -rf $(VENV) .pytest_cache
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
