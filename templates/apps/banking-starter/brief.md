# Add the banking starter

Give this app a small bank: users, accounts, a ledger, deposits, withdrawals and transfers
between a user's own accounts. The Java/Dropwizard reference implementation is a complete,
secure app; adapt it, do not weaken it.

What the app must do (the template's tests check every line over HTTP):

- `POST /api/register {username, password}` creates a user (username `^[a-z0-9_.-]{3,32}$`,
  password 10-128 characters). Passwords are hashed with Argon2id (`bank/auth/PasswordHasher.java`).
- `POST /api/login` answers `{token, token_type: "Bearer", expires_at}`. Tokens are 256 random
  bits; only their SHA-256 is stored; they expire (`BANK_TOKEN_TTL_SECONDS`, default 1800).
  One error message for an unknown user and a wrong password. After 5 failed logins for a
  username in 15 minutes, logins for it answer 429 with `Retry-After`.
- `POST /api/logout` revokes the token. `POST /api/password {current_password, new_password}`
  needs the current password (403 otherwise) and revokes every other session of the user.
- `POST /api/accounts {currency?}`, `GET /api/accounts`, `GET /api/accounts/{id}/balance`,
  `GET /api/accounts/{id}/entries`, `POST /api/accounts/{id}/deposit|withdraw
  {amount_cents, idempotency_key?}`, `POST /api/transfers {from_account_id, to_account_id,
  amount_cents, idempotency_key?}`. All need `Authorization: Bearer <token>`.
- **Ownership on every account route**: look accounts up with `WHERE id = ? AND user_id = ?`;
  someone else's account answers 404 exactly like a missing one.
- **The balance is the sum of `ledger_entries`**, never a stored column. Amounts are whole cents,
  1..100000000; `10.5`, `"10"`, `0` and negatives are refused. A withdrawal or transfer that would
  overdraw answers 409. Each movement is one `BEGIN IMMEDIATE` transaction, so two concurrent
  withdrawals cannot overdraw. Replaying an `idempotency_key` returns the first result (200).
- Parameterised SQL only. Security headers on every response (`bank/web/SecurityHeadersFilter.java`).
  No CORS. Error responses never include stack traces or exception text.
- `GET /openapi.json` serves the OpenAPI document; `GET /health` answers `{"status": "ok"}`.
- When `APP_ENV` is not `production`, seed the users in `BOSS_TEST_USERS`
  (`[{username, password, role}]`) at start-up (`bank/TestUsers.java`).

Use the reference for this app's stack:

- **java** (Dropwizard 4, JDBI 3, SQLite): the reference is a whole project (`pom.xml`,
  `config.yml`, `src/main/java/bank/...`, `src/main/resources/openapi.json`). For a new project,
  use it as the project root. For an existing Dropwizard app, register the resources, the auth
  filter and the headers filter in its `run()`, and merge the dependencies. Keep
  `<testSourceDirectory>tests</testSourceDirectory>` (or move the tests to the app's test
  directory) and copy `tests/bank/*.java` unchanged: they are the proof the template was applied.
  `config.yml` must keep `port: ${PORT:-8080}` and the `SubstitutingSourceProvider` so previews bind.
