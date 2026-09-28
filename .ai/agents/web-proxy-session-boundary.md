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
307 with a public HTTPS login Location, 303 with a post-login path relative to the public origin
and Secure cookie, and 401 for no-cookie `/v1/tasks` and `/api/coordination`.
Multi-user tenant separation is a separate public-hosting blocker:
principal-filtered reads, individual browser
sessions, and route-specific authorization are not supplied by this owner login.

## 2026-09-28 integration on current main

The original PR #225 commit `add10d3cff` was cherry-picked without conflict onto
`origin/main` `7b3ce2f3` in isolated branch
`ai/codex/boss-434-security-integration`. Review found two additional browser
boundary cases: a tab in the submitted `next` path could become an external
redirect, and a mutation from `localhost` could pass for a `127.0.0.1` host at
the same port. Both now fail closed. A local unauthenticated page rewrites to
sign-in without moving the browser between loopback hostnames; behind the
configured HTTPS proxy it redirects to that public origin. Login/logout use
path-relative redirects so the caller's host stays authoritative.

Focused session, proxy, and browser-origin tests and the optimized production
web build pass. Current-main's full web suite has eight failures in five
unrelated UI files; the same eight reproduce on clean `origin/main`, while all
BOSS-434 tests pass. Production Next smoke on loopback showed local sign-in,
401 on unauthenticated `/v1/*`, 403 on cross-origin mutation, and successful
same-origin session passage to the unconfigured upstream. Simulated TLS proxy
headers showed public HTTPS sign-in redirect, 426 on remote plain HTTP, 401
without a session, and a Secure `__Host` cookie after login. A real TLS proxy
and live Community VM remain unqualified.

Before deploying this middleware in any other web profile, inject both
`BOSS_CONSOLE_PASSWORD` and `BOSS_CONSOLE_SESSION_SECRET` and set the public
origin/proxy contract as appropriate. Community Compose does this. The root
Compose and current Goldeneye/Helm profiles do not yet prove the required
secrets at runtime; missing values intentionally return 503. No deployment or
merge to main is part of this integration branch.
