"""boss.extension/v1 (BOSSAPP-861): the manifest, the trust gate, every capability over the broker.

Every capability round-trip runs the in-tree example (plugins/extensions/deploy-notes) in the REAL
isolated child process. The rows are real Postgres in a database of its own, boss_test_861,
created here through psycopg and dropped at the end. The extension tools are called the way an
agent's are: mcp_registry.call_tool_with_receipt → MCPClient over real HTTP → the extension MCP
endpoint → the broker, and the test reads the receipt and the tool_call_audit row that left.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from sqlalchemy import create_engine, text

from boss_engine.extensions import broker, events, jobs, registry, tools, ui, webhooks
from boss_engine.extensions import state as state_store
from boss_engine.extensions.contract import (
    CAPABILITIES, ExtensionContractError, ExtensionManifest, Spec, bundle_checksum, load_manifest,
    validate_manifest,
)

REPO = Path(__file__).resolve().parents[2]
EXAMPLE = REPO / "plugins" / "extensions" / "deploy-notes"
DB_NAME = "boss_test_861"
EXAMPLE_ID = "example.deploy-notes"


# --------------------------------------------------------------------------------------------- #
# the database: boss_test_861, created and dropped here
# --------------------------------------------------------------------------------------------- #

def _admin_url() -> str:
    explicit = os.environ.get("BOSS_TEST_ADMIN_URL", "")
    if explicit:
        return explicit
    base = os.environ.get("BOSS_TEST_DATABASE_URL", "")
    if not base.startswith("postgres"):
        return ""
    head = base.split("?", 1)[0].rsplit("/", 1)[0]
    return head.replace("postgresql+psycopg://", "postgresql://")


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    admin = _admin_url()
    if not admin:
        pytest.skip("needs BOSS_TEST_DATABASE_URL (or BOSS_TEST_ADMIN_URL) to create boss_test_861")
    import psycopg

    try:
        with psycopg.connect(admin, autocommit=True) as c:
            c.execute(f"DROP DATABASE IF EXISTS {DB_NAME} WITH (FORCE)")
            c.execute(f"CREATE DATABASE {DB_NAME}")
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"test postgres unreachable: {type(exc).__name__}")
    url = admin.rsplit("/", 1)[0].replace("postgresql://", "postgresql+psycopg://") + "/" + DB_NAME
    root = tmp_path_factory.mktemp("extensions")
    saved = {k: os.environ.get(k) for k in ("DATABASE_URL", "BOSS_COMMUNITY_SECRET_STORE",
                                            "BOSS_KMS_MASTER_KEY", "BOSS_EXTENSIONS_DIR",
                                            "BOSS_EXTENSIONS_API_URL")}
    server, port = _mcp_http_server()
    os.environ.update(DATABASE_URL=url, BOSS_COMMUNITY_SECRET_STORE="database",
                      BOSS_KMS_MASTER_KEY="dGVzdC1vbmx5LWttcy1tYXN0ZXIta2V5LTg2MS0tLS0tLS0=",
                      BOSS_EXTENSIONS_DIR=str(root / "installed"),
                      BOSS_EXTENSIONS_API_URL=f"http://127.0.0.1:{port}")
    from boss_engine.config import Settings
    from boss_engine.db import ensure_schema

    eng = create_engine(url, future=True)
    ensure_schema(eng)
    settings = Settings()
    _SERVER_STATE["engine"] = eng
    try:
        yield {"engine": eng, "settings": settings, "root": root, "port": port}
    finally:
        server.shutdown()
        eng.dispose()
        from boss_engine import db as boss_db

        with boss_db._ENGINES_LOCK:
            for key in [k for k in boss_db._ENGINES if DB_NAME in k[0]]:
                boss_db._ENGINES.pop(key).dispose()
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        with psycopg.connect(admin, autocommit=True) as c:
            c.execute(f"DROP DATABASE IF EXISTS {DB_NAME} WITH (FORCE)")


_SERVER_STATE: dict = {}


def _mcp_http_server():
    """A real HTTP server answering the extension MCP endpoint path with extensions.tools.handle,
    the same function the API route calls."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # noqa: D401
            pass

        def do_POST(self):  # noqa: N802
            parts = self.path.split("?", 1)[0].strip("/").split("/")
            # v1/ext/{id}/projects/{pid}/mcp
            body = self.rfile.read(int(self.headers.get("content-length") or 0))
            if len(parts) != 6 or parts[:2] != ["v1", "ext"] or parts[3] != "projects" or parts[5] != "mcp":
                self.send_response(404)
                self.end_headers()
                return
            status, answer = tools.handle(_SERVER_STATE["engine"], parts[2], parts[4],
                                          self.headers.get("authorization"), json.loads(body or b"{}"))
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.end_headers()
            if answer is not None:
                self.wfile.write(json.dumps(answer).encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


def _project(eng) -> tuple[str, str, str]:
    pid = f"p_ext_{uuid4().hex[:10]}"
    with eng.begin() as c:
        c.execute(text("INSERT INTO orgs (id, name) VALUES ('org_ext', 'ext') ON CONFLICT DO NOTHING"))
        c.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES ('ws_ext', 'org_ext', 'ext') "
                       "ON CONFLICT DO NOTHING"))
        c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES (:p, 'ws_ext', :p, :p)"),
                  {"p": pid})
    return pid, "org_ext", "ws_ext"


def _install_example(env, *, source: Path = EXAMPLE):
    with env["engine"].begin() as c:
        return registry.install(c, env["settings"], str(source), checksum=bundle_checksum(source),
                                accept=list(load_manifest(source).spec.capabilities), actor="op_test")


def _enable(env, ext_id: str, pid: str):
    with env["engine"].begin() as c:
        return registry.set_enabled(c, env["settings"], ext_id, pid, enabled=True, actor="op_test")


def _load(env, ext_id: str = EXAMPLE_ID):
    with env["engine"].begin() as c:
        return registry.load(c, ext_id)


def make_bundle(where: Path, ext_id: str, code: str, *, capabilities: list[str],
                tools_: list[dict] | None = None, timeout: int = 20, extra: dict | None = None) -> Path:
    """A small extension written for one test, with its checksum computed."""
    where.mkdir(parents=True, exist_ok=True)
    (where / "ext.py").write_text(code, encoding="utf-8")
    spec = {"entrypoint": "ext:Ext", "checksum": bundle_checksum(where), "capabilities": capabilities,
            "runtime": {"timeoutSeconds": timeout}}
    if tools_:
        spec["tools"] = tools_
    spec.update(extra or {})
    manifest = {"apiVersion": "boss.extension/v1", "kind": "Extension",
                "metadata": {"id": ext_id, "name": ext_id, "version": "0.1.0"}, "spec": spec}
    (where / "boss-extension.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    return where


PEEKER = '''
import time
from boss_engine.extensions.sdk import Extension

class Ext(Extension):
    def on_tool(self, name, args, ctx):
        if name == "peek":
            return {"value": ctx.state.get(args["key"]), "keys": ctx.state.list("")}
        if name == "poke":
            ctx.state.put(args["key"], args["value"])
            return {"ok": True}
        if name == "nap":
            time.sleep(5)
            return {"slept": True}
'''

_TOOLS = [{"name": n, "description": n, "riskTier": 0} for n in ("peek", "poke", "nap")]


# --------------------------------------------------------------------------------------------- #
# 1. the manifest: closed capabilities, sections, pinned field sets (no database)
# --------------------------------------------------------------------------------------------- #

def _example_dict() -> dict:
    return yaml.safe_load((EXAMPLE / "boss-extension.yaml").read_text())


def test_the_example_manifest_is_valid_and_matches_its_files():
    m = load_manifest(EXAMPLE)
    assert m.id == EXAMPLE_ID and set(m.spec.capabilities) == set(CAPABILITIES)
    assert m.spec.checksum == bundle_checksum(EXAMPLE)


def test_the_capability_list_is_closed():
    assert CAPABILITIES == ("tools.register", "events.subscribe", "jobs.schedule", "webhooks.receive",
                            "state.read", "state.write", "ui.slot.register")
    bad = _example_dict()
    bad["spec"]["capabilities"].append("network.egress")
    with pytest.raises(ExtensionContractError) as exc:
        validate_manifest(bad)
    assert "unknown capabilities ['network.egress']" in str(exc.value)


@pytest.mark.parametrize("drop_cap, section", [("tools.register", "tools"), ("ui.slot.register", "ui"),
                                               ("webhooks.receive", "webhooks"), ("jobs.schedule", "jobs"),
                                               ("events.subscribe", "events")])
def test_a_section_needs_its_capability(drop_cap, section):
    bad = _example_dict()
    bad["spec"]["capabilities"].remove(drop_cap)
    with pytest.raises(ExtensionContractError) as exc:
        validate_manifest(bad)
    assert f"spec.{section} is declared but {drop_cap} is not in capabilities" in str(exc.value)


def test_a_capability_needs_its_section_and_unknown_fields_are_refused():
    bad = _example_dict()
    del bad["spec"]["webhooks"]
    with pytest.raises(ExtensionContractError, match="webhooks.receive needs a non-empty spec.webhooks"):
        validate_manifest(bad)
    bad = _example_dict()
    bad["spec"]["network"] = {"hosts": ["*"]}
    with pytest.raises(ExtensionContractError, match="unknown field"):
        validate_manifest(bad)
    bad = _example_dict()
    bad["spec"]["jobs"][0]["schedule"] = "whenever"
    with pytest.raises(ExtensionContractError, match="schedule"):
        validate_manifest(bad)
    bad = _example_dict()
    bad["spec"]["ui"]["slots"][0]["entry"] = "../../etc/passwd.html"
    with pytest.raises(ExtensionContractError, match="inside the bundle"):
        validate_manifest(bad)
    bad = _example_dict()
    bad["spec"]["ui"]["slots"][0]["type"] = "global_header"
    with pytest.raises(ExtensionContractError):
        validate_manifest(bad)


def test_a_plugin_v1_manifest_is_not_an_extension():
    with pytest.raises(ExtensionContractError, match="boss.plugin/v1"):
        validate_manifest({"apiVersion": "boss.plugin/v1", "kind": "InfraPlugin"})


def test_the_v1_field_sets_are_pinned():
    assert set(ExtensionManifest.model_fields) == {"apiVersion", "kind", "metadata", "spec"}
    assert set(Spec.model_fields) == {"entrypoint", "checksum", "capabilities", "runtime", "tools",
                                      "events", "jobs", "webhooks", "ui", "state"}


def test_cron_and_interval_schedules():
    t = datetime(2026, 11, 13, 10, 7, 30, tzinfo=timezone.utc)
    assert jobs.next_fire("0 * * * *", t) == datetime(2026, 11, 13, 11, 0, tzinfo=timezone.utc)
    assert jobs.next_fire("*/15 * * * *", t) == datetime(2026, 11, 13, 10, 15, tzinfo=timezone.utc)
    assert jobs.next_fire("every 10m", t) == t + timedelta(minutes=10)
    with pytest.raises(ValueError):
        jobs.parse_schedule("every 5s")


# --------------------------------------------------------------------------------------------- #
# 2. install: checksum + consent; the trust gate refuses changed files
# --------------------------------------------------------------------------------------------- #

def test_install_refuses_a_wrong_checksum_a_tampered_bundle_and_partial_consent(env, tmp_path):
    caps = list(load_manifest(EXAMPLE).spec.capabilities)
    with env["engine"].begin() as c:
        with pytest.raises(registry.ExtensionRefused) as exc:
            registry.install(c, env["settings"], str(EXAMPLE), checksum="sha256:" + "0" * 64,
                             accept=caps, actor="op")
        assert exc.value.code == "checksum_mismatch" and "the checksum you gave" in str(exc.value)
        with pytest.raises(registry.ExtensionRefused) as exc:
            registry.install(c, env["settings"], str(EXAMPLE), checksum=bundle_checksum(EXAMPLE),
                             accept=caps[:2], actor="op")
        assert exc.value.code == "consent_required"
    tampered = tmp_path / "tampered"
    shutil.copytree(EXAMPLE, tampered)
    (tampered / "extension.py").write_text((tampered / "extension.py").read_text() + "\n# changed\n")
    preview = registry.preview(str(tampered))
    assert preview["trust"]["trusted"] is False
    with env["engine"].begin() as c:
        with pytest.raises(registry.ExtensionRefused) as exc:
            registry.install(c, env["settings"], str(tampered), checksum=bundle_checksum(tampered),
                             accept=caps, actor="op")
        assert exc.value.code == "checksum_mismatch" and "manifest" in str(exc.value)
        assert c.execute(text("SELECT count(*) FROM extension_installs")).scalar() == 0


def test_install_from_an_archive_and_the_preview_shows_the_consent(env, tmp_path):
    archive = shutil.make_archive(str(tmp_path / "bundle"), "gztar", root_dir=EXAMPLE)
    preview = registry.preview(archive)
    assert preview["id"] == EXAMPLE_ID and preview["computedChecksum"] == bundle_checksum(EXAMPLE)
    assert [c["capability"] for c in preview["capabilities"]] == list(CAPABILITIES)
    assert all(c["words"] for c in preview["capabilities"])


def test_the_trust_gate_refuses_files_changed_after_install(env, tmp_path):
    src = make_bundle(tmp_path / "gate", "test.gate", PEEKER, capabilities=["tools.register"], tools_=_TOOLS)
    pid, _, _ = _project(env["engine"])
    ext = _install_example(env, source=src)
    _enable(env, "test.gate", pid)
    ok = broker.call(env["engine"], _load(env, "test.gate"), "tool.call",
                     {"name": "poke", "arguments": {"key": "k", "value": 1}}, project_id=pid)
    assert ok.ok is False and ok.code == "permission_denied"     # no state.write declared
    (ext.directory / "ext.py").write_text(PEEKER + "\n# swapped after approval\n")
    again = _load(env, "test.gate")
    assert again.verdict()["trusted"] is False
    assert "changed after it was installed" in again.verdict()["problems"][0]
    refused = broker.call(env["engine"], again, "tool.call", {"name": "peek", "arguments": {"key": "k"}},
                          project_id=pid)
    assert refused.ok is False and refused.code == "extension_untrusted"
    with env["engine"].begin() as c:
        with pytest.raises(registry.ExtensionRefused):
            registry.set_enabled(c, env["settings"], "test.gate", pid, enabled=True, actor="op")


def test_the_child_itself_refuses_a_tree_that_does_not_match(tmp_path):
    """The second gate: even handed a path directly, the child imports nothing that changed."""
    src = make_bundle(tmp_path / "child", "test.child", "raise SystemExit('imported!')\n",
                      capabilities=["tools.register"], tools_=_TOOLS)
    proc = subprocess.run([sys.executable, "-m", "boss_engine.extensions.child", str(src), "ext:Ext",
                           "sha256:" + "1" * 64], input=json.dumps({"method": "tool.call"}) + "\n",
                          capture_output=True, text=True, timeout=60,
                          env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(REPO / "engine")})
    reply = json.loads(proc.stdout.strip().splitlines()[-1])
    assert reply["op"] == "refused" and reply["code"] == "checksum_changed"
    assert "imported!" not in proc.stderr


# --------------------------------------------------------------------------------------------- #
# 3. tools.register: through tool policy, the receipt and the audit row
# --------------------------------------------------------------------------------------------- #

def _audit(eng, pid: str, tool: str) -> list[dict]:
    with eng.begin() as c:
        return [dict(r) for r in c.execute(text(
            "SELECT tool, server_id, decision, outcome, args_sha256 FROM tool_call_audit "
            "WHERE project_id = :p AND tool = :t ORDER BY recorded_at, id"), {"p": pid, "t": tool}).mappings()]


@pytest.fixture(scope="module")
def example(env):
    _install_example(env)
    return _load(env)


def test_extension_tools_ride_tool_policy_receipts_and_the_audit_log(env, example):
    from boss_engine.services import mcp_registry
    from boss_engine.services.mcp_client import MCPClient, forget_sessions

    eng, settings = env["engine"], env["settings"]
    pid, org, ws = _project(eng)
    view = _enable(env, EXAMPLE_ID, pid)
    assert view["enabled"] is True
    with eng.begin() as c:
        server_id = registry.load(c, EXAMPLE_ID).projects[pid]["mcp_server_id"]
        server = mcp_registry.get_mcp_server(c, pid, server_id)
    assert server["url"].endswith(f"/v1/ext/{EXAMPLE_ID}/projects/{pid}/mcp")
    assert server["provenance"]["source"] == "extension" and mcp_registry.reviewed(server)
    assert server["auth"]["type"] == "bearer" and server["auth"]["set"] is True

    # the registry's own read: the tools appear like any MCP server's (what the agent loop offers)
    forget_sessions()
    with eng.begin() as c:
        headers = mcp_registry.auth_headers(c, settings, pid, server)
    listed = MCPClient(settings).list_tools(server["url"], headers=headers)
    assert [t["name"] for t in listed] == ["add_note", "list_notes", "clear_notes"]
    assert mcp_registry.tool_policy(server, "add_note", 1) == "allow"
    assert mcp_registry.tool_policy(server, "clear_notes", 2) == "ask"

    with eng.begin() as c:
        out = mcp_registry.call_tool_with_receipt(c, settings, pid, server, "add_note",
                                                  {"text": "shipped v2"}, dedupe_key=f"t1:{pid}",
                                                  tenant_scope=(org, ws))
    assert out["output"] == {"ok": True, "count": 1}
    rows = _audit(eng, pid, "add_note")
    assert len(rows) == 1 and rows[0]["decision"] == "allow" and rows[0]["server_id"] == server_id
    assert rows[0]["outcome"] not in ("refused", "failed") and rows[0]["args_sha256"]
    with eng.begin() as c:
        receipt = c.execute(text("SELECT status, effect_kind FROM receipts WHERE id = :i"),
                            {"i": out["receipt_id"]}).mappings().first()
    assert receipt["status"] == "succeeded" and receipt["effect_kind"] == "mcp.tool.call"

    with eng.begin() as c:
        listed_notes = mcp_registry.call_tool_with_receipt(c, settings, pid, server, "list_notes", {},
                                                           dedupe_key=f"t2:{pid}", tenant_scope=(org, ws))
    assert [n["text"] for n in listed_notes["output"]["notes"]] == ["shipped v2"]

    # tier 2: Ask — refused without a person's approval, and the refusal is audited
    with eng.begin() as c:
        with pytest.raises(mcp_registry.McpServerUnreviewed):
            mcp_registry.call_tool_with_receipt(c, settings, pid, server, "clear_notes", {},
                                                dedupe_key=f"t3:{pid}", tenant_scope=(org, ws))
    assert [(r["decision"], r["outcome"]) for r in _audit(eng, pid, "clear_notes")] == [("ask", "refused")]

    # the operator turns a tool Off: it never runs, and that is audited too
    with eng.begin() as c:
        mcp_registry.set_tool_policy(c, pid, server_id, "list_notes", "off", actor="op")
        server = mcp_registry.get_mcp_server(c, pid, server_id)
    with eng.begin() as c:
        with pytest.raises(mcp_registry.McpToolOff):
            mcp_registry.call_tool_with_receipt(c, settings, pid, server, "list_notes", {},
                                                dedupe_key=f"t4:{pid}", tenant_scope=(org, ws))
    assert _audit(eng, pid, "list_notes")[-1]["outcome"] == "refused"

    # disabling the extension switches its server off
    with eng.begin() as c:
        registry.set_enabled(c, settings, EXAMPLE_ID, pid, enabled=False, actor="op")
        assert mcp_registry.get_mcp_server(c, pid, server_id)["enabled"] is False


def test_the_mcp_endpoint_refuses_a_wrong_token_and_a_project_it_is_off_in(env, example):
    pid, _, _ = _project(env["engine"])
    status, body = tools.handle(env["engine"], EXAMPLE_ID, pid, "Bearer nope",
                                {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert status == 401
    token = registry.mcp_token(_load(env), pid)
    status, body = tools.handle(env["engine"], EXAMPLE_ID, pid, f"Bearer {token}",
                                {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert status == 200 and "not enabled" in body["error"]["message"]


def test_a_slow_tool_times_out_in_the_broker(env, tmp_path):
    src = make_bundle(tmp_path / "slow", "test.slow", PEEKER, capabilities=["tools.register"],
                      tools_=_TOOLS, timeout=1)
    _install_example(env, source=src)
    started = time.monotonic()
    outcome = broker.call(env["engine"], _load(env, "test.slow"), "tool.call",
                          {"name": "nap", "arguments": {}}, project_id="p")
    assert outcome.ok is False and outcome.code == "timeout"
    assert time.monotonic() - started < 4


# --------------------------------------------------------------------------------------------- #
# 4. state: namespaced; two extensions cannot read each other
# --------------------------------------------------------------------------------------------- #

def test_state_is_namespaced_per_extension(env, example, tmp_path):
    eng = env["engine"]
    pid, _, _ = _project(eng)
    src = make_bundle(tmp_path / "peeker", "test.peeker", PEEKER,
                      capabilities=["tools.register", "state.read", "state.write"], tools_=_TOOLS)
    _install_example(env, source=src)
    _enable(env, EXAMPLE_ID, pid)
    _enable(env, "test.peeker", pid)
    wrote = broker.call(eng, _load(env), "tool.call", {"name": "add_note", "arguments": {"text": "mine"}},
                        project_id=pid)
    assert wrote.ok, wrote.error
    peeker = _load(env, "test.peeker")
    peek = broker.call(eng, peeker, "tool.call", {"name": "peek", "arguments": {"key": "ui:notes"}},
                       project_id=pid)
    assert peek.ok and peek.result == {"value": None, "keys": []}
    poke = broker.call(eng, peeker, "tool.call",
                       {"name": "poke", "arguments": {"key": "ui:notes", "value": ["forged"]}}, project_id=pid)
    assert poke.ok
    with eng.begin() as c:
        _, mine = state_store.get(c, EXAMPLE_ID, pid, "ui:notes")
        _, theirs = state_store.get(c, "test.peeker", pid, "ui:notes")
    assert [n["text"] for n in mine] == ["mine"] and theirs == ["forged"]
    # and another PROJECT is another namespace for the same extension
    other = broker.call(eng, peeker, "tool.call", {"name": "peek", "arguments": {"key": "ui:notes"}},
                        project_id=pid + "_other")
    assert other.result["value"] is None
    # values are size-capped
    big = broker.call(eng, peeker, "tool.call",
                      {"name": "poke", "arguments": {"key": "big", "value": "x" * 20000}}, project_id=pid)
    assert big.ok is False and "at most 16384 bytes" in (big.error or "")


# --------------------------------------------------------------------------------------------- #
# 5. events.subscribe: filtered, acked, at least once
# --------------------------------------------------------------------------------------------- #

def test_events_are_filtered_delivered_and_acked_at_least_once(env, example, monkeypatch):
    from boss_engine.services.core import emit_event

    eng = env["engine"]
    pid, _, _ = _project(eng)
    other, _, _ = _project(eng)
    _enable(env, EXAMPLE_ID, pid)
    with eng.begin() as c:
        emit_event(c, pid, "deploy.succeeded", "deployer", "deployment", "dep_1", {"app": "shop"})
        emit_event(c, pid, "workflow.started", "x", "workflow_run", "wr_1", {})
        emit_event(c, other, "deploy.failed", "deployer", "deployment", "dep_9", {})   # not enabled there
    ext = _load(env)

    # a delivery that fails leaves the cursor where it was: the same event comes again
    real_call = broker.call
    monkeypatch.setattr(broker, "call", lambda *a, **k: broker.CallOutcome(False, code="timeout",
                                                                           error="boom"))
    failed = events.pump(eng, ext, pid, grace_seconds=0)
    assert failed["ok"] is False and failed["delivered"] == 1 and failed["acked"] == 0
    monkeypatch.setattr(broker, "call", real_call)

    first = events.pump(eng, ext, pid, grace_seconds=0)
    assert first == {"ok": True, "delivered": 1, "acked": 1}
    with eng.begin() as c:
        _, notes = state_store.get(c, EXAMPLE_ID, pid, "ui:notes")
        cur = events.cursor(c, EXAMPLE_ID, pid)
    assert [n["text"] for n in notes] == ["deploy.succeeded on deployment dep_1"]
    assert cur["delivered"] == 1 and cur["last_event_id"]
    assert events.pump(eng, ext, pid, grace_seconds=0)["delivered"] == 0
    assert events.pump(eng, ext, other, grace_seconds=0)["ok"] is False      # not enabled there
    # an event inside the grace window waits
    with eng.begin() as c:
        emit_event(c, pid, "deploy.started", "deployer", "deployment", "dep_2", {})
    assert events.pump(eng, ext, pid, grace_seconds=60)["delivered"] == 0
    assert events.pump(eng, ext, pid, grace_seconds=0)["acked"] == 1


# --------------------------------------------------------------------------------------------- #
# 6. jobs.schedule: the lease lets exactly one process fire
# --------------------------------------------------------------------------------------------- #

def test_the_job_lease_lets_one_process_fire(env, example):
    eng = env["engine"]
    pid, _, _ = _project(eng)
    _enable(env, EXAMPLE_ID, pid)
    t0 = datetime(2026, 11, 13, 9, 30, tzinfo=timezone.utc)
    with eng.begin() as c:
        c.execute(text("DELETE FROM extension_job_leases WHERE extension_id = :e"), {"e": EXAMPLE_ID})
        jobs.ensure_row(c, EXAMPLE_ID, "digest", t0)
        assert jobs.claim(c, EXAMPLE_ID, "digest", "A", t0, timedelta(minutes=5)) is True
        assert jobs.claim(c, EXAMPLE_ID, "digest", "B", t0, timedelta(minutes=5)) is False
        assert jobs.claim(c, EXAMPLE_ID, "digest", "B", t0 + timedelta(minutes=6), timedelta(minutes=5)) is True
        c.execute(text("DELETE FROM extension_job_leases WHERE extension_id = :e"), {"e": EXAMPLE_ID})

    jobs.run_due(eng, holder="A", now=t0)          # first sight: schedules 10:00, fires nothing
    due = t0 + timedelta(minutes=31)
    results: dict[str, list] = {}

    def tick(holder: str) -> None:
        results[holder] = [r for r in jobs.run_due(eng, holder=holder, now=due) if r["job"] == "digest"]

    threads = [threading.Thread(target=tick, args=(h,)) for h in ("A", "B", "C")]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=120)
    fired = [r for rs in results.values() for r in rs]
    assert len(fired) == 1 and fired[0]["ok"] is True, results
    with eng.begin() as c:
        _, runs = state_store.get(c, EXAMPLE_ID, None, "digest:runs")
        row = c.execute(text("SELECT holder, next_run_at, last_status FROM extension_job_leases "
                             "WHERE extension_id = :e AND job = 'digest'"), {"e": EXAMPLE_ID}).mappings().first()
    assert runs == 1
    assert row["holder"] == "" and row["last_status"] == "ok"
    assert row["next_run_at"] == datetime(2026, 11, 13, 11, 0, tzinfo=timezone.utc)
    assert [r for r in jobs.run_due(eng, holder="A", now=due) if r["job"] == "digest"] == []


# --------------------------------------------------------------------------------------------- #
# 7. webhooks.receive: the per-hook secret is checked before the child starts
# --------------------------------------------------------------------------------------------- #

def test_webhooks_refuse_a_bad_secret_and_route_a_signed_body(env, example, monkeypatch):
    eng = env["engine"]
    pid, _, _ = _project(eng)
    _enable(env, EXAMPLE_ID, pid)
    ext = _load(env)
    body = json.dumps({"note": "deployed from CI"}).encode()
    spawned: list = []
    real_call = broker.call
    monkeypatch.setattr(broker, "call", lambda *a, **k: spawned.append(a) or real_call(*a, **k))

    assert webhooks.receive(eng, ext, "deploy", body, {})[0] == 401
    wrong = "sha256=" + hmac.new(b"guess", body, hashlib.sha256).hexdigest()
    assert webhooks.receive(eng, ext, "deploy", body, {"X-Boss-Signature": wrong})[0] == 401
    other_hook = webhooks.sign(webhooks.hook_secret(ext, "other"), body)
    assert webhooks.receive(eng, ext, "deploy", body, {"X-Boss-Signature": other_hook})[0] == 401
    assert webhooks.receive(eng, ext, "nope", body, {})[0] == 404
    assert spawned == []                                   # nothing reached the extension

    good = webhooks.sign(webhooks.hook_secret(ext, "deploy"), body)
    status, answer = webhooks.receive(eng, ext, "deploy", body,
                                      {"X-Boss-Signature": good, "User-Agent": "ci/1", "Authorization": "x"})
    assert (status, answer["received"]) == (202, True)
    with eng.begin() as c:
        _, inbox = state_store.get(c, EXAMPLE_ID, None, "webhook:inbox")
    assert inbox[-1]["text"] == "deployed from CI" and inbox[-1]["from"] == "ci/1"


# --------------------------------------------------------------------------------------------- #
# 8. ui.slot.register: the frame document and its CSP
# --------------------------------------------------------------------------------------------- #

def test_the_panel_document_is_locked_into_an_opaque_sandbox(env, example):
    eng = env["engine"]
    pid, _, _ = _project(eng)
    _enable(env, EXAMPLE_ID, pid)
    ext = _load(env)
    html = ui.document(ext, "notes")
    head = html[:html.index("</script>") + 9]
    assert '<meta http-equiv="Content-Security-Policy"' in head and "connect-src 'none'" in head
    assert "window.bossExt" in head and "parent.postMessage" in head
    assert html.index("Content-Security-Policy") < html.index('id="notes"')
    csp = ui.headers()["Content-Security-Policy"]
    assert csp.startswith("sandbox allow-scripts;") and "allow-same-origin" not in csp
    assert "frame-ancestors 'self'" in csp and "connect-src 'none'" in csp and "default-src 'none'" in csp
    with pytest.raises(registry.ExtensionRefused):
        ui.document(ext, "missing")

    broker.call(eng, ext, "tool.call", {"name": "add_note", "arguments": {"text": "panel"}}, project_id=pid)
    ctx = ui.context(eng, ext, "notes", pid)
    assert ctx["slotType"] == "project_panel" and [n["text"] for n in ctx["data"]["notes"]][-1] == "panel"
    assert set(ctx) == {"extensionId", "version", "slot", "slotType", "title", "projectId", "data"}
    with pytest.raises(registry.ExtensionRefused):
        ui.context(eng, ext, "notes", "p_not_enabled")


def test_the_api_routes_serve_the_frame_with_its_csp_and_verify_webhooks(env, example):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from apps.api.app.routes_extensions import build_extensions_router

    eng = env["engine"]
    pid, _, _ = _project(eng)
    _enable(env, EXAMPLE_ID, pid)
    app = FastAPI()
    app.include_router(build_extensions_router(engine_getter=lambda: eng, settings_getter=lambda: env["settings"]))
    client = TestClient(app)
    frame = client.get(f"/v1/ext/{EXAMPLE_ID}/ui/notes", params={"project_id": pid})
    assert frame.status_code == 200 and frame.headers["content-type"].startswith("text/html")
    assert frame.headers["content-security-policy"] == ui.HEADER_CSP
    assert frame.headers["x-content-type-options"] == "nosniff"
    assert "window.bossExt" in frame.text
    listed = client.get("/v1/extensions", params={"project_id": pid}).json()
    row = next(e for e in listed["extensions"] if e["id"] == EXAMPLE_ID)
    assert row["enabled"] is True and row["trust"]["trusted"] is True
    slots = client.get("/v1/extensions/slots", params={"project_id": pid, "type": "project_panel"}).json()
    assert {"extensionId": EXAMPLE_ID, "name": "notes", "title": "Deploy notes", "type": "project_panel"} in slots["slots"]
    body = b'{"note": "via route"}'
    assert client.post(f"/v1/ext/{EXAMPLE_ID}/webhook/deploy", content=body).status_code == 401
    sig = webhooks.sign(webhooks.hook_secret(_load(env), "deploy"), body)
    hit = client.post(f"/v1/ext/{EXAMPLE_ID}/webhook/deploy", content=body, headers={"X-Boss-Signature": sig})
    assert hit.status_code == 202 and hit.json()["received"] is True
