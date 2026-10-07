# Sign-in setup

This page is for Owners and Admins. It covers BOSS's public address, email (for sign-in links, codes and password resets), single sign-on (OIDC and SAML), social sign-in, SCIM provisioning and self sign-up. Everything here is in **Identity › Sign-in & SSO** unless it says otherwise.

## 1. The public address

Passkeys, sign-in cookies, single sign-on redirects and every emailed link use one address: BOSS's public address. BOSS never takes it from a request's `Host`.

- The installer's `--console-origin https://boss.example.com` writes it to `.env` as `BOSS_CONSOLE_PUBLIC_ORIGIN`. A `BOSS_PUBLIC_URL` you set yourself wins over it.
- With neither, the first Owner's claim binds the address their browser used (HTTPS, or `http://localhost`).
- With none at all, BOSS sends no emailed links and offers no passkeys or single sign-on. **Identity › Overview** says so.

The sign-in settings page shows the address in use. To serve BOSS on a domain, see [Deploy](hostname.md).

## 2. Email (SMTP)

Without email, people sign in with a password or a passkey, and an admin hands over reset and invite links. With email, BOSS also offers:

- **Email link**: a sign-in link that works once, for 10 minutes, in the same browser;
- **Email code**: a 6-digit code, 5 tries;
- password reset links, invitation emails, and key-expiry reminders.

Set these in `.env`, then restart the api and worker (`boss update`, or recreate the containers):

```bash
BOSS_EMAIL_SMTP_HOST=smtp.example.com
BOSS_EMAIL_SMTP_PORT=587
BOSS_EMAIL_SMTP_USERNAME=boss@example.com
BOSS_EMAIL_SMTP_PASSWORD=...
BOSS_EMAIL_FROM_ADDRESS=boss@example.com
BOSS_EMAIL_FROM_NAME=BOSS
```

The settings page then shows **Email (SMTP) configured**. An Owner can turn email links and email codes off in the policy. An email method never counts as a second factor: people whose role needs one still need a passkey or an authenticator app.

## 3. Single sign-on

BOSS signs people in with any OIDC or SAML provider: Keycloak, Okta, Microsoft Entra ID, Google Workspace, or another.

### Add an OIDC provider

1. In your identity provider, create an application (client) for BOSS:
    - redirect URL: `https://boss.example.com/v1/auth/sso/<provider id>/callback` (the provider page in BOSS shows the exact URL);
    - scopes: `openid email profile`.
2. In BOSS, add the provider: preset (**Keycloak**, **Okta**, **Microsoft**, **Google**, or **generic**), its issuer URL, client id and client secret. The secret is kept in the vault and never shown again.
3. **Test sign-in** runs discovery, fetches the signing keys and checks the redirect.
4. Choose:
    - **Create people on first sign-in**: someone from one of the provider's domains who has no account yet gets one (with no role until you give one, unless the provider maps groups);
    - **Trust its MFA**: accept the provider's second factor instead of asking for BOSS's;
    - **Require it for domains**: see [Enforce single sign-on](#enforce-single-sign-on).

The provider then shows as a button on the sign-in page.

API: `POST /v1/identity/providers`, `PATCH /v1/identity/providers/{id}`, `POST /v1/identity/providers/{id}/test`.

### Add a SAML provider

BOSS's SAML service provider details:

| Field | Value |
| --- | --- |
| Metadata | `https://boss.example.com/saml/v2/metadata` |
| Assertion consumer service (ACS) | `https://boss.example.com/saml/v2/acs` |

These two paths, and SCIM's `/scim/v2`, are served by the BOSS API, not the console. In your HTTPS proxy, send `/saml/` and `/scim/` on the public address to the API (`127.0.0.1:8000` by default). See [Deploy](hostname.md).

Add the provider in BOSS with the identity provider's entity id, sign-in URL and signing certificate (up to two, for rollover). Then test it as above.

### Providers from an older install

Providers set up before named accounts are copied here, switched **off**. Add each one's client secret, test it, and turn it on.

### Enforce single sign-on

**Require it for domains** makes the provider the only way in for people with an email in those domains. For them BOSS refuses:

- a password;
- an email link or email code;
- any other sign-in provider.

Passkeys still work for **Owners**, so an Owner can get in if the provider is down. Add the domain to the provider first.

## 4. Social sign-in

GitHub, Google, Microsoft and Apple can be offered as sign-in buttons. Add them like an OIDC provider, with the matching preset. A social sign-in links to an existing account only through a verified email in a domain you listed on the provider. With **Create people on first sign-in**, or self sign-up for that domain, it can also create the account, with no role.

## 5. SCIM provisioning

SCIM lets your identity provider create, update and remove people and groups in BOSS.

1. Open the SSO provider in BOSS and create a SCIM key (`POST /v1/identity/providers/{id}/scim-keys`). It is shown once.
2. In your identity provider, set:
    - base URL: `https://boss.example.com/scim/v2` (routed to the API, as for SAML above);
    - token: the SCIM key.
3. Assign people and groups to the BOSS application.

People provisioned this way start with no role. Bind roles to their SCIM groups (**Identity › Groups**), and every member gets them. SCIM groups are read-only in BOSS.

An older install's SCIM bearer tokens keep working until you replace them with a SCIM key.

## 6. Self sign-up

With email set up, an Owner can turn on **People may create accounts** and list **Allowed sign-up domains**. Someone from one of those domains can then create an account from the sign-in page. They verify their email first, and start with no role.

## 7. The security policy

The same page holds the policy. Changes apply to everyone, need an Owner and step-up, and are audited.

| Setting | Default |
| --- | --- |
| Roles that must use a second factor | Owner, Admin, Auditor |
| Idle sign-out | 12 hours |
| Session at most | 7 days |
| Password at least | 12 characters |
| Check breached passwords | off |
| Keys expire within | 365 days (90 by default) |
| Revoke keys unused for N days | off |
| Grants last at most | 90 days (30 by default) |

The full list is in [Identity administration](IDENTITY_ADMIN.md#the-identity-policy).

## Next steps

- [People and access](PEOPLE_AND_ACCESS.md): invite people and give them roles.
- [Sign in](CONSOLE_AUTH.md): how people sign in, and HTTPS.
