# Web proxy owner-session boundary

**Plane:** BOSS-434 (In Progress). **Branch:** `ai/codex/web-proxy-session-boundary`,
from `origin/main` `9a72bd9a`. **Owner:** Codex TUI/API agent.

Finding: Next's `/v1/[...path]` and hand-written `/api` routes use a server-wide
`BOSS_API_KEY` with no browser login. FastAPI authenticates that service key, but
`GET /v1/projects` lists all orgs/projects without principal filtering. An
unauthenticated visitor could call privileged console endpoints directly.

The fix gates all Next pages and route handlers in middleware with an independent
owner password and signed HttpOnly session, rejects cross-origin mutations, and
rejects remote plain HTTP. The browser never receives the service API key. Local
Next integration proved 401 on no-cookie `/v1/tasks`, `/api/projects-strip`, and
`/api/coordination`, plus two authenticated project IDs and 403 on a cross-origin
POST. The installer branch `ai/codex/community-vm-installer` / BOSS-428 / PR #223
provides the generated secrets and loopback-only API/web ports. UI agent's new
`/api/coordination` route is covered by middleware without file overlap.

The full web suite passed (189 files, 2,323 tests; one file/test skipped), the
optimized Next production build passed, and TypeScript passed. CSS emitted two
pre-existing Autoprefixer warnings. Installer PR #223's Compose config was
interpolated with test values: the web service receives password, signing secret,
and optional public origin; the API service receives none of these; both published
ports bind to `127.0.0.1`. Next normalizes its request URL to localhost behind a loopback listener,
so remote TLS proxy access requires `BOSS_CONSOLE_PUBLIC_ORIGIN=https://...` and
proxy-overwritten Host, X-Forwarded-Host, and X-Forwarded-Proto. Remaining
qualification: test this contract against a real TLS reverse proxy. A production
Next listener with simulated proxy headers returned 426 for plain remote login,
307 with a public HTTPS login Location, 303 with a public HTTPS post-login Location
and Secure cookie, and 401 for no-cookie `/v1/tasks` and `/api/coordination`.
Multi-user tenant separation is a separate public-hosting blocker:
principal-filtered reads, individual browser
sessions, and route-specific authorization are not supplied by this owner login.
