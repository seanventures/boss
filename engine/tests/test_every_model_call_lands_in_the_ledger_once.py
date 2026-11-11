"""Every model call lands in llm_calls EXACTLY ONCE, with the stage its boundary named.

BOSSAPP-998, from the verified audit BOSSAPP-997:

  * Chat turns, panel seats and decisions — the chattiest call class — wrote NO llm_calls row
    at all on a Fixed-mode install, so the operator's governance surfaces (Obs, get_costs,
    provider_audit) never saw them. The recording seam is `model_routing.project_chat_adapter`:
    EVERY adapter it returns records each call through llm_telemetry.record_llm_call — the
    Smart path in FailoverChatAdapter, the Fixed path in RecordedChatAdapter.
  * The model gateway double-recorded research / converse / proof calls: the call's maker wrote
    its honest row AND the settlement wrote a flat 'model_gateway' row that joined to nothing.
    The settlement now records only a call nothing else recorded — under the stage its
    invocation metadata named, through the same seam.
  * The test arbiter was ledgered as review_code (held by its own test below).

These drive the REAL seam on a real Postgres (BOSS_TEST_DATABASE_URL); only the provider is
scripted. A NEW chat-shaped call site cannot dodge the ledger: there is no unrecorded adapter
out of project_chat_adapter, and a gateway settlement records whatever its caller did not.
"""
from __future__ import annotations

import ast
import os
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from boss_engine.config import Settings
from boss_engine.services import model_routing
from boss_engine.services.llm_telemetry import (
    STAGE_ROLES,
    clear_llm_context,
    record_llm_call,
    set_llm_context,
)
from boss_engine.services.model_router.router import RouteRequest
from boss_engine.services.model_routing import ModelChoice

PG_URL = os.environ.get("BOSS_TEST_DATABASE_URL", "")

SERVICES = Path(model_routing.__file__).parent


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
    pid = f"p_ledger1_{uuid4().hex[:10]}"
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


def _rows(pg, pid: str) -> list[dict]:
    with pg.connect() as c:
        return [dict(r) for r in c.execute(text(
            "SELECT stage, engine, model, role, ok, error, prompt_tokens, completion_tokens, "
            "       included_in_subscription, project_id "
            "FROM llm_calls WHERE project_id = :p ORDER BY created_at"),
            {"p": pid}).mappings()]


class ScriptedChatModel:
    """The scripted conformance engine's chat shape: deterministic, no model behind it."""

    def __init__(self, fail: bool = False):
        self.fail = fail
        self.calls = 0

    def _usage(self) -> dict:
        return {"model": "scripted-v1", "prompt_tokens": 8, "completion_tokens": 2,
                "billing": "local"}

    def create_text_response(self, prompt, system_prompt=None, model=None):
        self.calls += 1
        if self.fail:
            raise RuntimeError("scripted fault: the provider refused")
        return "scripted-session", "OK", self._usage()

    def create_chat_completion(self, messages, **kwargs):
        self.calls += 1
        return {"content": "ok", "tool_calls": None}, self._usage()

    def create_plain_answer(self, system_prompt, prompt):
        self.calls += 1
        return "OK", self._usage()


@pytest.fixture()
def fixed_mode(monkeypatch):
    """Fixed routing with the scripted chat model behind the one seam; nothing else faked."""
    scripted = ScriptedChatModel()
    monkeypatch.setattr(model_routing, "route_call", lambda *a, **k: None)
    monkeypatch.setattr(model_routing, "choose",
                        lambda *a, **k: ModelChoice("scripted", "the conformance engine"))
    monkeypatch.setattr(model_routing, "adapter_for", lambda *a, **k: scripted)
    return scripted


def _adapter(pg, settings, pid, purpose):
    with pg.connect() as c:
        with model_routing.routing_purpose(purpose):
            return model_routing.project_chat_adapter(c, settings, pid)


#: The surfaces the audit found unledgered, each by the purpose its real boundary names
#: (intent_router chat turns, meeting_service panel seats, agent_decision decisions).
SURFACES = [
    ("chat turn", RouteRequest(kind="chat", step="chat"), "chat"),
    ("panel seat", RouteRequest(kind="panel", step="panel_member"), "panel_member"),
    ("decision", RouteRequest(kind="agent", step="agent_decision"), "agent_decision"),
]


@pytest.mark.parametrize("name,purpose,stage", SURFACES, ids=[s[0] for s in SURFACES])
def test_a_chat_shaped_call_lands_exactly_once_with_its_own_stage(
        pg, project, fixed_mode, name, purpose, stage):
    settings = Settings()
    set_llm_context(db_engine=pg)
    try:
        adapter = _adapter(pg, settings, project,
                           RouteRequest(kind=purpose.kind, step=purpose.step,
                                        project_id=project))
        adapter.create_text_response("one governed call")
    finally:
        clear_llm_context()
    rows = _rows(pg, project)
    assert len(rows) == 1, f"a {name} must write exactly one llm_calls row, got {rows}"
    assert rows[0]["stage"] == stage
    assert rows[0]["engine"] == "scripted"
    assert rows[0]["model"] == "scripted-v1"
    assert rows[0]["ok"] is True
    assert rows[0]["project_id"] == project
    assert rows[0]["included_in_subscription"] is True, "billing: local is not money"


def test_every_adapter_shape_records_once_and_a_failure_records_too(pg, project, fixed_mode):
    settings = Settings()
    set_llm_context(db_engine=pg)
    try:
        adapter = _adapter(pg, settings, project,
                           RouteRequest(kind="chat", step="chat", project_id=project))
        adapter.create_chat_completion([{"role": "user", "content": "hi"}])
        adapter.create_plain_answer("be brief", "hi")
        fixed_mode.fail = True
        with pytest.raises(RuntimeError):
            adapter.create_text_response("this one fails")
    finally:
        clear_llm_context()
    rows = _rows(pg, project)
    assert len(rows) == 3, rows
    assert [r["ok"] for r in rows] == [True, True, False]
    assert "scripted fault" in str(rows[2]["error"])
    assert all(r["stage"] == "chat" for r in rows)


def test_an_unnamed_boundary_is_still_ledgered_never_invisible(pg, project, fixed_mode):
    """A NEW call site that names no purpose cannot dodge the ledger: the Fixed path has no
    unrecorded adapter left. Its stage is the router's generic name for an unnamed boundary."""
    settings = Settings()
    set_llm_context(db_engine=pg)
    try:
        with pg.connect() as c:
            adapter = model_routing.project_chat_adapter(c, settings, project)
        adapter.create_text_response("an ungoverned one-off")
    finally:
        clear_llm_context()
    rows = _rows(pg, project)
    assert len(rows) == 1, rows
    assert rows[0]["stage"] == "model_call"


def test_a_caller_that_records_its_own_turns_switches_the_wrapper_off(
        pg, project, fixed_mode):
    """agent_converse's contract: it writes the stage-converse row itself (success AND failure),
    so it sets `_record = False` on the adapter — one row per call, written by one hand."""
    settings = Settings()
    set_llm_context(db_engine=pg)
    try:
        adapter = _adapter(pg, settings, project,
                           RouteRequest(kind="chat", step="chat", project_id=project))
        assert hasattr(adapter, "_record"), \
            "the seam's opt-out contract (agent_converse.make_adapter) must keep working"
        adapter._record = False
        adapter.create_text_response("the caller will record this one")
    finally:
        clear_llm_context()
    assert _rows(pg, project) == [], "the wrapper must not record when the caller does"


# ── The gateway settlement: one row per call, whoever writes it ───────────────────────────────

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


def _proof(pg, pid: str, prove) -> dict:
    from boss_engine.services.setup_service import governed_proof

    return governed_proof(
        pg, Settings(), pid, "setup-openai", "usr_ledger_owner",
        intent=uuid4().hex, candidate=_candidate(pid), credential="ledger-nonsecret-marker",
        backend="database", capacity_group="ledger-connection", bound_prove=prove)


def test_a_gateway_settled_call_nothing_else_recorded_lands_once_with_its_stage(pg, project):
    """An API-key proof makes no self-recorded call; the settlement is its recorder — ONE row,
    under the stage the invocation metadata named, never the flat 'model_gateway'."""
    _budget(pg, project)
    result = _proof(pg, project, lambda bound: {"ok": True, "state": "working",
                                                "cost_usd_micros": 1000})
    assert result.get("ok"), result
    rows = _rows(pg, project)
    assert len(rows) == 1, rows
    assert rows[0]["stage"] == "connection_proof"
    assert rows[0]["ok"] is True


def test_the_old_double_write_site_now_writes_once(pg, project):
    """The killed duplicate: a call that records itself (a CLI proof, a research session, a
    converse turn) and THEN settles through the gateway lands once — the caller's row, with the
    caller's stage; the settlement adds nothing."""
    _budget(pg, project)

    def prove(bound):
        # What cli_harness.run_role does inside a real CLI proof: record through the seam.
        row_id = record_llm_call(engine_name="scripted",
                                 usage={"model": "scripted-v1", "prompt_tokens": 4,
                                        "completion_tokens": 1, "billing": "local"},
                                 cost_usd_micros=0, latency_ms=5, turns=1)
        assert row_id, "the seam returns the row id it wrote"
        return {"ok": True, "state": "working", "cost_usd_micros": 1000}

    result = _proof(pg, project, prove)
    assert result.get("ok"), result
    rows = _rows(pg, project)
    assert len(rows) == 1, f"exactly one row for one call, got {rows}"
    assert rows[0]["engine"] == "scripted", "the caller's own row is the one that is kept"
    assert rows[0]["stage"] == "connection_proof", \
        "the ambient proof stage, set by governed_proof, names the row"


def test_a_failed_call_the_caller_already_recorded_is_not_recorded_again(pg, project):
    """A CLI that timed out records its failed row before raising; the failed settlement must
    not add a second one (314 of the month's proof rows were exactly this twin pair)."""
    _budget(pg, project)

    def prove(bound):
        record_llm_call(engine_name="scripted", usage={"model": "scripted-v1"},
                        ok=False, error="timed out after 420s", latency_ms=420000)
        # 401: an explicit provider rejection, so the spend settles as FAILED through the
        # gateway (the strongest case: a failed settlement that must still not double-write).
        return {"ok": False, "error": "the agent timed out", "status": 401}

    _proof(pg, project, prove)
    rows = _rows(pg, project)
    assert len(rows) == 1, rows
    assert rows[0]["ok"] is False and "timed out" in str(rows[0]["error"])


# ── The arbiter's own stage, enforced at the source ───────────────────────────────────────────

def test_the_test_arbiter_is_ledgered_under_its_own_stage():
    """The arbiter judges QA's oracle; booked as review_code it overstated review spend and hid
    its own. The pipeline names the stage where it sets the call context, and the role map
    knows who spends it."""
    assert STAGE_ROLES.get("test_arbitration") == "adversarial_reviewer"
    tree = ast.parse((SERVICES / "governed_code_task_service.py").read_text())
    arbiters = [n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == "_arbitrate_tests"]
    assert arbiters, "_arbitrate_tests moved; point this test at the arbiter's new home"
    stages = [kw.value.value
              for n in ast.walk(arbiters[0]) if isinstance(n, ast.Call)
              and getattr(n.func, "id", getattr(n.func, "attr", "")) == "set_llm_context"
              for kw in n.keywords if kw.arg == "stage" and isinstance(kw.value, ast.Constant)]
    assert "test_arbitration" in stages, stages
    assert "review_code" not in stages, \
        "the arbiter must never book its call as review_code again"


def test_the_gateway_writes_llm_calls_only_through_the_recording_seam():
    """No raw INSERT INTO llm_calls outside llm_telemetry: a second writer is where the
    double-count came from. (Tests and the schema itself may; services may not.)"""
    offenders = []
    for path in sorted(SERVICES.rglob("*.py")):
        if path.name == "llm_telemetry.py":
            continue
        if "INSERT INTO llm_calls" in path.read_text():
            offenders.append(str(path))
    assert offenders == [], f"record through llm_telemetry.record_llm_call: {offenders}"
