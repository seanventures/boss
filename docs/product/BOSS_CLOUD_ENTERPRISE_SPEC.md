# BOSS Cloud & Enterprise product specification

**Status:** Active implementation baseline (2026-08-12)  
**Marketing source:** https://boss.sean.network/#cloud · `#enterprise` · `#opensource`  
**Plane:** BOSS-337 / commercial packaging · Account Connect (GAP-002)  
**Runtime editions:** `community` · `cloud` · `enterprise_cloud` · `enterprise_self_hosted`

---

## 1. Product principle

| Layer | Independence | How it connects up |
|---|---|---|
| **Community (open / self-host)** | **Fully independent.** BYOK models + infra, local Postgres, Mission Control, Software Factory, ProviderOps kernel, decisions/receipts/budgets. **No Account Connect required.** | Optional **Easy Connect** to BOSS Cloud for managed capabilities |
| **BOSS Cloud** | Hosted control plane + managed workers/infra. Requires Account Connect org link when `BOSS_EDITION=cloud`. | Operators use hosted Mission Control; self-host nodes may attach later |
| **Enterprise** | Cloud or self-host. Adds SSO/SCIM, HA/cluster, policy packs, premium support. Account Connect optional on self-host, required on enterprise_cloud. | Design partnership / sales |

**Hard rule:** Community never soft-depends on Cloud. Missing Cloud URL must not break local work. Connecting to Cloud is opt-in via Easy Connect.

---

## 2. Marketing → capability map (BOSS Cloud)

| Site claim (#cloud) | Capability key | Runtime status | Notes |
|---|---|---|---|
| Hosted Mission Control | `hosted_mission_control` | **partial** | Cloud edition + hosted deploy; self-host MC is Community |
| Durable task & decision state | `durable_control_plane` | **shipped** | Community & Cloud (local PG vs managed PG) |
| Managed workers & elastic infra | `managed_workers` | **planned/runtime-flag** | Requires Cloud link + worker fabric |
| Team collaboration | `team_collaboration` | **partial** | Team invites via Account Connect when linked |
| Model & skill routing | `managed_model_gateway` | **partial** | Local entitlement broker Community; managed gateway Cloud |
| Connectors & integrations | `managed_connector_gateway` | **partial** | Marketplace + AC connector refs when linked |
| Approvals & policy management | `approvals_policy` | **shipped** | Kernel shared; Cloud adds hosted policy packs |
| Evidence retention & support | `evidence_retention_support` | **partial** | Receipts shipped; retention SLAs Cloud/Enterprise |

---

## 3. Marketing → capability map (Enterprise)

| Site claim (#enterprise) | Capability key | Runtime status |
|---|---|---|
| Private & local model endpoints | `private_model_endpoints` | partial (BYO base URL / keys) |
| Bring-your-own provider accounts | `byok_providers` | **shipped Community+** |
| Model & tool allowlists | `model_tool_allowlists` | partial |
| Workspace & project controls | `workspace_project_controls` | shipped |
| Budgets & execution limits | `enterprise_budget_controls` | partial (real BudgetPort on paid editions) |
| Policy-based escalation | `policy_escalation` | partial |
| Role & skill permissions | `rbac_skill_permissions` | shipped baseline |
| Human approval gates | `human_approval_gates` | shipped |
| Receipts & evidence trails | `receipts_evidence` | shipped |
| Hybrid local & cloud workers | `hybrid_workers` | planned |
| SSO / SCIM | `sso_oidc_saml`, `scim` | **enterprise routes** (fail-closed until configured) |
| Clustering / HA | `ha_clustering` | planned / ops profiles |

---

## 4. Community independence contract

Community **must** work with:

- `BOSS_EDITION=community`
- `BOSS_ACCOUNT_CONNECT_URL` **unset**
- Local API keys / env SecretRefs
- No paid route mounts (account-connect, marketplace, SAML/SCIM, self-improvement automation)

Community **may**:

- Call `GET /v1/cloud/catalog` (discover Cloud features)
- Call `GET /v1/cloud/status` (independence + link state)
- Call `POST /v1/cloud/easy-connect` (start browser or device auth to Cloud)
- After link, sync entitlements and unlock **optional** Cloud-linked capabilities without losing local runtime

Community **must not**:

- Block CEO command / code-task / budget / GitHub / ProviderOps because Cloud is unlinked
- Mount broken Enterprise forms as required settings

---

## 5. Easy Connect (Community → Cloud)

### 5.1 Browser flow (one click)

1. Operator opens Mission Control → **Editions / Cloud** → **Connect to BOSS Cloud**
2. Control plane `POST /v1/cloud/easy-connect` `{ method: "browser", return_url }`
3. Creates auth session (receipted) → returns `authorization_url`
4. Operator completes hosted login
5. Callback `POST .../complete` with code/state → link org → sync entitlements

### 5.2 Device flow (really easy on headless/self-host)

1. Operator starts Easy Connect with `method: "device"`
2. API returns `user_code` + `verification_uri` + `device_code` + poll interval
3. Operator opens verification URI on any device, enters user code
4. Control plane polls until authorized, then links org

### 5.3 Fail-closed

- No silent “connected” without receipt + hosted_org_id
- Wrong state/code → deny event, no link
- Unlink detaches entitlements visibly

---

## 6. Edition runtime matrix

| Behavior | community | cloud | enterprise_self_hosted | enterprise_cloud |
|---|---|---|---|---|
| Local auth / API keys | yes | limited (hosted preferred) | yes | hosted preferred |
| Account Connect required | **no** | **yes** | no | yes |
| ProviderOps kernel | yes | yes (+ managed) | yes | yes |
| Account Connect APIs mounted | no* | yes | yes | yes |
| Marketplace | no | yes | optional | yes |
| SSO/SCIM | no | no | yes | yes |
| Real BudgetPort / EntitlementPort | noop/allow** | yes | yes | yes |
| Easy Connect start from local MC | **yes** | n/a | optional | n/a |

\* Easy Connect lives under `/v1/cloud/*` (community-mounted), not under gated `/v1/account-connect/*` for discovery/start.  
\** Community uses Noop/AllowAll ports by design; Cloud/Enterprise use Postgres ports.

---

## 7. API surface (this delivery)

| Method | Path | Edition mount | Purpose |
|---|---|---|---|
| GET | `/v1/cloud/catalog` | community+ | Feature matrix + independence claims |
| GET | `/v1/cloud/status` | community+ | Link state, pending sessions, workers honesty, easy-connect readiness |
| GET | `/v1/cloud/comparison` | community+ | Community vs Cloud vs Enterprise packaging matrix |
| GET | `/v1/cloud/capabilities` | community+ | Local vs Cloud-locked capability buckets |
| GET | `/v1/cloud/workers` | community+ | Managed worker fabric status (**planned**, never fake capacity) |
| POST | `/v1/cloud/easy-connect` | community+ | Start browser or device Easy Connect |
| GET | `/v1/cloud/easy-connect/{session_id}` | community+ | Poll device / session status |
| POST | `/v1/cloud/easy-connect/{session_id}/complete` | community+ | Complete browser callback (code/state) |
| POST | `/v1/cloud/unlink` | community+ | Disconnect Cloud; Community remains independent |
| GET | `/v1/edition` | all | Existing edition status |

Paid operational routes remain under `/v1/account-connect/*`, `/v1/marketplace/*`, `/v1/enterprise/*`, SAML/SCIM (enterprise router only).

### 7.1 Mission Control surface

Editions / Commercial panel loads Community-safe `/v1/cloud/*` even when
Account Connect paid routes are unmounted. Shows independence banner, feature
catalog with status honesty, pending device codes, Easy Connect CTAs, and
Disconnect when linked.

---

## 8. Acceptance tests

1. Community + no AC URL: catalog 200, status `independent=true`, control plane work still works.  
2. Easy Connect browser start creates receipted auth session (or clear blocked reason).  
3. Device flow: user_code issued; verify completes; link + entitlement sync when AC configured.  
4. Cloud edition without link: `edition.status=blocked_account_connect_required`.  
5. Community never returns 404 on core ProviderOps/command paths due to missing Cloud.  
6. UI shows independence + Easy Connect CTA without requiring Enterprise edition env.  
7. `GET /v1/cloud/comparison` returns marketing-aligned rows; `workers.available_now=false`.  
8. `POST /v1/cloud/unlink` without link → 404; after link → Community independent again.  
9. Commercial MC feed remains reachable on Community when AC list routes 404.

---

## 9. Out of scope for this baseline (tracked, not fake-done)

- Multi-tenant SaaS billing meter  
- Fully elastic multi-region worker fabric  
- Public Cloud DNS multi-tenant product  
- Complete SSO/SCIM live IdP certification  

Those stay **planned** in the catalog with honest `status` fields.

---

## 10. Source of truth

- Spec packs: `packages/specpack/packs/{community,cloud,enterprise}_edition.yaml`  
- Runtime: `engine/boss_engine/services/commercial.py`, `cloud_product.py`  
- Hosted AC: `apps/account_connect`  
- Web: Mission Control Editions / Cloud connect surface  
