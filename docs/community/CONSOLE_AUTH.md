# Sign in to Mission Control

Mission Control is the web console for your BOSS install. This page explains how people sign in, what happens to the old shared password, and how to reach the console from another computer over HTTPS.

## Named accounts

Each person signs in as themselves and holds an access role (Owner, Admin, Operator, Viewer or Auditor). What they see and can do follows that role, project by project. [People and access](PEOPLE_AND_ACCESS.md) explains roles, groups and invitations.

The first person to open a new or upgraded install **claims** it at `/claim` and becomes the first Owner. The sign-in page shows **This BOSS has no Owner yet. Claim it** until then. [Upgrade to named accounts](IDENTITY_UPGRADE.md) walks through the claim.

If the sign-in or claim page says **Sign-in unavailable**, BOSS could not load
the sign-in options. It cannot tell whether an Owner exists yet. Restore the
API connection, then choose **Retry**; it reloads the current page and keeps
your return destination or setup-link fragment. A failed read never counts as
an Owner claim or a successful sign-in.

## Ways to sign in

If no social sign-in or passkey is usable on this installation, the password
form stays prominent. Open **Other sign-in options** to see those methods and
why they are unavailable. Configured, usable methods stay in the main sign-in
screen; expanding the list does not enable a method or change access.

| Method | Needs |
| --- | --- |
| Passkey | Works once BOSS knows its public address (HTTPS, or `http://localhost`). |
| Email and password, then a second factor | A passkey or an authenticator app. Owners, Admins and Auditors must have one. |
| Email link or 6-digit email code | Email (SMTP) set up. See [Sign-in setup](SIGN_IN_SETUP.md). |
| Single sign-on (OIDC or SAML) and social sign-in | A provider set up in **Identity › Sign-in & SSO**. |
| The shared password, no email | Only for 30 days after the claim, as **Operator**. See below. |

Lost your second factor? Use a recovery code. If you have none left, an Admin can reset your MFA (**Identity › People ›** you **› Reset MFA**).

Your own settings are under **My account**: **Security** (password, passkeys, authenticator app, recovery codes), **Sessions**, **Agents for me** and **API keys**.

## The shared console password

Older installs have one shared password, `BOSS_CONSOLE_PASSWORD` (`boss password` shows it). With named accounts it changes role:

1. **Before the claim**, it only proves you run this BOSS, on the claim screen.
2. **For 30 days after the claim**, it still signs in: choose **Use the shared password** on the sign-in page. No email is needed. You are **Shared console password**, with the **Operator** role, and every page shows how many days are left.
3. **After that**, it is refused. Ask an Owner to invite you.

An Owner can extend the 30 days once, by at most 30 more. Rotate the password with `python3 scripts/install/console_password.py reset` while it is still in use.

## What a session protects

- Protected console pages and business API requests need a valid session (or an appropriate API key, for scripts). Health, sign-in and initial claim endpoints have their own access rules; a reachable health endpoint does not prove a signed-in console works.
- The session cookie is `__Host-`, HttpOnly and `Secure`. Its token never appears in a page or a response body.
- Writes from the browser must come from the console's own origin and carry the session's CSRF token.
- **Sign out** in the account menu ends the session. **My account › Sessions** ends the others.
- Sessions end after 12 hours idle or 7 days at most (an Owner can change both in **Identity › Sign-in & SSO**).

!!! note "Safari and plain http://localhost"
    Sign-in cookies are always `Secure`. Chrome, Edge and Firefox accept them on `http://localhost` and `http://127.0.0.1`; Safari does not, so signing in on a plain-HTTP laptop install seems to do nothing in Safari. Use another browser there, or put the console behind HTTPS.

## Reach the console

The installer binds the web listener to `127.0.0.1`. On the install machine, open `http://127.0.0.1:3000`.

From another computer, you have two options.

### Use an SSH tunnel

A tunnel needs no proxy. Open it to the VM you administer:

```bash
ssh -L 3000:127.0.0.1:3000 -L 3001:127.0.0.1:3001 -L 8000:127.0.0.1:8000 user@your-vm
```

Then open `http://127.0.0.1:3000` on the computer that opened the tunnel. Port 3001 is where previews open (see [Previews have their own address](#previews-have-their-own-address)).

### Serve the console on a hostname

1. Pass `--console-origin https://console.example` to the installer.
2. Point DNS or a hosts entry at the install machine.
3. Run a TLS proxy on that machine that forwards to `127.0.0.1:3000` and overwrites `Host`, `X-Forwarded-Host`, and `X-Forwarded-Proto`.

The HTTPS origin answers only once the proxy is in place. Postgres stays on loopback, the API stays on loopback unless you pass `--api-bind`, and authentication is always required. [Console hostname](hostname.md) walks through the full setup.

### Previews have their own address

A preview runs an app BOSS built, which is code you have not reviewed yet, so it never runs on the console's address. Previews open on their own origin: by default the console's host on port 3001 (`http://127.0.0.1:3000` → previews at `http://127.0.0.1:3001`). That listener serves preview pages and nothing else, and it removes every cookie and credential before the request reaches anything. The console sends a preview link there automatically.

- `BOSS_PREVIEW_PORT` is the port (default `3001`). Set `0` to turn previews off.
- `BOSS_PREVIEW_ORIGIN` is the address a browser opens previews at. Set it when the console is behind HTTPS or on a hostname, for example `https://previews.console.example`, and point your TLS proxy for that name at port 3001. Use a different hostname from the console's, never a path under it.
- `BOSS_PREVIEW_BIND` is the interface the port is published on (default `127.0.0.1`).

## Requirements for remote HTTPS

The console rejects remote plain HTTP, including the login form.

`--console-origin` writes `BOSS_CONSOLE_PUBLIC_ORIGIN` to `.env`, for example `https://console.example` with no trailing slash. Sign-in uses it as BOSS's public address: cookies, passkeys, CSRF checks and every emailed link. (`BOSS_PUBLIC_URL`, when set, takes precedence.) Your proxy must **overwrite** these headers with fixed values:

| Header | Value |
| --- | --- |
| `Host` | The public origin's host |
| `X-Forwarded-Host` | The public origin's host |
| `X-Forwarded-Proto` | `https` |

The console trusts these headers only because the Next.js listener stays on loopback, and it rejects requests whose proxy headers are missing or do not match the configured HTTPS origin. See [Console hostname](hostname.md) for an nginx example and for applying the origin to a running install without rebuilding.

!!! warning "Verify before you expose the console"
    Test the deployed proxy with an unauthenticated `/login` request, a real sign-in, and a state-changing request. The public edge must reject or redirect plain HTTP and discard any forwarded headers supplied by the client.

## Next steps

- [Quickstart](QUICKSTART.md), for the install and a first task.
- [Model connections](MODEL_CONNECTIONS.md), once you can sign in.
- [SECURITY.md](https://github.com/seanventures/boss/blob/main/SECURITY.md), to report a vulnerability by email rather than in a public issue.
- [Guide index](README.md), for the terminal and feedback.
