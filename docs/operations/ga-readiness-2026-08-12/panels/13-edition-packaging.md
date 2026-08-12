# Panel 13 — Community vs Enterprise Packaging (GA)

**Date:** 2026-08-12  
**Panel:** Community vs Enterprise Packaging Expert Panel  
**Scope:** edition routers, composition-root ports, ConsoleShell / web honesty, marketplace & Account Connect gating, `make release-blockers`  
**Evidence roots:**  
- `apps/api/app/routers/community.py`, `enterprise.py`  
- `apps/api/app/bootstrap_ports.py`  
- `apps/api/tests/test_edition_routers.py`  
- `engine/boss_engine/services/commercial.py`  
- `packages/specpack/packs/{community,cloud,enterprise}_edition.yaml`  
- `packages/specpack/policies/commercial_packaging_policy.yaml`  
- `apps/web/app/ConsoleShell.tsx`, `pipeline-ui.tsx`, `provider-configuration-ui.tsx`, `lib/self-improvement.ts`, `lib/providerops.ts`  
- `scripts/check_external_rollout_readiness.py` (`make release-blockers`)  
- `docs/product/PRD-v2.0.md` (Commercial Strategy)  
- `docs/marketing/GTM_GAP_ANALYSIS.md`, `BOSS_OSS_ONE_PAGER.md`

---

## 1. Verdict (short)

| Question | Answer |
|---|---|
| **Can Community GA ship without Enterprise?** | **Yes.** Community is a coherent self-host SKU: durable control plane, local auth, no Account Connect requirement, paid routes unmounted (404). Enterprise/Cloud are **separate product GAs** with external hosted prerequisites. |
| **Can Enterprise/Cloud GA ship on Community alone?** | **No.** Cloud requires hosted Account Connect; Enterprise identity, marketplace, cluster, and support paths are fail-closed or `external_verification_required` until real services and operator config exist. |
| **Honest packaging claim for a Community-first GA?** | Ship **“BOSS Community: useful self-hosted governed execution + BYOK ProviderOps”**. Do **not** claim hosted SSO, marketplace, managed fleets, enterprise SIM, or hard multi-tenant spend enforcement as Community features. |

**Panel recommendation:** Gate **Community GA** on Community surface completeness + honesty (UI degrades cleanly; no half-workflows). Hold **Cloud / Enterprise GA** until Account Connect, identity, marketplace, and cluster checks move past blocked/external-only.

---

## 2. Edition model as implemented

### 2.1 Edition IDs

| Runtime `BOSS_EDITION` | Normalized commercial ID | Pack | Account Connect |
|---|---|---|---|
| `community` (default) | `community` | `community_edition.yaml` | **not required**; `connection_mode=disabled` |
| `cloud` | `cloud` | `cloud_edition.yaml` | **required**; status `blocked_account_connect_required` until linked |
| `enterprise` | `enterprise_self_hosted` | `enterprise_edition.yaml` | optional / detached |
| `enterprise_self_hosted` | same | same | detached allowed |
| `enterprise_cloud` | `enterprise_cloud` | same enterprise pack | required when treated as cloud-attached |

Paid editions for **route mounting** (`ENTERPRISE_EDITIONS`):  
`cloud`, `enterprise`, `enterprise_cloud`, `enterprise_self_hosted`.

### 2.2 HTTP partition (`mount_edition_routers`)

- Every HTTP `APIRoute` is classified at startup.  
- **Community router always mounts.**  
- **Enterprise router mounts only** when edition ∈ paid set.  
- Community process: paid paths are **absent → 404** (not soft 403 with fake success).

**Paid path prefixes (fail closed by omission in Community):**

```
/saml/
/scim/
/v1/account-connect
/v1/enterprise
/v1/marketplace
/v1/self-improvement
/v1/projects/*terraform-*
```

**Explicitly Community (full workflow, not half-gated):**

- Core work: code-tasks, events, decisions, skills, MCP, previews  
- Delivery / deploy targets / deployment runs (entire CI/CD registration + deploy path)  
- ProviderOps kernel: provider definitions/accounts, resources, ChangeSets approve/apply, receipts, worker pools, autoscaler plans, resource janitor, costs, drift, reconciliation  

Historical bug class encoded in tests: **do not split a workflow across editions** (e.g. store keys without ChangeSets, or deploy UI without target registration). That class must stay fixed for Community GA.

### 2.3 Composition root (ports)

With a database present, **every edition** gets:

- `PostgresPersistence`, `PostgresEventSink`, `PostgresDecisionSink`

**Paid only:**

- `PostgresBudgetPort`, `PostgresEntitlementPort`

**Community deliberately:**

- `NoopBudgetPort`, `AllowAllEntitlementPort` (user-supplied model)

This is intentional: free tier runs durable work; paid tier sells **governance enforcement**. Marketing must not claim Community has hard multi-tenant budget/entitlement broker enforcement identical to Enterprise.

Without any engine/DB (standalone OSS library path), in-memory defaults remain — that is **not** the Community control-plane product.

### 2.4 Commercial status API

`GET /v1/edition` (via `CommercialService.edition_status`):

- Community → `status=active`, `account_connect_required=false`, `local_auth_allowed=true`  
- Cloud unlinked → `status=blocked_account_connect_required` (fail-closed commercial posture, not silent free Cloud)  
- Capabilities from edition packs; paid packs inherit Community `includes` via `requires: [community_edition]`

Packs note: `cloud_edition.yaml` / `enterprise_edition.yaml` still carry `status: planned` for several managed capabilities — do not market those as generally available solely because routes exist.

---

## 3. Web / ConsoleShell packaging honesty

| Surface | Behavior | GA risk |
|---|---|---|
| **ConsoleShell nav** | Always lists **Enterprise** area (“Editions, identity, and account connect”) among Platform areas — **not edition-filtered** | Community users see enterprise chrome even when APIs 404 |
| **Self-improvement (Ops)** | Edition-aware: community marks `enterpriseOnly`, explains 404 vs outage | Good pattern — keep |
| **Pipeline custom profiles** | Non-enterprise editions show “Enterprise only” + presets path | Good pattern — keep |
| **Provider configuration** | Community banner: BYOK + full kernel; hosted SSO/marketplace/managed fleets paid | Aligns with routers after ProviderOps moved community |
| **Enterprise / marketplace / Account Connect panels** | Still rendered from commercial feed; unpaid edition routes missing | Risk of “visible but dead” Mission Control if feed errors look like bugs |

**Panel rule for Community GA UI:**  
Paid surfaces must either (a) be **hidden** on community edition, or (b) show a **single durable “Enterprise / Cloud only” state** with edition_id, never an empty error that looks like outage. Prefer (a)+(b) for Enterprise nav and marketplace widgets.

`lib/providerops.ts` documents that ProviderOps write automation is **community** (`isPaidWriteAutomationEdition` always true / deprecated). Do not reintroduce SKU-skip of ChangeSets.

---

## 4. Marketplace & Account Connect gating (must stay fail-closed)

### 4.1 Route level

On Community: `/v1/account-connect/*` and `/v1/marketplace/*` **not mounted**.  
No client should soft-succeed.

### 4.2 Service level (paid editions)

Account Connect adapter and marketplace sync:

- Missing `BOSS_ACCOUNT_CONNECT_URL` / not linked → **blocked/failed sync runs**, audit denial events, **no invented listings**  
- Health checks: missing hosted config → blocked record, not synthetic healthy  
- Cloud edition unlinked → edition status blocked; license capabilities reflect blocked state  
- Link/unlink audited; hosted org mismatch denials recorded  

### 4.3 Identity / enterprise runtime

- SCIM requires bearer tokens + rollout policy — fail closed without  
- SAML requires enable flag + policy — fail closed without  
- OIDC requires issuer/audience/JWKS when enabled  

These are **Enterprise/Cloud GA** gates, not Community GA gates.

### 4.4 Other external effects (edition-agnostic fail-closed)

Still required for any GA:

- Provider writes only via ChangeSets + receipts; missing credentials fail closed  
- Slack/email real send flags default false  
- Infra real provision flag default false  
- GitHub App community surface disabled without credentials  
- Bootstrap seed API default false  

---

## 5. `make release-blockers` interpretation

`scripts/check_external_rollout_readiness.py` is **not** a Community ship-stop for unpaid surfaces. It reports:

| Check ID | Role for packaging |
|---|---|
| `community_local_mode` | **Must be READY** for Community GA (default edition community, no hosted auth) |
| `hosted_account_connect_service` | Blocks **Cloud/Enterprise attached** claims |
| `hosted_marketplace_team_billing` | Blocks marketplace/team/billing GA claims |
| `enterprise_scim_runtime` / `enterprise_identity_runtime` | Blocks live SSO/SCIM GA claims |
| `external_support_provider_routing` | Blocks support-escalation claims |
| `self_hosted_package_artifacts` | Package files present (Kustomize/Helm) |
| `live_enterprise_cluster_rollout` | Always external verification — never claim live multi-node HA without recorded run |
| `providerops_write_rollout` | Real provision flags/tokens — external actuation readiness |

**Overall `BLOCKED` from release-blockers is expected** on a pure Community laptop without hosted services. That must **not** be misread as “Community cannot GA.” Map blocker checks to **SKU claims**.

---

## 6. What must stay fail-closed (non-negotiable)

### Community process

1. **Do not mount** paid prefixes when `BOSS_EDITION=community`.  
2. **Do not** swap durable Postgres persistence for in-memory on community control plane with a DB (known production footgun).  
3. **Do not** split BYOK ProviderOps or delivery workflows across editions.  
4. **Do not** invent marketplace listings, healthy Account Connect, or entitlement seats.  
5. **Do not** claim silent mock success for missing model/GitHub/provider credentials.  

### Cloud / Enterprise process

6. **Cloud unlinked → blocked**, not free Community with Cloud branding.  
7. **SSO/SCIM/OIDC** off until explicit config + policy.  
8. **Marketplace sync** blocked/failed when hosted path unavailable.  
9. **Live cluster / air-gap / multi-tenant HA** claims require external verification evidence, not package file presence alone.  
10. **Legal language:** do not call proprietary/source-available enterprise modules “open source” (`commercial_packaging_policy.language`).  

### Budget honesty

11. Community `NoopBudgetPort` means **no hard spend enforcement** at the port boundary — community may still have UI/budget tables, but GA copy must not promise Enterprise-grade hard caps unless enforcement is restored as a community feature deliberately.

---

## 7. Honest packaging claims matrix

### Claim now (Community GA-compatible **if** dogfood proves them)

| Claim | Basis |
|---|---|
| Self-hosted useful control plane without hosted BOSS auth | `edition_status` community active; PRD #14; release-blocker community_local_mode |
| Local auth allowed | commercial service + pack |
| Durable tasks, decisions, events, receipts, leased jobs | shared Postgres ports |
| BYOK model/infra keys + ProviderOps ChangeSets / worker pools | edition routers + providerops web copy |
| Governed code tasks → PR / delivery surfaces (community APIs) | community path classification tests |
| Paid commercial trust features omitted (404) | mount tests |

### Do **not** claim for Community GA

| Claim | Why |
|---|---|
| Hosted Account Connect / managed Cloud login | Cloud-only; fail-closed |
| Hosted marketplace catalog / billing portal / team invites as product | Hosted service + paid routes; pack status planned for many items |
| SSO/OIDC/SAML/SCIM production identity | Enterprise runtime + external verification |
| Enterprise self-improvement automation (`/v1/self-improvement/*`) | Paid mount only |
| Enterprise cluster / HA multi-node “validated live” | release-blockers always external |
| “Simple Mission Control is only CLI” **or** “full console is Enterprise-only” without nuance | Monorepo ships `boss-web`; OSS engine marketing still diverges (GTM G1) — pick one story |
| Community hard multi-tenant budget/entitlement enforcement identical to paid | `NoopBudgetPort` / `AllowAllEntitlementPort` |
| All PRD Enterprise modules “planned” as shipped | cloud/enterprise packs `status: planned` for large capability sets |

### Align PRD vs code before marketing freeze

| Topic | PRD / policy | Code today | Packaging action |
|---|---|---|---|
| ProviderOps write / janitor / autoscaler | Often listed as commercial / advanced | **Community kernel** | Update PRD/policy **or** re-gate (prefer keep community useful) |
| basic_budget_governor | Community pack includes | Noop budget port | Soft “budget visibility” vs hard caps language |
| Mission Control | Community “basic”; OSS one-pager “read-only” | Full console in monorepo | Ship scope: monorepo Community console vs extracted engine artifact |
| License | AGPLv3 **candidate**, legal review required | Not finalized | Block public “open source” superlatives until BOSS-300 / legal |

---

## 8. Can Community GA ship without Enterprise? — decision criteria

**Ship Community GA without Enterprise SKU when all of the following hold:**

1. **Community local mode READY** (`community_local_mode` + default `BOSS_EDITION=community`).  
2. **Edition router tests green** (paid routes absent; delivery + ProviderOps whole-workflow community).  
3. **Dogfood Community process** completes CEO→task→decision→receipt path without paid mounts.  
4. **UI honesty:** Enterprise-only panels labeled or hidden; no “dead button looks like bug” class.  
5. **Marketing pack locked** to Community matrix above; OSS engine claims match `engine/` extraction kit (licensing + SECURITY still hard gates for **public** engine flip — separate from internal Community GA).  
6. **External effects** remain fail-closed by default (providers, send, provision).  

**Do not require for Community GA:**

- Account Connect URL healthy  
- SCIM/SAML tokens  
- Marketplace sync green  
- Live multi-node enterprise cluster validation  
- Self-improvement proposal APIs  

Those are **Cloud / Enterprise GA entry criteria**.

---

## 9. Risks & open conflicts

1. **ConsoleShell always exposes Enterprise nav** → packaging mismatch / trust hit (see also PRD_CRITICAL_GAPS §8 historical “visible but dead”).  
2. **PRD commercial modules vs community ProviderOps write** — product must pick one fence or marketing will over- or under-sell.  
3. **GTM OSS one-pager** still lists local models + read-only Mission Control that may outpace or mismatch monorepo Community.  
4. **`make release-blockers` overall BLOCKED** confuses release owners; document SKU mapping (this panel).  
5. **License undecided** — Community GA for internal dogfood ≠ public open-source announcement.  
6. **Community NoopBudgetPort** — if dogfood spends real money via BYOK, operators need external caps; product should say so.

---

## 10. Recommendations (ordered)

1. **Declare two GA tracks:** Community GA (this panel) and Commercial GA (Cloud + Enterprise).  
2. **Freeze Community claims** from §7; rewrite any deck/one-pager that puts marketplace/SSO in free tier.  
3. **Edition-filter or badge** ConsoleShell Enterprise area + marketplace widgets on community.  
4. **Reconcile PRD §Commercial** with ProviderOps-in-community (prefer code: useful open core).  
5. Keep **fail-closed** list in §6 in release checklist; never “mock to green” for hosted paths.  
6. Treat **release-blockers BLOCKED** as normal for Community-only environments; fail CI only on `community_local_mode` + package artifacts for Community track.  
7. Hold **public OSS flip** on licensing/security kit (`engine/COMMUNITY_READINESS.md`), independent of internal Community GA.

---

## 11. Panel conclusion

**Community GA can ship without Enterprise** because the runtime already partitions paid surfaces by mount, keeps durable execution free, and requires no hosted Account Connect.  

**What must stay fail-closed:** paid route omission, Cloud-without-link block, marketplace/identity/provider external effects without config, and honest non-invention of hosted state.  

**Honest packaging:** Community = useful self-host + BYOK governed ops; Enterprise/Cloud = operational trust (identity, marketplace, managed attach, clustering, SIM automation, enforced budget/entitlements). Do not sell the second as free, and do not ship the first as a crippled demo.

---

## Evidence checklist (for release owners)

- [ ] `BOSS_EDITION=community` → `/v1/account-connect/*`, `/v1/marketplace/*`, `/v1/enterprise/*`, `/v1/self-improvement/*` absent  
- [ ] Community dogfood: code-task + decision + receipt without paid routes  
- [ ] ProviderOps ChangeSet apply works with operator BYOK (community)  
- [ ] `GET /v1/edition` community active, account_connect not required  
- [ ] Cloud unlinked still `blocked_account_connect_required` when `BOSS_EDITION=cloud`  
- [ ] Web: no silent 404 loops presented as API outage on paid panels  
- [ ] Marketing copy reviewed against §7  
- [ ] `make release-blockers` interpreted by SKU, not as single global red light for Community  
)
