# Add transactional email: password reset, invites and notifications

Add email to this app: a password-reset flow, invites, and notifications the app's own code
sends, through **SMTP** or **Resend**. With no provider set (every build and preview), nothing
leaves the app: each message is kept in a local **mail catcher** at `/dev/mail`, so a reviewer
can open a reset or invite link from the preview. Follow the reference implementation for this
app's stack; adapt it to the app's router, data layer and UI rather than copying files over the
top of the app's own.

## The contract (the connector's own tests check exactly this, over HTTP)

| Route | Behaviour |
|---|---|
| `GET /api/email/status` | `{provider, from, catcher, catcher_url, templates, outbox}`: `provider` is `smtp`, `resend`, `catcher` or `off`; `outbox` counts messages by status. Never a credential |
| `POST /api/email/password-reset` | `{email}` → `202 {"ok": true}` whether or not the address has an account (no enumeration); `400 bad_email` for something that is not an address. For a known address: a one-use token (only its SHA-256 is stored), valid `EMAIL_RESET_TTL_MINUTES`, replacing any earlier unused one, and the `password_reset` email with the link `APP_BASE_URL` + `EMAIL_RESET_PATH` + `?token=`. At most 3 reset emails per address per 15 minutes |
| `POST /api/email/password-reset/confirm` | `{token, password}` → `400 weak_password` (under 8 characters), `400 invalid_token` (unknown, used, expired, or another kind), else the app's `setPassword(userId, password)` and `204` |
| `POST /api/email/invites` | `403 forbidden` unless the app's `canInvite(req)` says yes (default: nobody); `{email, role?}`, `400 bad_email` / `bad_role` (role from the app's `inviteRoles`, default `["member"]`); a one-use token valid `EMAIL_INVITE_TTL_HOURS` and the `invite` email; `201 {invite_id, message_id, status}`, or `502 email_not_sent` with the failure code when the email could not be sent |
| `POST /api/email/invites/accept` | `{token}` → `400 invalid_token`, else the app's `onInviteAccepted({email, role, invitedBy}, req)` and `200 {email, role}` |
| `GET /dev/mail` | The mail catcher's inbox (HTML, escaped, `Content-Security-Policy: default-src 'none'`); when a provider is set, or in production, a page that says the catcher is off |
| `GET /dev/mail/messages`, `GET /dev/mail/messages/:id`, `DELETE /dev/mail/messages` | The caught messages as JSON, one message, and emptying the catcher; `enabled: false` and no messages when it is off |

- **Sending** (`email.send(template, {to, vars, dedupeKey})`, `email.notify(to, {title, body,
  url, action, dedupeKey})`): every message is a row in the outbox (`email_messages`, schema.sql)
  with a UNIQUE `dedupe_key`, so the same notification is never sent twice; Resend also gets it
  as its `Idempotency-Key`. A try-later answer (network, 429, 5xx, SMTP 4xx) is retried up to
  `EMAIL_MAX_ATTEMPTS` with backoff; anything else, or the last attempt, marks the row `failed`
  with a fixed code, logs it (without the address) and calls the app's `onFailure`. A failure is
  never dropped.
- **Links** are built from `APP_BASE_URL`, never from the request's `Host` header. A
  notification's `url` is a path on this app or an http(s) address.
- **Which provider**: `EMAIL_PROVIDER` unset picks Resend when `RESEND_API_KEY` is set, else SMTP
  when `SMTP_HOST` is set, else the catcher. In production (`NODE_ENV` or `APP_ENV` is
  `production`) the catcher never runs: with no provider email is `off` and every send fails
  visibly with `email_not_configured`. SMTP requires STARTTLS by default (`SMTP_TLS`:
  `starttls`, `implicit` for port 465, `none` refused in production). Resend's API host is
  fixed in the code, never configuration.
- **Templates**: `password_reset`, `invite`, `notification`, each `{subject, text, html}`. Restyle
  them in the app's own voice and brand (the `templates` option); every variable stays escaped
  in the HTML and no CR/LF reaches a subject.
- **Data**: the outbox and the token table (`schema.sql`) mapped onto the app's database; user
  ids are the app's own users. Hooks connect the connector to the app: `findUserByEmail`,
  `setPassword` (the app's own password hashing), `canInvite` (the app's sign-in, role and CSRF
  check), `onInviteAccepted` (create the membership), `onFailure`.
- **UI**: a forgot-password form, the choose-a-new-password page the reset link opens, an invite
  form for the people `canInvite` allows, and the accept-invite page, built with the app's own
  components. With `login-oauth`, `canInvite` reads its signed-in user and `X-CSRF-Token`.
- **Secrets** (`RESEND_API_KEY`, `SMTP_PASSWORD`) come from the environment at run time. Never
  write a value into the code, a config file, `.env` or a test.

## Per stack

- **node** (Express): `email/config.mjs`, `email/templates.mjs`, `email/transports.mjs`
  (nodemailer for SMTP, fetch for Resend, the catcher), `email/store.mjs` (the outbox and the
  tokens; adapt it to the app's database with `schema.sql`), `email/routes.mjs`
  (`createEmail(config, hooks)` returns the router, `send` and `notify`), `server.mjs` (how to
  mount it) and `ui/EmailForms.jsx` (React). Add `nodemailer` at the version in `package.json`
  to the app's dependencies. If the app is not ESM, keep these files as `.mjs` and load them
  with `import()`.

## Its tests stay with the connector

This connector is pre-tested. Its own tests send through an in-process fake SMTP server and a
fake Resend API (no network) and check the catcher, the reset and invite flows, one-use and
expiring tokens, no account enumeration, links built from `APP_BASE_URL`, dedupe, retries and
visible failures, and production's refusal of the catcher. They run in the connector's
own test tier, not in the app: do not copy them (or the reference `package.json`'s
`test:transactional-email` scripts) into the app, and do not write tests for the connector. A
build that adds it is checked for its wiring only: the app starts and these routes answer.
