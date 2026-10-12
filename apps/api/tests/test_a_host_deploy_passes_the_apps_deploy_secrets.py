"""A governed host deploy gives the app its `deploy` secrets (F2 §2.4), and nothing else sees them.

The app's templates declare PAY_KEY. Its `deploy` binding reaches the host as the container's
environment (written on ssh's stdin to a mode-600 env file, then `docker run --env-file`). The
plan, the ChangeSet, the decision and the receipt name it; no row, log line or argv carries the
value. A live key is used only once a person approved its decision; a declared secret with no
deploy value, or a live key awaiting approval, refuses the deploy in words.
"""
from __future__ import annotations

import base64
import json
import logging
import uuid

import pytest
from sqlalchemy import text

from apps.api.tests.test_a_host_deploys_only_through_an_approved_changeset import (  # noqa: F401
    HOME, _approve_and_apply, _governance, hosted,
)
from boss_engine.app_links.service import project_org
from boss_engine.identity import vault
from boss_engine.services.agent_assignment import app_key as key_of
from boss_engine.templates import secrets as app_secrets
from boss_engine.templates.manifest import SecretSpec

SPEC = SecretSpec(name="PAY_KEY", pattern=r"^pk_test_[a-z0-9]{12,}$",
                  live_pattern=r"^pk_live_[a-z0-9]{12,}$")
TEST_VALUE = "pk_test_q7w8e9r0t1y2u3"
LIVE_VALUE = "pk_live_z9x8c7v6b5n4m3"


@pytest.fixture(autouse=True)
def master_key(monkeypatch):
    monkeypatch.setenv("BOSS_MASTER_KEY", base64.b64encode(b"s" * 32).decode())
    monkeypatch.delenv("BOSS_MASTER_KEY_FILE", raising=False)
    vault.reset_master_key_cache()
    yield
    vault.reset_master_key_cache()


@pytest.fixture()
def app(engine, hosted):  # noqa: F811
    with engine.begin() as c:
        name = c.execute(text("SELECT app_name FROM deployment_targets WHERE id = :t"),
                         {"t": hosted["target_id"]}).scalar_one()
        org = project_org(c, HOME)
        tid = f"tpl_{uuid.uuid4().hex[:10]}"
        c.execute(text("INSERT INTO app_templates (id, org_id, template_key, version, source, manifest, "
                       "status) VALUES (:i, :o, :k, '1.0.0', 'builtin', CAST(:m AS JSONB), 'active')"),
                  {"i": tid, "o": org, "k": f"pay-{tid[-6:]}",
                   "m": json.dumps({"id": "pay", "secrets": [SPEC.model_dump(exclude_none=True)]})})
        c.execute(text("INSERT INTO app_template_applications (id, project_id, app_key, template_id, "
                       "version, stack, status) VALUES (:i, :p, :a, :t, '1.0.0', 'node', 'applied')"),
                  {"i": f"tapp_{uuid.uuid4().hex[:10]}", "p": HOME, "a": key_of(name), "t": tid})
    yield {"key": key_of(name), "org": org, "name": name}
    with engine.begin() as c:
        c.execute(text("DELETE FROM app_template_applications WHERE template_id = :t"), {"t": tid})
        c.execute(text("DELETE FROM app_secret_bindings WHERE project_id = :p AND app_key = :a"),
                  {"p": HOME, "a": key_of(name)})
        c.execute(text("DELETE FROM app_templates WHERE id = :t"), {"t": tid})


def _bind(engine, app, *, mode: str, value: str) -> dict:
    with engine.begin() as c:
        return app_secrets.put(c, org_id=app["org"], project_id=HOME, app_key=app["key"], name="PAY_KEY",
                               env="deploy", mode=mode, value=value, actor_id="ceo", spec=SPEC,
                               may_live=True)


def _deploy(client, hosted):  # noqa: F811
    return client.post("/v1/delivery/deploy", json={
        "target_id": hosted["target_id"], "actor": "ceo", "ref": "main"})


def _record_stdin(hosted):  # noqa: F811
    host, fed = hosted["host"], []
    real = host.run

    def run(argv, *a, **kw):
        if kw.get("input") is not None:
            fed.append(kw["input"])
        done = real(argv, *a, **kw)
        if not isinstance(argv, str) and "BOSS_ENV_WRITTEN" in argv[-1] and done.returncode == 0:
            done.stdout = "BOSS_ENV_WRITTEN\n"
        return done
    import boss_engine.services.deploy_service as ds
    ds.owned_process.run = run
    return fed


def _rows_holding(engine, value: str) -> list[str]:
    """Every table in this run's schema whose rows carry `value` as text (the vault holds it
    encrypted, so it never matches)."""
    found = []
    with engine.begin() as c:
        tables = c.execute(text("SELECT table_name FROM information_schema.tables "
                                "WHERE table_schema = current_schema() AND table_type = 'BASE TABLE'"
                                )).scalars().all()
        for t in tables:
            if c.execute(text(f'SELECT 1 FROM "{t}" x WHERE x::text LIKE :v LIMIT 1'),
                         {"v": f"%{value}%"}).first():
                found.append(t)
    return found


def test_the_deploy_gets_the_apps_test_secret_as_its_environment_and_nothing_else_sees_it(
        client, engine, hosted, app, caplog):  # noqa: F811
    caplog.set_level(logging.DEBUG)
    _bind(engine, app, mode="test", value=TEST_VALUE)
    fed = _record_stdin(hosted)

    planned = _deploy(client, hosted)
    assert planned.status_code == 200, planned.text
    plan = planned.json()
    changeset, _res, _rc, _run = _governance(engine, plan)
    assert changeset["operations"][0]["desired_state"]["app_secrets"] == [
        {"name": "PAY_KEY", "mode": "test", "source": "app_secret", "env": "deploy"}]

    outcome, _ = _approve_and_apply(client, engine, plan)
    _changeset, _res, receipts, run = _governance(engine, plan)
    assert run["status"] == "succeeded", (outcome, run["failure_reason"])

    assert any(f"PAY_KEY={TEST_VALUE}\n" in s for s in fed), "the value reaches the host on stdin"
    runs = [c for c in hosted["host"].commands if c.startswith("docker rm -f") and "docker run" in c]
    assert runs and all("--env-file" in c and ".boss/app.env" in c for c in runs)
    argv_text = json.dumps(hosted["host"].argvs)
    assert TEST_VALUE not in argv_text
    assert _rows_holding(engine, TEST_VALUE) == []   # ChangeSet, receipt, decision, run, steps, audit
    assert TEST_VALUE not in caplog.text and TEST_VALUE not in json.dumps(outcome)


def test_a_live_key_is_used_only_after_its_decision_is_approved(client, engine, hosted, app):  # noqa: F811
    bound = _bind(engine, app, mode="live", value=LIVE_VALUE)
    refused = _deploy(client, hosted)
    assert refused.status_code in (409, 422), refused.text
    words = json.dumps(refused.json())
    assert "PAY_KEY is a live key a person has not approved yet" in words and LIVE_VALUE not in words

    with engine.begin() as c:
        c.execute(text("UPDATE decisions SET status = 'approved' WHERE id = :d"),
                  {"d": bound["live_decision_id"]})
    fed = _record_stdin(hosted)
    plan = _deploy(client, hosted).json()
    with engine.begin() as c:   # approved, then withdrawn before the apply: the apply checks again
        c.execute(text("UPDATE decisions SET status = 'rejected' WHERE id = :d"),
                  {"d": bound["live_decision_id"]})
    _approve_and_apply(client, engine, plan)
    *_, run = _governance(engine, plan)
    assert run["status"] != "succeeded" and "not approved" in (run["failure_reason"] or "")
    assert not any(LIVE_VALUE in s for s in fed)

    with engine.begin() as c:
        c.execute(text("UPDATE decisions SET status = 'approved' WHERE id = :d"),
                  {"d": bound["live_decision_id"]})
    plan = _deploy(client, hosted).json()
    _approve_and_apply(client, engine, plan)
    *_, run = _governance(engine, plan)
    assert run["status"] == "succeeded", run["failure_reason"]
    assert any(f"PAY_KEY={LIVE_VALUE}\n" in s for s in fed)
    assert _rows_holding(engine, LIVE_VALUE) == []


def test_a_declared_secret_with_no_deploy_value_refuses_the_deploy_in_words(client, engine, hosted, app):  # noqa: F811
    with engine.begin() as c:   # a build/preview value is not a deploy value
        app_secrets.put(c, org_id=app["org"], project_id=HOME, app_key=app["key"], name="PAY_KEY",
                        env="preview", mode="test", value=TEST_VALUE, actor_id="ceo", spec=SPEC,
                        may_live=False)
    refused = _deploy(client, hosted)
    assert refused.status_code in (409, 422), refused.text
    words = json.dumps(refused.json())
    assert f"{app['name']} cannot deploy: PAY_KEY has no deploy value" in words
    assert "Secrets" in words and TEST_VALUE not in words
    assert hosted["host"].writes() == []


def test_an_optional_secret_never_refuses_and_a_generated_one_is_made_for_deploy(engine, app):
    """HelpDesk was refused for "GOOGLE_CLIENT_ID has no deploy value" (a sign-in the app turns
    on only when its id is set) and "SESSION_SECRET has no deploy value" (a key BOSS generates
    for build and preview, and nobody is meant to choose). Neither is the operator's to fill."""
    extra = [{"name": "OPT_CLIENT_ID", "test_default": "stub", "required": False},
             {"name": "SESS_KEY", "test_default": "generated"}]
    with engine.begin() as c:
        tid = c.execute(text("SELECT template_id FROM app_template_applications WHERE project_id = :p "
                             "AND app_key = :a"), {"p": HOME, "a": app["key"]}).scalar_one()
        c.execute(text("UPDATE app_templates SET manifest = jsonb_set(manifest, '{secrets}', "
                       "(manifest->'secrets') || CAST(:x AS JSONB)) WHERE id = :t"),
                  {"t": tid, "x": json.dumps(extra)})
        _passed, missing = app_secrets.deploy_secrets(c, project_id=HOME, app_key=app["key"])
        assert not any("OPT_CLIENT_ID" in m for m in missing)
        assert any("SESS_KEY" in m for m in missing) and any("PAY_KEY" in m for m in missing)

        made = app_secrets.ensure_deploy_generated(c, org_id=app["org"], project_id=HOME,
                                                   app_key=app["key"])
        assert made == ["SESS_KEY"]
        # Once only: a second pass, or a value somebody set, is never replaced.
        assert app_secrets.ensure_deploy_generated(c, org_id=app["org"], project_id=HOME,
                                                   app_key=app["key"]) == []
        passed, missing = app_secrets.deploy_secrets(c, project_id=HOME, app_key=app["key"])
    assert missing == ["PAY_KEY has no deploy value"]
    assert {"name": "SESS_KEY", "mode": "generated", "source": "app_secret", "env": "deploy"} in passed


def test_a_stack_reads_the_same_deploy_environment_including_the_apps_own_settings(engine, hosted, app):  # noqa: F811
    """The stack deploy's reader (stack_deploy.app_deploy_env) is the host path's: the app's
    deploy secrets, and a setting no template declares (DEMO_PASSWORD), set for deploy only."""
    from boss_engine.services.stack_deploy import app_deploy_env

    _bind(engine, app, mode="test", value=TEST_VALUE)
    with engine.begin() as c:
        app_secrets.put(c, org_id=app["org"], project_id=HOME, app_key=app["key"],
                        name="DEMO_PASSWORD", env="deploy", mode="test", value="demo-pw-123456",
                        actor_id="ceo", may_live=False,
                        spec=app_secrets.deploy_value_spec("DEMO_PASSWORD", ["deploy"], "test"))
        target = dict(c.execute(text("SELECT * FROM deployment_targets WHERE id = :t"),
                                {"t": hosted["target_id"]}).mappings().one())
        env = app_deploy_env(c, HOME, target, None)
        _passed, missing = app_secrets.deploy_secrets(c, project_id=HOME, app_key=app["key"])
    assert env["PAY_KEY"] == TEST_VALUE and env["DEMO_PASSWORD"] == "demo-pw-123456"
    assert missing == []
