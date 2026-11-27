"""A secret an app's own start error asks for is a person's card they can ACT on (ENT-REC-13).

The app refuses to start without PAYMENTS_API_KEY, and nothing (no template, no .env.example
mark, no boss.deploy.yaml) declares it. BOSS's start check names it as a secret only a person has
and the card says to set it for build, preview and deploy. The app's Secrets accept a build or
preview value only for a name the app's start checks recorded (secrets.app_declared_env);
otherwise "a value nothing declares is set for deploy only", and the person would be stuck.
This walks the whole path: the card, the value set through the real route for build and preview,
and Retry: the next start of the app is given the value and the app serves.
"""
from __future__ import annotations

import base64
import json
import textwrap
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.app import main
from apps.api.tests.identity_fixtures import IdentityWorld
from boss_engine.app_links.service import app_index
from boss_engine.identity import vault
from boss_engine.services import governed_code_task_service as gov
from boss_engine.services import preview_service as ps
from boss_engine.services.core import emit_event
from boss_engine.templates import run_env

BUILT = ["pay/server.py", "pay/boss.preview.command"]
TEST_VALUE = "pk_test_" + "q7w8e9r0t1y2u3"


@pytest.fixture(autouse=True)
def master_key(monkeypatch):
    monkeypatch.setenv("BOSS_MASTER_KEY", base64.b64encode(b"s" * 32).decode())
    monkeypatch.delenv("BOSS_MASTER_KEY_FILE", raising=False)
    vault.reset_master_key_cache()
    yield
    vault.reset_master_key_cache()


def _app(root):
    (root / "pay").mkdir(parents=True)
    (root / "pay" / "server.py").write_text(textwrap.dedent('''\
        import os, sys
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        if not os.environ.get("PAYMENTS_API_KEY"):
            sys.exit("Error: PAYMENTS_API_KEY must be set")

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
            def log_message(self, *a):
                pass

        ThreadingHTTPServer(("0.0.0.0", int(os.environ["PORT"])), H).serve_forever()
        '''))
    (root / "pay" / "boss.preview.command").write_text("python3 server.py\n")


def test_the_card_the_route_and_retry(engine, tmp_path):
    world = IdentityWorld.create(engine)
    operator = world.person("pay-operator")
    world.bind(operator, "operator")
    project = world.project_a
    run_id = f"crun_{uuid.uuid4().hex[:10]}"
    with engine.begin() as c:
        c.execute(text(
            "INSERT INTO code_task_runs (id, project_id, status, phase, title, spec, state, "
            "finalize_ctx) VALUES (:id, :p, 'completed', 'done', 'Pay Desk', '{}'::jsonb, "
            "CAST(:s AS JSONB), '{}'::jsonb)"),
            {"id": run_id, "p": project, "s": json.dumps({"impl_files": BUILT, "test_files": []})})
        key = app_index(c, project)[0][run_id]
    run = {"id": run_id, "project_id": project, "finalize_ctx": {}}
    root = tmp_path / "repo"
    _app(root)

    # 1. The start check: a person's card, with the name recorded as the app's own declaration.
    record = ps.prove_app_serves(root, BUILT, timeout=20)
    assert record["needs_secret"] == ["PAYMENTS_API_KEY"], record
    assert record["error"].startswith(
        "the app needs a secret: PAYMENTS_API_KEY (named by pay's start error)")
    assert gov.classify_gate_faults(failing=["app_serves"],
                                    evidence={"app_serves": record}) == {"app_serves": "PERSON"}
    action = run_env.needs_secret_action(record["needs_secret"])
    assert action.startswith("Set PAYMENTS_API_KEY for environments build, preview and deploy")
    with engine.begin() as c:
        emit_event(c, project, "code_task.app_serve_checked", "boss", "code_task_run", run_id,
                   {k: (v[:600] if isinstance(v, str) else v) for k, v in record.items()})

    # 2. The person sets it through the real route, for every env the card names.
    client = TestClient(main.app, raise_server_exceptions=False)
    with world.acting_as(world.ctx(operator)):
        r = client.put(f"/v1/apps/{key}/secrets/PAYMENTS_API_KEY", json={
            "project_id": project, "envs": ["build", "preview", "deploy"], "value": TEST_VALUE})
        assert r.status_code == 200, r.text
        assert TEST_VALUE not in r.text
        # The control: a name no start check recorded stays deploy-only.
        r = client.put(f"/v1/apps/{key}/secrets/OTHER_API_KEY", json={
            "project_id": project, "envs": ["build"], "value": TEST_VALUE})
        assert r.status_code == 404 and r.json()["error"] == "secret_not_declared", r.text

    # 3. Retry: the build's next start reads the value bound for `build` and the app serves.
    needs, bound = gov._app_run_declarations(engine, run)
    assert bound == {"PAYMENTS_API_KEY": TEST_VALUE}
    again = ps.prove_app_serves(root, BUILT, timeout=20, needs=needs, bound=bound)
    assert again["ok"] is True and again["proven"] is True, again
    with engine.begin() as c:
        c.execute(text("DELETE FROM app_secret_bindings WHERE project_id = :p AND app_key = :a"),
                  {"p": project, "a": key})
        c.execute(text("DELETE FROM events WHERE entity_id = :r"), {"r": run_id})
        c.execute(text("DELETE FROM code_task_runs WHERE id = :r"), {"r": run_id})
