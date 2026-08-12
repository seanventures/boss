# Work / bug trackers — multi-provider support

**Default:** Plane  
**Alternatives:** Jira Cloud · Linear · GitHub Issues · Azure DevOps  

BOSS ranks and leases work internally. The **work tracker** is the external system of
record for tickets. Operators pick one default per project; bindings sync through
leased, receipted jobs.

---

## Catalog

| Key | Product | Create issues |
|---|---|---|
| `plane` | Plane (default) | REST `X-API-Key` |
| `jira` | Jira Cloud | REST v3 + Basic email:token |
| `linear` | Linear | GraphQL API key |
| `github_issues` | GitHub Issues | REST + PAT |
| `azure_devops` | Azure DevOps Boards | REST + PAT |

---

## APIs

| Method | Path | Purpose |
|---|---|---|
| GET | `/v1/work-trackers/catalog` | Field schemas for connect forms |
| GET | `/v1/work-trackers?project_id=` | Installed providers (Plane seeded default) |
| POST | `/v1/work-trackers/connect` | Save config, optional live test, optional set default |
| POST | `/v1/work-trackers/set-default` | Switch default system of record |
| POST | `/v1/work-trackers/test` | Connectivity probe |
| POST | `/v1/work-orchestration/tracker-bindings` | Bind a work item → enqueue sync |
| POST | `/v1/work-orchestration/tracker-sync/execute` | Worker lease creates external issue |

Secrets in list responses are redacted (`***`). Prefer `credential_ref: env:NAME` in production.

---

## Mission Control

**Work Allocation** → Delivery presence:

- See installed trackers; **Make default** on non-default plugins  
- **Connect work tracker** form for Plane / Jira / Linear / GitHub / Azure DevOps  

---

## Policy

`packages/specpack/policies/work_orchestration_policy.yaml` — `default_provider: plane` and
`supported_tracker_providers` list.

---

## Implementation

- Adapters: `engine/boss_engine/services/work_tracker_adapters.py`  
- Orchestration: `engine/boss_engine/services/work_orchestration.py`  
- Tables: `work_tracker_providers`, `work_tracker_bindings`  
