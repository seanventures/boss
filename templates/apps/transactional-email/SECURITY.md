# Email (SMTP or Resend): security note

## What it protects

Delivery of the app's own messages and the links in them: a password-reset link and an invite
link each carry a one-use, expiring token that grants something (a new password, a membership).
It does not decide who may reset or invite: whose password a reset sets is the app's own users
table (`findUserByEmail`, `setPassword`), and who may invite is the app's `canInvite` (nobody,
until the app wires it).

## Reset and invite links

- **Tokens**: 32 random bytes, base64url. Only their SHA-256 is stored; the token itself exists
  only in the email. Checking a token and marking it used is one step, so a token works once,
  even under concurrent requests (keep that when moving the store to the app's database: one
  `UPDATE ... WHERE used_at IS NULL AND expires_at > now()`).
- **Lifetimes**: reset 30 minutes (`EMAIL_RESET_TTL_MINUTES`), invite 7 days
  (`EMAIL_INVITE_TTL_HOURS`). A new reset link revokes the earlier unused ones. A reset token is
  never accepted as an invite token, or the reverse.
- **No reset poisoning**: every link is built from `APP_BASE_URL`, never from the request's
  `Host` or `X-Forwarded-Host`, so an attacker cannot make the app email a victim a link to the
  attacker's host. Reset and invite paths must be paths on this app.
- **No account enumeration**: a reset request answers `202 {"ok": true}` for every address; only
  the mailbox's owner learns whether an account exists.
- **Rate limit**: at most 3 reset emails per address per 15 minutes; further requests get the
  same 202 and no mail. The reference counter is in memory (one process); with more than one
  instance, count in the database. Add a per-IP limit at the proxy if needed.
- **Passwords**: the connector checks a minimum length (8) and hands the password to the app's
  `setPassword`, which hashes it the app's way. The connector never stores or logs one.

## Messages

- Every variable is HTML-escaped in a template's HTML part; CR/LF never reach a subject, and an
  address with whitespace or header characters is refused, so no header injection.
- A notification's link is a path on this app or an http(s) address (no `javascript:`).
- nodemailer runs with file and URL access off: a message can never attach a file from the
  server or make it fetch a URL.

## The mail catcher

- It runs only when no provider is set AND the app is not in production (`NODE_ENV` or `APP_ENV`
  is `production`). Configured explicitly for production, the app refuses to start. In production
  with no provider, email is `off`: sends fail visibly, and `/dev/mail` holds nothing.
- A preview is reached through BOSS's preview gateway, so the catcher's links are seen by the
  people who can open the preview. Do not point a preview without a provider at real users'
  addresses and expect them to receive mail.
- Its pages are escaped, `Content-Security-Policy: default-src 'none'`, `no-store`, `noindex`,
  `Referrer-Policy: no-referrer`. It keeps the newest 200 messages in memory.

## Transport

- SMTP requires STARTTLS by default (`SMTP_TLS=starttls`) or implicit TLS (`implicit`); `none`
  is refused in production. Resend is HTTPS only, and its host is fixed in the code
  (`https://api.resend.com`), never read from configuration, so a setting can never redirect the
  API key to another host.
- The connector declares `api.resend.com` and `smtp.resend.com` for egress, approved at apply.
  Another SMTP host is the operator's choice and is not on that list.

## Failures

A send is a row in the outbox with a UNIQUE `dedupe_key` (Resend also receives it as its
`Idempotency-Key`), so a retried request never sends twice. A failure is retried when trying
again can help, then recorded on its row as `failed` with a fixed code (never the provider's raw
answer, which can echo addresses or credentials), logged without the address and passed to the
app's `onFailure`. Nothing is silently dropped.

## Secrets

`RESEND_API_KEY` and `SMTP_PASSWORD` are app secrets: BOSS keeps them in its vault and hands them
to the app as environment variables at run time. They go only into the provider's
Authorization header or SMTP AUTH, never into a log, an error, `/api/email/status` or the tests
(which use fakes and values that say they are fake).

## What it does not do

- No inbound email, bounces or complaint webhooks: a hard bounce shows as a failed send only when
  the provider refuses it at once.
- No queue worker: a send runs in the request that causes it. For bulk notifications, call
  `notify` from the app's own background job.
- No unsubscribe handling; it is for transactional mail, not marketing.
- No password login of its own: it resets a password the app already keeps.
