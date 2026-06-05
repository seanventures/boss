SHELL := /bin/zsh
VENV := .venv
PYTHON := $(VENV)/bin/python
PIP := $(VENV)/bin/pip
BOSS_POSTGRES_PORT ?= 5432
DATABASE_URL ?= postgresql+psycopg://boss:boss@localhost:$(BOSS_POSTGRES_PORT)/boss

.PHONY: install install-node install-python wait-db dev test lint build seed smoke k8s-validate infra-validate release-blockers real-smoke-plan real-smoke-pr real-smoke-merge real-smoke-deploy worker-drain-once sandbox-template

install: install-node install-python

install-node:
	pnpm install

install-python: $(PYTHON)

$(PYTHON):
	python3 -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -r apps/api/requirements.txt -r apps/worker/requirements.txt

wait-db: install-python
	DATABASE_URL=$(DATABASE_URL) $(PYTHON) scripts/wait_for_postgres.py

dev: install
	docker compose -f infra/docker-compose.yml up -d db
	DATABASE_URL=$(DATABASE_URL) $(PYTHON) scripts/wait_for_postgres.py
	DATABASE_URL=$(DATABASE_URL) pnpm dev

test: install-python install-node
	DATABASE_URL=$(DATABASE_URL) $(PYTHON) scripts/wait_for_postgres.py
	DATABASE_URL=$(DATABASE_URL) pnpm test

lint: install-python install-node
	DATABASE_URL=$(DATABASE_URL) pnpm lint

build: install-node
	pnpm --filter boss-web build

seed: install-python
	docker compose -f infra/docker-compose.yml up -d db
	DATABASE_URL=$(DATABASE_URL) $(PYTHON) scripts/wait_for_postgres.py
	DATABASE_URL=$(DATABASE_URL) $(PYTHON) -m apps.api.app.bootstrap

smoke: install-python
	docker compose -f infra/docker-compose.yml up -d db
	DATABASE_URL=$(DATABASE_URL) $(PYTHON) scripts/wait_for_postgres.py
	DATABASE_URL=$(DATABASE_URL) $(PYTHON) -m apps.api.tests.smoke

k8s-validate: install-python
	$(PYTHON) scripts/validate_kubernetes_manifests.py

infra-validate: install-python
	$(PYTHON) scripts/validate_infra_profiles.py

release-blockers: install-python
	$(PYTHON) scripts/check_external_rollout_readiness.py

real-smoke-plan: install-python
	docker compose -f infra/docker-compose.yml up -d db
	DATABASE_URL=$(DATABASE_URL) $(PYTHON) scripts/wait_for_postgres.py
	DATABASE_URL=$(DATABASE_URL) $(PYTHON) scripts/run_real_control_plane_smoke.py

real-smoke-pr: install-python
	docker compose -f infra/docker-compose.yml up -d db
	DATABASE_URL=$(DATABASE_URL) $(PYTHON) scripts/wait_for_postgres.py
	DATABASE_URL=$(DATABASE_URL) $(PYTHON) scripts/run_real_control_plane_smoke.py --with-pr

real-smoke-merge: install-python
	docker compose -f infra/docker-compose.yml up -d db
	DATABASE_URL=$(DATABASE_URL) $(PYTHON) scripts/wait_for_postgres.py
	DATABASE_URL=$(DATABASE_URL) $(PYTHON) scripts/run_real_control_plane_smoke.py --with-pr --with-merge

real-smoke-deploy: install-python
	docker compose -f infra/docker-compose.yml up -d db
	DATABASE_URL=$(DATABASE_URL) $(PYTHON) scripts/wait_for_postgres.py
	DATABASE_URL=$(DATABASE_URL) $(PYTHON) scripts/run_real_control_plane_smoke.py --with-pr --with-merge --with-deploy

worker-drain-once: install-python
	DATABASE_URL=$(DATABASE_URL) $(PYTHON) -m apps.worker.boss_worker.main --drain-once

sandbox-template:
	mkdir -p tmp
	rsync -a --delete templates/boss-sandbox/ tmp/boss-sandbox/
