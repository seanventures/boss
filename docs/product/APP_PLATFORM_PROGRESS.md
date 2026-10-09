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

## Stream S4: security scan engine and API (F4, §4; UI and workflow wiring later)

Branch `feat/s4-security` (worktree `~/src/boss-wt-s4-security`), from
`feat/app-platform` @ 8b7cc39d.

| # | Step | State |
|---|---|---|
| 1 | `security` toolchain layer: Dockerfile.community stage, registry entry, egress `api.osv.dev` (D6) | done |
| 2 | `engine/boss_engine/security_scan/`: parsers, fingerprints, redaction, scope, runners, service, report + unit tests | done |
| 3 | `routes_security.py` (501s replaced), worker job `security.scan`, authz tests | done |
| 4 | End-to-end proof on a compose stack (Node/Express + SQLite app: scan, fix, re-scan) | done |

### Notes per step

1. The layer. `BOSS_TOOLCHAINS=security` selects `toolchains-security`, built FROM `toolchains-java`
   (ZAP needs a JRE; spec §4.2), so the manifest carries `java` and `security`. Pinned:
   Semgrep 1.180.0 + every dependency from a hashed lock
   (`engine/boss_engine/security_scan/semgrep-requirements.lock`, `pip --require-hashes
   --only-binary=:all:`) in its own venv; OSV-Scanner 2.6.0 and ZAP 2.17.0 (Linux tarball) checked
   against the release SHA-256s. Rule packs: semgrep-rules @ 9bbf021d, directories javascript,
   typescript, python, java, generic/secrets (891 rule files), selected by `rulepack.py` and
   verified by a CONTENT digest (`sha256:1ebc108c…`), not the archive's hash (GitHub re-gzips).
   No rule is fetched at scan time. The rules are **not committed** to this repo: they are under the
   Semgrep Rules License v1.0 — fetched at image build on the operator's host. Open question for
   the operator: is that use within the license for BOSS Community? (Flagged, not decided here.)
   Image size (arm64 stage, measured): Java 372 MB + Semgrep venv 383 MB + ZAP 268 MB + OSV 52 MB
   + rules 5 MB ≈ 1.08 GB on the api/worker image when `security` is on; nothing when off.
   `boss toolchains enable security` works (`KNOWN_TOOLCHAINS` in boss.sh).

2. The engine (`engine/boss_engine/security_scan/`). `parsers` (Semgrep SARIF, OSV JSON grouped by
   advisory aliases, ZAP traditional-json one finding per alert × route template × param; 50 MB raw,
   10k findings, 4 KB evidence; a shape the tool does not write is a ParseError, so the tool is
   `failed`, never "0 findings"); `fingerprints` (code: file + enclosing function found from the
   checkout, JS/TS, Python, Java/Kotlin; dependency: lockfile + ecosystem + package, not version;
   DAST: method + path template + param, not host/port/ids; over the REDACTED snippet, so a rotated
   hardcoded secret is the same finding and no hash of a secret is stored; `snippet_key` finds
   "moved"); `redact` (identity.redaction, then string literals of secret rules, token shapes,
   cookie values, 4 KB cap); `scope` (app = latest build of that app id in the project; ref = its
   branch or a sha on it; ZAP target `sandbox` or `preview:<id>` of the same project AND app served on
   loopback; anything else refused); `runners` (Semgrep offline with the vendored packs, OSV through
   egress, the app started in the sandbox via `preview_service.how_app_runs` on a loopback port,
   ZAP Automation Framework plan scoped to that one URL, passive unless `mode=api`; per-tool
   timeouts `BOSS_SECURITY_TIMEOUT_<TOOL>`); `service` (request → queued scan + `security.scan` job;
   execute in owner scope `sandbox`/`secscan_<id>` with `sandbox_identity.bind`, so every tool runs as
   the run's uid; first_seen and a person's triage carry to later scans by fingerprint; triage needs
   a reason, emits `security_finding.triaged`); `report` (fixed / still_open / moved / removed /
   not_rescanned / new, `rules_differ` when tool versions or rule digests differ, Markdown export).
   Fixtures are real output of the pinned tools over `tests/e2e/security/vulnapp` and
   `vulnapp-fixed`. Tests: `engine/tests/test_security_scan_engine.py` (51).
   Decision: old scans' findings are never rewritten to `fixed`; "fixed" is the report's verdict
   between two scans (history stays as scanned; only triage changes a row).

3. API and job. The six §4.3 routes answer for real (`apps/api/app/routes_security.py`); bodies are
   `extra=forbid` (a smuggled `url` is a 422). `POST /v1/security/scans` → 202 `{scan_id}` and a
   `security.scan` job (`job_permissions`: `security_scan:run`), stamped with the requester's
   authority; the worker leases it and calls `POST /v1/security/scans/{id}/run` (worker:lease,
   `require_worker_principal`), which runs the scan in the API (where the scanners and the sandbox
   helper are). Worker: one capability, one branch, one client method. Tests:
   `apps/api/tests/test_security_scan_routes.py` (13: roles O/A/P run and triage, V/Auditor read
   only; another project's person gets 404s; worker-only run; scope refusals against the DB —
   free URLs incl. the metadata address, another project's preview, another app's, a remote or
   stopped preview, a branch ref, an unknown tool, scanners not installed; scan → fix → re-scan
   report from real tool output; triage reason/event/carry-over; failed tool = not_rescanned;
   rules-digest difference flagged). `test_app_platform_day1_routes.py` no longer lists S4.
   Regenerated `tests/identity/matrix.yaml`, `apps/web/tests/fixtures/identity-api-routes.json`.
   Identity suites (catalogue use, matrix, public, read-only, system-only, job kinds) + worker: green.
   Observation for S0/identity: there is no `security_finding:read` / `security_scan:read`, so the
   PDP's no-existence-oracle rule answers a DENIED triage with 404 even to a viewer who can read the
   finding through `project:read`. Harmless (the viewer still cannot triage), but the console must
   not read a 404 there as "gone".

4. The proof (`python3 tests/e2e/security/scan_proof.py`; ~15 min cold, mostly the image build).
   Own compose project and image tag `bosss4` (refuses to start when another e2e stack is up),
   `BOSS_TOOLCHAINS=security`, db + api + worker + egress with the real egress (internet for npm and
   api.osv.dev), `down -v` always. The app (`tests/e2e/security/vulnapp`: Express + sql.js SQLite,
   SQLi in `findUser`, hardcoded JWT key in `issueToken`, lodash 4.17.20, no security headers) is
   registered as a build's LOCAL workspace (a build with no PR; `checkout` copies it, as previews
   do) — the GitHub clone path (`identity.runs.repo_access`) is the production one but is not
   driven here (it needs a connected GitHub App). Everything after that is the product: POST
   /v1/security/scans as the owner key → worker leases `security.scan` → API runs the tools as a
   sandbox uid. Result 2026-10-08, all PASS:
   - before (d1a1af20): Semgrep 3 (SQLi high, hardcoded JWT medium, csurf low; 18 s), OSV 3 lodash
     advisories (GHSA-35jh high; 0.6 s, through egress), ZAP baseline 14 (CSP, anti-clickjacking,
     nosniff, X-Powered-By, …; 12 s, app started in the sandbox on loopback);
   - after the fix (cab05493): Semgrep 1, OSV 0, ZAP 4; the planted findings are gone; all 5
     remaining findings keep their fingerprints (one moved lines);
   - report: 15 fixed, 5 still open, 0 new, 0 moved/removed; Markdown export;
   - scans recorded `sandbox: {isolated: true, uid: 61610 / 61095, network_fenced: true}`; egress
     refused nothing.
   Image: api with `security` 1.82 GB vs 1.21 GB default (docker content size, compressed);
   ≈ +1.08 GB on disk uncompressed (stage measure in step 1).

### Resume notes (S4)

- Not built here (later streams): the `security_review` workflow template, the Panel /
  recommendations (`security_recommendations` table is untouched), MCP tools, the UI, F5 links
  (a preview on another host is refused with `preview_not_reachable`), the vulns.yaml detection
  score. ZAP `mode=api` is wired (openapi + activeScan jobs, off by default per D12) but only
  unit-tested; the banking Java demo will be its first real run.
- Open: the Semgrep Rules License question (step 1); whether the reviewer Passport preset gets
  `security_scan:run` (S0 left it to S4: not granted — the workflow stream should decide with the
  template); `security_finding:read` absence (step 3 note).
- Re-run: unit `PYTHONPATH=.:engine pytest engine/tests/test_security_scan_engine.py`; API (needs
  a test DB) `apps/api/tests/test_security_scan_routes.py`; e2e `tests/e2e/security/scan_proof.py`.

## Stream S5: cross-app links (F5)

Branch `feat/s5-links` (worktree `~/src/boss-wt-s5-links`, from `feat/app-platform` @ 8b7cc39d).

| # | Step | State |
|---|---|---|
| 1 | Engine: `app_links/service.py`, rule module `identity/link_rules.py`, F5 schema block, `app_link:manage`, audit event types | done |
| 2 | API: `routes_links.py` built (list, create, revoke, test-user presence, D9 reveal) + `test_app_links.py` | done |
| 3 | Link gateway `apps/egress/boss_egress/links.py` (a port per link, per-request checks, NOTIFY) + adversarial tests | done |
| 4 | Wiring: preview start/respawn and the build get the link env; previews take ports from `BOSS_PREVIEW_PORTS`; the sandbox fence | done |
| 5 | Console App ▸ Links + browser-walked screenshot | done (independent tester walk: pending) |
| 6 | End-to-end proof on a compose stack | done: PASS |

### Decisions

- **`app_link:manage` (new, project scope, Owner/Admin/Operator, not step-up).** S0 left create/revoke
  on `project:update` (Owner/Admin + step-up), which would stop the operator in §5.1's own stories from
  linking the two apps they preview. A link hands out only GENERATED, test-only users of an app of the
  SAME project (D7, S5.3) through a gateway that reaches only that app's preview, so it is an
  operator's job and not a critical one. Not delegable: an agent cannot link apps. Catalogue 192 → 193;
  role sizes owner 188, admin 179, operator 92 (pins updated in the two catalogue tests).
- Listing links and test-user presence stay `project:read` (no value is ever shown). The reveal is
  `app_secret:write` (step-up, D9) and writes `app_link.test_users_revealed`.
- Audit: `app_link.created`, `app_link.revoked`, `app_link.test_users_revealed` join `audit.EVENT_TYPES`.
  Detail keys never name a credential (`gives`, not `credentials`: the audit refuses credential-named keys).
- **App key** = `agent_assignment.app_key(app name)`, the identity key every app view groups runs by;
  a path may also name the app by any of its run ids (the console's app rows are keyed by run id) or
  its name. `service.resolve_app_key` — S2 may want to share it for `/v1/apps/{app_key}/…`.
- **Schema**: the alias is unique among ACTIVE links only (partial index), so a revoked alias can be
  linked again; `gateway_port` is unique among active links. New tables in the F5 block:
  `app_link_passes` (hash only), `app_test_users` (presence, count, generation; values in the vault).
- **Test users** live in the vault as `provider_credential` items with provider `boss_test_users` (S2
  owns the `app_secret` vault kind; switch to it, mode `generated`, once S2 lands). 3 users per
  (provider app, env): two `user`, one `admin` (two users make IDOR tests possible). Rotated on each new
  preview of the provider; the old vault item is deleted. Every value is registered with the
  redaction filter in the process that holds it.
- **Link pass** (`boss_lnk_…`, header `X-Boss-Link`, env `BOSS_LINK_<ALIAS>_TOKEN`): one per (link,
  consumer workload). The gateway cannot tell sandbox uids apart (they share the api container's
  address) and the API process cannot change iptables at runtime (it drops NET_ADMIN for good), so
  the identity "B's run token or B's preview" is carried by a pass BOSS mints for that workload, and
  the gateway checks the workload is live on every request (the run is running and its egress
  credential is not revoked; the preview is running and unexpired). B's own `Authorization` and
  cookies go to A untouched; the pass never does.
- `credentials=client` is refused (422) until F6 exists.

### Tests

- `engine/tests/test_link_rules.py` (26): the gateway decision table row by row, header filtering,
  port ranges.
- `apps/api/tests/test_app_links.py` (23): who may link/see/revoke per role; D7 by every door (provider
  of another project, consumer of another project, revoke naming another project); bad alias/env/
  credentials; one port per link and alias reuse after revoke; revocation kills every pass and both
  events are audited; test users flow to provider and consumer, rotate per preview, are encrypted at
  rest, never appear in logs, lists, presence, create responses or audit rows; the D9 reveal needs
  step-up and is audited; a pass dies with its workload.
- `apps/api/tests/test_link_gateway.py` (12, real sockets, real LISTEN/NOTIFY): B signs in to A as A's
  test user through the link; A sees B's own Authorization, never the pass, and a clean Host; no pass,
  a forged pass or a run token instead of a pass → 401 and A is never called; absolute-form targets
  naming the API, another preview, the metadata IP or the DB → 400 and none is called; traversal and
  encoded separators → 400; a Host header naming another preview is ignored; a pass opens only its own
  link's port; a pool port with no link refuses connections; a preview row tampered to point at BOSS
  (outside the preview range, or 8000) → 403 and BOSS is never called; B's preview stopping kills its
  pass; A's preview gone → 502 `provider_not_running`; the cache alone (no NOTIFY) sees a revocation
  within `CACHE_SECONDS` (2 s); revoking cuts B off (measured < 5 s, typically < 1 s) and closes the port.
- Regenerated: `tests/identity/matrix.yaml`; `test_app_platform_day1_routes.py` no longer lists S5.

### The gateway (step 3)

`apps/egress/boss_egress/links.py`, started by `python -m apps.egress.boss_egress` unless
`BOSS_LINK_GATEWAY=off`. One `ThreadingHTTPServer` per active preview link's port (pool
`BOSS_LINK_PORTS`, default 18100-18199), opened and closed by a sync every second and on every NOTIFY
on `app_links`. Facts are cached ≤ 2 s and dropped on NOTIFY. Forwards to `BOSS_LINK_PREVIEW_HOST`
(default `api`, where process previews run) on the provider's newest running process preview port,
only inside `BOSS_PREVIEW_PORTS`. Request bodies ≤ 10 MB, no chunked uploads (411). Logs a line per
refusal (port, link, reason), never a header or a query.

### Wiring (step 4)

- **Build**: `app_links/run_env.for_run` wraps both build entry points (`code_task_service`,
  `governed_code_task_service`, one `with` line each, inside the sandbox bind). It computes the run's
  link environment once (`workload_environment`, consumer kind `run`) and `agent_env.agent_environment`
  adds it, after its credential filter, to every process spawned in THAT build's owner scope. When the
  build ends, the entry is dropped and the run's passes are revoked. A project with no active link
  costs one indexed query.
- **Preview**: `preview_service.preview_link_environment` (consumer kind `preview`; as provider,
  `BOSS_TEST_USERS`, rotated on a new preview, not on a respawn) feeds `launch_app(extra_env=…)`, added
  after `app_environment`'s filter like PORT. `stop_preview` revokes the preview's passes.
- **Preview ports**: previews (start and respawn) take a port from `BOSS_PREVIEW_PORTS` (default
  19000-19999; falls back to an ephemeral port only if the range is full). The serve proof keeps
  ephemeral ports.
- **Fence** (`scripts/sandbox/entrypoint.sh`, static, root at start: the API drops NET_ADMIN for good,
  so nothing is added at runtime): the pool is REJECTed to `BOSS_PREVIEW_PORTS` on every address
  BEFORE loopback is accepted, and may reach `BOSS_LINK_PORTS` on the container networks next to
  8000/8090/8091. So a build reaches another app's preview only through the gateway, which checks the
  pass. `engine/tests/test_link_fence.py` renders the chain the script builds and checks the order.
- Deploy: `service.deployed_url` / `link_url` give a `deploy` link the provider's public URL from its
  newest healthy deployment's release packet; **not yet wired into the governed deploy's environment
  or ChangeSet** (resume item).

### Console (step 5)

Factory ▸ Apps ▸ row ⋯ ▸ **Links** opens `apps/web/app/factory/links-sheet.tsx` (one surface): rows
`ALIAS → App · preview · test users · active`, the address (click to copy), last used, test-user count,
Revoke, and *Reveal test users* (D9). `+ Link`: Talks to (the project's other apps), Alias, Reaches its
Preview/Deployed, Test users. Server actions in `links-actions.ts` (declared: `app_link:manage`,
`app_secret:write`). Walked in Chromium as the seeded **Operator** on the proof stack
(`tests/e2e/links/walk_console.py`): screenshot `docs/product/app-platform/s5-app-links.png`.
Test: `apps/web/tests/app-links-sheet.test.tsx` (3). The independent tester walk (§8.3) is not done yet.

### End-to-end proof (step 6): PASS

`tests/e2e/links/proof.py` on a compose stack (`bosss5`: db, api, egress built from this branch,
claimed, `down -v` after). In the api container: A "Banking" (a tiny API with a login,
`tests/e2e/links/provider_a.py`) starts the way a preview does (preview port, `app_environment` +
`preview_link_environment`, seeding `BOSS_TEST_USERS`); an Operator key links B → A (BANK, preview, test
users) and B → C (LEDGER) through the real API; B (`b_tests.py`) runs as sandbox uid 61777 through
`boss-sandbox-exec`, behind the real iptables fence, with a build's environment. Result (2026-10-08):

```
linked: 201/201, bank_url http://egress:18100
provider_preview: port 19086, seeded_test_users true
build_env: BOSS_LINK_BANK_{TOKEN,URL,USERS}, BOSS_LINK_LEDGER_{TOKEN,URL}
B signs in to A as A's test user through the link: true (200)
around the gateway: A on loopback, A on the api address, the other preview, the database,
  169.254.169.254, the console -> all ConnectionRefused (fence)
gateway: no pass -> no_link_pass; absolute-form to api:8000 -> 400; BANK pass on LEDGER's port -> wrong_link
revoke through the API: 200; B's next run fails after 0.1 s (port closed)
verdict PASS
```

Re-run: build `api egress web` with `BOSS_COMMUNITY_IMAGE_TAG=bosss5`, `up` with `--keep`, run
`from boss_engine.bootstrap import seed; seed()` in the api, claim + seed accounts (harness
`identity.claim_owner` / `seed_accounts` at the console origin; `BOSS_CONSOLE_PUBLIC_ORIGIN` must be
set), then `proof.py`. Only one compose stack on this Mac at a time.

### Known gaps and resume notes

1. **Previews run as the control plane's uid** (pre-existing: `preview_service` is process-based and
   never binds a sandbox uid). So the fence holds for a BUILD of B, not for B's PREVIEW: B's preview can
   reach anything the API container can. S5.2 for preview→preview holds only at the gateway. Fix: lease a
   sandbox uid per preview (outside S5's files; worth its own change).
2. Deploy links: URL resolution exists; wiring into the governed deploy env + ChangeSet is not done.
3. "Bring-up dependency" (start A inside B's build sandbox, §5.4) is not built; builds use A's live
   preview through the gateway.
4. Test users use vault kind `provider_credential` (provider `boss_test_users`) until S2's `app_secret`.
5. The composer's "Talks to" field (§5.5) is S2-UI's file (`compose-links.ts`); the API is ready for it.
6. Independent tester walk of App ▸ Links (§8.3) still to do.
7. Merge note: S2/S4/S6 also change the catalogue counts (193 here) and `audit.EVENT_TYPES`.
8. Pre-existing failures seen while running affected tests (fail on the base too): 17 in
   `test_a_build_proves_its_app_runs`, `test_agent_behaviour_matrix`, `test_boss_can_actually_debug`,
   `test_sandbox_s1_go`; `test_ga_write_authority::test_prd_and_web_app_builds_require_target_project_execution_grant`;
   `test_logs_reach_the_console` does not import. Affected run: 602 passed, 18 failed (all 18 fail on 8b7cc39d).
9. Resume: `cd ~/src/boss-wt-s5-links && git pull` (branch `feat/s5-links`); test DB
   `boss_test_s5` was dropped (recreate it on boss-test-pg to run the API suites).

## Stream S6: BOSS as an OIDC provider (F6)

Worktree `~/src/boss-wt-s6-oidc`, branch `feat/s6-oidc` (from `feat/app-platform`
@ 8b7cc39d). Engine: `engine/boss_engine/oidc_provider/`; routes: `apps/api/app/oidc_routes.py`.

| # | Step | State |
|---|---|---|
| 1 | Protocol core (discovery, JWKS, authorize, consent, token, userinfo, revocation, end_session), client registry API, workforce pools on the console session | done |
| 2 | Unit, admin API and adversarial suites (the named list) | done |
| 3 | End-to-end proof: a Node app with openid-client on a local stack | done |
| 4 | OpenID conformance suite (Basic OP), if Docker allows | not run (see below) |
| P2 | Customer pools, hosted pages polish, console Identity ▸ App sign-in | phase 2 |

### Decisions and notes

- **Issuer** = `${BOSS_PUBLIC_URL origin}/oidc/<org_id>` (the org slug is the org id: `orgs` has no
  slug). Built only from the configured public origin, never from Host; no public origin = 503.
- **Keys**: the `oidc` family lives in its own table `oidc_signing_keys` (per org, RS256 + EdDSA,
  next/active/retiring/retired, private halves in the vault). BOSS-internal verifiers only read
  `identity_signing_keys`, and the oidc verifier only reads `oidc_signing_keys` (kids start
  `oidc-`): the separation is structural. `signing_keys.rotate_oidc_family_if_due` is the one
  function S6 adds there; keys are provisioned when the first pool or client is created, and
  `keys.signing_key` rotates a due key on use. TODO (identity owner): call
  `rotate_oidc_family_if_due` from `identity/maintenance.py` next to `rotate_if_due`.
- **Grants**: one `oidc_grants` row per sign-in; the code, every access token (`sid` claim) and the
  refresh family hang off it. Code replay, refresh reuse, a code or refresh token presented by
  another client, logout with `id_token_hint`, consent revoke, client disable/delete and losing
  `oidc_app:sign_in` all revoke the grant; userinfo checks it live.
- **Interactions** (`oidc_interactions`): no session -> `/login?next=/oidc/<org>/resume?interaction=…`
  (single use, 10 min); consent page -> POST `/oidc/<org>/consent` (session + Origin + HMAC(session,
  interaction) token). Two routes beyond S0's stubs: `GET /oidc/{org}/resume`, `POST /oidc/{org}/consent`.
- **Refresh tokens** only with `offline_access`, which always needs an explicit consent (even
  `first_party`). Family max age 90 days, each token 30 days, rotated on every use.
- **Access tokens**: RS256 always, `typ: at+jwt`, `aud` = client `resource` or client_id.
- **Pairwise `sub`** = HMAC(pool subject key, subject ‖ client); `public` = HMAC(pool key, subject).
  Never a BOSS id. The pool key is random, in the vault.
- **Phase-1 limits**: `prompt=login` or an exceeded `max_age` for a signed-in person answers
  `login_required` (no forced re-authentication from the OIDC flow yet); a client with
  `require_mfa` and a session without MFA gets a BOSS page, not a step-up prompt. Customer pools
  answer `temporarily_unavailable` at authorize; their user routes stay 501.
- **Amendment AP-4** (IDENTITY_BUILD.md §7): `oidc.*` audit events, `oidc.*` rate-limit buckets,
  the one signing_keys function.
- Open question from S0 (Auditor `oidc:read`): not added; every admin GET needs `oidc:admin` or
  `oidc_client:manage` (both step-up). Revisit with the console UI in phase 2.

### Tests (S6)

- `engine/tests/oidc/test_oidc_rules.py` (44, no DB): S256 vector, redirect-URI rules, client
  validation, inline-JWKS rules, page escaping and headers.
- `apps/api/tests/test_oidc_provider_flow.py` (10): discovery, JWKS, the whole workforce flow,
  sign-in redirect and resume, remembered consent, EdDSA client, public client, revocation.
- `apps/api/tests/test_oidc_provider_admin.py` (11): permissions, secret once, first-party, no URL
  fields (422), roles in tokens, consent revoke and client delete end tokens.
- `apps/api/tests/test_oidc_provider_adversarial.py` (99): A1 code replay, A2 PKCE, A3 redirect_uri
  tricks (16 shapes at authorize, 12 at registration), A4 mix-up, A5 refresh reuse, A6 alg/typ/
  key-family confusion (none, HS256 with the public key, rogue key under a real kid, ES256, BOSS
  run/actor typ, ID token as AT, AT as id_token_hint, app tokens and refresh token as BOSS bearer,
  console cookie as AT), A7 aud/azp and private_key_jwt (8 bad assertions, jti replay), A8 nonce,
  A9 pairwise/public subjects and client binding, A10 app cookie on a BOSS route, A11 CSRF on
  consent (Origin, Sec-Fetch-Site, token, other session, reuse) and prompt=none, A12 leakage (logs,
  audit details, hashed at rest, no AT in URLs), A13 client auth downgrade and lockout, A14 grant
  loss, disabled person/client, legacy shared session, require_mfa, A15 logout open redirect, A16
  request/request_uri/registration/response modes/duplicates.

### End-to-end proof (S6)

`tests/e2e/oidc/` (`run.sh <console origin>`; `setup_world.py` + `rp.mjs`, openid-client 6.8.1
pinned). Run 2026-10-08 on a local stack: API (`python -m apps.api.serve`, port 18093, DB
`boss_test_s6`, BOSS_PUBLIC_URL=http://localhost:13000) + console (`next dev`, port 13000,
forwarding /oidc/* through `apps/web/lib/oidc-proxy.ts`). A compose stack was not used: bosss4
then bosss2 held the one-stack slot. Result, all through the console origin: discovery -> consent
page for a signed-in member -> consent POST -> callback with `iss` -> openid-client code exchange
(state, iss, PKCE, nonce, ID token signature) -> userinfo (name, email, roles) -> refresh rotation
-> userinfo with the refreshed AT -> end_session back to the app -> the AT is dead (401) -> the
rotated refresh token is refused. `E2E PASS`. Also checked by hand: no session -> 302 to the
console `/login?next=/oidc/<org>/resume?...` (200), an unregistered redirect_uri -> 400, no Location.
Found and fixed by the e2e: a token-endpoint 401 for a non-Basic client carried a Bearer challenge
(openid-client v6 treats any challenge as fatal); it now carries none (Basic when Basic was used).

### Resume (S6)

- `cd ~/src/boss-wt-s6-oidc && git pull` (branch `feat/s6-oidc`).
- Tests: DB `boss_test_s6` on boss-test-pg (localhost:55432, user boss; password via
  `docker exec boss-test-pg printenv POSTGRES_PASSWORD`, never printed). Run with
  `DATABASE_URL=postgresql+psycopg://boss:$PW@localhost:55432/boss_test_s6 PYTHONPATH=.:engine python3 -m pytest -q apps/api/tests/test_oidc_provider_*.py`.
- DB `boss_test_s6` was DROPPED at the end of phase 1: `CREATE DATABASE boss_test_s6` first.
- Conformance (step 4), not run: the suite is its own compose stack (server, MongoDB, nginx), and
  the one-stack slot was held (bosss4, then bosss2). It also needs the issuer reachable from its
  containers (BOSS_PUBLIC_URL on host.docker.internal, or the suite on the host network) and a
  browser step for authorize (its `browser` config: a cookie of a signed-in member, the consent
  click). Expected gaps it will flag: `prompt=login` and `max_age` re-authentication
  (`login_required` today), and `response_mode=form_post` (not offered).
- Phase 2, in order: (1) customer pools: `oidc_pool_users`, `oidc_sessions` (cookie
  `__Host-boss_app_session`, Path=/oidc/), `oidc_pool_user_factors` (tables exist), reusing
  passwords/totp/webauthn/ratelimit/email_signin parameterised by a realm; sign-in/sign-up/MFA/
  passkey hosted pages; the pool-user admin routes (still 501); (2) re-authentication for
  `prompt=login`/`max_age` and step-up for `require_mfa` instead of the error page; (3) the
  console Identity ▸ App sign-in (Pools, Clients, Users, Roles; side sheets); (4) hosted pages in
  the console's sign-in style; (5) conformance.
- After a route change regenerate: `python3 -m apps.api.tests.identity_matrix`,
  `python3 apps/api/tests/test_identity_authz_public_routes.py`, `python3 scripts/dump-identity-api-routes.py`.

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

## Integration (feat/app-platform)

Integrator merges S2, S4, S5, S6 in that order, then three must-fixes (preview sandbox uid,
template gates block the PR, `COPY templates` in the image), then one combined compose proof.
Test DB: `boss_test_integ` on boss-test-pg (drop it when done).

| # | Step | State |
|---|---|---|
| 1a | Merge S2 (feat/s2-templates @ 54f32a91): clean. engine/tests/templates + test_templates_api 156 passed; identity suites 46 passed | done |
| 1b | Merge S4 (feat/s4-security @ bae890a7): conflicts in the day-1 test (rewritten: MODULES + BUILT per module) and this file. S4 suites 64 passed; identity suites 46 passed | done |
| 1c | Merge S5 (feat/s5-links @ 5caa424c): conflicts in the day-1 test (links added to BUILT) and this file; regenerated matrix/public/fixture/catalogue: no diff. S5 suites 65 passed; identity suites 46 passed | done |
| 1d | Merge S6 (feat/s6-oidc @ 9123ee29): conflicts in the day-1 test (oidc_routes BUILT except `/users`, phase 2), audit.EVENT_TYPES (union: app_link.* + oidc.*), matrix.yaml (regenerated: 1445 operations) and this file. S6 suites (engine/tests/oidc + test_oidc_provider_flow/admin/adversarial) 164 passed; identity suites 46 passed | done |
| 2a | Previews run as a sandbox uid, fenced (57386907) | done |
| 2b | template_tests / no_committed_secrets block the push and the PR (b5f7d5fb) | done |
| 2c | Dockerfile.community ships templates/ (634382f5) | done |
| 3 | Combined compose proof (project `bossint`) | in progress: preview sandbox PASS, apply hello PASS, S5 links PASS; final re-run of the walk on the pushed image running |
| 2d | Template files available to the build read-only (not only a 48 KB inline brief) | next |
| 2e | No host deploy of an app built from an `insecure_by_design` template | |
| 1e | Merge feat/s3-banking (555d1908) | |

Identity coverage suites run after each merge: catalogue use, matrix, public routes, read-only
GETs, system-only, route fixture freshness, day-1 routes, catalogue-is-generated.

### Integration notes

- **Day-1 routes test** rewritten once for all streams: `MODULES` (owner per router) + `BUILT`
  (per module, a path predicate: S2/S4/S5 fully built; S6 built except `/users`, phase 2).
- **Merge bug found and fixed**: S5 added a second `preview_service.preview_port()` (the app-port
  allocator) that silently replaced the preview ORIGIN's port (BOSS_PREVIEW_PORT, 3001): the
  three `test_previews_have_their_own_origin` failures on the merged base. Renamed
  `app_preview_port()`; `engine/tests/test_no_module_defines_a_name_twice.py` refuses any
  module defining a top-level name twice.
- **2a design**: `sandbox_identity.acquire/release` (bind's lease, held across calls) per preview
  id; tree in the uid's run dir; log at `$BOSS_PREVIEW_ROOT/<id>.log` (0600, O_NOFOLLOW);
  discovery only accepts a port the uid listens on; fence `BOSS_PREVIEW_SELF` = uid 61000+i may
  reach port 19000+i on loopback (preview_service gives a preview exactly that port), every other
  preview port rejected, now on ::1 too. Proof: `tests/e2e/preview_sandbox/proof.py`.
- **2b design**: `TEMPLATE_GATES` in `_NEVER_SHIP_PAST`, the advisory floor and
  `UNWAIVABLE_GATES`; `template_push_hold` + `_push_held(where)` before every push site
  (AST-enforced by `test_template_gates_block_the_pr.py`). Seen live: the template build's
  `checkpoint:write_tests` push was held (`code_task.push_held`), the PR opened after the gates
  passed.
- **Affected tests after the fixes** (preview suites, gate suites, sandbox): 369 passed; the
  failures are the base's own (13 on 9e2eca27: test_a_build_proves_its_app_runs x6 (inspect
  the old wrapper), test_sandbox_s1_go x3, test_every_surface_agrees (build_waits.py),
  test_previews_have_their_own_origin x3, now fixed) plus
  test_a_previews_container_log_reaches_the_activity_log (fixture scope clash when run in one
  session with apps/api tests; passes alone).
- Not run here: the web vitest suites (no node_modules in this worktree).
