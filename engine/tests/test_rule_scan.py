"""Scan for business rules (KNOWLEDGE_GRAPH_PRODUCT_SPEC.md §4.5, stream S3).

On the scripted conformance engine — no paid model: every call is reserved before it is sent,
the scan stops at each of its limits and says which, its report has one shape, every proposal
goes through rules.propose_rule with evidence BOSS read itself and provenance from the LLM
context, and the catalogue workflow, the review gate and its buttons hold together.

The DB test at the end walks a real code graph and reserves through the real budget governor
(BOSS_KG_TEST_DATABASE_URL, a disposable database; tree-sitter, so inside the API image).
"""
from __future__ import annotations

import contextlib
import os
import uuid
from typing import Any

import pytest

from boss_engine.services import decision_actions, workflow_outcomes
from boss_engine.services.knowledge import rule_scan, rules
from boss_engine.services.knowledge.rule_scan import Unit
from boss_engine.services.llm_telemetry import clear_llm_context, get_llm_context, set_llm_context
from boss_engine.services.scripted_engine import ScriptedEngine
from boss_engine.services.workflow_capabilities import CAPABILITIES

REPO = "owner/app"
PKG = "com.example.loans"
SCOPE = [f"pkg:{REPO}:core:{PKG}"]


# ── fakes around the scan's edges ───────────────────────────────────────────────────────────

class FakeConnection:
    engine = None

    @contextlib.contextmanager
    def begin_nested(self):
        yield self


class Log(list):
    pass


class RecordingEngine:
    """The scripted conformance engine, with its calls written into the shared log."""

    def __init__(self, log: Log, *, fail_on: int = 0, unreported: bool = False):
        self.inner = ScriptedEngine()
        self.engine = self.inner.engine
        self.log = log
        self.fail_on = fail_on
        self.unreported = unreported
        self.prompts: list[str] = []

    def run_role(self, prompt, cwd, **kwargs):
        self.log.append(("call", len(self.prompts) + 1))
        self.prompts.append(prompt)
        assert kwargs.get("read_only") is True, "a scan never writes code"
        assert get_llm_context().get("stage") == rule_scan.STAGE
        if self.fail_on and len(self.prompts) == self.fail_on:
            raise RuntimeError("scripted engine down")
        out = self.inner.run_role(prompt, cwd, **kwargs)
        if not self.unreported:
            out["cost_unreported"] = False
        return out


class RecordingGovernor:
    def __init__(self, log: Log, *, estimate: int = 100_000, cost: int = 100_000,
                 refuse_on: int = 0):
        self.log = log
        self.estimate_usd_micros = estimate
        self.cost = cost
        self.refuse_on = refuse_on
        self.reserved = 0

    def reserve(self):
        self.reserved += 1
        if self.refuse_on and self.reserved == self.refuse_on:
            raise rule_scan.BudgetRefused("Budget reservation denied (budget cap)")
        self.log.append(("reserve", self.reserved))
        return ("res", self.reserved)

    def settle(self, held, usage):
        self.log.append(("settle", held[1]))
        return self.cost

    def fail(self, held, error):
        self.log.append(("release", held[1]))


def _unit(i: int, *, package: str = PKG, writes=(), callees=None, lines=20) -> Unit:
    path = f"core/src/{package.replace('.', '/')}/C{i}.java"
    key = f"symbol:{REPO}:{path}:C{i}"
    start = 10
    unit = Unit(key=key, name=f"C{i}", path=path, repo=REPO, area="core", package=package,
                line=start, end_line=start + lines, writes=set(writes),
                callees=dict(callees or {}))
    unit.symbols = [(key, f"C{i}", start, start + lines),
                    (f"{key}.check", f"C{i}.check", start + 2, start + 8)]
    return unit


def _reader(path, start, end, repo):
    return {"content": "\n".join(f"line {n} of {path.rsplit('/', 1)[-1]}"
                                 for n in range(start, end + 1)), "start": start, "end": end}


@pytest.fixture()
def proposed(monkeypatch):
    calls: list[dict[str, Any]] = []

    def propose_rule(connection, **kwargs):
        calls.append({**kwargs, "_context": get_llm_context()})
        return {"node": {"id": f"kgn_rule_{len(calls)}", "key": f"rule:{len(calls)}"},
                "status": "proposed"}

    monkeypatch.setattr(rules, "propose_rule", propose_rule)
    monkeypatch.setattr(rule_scan, "_check_entitlement", lambda *a, **k: None)
    monkeypatch.setattr(rule_scan, "_project_cap", lambda *a, **k: 0)
    from boss_engine.services import llm_telemetry

    monkeypatch.setattr(llm_telemetry, "record_llm_call", lambda *a, **k: None)
    monkeypatch.setattr(llm_telemetry, "record_agent_turns", lambda *a, **k: 0)
    clear_llm_context()
    yield calls
    clear_llm_context()


def _scan(monkeypatch, units, *, log=None, engine=None, governor=None, **inputs):
    log = log if log is not None else Log()
    monkeypatch.setattr(rule_scan, "units_in_scope", lambda *a, **k: list(units))
    engine = engine or RecordingEngine(log)
    governor = governor or RecordingGovernor(log)
    report = rule_scan.scan(
        FakeConnection(), None, project_id="proj_rs", scope=SCOPE, inputs=inputs,
        scan_id="rscan_test", agent_role="chief_architect", agent_name="Scout",
        workflow_run_id="wfr_test", step_id="scan", agent=engine, engine="scripted",
        reader=_reader, governor=governor)
    return report, log, engine


# ── budget ──────────────────────────────────────────────────────────────────────────────────

def test_every_model_call_is_reserved_before_it_is_sent(monkeypatch, proposed):
    report, log, _engine = _scan(monkeypatch, [_unit(i) for i in range(3)])
    assert report["calls"] == 3
    assert log == [("reserve", 1), ("call", 1), ("settle", 1),
                   ("reserve", 2), ("call", 2), ("settle", 2),
                   ("reserve", 3), ("call", 3), ("settle", 3)]


def test_a_refused_reservation_sends_nothing_and_stops_in_words(monkeypatch, proposed):
    log = Log()
    report, log, engine = _scan(monkeypatch, [_unit(i) for i in range(3)], log=log,
                                governor=RecordingGovernor(log, refuse_on=2))
    assert len(engine.prompts) == 1, "the call whose reservation was refused was sent"
    assert report["stopped_by"] == "budget_refused"
    assert "budget cap" in report["stopped_reason"]


def test_it_stops_before_a_call_that_could_pass_its_budget(monkeypatch, proposed):
    log = Log()
    report, _log, engine = _scan(monkeypatch, [_unit(i) for i in range(5)], log=log,
                                 governor=RecordingGovernor(log, estimate=100_000, cost=100_000),
                                 budget_usd=0.25)
    # 0.10 + 0.10 spent; a third call reserving 0.10 could reach 0.30 > 0.25.
    assert len(engine.prompts) == 2
    assert report["stopped_by"] == "budget"
    assert report["spend"] == pytest.approx(0.20)
    assert "$0.25" in report["stopped_reason"]


def test_an_engine_that_does_not_report_its_cost_is_charged_its_reservation(monkeypatch,
                                                                            proposed):
    log = Log()
    report, _log, engine = _scan(monkeypatch, [_unit(i) for i in range(5)], log=log,
                                 engine=RecordingEngine(log, unreported=True),
                                 governor=RecordingGovernor(log, estimate=100_000, cost=0),
                                 budget_usd=0.25)
    assert len(engine.prompts) == 2, "an unreported cost was counted as free"
    assert report["stopped_by"] == "budget"


def test_a_failed_call_is_released_and_the_scan_stops_with_what_it_had(monkeypatch, proposed):
    log = Log()
    report, log, _engine = _scan(monkeypatch, [_unit(i) for i in range(3)], log=log,
                                 engine=RecordingEngine(log, fail_on=2))
    assert ("release", 2) in log and ("settle", 2) not in log
    assert report["stopped_by"] == "engine_failed"
    assert report["rules_proposed"] == 1, "what was proposed before the failure was lost"
    assert "scripted engine down" in report["stopped_reason"]
    verdict = workflow_outcomes.VERDICTS["scan_business_rules"](
        {"scan_id": "x", "stopped_by": "engine_failed", "rules_proposed": 1}, {})
    assert verdict.outcome == workflow_outcomes.SUCCEEDED


# ── limits ──────────────────────────────────────────────────────────────────────────────────

def test_it_stops_at_max_files(monkeypatch, proposed):
    report, _log, engine = _scan(monkeypatch, [_unit(i) for i in range(5)], max_files=2)
    assert report["files_read"] == 2 and len(engine.prompts) == 2
    assert report["stopped_by"] == "max_files"
    assert "(2)" in report["stopped_reason"]


def test_it_stops_at_max_rules(monkeypatch, proposed):
    report, _log, engine = _scan(monkeypatch, [_unit(i) for i in range(5)], max_rules=2)
    assert report["rules_proposed"] == 2 == len(proposed)
    assert len(engine.prompts) == 2, "it called the model again with no rule left to propose"
    assert report["stopped_by"] == "max_rules"


def test_it_stops_when_the_scope_is_read(monkeypatch, proposed):
    report, _log, _engine = _scan(monkeypatch, [_unit(i) for i in range(3)])
    assert report["stopped_by"] == "scope_exhausted"
    assert report["stopped_reason"] == "every class in the scope was read"


@pytest.mark.parametrize("inputs, words", [
    ({"max_files": 0}, "at least 1"),
    ({"max_rules": 10_000}, "at most"),
    ({"budget_usd": 0}, "more than $0.00"),
    ({"budget_usd": "lots"}, "amount in dollars"),
])
def test_bad_limits_are_refused_in_words(inputs, words):
    with pytest.raises(rule_scan.RuleScanRefused, match=words.replace("$", r"\$")):
        rule_scan.parse_limits(inputs)


def test_the_budget_is_at_most_the_projects_cap():
    limits = rule_scan.parse_limits({"budget_usd": 5}, cap_usd_micros=3_000_000)
    assert limits.budget_usd_micros == 3_000_000
    assert "cap" in limits.notes[0]
    default = rule_scan.parse_limits({})
    assert (default.max_files, default.max_rules, default.budget_usd_micros) == (
        60, 25, 2_000_000)


# ── the report ──────────────────────────────────────────────────────────────────────────────

def test_the_report_has_one_shape(monkeypatch, proposed):
    report, _log, _engine = _scan(monkeypatch, [_unit(i) for i in range(2)])
    for key in ("scan_id", "files_read", "rules_proposed", "spend", "stopped_by",
                "stopped_reason", "summary", "proposals", "set_aside", "unread", "limits",
                "calls", "provenance", "scope"):
        assert key in report, key
    assert report["stopped_by"] in rule_scan.STOPPED_BY
    assert report["proposals"][0]["rule_id"] == "kgn_rule_1"
    assert report["summary"].startswith("Proposed 2 rules from 2 files")
    assert "Rules review" in report["summary"]


def test_the_step_result_carries_what_the_review_gate_and_console_read(monkeypatch, proposed):
    monkeypatch.setattr(rule_scan, "units_in_scope", lambda *a, **k: [_unit(1)])
    monkeypatch.setattr(rule_scan, "_scan_agent",
                        lambda *a, **k: (RecordingEngine(Log()), "scripted"))
    monkeypatch.setattr(rule_scan, "Governor", lambda *a, **k: RecordingGovernor(Log()))
    monkeypatch.setattr(rule_scan, "repo_reader", lambda *a, **k: _reader)
    monkeypatch.setattr(rule_scan, "_agent_name", lambda *a, **k: "Scout")
    monkeypatch.setattr(rule_scan, "_step_claim", lambda *a, **k: None)
    from boss_engine.services import model_routing

    monkeypatch.setattr(model_routing, "routed_settings", lambda c, s, p: s)
    out = rule_scan.run_step(FakeConnection(), None, "proj_rs",
                             {"id": "scan", "captain": "chief_architect"},
                             {"run_id": "wfr_abc"}, {"scope": SCOPE})
    assert set(CAPABILITIES["scan_business_rules"].produces) <= set(out)
    assert out["review_link"] == {"area": "knowledge", "view": "rules", "status": "proposed",
                                  "scan_id": out["scan_id"], "label": "Open Rules review"}
    verdict = workflow_outcomes.judge("scan_business_rules", out, {})
    assert verdict.outcome == workflow_outcomes.SUCCEEDED


# ── proposals ───────────────────────────────────────────────────────────────────────────────

def test_proposals_go_through_propose_rule_with_evidence_boss_read(monkeypatch, proposed):
    monkeypatch.setattr(rule_scan, "_package_known", lambda *a, **k: True)
    report, _log, _engine = _scan(monkeypatch, [_unit(7)])
    assert report["rules_proposed"] == 1
    (call,) = proposed
    unit = _unit(7)
    assert call["project_id"] == "proj_rs"
    assert call["category"] in rules.CATEGORIES
    assert call["scope"] == {"repo": REPO, "areas": ["core"],
                             "packages": [f"pkg:{REPO}:core:{PKG}"], "symbols": [unit.key]}
    assert call["scan_id"] == "rscan_test"
    (evidence,) = call["evidence"]
    assert evidence["symbol"] == unit.key, "evidence must be a key the graph resolves"
    assert evidence["lines"] == [10, 10]
    # BOSS's own copy of the cited line, never the model's retyping of it.
    assert evidence["excerpt"] == "line 10 of C7.java"


def test_the_persons_focus_reaches_the_agent(monkeypatch, proposed):
    _report, _log, engine = _scan(monkeypatch, [_unit(1)], focus="who may approve a payout")
    assert "CARES ABOUT MOST: who may approve a payout" in engine.prompts[0]


def test_each_proposal_carries_provenance_from_the_context(monkeypatch, proposed):
    set_llm_context(stage="caller_stage", project_id="other")
    _scan(monkeypatch, [_unit(1)])
    context = proposed[0]["_context"]
    assert context["role_key"] == "chief_architect"
    assert context["agent_name"] == "Scout"
    assert context["workflow_run_id"] == "wfr_test"
    assert context["step"] == "scan"
    assert context["scan_id"] == context["knowledge_scan_id"] == "rscan_test"
    assert context["stage"] == rule_scan.STAGE
    assert context["model_id"] == "scripted-v1"
    # The caller's context is put back.
    assert get_llm_context() == {"stage": "caller_stage", "project_id": "other"}


def test_evidence_the_scan_did_not_read_is_set_aside_not_proposed():
    unit = _unit(1)
    held = {10: "a", 11: "b"}
    args, why = rule_scan.validate_proposal(unit, {
        "title": "t", "statement": "s", "category": "limit",
        "evidence": [{"symbol": "C1.check", "lines": [40, 41], "excerpt": "invented"}]}, held)
    assert args is None and "lines of the code that was read" in why
    args, _ = rule_scan.validate_proposal(unit, {
        "statement": "s", "category": "nonsense", "confidence": 7,
        "evidence": [{"symbol": "check", "lines": "10-11", "excerpt": "invented"}]}, held)
    assert args["category"] == "other" and args["confidence"] == 1.0
    assert args["evidence"] == [{"symbol": f"{unit.key}.check", "lines": [10, 11],
                                 "excerpt": "a\nb"}]


def test_a_refused_proposal_is_reported_and_the_scan_goes_on(monkeypatch, proposed):
    def refuse(connection, **kwargs):
        raise ValueError("evidence symbol does not resolve in this project")

    monkeypatch.setattr(rules, "propose_rule", refuse)
    report, _log, engine = _scan(monkeypatch, [_unit(i) for i in range(2)])
    assert len(engine.prompts) == 2 and report["rules_proposed"] == 0
    assert report["set_aside"][0]["reason"].startswith("evidence symbol does not resolve")


def test_an_unreadable_answer_is_counted_not_guessed():
    assert rule_scan.parse_answer("I think there are rules here.") is None
    assert rule_scan.parse_answer("=== RULES ===\n[]\n=== END RULES ===") == []
    fenced = "=== RULES ===\n```json\n[{\"statement\": \"x\"}]\n```\n=== END RULES ==="
    assert rule_scan.parse_answer(fenced) == [{"statement": "x"}]


def test_the_scripted_engine_plays_the_scan_stage(monkeypatch):
    from boss_engine.services import llm_telemetry

    monkeypatch.setattr(llm_telemetry, "record_llm_call", lambda *a, **k: None)
    monkeypatch.setattr(llm_telemetry, "record_agent_turns", lambda *a, **k: 0)
    set_llm_context(stage=rule_scan.STAGE)
    try:
        prompt = rule_scan.scan_prompt(_unit(1), "   10 | x", start=10, end=10, facts=[],
                                       remaining=3, scope_words="package p")
        out = ScriptedEngine().run_role(prompt, os.getcwd(), read_only=True)
    finally:
        clear_llm_context()
    (rule,) = rule_scan.parse_answer(out["summary"])
    assert rule["evidence"] == [{"symbol": "C1", "lines": [10, 10]}]


# ── scope and walk ──────────────────────────────────────────────────────────────────────────

def test_the_consoles_scope_shape_is_accepted():
    """The Graph's "Scan for rules here" starts the workflow with input
    {scope: [<node key or id>], max_files, max_rules, budget_usd}."""
    scope = rule_scan.parse_scope([
        f"area:{REPO}:core", f"pkg:{REPO}:core:{PKG}", f"symbol:{REPO}:core/A.java:A",
        f"file:{REPO}:core/B.java", "kgn_208a03a604874f2eacc1"])
    assert scope.repo == REPO and scope.symbols == (f"symbol:{REPO}:core/A.java:A",)
    assert scope.files == ("core/B.java",) and scope.ids == ("kgn_208a03a604874f2eacc1",)
    # A repository with no name (code_index._prefix writes no segment for it).
    bare = rule_scan.parse_scope(["symbol:money/ledger.py:Ledger", "file:money/x.py",
                                  "area::money"])
    assert bare.repo is None and bare.files == ("money/x.py",)
    limits = rule_scan.parse_limits({"max_files": 60, "max_rules": 25, "budget_usd": 2.0})
    assert (limits.max_files, limits.max_rules, limits.budget_usd_micros) == (60, 25, 2_000_000)


@pytest.mark.parametrize("value, words", [
    (5, "must be a list of keys"),
    ([{"key": "area:x:y"}], "must be a key"),
    ([f"area:{REPO}:a"] * 1 + [f"pkg:{REPO}:a:p{i}" for i in range(60)], "at most 50"),
    (["symbol:nocolon"], "not a class key"),
])
def test_a_scope_the_scan_cannot_read_is_refused_in_words(value, words):
    with pytest.raises(rule_scan.RuleScanRefused, match=words):
        rule_scan.parse_scope(value)


def test_scope_keys_are_the_trees():
    scope = rule_scan.parse_scope(f"area:{REPO}:core, pkg:{REPO}:core:{PKG}")
    assert scope.repo == REPO and scope.areas == ("core",) and scope.packages == (("core", PKG),)
    # A bare word is a repository path now (resolved, or refused, against the graph:
    # test_scope_forms_resolve_on_a_real_graph_and_are_checked_at_start); text that is
    # neither a key nor a path is still refused here.
    assert rule_scan.parse_scope(["loans"]).paths == ("loans",)
    for bad, words in (([], "Choose what to scan"), (["loans? please"], "not something"),
                       ([f"area:{REPO}:a", "area:other/repo:b"], "one repository")):
        with pytest.raises(rule_scan.RuleScanRefused, match=words):
            rule_scan.parse_scope(bad)


def test_the_walk_goes_module_by_module_heaviest_first_and_follows_calls():
    light = _unit(1, package="p.light")
    caller = _unit(2, package="p.heavy", writes={"loan"})
    callee = _unit(3, package="p.light")
    other = _unit(4, package="p.heavy")
    caller.callees = {callee.key: 5}
    units = [light, caller, callee, other]
    for unit in units:
        unit.score = rule_scan.weigh(unit, {u.key: u for u in units})
    order = [u.name for u in rule_scan.walk_order(units)]
    # The heavy package first; its writer, then what the writer calls, then the rest.
    assert order == ["C2", "C3", "C4", "C1"]


def test_tests_and_interfaces_are_not_read():
    test_unit = _unit(1)
    test_unit.path = "core/src/test/java/C1Test.java"
    iface = _unit(2)
    iface.type_kind = "interface"
    assert not rule_scan._readable(test_unit)
    assert not rule_scan._readable(iface)
    assert rule_scan._readable(_unit(3))


# ── the capability, the catalogue, the gate ─────────────────────────────────────────────────

def test_the_capabilities_are_declared_and_judged():
    scan = CAPABILITIES["scan_business_rules"]
    assert scan.required_inputs == ("scope",)
    assert set(scan.optional_inputs) == {"focus", "max_files", "max_rules", "budget_usd",
                                         "exclude_tests"}
    assert scan.tool is None and scan.needs_model and not scan.is_async
    review = CAPABILITIES["rules_review"]
    assert review.is_gate and review.risk_tier >= 2
    assert workflow_outcomes.VERDICTS["rules_review"] is workflow_outcomes.answered_gate
    assert workflow_outcomes.judge("scan_business_rules", {"status": "succeeded"}, {}).outcome \
        == workflow_outcomes.FAILED


def test_the_catalogue_entry_validates_and_asks_for_its_scope():
    from boss_engine.services import workflows
    from boss_engine.services.workflow_catalogue import (CATALOGUE, catalogue_summary,
                                                         entry_spec)

    entry = next(e for e in CATALOGUE if e["key"] == "business_rules_scan")
    spec = entry_spec(entry)
    effective = workflows.validate_workflow_spec(
        {"entry_step": spec["entry_step"], "steps": [dict(s) for s in entry["steps"]]})
    assert [s["id"] for s in effective["steps"]] == ["scan", "review"]
    assert [(s["binding"]["capability"]) for s in entry["steps"]] == [
        "scan_business_rules", "rules_review"]
    assert entry["steps"][1]["binding"]["inputs"]["scan_id"] == {
        "from_step": "scan", "field": "scan_id"}
    assert spec["input_schema"]["required"] == ["scope"]
    assert set(spec["input_schema"]["properties"]) == {"scope", "directive", "max_files",
                                                       "max_rules", "budget_usd",
                                                       "exclude_tests"}
    assert entry["steps"][0]["binding"]["inputs"]["focus"] == {"from_input": "directive"}
    card = next(c for c in catalogue_summary(prd_auto_approve=True)
                if c["key"] == "business_rules_scan")
    assert card["gates"] == ["review"]
    assert card["example"]["scope"]


def test_the_review_gate_is_a_workflow_gate_with_its_own_buttons():
    decision = {"status": "pending", "recommendation": {
        "action": rule_scan.REVIEW_ACTION, "workflow_run_id": "wfr_x", "step_id": "review",
        "capability": "rules_review", "scan_id": "rscan_x"}}
    assert rule_scan.REVIEW_ACTION in decision_actions.WORKFLOW_GATE_ACTIONS
    assert decision_actions.kind_of(decision) == "workflow_gate"
    actions = decision_actions.allowed_actions(decision)
    assert [a["action"] for a in actions] == ["approve", "reject", "delegate"]
    assert "Approves no rule" in actions[0]["effect"]
    with pytest.raises(decision_actions.DecisionActionNotAllowed):
        decision_actions.require_allowed(decision, "revise")


def test_an_empty_queue_lets_the_gate_step_aside():
    result = rule_scan.not_applicable({"steps": {"scan": {"scan_id": "rscan_x"}}})
    assert result["status"] == "not_applicable" and result["scan_id"] == "rscan_x"
    assert workflow_outcomes.judge("rules_review", result, {}).outcome == \
        workflow_outcomes.SUCCEEDED


# ── on a real graph and the real budget governor ───────────────────────────────────────────

def _database_url() -> str:
    return os.environ.get("BOSS_KG_TEST_DATABASE_URL") or ""


@pytest.fixture(scope="module")
def pg_engine():
    url = _database_url()
    name = url.rsplit("/", 1)[-1].split("?", 1)[0].lower()
    if not url or not any(m in name for m in ("test", "testing", "ci", "scratch", "tmp")):
        pytest.skip("BOSS_KG_TEST_DATABASE_URL must name a disposable database")
    from sqlalchemy import create_engine

    from boss_engine.db import ensure_schema

    engine = create_engine(url, future=True)
    ensure_schema(engine)
    return engine


LEDGER = '''
class Ledger:
    def post(self, amount):
        if amount <= 0:
            raise ValueError("amount must be positive")
        db.execute("INSERT INTO ledger_entries (amount) VALUES (%s)", amount)


class Loans:
    def disburse(self, loan, amount):
        Ledger().post(amount)
        return amount


class Helper:
    def fmt(self, x):
        return str(x)
'''


def test_a_scan_on_a_real_graph_reserves_through_the_governor(pg_engine, proposed, monkeypatch):
    pytest.importorskip("tree_sitter")
    from sqlalchemy import text

    from boss_engine.config import get_settings
    from boss_engine.services.knowledge.code_index import index_files
    from boss_engine.services.knowledge.store import PostgresKnowledgeStore

    suffix = uuid.uuid4().hex[:8]
    project = f"proj_rs_{suffix}"
    with pg_engine.connect() as conn:
        transaction = conn.begin()
        try:
            conn.execute(text("INSERT INTO orgs (id, name) VALUES (:o, 'RS')"),
                         {"o": f"org_rs_{suffix}"})
            conn.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES (:w, :o, 'RS')"),
                         {"w": f"ws_rs_{suffix}", "o": f"org_rs_{suffix}"})
            conn.execute(text("INSERT INTO projects (id, workspace_id, name, slug) "
                              "VALUES (:p, :w, 'RS', :s)"),
                         {"p": project, "w": f"ws_rs_{suffix}", "s": f"rs-{suffix}"})
            index_files(conn, PostgresKnowledgeStore(), project_id=project,
                        files=[{"path": "money/ledger.py", "language": "python",
                                "source": LEDGER}])
            scope = rule_scan.parse_scope(["area::money"])
            # The path reading, before the Graph's tree exists.
            assert {u.name for u in rule_scan.units_in_scope(conn, project, scope)} >= {
                "Ledger", "Loans", "Helper"}
            from boss_engine.services.knowledge import view_rollup

            view_rollup.rebuild(conn, project)
            tree = conn.execute(text(
                "SELECT key, level, node_id FROM kg_view_tree WHERE project_id = :p"),
                {"p": project}).mappings().all()
            pkg_key = next(r["key"] for r in tree if r["level"] == "package")
            ledger = next(r for r in tree if r["level"] == "class" and r["key"].endswith(":Ledger"))
            post = next(r for r in tree if r["level"] == "method"
                        and r["key"].endswith("Ledger.post"))
            # The tree's own keys, a node id and a method (read as its class) all resolve.
            by_pkg = rule_scan.units_in_scope(conn, project, rule_scan.parse_scope([pkg_key]))
            assert {u.name for u in by_pkg} >= {"Ledger", "Loans", "Helper"}
            by_id = rule_scan.resolve_ids(conn, project,
                                          rule_scan.parse_scope([ledger["node_id"]]))
            assert by_id.symbols == (ledger["key"],)
            by_method = rule_scan.units_in_scope(conn, project,
                                                 rule_scan.parse_scope([post["key"]]))
            assert [u.name for u in by_method] == ["Ledger"]
            units = rule_scan.units_in_scope(conn, project, scope)
            by_name = {u.name: u for u in units}
            assert {"Ledger", "Loans", "Helper"} <= set(by_name)
            assert "ledger_entries" in by_name["Ledger"].writes
            assert by_name["Ledger"].score > by_name["Helper"].score
            order = [u.name for u in rule_scan.walk_order(units)]
            assert order.index("Ledger") < order.index("Helper")

            log = Log()
            engine = RecordingEngine(log)
            settings = get_settings()
            governor = rule_scan.Governor(conn, settings, project, engine="native",
                                          ref="rscan_db")
            assert governor.estimate_usd_micros == rule_scan.CALL_ESTIMATE_USD_MICROS
            report = rule_scan.scan(
                conn, settings, project_id=project, scope=["area::money"],
                inputs={"max_rules": 2}, scan_id="rscan_db", agent=engine, engine="native",
                reader=lambda path, s, e, repo: {"content": "\n".join(
                    LEDGER.splitlines()[s - 1:e]), "start": s, "end": e},
                governor=governor)
            assert report["rules_proposed"] == 2 and report["stopped_by"] == "max_rules"
            assert len(proposed) == 2
            rows = conn.execute(text(
                "SELECT category, amount_usd_micros, status FROM budget_reservations "
                "WHERE project_id = :p ORDER BY created_at"), {"p": project}).all()
            assert len(rows) == report["calls"] == 2
            assert {r[0] for r in rows} == {"knowledge"}
            assert all(r[1] == rule_scan.CALL_ESTIMATE_USD_MICROS for r in rows)
            assert all(r[2] != "active" for r in rows), "a reservation was left open"
        finally:
            transaction.rollback()


# ── through the real runner: scan → review gate → settled when the queue is empty ───────────

def test_the_workflow_scans_opens_its_review_and_closes_it_when_the_queue_is_empty(
        monkeypatch, proposed):
    from sqlalchemy import text

    from boss_engine.services import workflow_capability_runner as runner
    from boss_engine.services.workflow_catalogue import CATALOGUE, entry_spec

    from .test_workflow_capability_runner import Ctx, _publish, _start, _steps

    if not os.environ.get("BOSS_TEST_DATABASE_URL"):
        pytest.skip("needs BOSS_TEST_DATABASE_URL (the runner's disposable database)")
    from sqlalchemy import create_engine

    eng = create_engine(os.environ["BOSS_TEST_DATABASE_URL"], future=True)
    with eng.begin() as c:
        workspace = c.execute(text("SELECT workspace_id FROM projects LIMIT 1")).scalar()
    if workspace is None:
        pytest.skip("the test database has no workspace")
    project = f"p_rs_{uuid.uuid4().hex[:8]}"

    def propose_rule(connection, **kwargs):
        ctx = get_llm_context()
        node_id = f"kgn_rs_{uuid.uuid4().hex[:12]}"
        connection.execute(text(
            "INSERT INTO kg_nodes (id, org_id, project_id, kind, node_key, name, body, layer, "
            "trust_tier, attrs) SELECT :i, w.org_id, :p, 'rule', :k, :n, :b, 'inferred', "
            "'inferred', CAST(:a AS JSONB) FROM projects p JOIN workspaces w "
            "ON w.id = p.workspace_id WHERE p.id = :p"),
            {"i": node_id, "p": kwargs["project_id"], "k": f"rule:{node_id}",
             "n": kwargs["title"], "b": kwargs["statement"],
             "a": __import__("json").dumps({"status": "proposed", "provenance": {
                 "scan_id": ctx.get("scan_id"), "step": ctx.get("step"),
                 "workflow_run_id": ctx.get("workflow_run_id")}})})
        return {"node": {"id": node_id}, "status": "proposed"}

    monkeypatch.setattr(rules, "propose_rule", propose_rule)
    monkeypatch.setattr(rule_scan, "units_in_scope", lambda *a, **k: [_unit(1), _unit(2)])
    monkeypatch.setattr(rule_scan, "_scan_agent",
                        lambda *a, **k: (RecordingEngine(Log()), "scripted"))
    monkeypatch.setattr(rule_scan, "Governor", lambda *a, **k: RecordingGovernor(Log()))
    monkeypatch.setattr(rule_scan, "repo_reader", lambda *a, **k: _reader)
    entry = next(e for e in CATALOGUE if e["key"] == "business_rules_scan")
    spec = entry_spec(entry)
    with eng.connect() as c:
        everything = c.begin()     # rolled back: the test leaves nothing behind
        c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) "
                       "VALUES (:i, :w, 'rule scan test', :i)"), {"i": project, "w": workspace})
        try:
            _definition, version = _publish(c, project, f"rs_{uuid.uuid4().hex[:6]}",
                                            spec["steps"], spec["entry_step"])
            run = _start(c, project, version["id"], {"scope": SCOPE})
            for _ in range(4):
                runner.tick(c, Ctx(eng), project_id=project)
            steps = _steps(c, run["id"])
            assert steps["scan"]["status"] == "succeeded", steps["scan"]["last_error"]
            out = steps["scan"]["output"]
            assert out["rules_proposed"] == 2 and out["stopped_by"] == "scope_exhausted"
            assert steps["review"]["status"] == "waiting"
            decision = dict(c.execute(text(
                "SELECT d.* FROM decisions d JOIN workflow_step_runs s ON s.decision_id = d.id "
                "WHERE s.workflow_run_id = :r AND s.step_id = 'review'"),
                {"r": run["id"]}).mappings().one())
            rec = decision["recommendation"]
            assert rec["action"] == rule_scan.REVIEW_ACTION
            assert rec["scan_id"] == out["scan_id"] and rec["proposed_rules"] == 2
            assert rec["link"]["view"] == "rules" and rec["link"]["scan_id"] == out["scan_id"]
            assert [a["label"] for a in decision_actions.allowed_actions(decision)] == [
                "Finish the review", "Stop the run", "Hand to someone else"]
            # One rule answered: the gate stays open for the other.
            first = c.execute(text("SELECT id FROM kg_nodes WHERE project_id = :p AND "
                                   "kind = 'rule' ORDER BY id LIMIT 1"), {"p": project}).scalar()
            approve = text("UPDATE kg_nodes SET attrs = attrs || '{\"status\": \"approved\"}' "
                           "WHERE project_id = :p AND kind = 'rule' AND (:i = '' OR id = :i)")
            c.execute(approve, {"p": project, "i": first})
            assert rule_scan.settle_review_gate(c, None, project, out["scan_id"]) is None
            runner.tick(c, Ctx(eng), project_id=project)
            assert _steps(c, run["id"])["review"]["status"] == "waiting"
            # The last one answered: the runner ends the review by itself.
            c.execute(approve, {"p": project, "i": ""})
            for _ in range(3):
                runner.tick(c, Ctx(eng), project_id=project)
            answered = c.execute(text("SELECT status, approved_by FROM decisions WHERE id = :d"),
                                 {"d": decision["id"]}).one()
            assert tuple(answered) == ("approved", "auto_approval")
            steps = _steps(c, run["id"])
            assert steps["review"]["status"] == "succeeded"
            status = c.execute(text("SELECT status FROM workflow_runs WHERE id = :r"),
                               {"r": run["id"]}).scalar()
            assert status == "completed"
        finally:
            everything.rollback()


def test_a_scan_writes_real_proposed_rules_through_the_rules_layer(pg_engine, monkeypatch):
    """No fake at the writer: rules.propose_rule (S2a) takes the scan's evidence and context."""
    pytest.importorskip("tree_sitter")
    from sqlalchemy import text

    from boss_engine.services import llm_telemetry
    from boss_engine.services.knowledge.code_index import index_files
    from boss_engine.services.knowledge.store import PostgresKnowledgeStore

    monkeypatch.setattr(llm_telemetry, "record_llm_call", lambda *a, **k: None)
    monkeypatch.setattr(llm_telemetry, "record_agent_turns", lambda *a, **k: 0)
    suffix = uuid.uuid4().hex[:8]
    project = f"proj_rr_{suffix}"
    with pg_engine.connect() as conn:
        transaction = conn.begin()
        try:
            conn.execute(text("INSERT INTO orgs (id, name) VALUES (:o, 'RR')"), {"o": f"org_{suffix}"})
            conn.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES (:w, :o, 'RR')"),
                         {"w": f"ws_{suffix}", "o": f"org_{suffix}"})
            conn.execute(text("INSERT INTO projects (id, workspace_id, name, slug) "
                              "VALUES (:p, :w, 'RR', :s)"),
                         {"p": project, "w": f"ws_{suffix}", "s": f"rr-{suffix}"})
            index_files(conn, PostgresKnowledgeStore(), project_id=project,
                        files=[{"path": "money/ledger.py", "language": "python",
                                "source": LEDGER}])
            log = Log()
            report = rule_scan.scan(
                conn, None, project_id=project, scope=["area::money"], scan_id="rscan_real",
                agent_role="chief_architect", agent_name="Scout", workflow_run_id="wfr_real",
                step_id="scan", agent=RecordingEngine(log), engine="scripted",
                reader=lambda path, s, e, repo: {"content": "\n".join(
                    LEDGER.splitlines()[s - 1:e]), "start": s, "end": e},
                governor=RecordingGovernor(log))
            assert report["rules_proposed"] >= 2, report["set_aside"]
            rows = conn.execute(text(
                "SELECT n.id, n.trust_tier, n.attrs FROM kg_nodes n WHERE n.project_id = :p "
                "AND n.kind = 'rule'"), {"p": project}).mappings().all()
            assert len(rows) == report["rules_proposed"]
            for row in rows:
                attrs = row["attrs"]
                assert row["trust_tier"] == "inferred" and attrs["status"] == "proposed"
                assert attrs["scan_id"] == "rscan_real"
                prov = attrs["provenance"]
                assert (prov["agent_role"], prov["agent_name"], prov["workflow_run_id"],
                        prov["step"], prov["model_id"]) == (
                    "chief_architect", "Scout", "wfr_real", "scan", "scripted-v1")
                cited = conn.execute(text(
                    "SELECT attrs FROM kg_edges WHERE src_id = :r AND kind = 'evidenced_by'"),
                    {"r": row["id"]}).scalars().all()
                assert cited and cited[0]["excerpt"], "the evidence carries BOSS's excerpt"
            assert rule_scan.open_proposals(conn, project, "rscan_real") == len(rows)
        finally:
            transaction.rollback()


# ── the scope forms people and agents actually use (brec_40c192798f564212a02d) ──────────────
#
# Two credit scans deadlettered (wfr_5af8917f…, wfr_ab044938…): the agent's graph tools name a
# package module:<repo>:<package>, a person names code by its path, and parse_scope accepted
# only area:/pkg:/symbol:/file:. Both forms are accepted now, resolved against the graph, and
# the whole scope is checked when the run STARTS, before anything is queued or spent.

def test_module_keys_and_plain_paths_are_accepted_for_resolution():
    scope = rule_scan.parse_scope([f"module:{REPO}:{PKG}",
                                   "core/src/main/java/com/example/loans/",
                                   "./core/src/main/java/com/example/Ledger.java"])
    assert scope.repo == REPO and scope.modules == (PKG,)
    assert scope.paths == ("core/src/main/java/com/example/loans",
                           "core/src/main/java/com/example/Ledger.java")
    with pytest.raises(rule_scan.RuleScanRefused, match="module:<repo>:<package>"):
        rule_scan.parse_scope(["module:"])
    with pytest.raises(rule_scan.RuleScanRefused) as refused:
        rule_scan.parse_scope(["what? this"])
    assert "plain repository path" in str(refused.value)


def test_test_code_is_read_only_when_asked():
    assert rule_scan.is_test_path("core/src/test/java/C1Test.java")
    assert not rule_scan.is_test_path("core/src/main/java/C1.java")
    test_unit = _unit(1)
    test_unit.path = "core/src/test/java/C1Test.java"
    assert not rule_scan._readable(test_unit)
    assert rule_scan._readable(test_unit, exclude_tests=False)
    assert rule_scan._excludes_tests(None) and rule_scan._excludes_tests("")
    assert not rule_scan._excludes_tests(False) and not rule_scan._excludes_tests("false")


def test_the_catalogue_entry_declares_every_scan_input():
    from boss_engine.services.workflow_catalogue import CATALOGUE, input_schema_for

    entry = next(e for e in CATALOGUE if e["key"] == "business_rules_scan")
    props = input_schema_for(entry)["properties"]
    assert {"scope", "max_files", "max_rules", "budget_usd", "exclude_tests"} <= set(props)
    assert props["exclude_tests"]["type"] == "boolean" and props["exclude_tests"]["default"] is True
    assert "module:" in props["scope"]["description"]
    assert "exclude_tests" in CAPABILITIES["scan_business_rules"].optional_inputs


LOANS_TEST = '''
class LoansTest:
    def test_disburse(self):
        assert True
'''


def test_scope_forms_resolve_on_a_real_graph_and_are_checked_at_start(pg_engine):
    pytest.importorskip("tree_sitter")
    from sqlalchemy import text

    from boss_engine.services import workflows
    from boss_engine.services.knowledge import view_rollup
    from boss_engine.services.knowledge.code_index import index_files
    from boss_engine.services.knowledge.store import PostgresKnowledgeStore

    suffix = uuid.uuid4().hex[:8]
    project = f"proj_rsf_{suffix}"
    with pg_engine.connect() as conn:
        transaction = conn.begin()
        try:
            conn.execute(text("INSERT INTO orgs (id, name) VALUES (:o, 'RSF')"),
                         {"o": f"org_rsf_{suffix}"})
            conn.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES (:w, :o, 'RSF')"),
                         {"w": f"ws_rsf_{suffix}", "o": f"org_rsf_{suffix}"})
            conn.execute(text("INSERT INTO projects (id, workspace_id, name, slug) "
                              "VALUES (:p, :w, 'RSF', :s)"),
                         {"p": project, "w": f"ws_rsf_{suffix}", "s": f"rsf-{suffix}"})
            index_files(conn, PostgresKnowledgeStore(), project_id=project,
                        files=[{"path": "money/ledger.py", "language": "python",
                                "source": LEDGER},
                               {"path": "money/tests/test_loans.py", "language": "python",
                                "source": LOANS_TEST}])
            view_rollup.rebuild(conn, project)
            pkg = conn.execute(text(
                "SELECT key, package FROM kg_view_tree WHERE project_id = :p AND level = 'package' "
                "AND package NOT LIKE '%tests%' ORDER BY key LIMIT 1"), {"p": project}).mappings().one()

            # module:<package> (as kg_schema / kg_cypher show it) → the tree's package key
            by_module = rule_scan.resolve_ids(
                conn, project, rule_scan.parse_scope([f"module:{pkg['package']}"]))
            assert pkg["key"] in by_module.keys and not by_module.modules
            # a folder → the packages of the classes under it; a file → its file key
            by_folder = rule_scan.resolve_ids(conn, project, rule_scan.parse_scope(["money/"]))
            assert by_folder.packages and not by_folder.paths
            by_file = rule_scan.resolve_ids(conn, project,
                                            rule_scan.parse_scope(["money/ledger.py"]))
            assert by_file.files == ("money/ledger.py",)
            # nothing indexed there → refused in words, before any step
            with pytest.raises(rule_scan.RuleScanRefused, match="not a file or folder"):
                rule_scan.resolve_ids(conn, project, rule_scan.parse_scope(["nowhere/at/all"]))
            with pytest.raises(rule_scan.RuleScanRefused, match="names no package"):
                rule_scan.resolve_ids(conn, project, rule_scan.parse_scope(["module:no.such"]))

            # THE START CHECK: a spec with a scan step gets its scope resolved, or is refused
            spec = {"steps": [{"id": "scan", "binding": {"capability": "scan_business_rules"}}]}
            checked = workflows.checked_capability_inputs(
                conn, project, spec, {"scope": ["money/ledger.py"], "directive": "d"})
            assert checked["scope"] == [f"file:money/ledger.py"]
            with pytest.raises(workflows.WorkflowValidationError, match="the scan cannot start"):
                workflows.checked_capability_inputs(conn, project, spec,
                                                    {"scope": ["nowhere/at/all"]})
            # test code only when asked
            only_tests = ["money/tests/test_loans.py"]
            with pytest.raises(rule_scan.RuleScanRefused, match="outside its tests|no code"):
                rule_scan.validate_scope(conn, project, only_tests)
            assert rule_scan.validate_scope(conn, project, only_tests, exclude_tests=False)
            # a spec with no scan step is left alone
            assert workflows.checked_capability_inputs(conn, project, {"steps": []},
                                                       {"scope": ["anything"]}) == {
                "scope": ["anything"]}
        finally:
            transaction.rollback()


def test_describe_workflow_shows_the_scans_optional_inputs():
    """describe_workflow (workflow_explain) listed only required inputs, so an agent never saw
    the limits it could set. Each is listed, as optional, from the capability's palette."""
    from boss_engine.services import workflow_capabilities, workflow_explain

    scan = next(c for c in workflow_capabilities.palette() if c["key"] == "scan_business_rules")
    declared = workflow_explain.step_inputs(scan, {})
    assert ("scope", True) in declared
    assert {("max_files", False), ("max_rules", False), ("budget_usd", False),
            ("exclude_tests", False)} <= set(declared)
    # a custom step's own schema still stands when its capability declares nothing required
    assert workflow_explain.step_inputs({}, {"inputs": [{"name": "url", "required": True}]}) == [
        ("url", True)]
