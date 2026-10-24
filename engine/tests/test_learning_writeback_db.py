"""Learning while executing (BOSSAPP-768, KNOWLEDGE_GRAPH_PRODUCT_SPEC.md §2.3, §4.1-4.4).

Breaks covered: a learning whose provenance comes from the tool's arguments rather than the run;
a repeat that writes a second node instead of counting; a secret, or no evidence, stored anyway;
confidence above what the evidence carries; an item under 0.4 handed to the next agent; a hook
that writes the wrong kind, loses the run/step, or raises into the pipeline; a test failure
whose fix is not linked; a contradicted decision left standing; the live feed missing what a
run just learned; a pack that does not quote the rules in scope and what was learned before, or
gives learned items to the keyword arm; a kg_why answer not logged consulted; Learned before missing an item that is near the task only
in meaning, ranking a one-list match above an item two lists agree on, or failing (or calling the
model) when there is no embedding model, no vector in its space, or a model that does not answer.
"""
from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path

import pytest
from boss_engine.db import ensure_schema
from boss_engine.services.knowledge import learning_context, learning_live, rules
from boss_engine.services.knowledge import learning_writeback as lw
from boss_engine.services.knowledge.code_index import index_files, symbol_key
from boss_engine.services.knowledge.store import PostgresKnowledgeStore
from boss_engine.services.llm_telemetry import clear_llm_context, set_llm_context
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

STORE = PostgresKnowledgeStore()
P = "proj_kgl"
REPO = "acme/bank"
PATH = "loan-core/src/loans/disburse.py"
SOURCE = '''class LoanDisburser:
    def disburse(self, loan, account):
        if account is None:
            raise LinkedAccountRequired("loan.link.to.savings.account.required")
        return loan.amount

    def charge(self, loan):
        return loan.fee
'''
DISBURSE = symbol_key(REPO, PATH, "LoanDisburser.disburse")
FIXTURES = Path(__file__).resolve().parents[2] / "apps" / "web" / "tests" / "fixtures"


def _database_url() -> str:
    return os.environ.get("BOSS_KG_TEST_DATABASE_URL") or os.environ.get("DATABASE_URL") or ""


@pytest.fixture(scope="module")
def pg_engine():
    url = _database_url()
    name = url.rsplit("/", 1)[-1].split("?", 1)[0].lower()
    if not url or not any(marker in name for marker in ("test", "testing", "ci", "scratch", "tmp")):
        pytest.skip("BOSS_KG_TEST_DATABASE_URL must name a disposable database")
    engine = create_engine(url, future=True)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        pytest.skip(f"disposable postgres is not reachable: {exc.__class__.__name__}")
    ensure_schema(engine)
    return engine


@pytest.fixture()
def connection(pg_engine):
    with pg_engine.connect() as conn:
        transaction = conn.begin()
        try:
            conn.execute(text("INSERT INTO orgs (id, name) VALUES ('org_kgl', 'KGL')"))
            conn.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES ('ws_kgl', 'org_kgl', 'KGL')"))
            conn.execute(text(f"INSERT INTO projects (id, workspace_id, name, slug) VALUES ('{P}', 'ws_kgl', 'KGL', 'kgl')"))
            conn.execute(text(
                "INSERT INTO roles (id, project_id, role_key, display_name, spec) VALUES "
                f"('role_kgl_be', '{P}', 'backend_engineer', 'Backend Engineer', "
                "CAST('{\"persona\": {\"callsign\": \"Builder\"}}' AS JSONB)), "
                f"('role_kgl_sre', '{P}', 'sre', 'SRE', CAST('{{\"persona\": {{\"callsign\": \"Responder\"}}}}' AS JSONB))"))
            index_files(conn, STORE, project_id=P, repo=REPO,
                        files=[{"path": PATH, "language": "python", "source": SOURCE}])
            set_llm_context(role_key="backend_engineer", run_id="crun_build_a", stage="implement",
                            model_id="scripted-v1", project_id=P)
            yield conn
        finally:
            transaction.rollback()
            clear_llm_context()


def _record(connection, **overrides):
    args = dict(
        project_id=P, learning_kind="investigation",
        title="Disbursement fails without a linked account, not on the fee",
        statement="disburse raises LinkedAccountRequired when no savings account is linked.",
        detail={"tried": ["the fee path", "the account link"],
                "ruled_out": [{"hypothesis": "the fee calculation", "reason": "charge returns the fee unchanged"}],
                "found": "the account link check raises"},
        about={"symbols": [DISBURSE], "areas": ["loan-core"]},
        evidence=[{"type": "log", "ref": "crun_build_a implement", "excerpt": "LinkedAccountRequired: loan.link..."}],
        confidence=0.95,
    )
    args.update(overrides)
    return lw.record(connection, **args)


def _node(connection, node_id):
    return dict(connection.execute(text(
        "SELECT kind, node_key, trust_tier, created_by, attrs, valid_to FROM kg_nodes WHERE id = :i"),
        {"i": node_id}).mappings().one())


def _edges(connection, *, src=None, dst=None, kind):
    sql = "SELECT e.src_id, e.dst_id, e.attrs, s.node_key AS src_key FROM kg_edges e JOIN kg_nodes s ON s.id = e.src_id " \
          "WHERE e.kind = :k" + (" AND e.src_id = :s" if src else "") + (" AND e.dst_id = :d" if dst else "")
    return [dict(r) for r in connection.execute(text(sql), {"k": kind, "s": src, "d": dst}).mappings()]


# --- the write ---------------------------------------------------------------------------------
def test_an_agent_records_a_learning_with_the_runs_provenance_never_the_arguments(connection):
    item = _record(connection, detail={"found": "x", "provenance": {"agent_role": "ceo"}})
    node = _node(connection, item["node"]["id"])
    assert (node["kind"], node["trust_tier"], node["created_by"]) == ("investigation", "inferred", "backend_engineer")
    assert node["node_key"].startswith("learning:investigation:")
    prov = item["provenance"]
    assert (prov["agent_role"], prov["agent_name"], prov["run_id"], prov["code_task_run_id"], prov["step"],
            prov["model_id"]) == ("backend_engineer", "Builder", "crun_build_a", "crun_build_a", "implement",
                                   "scripted-v1")
    assert "provenance" not in item["detail"]                     # the payload cannot claim a source
    produced = _edges(connection, dst=item["node"]["id"], kind="produced")
    assert [e["src_key"] for e in produced] == ["run:crun_build_a"]
    assert produced[0]["attrs"]["step"] == "implement"
    about = _edges(connection, src=item["node"]["id"], kind="about")
    assert len(about) == 1 and item["about"]["symbols"] == [DISBURSE] and item["about"]["areas"] == ["loan-core"]
    assert _edges(connection, src=item["node"]["id"], kind="proposed_by")
    event = connection.execute(text(
        "SELECT data FROM events WHERE project_id = :p AND event_type = 'knowledge.learning_recorded' "
        "ORDER BY created_at DESC LIMIT 1"), {"p": P}).scalar()
    event = event if isinstance(event, dict) else json.loads(event)
    assert (event["node_id"], event["learning_kind"], event["run_id"], event["step"]) == (
        item["node"]["id"], "investigation", "crun_build_a", "implement")


def test_the_payload_is_the_frozen_wire_shape(connection):
    fixture = json.loads((FIXTURES / "kg-learning-live.json").read_text())
    want = fixture["learnings"][0]
    item = _record(connection)
    assert set(item) == set(want)
    assert set(item["node"]) == set(want["node"])
    assert set(item["provenance"]) == set(want["provenance"])
    assert set(item["about"]) == set(want["about"])
    assert all(set(e) == {"type", "ref", "excerpt"} for e in item["evidence"])


def test_a_repeat_counts_and_adds_evidence_instead_of_a_second_node(connection):
    first = _record(connection, learning_kind="test_failure", title="disburse test fails",
                    statement="test_disburse fails: LinkedAccountRequired",
                    detail={"test": "test_disburse", "error_signature": "LinkedAccountRequired at line 4 (run 1)"},
                    evidence=[{"type": "test", "ref": "pytest -k disburse", "excerpt": "FAILED line 4"}])
    set_llm_context(run_id="crun_build_b", stage="implement-repair1")
    again = _record(connection, learning_kind="test_failure", title="disburse test fails again",
                    statement="it failed again",
                    detail={"test": "test_disburse", "error_signature": "LinkedAccountRequired at line 9 (run 2)"},
                    evidence=[{"type": "test", "ref": "pytest -k disburse", "excerpt": "FAILED line 9"}])
    assert again["node"]["id"] == first["node"]["id"]
    assert again["seen_count"] == 2 and again["last_seen_run"] == "crun_build_b"
    assert len(again["evidence"]) == 2
    assert sorted(e["src_key"] for e in _edges(connection, dst=first["node"]["id"], kind="produced")) == [
        "run:crun_build_a", "run:crun_build_b"]
    assert connection.execute(text(
        "SELECT count(*) FROM kg_nodes WHERE project_id = :p AND kind = 'test_failure'"), {"p": P}).scalar() == 1


def test_a_secret_or_no_evidence_is_refused_and_nothing_is_stored(connection):
    with pytest.raises(lw.LearningRefused, match="secret"):
        _record(connection, statement="the deploy works with token ghp_" + "a" * 36)
    with pytest.raises(lw.LearningRefused, match="evidence"):
        _record(connection, evidence=[])
    with pytest.raises(lw.LearningRefused):
        _record(connection, detail={"tried": ["x"]})                 # an investigation that found nothing
    assert connection.execute(text(
        "SELECT count(*) FROM kg_nodes WHERE project_id = :p AND node_key LIKE 'learning:%'"), {"p": P}).scalar() == 0


def test_confidence_is_capped_by_the_evidence(connection):
    log_only = _record(connection, confidence=0.99)
    assert log_only["confidence"] == lw.CAP_LOG_ONLY
    passing = _record(connection, learning_kind="fix", title="Keep the link check", statement="fixed it",
                      detail={"change_summary": "kept the link check", "files": [PATH]},
                      evidence=[{"type": "test", "ref": "pytest", "excerpt": "1 passed", "passed": True}],
                      confidence=1.0)
    assert passing["confidence"] == lw.CAP_PASSING_TEST
    diff = _record(connection, learning_kind="fact", title="A fact", statement="the fee is flat",
                   detail={}, evidence=[{"type": "diff", "ref": "abc123"}], confidence=1.0)
    assert diff["confidence"] == lw.CAP_OTHER


def test_an_item_below_the_injection_floor_is_stored_but_never_given_to_the_next_agent(connection):
    weak = _record(connection, confidence=0.2)
    strong = _record(connection, learning_kind="fact", title="Linked savings account required",
                     statement="disburse needs a linked savings account", detail={}, confidence=0.5)
    assert _node(connection, weak["node"]["id"])["attrs"]["injectable"] is False
    found = learning_context.learned_before(connection, P, query="LoanDisburser.disburse linked account",
                                            seeds=learning_context.code_seeds(connection, P, "LoanDisburser.disburse"))
    ids = [i["id"] for i in found]
    assert strong["node"]["id"] in ids and weak["node"]["id"] not in ids


class _Embedder:
    """A scripted embedding model: each text's vector is looked up by its first word."""
    model_id = "scripted:emb:3"

    def __init__(self, vectors, fail=False):
        self.vectors, self.fail, self.calls = vectors, fail, 0

    def embed(self, texts):
        self.calls += 1
        if self.fail:
            raise RuntimeError("budget refused")
        return [self.vectors[t.split()[0].lower()] for t in texts]


def _embed(connection, node_id, vector, model_id=_Embedder.model_id):
    content_hash = connection.execute(text("SELECT content_hash FROM kg_nodes WHERE id = :i"),
                                      {"i": node_id}).scalar_one()
    STORE.upsert_embedding(connection, project_id=P, node_id=node_id, model_id=model_id,
                           embedding=vector, content_hash=content_hash)


def _three_learnings(connection):
    """account: the default investigation; month: matches "month end" by words only; payout: by
    words and by meaning. Vectors: the query is (1, 0, 0)."""
    account = _record(connection)["node"]["id"]
    month = _record(connection, learning_kind="fact", title="Interest posts at month end",
                    statement="Interest accrual posts on the last day of the month.", detail={},
                    confidence=0.8)["node"]["id"]
    payout = _record(connection, learning_kind="root_cause", title="Payout refused at month end",
                     statement="The payout was refused because the month end batch held the account.",
                     detail={}, confidence=0.8)["node"]["id"]
    _embed(connection, account, [0.9, 0.1, 0.0])
    _embed(connection, month, [0.0, 0.0, 1.0])
    _embed(connection, payout, [0.8, 0.0, 0.2])
    return account, month, payout


def test_learned_before_finds_an_item_near_the_task_only_in_meaning(connection):
    account, month, _payout = _three_learnings(connection)
    query = "Money transfer refused because nothing attached"          # no word of `account`
    model = _Embedder({"money": [1.0, 0.0, 0.0]})
    found = [i["id"] for i in learning_context.learned_before(connection, P, query=query, embedder=model)]
    assert account in found and month not in found and model.calls == 1
    # No embedding model set for the org: the words alone, and no error.
    words_only = [i["id"] for i in learning_context.learned_before(connection, P, query=query)]
    assert account not in words_only


def test_learned_before_fuses_words_meaning_usefulness_and_recency_by_reciprocal_rank(connection):
    account, month, payout = _three_learnings(connection)
    model = _Embedder({"payout": [1.0, 0.0, 0.0]})
    found = learning_context.learned_before(connection, P, query="Payout month end", embedder=model)
    ids = [i["id"] for i in found]
    assert ids[0] == payout                     # words and meaning agree on it
    assert set(ids) == {account, month, payout}
    assert found[0]["score"] > found[1]["score"] > 0


def test_learned_before_falls_back_to_words_when_the_model_fails_or_has_no_vectors(connection):
    _account, month, payout = _three_learnings(connection)
    query = "Payout month end"
    words = [i["id"] for i in learning_context.learned_before(connection, P, query=query)]
    assert set(words) == {month, payout}
    failing = _Embedder({}, fail=True)
    assert [i["id"] for i in learning_context.learned_before(connection, P, query=query, embedder=failing)] == words
    other_space = _Embedder({"payout": [1.0, 0.0, 0.0]})
    other_space.model_id = "scripted:other:3"           # no learned item has a vector in its space
    assert [i["id"] for i in learning_context.learned_before(connection, P, query=query,
                                                              embedder=other_space)] == words
    assert other_space.calls == 0                        # and the model is not called for nothing


class _Ctx:
    def __init__(self, connection):
        self.connection = connection

    @contextmanager
    def begin(self):
        yield self.connection


def test_what_kg_why_returns_to_a_run_is_logged_consulted_with_its_edge(connection):
    from boss_engine.tools.builtin import build_default_registry

    item = _record(connection)["node"]["id"]
    symbol = STORE.find_by_key(connection, project_id=P, kind="symbol", node_key=DISBURSE)["id"]
    set_llm_context(role_key="backend_engineer", run_id="crun_build_b", stage="implement", project_id=P,
                    knowledge_seeds=[symbol])           # the build's own code: in its scope
    out = build_default_registry().get("kg_why").handler(_Ctx(connection), project_id=P, actor="backend_engineer",
                                                         args={"node_id": symbol})
    assert "why" in out, out
    assert item in [hit["node_id"] for hit in out["why"]]
    used = connection.execute(text(
        "SELECT use_kind, agent_role FROM kg_usage WHERE project_id = :p AND run_id = 'crun_build_b' AND fact_id = :f"),
        {"p": P, "f": item}).all()
    assert [tuple(u) for u in used] == [("consulted", "backend_engineer")]
    assert "run:crun_build_b" in [e["src_key"] for e in _edges(connection, dst=item, kind="consulted")]


def test_a_decision_contradicting_an_open_one_on_the_same_question_closes_it(connection):
    first = _record(connection, learning_kind="decision", title="Retry or rebuild: retry",
                    statement="retry the step", detail={"question": "Retry or rebuild the step?", "chosen": "retry",
                                                        "reasons": ["the work is saved"]})
    second = _record(connection, learning_kind="decision", title="Retry or rebuild: rebuild",
                     statement="rebuild", detail={"question": "Retry or rebuild the step?", "chosen": "rebuild",
                                                  "reasons": ["the branch is gone"]})
    assert first["node"]["id"] != second["node"]["id"]
    assert _node(connection, first["node"]["id"])["valid_to"] is not None
    assert _edges(connection, src=second["node"]["id"], dst=first["node"]["id"], kind="supersedes")
    named = _record(connection, learning_kind="fact", title="Fee is per loan", statement="fee per loan", detail={},
                    supersedes=second["node"]["id"])
    assert _node(connection, second["node"]["id"])["valid_to"] is not None and named


# --- the hooks ---------------------------------------------------------------------------------
def test_a_failed_gate_records_a_test_failure_and_the_pass_after_it_a_linked_fix(connection):
    out = lw.observe(connection, project_id=P, hook="repair_checkpoint", run_id="crun_build_a", attempt=0,
                     passed=False, tests_passed=False,
                     output="FAILED tests/test_disburse.py::test_no_account\nLinkedAccountRequired: loan.link",
                     command="pytest -q", failing=["tests/test_disburse.py::test_no_account"], files=[PATH])
    assert [i["learning_kind"] for i in out] == ["test_failure"]
    failure = out[0]
    assert failure["detail"]["test"] == "tests/test_disburse.py::test_no_account"
    assert "LinkedAccountRequired" in failure["detail"]["error_signature"]
    assert failure["provenance"]["step"] == "implement" and failure["provenance"]["run_id"] == "crun_build_a"
    assert failure["confidence"] == lw.CAP_PASSING_TEST              # BOSS ran the gate itself
    # A gate that failed with the tests green (an edited test) is no test failure.
    assert lw.observe(connection, project_id=P, hook="repair_checkpoint", run_id="crun_build_a", attempt=1,
                      passed=False, tests_passed=True, output="ok", command="pytest -q", files=[PATH]) == []
    fixed = lw.observe(connection, project_id=P, hook="repair_checkpoint", run_id="crun_build_a", attempt=1,
                       passed=True, tests_passed=True, output="", command="pytest -q", files=[PATH],
                       summary="Restored the linked-account check before charging.")
    assert [i["learning_kind"] for i in fixed] == ["fix"]
    fix = fixed[0]
    assert fix["provenance"]["step"] == "implement-repair1"
    assert fix["detail"]["files"] == [PATH]
    assert _edges(connection, src=failure["node"]["id"], dst=fix["node"]["id"], kind="fixed_by")
    assert _edges(connection, src=fix["node"]["id"], kind="about")       # about the file it changed
    # The next pass of the same run has nothing open to fix.
    assert lw.observe(connection, project_id=P, hook="repair_checkpoint", run_id="crun_build_a", attempt=2,
                      passed=True, tests_passed=True, output="", command="pytest -q", files=[PATH]) == []


def test_a_command_that_failed_then_worked_differently_in_the_same_stage_is_a_workaround(connection):
    set_llm_context(db_engine=connection, run_id="crun_build_a", stage="implement", project_id=P)
    lw._TURNS.close_stage("crun_build_a")
    turns = [
        {"direction": "tool_call", "tool_name": "Bash", "content": {"command": "cd app && npm run build"}},
        {"direction": "tool_result", "tool_name": "Bash", "ok": False,
         "content": "Error: Cannot find module 'vite'\n    at Module._resolveFilename"},
        {"direction": "tool_call", "tool_name": "Bash", "content": {"command": "ls"}},
        {"direction": "tool_result", "tool_name": "Bash", "ok": True, "content": "src"},
        {"direction": "tool_call", "tool_name": "Bash", "content": {"command": "npm run build --workspace app"}},
        {"direction": "tool_result", "tool_name": "Bash", "ok": True, "content": "built"},
    ]
    for turn in turns:
        lw.observe_turn(turn)
    rows = connection.execute(text(
        "SELECT attrs FROM kg_nodes WHERE project_id = :p AND kind = 'workaround'"), {"p": P}).scalars().all()
    assert len(rows) == 1
    attrs = rows[0]
    assert "npm run build" in attrs["detail"]["problem"] and "--workspace app" in attrs["detail"]["workaround"]
    assert attrs["provenance"]["step"] == "implement" and attrs["confidence"] == lw.CAP_LOG_ONLY
    # The same command failing and then passing unchanged (a code fix) is not a workaround, and a
    # stage boundary forgets the window.
    for turn in ({"direction": "tool_call", "tool_name": "Bash", "content": {"command": "pytest -q"}},
                 {"direction": "tool_result", "tool_name": "Bash", "ok": False, "content": "1 failed"}):
        lw.observe_turn(turn)
    lw.observe(None, project_id=P, hook="build_stage", run_id="crun_build_a", where="review_code")
    for turn in ({"direction": "tool_call", "tool_name": "Bash", "content": {"command": "pytest -q -x"}},
                 {"direction": "tool_result", "tool_name": "Bash", "ok": True, "content": "passed"}):
        lw.observe_turn(turn)
    assert connection.execute(text(
        "SELECT count(*) FROM kg_nodes WHERE project_id = :p AND kind = 'workaround'"), {"p": P}).scalar() == 1


def _step(output, **extra):
    return {"id": "wsr_1", "workflow_run_id": "wfr_inc_1", "step_id": extra.pop("step_id", "investigate"),
            "output": output, **extra}


def test_step_completion_records_the_steps_typed_outputs_and_its_learned_list(connection):
    clear_llm_context()
    inv = lw.write_back(connection, project_id=P, hook="step_complete", observed={
        "step": _step({"title": "Fix orders: KeyError", "root_cause": "Release abc1234 went out 3 min before; KeyError at disburse.py",
                       "hypothesis": {"basis": "release_before_onset", "confidence": 0.6, "suspect_commit": "abc1234",
                                      "suspect_file": PATH, "exception": "KeyError"},
                       "logs_word": "Read 40 lines", "diff_word": "2 files changed"}),
        "template": {"id": "investigate", "captain": "sre", "binding": {"capability": "investigate_incident"}}})
    kinds = sorted(i["learning_kind"] for i in inv)
    assert kinds == ["investigation", "root_cause"]
    investigation = next(i for i in inv if i["learning_kind"] == "investigation")
    assert investigation["detail"]["ruled_out"] and investigation["detail"]["found"].startswith("Release abc1234")
    assert (investigation["provenance"]["agent_role"], investigation["provenance"]["agent_name"],
            investigation["provenance"]["workflow_run_id"], investigation["provenance"]["step"]) == (
        "sre", "Responder", "wfr_inc_1", "investigate")
    decided = lw.write_back(connection, project_id=P, hook="step_complete", observed={
        "step": _step({"kind": "typed_decision", "answer": "rollback", "confidence": 0.8,
                       "reason": "the release changed the failing frame",
                       "learned": [{"learning_kind": "fact", "title": "Orders depend on the fee field",
                                    "statement": "orders read loan.fee", "evidence": [{"type": "file", "ref": PATH}],
                                    "confidence": 0.7}]}, step_id="decide"),
        "template": {"id": "decide", "captain": "sre",
                     "binding": {"typed_decision": {"question": "Roll back or fix forward?",
                                                    "options": {"rollback": "", "fix_forward": ""}}}}})
    assert sorted(i["learning_kind"] for i in decided) == ["decision", "fact"]
    decision = next(i for i in decided if i["learning_kind"] == "decision")
    assert decision["detail"]["chosen"] == "rollback" and decision["detail"]["alternatives"] == ["fix_forward"]
    report = lw.write_back(connection, project_id=P, hook="step_complete", observed={
        "step": _step({"report_markdown": "# Incident"}, step_id="report"),
        "template": {"id": "report", "captain": "sre", "binding": {"capability": "incident_report"}},
        "context": {"title": "Orders fail with KeyError", "root_cause": "the release dropped the fee key",
                    "pull_request_url": "https://github.com/acme/bank/pull/9", "recovery_summary": "errors back to 0.1%"}})
    assert [i["learning_kind"] for i in report] == ["precedent"]
    assert report[0]["detail"]["symptom"] == "Orders fail with KeyError"


def test_a_domain_agents_decision_is_recorded_only_with_what_it_relied_on(connection):
    from boss_engine.services import agent_decision
    from boss_engine.services.agent_service import AgentService

    assert lw.write_back(connection, project_id=P, hook="agent_decide", observed={
        "chosen": "page_oncall", "reason": "errors rising", "evidence": []}) == []
    out = lw.write_back(connection, project_id=P, hook="agent_decide", observed={
        "chosen": "page_oncall", "reason": "error rate 12% on /orders for 10 minutes", "confidence": 0.8,
        "evidence": ["error_rate=0.12 on /orders"], "question": "Triage alert alr_1", "external_ref": "alert:alr_1",
        "alternatives": ["page_oncall", "dismiss"], "agent_role": "sre"})
    assert [i["learning_kind"] for i in out] == ["decision"]
    assert out[0]["provenance"]["agent_role"] == "sre" and out[0]["detail"]["alternatives"] == ["dismiss"]
    # The pipeline's decide hands its decision to the hook (one line at the call site).
    import inspect

    assert 'hook="agent_decide"' in inspect.getsource(AgentService.decide)
    assert agent_decision.DECIDED


def test_a_hook_never_raises_into_the_pipeline(connection):
    for hook in sorted(lw.HOOKS):
        assert lw.write_back(connection, project_id=P, hook=hook, observed={"step": "garbage", "template": 7,
                                                                            "evidence": "x"}) == []
    assert lw.write_back(None, project_id=P, hook="step_complete", observed={}) == []
    assert lw.write_back(connection, project_id=P, hook="not_a_hook", observed={}) == []
    assert lw.observe(object(), project_id=P, hook="repair_checkpoint", run_id="crun_x") == []
    lw.observe_turn({"direction": "tool_result", "content": object()})


# --- live, and the pack -------------------------------------------------------------------------
def test_the_live_feed_carries_what_a_run_just_learned_after_the_cursor(connection):
    empty = learning_live.live(connection, P, run="crun_build_a")
    assert empty["learnings"] == [] and empty["poll_after_ms"] == learning_live.POLL_AFTER_MS
    fixture = json.loads((FIXTURES / "kg-learning-live.json").read_text())
    assert set(empty) == set(fixture) - {"_fixture"}
    item = _record(connection)
    feed = learning_live.live(connection, P, since=empty["cursor"], run="crun_build_a")
    assert [i["node"]["id"] for i in feed["learnings"]] == [item["node"]["id"]]
    produced = [e for e in feed["edges"] if e["kind"] == "produced"]
    assert produced and produced[0]["run_id"] == "crun_build_a" and produced[0]["dst"] == item["node"]["id"]
    assert all(set(e) == set(fixture["edges"][0]) for e in feed["edges"])
    other = learning_live.live(connection, P, run="crun_someone_else")
    assert other["learnings"] == []
    hidden = learning_live.live(connection, P, run="crun_build_a", withheld={"investigation"})
    assert hidden["learnings"] == []


def test_the_pack_quotes_rules_in_scope_and_learned_before_and_keyword_mode_gets_no_learnings(connection):
    set_llm_context(role_key="chief_architect", run_id="crun_scan", stage="scan")
    rule = rules.propose_rule(connection, project_id=P, title="Disbursal needs a linked account",
                              statement="A loan is disbursed only to a linked savings account.", category="validation",
                              scope={"symbols": [DISBURSE]},
                              evidence=[{"symbol": DISBURSE, "lines": [3, 4], "excerpt": "if account is None"}],
                              rationale="disburse raises without one", confidence=0.9)
    rules.approve_rule(connection, project_id=P, rule_id=rule["node"]["id"], actor_ref="usr_owner")
    set_llm_context(role_key="backend_engineer", run_id="crun_build_a", stage="implement")
    item = _record(connection, learning_kind="fix", title="Keep the link check before the charge",
                   statement="Keep the linked-account check before the charge in disburse.",
                   detail={"change_summary": "kept the check", "files": [PATH]},
                   evidence=[{"type": "test", "ref": "pytest", "passed": True}], confidence=0.9)
    found = learning_context.sections(connection, P, query="Change LoanDisburser.disburse to add a fee", mode="on")
    assert found["rule_ids"] == [rule["node"]["id"]] and found["learning_ids"] == [item["node"]["id"]]
    assert f"[R:{rule['node']['id']}]" in found["text"] and f"[L:{item['node']['id']}]" in found["text"]
    assert "learned by Builder in crun_build_a" in found["text"]
    keyword = learning_context.sections(connection, P, query="Change LoanDisburser.disburse", mode="keyword")
    assert keyword["rule_ids"] and keyword["learning_ids"] == []
    assert learning_context.sections(connection, P, query="LoanDisburser.disburse", mode="off")["text"] == ""
    # A proposed rule is never injected.
    set_llm_context(role_key="chief_architect", run_id="crun_scan", stage="scan")
    rules.propose_rule(connection, project_id=P, title="Fee is flat", statement="The disbursement fee is flat.",
                       category="calculation", scope={"symbols": [DISBURSE]},
                       evidence=[{"symbol": DISBURSE, "lines": [3, 3]}], rationale="", confidence=0.5)
    again = learning_context.sections(connection, P, query="Change LoanDisburser.disburse", mode="on")
    assert again["rule_ids"] == [rule["node"]["id"]]
    # The turn's pack carries the sections, logged shown (no edge).
    from boss_engine.services.knowledge import runtime

    set_llm_context(role_key="backend_engineer", run_id="crun_build_b", stage="implement")
    text_ = runtime.quoted_pack_for_turn(connection, project_id=P, query="Change LoanDisburser.disburse",
                                         run_id="crun_build_b", step="implement")
    assert f"[L:{item['node']['id']}]" in text_ and "Rules in scope" in text_
    shown = connection.execute(text(
        "SELECT use_kind FROM kg_usage WHERE project_id = :p AND run_id = 'crun_build_b' AND fact_id = :f"),
        {"p": P, "f": item["node"]["id"]}).scalars().all()
    assert shown == ["shown"]
    assert not _edges(connection, dst=item["node"]["id"], kind="consulted")
