"""F6 e2e setup, customer pool: a pool "Bank customers" (open sign-up, MFA required, passkeys on,
email confirmation off so no mail server is needed) and a confidential client for the RP.

    PYTHONPATH=.:engine python3 tests/e2e/oidc/setup_customer.py <out.json> <rp_origin>
"""
from __future__ import annotations

import json
import os
import sys

from sqlalchemy import text

from boss_engine.db import begin
from boss_engine.oidc_provider import clients, pools


def main(out: str, rp: str) -> None:
    with begin() as db:
        org = db.execute(text("SELECT id FROM orgs ORDER BY created_at, id LIMIT 1")).scalar()
        project = db.execute(text("SELECT p.id FROM projects p JOIN workspaces w ON w.id = p.workspace_id "
                                  "WHERE w.org_id = :o ORDER BY p.created_at LIMIT 1"), {"o": org}).scalar()
        pool = pools.create(db, org_id=org, actor="usr_e2e", body={
            "name": "Bank customers", "kind": "customer", "signup": "open", "mfa": "required", "passkeys": True,
            "password_policy": {"verify_email": False}})
        client, secret = clients.create(db, org_id=org, actor="usr_e2e", body={
            "project_id": project, "app_key": "bank-e2e", "pool_id": pool["id"], "name": "Bank",
            "redirect_uris": [f"{rp}/callback"], "post_logout_redirect_uris": [f"{rp}/signed-out"],
            "allowed_scopes": ["openid", "profile", "email", "roles", "offline_access"]})
        pools.create_role(db, actor="usr_e2e", client=client, role_key="customer", name="Customer")
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"org": org, "client_id": client["id"], "client_secret": secret, "pool": pool["id"]}, f)
    print(f"customer world ready: org={org} client={client['id']}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
