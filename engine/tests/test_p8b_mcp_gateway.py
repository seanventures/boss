"""P8b S12/S13 (BOSSAPP-1073, BOSSAPP-1074): the host-facing MCP/tool gateway and host-held
tokens, with no database.

  * the host's forward is read exactly as the model gateway reads it: the handle MOVED into
    X-Boss-Handle (a handle left in Authorization is not read), the host's word on VM, run and
    epoch, a trace id;
  * the handle row is checked against that word: unknown / revoked / expired refused; a handle
    bound to VM A presented from VM B is a REPLAY (revoked, a host_events(handle_replay) row,
    401); a code task's unbound handle is bound to the first VM that redeems it; a stale epoch
    (the host's, or the run's current one in Postgres) is 409 fenced within that one request;
  * the grant the gateway's policy, parking and receipts use is the row's allowlist: a server or
    tool outside it is refused before any upstream lookup (403 / boss.denied_by_policy);
  * S13: the per-destination audiences exist and a host is never issued boss:api; a host-held
    shim has no private key and cannot sign; a missing delegated token is tolerated unless
    BOSS_HOST_TOKENS_REQUIRED; the model gateway's check follows the same rule;
  * the Go route table (routes.go) carries HostProof and the boss:mcp audience on the MCP rule,
    and the contract names agree across the three sides (guest URL, endpoint, header names);
  * upstream credentials never appear in a guest-visible result: a tool failure's text is
    redacted through the run redactor's registry.

The database cases (rows in Postgres, the fence read from jobs, the audit batch in guest_calls,
the run's work item carrying `mcpgw` + `mcp_servers`, the tokens endpoint and the audience
cross-refusal with real DPoP proofs) live in test_p8b_mcp_gateway_db.py.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import time
from pathlib import Path
from typing import Any, Optional

import pytest

from boss_engine.identity import delegation
from boss_engine.services import mcp_gateway as gw

ROOT = Path(__file__).resolve().parents[2]
HANDLE = gw.TOKEN_PREFIX + "b" * 64
NOW = dt.datetime.now(dt.timezone.utc)


def _row(**over: Any) -> dict:
    base = dict(id="mcph_1", token_hash=gw._hash(HANDLE), org_id="org_1", project_id="p_1",
                run_id="run_1", fence_epoch=3, vm_id="vm_a", deployment_id="dep_1",
                role_key="forge", servers={"jira": {"server_id": "mcp_jira",
                                                    "granted_tools": ["search", "comment"]}},
                expires_at=NOW + dt.timedelta(hours=1), revoked_at=None)
    base.update(over)
    return base


class FakeResult:
    def __init__(self, row: Optional[dict] = None, scalar: Any = None) -> None:
        self._row, self._scalar, self.rowcount = row, scalar, 1 if row or scalar else 0

    def mappings(self) -> "FakeResult":
        return self

    def first(self) -> Optional[dict]:
        return dict(self._row) if self._row else None

    def scalar(self) -> Any:
        return self._scalar


class FakeConn:
    """Answers the gateway's SQL by what it asks: the handle row, the run's current epoch
    (jobs / code_task_runs), the UPDATEs it issues (recorded), the host_events insert."""

    def __init__(self, row: Optional[dict], *, job_epoch: Optional[int] = None) -> None:
        self.row, self.job_epoch = row, job_epoch
        self.updates: list[tuple[str, dict]] = []
        self.events: list[dict] = []

    def execute(self, stmt: Any, params: Optional[dict] = None) -> FakeResult:
        sql = str(getattr(stmt, "text", stmt))
        params = params or {}
        if sql.startswith("UPDATE mcp_gateway_handles SET vm_id"):
            self.updates.append(("bind", params))
            if self.row is not None and self.row.get("vm_id") is None:
                self.row["vm_id"] = params["v"]
            return FakeResult()
        if sql.startswith("UPDATE mcp_gateway_handles SET revoked_at"):
            self.updates.append(("revoke", params))
            if self.row is not None:
                self.row["revoked_at"] = NOW
            return FakeResult()
        if sql.startswith("INSERT INTO host_events"):
            self.events.append(dict(params))
            return FakeResult()
        if "FROM mcp_gateway_handles WHERE token_hash" in sql and sql.startswith("SELECT vm_id"):
            return FakeResult(scalar=(self.row or {}).get("vm_id"))
        if "FROM mcp_gateway_handles WHERE token_hash" in sql:
            return FakeResult(row=self.row)
        if "SELECT fence_epoch FROM jobs" in sql:
            return FakeResult(scalar=self.job_epoch)
        if "SELECT fence_epoch FROM code_task_runs" in sql:
            return FakeResult(scalar=None)
        raise AssertionError(f"unexpected SQL: {sql[:80]}")

    def begin_nested(self):
        import contextlib

        return contextlib.nullcontext()


def _gateway() -> gw.McpGateway:
    return gw.McpGateway(db=lambda: None, settings=object())


def _caller(**over: Any) -> gw.HostCaller:
    base = dict(host_id="hst_1", org_id="org_1", vm_id="vm_a", run_id="run_1", epoch=3,
                deployment_id="dep_1")
    base.update(over)
    return gw.HostCaller(**base)


# ── the host's forward, read ─────────────────────────────────────────────────────────────────

def test_the_handle_is_read_from_x_boss_handle_only_and_the_hosts_word_from_its_headers():
    headers = {"Authorization": "BossHost proof", "X-Boss-Handle": HANDLE, "X-Boss-Vm": "vm_a",
               "X-Boss-Run": "run_1", "X-Boss-Fence-Epoch": "3", "X-Boss-Deployment": "dep_1",
               "traceparent": "00-" + "a" * 32 + "-" + "b" * 16 + "-01"}
    assert gw.presented_handle(headers) == HANDLE
    caller = gw.HostCaller.from_headers(headers, host_id="hst_1", org_id="org_1")
    assert (caller.vm_id, caller.run_id, caller.epoch, caller.deployment_id) == \
        ("vm_a", "run_1", 3, "dep_1")
    assert caller.trace_id.startswith("00-")
    # A handle a guest left in its own credential header is NOT the handle: the host moves it.
    assert gw.presented_handle({"Authorization": "Bearer " + HANDLE}) == ""
    with pytest.raises(gw.Refused) as refused:
        gw.HostCaller.from_headers({"X-Boss-Fence-Epoch": "three"}, host_id="h", org_id="o")
    assert refused.value.reason == "bad_epoch"


# ── the handle, bound ────────────────────────────────────────────────────────────────────────

def test_a_bound_handle_resolves_to_the_rows_allowlist_and_binding():
    conn = FakeConn(_row(), job_epoch=3)
    grant = _gateway().host_lane.resolve(conn, _caller(), HANDLE)
    assert isinstance(grant, gw.GatewayGrant) and grant.db_backed
    assert grant.run_id == "run_1" and grant.project_id == "p_1" and grant.role_key == "forge"
    assert grant.servers == {"jira": {"server_id": "mcp_jira", "granted_tools": ["search", "comment"]}}
    assert (grant.vm_id, grant.deployment_id, grant.org_id, grant.epoch) == ("vm_a", "dep_1", "org_1", 3)
    assert grant.live() and conn.updates == []


@pytest.mark.parametrize("row, reason", [
    (None, "unknown_handle"),
    (_row(revoked_at=NOW), "handle_revoked"),
    (_row(expires_at=NOW - dt.timedelta(seconds=1)), "handle_expired"),
])
def test_unknown_revoked_and_expired_handles_are_refused(row, reason):
    with pytest.raises(gw.Refused) as refused:
        _gateway().host_lane.resolve(FakeConn(row), _caller(), HANDLE)
    assert refused.value.status == 401 and refused.value.reason == reason


def test_no_handle_or_a_foreign_prefix_is_refused():
    for raw in ("", "boss_mdl_" + "a" * 64, "sk-ant-real-key"):
        with pytest.raises(gw.Refused) as refused:
            _gateway().host_lane.resolve(FakeConn(_row()), _caller(), raw)
        assert refused.value.reason == "no_handle"


def test_a_handle_from_vm_a_presented_from_vm_b_is_a_replay_revoked_and_reported(monkeypatch):
    from boss_engine.services import fleet

    recorded: list[dict] = []
    monkeypatch.setattr(fleet, "host_event", lambda c, **kw: recorded.append(kw))
    conn = FakeConn(_row(), job_epoch=3)
    with pytest.raises(gw.Refused) as refused:
        _gateway().host_lane.resolve(conn, _caller(vm_id="vm_b"), HANDLE)
    assert refused.value.status == 401 and refused.value.reason == "handle_replay"
    assert conn.updates == [("revoke", {"h": gw._hash(HANDLE), "r": "handle_replay:another_vm"})]
    assert recorded and recorded[0]["kind"] == "handle_replay"
    assert recorded[0]["actor"] == "mcp_gateway" and recorded[0]["vm_id"] == "vm_b"
    assert recorded[0]["detail"]["bound_vm"] == "vm_a" and recorded[0]["detail"]["why"] == "another_vm"
    assert HANDLE not in json.dumps(recorded, default=str)
    # Revoked: refused on its own VM from now on.
    with pytest.raises(gw.Refused) as again:
        _gateway().host_lane.resolve(conn, _caller(), HANDLE)
    assert again.value.reason == "handle_revoked"


def test_another_run_or_another_org_is_a_replay_too(monkeypatch):
    from boss_engine.services import fleet

    monkeypatch.setattr(fleet, "host_event", lambda c, **kw: None)
    for caller, why in ((_caller(run_id="run_2"), "another_run"), (_caller(org_id="org_2"), "another_org")):
        conn = FakeConn(_row(), job_epoch=3)
        with pytest.raises(gw.Refused) as refused:
            _gateway().host_lane.resolve(conn, caller, HANDLE)
        assert refused.value.reason == "handle_replay"
        assert conn.updates[0][1]["r"] == f"handle_replay:{why}"


def test_a_code_tasks_unbound_handle_is_bound_to_the_first_vm_that_redeems_it(monkeypatch):
    from boss_engine.services import fleet

    monkeypatch.setattr(fleet, "host_event", lambda c, **kw: None)
    conn = FakeConn(_row(vm_id=None), job_epoch=3)
    grant = _gateway().host_lane.resolve(conn, _caller(vm_id="vm_first"), HANDLE)
    assert grant.vm_id == "vm_first" and conn.updates == [("bind", {"h": gw._hash(HANDLE), "v": "vm_first"})]
    with pytest.raises(gw.Refused) as refused:
        _gateway().host_lane.resolve(conn, _caller(vm_id="vm_second"), HANDLE)
    assert refused.value.reason == "handle_replay"


def test_a_stale_epoch_is_409_fenced_within_one_request():
    # The host's epoch is not the handle's.
    with pytest.raises(gw.Refused) as refused:
        _gateway().host_lane.resolve(FakeConn(_row(), job_epoch=3), _caller(epoch=2), HANDLE)
    assert refused.value.status == 409 and refused.value.reason == "fenced"
    # The run moved on in Postgres (its job is at epoch 4 now) — refused with no second request.
    with pytest.raises(gw.Refused) as moved:
        _gateway().host_lane.resolve(FakeConn(_row(), job_epoch=4), _caller(), HANDLE)
    assert moved.value.status == 409 and moved.value.reason == "fenced"
    # A local-lane run (no fenced lease) is not fenced.
    assert _gateway().host_lane.resolve(FakeConn(_row(), job_epoch=None), _caller(), HANDLE).run_id == "run_1"


# ── the allowlist ─────────────────────────────────────────────────────────────────────────────

def test_a_tool_outside_the_runs_allowlist_is_refused_before_any_upstream_lookup(monkeypatch):
    """The grant's servers come from the row; `handle` refuses a server not on it, and the
    policy seam refuses a tool the grant does not name (not_granted) without reaching the
    registry's upstream call."""
    gateway = _gateway()
    grant = gateway.host_lane.resolve(FakeConn(_row(), job_epoch=3), _caller(), HANDLE)
    answer = gateway.handle(grant, "github", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert answer["error"]["code"] == -32004 and "no server 'github'" in answer["error"]["message"]
    verdict = gw.tool_policy_verdict({"id": "mcp_jira", "name": "Jira"}, "delete_project",
                                     granted_tools=grant.servers["jira"]["granted_tools"])
    assert verdict.decision == "deny" and verdict.reason == "not_granted"
    # `boss_run` is always on: the run's own tools need no grant.
    tools = gateway.handle(grant, gw.RUN_SERVER, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    names = {t["name"] for t in tools["result"]["tools"]}
    assert {"ask_person", "request_approval", "report_blocked"} <= names


# ── S13: audiences and host-held shims ───────────────────────────────────────────────────────

def test_per_destination_audiences_exist_and_a_host_never_gets_boss_api():
    assert delegation.AUD_MODEL == "boss:model" and delegation.AUD_RUN_PLANE == "boss:run_plane"
    assert {delegation.AUD_MODEL, delegation.AUD_MCP, delegation.AUD_EGRESS,
            delegation.AUD_RUN_PLANE} <= delegation.RUN_AUDIENCES
    assert delegation.AUD_API not in delegation.HOST_AUDIENCES
    assert delegation.AUD_TOKEN not in delegation.HOST_AUDIENCES
    # The schema's CHECK names every audience the token service may mint.
    sql = (ROOT / "db" / "schema.sql").read_text(encoding="utf-8")
    check = re.search(r"identity_run_credentials_aud_check\s+CHECK \(aud IN \(([^)]*)\)\)", sql).group(1)
    for aud in delegation.RUN_AUDIENCES | {delegation.AUD_TOKEN}:
        assert f"'{aud}'" in check, aud


def test_a_host_held_shim_has_no_key_and_cannot_sign():
    from boss_engine.identity import runs, tokens

    shim = runs.Shim(run_id="arun_1", key=None, host_jkt="jkt-of-the-host")
    assert shim.host_held and shim.jkt == "jkt-of-the-host"
    shim.tokens[delegation.AUD_MCP] = delegation.IssuedToken(access_token="t", jti="rt_1")
    with pytest.raises(tokens.TokenError) as refused:
        shim.authorization(delegation.AUD_MCP, "POST", "https://cp.example/mcp/jira")
    assert refused.value.reason == "host_held_shim_cannot_sign"
    assert "t" not in repr(shim) or "tokens" not in repr(shim)
    local = runs.Shim(run_id="run_ct:1")
    assert not local.host_held and local.jkt == local.key.jkt


def test_a_missing_delegated_token_is_tolerated_unless_required(monkeypatch):
    lane = _gateway().host_lane
    monkeypatch.delenv(gw.HOST_TOKENS_REQUIRED_ENV, raising=False)
    assert lane.check_delegation(None, {}, run_id="run_1", method="POST",
                                 url="https://cp.example/mcp/jira") is None
    monkeypatch.setenv(gw.HOST_TOKENS_REQUIRED_ENV, "1")
    with pytest.raises(gw.Refused) as refused:
        lane.check_delegation(None, {}, run_id="run_1", method="POST", url="https://cp.example/mcp/jira")
    assert refused.value.status == 401 and refused.value.reason == "run_token_required"
    # The model gateway applies the same rule, in the same words.
    from boss_engine.services import streaming_model_gateway as smg

    with pytest.raises(smg.Refused) as model_refused:
        smg.ModelGateway.check_delegation(None, {}, run_id="run_1", method="POST",
                                          url="https://gw.example/anthropic/v1/messages")
    assert model_refused.value.reason == "run_token_required"
    monkeypatch.delenv(gw.HOST_TOKENS_REQUIRED_ENV)
    assert smg.ModelGateway.check_delegation(None, {}, run_id="run_1", method="POST", url="u") is None


def test_a_wrong_audience_or_a_wrong_key_is_refused_in_words(monkeypatch):
    """verify_for_mcp is the audience pin; its refusal reasons reach the host as
    run_token_<reason>, never with the token itself."""
    from boss_engine.identity import tokens

    calls: list = []

    def refuse(token, **kw):
        calls.append(kw)
        raise tokens.TokenError("invalid_token", 401, "bad_aud")

    monkeypatch.setattr(tokens, "verify_for_mcp", refuse)
    with pytest.raises(gw.Refused) as refused:
        _gateway().host_lane.check_delegation(None, {"X-Boss-Delegation": "model.token.x", "DPoP": "p"},
                                         run_id="run_1", method="POST", url="https://cp/mcp/jira")
    assert refused.value.reason == "run_token_bad_aud"
    assert "model.token.x" not in str(refused.value)
    assert calls[0]["dpop_proof"] == "p" and calls[0]["htm"] == "POST" and calls[0]["htu"] == "https://cp/mcp/jira"

    class Claims:
        class task:
            run_id = "run_other"

    monkeypatch.setattr(tokens, "verify_for_mcp", lambda token, **kw: Claims())
    with pytest.raises(gw.Refused) as other:
        _gateway().host_lane.check_delegation(None, {"X-Boss-Delegation": "t", "DPoP": "p"},
                                         run_id="run_1", method="POST", url="u")
    assert other.value.reason == "run_token_mismatch"


# ── the contract agrees across the three sides ───────────────────────────────────────────────

def test_the_go_route_table_carries_the_host_proof_and_the_mcp_audience():
    routes = (ROOT / "services" / "sandbox-service" / "internal" / "gateway" / "routes.go").read_text()
    mcp_rule = next(line for line in routes.splitlines() if 'ID: "mcp"' in line)
    assert "HostProof: true" in mcp_rule and "Audience: hostapi.AudMCP" in mcp_rule
    assert "Methods: post" in mcp_rule, "POST only: the control plane answers GET/DELETE with 405"
    hostapi = (ROOT / "services" / "sandbox-service" / "internal" / "hostapi" / "hostapi.go").read_text()
    assert 'HeaderDelegation = "X-Boss-Delegation"' in hostapi and 'HeaderDPoP       = "DPoP"' in hostapi
    assert f'AudMCP      = "{delegation.AUD_MCP}"' in hostapi
    assert f'AudModel    = "{delegation.AUD_MODEL}"' in hostapi
    assert f'AudRunPlane = "{delegation.AUD_RUN_PLANE}"' in hostapi
    assert 'PathRunTokens = "/v1/run-plane/runs/%s/tokens"' in hostapi
    # A guest-made DPoP header never survives the host's strip.
    assert '"DPoP"' in routes.split("var strippedHeaders")[1].split("}")[0]
    # The endpoint boss-hostd forwards to is the control plane's own /mcp route, and the guest
    # URL is the vsock relay's port 7103.
    from boss_engine.services import fleet
    from boss_engine.services.sandbox_contract import DEFAULT_ENDPOINTS

    assert fleet.endpoints("hst_1")["mcp_gateway"] == gw.HOST_ROUTE == "/mcp"
    assert gw.GUEST_MCP_BASE.startswith(DEFAULT_ENDPOINTS["mcp"])


def test_the_model_gateway_and_the_mcp_gateway_name_the_same_headers():
    from boss_engine.services import streaming_model_gateway as smg

    assert smg.DELEGATION_HEADER == gw.DELEGATION_HEADER == "x-boss-delegation"
    assert smg.DPOP_HEADER == gw.DPOP_HEADER == "dpop"
    assert smg.HANDLE_HEADER == gw.HANDLE_HEADER and smg.EPOCH_HEADER == gw.EPOCH_HEADER


# ── nothing upstream leaks to the guest ─────────────────────────────────────────────────────

def test_a_queue_sink_streams_keepalives_and_the_result_then_ends():
    sink = gw.QueueSink()
    sink.keepalive()
    sink.progress('{"jsonrpc":"2.0","method":"notifications/progress"}')
    sink.finish({"jsonrpc": "2.0", "id": 1, "result": {"ok": True}})
    chunks = []
    while True:
        item = sink.q.get_nowait()
        if item is None:
            break
        chunks.append(item)
    assert chunks[0] == b": keepalive\n\n" and chunks[1].startswith(b"event: message\ndata: ")
    assert b'"result": {"ok": true}' in chunks[2]
    sink.gone.set()
    with pytest.raises(gw.ClientGone):
        sink.keepalive()


def test_the_guest_handle_is_registered_with_the_redactor_and_the_urls_carry_no_credential(monkeypatch):
    from boss_engine.identity import redaction
    from boss_engine.services import code_task_mcp

    monkeypatch.setattr(code_task_mcp, "plan", lambda c, s, p, r, gateway: {
        "gateway_servers": {"jira": {"server_id": "mcp_jira", "granted_tools": ["search"]},
                            gw.RUN_SERVER: {}}, "servers": {}, "disallowed": []})
    inserted: list = []

    class Conn:
        def execute(self, stmt, params=None):
            inserted.append(params)
            return FakeResult()

    out = gw.guest_handle(Conn(), object(), org_id="org_1", project_id="p_1", run_id="arun_1",
                          epoch=2, vm_id=None, role_key="triage_bot", deployment_id="dep_1")
    raw = out["mcpgw"]
    assert raw.startswith(gw.TOKEN_PREFIX) and len(raw) == len(gw.TOKEN_PREFIX) + 64
    assert out["mcp_servers"] == {"jira": "http://127.0.0.1:7103/mcp/jira",
                                  "boss_run": "http://127.0.0.1:7103/mcp/boss_run"}
    assert raw not in json.dumps(out["mcp_servers"])
    row = inserted[0]
    assert row["h"] == gw._hash(raw) and row["v"] is None and row["e"] == 2 and row["k"] == "triage_bot"
    assert json.loads(row["s"]) == {"jira": {"server_id": "mcp_jira", "granted_tools": ["search"]}}
    assert raw not in json.dumps(row)
    assert raw not in redaction.redact_text(f"the handle is {raw}")


def test_an_upstream_failure_text_is_redacted_before_it_reaches_the_guest():
    from boss_engine.identity import redaction

    redaction.register_secrets(["ghp_UPSTREAM_TOKEN_123456789"])
    err = gw.tool_error("tool_failed", redaction.redact_text(
        "search failed: 401 for Bearer ghp_UPSTREAM_TOKEN_123456789"))
    assert "ghp_UPSTREAM_TOKEN_123456789" not in json.dumps(err)
    assert err["isError"] and err["structuredContent"]["error"]["type"] == "boss.tool_failed"


def test_guest_grants_time_out_like_loopback_grants():
    row = _row(expires_at=NOW + dt.timedelta(seconds=30))
    grant = _gateway().grant_from_row(row)
    assert grant.live() and not grant.live(now=time.time() + 60)
