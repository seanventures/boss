# Banking starter: security note

A reference for how a small ledger API should be built. What it protects, and how:

- **Passwords**: Argon2id (19 MiB, t=2, p=1, 16-byte random salt), compared in constant time.
  An unknown username is checked against a decoy hash, so it costs the same time as a wrong
  password, and both get the same 401 message (no username enumeration at login).
- **Sessions**: opaque 256-bit bearer tokens; the database stores only their SHA-256. They
  expire (default 30 minutes), are revoked on logout, and a password change (which needs the
  current password) revokes every other session. No signing key exists, so none can leak.
  Opaque tokens were chosen over JWTs so revocation is immediate.
- **Brute force**: 5 failed logins per username per 15 minutes, then 429 with `Retry-After`.
  The counter is in memory, per process.
- **Authorisation**: every account and transfer route requires a token, and every account is
  read with `WHERE id = ? AND user_id = ?`; another user's account is a 404, the same as a
  missing one.
- **Money**: whole cents only (fractions and strings are refused, not rounded); 1..100,000,000
  per movement; the balance is computed from the ledger; withdrawals and transfers that would
  overdraw are refused inside one `BEGIN IMMEDIATE` transaction, so concurrent requests cannot
  overdraw; idempotency keys make retries safe.
- **Injection**: every SQL statement is a constant with bound parameters (JDBI).
- **Responses**: `nosniff`, `X-Frame-Options: DENY`, a CSP that loads nothing, `no-store`,
  `no-referrer`, HSTS; no CORS headers. Errors are short fixed messages; unexpected errors are
  logged with an id and answered with a generic 500.
- **Operations**: Dropwizard's admin servlet listens on 127.0.0.1 only. No secrets in code or
  configuration; test users come from `BOSS_TEST_USERS` and are never seeded when
  `APP_ENV=production`.

What it does NOT do: TLS (put it behind the host's HTTPS), a shared rate-limit store across
instances, account lockout notifications, MFA, or a registration rate limit. Registration
answers 409 for a taken username, which tells a caller that the name exists. SQLite suits one
instance; use Postgres for more.
