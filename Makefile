UV ?= uv
PYTHON ?= backend/.venv/bin/python
NPM ?= npm
CHECK_VERSIONS ?= $(PYTHON) packaging/tools/check_versions.py
VERIFY_SETUP ?= $(MAKE) --no-print-directory setup

.PHONY: setup lint typecheck test test-integration build-extension smoke verify-source

setup:
	$(UV) sync --project backend --frozen --extra dev --no-install-project
	cd extension && $(NPM) ci

lint:
	$(CHECK_VERSIONS)
	$(PYTHON) packaging/tools/license_inventory.py
	cd backend && ../$(PYTHON) -m ruff check src tests ../scripts
	cd backend && ../$(PYTHON) -m ruff format --check src tests ../scripts
	$(PYTHON) -m ruff check --config backend/pyproject.toml packaging/tools packaging/tests
	$(PYTHON) -m ruff format --check --config backend/pyproject.toml packaging/tools packaging/tests
	cd extension && $(NPM) run lint

typecheck:
	cd backend && ../$(PYTHON) -m mypy src/lvt
	cd extension && $(NPM) run typecheck

test:
	PYTHONPATH=backend/src $(PYTHON) -m pytest backend/tests/unit packaging/tests
	cd extension && $(NPM) test

test-integration:
	PYTHONPATH=backend/src $(PYTHON) -m pytest backend/tests/integration
	cd extension && $(NPM) run test:e2e

build-extension:
	cd extension && $(NPM) run build
	cd extension && node scripts/verify-dist.mjs

smoke:
	PYTHONPATH=backend/src $(PYTHON) -m pytest backend/tests/integration/test_api.py::test_batch_create_accepts_valid_and_rejects_invalid_urls
	cd extension && node scripts/verify-dist.mjs

verify-source:
	$(VERIFY_SETUP)
	$(MAKE) --no-print-directory lint
	$(MAKE) --no-print-directory typecheck
	$(MAKE) --no-print-directory test
	$(MAKE) --no-print-directory test-integration
	$(MAKE) --no-print-directory build-extension
	$(MAKE) --no-print-directory smoke
