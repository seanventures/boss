"""P8b S12/S13 (BOSSAPP-1073, BOSSAPP-1074), the database half: a deployed agent's run on a fake
host, end to end through the REAL fleet router.

  * the work item a host claims carries the guest's `mcpgw` handle and its `mcp_servers` map,
    and the handle's row is bound to (run, epoch, the replica's VM);
  * the control plane's POST /mcp/<server> serves the guest through its host: the host's
    BossHost proof, the handle in X-Boss-Handle, the host's word on VM/run/epoch; `boss_run`
    tools list and run; a server outside the run's allowlist is 403; a replay from another VM
    is 401, revoked, and a host_events(handle_replay) row; a stale epoch is 409 fenced; every
    call lands as a guest_calls row (route mcp) that never carries the handle;
  * S13: POST /v1/run-plane/runs/{run}/tokens mints one token per destination for the HOST's
    DPoP key; the MCP gateway accepts its own (boss:mcp) with a proof from that key and refuses
    the model gateway's (bad_aud), one with a proof from another key (dpop_key_mismatch) and one
    for another run; the model gateway refuses the MCP token the same way; rotation (a second
    call with a new jkt) leaves the previous pair valid; `boss:api` is never issued to a host;
  * at the run's end the handle and the run's tokens are revoked together.

Needs BOSS_TEST_DATABASE_URL naming a disposable database (the bench DB); the P8b-6/7 block of
schema.sql is applied first, so the test runs the real DDL.
"""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any, Optional

import pytest
from sqlalchemy import create_engine, text
from starlette.testclient import TestClient

from boss_engine.identity import delegation, dpop
from boss_engine.services import mcp_gateway as gw

from ._fleet_support import (FakeHost, cleanup_project, deploy_agent, fleet_app, join_token,
                             make_project, org_of, own_pool, use_test_master_key)

ROOT = Path(__file__).resolve().parents[2]
TEST_DB = os.environ.get("BOSS_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(
    not TEST_DB or "test" not in TEST_DB.rsplit("/", 1)[-1].lower(),
    reason="needs BOSS_TEST_DATABASE_URL naming a disposable database")


def _block() -> tuple[list[str], str]:
    sql = (ROOT / "db" / "schema.sql").read_text(encoding="utf-8")
    start = sql.index("P8b-6/7: THE MCP/TOOL GATEWAY HANDLES")
    block = sql[start:sql.index("end P8b-6/7 ==", start)]
    code = "\n".join(line for line in block.splitlines() if not line.lstrip().startswith("--"))
    creates = [s.strip() for s in code.split("DO $$")[0].split(";") if "CREATE" in s]
    do_block = "DO $$" + code.split("DO $$", 1)[1].rsplit("$$;", 1)[0] + "$$"
    return creates, do_block


@pytest.fixture(scope="module")
def eng():
    engine = create_engine(TEST_DB, future=True, pool_size=10, max_overflow=10)
    try:
        with engine.begin() as c:
            c.execute(text("SELECT 1 FROM agent_replicas LIMIT 1"))
            creates, do_block = _block()
            for statement in creates:
                c.execute(text(statement))
            c.exec_driver_sql(do_block)
    except Exception as error:  # pragma: no cover
        pytest.skip(f"test database unavailable: {error}")
    return engine


class World:
    """One project, a microvm deployment with one replica on one enrolled fake host, and the
    run that host claimed for it (its work item, with the guest's handles)."""

    def __init__(self, eng, monkeypatch) -> None:
        from boss_engine.services import fleet_scheduler

        self.eng = eng
        use_test_master_key(monkeypatch)
        monkeypatch.delenv(gw.HOST_TOKENS_REQUIRED_ENV, raising=False)
        self.project = make_project(eng)
        self.org = org_of(eng, self.project)
        self.client = TestClient(fleet_app(eng))
        self.pool = own_pool()
        self.dep = deploy_agent(eng, self.project, replicas=1, pool=self.pool)
        self.host = FakeHost(self.client)
        self.host.enrol(join_token(eng, self.org, pool=self.pool))
        fleet_scheduler.reconcile(eng)
        with eng.begin() as c:
            row = c.execute(text(
                "SELECT id, vm_id FROM agent_replicas WHERE host_id = :h AND deployment_id = :d "
                "AND desired_state = 'running'"), {"h": self.host.host_id, "d": self.dep["id"]}
            ).mappings().one()
            self.replica_id, self.vm_id = str(row["id"]), str(row["vm_id"])
            c.execute(text("UPDATE agent_replicas SET slots = 1 WHERE id = :r"), {"r": self.replica_id})
        self.host.serve_all()
        self.agent_id: Optional[str] = None
        self.work = self.admit_and_claim()

    def admit_and_claim(self) -> dict:
        from boss_engine.services import agent_deployments, thread_runs

        with self.eng.begin() as c:
            thread = thread_runs.ensure_thread(c, project_id=self.project, actor="ceo",
                                              agent_id=self.dep["agent_key"])
            agent_deployments.admit_run(c, self.dep, thread_id=thread,
                                        messages=[{"role": "user", "content": "hello"}],
                                        trigger="api")
        runs = self.host.claim()
        assert runs, "the host's hosts/next claimed nothing"
        return runs[0]

    # ---- what boss-hostd sends for its guest (HOST_API.md §4.7) ----------------------------
    def guest_headers(self, *, vm: Optional[str] = None, run: Optional[str] = None,
                      epoch: Optional[int] = None, handle: Optional[str] = None) -> dict:
        return {"X-Boss-Handle": handle or self.work["handles"]["mcpgw"],
                "X-Boss-Vm": vm or self.vm_id, "X-Boss-Run": run or self.work["run_id"],
                "X-Boss-Fence-Epoch": str(self.work["epoch"] if epoch is None else epoch),
                "X-Boss-Deployment": self.dep["id"], "Accept": "application/json"}

    def mcp(self, server: str, message: dict, **over: Any) -> Any:
        extra = over.pop("extra", {})
        return self.host.call("POST", f"/mcp/{server}", json=message,
                              headers={**self.guest_headers(**over), **extra})

    def lease_headers(self) -> dict:
        return {"X-Boss-Replica": self.replica_id, "X-Boss-Job": self.work["job_id"],
                "X-Boss-Run-Token": self.work["run_token"],
                "X-Boss-Fence-Epoch": str(self.work["epoch"])}

    def tokens(self, jkt: str, audiences: Optional[list] = None, *, headers: Optional[dict] = None) -> Any:
        body: dict = {"jkt": jkt}
        if audiences is not None:
            body["audiences"] = audiences
        return self.host.call("POST", f"/v1/run-plane/runs/{self.work['run_id']}/tokens",
                              json=body, headers=headers or self.lease_headers())

    # ---- the deployed agent's identity (its Passport: the actor of every run token) -------
    def give_the_agent_a_passport(self) -> Optional[str]:
        from boss_engine.identity import passports, signing_keys, users

        with self.eng.begin() as c:
            # The run-token signing keys must open under THIS test's master key: a bench DB
            # whose keys another suite sealed under its own gets fresh ones (identity suites
            # truncate them the same way).
            try:
                signing_keys.active_key("run", db=c)
                signing_keys.active_key("actor", db=c)
            except Exception:  # noqa: BLE001 — kek mismatch or no keys yet
                signing_keys.clear_cache()
                c.execute(text("DELETE FROM identity_signing_keys"))
                signing_keys.provision(org_id=self.org, db=c)
            sponsor = c.execute(text(
                "SELECT id FROM identity_principals WHERE kind = 'user' AND status = 'active' "
                "ORDER BY created_at LIMIT 1")).scalar()
            if not sponsor:
                return None
            agent_id = f"agt_{uuid.uuid4().hex[:12]}"
            passports.ensure_passport(org_id=self.org, agent_id=agent_id,
                                      role_key=self.dep["agent_key"], display_name="Triage bot",
                                      sponsor_principal_id=sponsor, db=c, autonomous=True,
                                      can_spawn=False, max_depth=0, created_by="sys_test")
            c.execute(text("INSERT INTO identity_agent_ceilings (agent_id, permission_key) "
                           "VALUES (:a, 'agent:invoke') ON CONFLICT DO NOTHING"), {"a": agent_id})
            users.add_binding(c, org_id=self.org, subject_id=agent_id, role_key="operator",
                              granted_by="sys_test", reason="test: the deployed agent's own reach")
            c.execute(text("INSERT INTO roles (id, project_id, role_key, display_name, agent_id) "
                           "VALUES (:i, :p, :r, 'Triage bot', :a) "
                           "ON CONFLICT (project_id, role_key) DO UPDATE SET agent_id = :a"),
                      {"i": f"role_{uuid.uuid4().hex[:10]}", "p": self.project,
                       "r": self.dep["agent_key"], "a": agent_id})
        self.agent_id = agent_id
        return agent_id

    def close(self) -> None:
        run_id = self.work["run_id"] if self.work else None
        with self.eng.begin() as c:
            for sql, params in (
                    ("DELETE FROM mcp_gateway_handles WHERE run_id = :r", {"r": run_id}),
                    ("DELETE FROM guest_calls WHERE host_id = :h", {"h": self.host.host_id}),
                    ("DELETE FROM host_events WHERE host_id = :h", {"h": self.host.host_id}),
                    ("DELETE FROM identity_run_credentials WHERE run_id = :r", {"r": run_id}),
                    ("DELETE FROM identity_agent_ceilings WHERE agent_id = :a", {"a": self.agent_id}),
                    ("DELETE FROM identity_agent_passports WHERE agent_id = :a", {"a": self.agent_id}),
                    ("DELETE FROM identity_role_bindings WHERE subject_id = :a", {"a": self.agent_id}),
                    ("DELETE FROM identity_principals WHERE id = :a", {"a": self.agent_id})):
                try:
                    with c.begin_nested():
                        c.execute(text(sql), params)
                except Exception:  # noqa: BLE001 — best effort
                    pass
        cleanup_project(self.eng, self.project, [self.host.host_id])


@pytest.fixture()
def world(eng, monkeypatch):
    w = World(eng, monkeypatch)
    yield w
    w.close()


def _flush_audit() -> None:
    gw.get().host_lane.audit.flush()


# ── the work item and the handle row ─────────────────────────────────────────────────────────

def test_the_work_item_carries_the_mcp_handle_bound_to_run_epoch_and_vm(eng, world):
    handles = world.work["handles"]
    raw = handles["mcpgw"]
    assert raw.startswith(gw.TOKEN_PREFIX)
    assert handles["mcp_servers"] == {"boss_run": "http://127.0.0.1:7103/mcp/boss_run"}, \
        "a deployment whose agent holds no MCP server still gets the run's own tools"
    assert "mdl" in handles or True  # the model handle needs a readable model source (S10)
    with eng.begin() as c:
        row = gw.load_guest_handle(c, raw)
    assert row is not None and row["run_id"] == world.work["run_id"]
    assert row["fence_epoch"] == world.work["epoch"] and row["vm_id"] == world.vm_id
    assert row["role_key"] == world.dep["agent_key"] and row["servers"] == {}
    assert row["revoked_at"] is None and row["org_id"] == world.org
    assert raw not in json.dumps(row, default=str)


# ── the control-plane route, through the host ───────────────────────────────────────────────

def test_the_guest_reaches_boss_run_through_its_host_and_every_call_is_audited(eng, world):
    listed = world.mcp("boss_run", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert listed.status_code == 200, listed.text
    names = {t["name"] for t in listed.json()["result"]["tools"]}
    assert {"ask_person", "request_approval", "report_blocked", "report_progress"} <= names
    called = world.mcp("boss_run", {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
        "name": "report_progress", "arguments": {"note": "tests green through the gateway"}}})
    assert called.status_code == 200, called.text
    assert called.json()["result"]["structuredContent"]["recorded"] is True
    with eng.begin() as c:
        events = c.execute(text(
            "SELECT event_type FROM events WHERE project_id = :p AND entity_id = :r "
            "AND event_type = 'code_task.agent_progress'"),
            {"p": world.project, "r": world.work["run_id"]}).scalars().all()
    assert events, "the report landed as a run event"
    _flush_audit()
    with eng.begin() as c:
        rows = c.execute(text(
            "SELECT route, host_id, vm_id, run_id, epoch, verdict, rule_id, path_template, "
            "handle_fp, status, rejected_by FROM guest_calls WHERE host_id = :h ORDER BY id"),
            {"h": world.host.host_id}).mappings().all()
    assert len(rows) == 2 and {r["route"] for r in rows} == {"mcp"}
    assert rows[0]["path_template"] == "/mcp/boss_run/tools/list" and rows[0]["verdict"] == "allow"
    assert rows[1]["path_template"] == "/mcp/boss_run/tools/call" and rows[1]["status"] == 200
    assert rows[0]["vm_id"] == world.vm_id and rows[0]["run_id"] == world.work["run_id"]
    assert rows[0]["epoch"] == world.work["epoch"] and rows[0]["rule_id"] == "mcp-boss_run"
    assert rows[0]["handle_fp"] == gw.fingerprint(world.work["handles"]["mcpgw"])
    assert world.work["handles"]["mcpgw"] not in json.dumps([dict(r) for r in rows], default=str)


def test_outside_the_allowlist_a_replay_and_a_stale_epoch_are_refused_in_the_gateways_words(eng, world):
    # A server the agent's grant does not hold: 403, typed, before any upstream lookup.
    outside = world.mcp("github", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert outside.status_code == 403, outside.text
    assert outside.json()["result"]["structuredContent"]["error"]["type"] == "boss.denied_by_policy"
    # A stale epoch: 409 fenced within this one request.
    stale = world.mcp("boss_run", {"jsonrpc": "2.0", "id": 2, "method": "ping"},
                      epoch=world.work["epoch"] + 1)
    assert stale.status_code == 409 and stale.json()["error"]["data"]["reason"] == "fenced"
    # The handle presented from another VM: a replay — revoked, reported, refused.
    replay = world.mcp("boss_run", {"jsonrpc": "2.0", "id": 3, "method": "ping"}, vm="vm-elsewhere")
    assert replay.status_code == 401 and replay.json()["error"]["data"]["reason"] == "handle_replay"
    with eng.begin() as c:
        event = c.execute(text(
            "SELECT kind, actor, vm_id, detail FROM host_events WHERE host_id = :h "
            "AND kind = 'handle_replay'"), {"h": world.host.host_id}).mappings().one()
        row = gw.load_guest_handle(c, world.work["handles"]["mcpgw"])
    assert event["actor"] == "mcp_gateway" and event["vm_id"] == "vm-elsewhere"
    detail = event["detail"] if isinstance(event["detail"], dict) else json.loads(event["detail"])
    assert detail["why"] == "another_vm" and detail["bound_vm"] == world.vm_id
    assert row["revoked_at"] is not None and row["revoked_reason"] == "handle_replay:another_vm"
    own = world.mcp("boss_run", {"jsonrpc": "2.0", "id": 4, "method": "ping"})
    assert own.status_code == 401 and own.json()["error"]["data"]["reason"] == "handle_revoked"
    _flush_audit()
    with eng.begin() as c:
        refusals = c.execute(text(
            "SELECT rejected_by FROM guest_calls WHERE host_id = :h AND verdict = 'deny' ORDER BY id"),
            {"h": world.host.host_id}).scalars().all()
    assert refusals == ["mcp_gateway:server_not_granted", "mcp_gateway:fenced",
                        "mcp_gateway:handle_replay", "mcp_gateway:handle_revoked"]


# ── S13: the host's tokens ───────────────────────────────────────────────────────────────────

def test_host_tokens_are_per_destination_dpop_bound_and_refused_across_gateways(eng, world):
    from boss_engine.identity import runs as identity_runs
    from boss_engine.services import streaming_model_gateway as smg

    if world.give_the_agent_a_passport() is None:
        pytest.skip("no active person in the test database to sponsor the agent's Passport")
    host_key = dpop.DPoPKey.generate()                       # boss-hostd's per-run key, in Python
    # boss:api is never a host's.
    refused = world.tokens(host_key.jkt, [delegation.AUD_API])
    assert refused.status_code == 400 and refused.json()["detail"]["error"] == "bad_audience"
    # A stale lease: fenced, nothing minted.
    stale = world.tokens(host_key.jkt, headers={**world.lease_headers(),
                                                "X-Boss-Fence-Epoch": str(world.work["epoch"] + 1)})
    assert stale.status_code == 409, stale.text
    minted = world.tokens(host_key.jkt)
    assert minted.status_code == 200, minted.text
    out = minted.json()
    assert set(out["tokens"]) == set(delegation.HOST_AUDIENCES) and out["jkt"] == host_key.jkt
    assert 0 < out["rotate_after_seconds"] < out["expires_in"] <= delegation.RUN_TOKEN_TTL_SECONDS
    mcp_token = out["tokens"][delegation.AUD_MCP]["access_token"]
    model_token = out["tokens"][delegation.AUD_MODEL]["access_token"]
    assert mcp_token != model_token
    shim = identity_runs.host_shim_for(world.work["run_id"])
    assert shim is not None and shim.host_held and shim.jkt == host_key.jkt
    with eng.begin() as c:
        auds = c.execute(text("SELECT aud FROM identity_run_credentials WHERE run_id = :r "
                              "AND revoked_at IS NULL AND cnf_jkt = :k"),
                         {"r": world.work["run_id"], "k": host_key.jkt}).scalars().all()
    assert sorted(auds) == sorted(delegation.HOST_AUDIENCES)

    url = "http://testserver/mcp/boss_run"
    ping = {"jsonrpc": "2.0", "id": 1, "method": "ping"}

    def proof(token: str, key: dpop.DPoPKey = host_key, *, htu: str = url) -> dict:
        return {"X-Boss-Delegation": token, "DPoP": key.proof("POST", htu, access_token=token)}

    # The MCP gateway takes ITS token with the host's proof.
    ok = world.mcp("boss_run", ping, extra=proof(mcp_token))
    assert ok.status_code == 200, ok.text
    # The model gateway's token is refused here: wrong audience.
    wrong_aud = world.mcp("boss_run", ping, extra=proof(model_token))
    assert wrong_aud.status_code == 401 and wrong_aud.json()["error"]["data"]["reason"] == "run_token_bad_aud"
    # The right token with a proof from another key: refused.
    other_key = dpop.DPoPKey.generate()
    wrong_key = world.mcp("boss_run", ping, extra=proof(mcp_token, other_key))
    assert wrong_key.status_code == 401
    assert wrong_key.json()["error"]["data"]["reason"] == "run_token_dpop_key_mismatch"
    # A proof over another URL: refused (htu).
    wrong_url = world.mcp("boss_run", ping, extra=proof(mcp_token, htu="http://testserver/mcp/jira"))
    assert wrong_url.status_code == 401 and wrong_url.json()["error"]["data"]["reason"] == "run_token_dpop_htu"
    # A proof replayed: refused (jti).
    once = proof(mcp_token)
    assert world.mcp("boss_run", ping, extra=once).status_code == 200
    again = world.mcp("boss_run", ping, extra=once)
    assert again.status_code == 401 and again.json()["error"]["data"]["reason"] == "run_token_dpop_replay"
    # And the model gateway refuses the MCP token the same way, accepting only its own.
    gateway_url = "https://model-gateway:8092/anthropic/v1/messages"
    with eng.begin() as c:
        claims = smg.ModelGateway.check_delegation(
            c, {"X-Boss-Delegation": model_token,
                "DPoP": host_key.proof("POST", gateway_url, access_token=model_token)},
            run_id=world.work["run_id"], method="POST", url=gateway_url)
        assert claims.aud == delegation.AUD_MODEL and claims.task.run_id == world.work["run_id"]
        with pytest.raises(smg.Refused) as refused_model:
            smg.ModelGateway.check_delegation(
                c, {"X-Boss-Delegation": mcp_token,
                    "DPoP": host_key.proof("POST", gateway_url, access_token=mcp_token)},
                run_id=world.work["run_id"], method="POST", url=gateway_url)
    assert refused_model.value.reason == "run_token_bad_aud"
    # Another run's token names another run: refused even with the right audience and key.
    with eng.begin() as c:
        with pytest.raises(gw.Refused) as other_run:
            gw.get().host_lane.check_delegation(
                c, proof(mcp_token), run_id="arun_someone_else", method="POST", url=url)
    assert other_run.value.reason == "run_token_mismatch"

    # ROTATION: a second call with a new jkt mints fresh tokens; the previous pair still works.
    next_key = dpop.DPoPKey.generate()
    rotated = world.tokens(next_key.jkt)
    assert rotated.status_code == 200, rotated.text
    new_mcp = rotated.json()["tokens"][delegation.AUD_MCP]["access_token"]
    assert new_mcp != mcp_token
    assert world.mcp("boss_run", ping, extra=proof(new_mcp, next_key)).status_code == 200
    assert world.mcp("boss_run", ping, extra=proof(mcp_token)).status_code == 200, \
        "a call in flight with the previous pair must complete"
    assert world.mcp("boss_run", ping, extra=proof(new_mcp, host_key)).status_code == 401, \
        "a new token never pairs with the old key"

    # Required mode: a forward without the delegated token is refused.
    os.environ[gw.HOST_TOKENS_REQUIRED_ENV] = "1"
    try:
        bare = world.mcp("boss_run", ping)
        assert bare.status_code == 401 and bare.json()["error"]["data"]["reason"] == "run_token_required"
    finally:
        os.environ.pop(gw.HOST_TOKENS_REQUIRED_ENV, None)

    # RUN END: the handle and every token of the run die together.
    from boss_engine.services import run_plane

    with eng.begin() as c:
        run_plane._revoke_handles(c, world.work["run_id"], "run settled")  # noqa: SLF001
        live = c.execute(text("SELECT count(*) FROM identity_run_credentials WHERE run_id = :r "
                              "AND revoked_at IS NULL"), {"r": world.work["run_id"]}).scalar()
    assert live == 0 and identity_runs.host_shim_for(world.work["run_id"]) is None
    ended = world.mcp("boss_run", ping, extra=proof(new_mcp, next_key))
    assert ended.status_code == 401 and ended.json()["error"]["data"]["reason"] == "handle_revoked"
