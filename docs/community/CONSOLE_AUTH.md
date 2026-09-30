# Sign in to Mission Control

Mission Control is the web console for your self-hosted BOSS install. This page explains how owner sign-in works, how to reach the console from another computer, and what a TLS proxy must do before you expose it on a hostname.

## How sign-in works

A single owner password signs you in, and all of the owner's projects appear in the project picker.

The installer generates `BOSS_CONSOLE_PASSWORD` and `BOSS_CONSOLE_SESSION_SECRET` in the private `.env` on the machine where BOSS runs, and passes them only to the web service. After sign-in, the browser receives a signed, HttpOnly session cookie that lasts eight hours. The browser never receives `BOSS_API_KEY`.

Manage the password from the install directory:

| Command | Effect |
| --- | --- |
| `python3 scripts/install/console_password.py show` | Prints the password in the terminal. |
| `python3 scripts/install/console_password.py reset` | Rotates the password and invalidates existing sessions. |

The console password is separate from the API key. The [installer guide](https://github.com/seanventures/boss/blob/main/scripts/install/README.md) has the full commands.

## What the session protects

Every page, `/v1/*` proxy request, and `/api/*` route requires a valid session:

- An unauthenticated API request returns `401` before the privileged key is attached.
- An unauthenticated page request redirects to sign-in on the same browser origin.
- State-changing browser requests must carry a matching `Origin` header.
- `POST /api/auth/logout` clears the session.
- If either console secret is missing or too short, protected requests fail with `503`.

!!! warning "Single-owner access only"
    The owner session protects the whole console, but it does not create separate browser identities or per-project authorization. `GET /v1/projects` currently returns all orgs and projects to a valid service key. A shared, multi-user deployment must not rely on this owner login for tenant isolation. Before offering multi-user hosting, add principal-scoped reads and individual OIDC or SAML browser sessions against an identity provider you run.

## Reach the console

The installer binds the web listener to `127.0.0.1` and keeps the console secrets in the local `.env`. On the install machine, open `http://127.0.0.1:3000`.

From another computer, you have two options.

### Use an SSH tunnel

A tunnel needs no proxy. Open it to the VM you administer:

```bash
ssh -L 3000:127.0.0.1:3000 -L 8000:127.0.0.1:8000 user@your-vm
```

Then open `http://127.0.0.1:3000` on the computer that opened the tunnel.

### Serve the console on a hostname

1. Pass `--console-origin https://console.example` to the installer.
2. Point DNS or a hosts entry at the install machine.
3. Run a TLS proxy on that machine that forwards to `127.0.0.1:3000` and overwrites `Host`, `X-Forwarded-Host`, and `X-Forwarded-Proto`.

The HTTPS origin answers only once the proxy is in place. Postgres stays on loopback, the API stays on loopback unless you pass `--api-bind`, and authentication is always required. [Console hostname](hostname.md) walks through the full setup.

## Requirements for remote HTTPS

The console rejects remote plain HTTP, including the login form.

`--console-origin` writes `BOSS_CONSOLE_PUBLIC_ORIGIN` to `.env`, for example `https://console.example` with no trailing slash. Your proxy must **overwrite** these headers with fixed values:

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
