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

## Stream S3-payments: `stripe-checkout` and `paypal-checkout` (F3 §3.2, §3.3)

Worktree `~/src/boss-wt-s3-payments`, branch `feat/s3-payments` (from
`feat/s2-templates`). Files only under `templates/apps/stripe-checkout/`,
`templates/apps/paypal-checkout/`, plus the engine suite
`engine/tests/templates/test_payments_templates.py` and this section.

Ids follow the stream brief (`stripe-checkout`, `paypal-checkout`); the spec's §3.2/§3.3 headings
say `payments-stripe` / `payments-paypal`. Rename in one place (the directory and `id:`) if the
operator prefers the spec's names, plus paypal-checkout's `works_with` and the engine suite's `PAYMENTS` tuple.

| Step | Status |
|---|---|
| stripe-checkout: manifest, brief, guide (Emergent/Lovable compared), SECURITY | done |
| stripe-checkout node (Express, stripe 23) | done: 16 tests |
| stripe-checkout python (FastAPI, stripe 16, SQLite) | done: 26 tests |
| stripe-checkout java (Spring Boot 3.4, stripe-java 34) | done: 18 tests (16 MockMvc + 2 Spring boot-up) |
| paypal-checkout: manifest, brief, guide, SECURITY | done |
| paypal-checkout node (Express, fetch client, no SDK) | done: 18 tests |
| paypal-checkout python (FastAPI, httpx, SQLite) | done: 25 tests |
| paypal-checkout java (Spring Boot, java.net.http) | done: 20 tests (18 MockMvc + 2 Spring boot-up) |

### What each template does (both, every stack)

- Prices only from the server (product table or the catalog-cart cart); client price/amount/currency
  ignored; quantities whole 1-99; integer minor units throughout.
- Two idempotent fulfilment paths: Stripe = success page retrieves the session + signed webhook;
  PayPal = server-side capture (buttons or return page) + PayPal-verified webhook. Both work on a
  private preview without inbound webhooks (D4); the guides document `stripe listen` and a tunnel.
- Webhooks: Stripe `constructEvent` (HMAC over the raw body, 300 s tolerance); PayPal
  verify-webhook-signature bound to `PAYPAL_WEBHOOK_ID`. Event ids recorded once (primary key)
  with the order change. Order state machine shared; amount mismatch or money for a cancelled
  order -> `needs_review`; the "ship it" hook runs once.
- Test mode: Stripe keys must match `sk_test_`/`pk_test_` (manifest patterns, and the app refuses a
  live key unless `STRIPE_MODE=live`); PayPal uses the sandbox host unless `PAYPAL_ENV=live` and
  `APP_ENV=production` (PayPal credentials have no shape, so no manifest pattern: a loose one would
  make the secret scan flag ordinary identifiers).
- Refunds optional: off by default, admin hook (default deny), recorded amount, idempotency key.
- catalog-cart contract coded against spec §3.4: Node `catalog.cartLineItems(cartId, req)`, Python
  `catalog.cart_line_items(cart_id, request)`, Java `Catalog.cartLines(cartId, request)`, each
  returning `[{product_id, name, unit_amount_cents, currency, quantity}]` or null/empty for a cart
  the requester does not own. If catalog-cart ships different names, only the adapter changes.

### Tests (S3-payments)

| Template / stack | Tests | Command (from the stack dir) |
|---|---|---|
| stripe-checkout node | 16 | `npm run test:template` (pretest runs `npm install`) |
| stripe-checkout python | 26 | `python3 -m pytest -q tests/template_stripe --junitxml=boss-template-junit.xml` |
| stripe-checkout java | 18 | `mvn -q test -Dtest=Stripe*` |
| paypal-checkout node | 18 | `npm run test:template` |
| paypal-checkout python | 25 | `python3 -m pytest -q tests/template_paypal --junitxml=boss-template-junit.xml` |
| paypal-checkout java | 20 | `mvn -q test -Dtest=PayPal*` |
| engine `test_payments_templates.py` | 29 + 6 real-run + 6 strict xfail | see Resume |

Every stack covers: signature verification with a secret generated in-test (Stripe: HMAC computed
in the test; PayPal: the fake's verify API with a per-test signing key), forged / edited /
unsigned / stale (Stripe) webhooks refused, replayed event processed once, tampered client price
ignored, capture/fulfilment idempotent, amount mismatch held, refunds, provider outage. Each
stack's signature check was mutation-tested (disabling it fails the webhook tests).

### Findings for other streams (S3-payments)

- **S2 (apply)**: `build_directive` inlines the reference in path order up to 48 KB and lists the
  rest by name. The payments references are 49-81 KB per stack, so the `tests/` files (which the
  agent must copy verbatim for `template_tests`) are the ones left out, and the
  `/opt/boss/template/` mount is not built. Fix: inline `tests/` first (or mount the reference).
  `test_the_directive_inlines_every_template_test_file` is a strict xfail that flips when fixed.
- **S2 (trial)**: the trial runs the manifest command in a bare copy. Node installs its deps
  (`pretest:template`), Maven resolves its own; Python needs `requirements.txt` installed first,
  which a single sandbox command cannot do. Either the trial installs `requirements.txt` for the
  python stack, or the image carries fastapi/httpx/stripe/pytest.
- **Spec naming**: the spec's §3.2/§3.3 call them `payments-stripe` / `payments-paypal`; the stream
  brief said `stripe-checkout` / `paypal-checkout`, which is what shipped.
- **Live PayPal egress**: the manifest requests only `api-m.sandbox.paypal.com`; going live needs
  `api-m.paypal.com` approved with the live-keys decision (documented in the guide).

### Resume (S3-payments)

- `cd ~/src/boss-wt-s3-payments && git pull` (branch `feat/s3-payments`).
- All six stacks are done and pushed. Remaining work belongs to other streams (findings above),
  plus the F3 end-to-end proof (§3.8) once catalog-cart and the S2 console land.
- Engine suite (no database needed): `PYTHONPATH=.:engine python3 -m pytest -q engine/tests/templates`.
  Add `BOSS_TEMPLATE_REAL_RUN=1` to run each stack's real tests through the gate (needs npm; a
  `python3` on PATH with `stacks/python/requirements.txt` installed, e.g. a venv first on PATH;
  mvn + Maven Central).
- Run a stack's tests by hand in a COPY (never in the template dir: `node_modules/` or `target/`
  there would break the validator's file limits): copy `stacks/<stack>/` to a scratch dir, then the
  manifest's `tests.<stack>.command`.
- Never paste a real key; tests build key-shaped values at run time (`"sk_test_" + random`), so the
  secret scan stays clean (a literal `sk_test_<alnum>` in a template file fails validation).
- The API suite (`apps/api/tests/test_templates_api.py`) keys on `hello` by name, so the new
  built-ins do not change it; it was not re-run here (it needs a test Postgres).
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
| 5 | `catalog-cart` python (FastAPI, 12 tests); tests of both templates build their app from the template modules, not main.py/server.mjs, so they survive adaptation and run together (27 in one app per stack, checked) | done |
| 6 | `catalog-cart` java (Spring Boot 3.5.16, 12 tests, JDK 21 via docker) | done |
| 7 | engine test `engine/tests/templates/test_builtin_templates_apply.py`: every built-in x stack validates (also as an import), its directive assembles, a build copying the reference passes `template_tests` + `no_committed_secrets`, test mode needs no pasted secret; `BOSS_TEMPLATE_TRIALS=1` runs the REAL test commands through the real gate (node + python green locally; java via docker) | done |

### Design notes and deviations (S3-login-catalog)

- **One HTTP contract per template, identical tests in every stack** (login 15, catalog 12),
  written against the manifest's routes. Each test suite builds its app from the template's own
  modules (not `server.mjs`/`main.py`), never imports `conftest`, and uses unique file names, so
  both templates' tests run together in one app (checked: 27 per stack) and survive adaptation.
  A scratch composition check (node, not committed) signed in through the fake IdP with the real
  session hook and saw the anonymous cart merge.
- **`BOSS_OIDC_CLIENT_ID/SECRET` are named `OIDC_BOSS_CLIENT_ID/SECRET` (+ `OIDC_BOSS_ISSUER`)**:
  S2's validator reserves the `BOSS_` prefix for BOSS's own configuration. **S6 must fill the
  `OIDC_BOSS_*` names.**
- **Node uses openid-client 6 (panva, OpenID-certified) + express-session, not Auth.js**: Auth.js's
  fixed `/auth/signin` POST + its own CSRF/session model do not fit the contract's routes; the
  operator allowed "a proven equivalent". Python: Authlib 1.8 with a small server-side session
  middleware (Starlette's SessionMiddleware is a readable signed cookie: the PKCE verifier would
  sit in the browser). Java: Spring Security OAuth2 Client with PKCE forced on confidential
  clients, a one-shot authorization-request repository and lazy client registrations.
- **Fake IdPs are our own, in each stack's tests** (Node `http`, Python `http.server` +
  cryptography, Java `com.sun.net.httpserver` + Nimbus), not `oauth2-mock-server` /
  `mock-oauth2-server`: one server plays OIDC (Google/BOSS) and GitHub's OAuth shape and VERIFIES
  PKCE, client secrets, redirect_uri and one-time codes; tests can tamper the nonce.
- Account linking is stricter than "verified email": BOTH the new identity and the existing
  account must have the email verified (otherwise a pre-created unverified account could be
  linked into).
- Spring Boot **3.5.16** (Boot 4.x exists; 3.5 kept for the known Security 6 API). The java login
  stack does not use `SESSION_SECRET` (container session ids).
- Not built: the Playwright axe smoke on the Node catalog UI (UI is reference components only:
  `ui/Catalog.jsx`, `ui/LoginButtons.jsx`, framework-free `account.html`/`catalog.html`).

### Findings for other streams (S3-login-catalog)

- **S2 trial (`trial.py`) runs the test command with no dependency install.** Node is covered (the
  templates' `pretest:*` npm script installs), Maven fetches its own, but the Python commands
  need `pip install -r requirements.txt` first (the sandbox image has fastapi/httpx from BOSS's
  own requirements, not authlib). Suggest a per-stack install step in the trial (and the gate)
  before the manifest command.
- **The reference is inlined up to 48 KB**: the java stacks exceed it, so some files reach the
  agent by name only until the read-only `/opt/boss/template/` mount exists (S2's deviation).
- Java and Python test commands need network (Maven Central / PyPI) inside the sandbox unless the
  toolchain image pre-seeds them.

### Resume (S3-login-catalog)

`cd ~/src/boss-wt-s3-login-catalog && git pull`; pick the first step not `done`.
Run a stack's tests in a scratch COPY of `stacks/<stack>/` (never in the template dir: a
`node_modules`/`__pycache__` there breaks the tree validator's limits):
node `npm run test:login-oauth` (Node 20: `docker run node:20-bookworm-slim`); python
`pip install -r requirements.txt && python3 -m pytest -q tests/template_login_oauth` (3.11:
`docker run python:3.11-slim-bookworm`); java `mvn -q -B test -Dtest=LoginOauthTemplateTest` (JDK 21:
`docker run -v <copy>:/w -v <scratch>/m2:/root/.m2 -w /w maven:3.9-eclipse-temurin-21 ...`; the
local JDK is 17).
Engine: `PYTHONPATH=.:engine python3 -m pytest -q engine/tests/templates` (179 pass, no DB);
add `BOSS_TEMPLATE_TRIALS=1` with a python3 on PATH that has the template requirements to run
the real test commands through the real gate (node + python: 6/6 green; java ran via docker).
Next: payments templates consume `checkoutLines`/`checkout_lines` (catalog-cart brief); S6 fills
`OIDC_BOSS_*`; an independent tester walks apply -> preview for both templates once the gallery
(S2-UI) is in.
## Stream S3-banking: `banking-starter` and `banking-starter-insecure`

Branch `feat/s3-banking` (worktree `~/src/boss-wt-s3-banking`), from
`feat/s2-templates` @ 54f32a91. Files: `templates/apps/banking-starter/`,
`templates/apps/banking-starter-insecure/`, `engine/tests/templates/test_banking_templates.py`.

| # | Step | State |
|---|---|---|
| 1 | `banking-starter` (Java 21, Dropwizard 4.0.17, JDBI 3 + SQLite, Argon2id via BouncyCastle, opaque expiring bearer tokens): 22 JUnit tests; validator OK; real gate run green | done |
| 2 | `banking-starter-insecure` (label `insecure_by_design`, `vulns.yaml` V1-V12), 15 functional JUnit tests (no exploit tests); validator OK (an import needs template:approve + ack); real gate run green | done |

### Notes (S3-banking)

- **Run the Java tests** (no local JDK 21): `docker run --rm -v <stack dir>:/w -v boss-s3-m2:/root/.m2
  -w /w -e JAVA_HOME=/opt/toolchains/java/jdk boss-s3-tcjava:dev /opt/toolchains/bin/mvn -B -ntp verify`
  (the image's `mvn` needs `JAVA_HOME`; `java` is not on its PATH). Then `rm -rf <stack dir>/target`:
  a local `target/` (jars > 2 MB) makes the built-in fail validation, and `sync_builtins` then
  silently skips it.
- **Engine test**: `PYTHONPATH=.:engine python3 -m pytest -q engine/tests/templates/test_banking_templates.py`
  validates both built-ins, assembles the java directive, and runs each template's real
  `mvn -B -ntp verify` through the real `template_tests` gate (in the image above, or a local
  JDK 21 + mvn; skipped when neither; `BOSS_TEST_JAVA_IMAGE` overrides the image).
- **Design**: opaque 256-bit tokens (SHA-256 stored, TTL `BANK_TOKEN_TTL_SECONDS`, revoked on
  logout and on password change for other sessions), not JWT, so no signing secret: the manifest
  declares no secrets. Ownership = `WHERE id = :id AND user_id = :user` (404 for not-yours).
  Movements run in SQLite `BEGIN IMMEDIATE` transactions (`SQLiteConfig.TransactionMode`), so
  two concurrent withdrawals cannot overdraw (tested, 5 rounds). Jackson coercion is turned off
  for integers (10.5 and "10" refused). Admin connector on 127.0.0.1:0. Preview: `port:
  ${PORT:-8080}` + `SubstitutingSourceProvider`, `boss.preview.health` = `/health`; verified the
  shaded jar binds `$PORT` in the image.
- **Finding for S2 (apply)**: `MAX_INLINE_REFERENCE` is 48 KB and `build_directive` inlines the
  reference in sorted order, stopping at the first file that does not fit. The banking reference
  is ~84 KB, so `src/main/java/bank/ledger/...` onward and every `tests/` file are listed as
  "Not inlined (too large)". The build agent then cannot copy the template's tests, and
  `template_tests` fails closed. Fix in S2: the read-only reference mount at `/opt/boss/template/`
  (spec §2.3), or inline `tests/` first and raise the cap. Not changed here (S2's file).
- **Finding for S4 / deploy**: nothing yet refuses a host deploy of an app whose applied templates
  carry `insecure_by_design` (spec §3.6 says a hard check in `governed_host_deploy.py`). Until it
  lands, the label only gates import (validator).

- **The training variant**: same routes as the starter plus `GET /` (red "Insecure by design —
  training only" banner) and an `X-Insecure-By-Design` header on every response. Planted flaws
  V1-V10 are the spec's table (§3.6; V3 is unsalted MD5 as the spec says), plus V11 (no auth on
  `GET /api/accounts/{id}/entries`, CWE-306) and V12 (JWT without `exp`, logout/password change
  revoke nothing, CWE-613). V9 is `commons-text` 1.9 (CVE-2022-42889), used only for HTML
  escaping on the banner page. `vulns.yaml` gives each flaw's locations (file, symbol, line,
  match), CWE/OWASP, detectors (semgrep / zap_baseline / zap_active / dependency_audit /
  tester_app / review) and the fix as references into `banking-starter`. The engine test checks
  every `line`/`match` against the code and every fix reference exists, so **when the code moves,
  update `vulns.yaml`'s lines**. The answer key is at the template root, so `build_directive`
  (brief + `stacks/java/`) never inlines it (tested). **Deviation from §3.6**: the spec's
  "exploit test per V-id" was NOT written (operator instruction 2026-10-08: functionality tests
  only; detection is verified by BOSS's scanners against `vulns.yaml`).

### Resume (S3-banking)

`cd ~/src/boss-wt-s3-banking && git pull` (branch `feat/s3-banking`). Check the
step table above; rerun the docker `mvn verify` per stack and the engine test.
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
| 3 | Browser walk against a local dev server + local API, screenshots in `docs/product/app-platform/s4wf-*` | done |
| 4 | Compose e2e: vulnerable app scan → panel → choice → fix → re-scan → re-review | not run (no free compose slot: `bossint` was up) |

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

3. The walk (2026-10-09). Local API (`uvicorn apps.api.app.main:app`, `BOSS_CONSOLE_AUTH=network`,
   own database) + `next dev` with `BOSS_API_URL`/`BOSS_CONSOLE_AUTH=network`, opened at
   `http://[::1]:<port>` (localhost and 127.0.0.1 carried other stacks' session cookies, which turn
   the network bridge off). Data: `boss-dogfood`, app `vuln-shop`; the scans are the REAL recorded
   tool output of `tests/e2e/security/vulnapp` / `vulnapp-fixed` through the real parsers and
   storage (`service.execute_scan` with the tool seam), and both reviews ran through the REAL
   runner — only the panel (a model) and the build were fakes landing finished rows. Result:
   review A passed in round 1 (5/5 targeted fixed, 0 new; round 2 "not needed"); review B waited
   at Choose, the checklist recorded the person's ticks through the API, the re-scan and
   re-review passed, the report showed d1a1af2 → cab0549, 20 → 5, Markdown export through the
   console proxy. Walk findings fixed in 3c76e432 (portal sheet, phone height, decided checklist
   read-only, a security panel is not a PRD, input styling). Screens: `s4wf-01..06` desktop
   (summary + checklist, ticks, review passed, report), `s4wf-07..09` at 390 px (forced via a
   390 px backdrop: the Chrome window cannot go below 614 px; the ≤640 px rules are active and
   nothing overflows), `s4wf-10` the checklist in the Meetings view.
   Seen, not fixed: approving the gate from the checklist answered 403 "usr_network_operator is
   not an active member" — the unclaimed install's network bridge cannot approve decisions; a
   signed-in member can (the API test proves the selection route per role; approval is the
   existing `/v1/decisions/{id}/approve`). The meeting hero's stage strip still reads
   "PRD · Build" for a security panel.

4. Not run. The loop as built needs, on a compose stack: `BOSS_TOOLCHAINS=security` (the S4 proof
   image), a model for the panel (or "Skip and proceed" on the panel: the checklist then holds
   only derived recommendations) and for the fix build (the governed code task), and the app
   registered as a build (scan_proof does it as a LOCAL workspace). With no model the honest
   proof is: start `security_review` → scan (real tools) → skip the panel → choose → the fix
   build cannot run without a model. The workflow wiring itself is proven by
   `test_security_review_workflow.py` (real runner) and the walk above (real recorded scans).

### Resume notes (S4-workflow)

- `cd ~/src/boss-wt-s4ui && git pull` (branch `feat/s4-workflow`); land by PR
  onto `feat/app-platform` after `feat/s4-security`.
- Next: (a) the compose e2e when a slot is free — extend `tests/e2e/security/scan_proof.py` with a
  `--review` mode that starts `security_review` through `POST /v1/delivery-workflows/start`,
  needs a connected model for the panel and the fix; (b) an independent tester walks App ›
  Security, the checklist (Meetings and App), the decision and the report on a claimed install;
  (c) the `vulns.yaml` detection score and F5 tester results in the report (after F3/F5);
  (d) a link from the fix build's cockpit to its review's report; (e) the meeting hero stage
  strip for security panels.
- Decisions taken here: reviewer preset holds `security_scan:run`; the panel is convened by `pm`
  and chaired (seated first) by the security lens; two bounded rounds drawn as steps; "removed
  with its code" counts as gone for the verdict, "moved" does not.
- Re-run: `PYTHONPATH=.:engine pytest engine/tests/test_security_review_workflow.py
  engine/tests/test_every_template_runs_end_to_end.py`; API (test DB)
  `apps/api/tests/test_security_review_routes.py`; web `cd apps/web && vitest run
  tests/app-security.test.tsx`. Test DB: `boss-test-pg` localhost:55432, user boss, a private
  database (this stream used `boss_test_s4wf`, dropped).
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

> **Integration note for S6 (open item, not changed in S6's code):** the `login-oauth` template
> reads BOSS as a provider from `OIDC_BOSS_CLIENT_ID`, `OIDC_BOSS_CLIENT_SECRET` and
> `OIDC_BOSS_ISSUER` (a template may not ask for `BOSS_*`: reserved). When an app registers as a
> BOSS client, S6 must hand the app its client settings under exactly those names (the secret as
> an app secret binding, never in the build). Also from integration: `oidc:read` now gates the
> admin GETs (see the spec's decisions), and the maintenance tick rotates the `oidc` family.

Worktree `~/src/boss-wt-s6-oidc`, branch `feat/s6-oidc` (from `feat/app-platform`
@ 8b7cc39d). Engine: `engine/boss_engine/oidc_provider/`; routes: `apps/api/app/oidc_routes.py`.

| # | Step | State |
|---|---|---|
| 1 | Protocol core (discovery, JWKS, authorize, consent, token, userinfo, revocation, end_session), client registry API, workforce pools on the console session | done |
| 2 | Unit, admin API and adversarial suites (the named list) | done |
| 3 | End-to-end proof: a Node app with openid-client on a local stack | done |
| 4 | OpenID conformance suite (Basic OP), if Docker allows | not run (see below) |
| P2.1 | Customer pools: hosted sign-in/sign-up/email code/TOTP/passkeys/reset/account pages, pool-user API, web proxy | done |
| P2.2 | Re-authentication for prompt=login/max_age, step-up for require_mfa | done |
| P2.3 | Console Identity ▸ App sign-in | done (independent tester walk: todo) |
| P2.4 | Conformance (if a stack slot is free) | blocked by decision (see below) |

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
- Phase-1 limits (prompt=login/max_age answered `login_required`, require_mfa as an error page,
  customer pools unavailable) are lifted by phase 2 (below).
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

### Phase 2.1: customer pools (S6)

- `oidc_provider/customers.py` is the realm: pool users (`opu_`, no identity_principals row), sessions
  (`oidc_sessions`, cookie `__Host-boss_app_session`, `bas_` tokens, 12 h idle / 7 d absolute, a NEW
  session at every sign-in and MFA), challenges (`oidc_app_challenges`, new table, bound to
  `__Host-boss_app_preauth`, 5 attempts, 10 min), Argon2id + the fair hash queue + new-password rules
  (passwords.py), TOTP via totp.py's constant-time ±1-step matching with last-step replay
  prevention (secret in the vault), passkeys via py_webauthn with identity's `rp_for` (RP ID = the
  BOSS host, user handle = pool user id, credential looked up in THIS pool only), emailed 6-digit
  codes (email_signin.DELIVER) for verification and reset, rate limits and soft lock in `oidc.*`
  buckets (AP-5). Every lookup names the pool.
- Deviation from §6.3: a `__Host-` cookie must have `Path=/`, so the app cookies are `Path=/`
  (host-only, Secure, HttpOnly, Lax). They are read only by the OIDC module; the console proxy
  forwards them only on /oidc/ browser steps; identity's authn never reads them.
- `oidc_provider/hosted.py`: pages and steps. `GET /oidc/{org}/account?view=signin|signup|forgot|
  account` and `POST /oidc/{org}/hosted/{step}` (signin, signup, verify-email, mfa, enrol-totp,
  forgot, reset, passkey-options, passkey-verify, account; wf-reauth for workforce). Steps check
  Origin + Sec-Fetch-Site; JSON steps need application/json; account actions need an HMAC of the app
  session. Pages: strict CSP with a nonce for the one passkey script, no framing.
- `protocol.py` is pool-agnostic now (`Who`): customer pools sign in on the hosted pages;
  `pool.mfa = required`, `client.require_mfa` or `acr_values=urn:boss:acr:mfa` force a second
  factor (enrolment if none). Signup: open, or domain; invite/closed = admin invite only (the
  invitee sets a password through "Forgot password"). `password_policy.verify_email` (default true)
  needs mail; `min_length` (default 12).
- Pool-user API (`oidc_pool_user:manage`): list, invite, PATCH status/reset_mfa/name; disable and
  MFA reset end sessions, challenges and every grant.
- Tests: `apps/api/tests/test_oidc_provider_customers.py` (26): the flow, then C1 a pool user is
  never a principal (no row; app cookie, app session as console cookie, as bearer, customer
  AT/ID token on `/v1/identity/me` and `/v1/projects` under enforce: 401; a workforce client never
  takes an app session; no BOSS or cross-client role), C2 cross-pool/org (session, challenge,
  same email in two pools, admin API from another org and on a workforce pool: 404, pairwise
  subjects), C3 anti-enumeration (same page for unknown email and wrong password; taken email at
  sign-up: same page, no mail, no verifiable code) and lockout, C4 TOTP replay and 5-strike
  challenge, password alone never finishes an MFA pool, C5 passkeys (register on the account page,
  sign in with the passkey alone; forged user handle, another pool, another origin, register
  without a session: refused), C6 CSRF on steps and account actions, framing/CSP, C7 consent revoke
  on the account page, disable, reset by code ends sessions, invite + MFA reset, viewer 403, C8 a
  new session at every sign-in.

### Phase 2.2: re-authentication (S6)

- Workforce: `prompt=login`, an exceeded `max_age`, or `require_mfa`/`acr_values=urn:boss:acr:mfa`
  with a session that has no second factor -> the hosted "Confirm it's you" page. The step is
  identity's own `signin.step_up` (TOTP with its replay guard; the password only for a member with
  no factor and only when MFA is not required) or `signin.passkey_options/verify` with purpose
  `step_up`; it rotates the console session (the new cookie is relayed) and the request continues
  with `since` = the interaction's creation, so only a confirmation made after the request counts.
  `prompt=none` with a stale session answers `login_required`/`interaction_required`.
- A workforce session counts as MFA only when it has a second-factor method (totp, passkey,
  webauthn, recovery_code, sso), not merely a `mfa_at` stamped by a password step-up: acr
  `urn:boss:acr:pwd` then.
- Customers: the same triggers show the hosted sign-in again; MFA-required asks the second factor
  (or enrolment).
- Tests: `apps/api/tests/test_oidc_provider_reauth.py` (R1 confirm with TOTP rotates the session
  and issues a fresh auth_time, R1 the same TOTP code cannot confirm twice, R2 max_age incl.
  prompt=none, R3 require_mfa refuses a password, R4 password route for a member with no factor
  gives acr pwd, R5 CSRF and no session).

### Phase 2.3: the console (S6)

- `apps/web/app/identity/app-sign-in/` (page + `app-sign-in.tsx`), registered in
  `lib/identity/pages.ts` (tab "App sign-in", access `anyScope: oidc_client:manage`), styles in
  `app/identity/identity-app-sign-in.css`. Tabs Pools · Clients · Users · Roles; rows open side
  sheets; register shows issuer, client id and secret ONCE in a locked sheet; rotate secret;
  disable/remove; pool create/edit/delete; invite, disable/enable, reset MFA; roles add/assign/
  remove. The admin reads are step-up permissions, so the lists load through `useGuardedCall`
  (the step-up sheet opens on first load). Recommendation (needs a catalogue amendment): an
  `oidc:read` (Auditor, Admin, Owner; no step-up) for the lists.
- Proxy: `lib/oidc-proxy.ts` forwards `account` and `hosted/<step>` as browser steps (console
  session + app cookies + Origin/Sec-Fetch-Site) and relays only the identity Set-Cookies.
- Tests: `apps/web/tests/identity-app-sign-in.test.tsx` (6), `oidc-proxy.test.ts` (5); all 35
  identity web test files pass; tsc clean. Not yet done: an independent tester's browser walk.

### Phase 2 results (2026-10-09)

- API suites after the phase-2 refactor: flow, admin, adversarial, day-1 routes, public routes,
  engine oidc + frozen interfaces: 193 passed, 1 skipped, after two expectations changed on
  purpose (require_mfa now steps up instead of a 403 page; a workforce pool has no pool users:
  404). Customers 26/26, re-authentication 6/6. Web: identity (35 files) and proxy tests pass.
- End-to-end (`tests/e2e/oidc/run.sh`, local API + console, through the console origin): the
  workforce proof (`E2E PASS`) and the customer-pool proof (`E2E CUSTOMER PASS`: hosted sign-up,
  TOTP enrolment, consent, MFA acr and pairwise sub verified by openid-client, userinfo, refresh,
  prompt=login with password + next TOTP, same sub, the account page disconnects the app, the
  app's next refresh fails).
- Not done: the passkey ceremony in a real browser (covered by API tests with a soft
  authenticator), the independent tester's walk of Identity ▸ App sign-in and the hosted pages,
  conformance (below).

### Phase 2.4: conformance (S6) — not run, needs an operator decision

The slot was free (2026-10-09) and the suite has prebuilt images (`docker-compose-prebuilt.yml`),
but the OpenID **Basic OP** plan (`oidcc-basic-certification-test-plan`, code flow,
client_secret_basic) sends no PKCE in every test but one (`oidcc-ensure-request-with-valid-pkce-
succeeds`) and requires a code flow WITHOUT a nonce to succeed (`OP-nonce-NoReq-code`); it also
requests `address` and `phone` scopes. §6.2 makes PKCE S256 mandatory for every client and nonce
mandatory for `openid`, and BOSS refuses unknown scopes; so nearly every module would fail at
authorize by design. Running it is only meaningful after one of: (a) a per-client "OIDC
certification compatibility" switch (PKCE optional for confidential clients, nonce optional, unknown
scopes ignored), off by default and Admin-only; or (b) accepting that BOSS is not Basic-OP
certifiable and running the suite's individual modules that match the profile (discovery, ID token
signature/kid, userinfo GET/POST header/body, code reuse, redirect_uri not registered, valid PKCE,
end_session). Decision for the operator (suggested D14).

### Resume (S6)

- `cd ~/src/boss-wt-s6-oidc && git pull` (branch `feat/s6-oidc`).
- Tests: DB `boss_test_s6` on boss-test-pg (localhost:55432, user boss; password via
  `docker exec boss-test-pg printenv POSTGRES_PASSWORD`, never printed). Run with
  `DATABASE_URL=postgresql+psycopg://boss:$PW@localhost:55432/boss_test_s6 PYTHONPATH=.:engine python3 -m pytest -q apps/api/tests/test_oidc_provider_*.py`.
- DB `boss_test_s6` is DROPPED after each session: `CREATE DATABASE boss_test_s6` first.
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

The integrator merged every stream branch into `feat/app-platform`, made the must-fixes as
classes with tests, and proved the result on one compose stack. Test DB: `boss_test_integ` on
boss-test-pg (dropped at the end). Every step was committed and pushed as it finished.

| # | Step | Commit | State |
|---|---|---|---|
| 1a | Merge S2 templates (54f32a91): clean. templates suites 156 passed, identity suites 46 | cd5fa17b | done |
| 1b | Merge S4 security (bae890a7): day-1 test rewritten (MODULES + BUILT). S4 suites 64 | 2c09632f | done |
| 1c | Merge S5 links (5caa424c). S5 suites 65 | e7a4480b | done |
| 1d | Merge S6 OIDC phase 1 (9123ee29): audit.EVENT_TYPES union, matrix regenerated. S6 suites 164 | 96b9f84f | done |
| 2a | Previews run as their own sandbox uid, fenced, own port only | 57386907 | done |
| 2b | template_tests / no_committed_secrets BLOCK: nothing pushed, no PR | b5f7d5fb | done |
| 2c | Dockerfile.community ships templates/ | 634382f5 | done |
| 2d | A template larger than the brief reaches the build whole, read-only (BOSS_TEMPLATE_DIR) | c0efc7cb | done |
| 2e | No host deploy of an app built from an insecure_by_design template | 54471528 | done |
| 2g | Dependencies installed before template tests (gate + trial); re-apply with tests already on the branch passes; the trial runs as its uid | 8f94cdca | done |
| 1e | Merge S3 banking (555d1908), login-catalog (88ab92c0), payments (b9870e74): doc conflicts only; the payments strict xfail removed (2d fixed it) | bf7aafcd, f95569a2, 2e3a4b1e, a6fb141b | done |
| 2j | catalog-cart provides the payments contract exactly (cartLineItems / cart_line_items / cartLines); compose test per stack | 39da0001 (branch fix/catalog-payments-contract) | done |
| 2k | Spec names the shipped ids stripe-checkout / paypal-checkout | 84a32001 | done |
| 1f | Merge S4-workflow (e81de02b): apps-tab.tsx union (Links + Security), generated files regenerated | f4651228 | done |
| 2s | "Fix selected" is one server action (approval checked first, saved + approved in one commit) | 5ee506d5 | done |
| 1g | Merge S6 phase 2 (5b28fd4b): every OIDC route built (day-1 test: all BUILT) | 06cb1838 | done |
| 2m | `oidc:read` (org, no step-up; Owner/Admin/Auditor) for every OIDC admin GET | f83bee52 | done |
| 2n | The maintenance tick rotates the apps' `oidc` keys | 5e0ef3fb | done |
| 2h | S6 note: the login template reads OIDC_BOSS_CLIENT_ID / _SECRET / _ISSUER (in S6's section) | a0ca2797 | open for S6 |
| 3 | Combined compose proof (project `bossint`, image from 634382f5 + the port fixes) | — | PASS (below) |

### The combined proof (compose project `bossint`, 2026-10-08/09)

Stack: `scratchpad/integ/e2e/` (`mkenv.py` 0600 env, ports 53760/58760/55860/58761/58790/58791;
`dc.sh` = compose -p bossint with community-compose + build-override; `setup.py up|down`). NO
templates mount: the built-ins come from the image.
- **Image**: `builtin_dir()` = `/app/templates/apps`, `hello` loads valid; sandbox
  `{mode: required, helper: true, network_fenced: true, isolating: true}`.
- **Apply hello** (`tests/e2e/templates/apply_hello_walk.py`): PASS. The app's first build, then
  `POST /v1/templates/hello/apply` → governed build: `template_tests` 2/2 from JUnit,
  `no_committed_secrets` 0 findings, PR on the fake GitHub with the template's tests and no
  JUnit report. The template build's `checkpoint:write_tests` push was HELD
  (`code_task.push_held`); the PR opened after the gates passed. A second `--resume` apply of
  the same template then failed `template_tests` ("tests not in the change") and pushed NOTHING
  (the hold, live); that gate case is fixed since (2g: tests already on the branch count).
- **Preview sandbox** (`tests/e2e/preview_sandbox/proof.py`): PASS, twice in a row. An evil
  preview ran as uid 61390 on its own port 19390: master.key, server.key, the identity dir,
  /proc/<api>/environ, /proc/1/environ, /proc/<api>/mem, another preview's directory and the
  sandboxes root → PermissionError; the DB, another preview's port on loopback and on the api
  address → ConnectionRefused; its own port and egress open; BOSS still reached it; teardown left
  no process and no directory. A REAL preview of the applied build through
  `POST /v1/code-tasks/{run}/preview` ran as uid 61801 on port 19801 (the other live preview
  61750/19750); as 61801: master.key and /proc/1/environ "Permission denied", the other preview
  refused, its own port open.
- **S5 links** (`tests/e2e/links/proof.py --keep`): PASS (B signs in to A through the link; every
  way around the gateway refused; revocation cuts B off in 0.09 s).
- The stack was taken down with `down -v`. Only one stack ran at a time.

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
- Web: the full vitest suite after every merge (node_modules symlinked from the main checkout,
  not committed): 5908 passed; the App sign-in screen needed the grant-limit scan to tell app
  roles from BOSS roles (2d885469). `deploy-drawer-polls-only-while-live` fails only in the full
  run (timing), passes alone.
- **Other findings fixed on the way**: the template trial ran the template's code as the API's uid
  (owner kind not in WORKLOAD_KINDS; owned_process now wraps whenever a sandbox identity is
  bound); `POST /v1/delivery/deploy` answered a GovernedHostDeployError (budget refusal, the
  insecure guard) with a 500 (now 409 in words); the insecure banking built-in failed the
  import-parity test without the approver's acknowledgement its label requires.
- **Found, NOT fixed (pre-existing on main)**: the Infra Deploy button for an app target calls
  `api_main.delivery_deploy_endpoint(body)` without its `request` argument (TypeError, 500) since
  00399f5f; `test_one_deploy_button_routes_by_target_kind` fakes the endpoint so it never shows.

### Final gate (2026-10-09, fresh `boss_test_integ`)

- API suites the integration touched (templates, security scan + review routes, links + gateway,
  deploy guard, oidc:read, oidc key rotation, preview origin, deploy button, build actions,
  advisory gate, day-1 routes): 145 passed, 2 failed only in the combined session and pass
  alone (the deploy-guard suite saw another suite's template applications: its fixture now
  clears them, 0c507e9a; `test_stop_is_instant_while_the_worker_holds_the_run`: timing).
- Engine suites (templates incl. the compose test, security review workflow, link rules/fence,
  previews sandbox, F1 sandbox, name-twice, security scan engine, oidc, gate labels, manifest):
  451 passed, 55 skipped (opt-in real runs / Docker).
- Identity coverage suites: 46 passed. S6 suites after phase 2 + oidc:read: 198 passed.
- Web: full vitest 5908 passed (see notes).

### Decisions for the operator

- `oidc:read` is Owner/Admin/Auditor only: an Operator who manages a project's clients can no
  longer LIST or READ them (grant `oidc:read` to Operator if wanted).
- The insecure-template deploy guard is per PROJECT (a target names its app freely): any
  application of an insecure_by_design template (not abandoned) blocks every host deploy of that
  project.
- Payments wiring: an unsellable cart raises catalog-cart's CatalogError; the app's glue must map
  it to the payment template's CheckoutInputError (else a 500 from the payment route, no payment
  made). Alternative: the payment adapters map catalog errors themselves.
- The template reference is a directory of the run (`$BOSS_TEMPLATE_DIR`), not the spec's
  `/opt/boss/template/` mount (no per-run mount namespace in the sandbox).

### Resume (integration)

- `cd ~/src/boss-wt-appplat && git pull` (branch `feat/app-platform`).
- Tests: create `boss_test_integ` on boss-test-pg; the runners are in the session scratchpad
  `integ/` (`t.sh` = pytest against it, `id.sh` = identity suites, `final.sh` = every suite the
  integration touched). Regenerate after a route or catalogue change: `PYTHONPATH=engine python3
  -m boss_engine.identity.codegen`, `PYTHONPATH=.:engine python3 -m apps.api.tests.identity_matrix`,
  `python3 apps/api/tests/test_identity_authz_public_routes.py`, `python3
  scripts/dump-identity-api-routes.py`, `cd apps/web && node scripts/read-routes.mjs`.
- Open items: S6 to emit OIDC_BOSS_* for the login template (2h); the Infra Deploy button bug
  above; the template tests in microVM mode (the gate fails closed there); the composer's
  "Talks to" (S5); S2's MCP tools; the independent tester walks (§8.3) of App ▸ Links, App ▸
  Security and App sign-in.


## Finish (feat/app-platform-finish)

Branch `feat/app-platform-finish` (worktree `~/src/boss-wt-finish`), from
`feat/app-platform` @ f9982f7e. Items (h), (o) here; (p), (q), (r), (s) built on their own branches
(`feat/app-platform-finish-<item>`, worktrees `boss-wt-finish-<item>`) and merged in. Test DB
`boss_test_finish` on boss-test-pg (dropped at the end). No compose stack was used.

| Item | What | Commit | State |
|---|---|---|---|
| (o) | The insecure-template deploy guard is per APP | 4a1407b8 | done |
| (h) | Sign in with BOSS for a login-oauth app: client, redirect URIs, OIDC_BOSS_* app secrets, one click | H_SHA | done |

### (o) The deploy guard is per app (4a1407b8)

`templates/deploy_guard.deploy_refusal(connection, project_id, *, target, commit_ref, release_packet,
source_run_id)`: the operator's decision replaces "per project". In a project that holds an
insecure_by_design application (not abandoned), a deploy is refused when any app it can be
attributed to is the insecure one (the target's `app_name` by key/name/run id, and by its normalised
key even when its builds are past the 200-run app index; the release packet's `code_task_run_id`; a
stack deploy's `run_id`; a `boss/code/<run>` branch ref), when it cannot be attributed to any app of
the project (a target named after nothing BOSS built) or names a build/branch BOSS cannot place, and,
in the insecure app's own repository, when its change is merged (`applied`) or the ref is one BOSS
cannot place (a bare sha, an unknown branch). The default branch while the insecure change is
unmerged and another app's own build branch deploy. Fails closed; each refusal carries its own
action. Every planner and the governed host apply pass the deploy's context (source test pins it).
Tests: `apps/api/tests/test_an_insecure_template_app_is_never_deployed.py` (20: every way of naming
the insecure app, what BOSS cannot attribute, a shared repository before/after merge, an unknown
repository, the host route deploying the other app past both planners).

### (h) Sign in with BOSS for a login-oauth app (H_SHA)

- **One click**: `POST /v1/apps/{app}/sign-in-with-boss {project_id, pool_id?}` (`oidc_client:manage`,
  step-up, plus `app_secret:write` checked in the handler; the app named by key, name or run id).
  `engine/boss_engine/oidc_provider/app_sign_in.py`: needs a login-oauth application on the app (409
  in words otherwise); registers a confidential `client_secret_basic` client (scopes openid profile
  email roles; not first-party, so the person consents) in the org's first workforce pool (an
  Owner/Admin's click creates one if none exists; an Operator's gets 409 `no_pool` in words), and
  binds `OIDC_BOSS_CLIENT_ID`, `OIDC_BOSS_CLIENT_SECRET`, `OIDC_BOSS_ISSUER` as app secrets
  (`templates.secrets.bind_issued`: vault kind app_secret, mode generated) for **preview and deploy,
  never build**. Idempotent: a second click rotates nothing; a client registered by hand is reused
  and its secret reissued (BOSS cannot read the shown-once secret back). `GET` (project:read) shows
  presence, client id, issuer and redirect URIs; `DELETE` removes the client (its grants end) and the
  bindings. No response ever carries the secret.
- **Redirect URIs** = `<base>/auth/callback/boss` for each running preview
  (`<preview origin>/v1/previews/<id>/web`; BOSS_PREVIEW_ORIGIN, else the public URL's host on
  BOSS_PREVIEW_PORT) and the app's deploy addresses (newest healthy deploy's public URL, its targets'
  public_url / https domains). `preview_service.preview_link_environment` now also adds
  `preview_app_environment`: the app's `preview` secrets (F2 §2.4, never live; only for apps with a
  template applied), `APP_BASE_URL`, and the new preview's redirect URI; `stop_preview` removes it.
  URIs an operator added stay. With no address yet, `http://localhost:3000/...` keeps the client
  valid until a real one exists. OIDC client `app_key` now accepts the platform's app keys
  (lowercase words, single-spaced).
- **Console**: Factory › Apps › ⋯ › **Sign-in** (`apps/web/app/factory/sign-in-sheet.tsx`,
  `sign-in-actions.ts`): state, client, issuer, "Returns to" in words, *Use Sign in with BOSS* /
  *Refresh addresses* / *Stop using*. Needs a rebuild of the web container to be visible.
- **Found and fixed by the proof**: the login-oauth Node stack sent the BOSS client secret in the
  body (openid-client's default, client_secret_post), which BOSS refuses: BOSS sign-in could never
  have worked. It now uses `ClientSecretBasic` for BOSS, and the stack's fake IdP accepts only Basic
  for the BOSS client (mutation-checked: the old code fails the template test). Python (Authlib) and
  Java (Spring) already use Basic.
- **Tests**: `apps/api/tests/test_sign_in_with_boss.py` (16), ending with the proof: the login-oauth
  Node stack (npm-installed into a scratch copy) runs with EXACTLY the environment a preview of the
  app gets from the vault, BOSS serves on a real loopback port; GET /auth/login/boss -> BOSS authorize
  (PKCE S256, nonce, state) -> a member's consent -> the app's callback -> code exchange with the bound
  secret -> `/api/me` = the member, provider boss; the secret never in the app's output. Skips only
  without Node 20+ or npm. `apps/web/tests/app-sign-in-sheet.test.tsx` (6). Node template tests 15/15.
  Affected: identity suites 46, OIDC engine + admin + flow + oidc:read + links + previews sandbox +
  name-twice 104 (+1 skip), templates API 45. tsc clean.
- **Not done / known limits**: (1) a sign-in **inside a preview cannot hold a session**: the preview
  proxy strips the browser's Cookie header and serves previews with a sandbox CSP (no
  allow-same-origin), so the app's session cookie never reaches it; the preview redirect URI is
  registered, but an interactive login works on a deployed or locally run app only. Fixing it is a
  preview-isolation decision (per-app preview origins), not this item. (2) Deploys do not yet read app
  secrets (`values_for_env(env="deploy")` has no caller in the deploy path), so a deployed app gets
  the bindings only once that wiring exists. (3) The Node stack builds its callback URL from
  `req.originalUrl` against APP_BASE_URL, which drops a path prefix such as a preview's; harmless
  until (1) is solved.
