# Mission Control access on a Community VM

Mission Control uses one owner login for a self-hosted Community installation. The
installer generates an independent `BOSS_CONSOLE_PASSWORD` and
`BOSS_CONSOLE_SESSION_SECRET` in its private environment file and passes them only to
the web service. The browser receives a signed, eight-hour, HttpOnly session cookie;
it never receives `BOSS_API_KEY`. All of the owner's projects remain in the picker.
Use the installer's `console_password.py show` and `reset` commands on the VM to
retrieve or rotate the owner password. Rotating it invalidates existing sessions.

Every page, `/v1/*` proxy request, and `/api/*` route requires this session. An
unauthenticated API request returns 401 before the privileged key is attached.
An unauthenticated page opens sign-in on the same browser origin.
State-changing browser requests require a matching `Origin`, and `POST
/api/auth/logout` clears the session. If either console secret is missing or too
short, protected requests fail with 503.

The installer binds the API and web ports to `127.0.0.1`. Use an SSH tunnel to
`http://127.0.0.1:3000` or put a TLS reverse proxy in front of that loopback port.
Remote plain HTTP is rejected, including the login form. For a remote TLS proxy,
set `BOSS_CONSOLE_PUBLIC_ORIGIN=https://console.example` on the web service (origin
only, with no trailing slash). The proxy must **overwrite** `Host`,
`X-Forwarded-Host`, and `X-Forwarded-Proto`; their values must match that public
origin's host, host, and `https` respectively. These headers are trusted only
because the Next listener remains on loopback. Keep the API port on loopback too.
Verify the deployed proxy with an unauthenticated `/login` request, real sign-in,
and a state-changing request before exposing the console. The public edge must
reject or redirect plain HTTP and must discard client-supplied forwarded headers;
Next rejects requests whose proxy headers are missing or do not match the configured
HTTPS origin.

This owner session protects the whole self-hosted console. It does not create
separate browser identities or per-project browser authorization. The control-plane
`GET /v1/projects` currently returns all orgs and projects to a valid service key,
so a shared multi-user deployment must not rely on this owner login for tenant
isolation. Add principal-scoped reads and individual OIDC/SAML browser sessions
before offering multi-user hosting.
