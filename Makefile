SHELL := /bin/bash
VENV := .venv
PYTHON_BOOTSTRAP ?= python3
PYTHON := $(VENV)/bin/python
PIP := $(VENV)/bin/pip
PYTHON_REQUIREMENTS_STAMP := $(VENV)/.boss-requirements-installed
BOSS_POSTGRES_PORT ?= 5432
DATABASE_URL ?= postgresql+psycopg://boss:boss@localhost:$(BOSS_POSTGRES_PORT)/boss

.PHONY: install install-node install-python clean wait-db dev test test-go lint build docs docs-install docs-generate docs-check seed seed-demo smoke k8s-validate infra-validate release-blockers real-smoke-plan real-smoke-pr real-smoke-merge real-smoke-deploy worker-drain-once sandbox-template deps-audit deps-audit-strict full-validate qa-benchmark qa-benchmark-e2e qa-live qa-workflow-e2e qa-factory-e2e qa-harness qa-official-reports qa-system qa-working-software qa-metatron-captain public-flip-check openapi-live-qa-parity metatron onboard

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

# Private design-partner first commands. Prints paths only — never secret values.
onboard:
	@echo "BOSS design-partner onboarding (private repository)"
	@echo "Guide:     docs/operations/DESIGN_PARTNER_ONBOARDING.md"
	@echo "Engine:    engine/DESIGN_PARTNER_ONBOARDING.md"
	@echo "Community: docs/community/QUICKSTART.md"
	@echo
	@echo "Week-1 local path (do not cat or echo secrets):"
	@echo "  1. Request GitHub access from the BOSS owner (human gate)."
	@echo "  2. cp .env.example .env    # edit locally; never commit"
	@echo "  3. make install && make seed && make dev"
	@echo "  4. make smoke"
	@echo "  5. Optional control-plane gate: make qa-live"
	@echo "     (requires BOSS_API_KEY in the environment; this target does not print it)"
	@echo
	@echo "Community is self-hosted BYOK. Cloud is optional. Sandboxes/previews are not guaranteed."
	@echo "File friction as a GitHub issue on this private repo. Do not invent Plane credentials."

dev: install
	docker compose -f infra/docker-compose.yml up -d db
	DATABASE_URL=$(DATABASE_URL) $(PYTHON) scripts/wait_for_postgres.py
	DATABASE_URL=$(DATABASE_URL) pnpm dev

# Stdlib Metatron captain TUI. Reads BOSS_API_URL + BOSS_API_KEY + BOSS_PROJECT_ID.
# Never prints the API key. Does not require the venv (stdlib only).
metatron:
	@test -n "$$BOSS_API_KEY" || (echo "BOSS_API_KEY is required (never print the key)."; exit 2)
	$(PYTHON_BOOTSTRAP) scripts/metatron_tui.py

test: install-python install-node
	DATABASE_URL=$(DATABASE_URL) $(PYTHON) scripts/wait_for_postgres.py
	DATABASE_URL=$(DATABASE_URL) PYTHON_BIN=$(PYTHON) pnpm test

# Optional Go scaffolds (S2 gateway, S3 dispatcher). Not part of Community `make test`.
# Engine does not require Go. Binaries are not mounted on goldeneye.
test-go:
	cd go/tenant-gateway && go test ./...
	cd services/webhook-dispatcher && go test ./...

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

# Launch a real governed benchmark through the BOSS API and export its durable scorecard.
# Required: BENCHMARK=bugbox|hookwatch|approveit PROJECT_ID=<boss-project-id>
# Optional: BOSS_API_URL, BOSS_API_KEY, OWNER, REPO, QA_BENCHMARK_OUTPUT.
qa-benchmark: install-python
	@test -n "$(BENCHMARK)" || (echo "BENCHMARK is required (bugbox, hookwatch, or approveit)."; exit 2)
	@test -n "$(PROJECT_ID)" || (echo "PROJECT_ID is required."; exit 2)
	BOSS_PROJECT_ID="$(PROJECT_ID)" PYTHONPATH=.:engine $(PYTHON) scripts/run_qa_benchmark.py --benchmark "$(BENCHMARK)" $(if $(OWNER),--owner "$(OWNER)") $(if $(REPO),--repo "$(REPO)") $(if $(QA_BENCHMARK_OUTPUT),--output "$(QA_BENCHMARK_OUTPUT)")

# End-to-end configured-deployment canary: queue -> leased worker -> terminal report -> durable tuning ledger.
# Requires a running BOSS API/worker plus model, budget, sandbox, and repository configuration.
qa-benchmark-e2e: install-python
	@test -n "$(BENCHMARK)" || (echo "BENCHMARK is required (bugbox, hookwatch, or approveit)."; exit 2)
	@test -n "$(PROJECT_ID)" || (echo "PROJECT_ID is required."; exit 2)
	BOSS_PROJECT_ID="$(PROJECT_ID)" PYTHONPATH=.:engine $(PYTHON) scripts/run_qa_benchmark.py --benchmark "$(BENCHMARK)" --sweep-tuning $(if $(OWNER),--owner "$(OWNER)") $(if $(REPO),--repo "$(REPO)") $(if $(QA_BENCHMARK_OUTPUT),--output "$(QA_BENCHMARK_OUTPUT)")

# Live functional dogfood gate (Plane BOSS-351). Talks only to a running BOSS API.
# Required: BOSS_API_KEY. Optional: BOSS_API_URL (default https://boss-api.local.sean.network),
# BOSS_PROJECT_ID (default boss-dogfood), BOSS_LIVE_GATE_OUTPUT.
# Proves health/auth, provider catalog+accounts, budget quote, command→task,
# decisions inbox, deadletters; metatron is soft/optional. Exits non-zero on required fail.
# Does not require full factory success (no sandbox/PR/merge). Never prints the API key.
qa-live:
	@test -n "$$BOSS_API_KEY" || (echo "BOSS_API_KEY is required (never commit it)."; exit 2)
	$(PYTHON_BOOTSTRAP) scripts/qa/live_dogfood_gate.py \
		--api-url "$${BOSS_API_URL:-https://boss-api.local.sean.network}" \
		--api-key "$$BOSS_API_KEY" \
		--project-id "$${BOSS_PROJECT_ID:-boss-dogfood}" \
		$(if $(BOSS_LIVE_GATE_OUTPUT),--output "$(BOSS_LIVE_GATE_OUTPUT)") \
		$(if $(SKIP_METATRON),--skip-metatron) \
		$(if $(SKIP_COMMAND),--skip-command)

# Workflow vertical-slice E2E: health → command propose → tasks → budget status|quote → jobs.
# Live: BOSS_API_KEY required. Optional: BOSS_API_BASE (or BOSS_API_URL), PROJECT_ID (or BOSS_PROJECT_ID).
# Structure-only (no network): make qa-workflow-e2e DRY_RUN=1
# DoD mapping: scripts/qa/ga_dod_checklist.md (criterion 2 + 8).
qa-workflow-e2e:
ifeq ($(DRY_RUN),1)
	$(PYTHON_BOOTSTRAP) scripts/qa/workflow_e2e_gate.py --dry-run \
		--api-base "$${BOSS_API_BASE:-$${BOSS_API_URL:-https://boss-api.local.sean.network}}" \
		--project-id "$${PROJECT_ID:-$${BOSS_PROJECT_ID:-boss-dogfood}}" \
		$(if $(BOSS_WORKFLOW_GATE_OUTPUT),--output "$(BOSS_WORKFLOW_GATE_OUTPUT)")
else
	@test -n "$$BOSS_API_KEY" || (echo "BOSS_API_KEY is required (never commit it). Or: make qa-workflow-e2e DRY_RUN=1"; exit 2)
	$(PYTHON_BOOTSTRAP) scripts/qa/workflow_e2e_gate.py \
		--api-base "$${BOSS_API_BASE:-$${BOSS_API_URL:-https://boss-api.local.sean.network}}" \
		--api-key "$$BOSS_API_KEY" \
		--project-id "$${PROJECT_ID:-$${BOSS_PROJECT_ID:-boss-dogfood}}" \
		$(if $(BOSS_WORKFLOW_GATE_OUTPUT),--output "$(BOSS_WORKFLOW_GATE_OUTPUT)")
endif

# Factory surface E2E: health + factory/wip + code-tasks list (+ optional create).
# Dry-run also proves quality-loop module + scorecard v1.1 ui/ux; never POSTs a budgeted code.task.
# Without BOSS_API_KEY (or with DRY_RUN=1): structural dry-run only, exit 0 if contracts OK.
# Live read-only: BOSS_API_KEY (+ BOSS_API_BASE, PROJECT_ID).
# Optional no-spend fixture: CREATE_FIXTURE=1 (POST simulated=true if fixture_supported).
# Optional create (budgeted): CREATE_CODE_TASK=1 + BOSS_FACTORY_ALLOW_CREATE=1 +
#   BOSS_FACTORY_OWNER + BOSS_FACTORY_REPO. See scripts/qa/factory_e2e_gate.py docstring.
# DoD mapping: scripts/qa/ga_dod_checklist.md (criteria 3 + 8).
qa-factory-e2e:
ifeq ($(DRY_RUN),1)
	$(PYTHON_BOOTSTRAP) scripts/qa/factory_e2e_gate.py --dry-run \
		--api-base "$${BOSS_API_BASE:-$${BOSS_API_URL:-https://boss-api.local.sean.network}}" \
		--project-id "$${PROJECT_ID:-$${BOSS_PROJECT_ID:-boss-dogfood}}" \
		$(if $(BOSS_FACTORY_GATE_OUTPUT),--output "$(BOSS_FACTORY_GATE_OUTPUT)")
else
	# BOSS_API_KEY is read from the environment by the script (never echoed).
	# Without a key the script auto dry-runs structural checks. CREATE_CODE_TASK=1
	# also requires BOSS_FACTORY_ALLOW_CREATE=1 + owner/repo env vars.
	# CREATE_FIXTURE=1 is the no-spend path (mutually exclusive with CREATE_CODE_TASK).
	$(PYTHON_BOOTSTRAP) scripts/qa/factory_e2e_gate.py \
		--api-base "$${BOSS_API_BASE:-$${BOSS_API_URL:-https://boss-api.local.sean.network}}" \
		--project-id "$${PROJECT_ID:-$${BOSS_PROJECT_ID:-boss-dogfood}}" \
		$(if $(filter 1,$(CREATE_FIXTURE)),--create-fixture,) \
		$(if $(filter 1,$(CREATE_CODE_TASK)),--create-code-task,) \
		$(if $(BOSS_FACTORY_GATE_OUTPUT),--output "$(BOSS_FACTORY_GATE_OUTPUT)")
endif

# Unified E2E harness: settings/features + 3 canary apps.
# DRY_RUN=1 structure only.
# CANARY_MODE=catalog|reuse|fixture|launch (launch needs BOSS_QA_ALLOW_CANARIES=1)
qa-harness:
ifeq ($(DRY_RUN),1)
	$(PYTHON_BOOTSTRAP) scripts/qa/harness.py --dry-run \
		--api-url "$${BOSS_API_BASE:-$${BOSS_API_URL:-https://boss-api.local.sean.network}}" \
		--project-id "$${PROJECT_ID:-$${BOSS_PROJECT_ID:-boss-dogfood}}" \
		--canary-mode "$(or $(CANARY_MODE),catalog)" \
		$(if $(BOSS_HARNESS_OUTPUT),--output "$(BOSS_HARNESS_OUTPUT)")
else
	@test -n "$$BOSS_API_KEY" || (echo "BOSS_API_KEY is required (never commit it). Or: make qa-harness DRY_RUN=1"; exit 2)
	$(PYTHON_BOOTSTRAP) scripts/qa/harness.py \
		--api-url "$${BOSS_API_BASE:-$${BOSS_API_URL:-https://boss-api.local.sean.network}}" \
		--project-id "$${PROJECT_ID:-$${BOSS_PROJECT_ID:-boss-dogfood}}" \
		--canary-mode "$(or $(CANARY_MODE),catalog)" \
		$(if $(BOSS_HARNESS_OUTPUT),--output "$(BOSS_HARNESS_OUTPUT)")
endif

# Official QA /reports inspect. Never invents scores.
# OFFICIAL=1 POSTs only if BOSS_QA_ALLOW_OFFICIAL=1 is already on.
qa-official-reports:
ifeq ($(OFFICIAL),1)
	@test -n "$$BOSS_API_KEY" || (echo "BOSS_API_KEY is required (never commit it)."; exit 2)
	$(PYTHON_BOOTSTRAP) scripts/qa/official_reports.py --mode official
else
	@test -n "$$BOSS_API_KEY" || (echo "BOSS_API_KEY is required (never commit it)."; exit 2)
	$(PYTHON_BOOTSTRAP) scripts/qa/official_reports.py --mode inspect
endif

# Captain NL + island contract only. Not live dogfood. Does not stamp GA.
# Overwatch/apply stay fail-closed; this target never spends or self-modifies BOSS.
qa-metatron-captain: install-python install-node
	PYTHONPATH=.:engine $(PYTHON) -m pytest engine/tests/test_metatron_captain.py engine/tests/test_focused_agents.py scripts/tests/test_metatron_tui.py -q
	pnpm --filter boss-web test -- tests/metatron-captain.test.ts tests/focused-agents.test.ts tests/edition-visibility.test.ts tests/platform-ops-ui.test.ts

# Community GA bar: completed factory app + reachable non-fixture preview.
# Never launches a new budgeted code.task. DRY_RUN=1 structure only.
qa-working-software:
ifeq ($(DRY_RUN),1)
	$(PYTHON_BOOTSTRAP) scripts/qa/working_software_gate.py --dry-run \
		--api-base "$${BOSS_API_BASE:-$${BOSS_API_URL:-https://boss-api.local.sean.network}}" \
		--project-id "$${PROJECT_ID:-$${BOSS_PROJECT_ID:-boss-dogfood}}"
else
	$(PYTHON_BOOTSTRAP) scripts/qa/working_software_gate.py \
		--api-base "$${BOSS_API_BASE:-$${BOSS_API_URL:-https://boss-api.local.sean.network}}" \
		--project-id "$${PROJECT_ID:-$${BOSS_PROJECT_ID:-boss-dogfood}}"
endif

# Community system gate: whole no-spend stack as subprocesses.
# Always: harness --dry-run, workflow (dry-run or live), factory --dry-run.
# When BOSS_API_KEY is set: harness catalog, live_dogfood, workflow live,
# factory live with --create-fixture only if fixture_supported.
# QA_PREVIEW_FIXTURE=1 adds POST+GET /v1/previews/{id}/web/.
# DRY_RUN=1 structure + child --dry-run only. Never echoes BOSS_API_KEY. Never spends.
qa-system:
ifeq ($(DRY_RUN),1)
	$(PYTHON_BOOTSTRAP) scripts/qa/system_gate.py --dry-run \
		--api-base "$${BOSS_API_BASE:-$${BOSS_API_URL:-https://boss-api.local.sean.network}}" \
		--project-id "$${PROJECT_ID:-$${BOSS_PROJECT_ID:-boss-dogfood}}" \
		$(if $(BOSS_SYSTEM_GATE_OUTPUT),--output "$(BOSS_SYSTEM_GATE_OUTPUT)")
else
	$(PYTHON_BOOTSTRAP) scripts/qa/system_gate.py \
		--api-base "$${BOSS_API_BASE:-$${BOSS_API_URL:-https://boss-api.local.sean.network}}" \
		--project-id "$${PROJECT_ID:-$${BOSS_PROJECT_ID:-boss-dogfood}}" \
		$(if $(BOSS_SYSTEM_GATE_OUTPUT),--output "$(BOSS_SYSTEM_GATE_OUTPUT)")
endif

public-flip-check:
	$(PYTHON_BOOTSTRAP) scripts/check_public_flip.py

openapi-live-qa-parity: install-python
	PYTHONPATH=.:engine $(PYTHON) scripts/validate_openapi_route_parity.py \
		--prefix /v1/code-tasks \
		--prefix /v1/delivery \
		--prefix /v1/deployment \
		--prefix /v1/deployment-targets \
		--prefix /v1/sandboxes \
		--prefix /v1/previews \
		--prefix /v1/factory \
		--prefix /v1/orchestration

sandbox-template:
	mkdir -p tmp
	rsync -a --delete templates/boss-sandbox/ tmp/boss-sandbox/

# Security/DevOps P2: dep audit (per .ai/security-review + AUTO-004 follow-up). Reports current state; recommends full tools for CI.
# No auto-fix; dev-only vulns expected in some (vite etc for web dev server). Invoked by full-validate.
# Advisory only — does not fail the build. Use deps-audit-strict to fail on high/critical runtime Python pins.
deps-audit: install-python
	@echo "=== Python runtime deps (freeze) ==="
	$(PYTHON) -m pip list --format=freeze | sed -n '1,30p'
	@echo ""
	@echo "=== Web pnpm audit (dev deps only; prod build unaffected) ==="
	cd apps/web && pnpm audit --json 2>/dev/null | $(abspath $(PYTHON)) -c 'import sys,json; d=json.load(sys.stdin); print(json.dumps(d.get("advisories", {}) or d, indent=2)[:2000])' || echo "(pnpm audit output or no vulns; run in web/ for details)"
	@echo ""
	@echo "Advisory only. Fail-on-high runtime Python: make deps-audit-strict. Dev-only npm/pnpm toolchain advisories stay waived."

# Local fail-on-high/critical gate for runtime Python pins (engine + app runtime).
# Does not enable GitHub Actions. Does not fail on waived npm/vitest/vite/esbuild/postcss findings.
deps-audit-strict: install-python
	$(PYTHON) -c "import pip_audit" 2>/dev/null || $(PIP) install -q 'pip-audit>=2.7,<3'
	$(PYTHON) scripts/deps_audit_strict.py

# Full local release readiness (per .ai/release-readiness, INFRA_RUNBOOK, AUTO-004).
# Includes all validators + build/lint/test + deps-audit. DB/docker required for test/smoke paths.
# Run before k8s deploys or claiming release readiness.
full-validate: lint deps-audit test build k8s-validate infra-validate release-blockers smoke
	@echo "full-validate complete (see individual outputs; external blockers expected from release-blockers)."
