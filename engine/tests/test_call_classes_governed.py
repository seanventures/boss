"""Every model call is CLASSED, and a project governs each class (BOSSAPP-999 + 1010).

Driven on the REAL seams on real Postgres (BOSS_TEST_DATABASE_URL); only the provider is
scripted, as in test_every_model_call_lands_in_the_ledger_once.py:

  * the mapping is total — every known stage maps, the unknown land in 'other', never NULL;
  * a denied class refuses a chat call in words, writes no llm_calls row, emits an audit event;
  * a calls-per-day cap trips on the N+1th call and resets daily; tokens-per-day likewise;
  * a denied build_stage refuses a governed turn (the stage fails honestly, in the policy's
    words) and a denied proof refuses the gateway lease;
  * defaults are inert: no setting, no behaviour change;
  * BOSSAPP-1010: a call that fails twice on a transient error then succeeds lands ONE row
    with retries=2 — never three rows, never a separate full-price twin.
"""
from __future__ import annotations

import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from boss_engine.config import Settings
from boss_engine.services import model_routing, project_settings
from boss_engine.services.call_classes import (
    CALL_CLASSES,
    CallClassRefused,
    STAGE_CLASSES,
    authorize,
    class_of,
    coerce_policy,
)
from boss_engine.services.llm_telemetry import (
    STAGE_ROLES,
    clear_llm_context,
    record_llm_call,
    set_llm_context,
)
from boss_engine.services.model_router.router import RouteRequest
from boss_engine.services.model_routing import ModelChoice

PG_URL = os.environ.get("BOSS_TEST_DATABASE_URL", "")


@pytest.fixture(scope="module")
def pg():
    if not PG_URL.startswith("postgres"):
        pytest.skip("needs BOSS_TEST_DATABASE_URL")
    from boss_engine.db import ensure_schema

    eng = create_engine(PG_URL, future=True)
    ensure_schema(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def project(pg) -> str:
    pid = f"p_cls_{uuid4().hex[:10]}"
    with pg.begin() as c:
        c.execute(text("INSERT INTO orgs (id, name) VALUES (:p, :p) ON CONFLICT DO NOTHING"),
                  {"p": pid})
        c.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES (:p, :p, :p) "
                       "ON CONFLICT DO NOTHING"), {"p": pid})
        c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) "
                       "VALUES (:p, :p, :p, :p)"), {"p": pid})
    yield pid
    clear_llm_context()
    with pg.begin() as c:
        c.execute(text("DELETE FROM llm_calls WHERE project_id = :p"), {"p": pid})
        c.execute(text("DELETE FROM project_settings WHERE project_id = :p"), {"p": pid})
        c.execute(text("DELETE FROM token_budget_policies WHERE project_id = :p"), {"p": pid})


def _set_policy(pg, pid: str, policy: dict) -> None:
    with pg.begin() as c:
        project_settings.set_one(c, pid, "call_classes", policy, actor="test")


def _rows(pg, pid: str) -> list[dict]:
    with pg.connect() as c:
        return [dict(r) for r in c.execute(text(
            "SELECT stage, call_class, ok, retries, total_tokens, error "
            "FROM llm_calls WHERE project_id = :p ORDER BY created_at"),
            {"p": pid}).mappings()]


def _refusal_events(pg, pid: str) -> list[dict]:
    with pg.connect() as c:
        return [dict(r) for r in c.execute(text(
            "SELECT entity_id, data FROM events WHERE project_id = :p "
            "AND event_type = 'model_call.class_refused' ORDER BY created_at"),
            {"p": pid}).mappings()]


def _age_todays_calls(pg, pid: str) -> None:
    """Yesterday's calls are yesterday's: the append-only trigger is told this is seeding."""
    with pg.begin() as c:
        c.execute(text("SET LOCAL boss.ledger_purge = 'on'"))
        c.execute(text("UPDATE llm_calls SET created_at = created_at - interval '25 hours' "
                       "WHERE project_id = :p"), {"p": pid})


class ScriptedChatModel:
    """The scripted conformance engine's chat shape; `fail_times` makes the first N calls
    raise a transient-looking provider error (529 overloaded)."""

    def __init__(self, fail_times: int = 0):
        self.fail_times = fail_times
        self.calls = 0

    def _usage(self) -> dict:
        return {"model": "scripted-v1", "prompt_tokens": 8, "completion_tokens": 2,
                "total_tokens": 10, "billing": "local"}

    def create_text_response(self, prompt, system_prompt=None, model=None):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RuntimeError("529 overloaded_error: the provider is at capacity")
        return "scripted-session", "OK", self._usage()

    def create_chat_completion(self, messages, **kwargs):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RuntimeError("529 overloaded_error: the provider is at capacity")
        return {"content": "ok", "tool_calls": None}, self._usage()


@pytest.fixture()
def fixed_mode(monkeypatch):
    """Fixed routing with the scripted chat model behind the one seam; nothing else faked."""
    scripted = ScriptedChatModel()
    monkeypatch.setattr(model_routing, "route_call", lambda *a, **k: None)
    monkeypatch.setattr(model_routing, "choose",
                        lambda *a, **k: ModelChoice("scripted", "the conformance engine"))
    monkeypatch.setattr(model_routing, "adapter_for", lambda *a, **k: scripted)
    return scripted


def _chat_adapter(pg, pid: str):
    with pg.connect() as c:
        with model_routing.routing_purpose(RouteRequest(kind="chat", step="chat",
                                                        project_id=pid)):
            return model_routing.project_chat_adapter(c, Settings(), pid)


# ── The mapping is total ──────────────────────────────────────────────────────────────────────

def test_every_known_stage_maps_and_the_unknown_land_in_other():
    from boss_engine.services.token_efficiency import KNOWN_STAGES

    known = set(STAGE_ROLES) | set(KNOWN_STAGES) | {
        "chat", "panel_member", "panel_facilitator", "agent_decision", "connection_proof",
        "model_call", "typed_decision", "converse", "rule_scan", "model_gateway"}
    for stage in known:
        assert class_of(stage) in CALL_CLASSES, stage
        assert class_of(stage) != "other", f"{stage} is a known stage; map it"
    # Every build-pipeline stage is class build_stage — the role map and the class map agree.
    for stage in STAGE_ROLES:
        assert class_of(stage) == "build_stage", stage
    # Total on ANY input: unknown, empty, None, not even a string. Never a crash, never NULL.
    for weird in ("no_such_stage_x", "", None, 123, object()):
        assert class_of(weird) == "other", repr(weird)
    assert set(STAGE_CLASSES.values()) <= set(CALL_CLASSES)


def test_the_knob_refuses_what_it_cannot_govern():
    assert coerce_policy({"chat": {"allow": False}}) == {
        "chat": {"allow": False, "calls_per_day": None, "tokens_per_day": None}}
    with pytest.raises(ValueError, match="not a call class"):
        coerce_policy({"chatt": {"allow": False}})
    with pytest.raises(ValueError, match="whole number"):
        coerce_policy({"chat": {"calls_per_day": "many"}})
    with pytest.raises(ValueError, match="0 or more"):
        coerce_policy({"chat": {"tokens_per_day": -1}})
    with pytest.raises(ValueError, match="does not govern"):
        coerce_policy({"chat": {"allow": True, "max_cost": 5}})


# ── The class lands on every row ──────────────────────────────────────────────────────────────

def test_every_row_carries_its_class_and_unknown_stages_are_other(pg, project, fixed_mode):
    set_llm_context(db_engine=pg)
    try:
        _chat_adapter(pg, project).create_text_response("one chat call")
        record_llm_call(engine_name="scripted", usage={"model": "scripted-v1"},
                        stage="implement", project_id=project)
        record_llm_call(engine_name="scripted", usage={"model": "scripted-v1"},
                        stage="no_such_stage_x", project_id=project)
    finally:
        clear_llm_context()
    rows = _rows(pg, project)
    assert [(r["stage"], r["call_class"]) for r in rows] == [
        ("chat", "chat"), ("implement", "build_stage"), ("no_such_stage_x", "other")]
    assert all(r["call_class"] is not None for r in rows)


# ── Deny / caps on the chat seam ──────────────────────────────────────────────────────────────

def test_a_denied_class_refuses_a_chat_call_in_words_with_an_audit_event(
        pg, project, fixed_mode):
    _set_policy(pg, project, {"chat": {"allow": False}})
    adapter = _chat_adapter(pg, project)
    set_llm_context(db_engine=pg)
    try:
        with pytest.raises(CallClassRefused, match="denies 'chat' model calls"):
            adapter.create_text_response("this must not reach a model")
    finally:
        clear_llm_context()
    assert fixed_mode.calls == 0, "a denied call must never reach the provider"
    assert _rows(pg, project) == [], "a refused call spends nothing and records no call row"
    events = _refusal_events(pg, project)
    assert len(events) == 1 and events[0]["entity_id"] == "chat"


def test_a_calls_per_day_cap_trips_on_the_n_plus_first_and_resets_daily(
        pg, project, fixed_mode):
    _set_policy(pg, project, {"chat": {"calls_per_day": 2}})
    set_llm_context(db_engine=pg)
    try:
        adapter = _chat_adapter(pg, project)
        adapter.create_text_response("one")
        adapter.create_text_response("two")
        with pytest.raises(CallClassRefused, match="daily volume cap"):
            adapter.create_text_response("three is over the cap")
        assert fixed_mode.calls == 2
        _age_todays_calls(pg, project)   # a new day: the cap resets
        adapter.create_text_response("a fresh day's first call")
    finally:
        clear_llm_context()
    assert fixed_mode.calls == 3
    assert len(_refusal_events(pg, project)) == 1


def test_a_tokens_per_day_cap_trips_once_the_tokens_are_spent(pg, project, fixed_mode):
    # Each scripted call reports 10 total tokens; the cap of 15 lets the second call START
    # (10 < 15) and refuses the third (20 >= 15): a volume cap is judged on what is recorded.
    _set_policy(pg, project, {"chat": {"tokens_per_day": 15}})
    set_llm_context(db_engine=pg)
    try:
        adapter = _chat_adapter(pg, project)
        adapter.create_text_response("one")
        adapter.create_text_response("two")
        with pytest.raises(CallClassRefused, match="daily token cap"):
            adapter.create_text_response("three")
    finally:
        clear_llm_context()
    assert fixed_mode.calls == 2


def test_defaults_are_inert_nothing_set_nothing_changes(pg, project, fixed_mode):
    set_llm_context(db_engine=pg)
    try:
        adapter = _chat_adapter(pg, project)
        adapter.create_text_response("no policy, no gate, exactly as before")
    finally:
        clear_llm_context()
    rows = _rows(pg, project)
    assert len(rows) == 1 and rows[0]["ok"] is True
    assert _refusal_events(pg, project) == []


# ── The governed() seam: a build stage fails honestly ─────────────────────────────────────────

def test_a_denied_build_stage_refuses_the_governed_turn_in_the_policys_words(pg, project):
    _set_policy(pg, project, {"build_stage": {"allow": False}})
    inner = ScriptedChatModel()
    inner._boss_choice = ModelChoice("scripted", "the conformance engine")
    with pg.connect() as c:
        governed = model_routing.governed(inner, c, Settings(), project,
                                          skill_id="test.build", ref="implement-turn")
        set_llm_context(db_engine=pg, project_id=project, stage="implement")
        try:
            with pytest.raises(CallClassRefused, match="denies 'build_stage' model calls"):
                governed.create_text_response("the stage's turn")
        finally:
            clear_llm_context()
    assert inner.calls == 0
    assert _rows(pg, project) == []
    assert len(_refusal_events(pg, project)) == 1


# ── The gateway lease path: a denied proof never leases capacity ──────────────────────────────

def _budget(pg, pid: str) -> None:
    with pg.begin() as c:
        c.execute(text(
            "INSERT INTO budget_policies (id, scope_type, scope_id, daily_cap_usd_micros, "
            "monthly_cap_usd_micros, burst_cap_usd_micros_per_min) "
            "VALUES (:p, 'project', :p, 1000000, 1000000, 0) ON CONFLICT DO NOTHING"),
            {"p": pid})


def _candidate(pid: str) -> dict:
    return {
        "provider": "openai", "source_kind": "api", "entitlement_kind": "api_key",
        "owner_type": "project", "owner_id": pid, "cost_mode": "usage_based_api",
        "auth_method": "api_key", "connection_status": "connected", "status": "active",
        "runtime_supported": True, "observed_capacity": {"credential_present": False},
        "supported_models": ["gpt-4o-mini"], "workload_classes_allowed": ["owner_sandbox"],
        "risk_tier_max": 1, "limit_shape": {"max_concurrent": 1},
    }


def test_a_denied_proof_class_refuses_the_gateway_lease(pg, project):
    from boss_engine.services.setup_service import governed_proof

    _budget(pg, project)
    _set_policy(pg, project, {"proof": {"allow": False}})
    called = {"n": 0}

    def prove(bound):
        called["n"] += 1
        return {"ok": True, "state": "working", "cost_usd_micros": 1000}

    result = governed_proof(
        pg, Settings(), project, "setup-openai", "usr_cls_owner",
        intent=uuid4().hex, candidate=_candidate(project), credential="cls-nonsecret-marker",
        backend="database", capacity_group="cls-connection", bound_prove=prove)
    assert not result.get("ok"), result
    assert "denies 'proof' model calls" in str(result), result
    assert called["n"] == 0, "a denied lease must never run the proof call"
    assert _rows(pg, project) == []
    assert len(_refusal_events(pg, project)) == 1


# ── Govern's token guides: the stored daily cap now stops calls (BOSSAPP-999 §3) ──────────────

def test_a_stored_daily_token_guide_is_enforced_through_the_same_gate(pg, project, fixed_mode):
    with pg.begin() as c:
        c.execute(text(
            "INSERT INTO token_budget_policies (id, project_id, stage, max_tokens_per_day) "
            "VALUES (:id, :p, 'chat', 15)"), {"id": f"tbp_{uuid4().hex[:8]}", "p": project})
    set_llm_context(db_engine=pg)
    try:
        adapter = _chat_adapter(pg, project)
        adapter.create_text_response("one")   # 10 tokens recorded
        adapter.create_text_response("two")   # 20 >= 15: the next chat call is refused
        with pytest.raises(CallClassRefused, match="token budget .* is spent"):
            adapter.create_text_response("three")
    finally:
        clear_llm_context()
    assert fixed_mode.calls == 2
    # An UNSTORED guide (the console's documented defaults) must never become a surprise cap:
    # another stage with no stored row still authorizes.
    with pg.connect() as c:
        authorize(c, Settings(), project, stage="implement")


# ── BOSSAPP-1010: a transient retry is ONE call in the ledger ─────────────────────────────────

def test_fails_twice_then_succeeds_is_one_row_with_retries_2(pg, project):
    from boss_engine.agent.transient import _with_transient_retry
    from boss_engine.services.model_router.failover import RecordedChatAdapter

    scripted = ScriptedChatModel(fail_times=2)
    adapter = RecordedChatAdapter(scripted, engine_name="scripted", stage="chat",
                                  project_id=project)
    set_llm_context(db_engine=pg)
    try:
        _with_transient_retry(lambda: adapter.create_text_response("hiccups twice"),
                              attempts=3, backoff=0.01)
    finally:
        clear_llm_context()
    rows = _rows(pg, project)
    assert len(rows) == 1, f"one logical call, one row: {rows}"
    assert rows[0]["ok"] is True
    assert rows[0]["retries"] == 2, rows
    assert rows[0]["call_class"] == "chat"


def test_a_call_that_never_succeeds_still_lands_exactly_one_failed_row(pg, project):
    from boss_engine.agent.transient import _with_transient_retry
    from boss_engine.services.model_router.failover import RecordedChatAdapter

    scripted = ScriptedChatModel(fail_times=99)
    adapter = RecordedChatAdapter(scripted, engine_name="scripted", stage="chat",
                                  project_id=project)
    set_llm_context(db_engine=pg)
    try:
        with pytest.raises(RuntimeError, match="overloaded"):
            _with_transient_retry(lambda: adapter.create_text_response("never lands"),
                                  attempts=3, backoff=0.01)
    finally:
        clear_llm_context()
    rows = _rows(pg, project)
    assert len(rows) == 1, f"three attempts, one logical call, one failed row: {rows}"
    assert rows[0]["ok"] is False
    assert rows[0]["retries"] == 2, "the folded attempts ride the final row"
    assert "overloaded" in str(rows[0]["error"])


def test_outside_a_retry_scope_a_failure_still_records_at_once(pg, project):
    """The BOSSAPP-998 contract stands: a direct failed call (no retry wrapper) records its
    own failed row immediately — the scope only folds what a wrapper will retry."""
    from boss_engine.services.model_router.failover import RecordedChatAdapter

    scripted = ScriptedChatModel(fail_times=1)
    adapter = RecordedChatAdapter(scripted, engine_name="scripted", stage="chat",
                                  project_id=project)
    set_llm_context(db_engine=pg)
    try:
        with pytest.raises(RuntimeError):
            adapter.create_text_response("fails once, nobody retries")
    finally:
        clear_llm_context()
    rows = _rows(pg, project)
    assert len(rows) == 1 and rows[0]["ok"] is False and rows[0]["retries"] == 0
