"""F6 e2e setup: in the running BOSS's database, a BOSS member (viewer on the default project)
already signed in to the console, a workforce pool, and a confidential client for the RP.

Writes the RP's config (client id and secret, the member's console session cookie) to the path
given, mode 600. Test-only: a real member signs in through the console.

    PYTHONPATH=.:engine python3 tests/e2e/oidc/setup_world.py <out.json> <rp_origin>
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import datetime, timezone

from sqlalchemy import text

from boss_engine.db import begin
from boss_engine.identity import authz, sessions
from boss_engine.oidc_provider import clients, pools


def main(out: str, rp: str) -> None:
    with begin() as db:
        org = db.execute(text("SELECT id FROM orgs ORDER BY created_at, id LIMIT 1")).scalar()
        if org is None:                       # an unclaimed install: the org a claim would make
            org, tag = "org_e2e", uuid.uuid4().hex[:8]
            db.execute(text("INSERT INTO orgs (id, name) VALUES (:o, 'E2E')"), {"o": org})
            db.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES (:w, :o, 'E2E')"), {"w": f"ws_{tag}", "o": org})
            db.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES (:p, :w, 'E2E', :p)"),
                       {"p": f"proj-{tag}", "w": f"ws_{tag}"})
        project = db.execute(text("SELECT p.id FROM projects p JOIN workspaces w ON w.id = p.workspace_id "
                                  "WHERE w.org_id = :o ORDER BY p.created_at LIMIT 1"), {"o": org}).scalar()
        pid = "usr_e2e" + uuid.uuid4().hex[:19]
        db.execute(text("INSERT INTO identity_principals (id, org_id, kind, status, display_name) VALUES "
                        "(:p, :o, 'user', 'active', 'E2E Member')"), {"p": pid, "o": org})
        db.execute(text("INSERT INTO identity_users (principal_id, email, email_verified_at) VALUES (:p, :e, now())"),
                   {"p": pid, "e": f"member-{pid[-6:]}@example.test"})
        db.execute(text("INSERT INTO identity_role_bindings (id, org_id, subject_type, subject_id, role_id, scope_type, "
                        "scope_id, granted_by) VALUES (:id, :o, 'principal', :p, 'arl_viewer', 'project', :pr, 'e2e')"),
                   {"id": "rbd_e2e" + uuid.uuid4().hex[:19], "o": org, "p": pid, "pr": project})
        authz.bump_epoch(org, db=db, reason="e2e")
        issued = sessions.issue(db, principal_id=pid, org_id=org, methods=["password", "totp"],
                                mfa_at=datetime.now(timezone.utc), ip="127.0.0.1", user_agent="e2e", device_id_hash=None)
        pool = pools.create(db, org_id=org, actor=pid, body={"name": "Staff", "kind": "workforce"})
        client, secret = clients.create(db, org_id=org, actor=pid, body={
            "project_id": project, "app_key": "e2e-rp", "pool_id": pool["id"], "name": "E2E RP",
            "redirect_uris": [f"{rp}/callback"], "post_logout_redirect_uris": [f"{rp}/signed-out"],
            "allowed_scopes": ["openid", "profile", "email", "roles", "offline_access"]})
        pools.create_role(db, actor=pid, client=client, role_key="member", name="Member")
        pools.assign_role(db, actor=pid, client=client, subject_ref=pid, role_key="member")
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"org": org, "client_id": client["id"], "client_secret": secret, "session": issued.token,
                   "member": pid}, f)
    print(f"world ready: org={org} client={client['id']}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
