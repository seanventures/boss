# helpdesk-mini: the plumbing gate's golden app

What a build agent produced for HelpDesk AI on 2026-10-13 (crun_247435e1), trimmed to what the
post-agent pipeline exercises: Node 22 + TypeScript run by `tsx`, eslint 9 (flat config,
`typescript-eslint` 8), Express 5 + Postgres (`DATABASE_URL`), the login-oauth connector as a
build applies it (`helpdesk/auth/*.mjs` byte-for-byte from `templates/apps/login-oauth/stacks/node`,
its tests in `helpdesk/tests/template_login_oauth/`, its two `test:login-oauth` scripts), a
`SESSION_SECRET` of at least 32 characters required at start (the connector refuses otherwise),
`boss.deploy.yaml` at the repository root and `helpdesk/boss.preview.command`, and a few P0/P1
vitest tests.

It is a known-good app: every failure the gate reports on it is BOSS's, not the app's.
`scripts/e2e-gate.sh plumbing` runs it (docs/operations/TEST_TIERS.md). Do not "fix" the app
to make the gate pass; `package-lock.json` pins what the gate installs. When the connector's
reference files change, copy them again (the gate checks they still match the template).
