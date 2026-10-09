-- login-oauth: the two tables behind auth/users.py. Map them onto the app's database layer.
CREATE TABLE IF NOT EXISTS users (
  id             TEXT PRIMARY KEY,
  email          TEXT UNIQUE,
  email_verified BOOLEAN NOT NULL DEFAULT FALSE,
  name           TEXT,
  avatar         TEXT,
  created_at     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- One row per (provider, subject): the provider's stable id for the person, never the email.
CREATE TABLE IF NOT EXISTS user_identities (
  provider   TEXT NOT NULL,
  subject    TEXT NOT NULL,
  user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (provider, subject)
);
