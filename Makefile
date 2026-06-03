SHELL := /bin/zsh
VENV := .venv
PYTHON := $(VENV)/bin/python
PIP := $(VENV)/bin/pip
DATABASE_URL ?= postgresql+psycopg://boss:boss@localhost:5432/boss

.PHONY: install install-node install-python dev test lint seed smoke sandbox-template

install: install-node install-python

install-node:
	pnpm install

install-python: $(PYTHON)

$(PYTHON):
	python3 -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -r apps/api/requirements.txt -r apps/worker/requirements.txt

dev: install
	docker compose -f infra/docker-compose.yml up -d db
	DATABASE_URL=$(DATABASE_URL) pnpm dev

test: install-python install-node
	DATABASE_URL=$(DATABASE_URL) pnpm test

lint: install-python install-node
	DATABASE_URL=$(DATABASE_URL) pnpm lint

seed: install-python
	docker compose -f infra/docker-compose.yml up -d db
	DATABASE_URL=$(DATABASE_URL) $(PYTHON) -m apps.api.app.bootstrap

smoke: install-python
	docker compose -f infra/docker-compose.yml up -d db
	DATABASE_URL=$(DATABASE_URL) $(PYTHON) -m apps.api.tests.smoke

sandbox-template:
	mkdir -p tmp
	rsync -a --delete templates/boss-sandbox/ tmp/boss-sandbox/

