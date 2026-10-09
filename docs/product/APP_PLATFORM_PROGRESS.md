# App platform build: progress

Spec: [APP_TEMPLATES_AND_SECURITY_LOOP.md](APP_TEMPLATES_AND_SECURITY_LOOP.md) (§8 is the build plan).
Branch: `feat/app-platform` (worktree `~/src/boss-wt-appplat`). Push after every step.

## Stream S0: day-1 interfaces

| # | Step | State |
|---|---|---|
| 1 | `db/schema.sql`: `-- BEGIN Fn … -- END Fn` blocks for F2, F4, F5, F6 with the tables the spec defines | done |
| 2 | Permission catalogue: the §8.2 permissions + regenerated files | done |
| 3 | Router stubs (`routes_templates.py`, `routes_security.py`, `routes_links.py`, `oidc_routes.py`), 501 `not_implemented_yet`, included in `main.py` | done |
| 4 | `engine/boss_engine/templates/manifest.py` (boss.template/v1, frozen) + unit tests | done |
| 5 | IDENTITY.md amendment for D10 (RS256 ID tokens for apps; EdDSA-only is BOSS-internal) | done |

### Notes per step

1. Schema. Every table §2.7, §2.4, §4.4, §5.2 and §6.3 lists column by column is created inside its
   feature's block (types inferred from the spec: ids TEXT, closed sets as CHECKs). Not specified
   column by column, left as `TODO(S6)` in the F6 block: `oidc_pool_user_factors`, `oidc_sessions`.
   `app_secret_bindings.mode`'s CHECK is table-level because the column reconcile in
   `boss_engine/db.py` cuts an inline definition at the word "generated". `ensure_schema` applied
   twice on a fresh database: 961/961 statements, no new reconcile skips.

2. Catalogue (`packages/identity/catalogue.yaml`, regenerated with
   `PYTHONPATH=engine python3 -m boss_engine.identity.codegen`). 180 → 192 permissions; the reviewed
   role sizes are updated in `engine/tests/test_identity_catalogue_is_generated.py` and
   `apps/web/tests/identity-contract.test.ts`.

   | Permission | Scope | Roles | Flags |
   |---|---|---|---|
   | `template:read` | org | O A P V U | delegable |
   | `template:import` | org | O A P | — |
   | `template:approve` | org | O A (D3) | step-up, critical |
   | `app_secret:read_metadata` | project | O A P | delegable |
   | `app_secret:write` | project | O A P | step-up (D9: reveal test users is behind it) |
   | `app_secret:live` | project | O A | step-up, critical |
   | `security_scan:run` | project | O A P | delegable |
   | `security_finding:triage` | project | O A P | — (a person's judgment, not delegable) |
   | `oidc:admin` | org | O A | step-up, critical |
   | `oidc_client:manage` | project | O A P | step-up |
   | `oidc_pool_user:manage` | org | O A | step-up |
   | `oidc_app:sign_in` | project | O A P V | — (bound at a project = may sign in to its apps) |

   Not done (owner streams decide): no Passport preset holds a new permission yet. S2 decides whether
   the builder/planner presets get `template:read`; S4 whether the reviewer preset gets
   `security_scan:run`.

   The spec's `app_secret:read_meta` is named `app_secret:read_metadata` (the catalogue's read
   actions are `read`, `read_metadata`, `read_all`; the spec now says so too).

3. Router stubs. `apps/api/app/routes_templates.py` (S2), `routes_security.py` (S4),
   `routes_links.py` (S5), `oidc_routes.py` (S6), included at the end of `main.py` before the
   coverage check. 54 operations, all declared (coverage 1442/1442). Each answers 501
   `{"error": "not_implemented_yet", "owner": "<stream>", "spec": ...}`;
   `apps/api/tests/test_app_platform_day1_routes.py` pins that under enforce (owner reaches every
   stub; no credential reaches only `/oidc/*`; a viewer cannot import or approve). A stream that
   builds its routes updates that test.
   Decisions made here (S-owners may revisit):
   - App-level routes take `project_id` (query for GET/DELETE, body otherwise): an app key is
     unique per project only.
   - Link create/revoke use `project:update` (the spec's "project:write" does not exist). That is
     Owner/Admin + step-up: operators cannot link apps until S5 or the operator decides otherwise.
   - D9's reveal is `POST /v1/apps/{app_key}/test-users/reveal` (`app_secret:write`, step-up).
   - OIDC admin GETs are declared with the manage keys (no `oidc:read` exists; listed in
     `GET_NOT_READ` of `test_identity_authz_read_only.py`). Open question for S6: does the Auditor
     need an `oidc:read`?
   - Protocol routes: discovery and JWKS are `public:jwks`; authorize, token, userinfo,
     revocation and end_session are `public:oidc_provider_endpoint`, a new `PublicReason`
     (IDENTITY_BUILD.md §7, AP-2) with verifier `oidc_routes.verify_oidc_request` (S6).
     `/oidc/` joins SCIM and SAML as a foreign protocol outside the idempotency contract.
   - New object kinds (`identity/object_kinds.py`): `security_scan`, `security_finding` (project via
     its scan), `app_link`, `oidc_client` (so `oidc_clients.project_id`/`app_key` are NOT NULL: a
     client belongs to one app).
   - `app_secret:live` and `oidc_app:sign_in` are checked inside handlers, not by a route: listed
     in `RESERVED` of `test_identity_authz_catalogue_use.py`.
   Regenerate after any route change:
   `PYTHONPATH=.:engine python3 -m apps.api.tests.identity_matrix`,
   `PYTHONPATH=.:engine python3 apps/api/tests/test_identity_authz_public_routes.py`,
   `PYTHONPATH=.:engine python3 scripts/dump-identity-api-routes.py`.
   Known failure on the base, not from this work:
   `test_a_streaming_write_is_excluded_rather_than_buffered` (`POST /v1/agents/{role_key}/test`).

4. Manifest. `engine/boss_engine/templates/manifest.py`: `validate_manifest(text|dict)`,
   `load_manifest(path|dir)`, `TemplateManifest` (pydantic, frozen, `extra="forbid"`),
   `ManifestError.reasons` (one `<field>: <what is wrong>` line per problem, all at once).
   Checks the manifest alone: id/semver/word/env-name patterns, stacks ∈ node|python|java, a test
   command (junit, min_tests ≥ 1) for exactly the declared stacks, compilable secret patterns,
   egress = bare FQDNs (no wildcard, IP, scheme/port, local/reserved TLD), `METHOD /path` routes,
   relative guide/security paths, no self-reference, no duplicate names. The import's checks that
   need the repo or the install (tree, sizes, secret scan, built-in id clash, BOSS hosts in egress,
   who may set `insecure_by_design`) are S2's. Tests: `engine/tests/test_template_manifest.py`
   (45; the spec's example verbatim is valid; the field set is pinned).

5. D10. IDENTITY.md gains D35 and a paragraph under the token families: "EdDSA only" is for
   BOSS's own tokens; app ID tokens are RS256 by default, EdDSA per client, in a separate `oidc`
   key family neither side accepts from the other; D3 points at D35. The amendment log
   (docs/maintainers/IDENTITY_BUILD.md §7) records AP-1 (catalogue), AP-2 (`oidc_provider_endpoint`)
   and AP-3 (D35).

## Stream S2: F2 template engine and API

Branch `feat/s2-templates` (worktree `~/src/boss-wt-s2-templates`), from
`feat/app-platform` @ 8b7cc39d. Engine: `engine/boss_engine/templates/`; API:
`apps/api/app/routes_templates.py` (the 501 stubs replaced, declarations unchanged).

| # | Step | State |
|---|---|---|
| 1 | Engine core: `validator.py` (the whole-tree import checks), `importer.py` (read-only git fetch), `secret_scan.py`, `gate.py`, `service.py` (catalogue, review), `secrets.py` (app secret bindings), built-in `templates/apps/hello/` | done |
| 2 | Vault kind `app_secret` (`identity/vault.py`) + the kind CHECK widened in the identity block (in place, `DO $$` like the skill_definitions widenings); F2 block: `app_template_files`, `app_secret_bindings.live_decision_id`, `.shape` | done |
| 3 | `apply.py` (apply = a governed build through `start_delivery_workflow`), `trial.py`, the `_template_gates` hook in `governed_code_task_service.py`, the real handlers | done |
| 4 | Unit suites `engine/tests/templates/` (validator, adversarial importer, gates, scan, apply assembly) | done |
| 5 | API suite `apps/api/tests/test_templates_api.py`; `test_app_platform_day1_routes.py` narrowed (S2 built) | done |
| 6 | E2E proof on a compose stack: apply hello to a scaffolded app via the API, `template_tests` passes, PR on the fake GitHub | done (green) |

### Design notes (S2)

- **One store for every template.** Built-ins are validated and upserted per org on first read
  (`sync_builtins`, idempotent by digest; a hidden one stays hidden) with their files in
  `app_template_files`, so apply, the gate and the gallery read built-ins and imports the same
  way, pinned by digest, with no shared volume between api and worker. `BOSS_TEMPLATES_DIR`
  overrides where built-ins are read from. **S1 / image owner: `Dockerfile.community` must
  `COPY templates ./templates`** for built-ins to exist in the image (not edited here: S1 owns it).
- **Import is fetch-and-read only.** `git init --bare` + `git fetch --depth 1` into a quarantine
  dir, read with `ls-tree`/`cat-file --batch`: no checkout, so no file, symlink or hook of the
  template ever exists on disk; git runs with hooks off, no system/global config, no submodules,
  fsck on, https only (`file://` only with `BOSS_TEMPLATE_IMPORT_ALLOW_LOCAL=1`), through
  `BOSS_EGRESS_CONNECT_PROXY` when set (the host must be on egress's allowlist: an operator
  decision). URL refusals: non-https, user-info, IP literals, local names, non-443 ports; refs
  that could be options (`--upload-pack=…`). `connection_id` (private repos via a GitHub
  connection) answers 422 not-supported for now.
- **Validation** (one list of reasons): size/count/2 MB per file, symlinks, submodules,
  `.gitmodules`, `.git` paths, escaping paths, binaries outside images/fonts, manifest, built-in
  id clash, stack dirs + non-empty tests, brief/guide/security note, LICENSE, egress to this
  BOSS's own hosts (`BOSS_PUBLIC_URL` & co.), **reserved env names** (a template may not ask for
  `BOSS_*`, `DATABASE_URL`, `GITHUB_TOKEN`, cloud creds, proxies, loader vars…),
  `insecure_by_design` only with template:approve + acknowledgement, and the secret scan.
  A rejected import keeps its report but no files; re-importing a rejected version replaces it.
- **Approval** (D3): template:approve (Owner/Admin) and never the importer (`seat_separation`
  403); pins (git_url, commit_sha, digest). Built-ins: DELETE → 403, `?hide=true` hides.
- **Apply** validates (active, stack declared, the app's inferred stack from its builds' files,
  `requires` applied, egress accepted via `accept_egress`, secrets bound or test-defaulted:
  generated/stub bindings are created) and calls `main.start_delivery_workflow` with
  `workflow_key` (default `fast_fix`), `extends_run_id` (the app's newest succeeded build) and
  `input.template = {id, key, version, digest, source, stack, framework, options,
  application_id, egress, app_key}`. The template's brief and its stack's reference
  implementation are inlined in the directive (≤ 48 KB; an imported template's inside an
  UNTRUSTED envelope whose end marker cannot be forged). **Deviation:** the spec's read-only
  mount at `/opt/boss/template/` needs sandbox code S2 does not own; the inline reference serves
  for now.
- **The gate hook** (`_template_gates`, one function + its call + one evidence key): reads
  `input.template` through the build's workflow run (the stored row, digest re-checked), adds
  `template_tests` (manifest command via `sandbox.run` in the app dir; JUnit reports written
  after the start are parsed by `<testcase>`, need ≥ `min_tests` passing, no failures, exit 0;
  each template test file's name must be in the diff; report files are removed so they never
  reach the PR) and `no_committed_secrets` (scan of the diff incl. the app's build-binding
  values). Fails closed on any lookup error. In microVM mode the template tests are not run
  (gate fails, says so): follow-up.
- **Secrets**: write-only; reads return name/env/mode/set_by/set_at/last_used_at/shape only.
  A name must be declared by a template applied to the app (or the `template_id` in the PUT).
  `test` values must match `pattern` and must not look live; `live` needs app_secret:live,
  env=deploy, and creates a pending decision (risk 3) that must be approved before
  `values_for_env(env="deploy")` returns it; builds and previews never get live.
  `values_for_env` is the engine-side reader for preview/deploy wiring (not wired into the
  sandbox env here: sandbox/preview/deploy code is other streams'). Replacing or deleting a
  binding crypto-shreds the old vault item.
- **Trial**: approved/built-in only (it runs template code), per stack in the build sandbox
  (`sandbox_identity.bind`, so `BOSS_SANDBOX_ISOLATION=required` refuses unisolated), recorded in
  `app_templates.trial`; cards show `tested` stacks.
- **Application status** is derived on read from the build its workflow run started: `pr_open`
  when a PR exists, `applied` when the PR's merge decision executed, `failed` otherwise.
- Org confinement: every handler reads templates by the caller's org; app routes check the
  project is in the caller's org.

### Tests (S2)

- `engine/tests/templates/` — validator (40), importer (39, real git over file://: symlink escape,
  huge file, hooks, submodule, size cap, option-injection refs, only-git-runs, proxy), gate +
  scan + apply assembly (32, incl. hello's real node and pytest tests through the gate).
- `apps/api/tests/test_templates_api.py` — every route per role, seat separation, malicious
  import, insecure label, built-in delete, trial, apply refusals and success, prompt injection,
  secrets round trip / patterns / live + decision / no value in responses or logs.

### E2E proof (S2, 2026-10-08): green

`tests/e2e/templates/apply_hello_walk.py` on a compose stack (project `bosss2`, enforce, every
outside service faked: `build-override.yml` + an override mounting `templates/` at
`/opt/boss-templates` with `BOSS_TEMPLATES_DIR=/opt/boss-templates/apps`, because the image does
not COPY `templates/` yet). The walk reuses build_walk's first half (GitHub App connect, repo,
Fast fix + consent → the app `e2e_feature`, Python, built and PR'd), then as the Owner:
`GET /v1/templates` lists hello; `POST /v1/templates/hello/apply {extends_run_id, stack: python,
start_anyway: [deployment_target]}` → 201; the build onto the app's branch records quality gate
`pass` with `template_tests` (pytest, 2/2 from `boss-template-junit.xml`, digest pinned) and
`no_committed_secrets` (0 findings) among 13 passed gates; the app's PR (#1, its branch) now
carries `hello.py` + `tests/template_hello/test_hello.py` and no JUnit report; the app's
templates show hello `pr_open`. Earlier runs also showed the gate failing CLOSED when the
template's test file was missing from the diff. `--resume` applies to an app an earlier walk
built. The stack was taken down with `down -v`.

Commands (scratch files in the session scratchpad `s2e2e/`: `mkenv.py` writes a 0600 env with
ports 53750/58750/55850/58751/58780/58781, `dc.sh` = compose -p bosss2 with the three files,
`setup.py up|down` = up + bootstrap seed + claim + seed-test-accounts):
`./dc.sh build api worker egress web && python3 setup.py up`, then
`cd tests && python3 -m e2e.templates.apply_hello_walk --stack-dir <s2e2e> --project bosss2
--console http://localhost:53750 --api http://127.0.0.1:58750 --fake http://127.0.0.1:58781
--shots <s2e2e>/shots`, then `python3 setup.py down`.

### Findings for other streams / follow-ups (S2)

- **S1 (image)**: `Dockerfile.community` needs `COPY templates ./templates` (built-ins).
- **Pipeline policy (not S2's)**: a build whose gate fails (e.g. `template_tests`,
  `tests_strong_enough`) still ended `completed` with quality gate `fail` and pushed to the app's
  PR — the same treatment every gate gets at this tier, so S2.1 holds, but the operator may want
  a failed gate to hold the PR.
- **Spec stage**: the scripted spec in the e2e replaced the directive, so the QA stage never saw
  the template; the e2e has the implementer copy the template's test file. A real model's spec
  carries the brief; the reference mount at `/opt/boss/template/` (sandbox code) would make this
  robust.
- Not built here: app secrets into the build/preview/deploy environment (the reader is
  `templates.secrets.values_for_env`; the wiring is preview/deploy/sandbox code), MCP tools
  (`list_templates`, `describe_template`, `apply_template`), private-repo import via a GitHub
  connection (422 for now), template tests in microVM mode.
- Known failure on the base (unchanged): `test_a_streaming_write_is_excluded_rather_than_buffered`.

### Resume (S2)

`cd ~/src/boss-wt-s2-templates && git pull` (branch `feat/s2-templates`). Tests:
`PYTHONPATH=.:engine python3 -m pytest engine/tests/templates apps/api/tests/test_templates_api.py
apps/api/tests/test_app_platform_day1_routes.py` with `DATABASE_URL` at boss-test-pg (a private
database, e.g. `boss_test_s2`, created and dropped by the runner). All 156 + the day-1 suite pass.

## S3-login-catalog: the login-oauth and catalog-cart templates (F3 §3.1, §3.4)

Branch `feat/s3-login-catalog` (worktree `~/src/boss-wt-s3-login-catalog`), from
`feat/s2-templates`. Files only in `templates/apps/login-oauth/` and `templates/apps/catalog-cart/`,
plus one new engine test (`engine/tests/templates/test_builtin_templates_apply.py`).

| # | Step | State |
|---|---|---|
| 1 | `login-oauth` node (Express + openid-client 6, express-session, fake IdP, 15 tests) | done |
| 2 | `login-oauth` python (FastAPI + Authlib 1.8, server-side session middleware, fake IdP, 15 tests) | done |
| 3 | `login-oauth` java (Spring Boot 3.5.16 + Spring Security OAuth2 client, lazy registrations, one-shot state repo, fake IdP on com.sun.net.httpserver + Nimbus, 15 tests) | done |
| 4 | `catalog-cart` node (Express, signed cart cookie, merge on sign-in, checkout contract, 12 tests) | done |
| 5 | `catalog-cart` python | todo |
| 6 | `catalog-cart` java (if time allows) | todo |
| 7 | engine test: every built-in manifest validates and applies (apply assembly) | todo |

### Resume (S3-login-catalog)

`cd ~/src/boss-wt-s3-login-catalog && git pull`; pick the first step not `done`.
Run a stack's tests in a scratch COPY of `stacks/<stack>/` (never in the template dir: a
`node_modules`/`__pycache__` there breaks the tree validator's limits):
node `npm run test:login-oauth` (Node 20: `docker run node:20-bookworm-slim`); python
`pip install -r requirements.txt && python3 -m pytest -q tests/template_login_oauth` (3.11:
`docker run python:3.11-slim-bookworm`); java `mvn -q -B test -Dtest=LoginOauthTemplateTest` (JDK 21:
`docker run -v <copy>:/w -v <scratch>/m2:/root/.m2 -w /w maven:3.9-eclipse-temurin-21 ...`; the
local JDK is 17).

## What's next (after S0)

S0 is complete. Per §8.2 the next streams start in parallel against these interfaces: S2 (F2
engine/API: `engine/boss_engine/templates/` beyond `manifest.py`, `routes_templates.py`, the
`template_tests` gate, vault kind `app_secret`, which needs `identity_vault_items.kind`'s CHECK
widened in the identity block by amendment), S4, S5, S6, then the UI streams. Each stream edits only
its own schema block and router module, and updates `test_app_platform_day1_routes.py` as its
routes stop answering 501.

## Resume instructions

- `cd ~/src/boss-wt-appplat && git pull` (branch `feat/app-platform`).
- Pick up the first step above that is not `done`.
- Test DB: container `boss-test-pg` on localhost:55432, user `boss`; password via
  `docker exec boss-test-pg printenv POSTGRES_PASSWORD` (never print it). Create a private database
  (S0 used `boss_test_s0`) and drop it afterwards.
