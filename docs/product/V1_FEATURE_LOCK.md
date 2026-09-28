# BOSS v1 feature lock

**Status:** Locked Community v1 contract (not a shipment claim)  
**Not public GA.** Do not invent shipped.  
**Companion:** [Community feature checklist](../community/FEATURES.md)

This file is the explicit **v1 lock**: what is **in** Community v1, what is **out**,
and what is **Cloud / Enterprise only**. Release-planning `POST /sync-from-features`
reads the tables below. It does **not** rewrite the feature catalog.

## Honesty rules

1. Cloud / Enterprise FEATURES stay **✦** (or Partial ◐). Never ✅ Community.
2. Community ◐ stays ◐. A catalog `Implemented` row cannot become Community shipped
   unless this lock says `lock=in` **and** `features=✅`.
3. `BOSS_MICROVM_CLAIMS_FROZEN` stays true. Firecracker is not a v1 claim.
4. This lock is not Community GA, Cloud GA, or public flip.

## How to read a row

| Token | Meaning |
|---|---|
| `in` | Community v1 surface. May be ✅ or ◐. ◐ is in-scope, not shipped. |
| `out` | Not Community v1 (after-v1, isolated, optional scale, frozen claim). |
| `cloud_enterprise` | Cloud / Enterprise only. Never Community shipped. |

Parser contract (machine-read by `engine/boss_engine/services/v1_feature_lock.py`):

- Tables with columns `id | feature | edition | lock | features | match | notes`
- `lock` is exactly `in`, `out`, or `cloud_enterprise`
- `features` first token is one of ✅ ◐ ○ ✦
- `match` is optional comma-separated title aliases

---

## In — Community v1

Community product surface locked for v1. Evidence marks copied from FEATURES /
Community spec. Partial is still in v1; it is not shipped.

| id | feature | edition | lock | features | match | notes |
|---|---|---|---|---|---|---|
| V1-IN-001 | Durable tasks, plans, jobs (Postgres) | community | in | ✅ | durable tasks | Community spec 4.1 |
| V1-IN-002 | Job leases, heartbeat, fencing | community | in | ✅ | job leases | No silent drops |
| V1-IN-003 | Deadletters and remediations | community | in | ✅ | deadletters | First-class remediations; not public GA |
| V1-IN-004 | Event / audit feed | community | in | ✅ | event / audit | |
| V1-IN-005 | Decision inbox (approve / reject / escalate) | community | in | ✅ | decision inbox | |
| V1-IN-006 | Receipts with unique dedupe keys | community | in | ✅ | receipts | |
| V1-IN-007 | Budget quote → reserve → consume / release | community | in | ✅ | budget quote | Soft ports on Community |
| V1-IN-008 | Artifacts and evidence packets | community | in | ✅ | artifacts | |
| V1-IN-009 | Single org / workspace / project model | community | in | ✅ | single org | |
| V1-IN-010 | Local auth / API keys | community | in | ✅ | local auth | |
| V1-IN-011 | HTTP control-plane API (OpenAPI) | community | in | ✅ | control-plane API | |
| V1-IN-012 | CLI | community | in | ✅ | | |
| V1-IN-013 | MCP server for agent clients | community | in | ✅ | MCP server | |
| V1-IN-014 | Self-hosted Mission Control console | community | in | ✅ | Mission Control | |
| V1-IN-015 | API-first clients (`packages/api-client`) | community | in | ✅ | api-client | |
| V1-IN-016 | Bring-your-own API keys (SecretRefs) | community | in | ✅ | Bring-your-own | |
| V1-IN-017 | SecretRef consumption (factory / OpenAI / GitHub / Plane) | community | in | ◐ | SecretRef consumption | Fail-closed; not GA |
| V1-IN-018 | Official QA /reports v1.1 | community | in | ◐ | official QA | Honesty helper; never invent scores |
| V1-IN-019 | Local / OpenAI-compatible base URLs | community | in | ✅ | OpenAI-compatible | |
| V1-IN-020 | Local model entitlement broker | community | in | ✅ | entitlement broker | Allow-all port by design |
| V1-IN-021 | Role packs and skill packs (specpack) | community | in | ✅ | skill packs | |
| V1-IN-022 | Agent Studio starter catalog | community | in | ◐ | Agent Studio | Not GA |
| V1-IN-023 | Pack YAML import / export in Mission Control | community | in | ✅ | Pack YAML | |
| V1-IN-024 | Outcome → governed code task | community | in | ✅ | governed code task | |
| V1-IN-025 | Spec / architecture / implement / test phases | community | in | ✅ | implement / test | |
| V1-IN-026 | Sandbox execution plane | community | in | ◐ | Sandbox execution | Local subprocess. MicroVM frozen |
| V1-IN-027 | Evidence-backed completion | community | in | ✅ | Evidence-backed | |
| V1-IN-028 | Factory quality pass after green verify | community | in | ◐ | Factory quality | Stays partial |
| V1-IN-029 | PR / review packets (with GitHub) | community | in | ✅ | review packets | |
| V1-IN-030 | Browser / automated test evidence | community | in | ✅ | Browser / automated | |
| V1-IN-031 | Deployment targets and runs | community | in | ✅ | Deployment targets | Metadata and run history; SSH planning and actuation paused for GA |
| V1-IN-032 | Project connection + repo binding | community | in | ✅ | repo binding | |
| V1-IN-033 | Easy connect patterns | community | in | ✅ | Easy connect | GitHub easy connect |
| V1-IN-034 | Branch policies and immutable snapshots | community | in | ✅ | Branch policies | |
| V1-IN-035 | Fenced branch leases | community | in | ✅ | branch leases | |
| V1-IN-036 | Safe sync and conflict resolution | community | in | ✅ | Safe sync | |
| V1-IN-037 | Merge authorizations | community | in | ✅ | Merge authorizations | |
| V1-IN-038 | Durable reconciler | community | in | ✅ | Durable reconciler | |
| V1-IN-039 | Provider accounts (model + infra keys) | community | in | ✅ | Provider accounts | |
| V1-IN-040 | Read-only inventory sync | community | in | ✅ | inventory sync | |
| V1-IN-041 | ChangeSets (plan / approve / apply) | community | in | ✅ | ChangeSets | |
| V1-IN-042 | Resource receipts and cost events | community | in | ◐ | Resource receipts | Exact $ needs billing keys |
| V1-IN-043 | Worker pools and scale proposals | community | in | ✅ | Worker pools | Local capacity, not Cloud fleets |
| V1-IN-044 | Role-scoped agents (Metatron, COO, PM, …) | community | in | ✅ | Role-scoped agents | |
| V1-IN-045 | Focused always-on agents | community | in | ◐ | Focused always-on | Not GA |
| V1-IN-046 | Metatron captain (persistent session / goals / watch) | community | in | ◐ | Metatron captain | Not public GA |
| V1-IN-047 | AI SRE kernel (awesome-ai-sre map) | community | in | ◐ | AI SRE | Ledger only; no invented RCA |
| V1-IN-048 | Work orchestration / portfolio | community | in | ✅ | Work orchestration | |
| V1-IN-049 | Meetings / multi-agent panels | community | in | ✅ | multi-agent panels | |
| V1-IN-050 | Agent process handoffs | community | in | ◐ | process handoffs | Not GA |
| V1-IN-051 | Project Manager steward | community | in | ◐ | Project Manager steward | Not GA |
| V1-IN-052 | Assurance / ground-truth runtime | community | in | ✅ | Assurance | |
| V1-IN-053 | Full Community product without Cloud | community | in | ✅ | without Cloud | Independence contract |
| V1-IN-054 | Optional Easy Connect to BOSS Cloud | community | in | ✅ | Easy Connect to BOSS Cloud | Opt-in only |
| V1-IN-055 | Self-hosted SSO (OIDC / SAML / SCIM) | community | in | ✅ | Self-hosted SSO | Not hosted multi-tenant IdP |
| V1-IN-056 | IdP catalog + registry (Okta, Entra, …) | community | in | ✅ | IdP catalog | Community |
| V1-IN-057 | Global activity log | community | in | ◐ | Global activity | Not GA |
| V1-IN-058 | Token efficacy / consumption | community | in | ◐ | token-efficiency | v1 program; schema-drift possible |
| V1-IN-059 | Auto-scaling workers | community | in | ◐ | worker-autoscaling | Local policies; not Cloud fleets |
| V1-IN-060 | Distributed job/worker | community | in | ✅ | distributed job | Same as leases + worker poll |
| V1-IN-061 | Test system | community | in | ◐ | official QA + verify | PARTIAL until official green |
| V1-IN-062 | Release planning board | community | in | ✅ | release-planning | Built / to-build / releases |
| V1-IN-063 | Knowledge graph | community | in | ◐ | knowledge/graph | v1 program |
| V1-IN-064 | On-call automation | community | in | ◐ | oncall-sre | v1 program |
| V1-IN-065 | MVP feature lock | community | in | ✅ | V1_FEATURE_LOCK | This file |
| V1-IN-066 | v1 feature spec final | community | in | ✅ | v1 feature spec | This lock + PRD/TECH + ledger |
| V1-IN-067 | OSS vs commercial boundary | community | in | ✅ | OSS vs commercial | CE rows never Community shipped |

---

## Out — not Community v1

After-v1, isolated swarm, optional unmounted scale, or frozen claims.
These must not be marked Community v1 shipped.

| id | feature | edition | lock | features | match | notes |
|---|---|---|---|---|---|---|
| V1-OUT-001 | Go tenant gateway (S2) | isolated | out | ◐ | tenant gateway | Not mounted; not a Community GA claim |
| V1-OUT-002 | Go event + webhook dispatcher (S3) | isolated | out | ◐ | webhook dispatcher | Optional sidecar; Python event SoR |
| V1-OUT-003 | Firecracker / microVM sandbox | isolated | out | ○ | Firecracker | Frozen; not a v1 claim |
| V1-OUT-004 | Identity integration (hosted / paid gates) | after_v1 | out | ◐ | identity-providers | Community SSO is in; hosted IdP is not |
| V1-OUT-005 | CI/CD pipeline | after_v1 | out | ◐ | /v1/cicd | Catalog exists; live poll credential-gated |
| V1-OUT-006 | Agent key management (KMS depth) | after_v1 | out | ◐ | Agent KMS | SecretRefs stay in v1 |
| V1-OUT-007 | Autonomous feedback loop | after_v1 | out | ◐ | learning_loop | No silent self-mod; automation routes are CE |
| V1-OUT-008 | Infra management (elastic / Goldeneye) | after_v1 | out | ◐ | elastic fabric | ProviderOps kernel is in v1 |
| V1-OUT-009 | UI simplification v1 | after_v1 | out | ○ | UI simplification | Needs cut list |
| V1-OUT-010 | Goldeneye features / metrics | isolated | out | ○ | goldeneye_nodes | Live 500 / missing table is not E2E |
| V1-OUT-011 | Signals analysis | isolated | out | ◐ | telemetry_mining | Exists; not v1 lock-in |
| V1-OUT-012 | Complete CI/CD | isolated | out | ◐ | security-scan gate | Provenance yes; live Actions gated |
| V1-OUT-013 | Security vuln scanning (OS/code) | isolated | out | ◐ | security-scan | Scanners 200; latest may be empty |
| V1-OUT-014 | Public Cloud multi-tenant SaaS GA | isolated | out | ○ | public SaaS GA | Separate Cloud track |

---

## Cloud / Enterprise only

Copied from FEATURES.md ✦ rows and Cloud/Enterprise spec. **Stay ✦ / Partial.**
A catalog `Implemented` status must **not** land on the release board as
Community shipped (`built` + `community_shipped=true`).

| id | feature | edition | lock | features | match | notes |
|---|---|---|---|---|---|---|
| V1-CE-001 | Managed multi-tenant model gateway | cloud | cloud_enterprise | ✦ | managed multi-tenant model gateway | FEATURES ✦ Cloud |
| V1-CE-002 | Hosted skill marketplace | cloud | cloud_enterprise | ✦ | hosted skill marketplace | FEATURES ✦ Cloud |
| V1-CE-003 | Managed elastic worker fleets | cloud | cloud_enterprise | ✦ | managed elastic worker | FEATURES ✦ Cloud (planned) |
| V1-CE-004 | Hosted connector marketplace | cloud | cloud_enterprise | ✦ | hosted connector marketplace | FEATURES ✦ Cloud |
| V1-CE-005 | Self-improvement automation routes | enterprise | cloud_enterprise | ✦ | Self-improvement automation | FEATURES ✦ Enterprise mount |
| V1-CE-006 | Hosted Account Connect & seats | cloud | cloud_enterprise | ✦ | Hosted Account Connect | FEATURES ✦ Cloud |
| V1-CE-007 | HA / multi-region clustering | enterprise | cloud_enterprise | ✦ | multi-region clustering | FEATURES ✦ planned Enterprise |
| V1-CE-008 | Premium support SLAs | enterprise | cloud_enterprise | ✦ | Premium support | FEATURES ✦ Enterprise |
| V1-CE-009 | Hosted Mission Control | cloud | cloud_enterprise | ✦ | Hosted Mission Control | Cloud spec; self-host MC is Community |
| V1-CE-010 | Seat billing / commercial portal | cloud | cloud_enterprise | ✦ | commercial portal | Cloud spec; not Community |
| V1-CE-011 | Hybrid local & cloud workers | enterprise | cloud_enterprise | ✦ | hybrid workers | Cloud/Enterprise spec planned |
| V1-CE-012 | Hosted multi-tenant IdP | cloud | cloud_enterprise | ✦ | hosted multi-tenant IdP | Self-hosted SSO is Community |

---

## What's built / to be built (v1 planning)

Use `/v1/release-planning/{status,board,releases}` plus this lock.
`GET /v1/v1-lock` (and `status.v1_lock`) expose **in / out / cloud counts only**.

| Board column | Meaning under this lock |
|---|---|
| `built` | Community v1 `in` **and** FEATURES ✅ **and** catalog Implemented |
| `building` | Community ◐, or Cloud/Ent Partial/✦ (never Community shipped) |
| `planned` | `out` of v1, or Planned catalog rows |
| `blocked` | Catalog/work Blocked |

Mission Control must not present a Cloud/Enterprise row as a Community ship.

## Source of truth map

| Artifact | Role |
|---|---|
| This file | v1 lock (in / out / Cloud-Enterprise) |
| `docs/community/FEATURES.md` | Public checklist; ✦ stay ✦ |
| `POST /v1/release-planning/sync-from-features` | Applies lock metadata; does not rewrite catalog |
| `GET /v1/v1-lock` + `GET /v1/release-planning/status` | in / out / cloud counts only |
