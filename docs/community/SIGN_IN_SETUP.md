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
    - redirect URL: `https://boss.example.com/v1/auth/sso/<provider id>/callback` (BOSS shows the exact URL, ready to copy, as soon as you start adding the provider);
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

### Provider guides

The same steps appear in BOSS under **How to set this up** when you add a provider, where the redirect URL is shown first, ready to copy.

<!-- sso-guides:start (generated from apps/web/lib/identity/sso-guides.ts) -->

#### Google

1. In Google Cloud, create an OAuth client of type Web application. [Create a client](https://console.cloud.google.com/auth/clients/create)
2. Under Authorized redirect URIs, add the redirect URL BOSS shows (`https://<your BOSS>/v1/auth/sso/<provider id>/callback`).
3. Under Audience, add yourself (and anyone testing) as a Test user. [Audience](https://console.cloud.google.com/auth/audience)
4. Copy the Client ID and Client secret into BOSS, then Add.

Copy back into BOSS: Client ID, Client secret.

Watch out: While the app's publishing status is Testing, only listed Test users can sign in.

#### GitHub

1. On GitHub, register a new OAuth app (under an organization's settings for a team). [New OAuth app](https://github.com/settings/applications/new)
2. Set Authorization callback URL to the redirect URL BOSS shows (`https://<your BOSS>/v1/auth/sso/<provider id>/callback`).
3. Generate a client secret.
4. Copy the Client ID and the secret into BOSS, then Add.

Copy back into BOSS: Client ID, Client secret.

Watch out: A GitHub OAuth app has one callback URL: use one app per BOSS install.

#### Microsoft Entra ID

1. In Entra, open App registrations and choose New registration. [App registrations](https://entra.microsoft.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade)
2. Pick Web as the platform and set the redirect URI to the redirect URL BOSS shows (`https://<your BOSS>/v1/auth/sso/<provider id>/callback`).
3. Under Certificates & secrets, add a client secret and copy its Value.
4. Copy the Directory (tenant) ID, the Application (client) ID and the secret into BOSS, then Add.

Copy back into BOSS: Tenant ID, Client ID, Client secret.

Watch out: Use your Directory (tenant) ID, not "common": BOSS signs in one tenant's people.

#### Okta

1. In the Okta Admin Console, go to Applications › Create App Integration › OIDC › Web Application.
2. Set Sign-in redirect URI to the redirect URL BOSS shows (`https://<your BOSS>/v1/auth/sso/<provider id>/callback`), plus the Sign-out redirect and Initiate login URIs (Login initiated by: Either Okta or App).
3. On the Sign On tab, edit the Groups claim filter: groups, Matches regex .* (or narrower).
4. Assign the people or groups who may sign in.
5. Copy your Okta URL (the issuer), the Client ID and Client secret into BOSS, then Add.

Copy back into BOSS: Issuer URL, Client ID, Client secret.

Watch out: The issuer is https://you.okta.com, never the -admin address; only assigned people can sign in.

#### Keycloak

1. In your realm's admin console, go to Clients › Create client (OpenID Connect).
2. Turn on Client authentication, and set Valid redirect URIs to the redirect URL BOSS shows (`https://<your BOSS>/v1/auth/sso/<provider id>/callback`).
3. Copy the client secret from the Credentials tab.
4. Copy the realm URL (the issuer), the Client ID and secret into BOSS, then Add.

Copy back into BOSS: Issuer URL, Client ID, Client secret.

Watch out: The issuer is the realm URL, ending in /realms/<name>.

#### Other OIDC

1. In your provider, create a confidential web client with the authorization code flow.
2. Set its redirect URL to the redirect URL BOSS shows (`https://<your BOSS>/v1/auth/sso/<provider id>/callback`), with scopes openid email profile.
3. Copy the issuer URL, Client ID and Client secret into BOSS, then Add.

Copy back into BOSS: Issuer URL, Client ID, Client secret.

Watch out: The issuer must serve /.well-known/openid-configuration over https.

#### SAML

1. In your identity provider, create a SAML 2.0 application for BOSS.
2. Give it BOSS's ACS URL (`https://<your BOSS>/saml/v2/acs`) and entity ID (`https://<your BOSS>/saml/v2/metadata`).
3. Turn on signed assertions, and send the email as the NameID or an email attribute.
4. Copy its entity ID, sign-in (SSO) URL and signing certificate into BOSS, then Add.

Copy back into BOSS: IdP entity ID, SSO URL, Signing certificate.

Watch out: BOSS refuses unsigned assertions: sign the assertion, not only the response.

#### Apple

1. In Apple Developer, create a Services ID with Sign in with Apple turned on. [Identifiers](https://developer.apple.com/account/resources/identifiers/list/serviceId)
2. Under its configuration, add your domain and the return URL the redirect URL BOSS shows (`https://<your BOSS>/v1/auth/sso/<provider id>/callback`).
3. Create a Sign in with Apple key and note its Key ID and your Team ID.
4. Copy the Services ID (the client ID) and the client secret made from that key into BOSS, then Add.

Copy back into BOSS: Services ID, Client secret.

Watch out: Apple posts back to BOSS (form_post), and needs a real domain, not an IP address.

<!-- sso-guides:end -->

### Okta in detail

Okta's issuer is your org URL, `https://<org>.okta.com` (the org authorization server), or a custom authorization server such as `https://<org>.okta.com/oauth2/default`. BOSS removes a trailing slash and refuses the Admin Console address (`https://<org>-admin.okta.com`). With the org URL, BOSS asks for the scopes `openid email profile groups`; with a custom server it asks for `openid email profile`, and you add a `groups` claim to that server (**Security › API › Authorization servers › Claims**). Either way the groups claim is `groups`: map Okta groups to BOSS groups under the provider's **Sync** sheet. When Okta leaves groups out of the ID token, BOSS reads them from Okta's userinfo endpoint for the same person.

What to enter in Okta:

| Okta setting | Value |
| --- | --- |
| Sign-in redirect URI | `https://boss.example.com/v1/auth/sso/<provider id>/callback` |
| Sign-out redirect URI | `https://boss.example.com/login` |
| Initiate login URI, Login initiated by **Either Okta or App** | `https://boss.example.com/v1/auth/sso/<provider id>/start` |
| Back-channel logout URI (where your Okta org offers it) | `https://boss.example.com/v1/auth/sso/<provider id>/backchannel-logout` |
| Sign On › OpenID Connect ID Token › Groups claim | type **Filter**, name `groups`, **Matches regex** `.*` |
| Provisioning › SCIM connector base URL | `https://boss.example.com/scim/v2` |
| Provisioning › Unique identifier field for users | `userName` |
| Provisioning › Authentication mode | **HTTP Header**, Bearer: a BOSS SCIM key (shown once) |
| Provisioning actions | Push New Users, Push Profile Updates, Push Groups (Import is not needed) |

On a laptop with no public address, use `http://localhost:<port>` in place of `https://boss.example.com` (see [Single sign-on on a laptop](CONSOLE_AUTH.md#single-sign-on-on-a-laptop-httplocalhost)).

**Sign-out**: signing out of BOSS after an Okta sign-in also ends the Okta session (RP-initiated logout) and comes back to `/login`. Okta can also end BOSS sessions itself through the back-channel logout URI: BOSS checks the logout token's signature against Okta's keys and ends only the sessions it names.

**Step-up**: an action that needs a fresh second factor can be confirmed by signing in at Okta again (`/v1/auth/sso/<provider id>/start?purpose=reauth`, which asks Okta for a new login). It counts only when **Trust its MFA** is on and Okta reports MFA.

**SCIM with Okta**: BOSS answers `/ServiceProviderConfig`, `/Schemas` and `/ResourceTypes`, stores the name parts and Okta's `externalId`, and accepts a pushed password without storing it (BOSS people sign in through Okta). Roles are never SCIM's to set.

#### Opt-ins (each off by default)

| Setting (console) | API field | What it allows |
| --- | --- | --- |
| **Link existing accounts** | `link_verified_domain_accounts` | Someone already in BOSS (say, through Google) whose email Okta reports as **verified**, in one of the provider's email domains, is linked to their Okta identity on their first Okta sign-in, instead of being refused. Every link is an `sso.link.auto` event in the audit log. Their own second factor is still asked for, unless **Trust its MFA** applies. |
| **Let SCIM manage existing people** | `scim_adopt_existing_by_domain` | Okta's SCIM connection may update, deprovision and push into groups people BOSS already had, when their verified email is in the provider's domains. Never an Owner or an Admin. The first change is a `scim.user.adopted` event. |
| **Allow sign-in from its dashboard** (SAML only) | `allow_idp_initiated` | A SAML response the identity provider started (a dashboard tile) is accepted. It must still be signed, addressed to BOSS (audience, recipient, destination), recent, and each assertion is used once. OIDC providers use the Initiate login URI instead. |

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
