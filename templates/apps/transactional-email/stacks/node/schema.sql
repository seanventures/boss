-- transactional-email: the outbox and the one-use link tokens behind email/store.mjs. Map them
-- onto the app's database layer; user ids refer to the app's own users table.
CREATE TABLE IF NOT EXISTS email_messages (
  id                  TEXT PRIMARY KEY,
  -- One message per key: a retried request never sends the same email twice.
  dedupe_key          TEXT UNIQUE,
  template            TEXT NOT NULL,
  to_address          TEXT NOT NULL,
  subject             TEXT NOT NULL,
  provider            TEXT NOT NULL,                -- smtp | resend | catcher | off
  status              TEXT NOT NULL DEFAULT 'queued'
                      CHECK (status IN ('queued', 'sending', 'retrying', 'sent', 'caught', 'failed')),
  attempts            INTEGER NOT NULL DEFAULT 0,
  last_error          TEXT,                         -- a fixed code, never a provider's raw answer
  provider_message_id TEXT,
  created_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  sent_at             TIMESTAMP
);

-- Reset and invite links. Only the SHA-256 of a token is stored; the token itself is in the email.
CREATE TABLE IF NOT EXISTS email_tokens (
  id          TEXT PRIMARY KEY,
  kind        TEXT NOT NULL CHECK (kind IN ('password_reset', 'invite')),
  token_hash  TEXT NOT NULL UNIQUE,
  email       TEXT NOT NULL,
  user_id     TEXT,                                 -- password_reset: the app's user
  role        TEXT,                                 -- invite: the role the invitee joins with
  invited_by  TEXT,
  expires_at  TIMESTAMP NOT NULL,
  used_at     TIMESTAMP,
  created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS email_tokens_recent ON email_tokens (kind, email, created_at);
