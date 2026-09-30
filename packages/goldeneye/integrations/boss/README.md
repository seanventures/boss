# Integration: BOSS control plane

Optional. Goldeneye nodes run without BOSS. When BOSS manages a fleet:

## Node → BOSS

Configure in `site.yaml`:

```yaml
integrations:
  boss:
    api_url: "https://boss.local.example.com"
    project_id: "boss-dogfood"
    node_token_env: BOSS_GOLDENEYE_NODE_TOKEN
```

(Legacy flat `boss:` key is still accepted by operators; prefer `integrations.boss`.)

## BOSS → Node

| API | Purpose |
|---|---|
| `POST /v1/goldeneye/nodes` | Register node |
| `POST /v1/goldeneye/packages/install` | Plan module install (simulated unless SSH deploy allowed) |
| `POST /v1/goldeneye/apps/deploy` | Plan app deploy |
| `GET  /v1/goldeneye/packages` | Catalog from this kit |
| `GET  /v1/goldeneye/status` | Fleet overview |

Catalog assembly uses `python/goldeneye_kit` when present, else `catalog.yaml`.

## Real actuation

Real remote `ge module install` requires:

- `BOSS_SSH_HOST_DEPLOY_ALLOWED=true`
- SSH key SecretRef / host credentials

Otherwise BOSS records **simulated** installs with receipts and operator hints.

## Boundary

| Belongs to Goldeneye | Belongs to BOSS |
|---|---|
| Module recipes, Traefik, registry | Receipts, budgets, RBAC |
| `ge` CLI, site.yaml | Project members, decisions |
| Host state under `/var/lib/goldeneye` | Postgres fleet tables |

Do not import `boss_engine` from module install scripts.
