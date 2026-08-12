# Panel 03 — ProviderOps / BYOK / Resource Plane

**Date:** 2026-08-12  
**Scope:** Community self-host GA readiness for ProviderOps kernel, BYOK registration, inventory, ChangeSet/receipt path, and secret hygiene  
**Edition under test:** `community` (self-host / dogfood)  
**Primary sources:** `apps/api/app/routers/community.py`, `enterprise.py`, `engine/boss_engine/services/resource_plane.py`, `secret_refs.py`, `secrets_service.py`, `secret_backends.py`, `apps/api/tests/test_edition_routers.py`, `test_provider_credentials.py`, web `lib/providerops.ts` + provider UI, live gate/probe under `docs/operations/ga-readiness-2026-08-12/`

---

## Verdict

| Question | Answer |
|---|---|
| **Community self-host GA ready for ProviderOps/BYOK?** | **CONDITIONAL YES (code + live mount: GREEN; first-run UX + catalog completeness: YELLOW)** |
| **Does community mount the full ProviderOps kernel?** | **Yes** — not paid-only |
| **Safe for GA as “bring your own keys and manage your own cloud”?** | **API kernel yes; operator onboarding still friction-heavy** |

**One-liner:** Community correctly exposes the full Resource Plane / ProviderOps surface (catalog → accounts → inventory → ChangeSets → receipts → pools/janitor). Security posture for “no raw secrets in APIs” is strong. GA blockers are **easy key registration UX**, **infra-only connection catalog**, **default write-provisioning off**, and **dogfood loop completeness**—not edition gating.

---

## 1. Edition mount: full kernel vs paid-only

### Design intent (explicit in code)

`enterprise.py` documents the commercial boundary clearly:

- **Community (free / self-host BYOK):** ProviderOps kernel end-to-end — catalog, provider accounts (model + infra API keys as SecretRefs), inventory sync, ChangeSets (plan/approve/apply + receipts), worker pools, autoscaler, Resource Janitor.
- **Paid only:** Account Connect, marketplace, SSO/SAML/SCIM, enterprise cluster/compliance, self-improvement automation, and paths matching `/v1/projects/*` + `terraform-`.

```text
_ENTERPRISE_PREFIXES =
  /saml/, /scim/, /v1/account-connect, /v1/enterprise,
  /v1/marketplace, /v1/self-improvement
+ terraform- under /v1/projects/
```

`community.py` partitions every HTTP route: non-enterprise → `community_router`; paid prefixes → `enterprise_router` only when edition ∈ `{cloud, enterprise, enterprise_cloud, enterprise_self_hosted}`.

### Community-classified ProviderOps surface (tests)

`test_edition_routers.py` asserts **not** enterprise for the full workflow, including:

| Surface | Paths |
|---|---|
| Catalog | `/v1/provider-definitions` |
| Accounts | `/v1/providers`, `…/test`, `…/rotate`, enable/disable/revoke |
| Inventory | `/v1/resources`, `/v1/resources/sync` |
| Receipts | `/v1/resource-receipts` |
| ChangeSets | `/v1/changesets`, `…/approve`, `…/apply` |
| Fleets | `/v1/worker-pools`, `/v1/autoscaler/plans` |
| Janitor | `/v1/resource-janitor/run` |
| Secrets | `/v1/secret-refs` |
| Health / FinOps signals | `/v1/provider-health`, `/v1/costs`, `/v1/drift-reports`, `/v1/reconciliation-runs` |
| Deploy (related) | `/v1/deployments*`, `/v1/deployment-targets*`, policies, runs |

Real community app bootstrap test asserts those routes are **mounted** and paid routes (`account-connect`, `enterprise/clusters`, `marketplace`, `self-improvement`) are **absent**.

### Live dogfood (same day)

| Probe | Result |
|---|---|
| `GET /v1/providers` | **200** |
| `GET /v1/secret-refs` | **200** |
| `GET /v1/changesets` | **200** |
| `GET /v1/worker-pools` | **200** |
| `GET /v1/provider-definitions` (gate) | **200**, count=5 |
| providers list (gate) | **200**, count=4 |
| Account Connect / SIM | **404** (expected paid) |

**Note:** Older ops notes (`LIVE_UI_FUNCTIONAL_AUDIT_2026-08-11`, `PRD_CRITICAL_GAPS_2026-08-12`) claimed ProviderOps was enterprise-gated on community. **That is stale relative to current `main` + today’s live probe.** Treat those docs as historical failure mode (half-gated workflow), now fixed in code.

### What is *not* on community (by design)

- Hosted Account Connect OAuth linking
- Marketplace listings
- Enterprise cluster / compliance packs
- Self-improvement automation API
- Terraform-named project paths (`terraform-` substring under `/v1/projects/`) — includes drift-reconcile style routes that match the heuristic

### Port composition vs kernel

`bootstrap_ports.py`: community still gets **Postgres persistence/events/decisions**. What is no-op is **engine-level** `NoopBudgetPort` + `AllowAllEntitlementPort`.  

Resource Plane **does not** use that no-op port for ChangeSets: it owns a real `BudgetGovernor` and enforces `BudgetReservation` on spendy apply. That is correct for self-host financial control and does **not** mean ProviderOps is paid.

---

## 2. Easy key registration (BYOK)

### What works

| Path | Status | Notes |
|---|---|---|
| Register SecretRef metadata | **Ready** | `POST /v1/secret-refs` — kinds `env|vault|local_runner|platform_secret_store` |
| Write-through secret upload | **Ready (API)** | `POST /v1/secrets` → backend put + metadata (`last4`, fingerprint); never returns value |
| Keypair generate | **Ready (API)** | `POST /v1/secrets/keypair` — private to backend, public only in response |
| Verify ref resolves | **Ready** | `POST /v1/secrets/{id}/verify` |
| Model credential upload | **Ready** | `POST /v1/entitlements/sources/{id}/credential` write-through |
| Bind model SecretRef | **Ready** | `…/credential-ref` |
| Provider account create | **Ready** | `POST /v1/providers` with `credential_refs` as `kind:key` pointers |
| Rotate with overlap | **Ready** | `POST /v1/providers/{id}/rotate` + lineage |
| GitHub easy-connect | **Ready (pattern)** | env SecretRef + connection one-shot for GH |

### Friction / gaps (GA)

1. **Provider UI is pointer-only, not paste-and-connect.**  
   `provider-configuration-ui.tsx` collects `credential_ref__*` as password fields with placeholder **“SecretRef pointer”** and copy “Secret material never enters BOSS.” Operators must already have `env:BOSS_…` (or vault/platform ref) prepared. There is **no** provider-account equivalent of model credential write-through or `/v1/secrets` + auto-bind.

2. **Password inputs for env var names** invite operators to paste raw tokens; API correctly **422**s (`env` keys must match `[A-Z_][A-Z0-9_]*`, raw metadata rejected). Security correct; UX hostile for first-run BYOK.

3. **Default write backend is `platform_secret_store`.** Without ops secret store configuration, community self-hosters fall back to **env** (read-only put) or **file** backends—documented in `secret_backends.py` / secrets design. Install docs must state the matrix clearly.

4. **Connection catalog is incomplete vs runtime adapters.**  
   `list_provider_definitions` only lists `PROVIDER_CONNECTION_PROFILES`: **aws, azure, digitalocean, gcp, ovhcloud** (5).  
   `SUPPORTED_PROVIDERS` also includes **cloudflare, supabase, openai, anthropic, ssh_host** with real skills, but they are **not** in the definition catalog → Mission Control connection picker under-represents first-class BYOK targets. API can still create accounts for unprofiled providers via generic `api_token` / primary slot fallback.

5. **Planned adapters advertised:** Azure/GCP/OVHcloud accept SecretRefs with `pending_adapter` / test → `unsupported`—honest, but must not look “connected” for GA marketing.

6. **Real writes default-off:** `BOSS_INFRA_REAL_PROVISION_ALLOWED` defaults **false**. Inventory/test/sync can work; droplet/DNS/etc. actuation stays fail-closed until operator opt-in—good safety, must be in runbook.

**GA bar for “easy BYOK”:** not fully met. Kernel registration is expert-grade; not “paste OpenAI + DO token and go” from the console alone.

---

## 3. Inventory

| Capability | Community? | Assessment |
|---|---|---|
| Resource list | Yes | `/v1/resources` |
| Inventory sync | Yes | `/v1/resources/sync` (rate-limited as costly endpoint) |
| Provider health | Yes | `/v1/provider-health` (+ poll) |
| Drift reports | Yes | `/v1/drift-reports` |
| Reconciliation runs | Yes | `/v1/reconciliation-runs` |
| Costs / anomalies | Yes | `/v1/costs` (+ anomaly paths in kernel) |
| Web feed | Yes | `fetchProviderOpsFeed` no longer edition-skips ChangeSets/pools |

**Live:** providers list non-empty (4 accounts on dogfood gate). Inventory value still depends on credentials resolving in the process environment / backend and adapters being `available`.

**Partial:** planned cloud providers do not sync live inventory. Cloudflare/OpenAI/etc. work via adapters but weak catalog/UI discovery.

---

## 4. ChangeSet → receipt path

Policy (`resource_plane_policy.yaml`):

- writes require ChangeSet  
- spendy writes require BudgetReservation  
- ResourceReceipt + dedupe_key  
- approval for destructive; rollback for production DNS/deploy  
- env policy for non-sandbox deploy  

Runtime (`ResourcePlaneService`):

1. **`create_changeset`** — validates ops, optional reservation → `quoted` / `budget_reserved` / `pending_approval`; records expected receipt dedupe keys; quotes via BudgetGovernor.  
2. **`approve`** — explicit transition.  
3. **`apply_changeset`** — requires `approved`; if `expected_cost_usd_micros > 0`, **active reservation required** or blocking decision (`provider_changeset_missing_reservation`); per-op ResourceReceipt with **dedupe_key**; settle/release budget; events for block/success/fail.  
4. **`list_resource_receipts`** — community.  

Supported write skills (non-exhaustive): DO droplet create/destroy + app deploy; Cloudflare DNS/R2/Worker/Pages; Supabase project create/pause + migration; AWS slice (EC2/S3/RDS/IAM/Route53); ssh_host deploy/teardown—each behind ChangeSet + receipt patterns in tests/CHANGELOG.

**Community assessment:** full actuation path is mounted and tested. GA risk is **operator configuration** (budget policy/reservations, real-provision flag, secrets resolving, read_only / destructive toggles on accounts)—not a missing route.

---

## 5. Security: no raw secrets in APIs

| Control | Evidence | Grade |
|---|---|---|
| SecretRef metadata-only storage | `SecretRefService` columns; `raw_secret_stored: false` | **Strong** |
| Reject sensitive metadata keys | `_SENSITIVE_METADATA_KEYS` + recursive validation | **Strong** |
| Reject literal tokens as credential_ref | `<kind>:<key>`; env key charset check | **Strong** |
| Provider connection_metadata sanitized | same validator | **Strong** |
| List/get SecretRef never returns material | API + OpenAPI language | **Strong** |
| Write-through upload returns last4/fingerprint only | `secrets_service.put_secret`, model credential upload | **Strong** |
| Events omit values | `secret.written` / `secret_ref.created` metadata only | **Strong** |
| Runtime resolve at use only | `secret_backends` / env resolve; not persisted on receipt paths | **Strong** |
| Actor gate | `providerops_manage` on SecretRef/provider mutations | **Adequate** (depends on member/RBAC wiring in deploy) |
| Agents must not receive raw secrets | SpecPack policy + role safety_gates (`no_raw_secrets`) | **Policy present** |

**Residual risks (not GA-blockers for API design, but ops notes):**

- Env backend means secrets live in process environment; process dumps / mis-logged debug could leak outside API contracts.  
- File backend must stay 0600 under `BOSS_SECRETS_DIR` (enforced).  
- `put_secret` inserts SecretRef via raw SQL (bypass of some service validators)—still sets `raw_secret_stored: false` and does not return value.  
- UI password fields are a social-engineering footgun for pasting real tokens into logs (server rejects; client may still log form errors poorly—watch client logging).

**Verdict on security for community GA:** **pass** for “no raw secrets in control-plane APIs.”

---

## 6. Web / Mission Control honesty

- `providerops.ts`: `paidWriteAutomationAvailable` hard-true; comment states community mounts full kernel; feed failures are partialFailures, not silent edition 404.  
- Provider configuration UI: connections / resources / health / FinOps views; create/test/sync forms.  
- Historical bug class (UI fan-out 404 → `apiReachable=false`) is **addressed in code**; live routes return 200. Re-verify SSR after deploy when auditing UI.

---

## 7. Missing / incomplete for community self-host GA

### Must-fix before calling ProviderOps GA “easy BYOK”

| # | Gap | Severity |
|---|---|---|
| 1 | **One-shot provider key onboarding:** paste token → write-through backend → create provider account + test connection (mirror model credential / GitHub easy-connect) | **P0 UX** |
| 2 | **Expand `PROVIDER_CONNECTION_PROFILES`** to include cloudflare, supabase, openai, anthropic, ssh_host with truthful auth slots | **P0 product truth** |
| 3 | **Self-host install runbook:** env matrix, `BOSS_INFRA_REAL_PROVISION_ALLOWED`, budget seed for spendy ChangeSets, platform_secret_store vs env | **P0 docs/ops** |
| 4 | **UI copy:** SecretRef pointer fields must not look like “paste API key”; show default env names from catalog | **P1** |
| 5 | **E2E dogfood demo script:** register → test → sync inventory → plan ChangeSet → approve → apply → ResourceReceipt visible | **P1 evidence** |
| 6 | Refresh stale gap docs that still claim enterprise-gated ProviderOps | **P2 docs** |

### Acceptable post-GA (paid or later)

- Azure/GCP/OVH live adapters (already `planned` honestly)  
- Hosted Account Connect  
- Vault backend polish  
- Terraform enterprise packaging  
- Managed multi-tenant ProviderOps  

### Explicit non-goals for community GA

- Silent external writes without ChangeSet  
- Raw secret round-trip APIs  
- Auto-apply production infra without approval/rollback  

---

## 8. Traceability matrix

| Requirement | Community status |
|---|---|
| Resource Plane kernel is core (not OpenClaw) | **Met** — core module + policy |
| Provider adapters are plugins | **Met** — adapter matrix + planned statuses |
| Every provider write is a ChangeSet | **Met** — apply path |
| Every provider action has ResourceReceipt + dedupe | **Met** |
| Spendy actions need BudgetReservation | **Met** (BudgetGovernor, even on community edition) |
| SecretRef only; no raw secrets to APIs/agents | **Met** (API/tests/policy) |
| Community BYOK full workflow (not half-gated) | **Met** (routers + tests + live 200s) |
| Easy first-run key registration | **Partial** |
| Complete inventory for advertised providers | **Partial** (catalog vs SUPPORTED_PROVIDERS skew) |

---

## 9. Panel conclusion

**Community self-host GA readiness for ProviderOps/BYOK: CONDITIONAL GO.**

- **GO** on architecture and edition packaging: full ProviderOps kernel is free; paid surface is trust/hosting apparatus only.  
- **GO** on security model for secrets-at-rest-in-API.  
- **GO with conditions** on operator experience and catalog completeness.  
- **Do not** market “click-connect all clouds” until easy write-through provider onboarding + full definition catalog land.  
- **Do** market “self-hosters can register SecretRefs, inventory, and actuate their own cloud through audited ChangeSets and receipts without a paid SKU.”

**Recommended GA statement:**

> Community includes the full ProviderOps Resource Plane kernel (providers, SecretRefs, inventory, ChangeSets, receipts, worker pools, janitor). Credentials are reference-only; raw secrets are not stored or returned by control-plane APIs. Operators bring keys via env or write-through secret backends and must enable real provisioning explicitly. Easy paste-and-connect for every provider is still a hardening item.

---

## Evidence index

- `/Volumes/DataSSD1/test/boss/apps/api/app/routers/enterprise.py` — community BYOK comment + paid prefixes  
- `/Volumes/DataSSD1/test/boss/apps/api/app/routers/community.py` — edition mount partition  
- `/Volumes/DataSSD1/test/boss/apps/api/tests/test_edition_routers.py` — full kernel community classification + real app path asserts  
- `/Volumes/DataSSD1/test/boss/apps/api/tests/test_provider_credentials.py` — raw secret rejection, rotation lineage, planned adapters  
- `/Volumes/DataSSD1/test/boss/engine/boss_engine/services/resource_plane.py` — ChangeSet/receipt/budget  
- `/Volumes/DataSSD1/test/boss/engine/boss_engine/services/secret_refs.py` / `secrets_service.py` / `secret_backends.py`  
- `/Volumes/DataSSD1/test/boss/packages/specpack/policies/resource_plane_policy.yaml`  
- `/Volumes/DataSSD1/test/boss/docs/operations/ga-readiness-2026-08-12/live_probe.txt` + `live_dogfood_gate.json`  
- Web: `apps/web/lib/providerops.ts`, `apps/web/app/provider-configuration-ui.tsx`
