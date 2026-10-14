-- login-oauth's tables, adapted onto this app's schema, and the tickets this app is about.
CREATE TABLE IF NOT EXISTS users (
  id             TEXT PRIMARY KEY,
  email          TEXT UNIQUE,
  email_verified BOOLEAN NOT NULL DEFAULT FALSE,
  name           TEXT,
  created_at     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS user_identities (
  provider   TEXT NOT NULL,
  subject    TEXT NOT NULL,
  user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  PRIMARY KEY (provider, subject)
);

CREATE TABLE IF NOT EXISTS tickets (
  id          SERIAL PRIMARY KEY,
  title       TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  category    TEXT NOT NULL DEFAULT 'Other',
  status      TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'pending', 'closed')),
  created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
