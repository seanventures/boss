# Banking starter: setup

Nothing to set up at a provider; the app keeps its data in a SQLite file (`BANK_DB_PATH`).

1. Open the API's preview: {{preview_url}}/health answers `{"status": "ok"}`.
2. The OpenAPI document is at {{preview_url}}/openapi.json.
3. Create a user: `POST {{preview_url}}/api/register` with `{"username": "alice", "password": "<10+ characters>"}`.
4. Log in: `POST {{preview_url}}/api/login` with the same body; send the returned token as
   `Authorization: Bearer <token>`.
5. Open an account (`POST /api/accounts`), deposit (`POST /api/accounts/<id>/deposit` with
   `{"amount_cents": 1000}`), and read the balance (`GET /api/accounts/<id>/balance`).

Settings: `BANK_TOKEN_TTL_SECONDS` (token lifetime, default 1800), `APP_ENV` (`production`
turns off test-user seeding). In a preview, BOSS's test users (`BOSS_TEST_USERS`) are created
for you.
