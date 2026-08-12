# Cloud product, editions & Account Connect

Community is a full self-hosted product. Cloud/Enterprise features are additive and **fail closed** when unlinked or unentitled.

| Prefix | Role |
|--------|------|
| `/v1/edition` | Runtime edition status for a project |
| `/v1/cloud/*` | Catalog, status, Easy Connect, workers, retention |
| `/v1/account-connect/*` | Link/unlink/sync, invites, billing portal |
| `/v1/entitlements` | License entitlements |
| `/v1/capabilities` | Capability matrix |

Specs: `docs/product/BOSS_COMMUNITY_EDITION_SPEC.md`, `docs/product/BOSS_CLOUD_ENTERPRISE_SPEC.md`, `docs/product/INDEPENDENCE_CONTRACT.md`.

```bash
export BOSS_API=http://127.0.0.1:8000
export BOSS_KEY=dev-key
export PROJECT=<project_id>
AUTH=(-H "Authorization: Bearer $BOSS_KEY" -H "Content-Type: application/json")
```

---

## Edition status

```bash
curl -sS -H "Authorization: Bearer $BOSS_KEY" \
  "$BOSS_API/v1/edition?project_id=$PROJECT" | jq .
```

Reports which commercial edition features are active vs local Community baseline.

---

## Cloud product surface

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/v1/cloud/catalog` | Feature catalog + independence contract |
| `GET` | `/v1/cloud/status` | Link state + independence |
| `GET` | `/v1/cloud/comparison` | Community vs Cloud vs Enterprise matrix |
| `GET` | `/v1/cloud/capabilities` | Local vs Cloud-locked capabilities |
| `GET` | `/v1/cloud/entitlements` | Entitlement snapshot |
| `GET` | `/v1/cloud/marketplace` | Marketplace listings (when entitled) |
| `GET` | `/v1/cloud/workers` | Managed worker inventory |
| `POST` | `/v1/cloud/workers/enroll` | Enroll managed worker |
| `POST` | `/v1/cloud/sync` | Sync entitlements from Cloud |
| `POST` | `/v1/cloud/team-invites` | Create invite |
| `GET` | `/v1/cloud/team-invites` | List invites |
| `POST` | `/v1/cloud/easy-connect` | Start Easy Connect session |
| `GET` | `/v1/cloud/easy-connect/{session_id}` | Session status |
| `POST` | `/v1/cloud/easy-connect/{session_id}/complete` | Complete by session |
| `POST` | `/v1/cloud/easy-connect/complete` | Complete by state token |
| `POST` | `/v1/cloud/unlink` | Unlink Cloud account |
| `GET/PUT` | `/v1/cloud/retention` | Data retention policy |

### Discover without linking

```bash
curl -sS -H "Authorization: Bearer $BOSS_KEY" \
  "$BOSS_API/v1/cloud/catalog?project_id=$PROJECT" | jq .

curl -sS -H "Authorization: Bearer $BOSS_KEY" \
  "$BOSS_API/v1/cloud/comparison?project_id=$PROJECT" | jq .

curl -sS -H "Authorization: Bearer $BOSS_KEY" \
  "$BOSS_API/v1/cloud/status?project_id=$PROJECT" | jq .

curl -sS -H "Authorization: Bearer $BOSS_KEY" \
  "$BOSS_API/v1/cloud/capabilities?project_id=$PROJECT" | jq .
```

Community can **read** the catalog and comparison; Cloud-locked actions return explicit blocked states when unlinked.

### Easy Connect

```bash
# 1) Start session
curl -sS "${AUTH[@]}" -d "{
  \"project_id\": \"$PROJECT\",
  \"actor\": \"ceo\",
  \"return_url\": \"https://localhost:3000/settings/cloud\"
}" "$BOSS_API/v1/cloud/easy-connect" | jq .
# → session_id, authorization_url / state

# 2) Poll session
curl -sS -H "Authorization: Bearer $BOSS_KEY" \
  "$BOSS_API/v1/cloud/easy-connect/<session_id>?project_id=$PROJECT" | jq .

# 3) Complete after browser OAuth/account step
curl -sS "${AUTH[@]}" -d "{
  \"project_id\": \"$PROJECT\",
  \"actor\": \"ceo\",
  \"state\": \"<state>\",
  \"authorization_code\": \"<code>\"
}" "$BOSS_API/v1/cloud/easy-connect/<session_id>/complete" | jq .
```

Until Cloud identity backends are configured in a deployment, complete/link paths stay **fail closed** (visible error + events), never fake-linked.

### Sync, workers, unlink

```bash
curl -sS "${AUTH[@]}" -d "{\"project_id\": \"$PROJECT\", \"actor\": \"ceo\"}" \
  "$BOSS_API/v1/cloud/sync" | jq .

curl -sS -H "Authorization: Bearer $BOSS_KEY" \
  "$BOSS_API/v1/cloud/workers?project_id=$PROJECT" | jq .

curl -sS "${AUTH[@]}" -d "{
  \"project_id\": \"$PROJECT\",
  \"actor\": \"ceo\",
  \"worker_label\": \"cloud-pool-a\"
}" "$BOSS_API/v1/cloud/workers/enroll" | jq .

curl -sS "${AUTH[@]}" -d "{\"project_id\": \"$PROJECT\", \"actor\": \"ceo\"}" \
  "$BOSS_API/v1/cloud/unlink" | jq .
```

### Retention

```bash
curl -sS -H "Authorization: Bearer $BOSS_KEY" \
  "$BOSS_API/v1/cloud/retention?project_id=$PROJECT" | jq .

curl -sS -X PUT "${AUTH[@]}" -d "{
  \"project_id\": \"$PROJECT\",
  \"actor\": \"ceo\",
  \"retention_days\": 90
}" "$BOSS_API/v1/cloud/retention" | jq .
```

---

## Account Connect

Parallel/lower-level link lifecycle used by console System Configuration.

| Method | Path |
|--------|------|
| `POST` | `/v1/account-connect/link` |
| `POST` | `/v1/account-connect/unlink` |
| `POST` | `/v1/account-connect/sync` |
| `GET/POST` | `/v1/account-connect/auth-sessions` (+ complete) |
| `GET/POST` | `/v1/account-connect/team-invites` (+ revoke) |
| `GET/POST` | `/v1/account-connect/billing-portal-sessions` |
| `GET/POST` | `/v1/account-connect/health-checks` |

```bash
curl -sS "${AUTH[@]}" -d "{
  \"project_id\": \"$PROJECT\",
  \"actor\": \"ceo\"
}" "$BOSS_API/v1/account-connect/link" | jq .

curl -sS "${AUTH[@]}" -d "{
  \"project_id\": \"$PROJECT\",
  \"actor\": \"ceo\"
}" "$BOSS_API/v1/account-connect/sync" | jq .

curl -sS "${AUTH[@]}" -d "{
  \"project_id\": \"$PROJECT\",
  \"actor\": \"ceo\"
}" "$BOSS_API/v1/account-connect/health-checks" | jq .

curl -sS "${AUTH[@]}" -d "{
  \"project_id\": \"$PROJECT\",
  \"actor\": \"ceo\"
}" "$BOSS_API/v1/account-connect/unlink" | jq .
```

Denials are audited (account-connect sync denial events).

---

## Entitlements & capabilities

```bash
curl -sS -H "Authorization: Bearer $BOSS_KEY" \
  "$BOSS_API/v1/entitlements?project_id=$PROJECT" | jq .

curl -sS -H "Authorization: Bearer $BOSS_KEY" \
  "$BOSS_API/v1/capabilities?project_id=$PROJECT" | jq .
```

---

## Enterprise identity (edition-gated)

When Enterprise SAML is enabled:

- `GET /saml/v2/{project_id}/metadata`
- `POST /saml/v2/{project_id}/acs` → access token for Bearer transport

SCIM (groups/users) under `/scim/v2/{project_id}/...` for directory sync. Disabled runtimes return **503** with an event rather than partial auth.

---

## Independence contract (summary)

- Community remains useful with **zero** Cloud link.  
- Cloud is optional capacity/marketplace/team path.  
- Unlink restores local-only operation; local data is not held hostage.  
- Missing Cloud config → explicit blocked status, not silent mocks.  
