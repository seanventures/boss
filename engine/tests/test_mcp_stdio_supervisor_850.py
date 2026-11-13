"""STDIO MCP SERVERS UNDER A SUPERVISOR (BOSSAPP-850), driven against a tiny REAL stdio server
(tests/fixtures/mcp_stdio_echo_server.py) in the supervisor's test-only subprocess mode.

What is proven: the registry accepts a stdio:// server only for a template of its own project;
mcp_client reaches it through the bridge exactly as an HTTP server (list, call, a receipt); the
bridge initializes the server once, renumbers ids, and refuses a caller without its token;
instances are reused by key, a credential reaches the server only as the env var the template
names, a dead one is respawned, an idle one is reaped by the resource reaper's tick; and the
docker launch plan sandboxes the container without putting a secret in argv.
"""
from __future__ import annotations

import concurrent.futures
import json

import httpx
import pytest
from sqlalchemy import text

from boss_engine.config import get_settings
from boss_engine.services import mcp_registry, mcp_stdio_supervisor as sup
from boss_engine.services import mcp_stdio_templates as templates
from boss_engine.services.mcp_client import MCPClient

from ._p29b_support import drop, echo_template, fresh_engine, make_project, stdio_runtime

PROJECT = "proj_stdio_a"
OTHER = "proj_stdio_b"


@pytest.fixture
def subprocess_runtime(monkeypatch):
    yield from stdio_runtime(monkeypatch)


@pytest.fixture(scope="module")
def db():
    engine, admin = fresh_engine()
    make_project(engine, PROJECT)
    make_project(engine, OTHER)
    with engine.begin() as c:
        templates.create_template(c, PROJECT, echo_template("echo"), actor="op")
        templates.create_template(c, PROJECT, echo_template(
            "shared", env_from_auth=None, reuse_key="template", idle_ttl_seconds=30),
            actor="op")
    yield engine
    drop(engine, admin)


@pytest.fixture
def settings(db):
    return get_settings().model_copy(update={"database_url": db.url.render_as_string(
        hide_password=False)})


def _stats(client: MCPClient, url: str, headers=None) -> dict:
    return client.call_tool(url, "stats", {}, headers=headers)


# ------------------------------------------------------------------- runtime guard

def test_the_subprocess_runtime_refuses_outside_tests(monkeypatch):
    monkeypatch.setenv(sup.RUNTIME_ENV, "subprocess")
    monkeypatch.delenv(sup.TEST_ONLY_FLAG, raising=False)
    with pytest.raises(sup.StdioUnavailable, match="sandboxed container"):
        sup.runtime()
    monkeypatch.setenv(sup.TEST_ONLY_FLAG, "1")
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    with pytest.raises(sup.StdioUnavailable):
        sup.runtime()
    monkeypatch.delenv(sup.RUNTIME_ENV, raising=False)
    assert sup.runtime() == "docker"


def test_the_docker_plan_sandboxes_and_keeps_the_secret_out_of_argv():
    template = {"id": "mcpstdio_x", "name": "pg", "project_id": "p", "image": "uv:py",
                "argv": ["uvx", "postgres-mcp", "--db=${MODE}"], "network": "none",
                "env_allowlist": ["DATABASE_URI", "MODE"], "env_defaults": {"MODE": "ro"},
                "env_from_auth": "DATABASE_URI"}
    argv, env = sup.launch_plan(template, "postgresql://u:SECRET@h/db", mode="docker",
                                docker="docker", name="boss-mcp-pg-1")
    joined = " ".join(argv)
    assert "SECRET" not in joined
    assert env["DATABASE_URI"] == "postgresql://u:SECRET@h/db"
    for flag in ("-i", "--rm", "--read-only", "--cap-drop", "no-new-privileges", "--pids-limit"):
        assert flag in argv
    assert argv[argv.index("--network") + 1] == "none"
    assert argv[argv.index("--user") + 1] == "65534:65534"
    assert "boss.owner=mcp_stdio" in argv
    assert argv[-3:] == ["uvx", "postgres-mcp", "--db=ro"]
    # Only allowlisted names are forwarded, by name.
    forwarded = [argv[i + 1] for i, a in enumerate(argv) if a == "--env"]
    assert set(forwarded) == {"HOME=/tmp", "DATABASE_URI", "MODE"}


# ------------------------------------------------------------------- the registry's URL check

def test_the_registry_takes_a_stdio_server_only_for_its_own_template(db, settings):
    with db.begin() as c:
        with pytest.raises(mcp_registry.McpServerError, match="needs transport 'stdio'"):
            mcp_registry.register_mcp_server(c, PROJECT, "x", f"stdio://{PROJECT}/echo",
                                             actor="op", settings=settings)
        with pytest.raises(mcp_registry.McpServerError, match="no stdio template named"):
            mcp_registry.register_mcp_server(c, PROJECT, "x", f"stdio://{PROJECT}/nope",
                                             transport="stdio", actor="op", settings=settings)
    with db.begin() as c:
        with pytest.raises(mcp_registry.McpServerError, match="its own project"):
            mcp_registry.register_mcp_server(c, OTHER, "x", f"stdio://{PROJECT}/echo",
                                             transport="stdio", actor="op", settings=settings)


def test_a_template_in_use_cannot_be_deleted_and_host_network_is_refused(db, settings):
    with db.begin() as c:
        with pytest.raises(templates.StdioTemplateError, match="never 'host'"):
            templates.create_template(c, PROJECT, echo_template("bad", network="host"),
                                      actor="op")
        mcp_registry.register_mcp_server(c, PROJECT, "echo-held", f"stdio://{PROJECT}/echo",
                                         transport="stdio", actor="op", settings=settings)
        tpl = templates.find_template(c, PROJECT, "echo")
        with pytest.raises(templates.StdioTemplateInUse, match="echo-held"):
            templates.delete_template(c, PROJECT, tpl["id"], actor="op")
        mcp_registry.delete_mcp_server(c, PROJECT, mcp_registry.find_mcp_server(
            c, PROJECT, "echo-held")["id"], actor="op")


# ------------------------------------------------------------------- lifecycle and bridge

def test_mcp_client_reaches_a_stdio_server_through_the_bridge(db, settings, subprocess_runtime):
    url = f"stdio://{PROJECT}/echo"
    client = MCPClient(settings)
    tools = client.list_tools(url, headers={"Authorization": "Bearer tok-A"})
    assert {t["name"] for t in tools} >= {"echo", "whoami", "stats"}
    said = client.call_tool(url, "echo", {"text": "hi"}, headers={"Authorization": "Bearer tok-A"})
    assert said == {"echo": "hi"}
    # The credential reached the server as the env var the template names; the non-secret
    # default too.
    who = client.call_tool(url, "whoami", {}, headers={"Authorization": "Bearer tok-A"})
    assert who == {"token": "tok-A", "greeting": "hello"}
    # One handshake per instance, whatever the client re-initializes.
    from boss_engine.services import mcp_client

    mcp_client.forget_sessions()
    assert _stats(client, url, {"Authorization": "Bearer tok-A"})["initialized"] == 1
    health = subprocess_runtime.health(PROJECT)
    assert len(health) == 1 and health[0]["alive"] and health[0]["mode"] == "subprocess"


def test_instances_are_reused_by_key_and_split_by_credential(db, settings, subprocess_runtime):
    url = f"stdio://{PROJECT}/echo"
    client = MCPClient(settings)
    a1 = _stats(client, url, {"Authorization": "Bearer tok-A"})["pid"]
    a2 = _stats(client, url, {"Authorization": "Bearer tok-A"})["pid"]
    b = _stats(client, url, {"Authorization": "Bearer tok-B"})["pid"]
    assert a1 == a2 and b != a1
    shared = f"stdio://{PROJECT}/shared"
    s1 = _stats(client, shared, {"Authorization": "Bearer one"})["pid"]
    s2 = _stats(client, shared, {"Authorization": "Bearer two"})["pid"]
    assert s1 == s2  # reuse_key template: one instance for every caller


def test_concurrent_callers_never_get_each_others_answers(db, settings, subprocess_runtime):
    url = f"stdio://{PROJECT}/echo"
    client = MCPClient(settings)
    headers = {"Authorization": "Bearer tok-A"}
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        got = list(pool.map(lambda i: client.call_tool(url, "echo", {"text": f"n{i}"},
                                                       headers=headers)["echo"], range(24)))
    assert got == [f"n{i}" for i in range(24)]


def test_the_bridge_refuses_a_caller_without_its_token(db, settings, subprocess_runtime):
    instance = subprocess_runtime.acquire(settings, f"stdio://{PROJECT}/echo",
                                         {"Authorization": "Bearer tok-A"})
    rpc = {"jsonrpc": "2.0", "id": 7, "method": "tools/list"}
    assert httpx.post(instance.url, json=rpc).status_code == 401
    assert httpx.post(instance.url, json=rpc,
                      headers={sup.BRIDGE_HEADER: "wrong"}).status_code == 401
    ok = httpx.post(instance.url, json=rpc, headers={sup.BRIDGE_HEADER: instance.bridge_token})
    assert ok.status_code == 200 and ok.json()["id"] == 7
    # A notification is accepted without an answer, as streamable HTTP says.
    note = httpx.post(instance.url, json={"jsonrpc": "2.0", "method": "notifications/x"},
                      headers={sup.BRIDGE_HEADER: instance.bridge_token})
    assert note.status_code == 202


def test_a_dead_server_is_respawned_and_an_idle_one_is_reaped(db, settings, subprocess_runtime,
                                                              monkeypatch):
    url = f"stdio://{PROJECT}/echo"
    client = MCPClient(settings)
    headers = {"Authorization": "Bearer tok-A"}
    first = _stats(client, url, headers)["pid"]
    with pytest.raises(Exception):
        client.call_tool(url, "die", {}, headers=headers)
    instance = subprocess_runtime.acquire(settings, url, headers)
    assert _stats(client, url, headers)["pid"] != first
    # Idle past the TTL: the reaper's own tick stops it (BOSSAPP-824's registration pattern).
    instance = subprocess_runtime.acquire(settings, url, headers)
    instance.last_used -= 3600
    from boss_engine.services import resource_reaper

    monkeypatch.setattr(sup, "sweep_orphan_containers", lambda binary="docker": [])
    report = resource_reaper.tick(None)
    assert "echo" in report["mcp_stdio"]["stopped_idle"]
    assert not instance.alive()
    assert subprocess_runtime.health(PROJECT) == []


def test_a_call_through_the_registry_lands_a_receipt_and_an_audit_row(db, settings,
                                                                      subprocess_runtime):
    with db.begin() as c:
        server = mcp_registry.register_mcp_server(
            c, PROJECT, "echo-srv", f"stdio://{PROJECT}/echo", transport="stdio", risk_tier=0,
            actor="op", settings=settings, auth={"type": "bearer", "secret": "tok-R"})
        mcp_registry.review_mcp_server(c, PROJECT, server["id"], actor="op")
    probe = mcp_registry.probe(settings, f"stdio://{PROJECT}/echo",
                               headers={"Authorization": "Bearer tok-R"})
    assert probe["reachable"], probe
    with db.begin() as c:
        server = mcp_registry.get_mcp_server(c, PROJECT, server["id"])
        out = mcp_registry.call_tool_with_receipt(
            c, settings, PROJECT, server, "whoami", {}, dedupe_key="t850:whoami",
            tenant_scope=("org_p29b", "ws_org_p29b"))
    assert out["output"]["token"] == "tok-R"
    with db.begin() as c:
        receipt = c.execute(text("SELECT status FROM receipts WHERE id = :id"),
                            {"id": out["receipt_id"]}).scalar_one()
        audit = c.execute(text("SELECT outcome FROM tool_call_audit WHERE server_id = :s "
                               "AND tool = 'whoami'"), {"s": server["id"]}).scalars().all()
    assert receipt == "succeeded" and audit == ["succeeded"]


def test_a_template_edit_replaces_the_running_instance(db, settings, subprocess_runtime):
    url = f"stdio://{PROJECT}/shared"
    client = MCPClient(settings)
    before = _stats(client, url)["pid"]
    with db.begin() as c:
        tpl = templates.find_template(c, PROJECT, "shared")
        templates.update_template(c, PROJECT, tpl["id"], {"env_defaults": {"ECHO_GREETING": "hej"}},
                                  actor="op")
    after = client.call_tool(url, "whoami", {})
    assert after["greeting"] == "hej"
    assert _stats(client, url)["pid"] != before
    assert len([h for h in subprocess_runtime.health(PROJECT) if h["template"] == "shared"]) == 1
    assert json.dumps(after)  # plain JSON all the way through
