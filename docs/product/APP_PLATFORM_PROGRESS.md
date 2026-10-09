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
