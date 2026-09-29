# Sign in to Mission Control

Mission Control is the console for a self-hosted Community install. One owner password signs you in. The installer creates `BOSS_CONSOLE_PASSWORD` and `BOSS_CONSOLE_SESSION_SECRET` in the private `.env` on the machine where BOSS runs, and passes them only to the web service. The browser receives a signed, eight-hour, HttpOnly session cookie. It never receives `BOSS_API_KEY`.

All of the owner's projects stay in the picker. On that machine, `python3 scripts/install/console_password.py show` prints the password in a terminal, and `reset` rotates it. Rotating the password invalidates existing sessions. The password is not the API key. The [installer guide](https://github.com/seanventures/boss/blob/main/scripts/install/README.md) has the exact commands.

## What the session covers

Every page, `/v1/*` proxy request, and `/api/*` route requires this session. An unauthenticated API request returns 401 before the privileged key is attached. An unauthenticated page opens sign-in on the same browser origin. State-changing browser requests require a matching `Origin`, and `POST /api/auth/logout` clears the session. If either console secret is missing or too short, protected requests fail with 503.

This owner session protects the whole self-hosted console. It does not create separate browser identities or per-project browser authorization. `GET /v1/projects` currently returns all orgs and projects to a valid service key, so a shared multi-user deployment must not rely on this owner login for tenant isolation. Add principal-scoped reads and individual OIDC or SAML browser sessions, against an identity provider you run, before offering multi-user hosting.

## Reach the console

The installer binds the API and web ports to `127.0.0.1` and creates the console secrets in the local `.env`. That file is not uploaded. On the install machine, open `http://127.0.0.1:3000`.

From another computer, use an SSH tunnel to a VM you administer. `user@your-vm` means that VM, not a shared host:

```bash
ssh -L 3000:127.0.0.1:3000 -L 8000:127.0.0.1:8000 user@your-vm
```

Then open `http://127.0.0.1:3000` on the computer that opened the tunnel. You can instead put a TLS reverse proxy you control in front of the loopback port.

## Remote HTTPS

Remote plain HTTP is rejected, including the login form. For a remote TLS proxy, set `BOSS_CONSOLE_PUBLIC_ORIGIN=https://console.example` on the web service. That value is the origin only, with no trailing slash. The proxy must **overwrite** `Host`, `X-Forwarded-Host`, and `X-Forwarded-Proto`. Their values must match that public origin's host, host, and `https` respectively. These headers are trusted only because the Next listener remains on loopback. Keep the API port on loopback too.

Verify the deployed proxy with an unauthenticated `/login` request, a real sign-in, and a state-changing request before exposing the console. The public edge must reject or redirect plain HTTP and must discard client-supplied forwarded headers. Next rejects requests whose proxy headers are missing or do not match the configured HTTPS origin.

## Next

- [Quickstart](QUICKSTART.md) for the install and the first task.
- [Model connections](MODEL_CONNECTIONS.md) after you can sign in.
- [Security](https://github.com/seanventures/boss/blob/main/SECURITY.md) to report a vulnerability by email. Do not open a public issue for one.
- [Guide index](README.md) for the terminal and feedback.
