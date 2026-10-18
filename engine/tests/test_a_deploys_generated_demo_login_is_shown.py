"""Setup › Demo login shows the password an app's deploy target actually runs with (D2).

HelpDesk AI's staging deploy generated DEMO_PASSWORD from its own boss.deploy.yaml
(`{generate: password}`) as a target secret; demo-logins?env=deploy read only the app's secret
bindings, so only the Preview password was listed and nobody could sign in as the demo customer.
"""
import json
import uuid
from types import SimpleNamespace

from sqlalchemy import text

from boss_engine.templates import secrets as secrets_service

from ._obs_seed import pg_engine, project


def test_a_stack_generated_demo_password_is_listed_for_its_target(monkeypatch):
    eng = pg_engine()
    from boss_engine.app_links import service as links
    from boss_engine.services import secret_backends, stack_deploy

    with eng.begin() as c:
        pid = project(c)
        tid = f"t_{uuid.uuid4().hex[:8]}"
        c.execute(text("INSERT INTO deployment_targets (id, project_id, app_name, repo, environment, "
                       " target_kind, target_ref, status, created_by) VALUES (:t, :p, 'helpdesk-ai', "
                       " 'acme/sandbox', 'staging', 'ssh', 'x', 'active', 'test')"), {"t": tid, "p": pid})
        c.execute(text("INSERT INTO stack_deployments (id, project_id, target_id, repository, ref, "
                       " commit_sha, manifest, status, created_by) VALUES (:i, :p, :t, 'acme/sandbox', "
                       " 'main', 'c1', CAST(:m AS JSONB), 'healthy', 'test')"),
                  {"i": f"sd_{uuid.uuid4().hex[:8]}", "p": pid, "t": tid, "m": json.dumps({
                      "name": "helpdesk-ai", "generated_env_keys": ["DEMO_PASSWORD", "SESSION_SECRET"]})})
        monkeypatch.setattr(links, "resolve_app_key", lambda db, p, raw: "helpdesk ai" if raw == "helpdesk-ai" else None)
        refs = {}

        def find(db, p, keys):
            refs["asked"] = keys
            return "sref_x" if any(k.endswith("STACK_HELPDESK_AI_DEMO_PASSWORD") for k in keys) else None

        monkeypatch.setattr(stack_deploy, "_find_secret_ref", find)
        monkeypatch.setattr(secret_backends, "resolve_secret_ref", lambda db, ref, s: "pw-staging-32")
        got = secrets_service.deploy_target_demo_logins(c, SimpleNamespace(), project_id=pid,
                                                        app_key="helpdesk ai")
        other = secrets_service.deploy_target_demo_logins(c, SimpleNamespace(), project_id=pid,
                                                          app_key="order api")
    assert got == [{"name": "DEMO_PASSWORD", "value": "pw-staging-32", "target_id": tid,
                    "label": "Staging"}], got
    assert other == []
    # SESSION_SECRET is generated too, but it is not a demo login: never returned.
    assert all(r["name"] == "DEMO_PASSWORD" for r in got)
