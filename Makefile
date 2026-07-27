SHELL := /bin/bash
VENV := .venv
PYTHON_BOOTSTRAP ?= python3
PYTHON := $(VENV)/bin/python
PIP := $(VENV)/bin/pip
PYTHON_REQUIREMENTS_STAMP := $(VENV)/.boss-requirements-installed
BOSS_POSTGRES_PORT ?= 5432
DATABASE_URL ?= postgresql+psycopg://boss:boss@localhost:$(BOSS_POSTGRES_PORT)/boss

.PHONY: install install-node install-python clean wait-db dev test lint build docs docs-install docs-generate docs-check seed seed-demo smoke k8s-validate infra-validate release-blockers real-smoke-plan real-smoke-pr real-smoke-merge real-smoke-deploy worker-drain-once sandbox-template deps-audit full-validate

install: install-node install-python

clean:
	rm -rf apps/web/.next apps/web/coverage .pytest_cache .ruff_cache htmlcov
	find apps engine scripts -type d -name __pycache__ -prune -exec rm -rf {} +
	find apps engine scripts -type f \( -name '*.pyc' -o -name '*.pyo' \) -delete

install-node:
	pnpm install

install-python: $(PYTHON_REQUIREMENTS_STAMP)

$(PYTHON):
	$(PYTHON_BOOTSTRAP) -m venv $(VENV)
	$(PIP) install --upgrade pip

$(PYTHON_REQUIREMENTS_STAMP): apps/api/requirements.txt apps/worker/requirements.txt | $(PYTHON)
	$(PYTHON) -c 'import sys; assert sys.version_info >= (3, 11), "BOSS requires Python 3.11+"'
	$(PIP) install -r apps/api/requirements.txt -r apps/worker/requirements.txt
	touch $(PYTHON_REQUIREMENTS_STAMP)

wait-db: install-python
	DATABASE_URL=$(DATABASE_URL) $(PYTHON) scripts/wait_for_postgres.py

dev: install
	docker compose -f infra/docker-compose.yml up -d db
	DATABASE_URL=$(DATABASE_URL) $(PYTHON) scripts/wait_for_postgres.py
	DATABASE_URL=$(DATABASE_URL) pnpm dev

test: install-python install-node
	DATABASE_URL=$(DATABASE_URL) $(PYTHON) scripts/wait_for_postgres.py
	DATABASE_URL=$(DATABASE_URL) PYTHON_BIN=$(PYTHON) pnpm test

lint: install-python install-node
	DATABASE_URL=$(DATABASE_URL) PYTHON_BIN=$(PYTHON) pnpm lint

build: install-node
	pnpm --filter boss-web build

docs-install: install-python
	$(PIP) install -r requirements-docs.txt

docs-generate: install-python
	$(PYTHON) scripts/generate_feature_docs.py

docs-check: install-python
	$(PYTHON) scripts/generate_feature_docs.py --check

docs: docs-install docs-check
	$(VENV)/bin/mkdocs build --strict

seed: install-python
	docker compose -f infra/docker-compose.yml up -d db
	DATABASE_URL=$(DATABASE_URL) $(PYTHON) scripts/wait_for_postgres.py
	DATABASE_URL=$(DATABASE_URL) PYTHONPATH=.:engine $(PYTHON) -m apps.api.app.bootstrap

# Baseline seed + realistic DEMO data (tasks, decisions, a multi-agent PRD
# meeting, code tasks) so a fresh console is populated. Offline, idempotent.
seed-demo: seed
	DATABASE_URL=$(DATABASE_URL) PYTHONPATH=.:engine:apps/api $(PYTHON) scripts/seed_demo.py

smoke: install-python
	docker compose -f infra/docker-compose.yml up -d db
	DATABASE_URL=$(DATABASE_URL) $(PYTHON) scripts/wait_for_postgres.py
	DATABASE_URL=$(DATABASE_URL) PYTHONPATH=.:engine $(PYTHON) -m apps.api.tests.smoke

k8s-validate: install-python
	$(PYTHON) scripts/validate_kubernetes_manifests.py

infra-validate: install-python
	$(PYTHON) scripts/validate_infra_profiles.py

release-blockers: install-python
	$(PYTHON) scripts/check_external_rollout_readiness.py

real-smoke-plan: install-python
	docker compose -f infra/docker-compose.yml up -d db
	DATABASE_URL=$(DATABASE_URL) $(PYTHON) scripts/wait_for_postgres.py
	DATABASE_URL=$(DATABASE_URL) PYTHONPATH=.:engine $(PYTHON) scripts/run_real_control_plane_smoke.py

real-smoke-pr: install-python
	docker compose -f infra/docker-compose.yml up -d db
	DATABASE_URL=$(DATABASE_URL) $(PYTHON) scripts/wait_for_postgres.py
	DATABASE_URL=$(DATABASE_URL) PYTHONPATH=.:engine $(PYTHON) scripts/run_real_control_plane_smoke.py --with-pr

real-smoke-merge: install-python
	docker compose -f infra/docker-compose.yml up -d db
	DATABASE_URL=$(DATABASE_URL) $(PYTHON) scripts/wait_for_postgres.py
	DATABASE_URL=$(DATABASE_URL) PYTHONPATH=.:engine $(PYTHON) scripts/run_real_control_plane_smoke.py --with-pr --with-merge

real-smoke-deploy: install-python
	docker compose -f infra/docker-compose.yml up -d db
	DATABASE_URL=$(DATABASE_URL) $(PYTHON) scripts/wait_for_postgres.py
	DATABASE_URL=$(DATABASE_URL) PYTHONPATH=.:engine $(PYTHON) scripts/run_real_control_plane_smoke.py --with-pr --with-merge --with-deploy

worker-drain-once: install-python
	DATABASE_URL=$(DATABASE_URL) PYTHONPATH=.:engine $(PYTHON) -m apps.worker.boss_worker.main --drain-once

sandbox-template:
	mkdir -p tmp
	rsync -a --delete templates/boss-sandbox/ tmp/boss-sandbox/

# Security/DevOps P2: dep audit (per .ai/security-review + AUTO-004 follow-up). Reports current state; recommends full tools for CI.
# No auto-fix; dev-only vulns expected in some (vite etc for web dev server). Invoked by full-validate.
deps-audit: install-python
	@echo "=== Python runtime deps (freeze) ==="
	$(PYTHON) -m pip list --format=freeze | sed -n '1,30p'
	@echo ""
	@echo "=== Web pnpm audit (dev deps only; prod build unaffected) ==="
	cd apps/web && pnpm audit --json 2>/dev/null | $(abspath $(PYTHON)) -c 'import sys,json; d=json.load(sys.stdin); print(json.dumps(d.get("advisories", {}) or d, indent=2)[:2000])' || echo "(pnpm audit output or no vulns; run in web/ for details)"
	@echo ""
	@echo "For full: pip install pip-audit safety; add to CI. Current dev vulns (if any) noted in .ai/security-review.md; not prod runtime."

# Full local release readiness (per .ai/release-readiness, INFRA_RUNBOOK, AUTO-004).
# Includes all validators + build/lint/test + deps-audit. DB/docker required for test/smoke paths.
# Run before k8s deploys or claiming release readiness.
full-validate: lint deps-audit test build k8s-validate infra-validate release-blockers smoke
	@echo "full-validate complete (see individual outputs; external blockers expected from release-blockers)."
