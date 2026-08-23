UV ?= uv

.PHONY: install lint format format-check typecheck security test build check lock

install:
	$(UV) sync --locked

lint:
	$(UV) run --locked ruff check kairos_strategy tests

format:
	$(UV) run --locked ruff format kairos_strategy tests

format-check:
	$(UV) run --locked ruff format --check kairos_strategy tests

typecheck:
	$(UV) run --locked mypy kairos_strategy

security:
	$(UV) run --locked bandit -q -r kairos_strategy

test:
	$(UV) run --locked pytest -q --tb=short

build:
	$(UV) build --no-sources

check: lint format-check typecheck security test build

lock:
	$(UV) lock
