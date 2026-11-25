# Okta groups to BOSS roles

How a team of 10–20 developers gets access through Okta: who is created, which role each person
gets, and what happens when Okta removes someone. Ticket: BOSSAPP-1294. Setup steps for the Okta
app itself are in [SIGN_IN_SETUP.md › Okta in detail](../community/SIGN_IN_SETUP.md#okta-in-detail).

## The model in one line

**Okta group → BOSS group → role binding.** Okta never sets a role. An admin in BOSS binds a role
to a group once; every member gets it, and loses it when they leave the group.

## Two ways groups arrive

| Way | What Okta sends | What BOSS does | Code |
|---|---|---|---|
| **SCIM** (recommended for a team) | Users and groups, pushed when they change | Creates people (`pending`, no password, no role) and groups (`source='scim'`, read-only in BOSS); keeps memberships in step | `engine/boss_engine/identity/scim.py`, `apps/api/app/routers/identity_scim.py` |
| **Groups claim at sign-in** (OIDC) | The `groups` claim in the ID token (or userinfo) | Maps each listed Okta group to a BOSS group you chose under the provider's **Sync** sheet; adds and removes those memberships on each sign-in | `engine/boss_engine/identity/social.py::sync_groups`; mappings via `group_mappings` on the provider (`apps/api/app/routers/identity_sso.py`) |

SCIM refuses any `roles`, `entitlements` or `x509certificates` attribute (400). SCIM may only change
people its own connection created, and never an Owner or Admin (403), unless **Let SCIM manage
existing people** is on, and even then never an Owner or Admin.

## Set it up

1. In Okta, set up the BOSS app with SCIM: push new users, profile updates and groups
   ([table](../community/SIGN_IN_SETUP.md#okta-in-detail)). The SCIM key comes from
   `POST /v1/identity/providers/{id}/scim-keys` and is shown once.
2. Create the Okta groups and assign them to the app. A suggested split for a preview team:

   | Okta group | BOSS role to bind | Gives |
   |---|---|---|
   | `boss-preview-developers` | Operator | Start and steer work, run workflows, approve decisions; not production deploys (`deployment:production`) |
   | `boss-preview-leads` | Admin | Everything Operator has, plus people, settings and production deploys |
   | `boss-preview-observers` | Viewer | Read only |
   | `boss-preview-audit` | Auditor | Read, plus the audit log (`audit:read`, `audit:export`) |

   The built-in roles are `owner`, `admin`, `operator`, `viewer`, `auditor`
   (`engine/boss_engine/identity/permissions_gen.py`, `ROLE_PERMISSIONS`). Custom roles:
   [PEOPLE_AND_ACCESS.md › Custom roles](../community/PEOPLE_AND_ACCESS.md#custom-roles).
   Bind Owner or Admin with care: SCIM can no longer add members to a group bound to either.
3. In BOSS, **Identity › Groups**: open each synced group and bind its role. Or by API:
   `POST /v1/identity/role-bindings` with
   `{"subject_type": "group", "subject_id": "<group id>", "role_id": "arl_operator"}`.
4. Check one person: their page shows **Effective access**, each permission with the role and
   group it came from (`GET /v1/identity/principals/{id}/effective`).

## What happens when Okta changes something

| Okta does | BOSS does | When |
|---|---|---|
| Adds a person to a group | They get the group's roles | Next request |
| Removes a person from a group | They lose those roles | Next request |
| Deactivates a person (`active=false`) | Disabled; every session ended; API keys suspended; delegations revoked | At once |
| Deletes a person | Same as deactivate (BOSS keeps the record) | At once |
| Deletes a group | Group, memberships and its role bindings removed | At once |

The deprovision path is `scim.py::_set_active` (`sessions.revoke_all`, API credentials suspended,
the delegation cascade) and the identity epoch is bumped, so authorization is re-read on the next
request.

## Proof

| Test | What it proves |
|---|---|
| `apps/api/tests/test_identity_scim_team_of_twenty.py` | 20 people provisioned over SCIM, three groups; roles follow groups exactly; a group move changes access on the next request; deactivate and delete end sessions at once and nobody else's |
| `apps/api/tests/test_identity_fxd_idp_groups.py` | A SCIM group and an OIDC-synced group grant a role until the IdP removes the member (real PDP, enforce mode) |
| `apps/api/tests/test_identity_authn_scim.py` | SCIM create, `active=false` cascade, delete disables, SCIM key good only for provisioning |
| `apps/api/tests/test_identity_okta_sso.py` | Okta sign-in against a fake Okta |

## Not yet

- A walk of this mapping in the console on a real Okta tenant (needs an Okta Integrator tenant).
- Jira sign-in through Okta: needs an Atlassian trial and the Okta tenant; not proven.
- A direct "Okta group name → role" setting: there is none by design; a BOSS group sits between.
