# Exported as Makefile in the public Community repository.
# The one-line installer creates .env and seeds the project. These targets operate
# an existing install from its checkout; they never print credentials.

SHELL := /bin/sh
# A fresh installer session cannot yet use the docker group added for future logins.
# Match the installer: use the socket directly when possible, then noninteractive sudo.
DOCKER := $(shell if docker info >/dev/null 2>&1; then printf 'docker'; elif command -v sudo >/dev/null 2>&1 && sudo -n docker info >/dev/null 2>&1; then printf 'sudo -n docker'; fi)
COMPOSE := $(DOCKER) compose -f scripts/install/community-compose.yml --env-file .env

.PHONY: help dev seed status check stop metatron mcp-setup mcp docker-ready e2e e2e-selftest eval-decisions

docker-ready:
	@test -n '$(DOCKER)' || { printf '%s\n' 'Docker is unavailable to this user. Start Docker or grant noninteractive sudo access to Docker; a new login also activates docker-group access.' >&2; exit 1; }

help:
	@printf '%s\n' 'BOSS Community' \
	  '  make dev     Start or rebuild the local stack after installation' \
	  '  make seed    Rerun the idempotent project seed' \
	  '  make status  Show service status' \
	  '  make check   Check installation health' \
	  '  make metatron  Open the local BOSS terminal in the API container (or: boss metatron)' \
	  '  make mcp-setup  Give an agent client its own key for one project' \
	  '  make mcp    Start a scoped MCP stdio session for an agent client' \
	  '  make stop    Stop services and keep data' \
	  '  make e2e     Boot a disposable authed stack and drive every template (tests/e2e)' \
	  'Install first: see docs/community/QUICKSTART.md for the reviewed-commit command'

dev: .env docker-ready
	$(COMPOSE) --profile web up -d --build

seed: .env docker-ready
	$(COMPOSE) exec -T -u boss api python -c 'from boss_engine.bootstrap import seed; seed()'

status: .env docker-ready
	$(COMPOSE) --profile web ps

check: .env
	./scripts/install/checkup.sh

# The terminal uses the host tools' key (BOSS_HOST_API_KEY, issued once by boss identity
# host-key), passed by name so it never appears on a command line; the shared key is retired.
metatron: .env docker-ready
	@key="$$(sed -n 's/^BOSS_HOST_API_KEY=//p' .env | head -n 1)"; \
	if [ -z "$$key" ]; then bash scripts/install/boss.sh --dir "$(CURDIR)" identity host-key >/dev/null 2>&1 || true; \
	  key="$$(sed -n 's/^BOSS_HOST_API_KEY=//p' .env | head -n 1)"; fi; \
	if [ -n "$$key" ]; then export BOSS_API_KEY="$$key"; set -- -e BOSS_API_KEY; else set --; fi; \
	$(COMPOSE) exec -u boss "$$@" api sh -c 'BOSS_PROJECT_ID="$${BOSS_DEFAULT_PROJECT_ID:-boss-dogfood}" python scripts/metatron_tui.py'

# BOSS_PROJECT_ID=another-project selects an accessible project. Setup asks the host
# shell for a service account pinned to that project and keeps its key in .env as
# BOSS_MCP_API_KEY; the MCP session receives only that key.
mcp-setup: .env docker-ready
	python3 scripts/install/mcp_access.py setup --restart-api

mcp: .env
	@python3 scripts/install/mcp_access.py serve

stop: .env docker-ready
	$(COMPOSE) --profile web stop

.env:
	@printf '%s\n' 'BOSS is not installed in this checkout (no .env here).' \
	  'If BOSS runs on a server, run this there, in its install directory:' \
	  '  ssh -t user@your-server "cd ~/boss && make metatron"' \
	  'To install BOSS on this machine instead: ./install.sh --dir "$$PWD"' >&2
	@exit 1

# THE END-TO-END HARNESS (tests/e2e/README.md), from the harness stream. A disposable authed
# stack with scripted models and fakes; drives every template and example and walks the
# console. It needs no install here (no .env): it builds and tears down its own stack.
#   make e2e                      everything, then tear down
#   make e2e E2E_ARGS="--only fast_fix --no-web --keep"
#   make e2e-selftest             the harness's own fast tests
e2e:
	scripts/e2e.sh all $(E2E_ARGS)

e2e-selftest:
	scripts/e2e.sh selftest

# Keyword decision eval only. No .env, no docker, no model: model rows stay unmeasured.
PYTHON ?= python3
eval-decisions:
	PYTHONPATH=engine:. $(PYTHON) evals/task_rigor/run.py
