"""Wave B tests: streaming Metatron agent turns, tool_call pause/resume, Last-Event-ID,
Idempotency-Key, BudgetReservation/Receipt authority, cancellation, timeouts, and shared SSE fixtures.
"""
from __future__ import annotations

import json
import socket
import time
from pathlib import Path
from threading import Event, Thread
from uuid import uuid4

import httpx
import pytest
import uvicorn
from sqlalchemy import text

from apps.api.app import main
from apps.api.app.config import get_settings as api_get_settings
from apps.api.tests.identity_fixtures import IdentityWorld
from apps.api.tests.test_agent_voice_stream_intent import connect_owned
from boss_engine.config import get_settings as engine_get_settings
from boss_engine.identity import api_keys, authz
from boss_engine.identity.principal import AuthContext, AuthMethod
from boss_engine.services import model_routing
from boss_engine.services.model_routing import ModelChoice

FIXTURES_DIR = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "metatron"


def _parse_sse_frames(raw_text: str) -> list[dict]:
    frames: list[dict] = []
    for block in raw_text.replace("\r\n", "\n").split("\n\n"):
        if not block.strip():
            continue
        lines = block.split("\n")
        if all(line.startswith(":") for line in lines):
            frames.append({"comment": "\n".join(lines)})
            continue
        frame: dict = {}
        data_lines: list[str] = []
        for line in lines:
            if line.startswith("id: "):
                frame["id"] = line[4:]
            elif line.startswith("event: "):
                frame["event"] = line[7:]
            elif line.startswith("data: "):
                data_lines.append(line[6:])
        if data_lines:
            frame["data"] = json.loads("\n".join(data_lines))
        frames.append(frame)
    return frames


class ScriptedChatModel:
    _boss_choice = ModelChoice("ollama", "owned-metatron-model", "approved_local_model_host")

    def __init__(self, engine, project_id: str, principal_id: str):
        self.engine = engine
        self.project_id = project_id
        self.principal_id = principal_id
        self.calls = 0
        self.seen_tools: list[list[str]] = []
        self.seen_messages: list[list[dict]] = []
        self.steps: list[list[tuple[str, object]]] = []
        self.block_step = 0
        self.entered = Event()
        self.release = Event()
        self.fail_step = 0

    def stream_chat_completion(self, messages, tools=None, tool_choice="auto"):
        self.calls += 1
        step_idx = self.calls
        self.seen_messages.append(list(messages))
        tool_names = [
            str((t.get("function") or {}).get("name") or "")
            for t in (tools or [])
            if isinstance(t, dict)
        ]
        self.seen_tools.append(tool_names)

        # Verify BudgetReservation, Job, ModelGatewayLease, Invocation & Receipt committed BEFORE contact!
        with self.engine.connect() as c:
            row = (
                c.execute(
                    text(
                        "SELECT j.status AS job, j.on_behalf_of, l.status AS lease, "
                        "i.status AS invocation, r.status AS receipt, b.status AS hold "
                        "FROM model_gateway_invocations i "
                        "JOIN jobs j ON j.id = i.job_id "
                        "JOIN model_gateway_leases l ON l.id = i.model_gateway_lease_id "
                        "JOIN receipts r ON r.id = i.receipt_id "
                        "JOIN budget_reservations b ON b.id = l.budget_reservation_id "
                        "WHERE i.project_id = :p AND i.status = 'in_progress' "
                        "ORDER BY i.started_at DESC LIMIT 1"
                    ),
                    {"p": self.project_id},
                )
                .mappings()
                .one()
            )
            assert row["job"] == "running"
            assert row["lease"] == "active"
            assert row["invocation"] == "in_progress"
            assert row["receipt"] == "in_progress"
            assert row["hold"] == "active"
            assert row["on_behalf_of"] == self.principal_id

        self.entered.set()
        if self.block_step == step_idx:
            assert self.release.wait(15), "test barrier did not release"
        if self.fail_step == step_idx:
            raise RuntimeError("owned provider failed mid-stream")

        if step_idx <= len(self.steps):
            for kind, val in self.steps[step_idx - 1]:
                yield (kind, val)
        else:
            yield ("delta", "Default final reply.")
            yield (
                "usage",
                {
                    "model": "owned-metatron-model",
                    "cost_usd_micros": 1000,
                    "prompt_tokens": 12,
                    "completion_tokens": 8,
                },
            )


def _mint_pat(engine, world: IdentityWorld, person) -> str:
    with engine.begin() as c:
        created = api_keys.create(
            c,
            creator=AuthContext(person, AuthMethod.SESSION),
            owner=person,
            kind="pat",
            name=f"mt-test-{uuid4().hex[:6]}",
            scopes=sorted(api_keys.held_permissions(c, person)),
            expires_in_days=1,
        )
    return created.key


@pytest.fixture
def turn_env(engine, monkeypatch):
    monkeypatch.setenv("BOSS_AUTHZ_MODE", "enforce")
    monkeypatch.setattr(main.settings, "authz_mode", "enforce")
    monkeypatch.setattr(api_get_settings(), "authz_mode", "enforce")
    monkeypatch.setattr(engine_get_settings(), "authz_mode", "enforce")
    monkeypatch.setenv("BOSS_METATRON_SSE_HEARTBEAT_SECONDS", "0.2")
    monkeypatch.setattr(main.settings, "require_api_auth", True)
    world = IdentityWorld.create(engine)
    owner = world.person("turn-owner")
    other = world.person("turn-other")
    binding = world.bind(owner, "owner", project=world.project_a)
    world.bind(other, "operator", project=world.project_a)

    connect_owned(engine, monkeypatch, world.project_a, paid=True)
    model = ScriptedChatModel(engine, world.project_a, owner.id)
    monkeypatch.setattr(model_routing, "project_chat_adapter", lambda *_a, **_k: model)

    owner_key = _mint_pat(engine, world, owner)
    other_key = _mint_pat(engine, world, other)

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(main.app, lifespan="off", log_level="critical"))
    thread = Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.01)
    assert server.started
    base_url = f"http://127.0.0.1:{sock.getsockname()[1]}"
    try:
        yield {
            "world": world,
            "owner": owner,
            "other": other,
            "binding": binding,
            "owner_key": owner_key,
            "other_key": other_key,
            "model": model,
            "base_url": base_url,
        }
    finally:
        model.release.set()
        server.should_exit = True
        thread.join(5)
        sock.close()


def test_text_turn_server_tool_and_policy_tool_filtering(turn_env, engine):
    world = turn_env["world"]
    model: ScriptedChatModel = turn_env["model"]
    headers = {
        "Authorization": f"Bearer {turn_env['owner_key']}",
        "Accept": "text/event-stream",
        "Idempotency-Key": str(uuid4()),
    }

    # Add a project deny rule forbidding `forbidden_tool`
    with engine.begin() as c:
        from boss_engine.services import tool_policy_engine

        tool_policy_engine.create_rule(
            c,
            world.project_a,
            {
                "scope": "project",
                "priority": 1,
                "effect": "deny",
                "selector": {"server": "metatron", "tool": "forbidden_tool"},
            },
            actor=turn_env["owner"].id,
        )

    # Step 1 calls server tool `list_projects`; Step 2 returns final streamed text
    model.steps = [
        [
            ("tool_calls", [{"id": "call_srv_1", "name": "list_projects", "arguments": {}}]),
            (
                "usage",
                {
                    "model": "owned-metatron-model",
                    "cost_usd_micros": 800,
                    "prompt_tokens": 15,
                    "completion_tokens": 6,
                },
            ),
        ],
        [
            ("delta", "Hello "),
            ("delta", "from BOSS!"),
            (
                "usage",
                {
                    "model": "owned-metatron-model",
                    "cost_usd_micros": 900,
                    "prompt_tokens": 25,
                    "completion_tokens": 10,
                },
            ),
        ],
    ]

    resp = httpx.post(
        f"{turn_env['base_url']}/v1/metatron/turns",
        headers=headers,
        json={
            "project_id": world.project_a,
            "message": "list projects and say hi",
            "mode": "ask",
            "client_tools": [
                {"name": "ssh_run", "description": "run ssh", "input_schema": {"type": "object"}, "risk_class": "remote-read"},
                {"name": "forbidden_tool", "description": "blocked", "input_schema": {"type": "object"}, "risk_class": "remote-write"},
            ],
        },
        timeout=15.0,
    )
    assert resp.status_code == 200, resp.text
    frames = _parse_sse_frames(resp.text)
    events = [f for f in frames if "event" in f]
    etypes = [e["event"] for e in events]
    assert etypes[0] == "turn.started"
    assert "server_tool.started" in etypes
    assert "server_tool.finished" in etypes
    assert etypes.count("text.delta") == 2
    assert etypes[-1] == "turn.completed"

    started = events[0]["data"]
    assert "ssh_run" in started["tools"]
    assert "forbidden_tool" not in started["tools"]
    assert any(d["name"] == "forbidden_tool" for d in started["dropped"])
    # Forbidden tool was NEVER shown to the model
    assert all("forbidden_tool" not in tlist for tlist in model.seen_tools)

    # Completed turn replays exact SSE bytes for same Idempotency-Key without calling model again
    replay = httpx.post(
        f"{turn_env['base_url']}/v1/metatron/turns",
        headers=headers,
        json={
            "project_id": world.project_a,
            "message": "list projects and say hi",
            "mode": "ask",
            "client_tools": [
                {"name": "ssh_run", "description": "run ssh", "input_schema": {"type": "object"}, "risk_class": "remote-read"},
                {"name": "forbidden_tool", "description": "blocked", "input_schema": {"type": "object"}, "risk_class": "remote-write"},
            ],
        },
        timeout=15.0,
    )
    assert replay.status_code == 200
    assert replay.headers.get("idempotency-replayed") == "true"
    assert replay.text == resp.text
    assert model.calls == 2  # still 2 (no second model run!)

    # Reusing same Idempotency-Key with changed body -> 422
    changed = httpx.post(
        f"{turn_env['base_url']}/v1/metatron/turns",
        headers=headers,
        json={"project_id": world.project_a, "message": "different prompt"},
        timeout=15.0,
    )
    assert changed.status_code == 422

    # Revoking caller permissions refuses replay -> 403
    with engine.begin() as c:
        c.execute(text("DELETE FROM identity_role_bindings WHERE id = :b"), {"b": turn_env["binding"]})
        authz.bump_epoch(world.org, db=c, reason="test.revoke.replay")
    revoked = httpx.post(
        f"{turn_env['base_url']}/v1/metatron/turns",
        headers=headers,
        json={
            "project_id": world.project_a,
            "message": "list projects and say hi",
            "mode": "ask",
            "client_tools": [
                {"name": "ssh_run", "description": "run ssh", "input_schema": {"type": "object"}, "risk_class": "remote-read"},
                {"name": "forbidden_tool", "description": "blocked", "input_schema": {"type": "object"}, "risk_class": "remote-write"},
            ],
        },
        timeout=15.0,
    )
    assert revoked.status_code == 403


def test_client_tool_pause_heartbeat_tool_results_413_and_last_event_id_resume(
    turn_env, engine
):
    world = turn_env["world"]
    model: ScriptedChatModel = turn_env["model"]
    owner_hdrs = {"Authorization": f"Bearer {turn_env['owner_key']}"}
    other_hdrs = {"Authorization": f"Bearer {turn_env['other_key']}"}

    model.steps = [
        [
            (
                "tool_calls",
                [{"id": "call_cli_1", "name": "ssh_run", "arguments": {"host": "web-1", "argv": ["sar", "-u", "1", "1"]}}],
            ),
            (
                "usage",
                {
                    "model": "owned-metatron-model",
                    "cost_usd_micros": 700,
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                },
            ),
        ],
        [
            ("delta", "CPU idle is 95%."),
            (
                "usage",
                {
                    "model": "owned-metatron-model",
                    "cost_usd_micros": 900,
                    "prompt_tokens": 20,
                    "completion_tokens": 8,
                },
            ),
        ],
    ]

    turn_key = str(uuid4())
    captured: dict = {}

    def _run_stream():
        with httpx.stream(
            "POST",
            f"{turn_env['base_url']}/v1/metatron/turns",
            headers={**owner_hdrs, "Accept": "text/event-stream", "Idempotency-Key": turn_key},
            json={
                "project_id": world.project_a,
                "message": "check web-1 cpu",
                "mode": "ask",
                "client_tools": [
                    {"name": "ssh_run", "description": "ssh", "input_schema": {"type": "object"}, "risk_class": "remote-read"}
                ],
            },
            timeout=15.0,
        ) as stream_resp:
            captured["status"] = stream_resp.status_code
            chunks: list[str] = []
            for line in stream_resp.iter_lines():
                chunks.append(line)
                if line.startswith("event: tool_call"):
                    captured["saw_tool_call"] = True
                if line.startswith("data: ") and '"turn_id":' in line and "turn_id" not in captured:
                    obj = json.loads(line[6:])
                    if "turn_id" in obj:
                        captured["turn_id"] = obj["turn_id"]
                if line == ": ping":
                    captured["saw_ping"] = True
            captured["raw"] = "\n".join(chunks)

    stream_thread = Thread(target=_run_stream, daemon=True)
    stream_thread.start()

    # Wait until tool_call is emitted and turn enters awaiting_tool_result
    for _ in range(100):
        if captured.get("saw_tool_call") and captured.get("turn_id"):
            break
        time.sleep(0.05)
    assert captured.get("saw_tool_call"), f"never saw tool_call: {captured}"
    turn_id = captured["turn_id"]

    # Wait briefly so at least one ': ping' heartbeat is emitted while awaiting_tool_result
    for _ in range(30):
        if captured.get("saw_ping"):
            break
        time.sleep(0.05)
    assert captured.get("saw_ping")

    # Concurrent retry with same Idempotency-Key returns 409
    concurrent = httpx.post(
        f"{turn_env['base_url']}/v1/metatron/turns",
        headers={**owner_hdrs, "Idempotency-Key": turn_key},
        json={
            "project_id": world.project_a,
            "message": "check web-1 cpu",
            "mode": "ask",
            "client_tools": [
                {"name": "ssh_run", "description": "ssh", "input_schema": {"type": "object"}, "risk_class": "remote-read"}
            ],
        },
        timeout=5.0,
    )
    assert concurrent.status_code == 409

    # Another principal cannot post tool_results, read events, or cancel
    assert (
        httpx.post(
            f"{turn_env['base_url']}/v1/metatron/turns/{turn_id}/tool_results",
            headers=other_hdrs,
            json={"call_id": "call_cli_1", "status": "ok", "output": {"text": "x"}},
        ).status_code
        == 403
    )
    assert (
        httpx.get(
            f"{turn_env['base_url']}/v1/metatron/turns/{turn_id}/events",
            headers=other_hdrs,
        ).status_code
        == 403
    )

    # Oversized tool_result (> 65536 bytes) -> 413 (both output-only and output + decision)
    too_big = httpx.post(
        f"{turn_env['base_url']}/v1/metatron/turns/{turn_id}/tool_results",
        headers={**owner_hdrs, "Idempotency-Key": f"{turn_id}:call_cli_1:big"},
        json={"call_id": "call_cli_1", "status": "ok", "output": {"text": "A" * 70000}},
        timeout=5.0,
    )
    assert too_big.status_code == 413
    assert too_big.json()["error"]["code"] == "tool_result_too_large"

    too_big_decision = httpx.post(
        f"{turn_env['base_url']}/v1/metatron/turns/{turn_id}/tool_results",
        headers={**owner_hdrs, "Idempotency-Key": f"{turn_id}:call_cli_1:bigdec"},
        json={
            "call_id": "call_cli_1",
            "status": "ok",
            "output": {"text": "B" * 65525},
            "decision": {"by": "reviewer", "notes": "D" * 10000},
        },
        timeout=5.0,
    )
    assert too_big_decision.status_code == 413
    assert too_big_decision.json()["error"]["code"] == "tool_result_too_large"

    # Valid tool_result -> 202 and stream completes cleanly
    ok_res = httpx.post(
        f"{turn_env['base_url']}/v1/metatron/turns/{turn_id}/tool_results",
        headers={**owner_hdrs, "Idempotency-Key": f"{turn_id}:call_cli_1"},
        json={
            "call_id": "call_cli_1",
            "status": "ok",
            "output": {"content": [{"type": "text", "text": "95% idle"}], "exit_code": 0},
            "decision": {"by": "person", "mode": "ask"},
        },
        timeout=5.0,
    )
    assert ok_res.status_code == 202

    stream_thread.join(timeout=10)
    assert not stream_thread.is_alive()
    frames = _parse_sse_frames(captured["raw"])
    events = [f for f in frames if "event" in f]
    assert [e["event"] for e in events][-2:] == ["usage", "turn.completed"]
    assert any(e["event"] == "text.delta" and "95%" in e["data"]["text"] for e in events)

    # Replay via GET /events with Last-Event-ID: <turn_id>:2 skips seq <= 2 and never calls model again
    calls_before = model.calls
    resumed = httpx.get(
        f"{turn_env['base_url']}/v1/metatron/turns/{turn_id}/events",
        headers={**owner_hdrs, "Last-Event-ID": f"{turn_id}:2"},
        timeout=10.0,
    )
    assert resumed.status_code == 200
    resumed_events = [f for f in _parse_sse_frames(resumed.text) if "event" in f]
    assert all(int(e["id"].split(":")[1]) > 2 for e in resumed_events)
    assert resumed_events[-1]["event"] == "turn.completed"
    assert model.calls == calls_before


def test_cancel_and_tool_timeout_and_reconciliation_and_budget_exhausted(
    turn_env, engine
):
    world = turn_env["world"]
    model: ScriptedChatModel = turn_env["model"]
    owner_hdrs = {"Authorization": f"Bearer {turn_env['owner_key']}"}

    # 1. Tool result timeout -> turn.failed (tool_result_timeout)
    model.steps = [
        [
            ("tool_calls", [{"id": "call_to_1", "name": "ssh_run", "arguments": {}}]),
            ("usage", {"model": "owned-metatron-model", "cost_usd_micros": 100, "prompt_tokens": 5, "completion_tokens": 5}),
        ]
    ]
    to_resp = httpx.post(
        f"{turn_env['base_url']}/v1/metatron/turns",
        headers=owner_hdrs,
        json={
            "project_id": world.project_a,
            "message": "run tool that times out",
            "tool_deadline_s": 1,
            "client_tools": [{"name": "ssh_run", "risk_class": "remote-read"}],
        },
        timeout=10.0,
    )
    assert to_resp.status_code == 200
    to_events = [f for f in _parse_sse_frames(to_resp.text) if "event" in f]
    assert to_events[-1]["event"] == "turn.failed"
    assert to_events[-1]["data"]["code"] == "tool_result_timeout"

    # 2. Cancel while awaiting tool result -> turn.completed {stop_reason: "cancelled"}
    model.calls = 0
    model.steps = [
        [
            ("tool_calls", [{"id": "call_can_1", "name": "ssh_run", "arguments": {}}]),
            ("usage", {"model": "owned-metatron-model", "cost_usd_micros": 100, "prompt_tokens": 5, "completion_tokens": 5}),
        ]
    ]
    can_cap: dict = {}

    def _run_cancel_stream():
        with httpx.stream(
            "POST",
            f"{turn_env['base_url']}/v1/metatron/turns",
            headers=owner_hdrs,
            json={
                "project_id": world.project_a,
                "message": "cancel me",
                "tool_deadline_s": 30,
                "client_tools": [{"name": "ssh_run", "risk_class": "remote-read"}],
            },
            timeout=10.0,
        ) as r:
            lines = []
            for line in r.iter_lines():
                lines.append(line)
                if line.startswith("data: ") and '"turn_id":' in line and "turn_id" not in can_cap:
                    can_cap["turn_id"] = json.loads(line[6:])["turn_id"]
                if line.startswith("event: tool_call"):
                    can_cap["ready"] = True
            can_cap["raw"] = "\n".join(lines)

    ct = Thread(target=_run_cancel_stream, daemon=True)
    ct.start()
    for _ in range(100):
        if can_cap.get("ready") and can_cap.get("turn_id"):
            break
        time.sleep(0.05)
    assert can_cap.get("ready")
    cancel_resp = httpx.post(
        f"{turn_env['base_url']}/v1/metatron/turns/{can_cap['turn_id']}/cancel",
        headers=owner_hdrs,
        timeout=5.0,
    )
    assert cancel_resp.status_code == 202
    ct.join(timeout=10)
    can_events = [f for f in _parse_sse_frames(can_cap["raw"]) if "event" in f]
    assert can_events[-1]["event"] == "turn.completed"
    assert can_events[-1]["data"]["stop_reason"] == "cancelled"

    # 3. Mid-stream provider failure after contact retains receipt & raises conversation_reconciliation decision
    model.calls = 0
    model.steps = []
    model.fail_step = 1
    fail_resp = httpx.post(
        f"{turn_env['base_url']}/v1/metatron/turns",
        headers=owner_hdrs,
        json={"project_id": world.project_a, "message": "uncertain failure"},
        timeout=10.0,
    )
    assert fail_resp.status_code == 200
    fail_events = [f for f in _parse_sse_frames(fail_resp.text) if "event" in f]
    assert fail_events[-1]["event"] == "turn.failed"
    assert fail_events[-1]["data"]["code"] == "unresolved_receipt"

    with engine.connect() as c:
        rec_dec = c.execute(
            text(
                "SELECT count(*) FROM decisions WHERE project_id = :p "
                "AND recommendation->>'action' = 'conversation_reconciliation'"
            ),
            {"p": world.project_a},
        ).scalar()
        assert rec_dec >= 1

    # 4. Budget exhausted -> turn.failed (code="budget_exhausted", retryable=False, zero model calls)
    model.calls = 0
    model.fail_step = 0
    model.steps = []
    with engine.begin() as c:
        c.execute(
            text("DELETE FROM budget_policies WHERE scope_type = 'project' AND scope_id = :p"),
            {"p": world.project_a},
        )
        c.execute(
            text(
                "INSERT INTO budget_policies (id, scope_type, scope_id, daily_cap_usd_micros, "
                "monthly_cap_usd_micros, burst_cap_usd_micros_per_min) "
                "VALUES (:i, 'project', :p, 1, 1, 1000000)"
            ),
            {"i": f"bp_{uuid4().hex[:10]}", "p": world.project_a},
        )
    bud_resp = httpx.post(
        f"{turn_env['base_url']}/v1/metatron/turns",
        headers=owner_hdrs,
        json={"project_id": world.project_a, "message": "over daily cap"},
        timeout=10.0,
    )
    assert bud_resp.status_code == 200
    bud_events = [f for f in _parse_sse_frames(bud_resp.text) if "event" in f]
    assert bud_events[0]["event"] == "turn.started"
    assert bud_events[-1]["event"] == "turn.failed"
    assert bud_events[-1]["data"]["code"] == "budget_exhausted"
    assert bud_events[-1]["data"]["retryable"] is False
    assert model.calls == 0


def test_budget_refusal_kinds_shadow_org_fallback_and_concurrent_restart_resume(
    engine, monkeypatch
):
    from boss_engine.services import metatron_turns
    from boss_engine.services.typed_decision_spend import TypedDecisionReconciliationRequired, _Authority

    world = IdentityWorld.create(engine)
    owner = world.person("shadow-owner")
    world.bind(owner, "owner", project=world.project_a)
    connect_owned(engine, monkeypatch, world.project_a, paid=True)

    # 1. Verify all budget/reconciliation refusal_kinds map accurately in MetatronStepSpend.run_chat_step
    spend = metatron_turns._make_step_spend(
        engine,
        engine_get_settings(),
        project_id=world.project_a,
        turn_id="mturn_refusal_check",
        step_index=1,
        actor=owner.id,
        principal_id=owner.id,
        org_id=world.org,
        ctx=None,
    )

    class _DummyAdapter:
        _boss_choice = ModelChoice("ollama", "owned-metatron-model", "approved_local_model_host")

    for kind in ("allocation_exceeded", "member_monthly_cap", "tier2_frozen", "cap_exceeded", "headroom"):
        monkeypatch.setattr(
            spend,
            "_prepare",
            lambda *_a, _k=kind, **_kw: _Authority(
                outcome="refused", message=f"refused:{_k}", refusal_kind=_k
            ),
        )
        with pytest.raises(metatron_turns.MetatronBudgetError) as exc_info:
            spend.run_chat_step(engine_get_settings(), _DummyAdapter(), messages=[{"role": "user", "content": "hi"}], tools=None)
        assert exc_info.value.code == "budget_exhausted"

    for rec_kind in ("reconciliation_required", "legacy_reconciliation_required"):
        monkeypatch.setattr(
            spend,
            "_prepare",
            lambda *_a, _k=rec_kind, **_kw: _Authority(
                outcome="refused", message=f"refused:{_k}", refusal_kind=_k
            ),
        )
        with pytest.raises(TypedDecisionReconciliationRequired):
            spend.run_chat_step(engine_get_settings(), _DummyAdapter(), messages=[{"role": "user", "content": "hi"}], tools=None)

    # 2. Shadow mode (`ctx=None`, `org_id=None`) resolves org_id via org_of() and enforces org-wide deny rules
    with engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO tool_policies_v2 "
                "(id, org_id, project_id, scope, priority, effect, selector, conditions, status, created_by) "
                "VALUES (:id, :o, NULL, 'org', 1, 'deny', CAST(:sel AS JSONB), '[]'::jsonb, 'active', :cb)"
            ),
            {
                "id": f"pol_org_{uuid4().hex[:8]}",
                "o": world.org,
                "sel": json.dumps({"server": "metatron", "tool": "org_blocked_tool"}),
                "cb": owner.id,
            },
        )
    turn_state = metatron_turns.start_turn(
        engine,
        engine_get_settings(),
        project_id=world.project_a,
        actor=owner.id,
        principal_id=owner.id,
        org_id=None,
        payload={
            "project_id": world.project_a,
            "message": "shadow mode org policy check",
            "client_tools": [{"name": "org_blocked_tool", "risk_class": "remote-read"}],
        },
        ctx=None,
    )
    assert turn_state["org_id"] == world.org
    assert "org_blocked_tool" not in turn_state["client_tool_map"]

    # 3. Concurrent _resume_after_restart launches _run_turn_loop at most once thanks to FOR UPDATE
    launched: list[str] = []
    monkeypatch.setattr(
        metatron_turns,
        "launch_turn_runner",
        lambda _eng, _set, st: launched.append(st["turn_id"]),
    )
    turn_id = turn_state["turn_id"]
    with engine.begin() as c:
        c.execute(
            text(
                "UPDATE metatron_turns SET status = 'awaiting_tool_result', "
                "pending_tool_calls = CAST(:ptc AS JSONB) WHERE id = :t"
            ),
            {"t": turn_id, "ptc": json.dumps([{"call_id": "call_race_1", "name": "ssh_run"}])},
        )
        c.execute(
            text(
                "INSERT INTO metatron_turn_tool_results (turn_id, call_id, status, output, decision) "
                "VALUES (:t, 'call_race_1', 'ok', '{}'::jsonb, '{}'::jsonb)"
            ),
            {"t": turn_id},
        )
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(
            pool.map(
                lambda _: metatron_turns._resume_after_restart(
                    engine, engine_get_settings(), turn_id, None
                ),
                range(4),
            )
        )
    assert launched == [turn_id]


def test_shared_sse_transcript_fixtures_parse_cleanly():
    expected = {
        "turn_text_only.sse": ["turn.started", "text.delta", "usage", "turn.completed"],
        "turn_server_tool.sse": [
            "turn.started",
            "server_tool.started",
            "server_tool.finished",
            "usage",
            "text.delta",
            "usage",
            "turn.completed",
        ],
        "turn_client_tool_resume.sse": [
            "turn.started",
            "usage",
            "tool_call",
            "text.delta",
            "usage",
            "turn.completed",
        ],
        "turn_policy_denied.sse": [
            "turn.started",
            "usage",
            "policy.denied",
            "text.delta",
            "usage",
            "turn.completed",
        ],
        "turn_cancelled.sse": ["turn.started", "text.delta", "turn.completed"],
        "turn_failed_budget.sse": ["turn.started", "turn.failed"],
        "turn_tool_timeout.sse": ["turn.started", "usage", "tool_call", "turn.failed"],
    }
    for fname, want_events in expected.items():
        path = FIXTURES_DIR / fname
        assert path.is_file(), f"missing shared SSE fixture {path}"
        frames = _parse_sse_frames(path.read_text(encoding="utf-8"))
        events = [f for f in frames if "event" in f]
        assert [e["event"] for e in events] == want_events, fname
        for idx, ev in enumerate(events, start=1):
            assert ev["id"].endswith(f":{idx}"), (fname, ev)
            assert isinstance(ev["data"], dict), (fname, ev)
