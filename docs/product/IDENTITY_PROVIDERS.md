# Identity provider packs

BOSS ships **first-class product packs** for the top enterprise identity providers on top of the generic OIDC / SAML / SCIM runtime (`boss_engine.services.oidc`, `saml`, `enterprise`). Packs provide catalog metadata, setup blueprints, claim maps, and dry-run enterprise config materialization. They do **not** reimplement protocol crypto.

**Edition:** Self-hosted SSO (SAML SP, SCIM 2.0, IdP registry/rollout, IdP packs) is available on **Community** and paid editions. Hosted Account Connect / multi-tenant IdP remains Cloud/Enterprise.

### Enable (Community or paid)

```bash
# OIDC bearer validation (API transport)
export BOSS_OIDC_ENABLED=true
export BOSS_OIDC_ISSUER=https://your-idp.example/oauth2/default
export BOSS_OIDC_AUDIENCE=your-client-id
# optional JWKS override
# export BOSS_OIDC_JWKS_URL=...

# SAML SP sessions
export BOSS_ENTERPRISE_SAML_ENABLED=true
```

Routes (mounted on Community, not 404):

| Path | Role |
|------|------|
| `/saml/v2/{project_id}/metadata` | SP metadata |
| `/saml/v2/{project_id}/acs` | Assertion consumer |
| `/scim/v2/{project_id}/Users|Groups` | SCIM 2.0 |
| `/v1/enterprise/identity-providers` | IdP registry |
| `/v1/enterprise/identity-rollout-policies` | Rollout policy |
| `/v1/enterprise/scim/*` | SCIM admin helpers |
| `/v1/identity-providers` | Top-10 IdP packs + blueprints |

## Open standards

| Standard | Role in BOSS |
| --- | --- |
| **OpenID Connect 1.0** | Primary SSO protocol. JWT validation via JWKS (`oidc.py`). |
| **SAML 2.0** | Enterprise federation ACS / metadata (`saml.py`). |
| **SCIM 2.0** | User/group provisioning into project membership (`/scim/v2`, enterprise SCIM store). |

Specs:

- [OpenID Connect Core 1.0](https://openid.net/specs/openid-connect-core-1_0.html)
- [SAML 2.0 (OASIS)](https://docs.oasis-open.org/security/saml/v2.0/)
- [SCIM 2.0 (RFC 7643 / 7644)](https://datatracker.ietf.org/doc/html/rfc7643)

## Top 10 IdP packs

| ID | Name | Protocols | Category |
| --- | --- | --- | --- |
| `okta` | Okta | OIDC, SAML, SCIM | Workforce |
| `entra_id` | Microsoft Entra ID | OIDC, SAML, SCIM | Workforce |
| `google_workspace` | Google Workspace | OIDC, SAML, SCIM | Workforce |
| `auth0` | Auth0 | OIDC, SAML, SCIM | CIAM / workforce |
| `keycloak` | Keycloak | OIDC, SAML, SCIM | Self-hosted |
| `github` | GitHub | OIDC | Developer |
| `gitlab` | GitLab | OIDC, SAML | Developer |
| `onelogin` | OneLogin | OIDC, SAML, SCIM | Workforce |
| `jumpcloud` | JumpCloud | OIDC, SAML, SCIM | Workforce |
| `ping` | PingFederate / PingOne | OIDC, SAML, SCIM | Enterprise federation |

## API

Prefix: `/v1/identity-providers` (mounted via `mount_platform_routers`).

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/v1/identity-providers` | Catalog list |
| `GET` | `/v1/identity-providers/{idp_id}` | Pack detail + blueprint template |
| `POST` | `/v1/identity-providers/{idp_id}/blueprint` | Dry-run enterprise IdP payload |
| `GET` | `/v1/identity-providers/project-status?project_id=` | Which packs are configured |

Durable storage remains on:

- `GET/POST /v1/enterprise/identity-providers`
- `GET/POST /v1/enterprise/identity-rollout-policies`
- `/saml/v2/{project_id}/...`
- `/scim/v2/{project_id}/...`

## Security rules

- **Fail closed.** Incomplete “active” records become `blocked` with `blocking_gaps`.
- **No raw secrets** in catalog, blueprint, or project-status responses. Use `secret_ref` only.
- **Blueprint is dry-run.** `apply=true` is rejected; operators review `enterprise_payload` then POST to the enterprise store.
- **Rollout modes** (`disabled` → `metadata_only` → `auth_allowed` → `provisioning_allowed`) gate SAML login and SCIM runtime.

## Code map

| Layer | Path |
| --- | --- |
| Catalog service | `engine/boss_engine/services/identity_provider_catalog.py` |
| API router | `apps/api/app/routers/identity_providers.py` |
| Generic OIDC | `engine/boss_engine/services/oidc.py` |
| Generic SAML | `engine/boss_engine/services/saml.py` |
| Enterprise store | `engine/boss_engine/services/enterprise.py` |
| Web client | `apps/web/lib/identity-providers.ts` |
| UI section | `apps/web/app/identity-providers-ui.tsx` |

## Operator flow

1. Browse `GET /v1/identity-providers` (or the Identity provider packs UI).
2. Open pack detail for setup steps and claim map.
3. `POST .../blueprint` with non-secret variables (`domain`, `tenant_id`, `client_id`, …).
4. Store client secrets out-of-band; set `secret_ref` on the payload.
5. `POST /v1/enterprise/identity-providers` with status `planned`.
6. Set identity rollout policy; test login / SCIM; promote to `active`.
