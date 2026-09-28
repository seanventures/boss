# BOSS Agent Key Management System (KMS)

**Status:** shipped (API + local vault + Vault/OpenBao adapters)  
**Audience:** operators, agent authors, security reviewers  

## Why this exists

Agents need a **project-wide, auditable** way to:

1. **Store** API keys, tokens, and SSH material for themselves  
2. **Share** (grant) access to other agents without pasting secrets into chats  
3. **Use** material only when authorized, with short leases and receipts  

BOSS already had **SecretRefs** (pointers in Postgres) and **secret backends** (values outside the DB). Agent KMS adds **ownership, grants, and leases** on top.

## Open-source backends

| Backend id | What | Notes |
|------------|------|--------|
| `local_vault` | AES-256-GCM files via [cryptography](https://github.com/pyca/cryptography) | Default for Community; `BOSS_KMS_MASTER_KEY` |
| `vault` | [HashiCorp Vault](https://github.com/hashicorp/vault) KV v2 HTTP | `BOSS_VAULT_ADDR` + `BOSS_VAULT_TOKEN` |
| `openbao` | [OpenBao](https://github.com/openbao/openbao) (Vault-compatible) | Same env as Vault |
| `platform_secret_store` | Goldeneye ops-agent `/secret` | Existing platform store |
| `file` | 0600 host files | Operator-managed paths |
| `environment` | Process env | Read-only, local dev |

BOSS **never stores raw secret values in Postgres** (`raw_secret_stored: false`). List APIs return metadata only (`last4`, fingerprint, grants).

## Agent model

- **Owner agent** — principal that stored the key; full rights  
- **Grants** — `use`, `read_meta`, `write`, `grant`, `admin` to other agents (or `*` for project-wide use)  
- **Lease** — short TTL (15–900s, default 120s); optional immediate `reveal`; resolve counts against `max_reveals`  
- **Receipt + events** — every lease is receipted (`kms.lease`); reveals emit `kms.key.revealed` without logging the value  

## API (prefix `/v1/kms`)

Project key operations require an authenticated project member with
`integration_manage`. The member in `actor` must match the API key or login
principal; when a principal is bound, `actor` may be omitted. Agent ownership
and grants are checked in addition to the project permission. A worker that
calls these HTTP routes needs its own project member and bound API key.

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/backends` | OSS backend catalog |
| `GET` | `/health` | Backend health probes |
| `POST` | `/keys` | Store key for `owner_agent` (write-through) |
| `GET` | `/keys?project_id=&agent_id=` | List keys agent can see (metadata) |
| `GET` | `/keys/{id}` | One key metadata |
| `POST` | `/keys/{id}/grant` | Share with another agent |
| `GET` | `/keys/{id}/grants` | List grants |
| `DELETE` | `/keys/{id}/grants/{grantee}` | Revoke grant |
| `POST` | `/keys/{id}/lease` | Lease (optional `reveal`) |
| `POST` | `/leases/{id}/resolve` | Fetch material under lease |
| `POST` | `/leases/{id}/revoke` | Revoke lease early |

### Store for self

```bash
curl -sS -H "Authorization: Bearer $BOSS_KEY" -H "Content-Type: application/json" \
  -d '{
    "project_id": "'"$PROJECT"'",
    "name": "openai-prod",
    "value": "'"$OPENAI_API_KEY"'",
    "owner_agent": "docs-agent",
    "actor": "ceo",
    "backend": "local_vault",
    "purpose": "model"
  }' "$BOSS_API/v1/kms/keys"
```

### Share with another agent

```bash
curl -sS -H "Authorization: Bearer $BOSS_KEY" -H "Content-Type: application/json" \
  -d '{
    "project_id": "'"$PROJECT"'",
    "granter_agent": "docs-agent",
    "grantee_agent": "code-agent",
    "permissions": ["use", "read_meta"],
    "actor": "ceo"
  }' "$BOSS_API/v1/kms/keys/$KEY_ID/grant"
```

### Use (lease + resolve)

```bash
# Prefer reveal=false so workers resolve server-side when possible
curl -sS -H "Authorization: Bearer $BOSS_KEY" -H "Content-Type: application/json" \
  -d '{
    "project_id": "'"$PROJECT"'",
    "agent_id": "code-agent",
    "actor": "ceo",
    "ttl_seconds": 120,
    "reveal": false
  }' "$BOSS_API/v1/kms/keys/$KEY_ID/lease"

curl -sS -H "Authorization: Bearer $BOSS_KEY" -H "Content-Type: application/json" \
  -d '{
    "project_id": "'"$PROJECT"'",
    "agent_id": "code-agent",
    "actor": "ceo"
  }' "$BOSS_API/v1/kms/leases/$LEASE_ID/resolve"
```

## Configuration

```bash
# Local vault (Community default)
export BOSS_KMS_MASTER_KEY="$(openssl rand -base64 32)"
# optional path for encrypted blobs
export BOSS_KMS_VAULT_DIR="$HOME/.config/boss/secrets/vault"

# HashiCorp Vault / OpenBao
export BOSS_VAULT_ADDR=https://vault.example:8200
export BOSS_VAULT_TOKEN=...
export BOSS_VAULT_KV_MOUNT=secret   # optional

# Tests only
export BOSS_KMS_ALLOW_EPHEMERAL_MASTER=true
```

## Relationship to existing `/v1/secrets`

| Surface | Role |
|---------|------|
| `/v1/secrets`, `/v1/secret-refs` | Operator SecretRef CRUD / write-through |
| `/v1/kms/*` | **Agent** ownership, grants, leases |

Agent KMS **uses** SecretRefs under the hood; do not paste secrets into agent chat or plans.

## Security notes

- List endpoints are fail-closed against value exfiltration  
- Lease max TTL is hard-capped (900s)  
- Cross-project access is impossible (project_id on every row)  
- Production must set a real `BOSS_KMS_MASTER_KEY` (never ephemeral)  
- Prefer `reveal=false` + worker-side resolve over returning secrets to LLM context  
