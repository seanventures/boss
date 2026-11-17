"""A new deployment target asks the app where it is healthy, and can be edited afterwards.

A target was created with a hard-coded GET / expecting 200; a gateway with no root page failed it
forever, though its boss.deploy.yaml declared /healthz. Asked to change the check, Ask BOSS had
no tool; asked for a "local" target with only a remote host registered, it silently built the
target on the remote host. Pinned here:

  * the manifest's health path (and the last deploy's manifest) becomes the new target's check;
  * update_deployment_target is OFFERED (nothing written), and on accept changes the check;
  * "local" with no local host is refused plainly, with the path forward, and nothing is created;
  * preflight warns when the health path is 404 and names the path that answers.

No model and no real CLI: the tool handlers are called directly, as the chat agent's run does.
"""
from __future__ import annotations

import types
from contextlib import contextmanager
from uuid import uuid4

import pytest
from sqlalchemy import text

from ._obs_seed import pg_engine
from boss_engine.config import get_settings
from boss_engine.services import deployment_fleet as fleet
from boss_engine.tools.builtin import build_default_registry

MANIFEST = """\
version: 1
name: gateway
kind: compose
compose: {file: docker-compose.yml, project: gateway}
health:
  - url: "{target.public_url}/healthz"
    expect: 200
"""


@pytest.fixture()
def world():
    eng = pg_engine()
    conn = eng.connect()
    tx = conn.begin()
    org = f"org_{uuid4().hex[:8]}"
    conn.execute(text("INSERT INTO orgs (id, name) VALUES (:o, :o)"), {"o": org})
    conn.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES (:w, :o, :o)"),
                 {"w": f"ws_{org}", "o": org})
    pid = f"p_{uuid4().hex[:8]}"
    conn.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES (:p, :w, :p, :p)"),
                 {"p": pid, "w": f"ws_{org}"})
    hid = f"dh_{uuid4().hex[:12]}"
    conn.execute(text(
        "INSERT INTO deployment_hosts (id, project_id, name, address, ssh_user, status) "
        "VALUES (:i, :p, 'remote-one', '198.51.100.7', 'root', 'verified')"), {"i": hid, "p": pid})
    yield conn, pid, hid
    tx.rollback()
    conn.close()


def _ctx(conn):
    @contextmanager
    def begin():
        yield conn
    return types.SimpleNamespace(begin=begin, settings=get_settings())


def _tool(name):
    return build_default_registry().get(name).handler


def _checks(conn, target_id):
    return conn.execute(text("SELECT health_checks FROM deployment_targets WHERE id = :t"),
                        {"t": target_id}).scalar()


def test_a_manifest_health_path_is_picked_for_a_new_target(world):
    conn, pid, _ = world
    made = fleet.create_deployment_target(conn, project_id=pid, app_name="Gateway X",
                                          repo="acme/gateway", manifest_text=MANIFEST)
    assert made["ok"], made
    assert made["health_path"] == "/healthz" and made["health_path_source"] == "manifest"
    assert _checks(conn, made["target_id"])[0]["path"] == "/healthz"


def test_the_last_deploys_manifest_is_used_when_none_is_passed(world):
    conn, pid, hid = world
    earlier = f"deptgt_{uuid4().hex[:8]}"
    conn.execute(text(
        "INSERT INTO deployment_targets (id, project_id, app_name, repo, environment, "
        " target_kind, target_ref, status, created_by) VALUES (:t, :p, 'Earlier', 'acme/gateway', "
        " 'production', 'ssh', 'x', 'retired', 'test')"), {"t": earlier, "p": pid})
    conn.execute(text(
        "INSERT INTO stack_deployments (id, project_id, target_id, repository, manifest_text, "
        " status, ref, commit_sha, created_by) VALUES (:i, :p, :t, 'acme/gateway', :m, 'healthy', 'main', "
        " 'abc123', 'test')"),
        {"i": f"sd_{uuid4().hex[:8]}", "p": pid, "t": earlier, "m": MANIFEST})
    made = fleet.create_deployment_target(conn, project_id=pid, app_name="Gateway Y",
                                          repo="acme/gateway")
    assert made["health_path"] == "/healthz", made


def test_with_no_declaration_the_root_is_used_and_said_to_be_a_default(world):
    conn, pid, _ = world
    made = fleet.create_deployment_target(conn, project_id=pid, app_name="Plain App",
                                          repo="acme/plain")
    assert made["health_path"] == "/" and made["health_path_source"] == "default"
    assert "update_deployment_target" in made["health_note"]


def test_update_is_offered_first_then_changes_the_check_when_accepted(world):
    conn, pid, _ = world
    made = fleet.create_deployment_target(conn, project_id=pid, app_name="Gateway Z",
                                          repo="acme/gateway")
    tid = made["target_id"]
    assert _checks(conn, tid)[0]["path"] == "/"

    offer = _tool("propose_action")(
        _ctx(conn), project_id=pid, actor="u",
        args={"tool": "update_deployment_target", "label": "Check /healthz",
              "args": {"target_id": tid, "health_path": "/healthz"}})
    assert offer["ok"] and offer["proposed"], offer
    assert _checks(conn, tid)[0]["path"] == "/", "a proposal must not write"

    # The operator's click runs the offered tool with the offered arguments.
    done = _tool("update_deployment_target")(_ctx(conn), project_id=pid, actor="u",
                                             args=offer["args"])
    assert done["ok"] and done["changed"]["health_path"] == "/healthz", done
    assert _checks(conn, tid)[0]["path"] == "/healthz"


def test_update_refuses_an_env_value_that_is_not_a_secret_reference(world):
    conn, pid, _ = world
    tid = fleet.create_deployment_target(conn, project_id=pid, app_name="Env App",
                                         repo="acme/e")["target_id"]
    out = fleet.update_deployment_target(conn, project_id=pid, target_id=tid,
                                         env={"API_KEY": "hunter2"})
    assert out["ok"] is False and "secret reference" in out["error"]


def test_asked_for_local_with_only_a_remote_host_it_refuses_and_offers_the_path(world):
    conn, pid, _ = world
    out = _tool("create_deployment_target")(
        _ctx(conn), project_id=pid, actor="u",
        args={"app_name": "Local App", "environment": "staging", "host": "local"})
    assert out["ok"] is False and out["no_matching_host"] is True
    assert "no host matches 'local'" in out["error"]
    assert "register this machine as a deployment host" in out["offer"]
    assert conn.execute(text("SELECT count(*) FROM deployment_targets WHERE project_id = :p"),
                        {"p": pid}).scalar() == 0, "nothing may be created somewhere else"


def test_a_registered_local_host_satisfies_local(world):
    conn, pid, _ = world
    conn.execute(text(
        "INSERT INTO deployment_hosts (id, project_id, name, address, ssh_user, status) "
        "VALUES (:i, :p, 'this-laptop', '127.0.0.1', 'root', 'verified')"),
        {"i": f"dh_{uuid4().hex[:12]}", "p": pid})
    made = fleet.create_deployment_target(conn, project_id=pid, app_name="Local App 2",
                                          host="local")
    assert made["ok"] and made["address"] == "127.0.0.1", made


def test_preflight_health_warns_on_404_and_names_the_path_that_answers():
    target = {"domains": ["198.51.100.7:8013"],
              "health_checks": [{"kind": "http", "scheme": "http", "path": "/", "port": 8013}]}
    answers = {"http://198.51.100.7:8013/": 404, "http://198.51.100.7:8013/healthz": 200}
    found = fleet.health_path_findings(target, probe=lambda url: answers.get(url, 404))
    assert found[0]["status"] == 404 and found[0]["suggest"] == "/healthz"
    assert "404" in found[0]["warning"]
    ok = fleet.health_path_findings(target, probe=lambda url: 200)
    assert "warning" not in ok[0]
