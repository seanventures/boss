CREATE TABLE IF NOT EXISTS ac_orgs (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ac_entitlements (
  id TEXT PRIMARY KEY,
  org_id TEXT NOT NULL REFERENCES ac_orgs(id),
  project_id TEXT,
  entitlement_key TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  edition TEXT,
  capabilities JSONB NOT NULL DEFAULT '[]'::jsonb,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(org_id, entitlement_key, project_id)
);

CREATE TABLE IF NOT EXISTS ac_marketplace_packs (
  id TEXT PRIMARY KEY,
  org_id TEXT NOT NULL REFERENCES ac_orgs(id),
  pack_id TEXT NOT NULL,
  display_name TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  publisher TEXT NOT NULL DEFAULT 'BOSS',
  price_model TEXT NOT NULL DEFAULT 'free',
  price_usd_micros INTEGER NOT NULL DEFAULT 0,
  required_entitlement TEXT,
  visibility TEXT NOT NULL DEFAULT 'catalog',
  status TEXT NOT NULL DEFAULT 'active',
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(org_id, pack_id)
);

CREATE TABLE IF NOT EXISTS ac_auth_sessions (
  id TEXT PRIMARY KEY,
  org_id TEXT REFERENCES ac_orgs(id),
  project_id TEXT NOT NULL,
  state TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  authorization_url TEXT NOT NULL,
  return_url TEXT,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ac_team_invites (
  id TEXT PRIMARY KEY,
  org_id TEXT NOT NULL REFERENCES ac_orgs(id),
  project_id TEXT NOT NULL,
  email TEXT NOT NULL,
  display_name TEXT,
  role_keys JSONB NOT NULL DEFAULT '[]'::jsonb,
  permissions JSONB NOT NULL DEFAULT '[]'::jsonb,
  status TEXT NOT NULL DEFAULT 'pending',
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ac_billing_portal_sessions (
  id TEXT PRIMARY KEY,
  org_id TEXT NOT NULL REFERENCES ac_orgs(id),
  project_id TEXT NOT NULL,
  portal_url TEXT NOT NULL,
  return_url TEXT,
  status TEXT NOT NULL DEFAULT 'created',
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
