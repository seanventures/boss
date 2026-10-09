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

## Stream S4-workflow: the security review workflow and its screens (F4 §4.2, §4.6; S4-UI)

Branch `feat/s4-workflow` (worktree `~/src/boss-wt-s4ui`), from `feat/s4-security`
@ bae890a7.

| # | Step | State |
|---|---|---|
| 1 | `security_review` catalogue workflow + six capabilities + runner wiring + verdict rules + tests | done |
| 2 | `/v1/security/reviews*` API; App › Security, checklist, before/after report (console) + vitest | done |
| 3 | Browser walk against a local dev server, screenshots in `docs/product/app-platform/` | in progress |
| 4 | Compose e2e: vulnerable app scan → panel → choice → fix → re-scan → re-review | not started |

### Notes per step

1. The workflow (`engine/boss_engine/services/workflow_catalogue.py`, key `security_review`, the 13th
   entry): `scan → panel → decide → fix → rescan → rereview → fix_2 → rescan_2 → rereview_2 → report`.
   Capabilities (`workflow_capabilities.py`): `security_scan` (tool `run_security_scan`, new, also
   reachable from chat; awaits the `security_scans` row), `security_panel` (tool `request_meeting`
   with new `seats` = security/engineer/architect and `purpose=security_review`, which raises no
   PRD decision in `meeting_service._finalize_meeting`), `security_decide` (gate; its checklist is
   `security_recommendations`, written by `security_review_steps.ensure_recommendations` before the
   gate opens: the Panel's fenced `security_recommendations` JSON, then a derived one per finding
   the Panel left uncovered; P0/P1 pre-ticked unless the Panel called it a false positive),
   `security_fix` (tool `create_code_task` with `extends_run_id` = the app's newest build, brief =
   chosen recommendations + their findings; round 2 = what the re-scan still found),
   `security_rereview` (no tool; `review.rereview_verdict`: PASS only when every targeted finding is
   `fixed`/`removed` or justified by a person's triage, and no new high/critical; `moved`,
   `still_open`, `not_rescanned` fail), `security_report` (artifact `security_report`). The loop is
   bounded and drawn as steps: round 2's steps answer `not_applicable` ("not needed") when round 1
   passed; a round-2 failure fails the step (settled) and the operator decides. Step ids are named
   literally in inputs (`"after": "fix"`). Decision: the reviewer Passport preset gains
   `security_scan:run` (sentry scans/re-scans); the panel is convened by `pm` (planner holds
   `meeting:convene`), seated with the security lens first. Report fix: a finding with no
   `snippet_key` is never paired as "moved".
   Tests: `engine/tests/test_security_review_workflow.py` (25: verdict rules, panel parsing,
   coverage, digest envelope, brief; the real runner through pass, changes-asked→round 2, final
   fail, new-high, default ticks, clean scan, choice refused after the fix starts). Shared tests
   updated for the 13th workflow / new tool / new capabilities. Known failures on the base (not
   from this work): `test_a_refused_gate_blocks_the_run_and_cannot_be_skipped[*]`,
   `test_an_agent_step_whose_approved_run_fails_stops_the_run`,
   `test_every_door_that_starts_a_run_checks_readiness_first`, 7 in
   `test_agent_behaviour_matrix.py`, `test_bundled_definitions_templates_and_examples_match_source`,
   `test_every_call_with_a_project_passes_the_project_roster`, `test_the_run_loads_them_from_its_own_spec`,
   `test_every_meeting_title_reader_uses_meeting_title`, `binding.role_id` in
   `test_every_step_field_naming_an_agent_guards_its_delete`.

2. API (`apps/api/app/routes_security.py`): `GET /v1/security/reviews?project_id&app_key`,
   `GET /v1/security/reviews/{run}` (steps, recommendations, the findings they cite),
   `POST /v1/security/reviews/{run}/selection` `{selected, accept:[{finding_id, reason}]}`
   (`security_finding:triage`; refused once the fix started). Tests:
   `apps/api/tests/test_security_review_routes.py` (7). Console (`apps/web/app/factory/security/`):
   Factory › Apps › ⋯ › **Security** opens App › Security (`?security=<app id>`; `/v1/factory/apps`
   rows now carry `app_id`); the checklist also renders in the Meetings view for a security panel
   (`meetings[].security_review.run_id`). "Fix selected" = record the ticks, then approve the gate
   (`/v1/decisions/{id}/approve`). Tests: `apps/web/tests/app-security.test.tsx` (10, incl. viewer
   vs operator gating). Not built: the `vulns.yaml` detection score and F5 tester results in the
   report (need F3's insecure template and F5); a cockpit link from the fix build to the report.
