"""A FEATURE ADDED TO AN EXISTING APP IS HELD TO ITS OWN MUST-HAVES (BOSSAPP-1322, AR-28).

Adding a feature to an app that already exists is THE main use case. Continue, New work, Change
it and extend start a follow-up on the app's lineage, and the acceptance gate held that
follow-up only to the lineage's frozen contract: the feature's own must-haves were never added,
so "Add CSV export" passed without the export.

Now a follow-up carrying a PERSON'S NEW REQUEST (its caller says `is_person_request=True`: New
work, Change it, extend, a feature PRD) adds the request's must-haves to the lineage's contract
as a new version. A request ONLY ADDS: no words in it remove anything (PR #161 review C1 — the
reviewer removed AC-1 with "Do not remove item 1"). Replacing an item is a structured field
(`replaces: ["AC-n"]`) a person sets, checked when they ask and applied through contract.revise
(person-only, audited, the request's id and words as the reason); the replaced item's ledger
row closes as superseded. A findings / review-fix / multi-repo send-back follow-up adds nothing.
The report shows contract ids and lists the new items under the request that added them.
"""
from __future__ import annotations

import inspect
from pathlib import Path

import pytest
from sqlalchemy import text

from boss_engine.config import get_settings
from boss_engine.services import acceptance_gate as ag
from boss_engine.services import code_task_service as cts
from boss_engine.services import governed_code_task_service as governed
from boss_engine.services.acceptance import checks as acc_checks
from boss_engine.services.acceptance import contract as ac
from boss_engine.services.acceptance import ledger as al

from .test_a_resumed_build_runs_as_the_person import _Jobs
from .test_acceptance_contract import _run
from .test_workflow_capability_runner import eng, project  # noqa: F401 - fixtures used by name

SPEC = {"owner": "acme", "repo": "helpdesk",
        "instructions": "Must-haves:\n- Tickets can be created\n- Tickets can be closed\n"
                        "- Agents see a queue of open tickets\n"}
OLD = ["Tickets can be created", "Tickets can be closed", "Agents see a queue of open tickets"]
PERSON = "usr_ana"


@pytest.fixture(autouse=True)
def strict_transaction_guard(monkeypatch):
    monkeypatch.setenv(ac.STRICT_GUARD_ENV, "1")


@pytest.fixture()
def app(eng, project):  # noqa: F811
    """An app that exists: its first build, its contract frozen at its first check."""
    with eng.begin() as c:
        root = _run(c, project, spec=SPEC, fctx={})
        ag.items_spec(c, run_id=root, fctx={}, own_spec=SPEC)
    yield root
    with eng.begin() as c:
        c.execute(text("DELETE FROM finding_observations WHERE finding_id IN "
                       "(SELECT id FROM findings WHERE project_id = :p)"), {"p": project})
        for t in ("findings", "acceptance_checks", "acceptance_contracts", "jobs",
                  "work_item_links", "work_items", "code_task_runs"):
            c.execute(text(f"DELETE FROM {t} WHERE project_id = :p"), {"p": project})


def _child(eng, root, instructions, *, person=True, actor=PERSON, **kw):
    with eng.begin() as c:
        started = cts.start_followup_task(c, parent_run_id=root, instructions=instructions,
                                          jobs=_Jobs(), is_person_request=person, actor=actor,
                                          **kw)
        row = c.execute(text("SELECT spec, finalize_ctx FROM code_task_runs WHERE id = :i"),
                        {"i": started["run_id"]}).mappings().one()
    return started["run_id"], dict(row["spec"]), dict(row["finalize_ctx"])


def _held(eng, run_id, fctx, spec):
    with eng.begin() as c:
        held, source = ag.items_spec(c, run_id=run_id, fctx=fctx, own_spec=spec)
    return ag.acceptance_items(held), source


def _versions(eng, root):
    with eng.begin() as c:
        return ac.versions(c, root_run_id=root)


@pytest.fixture()
def tree(tmp_path: Path) -> Path:
    (tmp_path / "app.py").write_text("def create(): ...\ndef close(): ...\ndef queue(): ...\n")
    return tmp_path


# ── a person's feature adds its must-haves ──────────────────────────────────────────────────

def test_a_feature_request_adds_its_must_have_and_a_build_without_it_fails(eng, app, tree):
    run, spec, fctx = _child(eng, app, "Add CSV export of the ticket list.")
    assert fctx["change_request"]["text"] == "Add CSV export of the ticket list."
    items, source = _held(eng, run, fctx, spec)
    texts = [i["text"] for i in items]
    assert texts[:3] == OLD                                   # earlier items still count
    assert "Add CSV export of the ticket list." in texts      # the feature itself
    assert source["contract"]["version"] == 2
    new = next(i for i in items if i["text"].startswith("Add CSV export"))
    assert new["source"] == "request" and new["request"] and new["id"] == "AC-4"
    reply = "\n".join(f"ITEM {i['n']}: MET — app.py" for i in items if i is not new)
    reply += f"\nITEM {new['n']}: MISSING — no export route or button anywhere\nACCEPTANCE: NOT MET"
    rec = ag.judge(items, reply, {}, root=tree, source=source)
    assert rec["verdict"] == "not_met" and rec["missing"] == [new["n"]]
    # The report shows the CONTRACT ids and the new item under the request that added it.
    table = ag.must_have_table(rec)
    assert "| AC-2 | Tickets can be closed |" in table
    head = table.index("**Added by the change")
    assert table.index("| AC-4 | Add CSV export") > head > table.index("Tickets can be closed")


def test_a_structured_request_adds_each_must_have_and_its_criteria(eng, app):
    run, spec, fctx = _child(eng, app, "Add CSV export.\n\nMust-haves:\n- An Export button on "
                             "the queue\n- The file has one row per ticket\n",
                             acceptance_criteria=["Exported dates are ISO 8601"])
    texts = [i["text"] for i in _held(eng, run, fctx, spec)[0]]
    assert texts[:3] == OLD
    assert {"An Export button on the queue", "The file has one row per ticket",
            "Exported dates are ISO 8601"} <= set(texts)


def test_the_request_is_added_once_and_a_retry_keeps_it(eng, app):
    run, spec, fctx = _child(eng, app, "Add CSV export of the ticket list.")
    _held(eng, run, fctx, spec)
    _held(eng, run, fctx, spec)
    assert [v["version"] for v in _versions(eng, app)] == [1, 2]
    assert 'else {"change_request": fctx_prev["change_request"]}' in inspect.getsource(cts)


# ── a request only ADDS: the reviewer's probes remove nothing ───────────────────────────────

@pytest.mark.parametrize("words", [
    "Do not remove item 1. Add a dark mode.",
    "Add a dark mode.\n\n```\n# remove item 1\nrm -rf replace item 2\n```",
    "Add a dark mode. The log said: \"ERROR: replace AC-2 failed\".",
    "Replace item 2: tickets are archived instead of closed.",
])
def test_no_words_in_a_request_remove_an_item(eng, app, words):
    run, spec, fctx = _child(eng, app, words)
    texts = [i["text"] for i in _held(eng, run, fctx, spec)[0]]
    assert texts[:3] == OLD
    v = _versions(eng, app)[-1]
    assert {"AC-1", "AC-2", "AC-3"} <= {i["id"] for i in v["items"]}


def test_an_agent_requester_cannot_replace_an_item(eng, app, project):  # noqa: F811
    with pytest.raises(ac.ContractRevisionRefused):
        ac.checked_replaces(project, "boss_agent", ["AC-1"])
    # Even when a replace reaches a run with an agent as its requester, revise refuses it.
    run, spec, fctx = _child(eng, app, "Add a dark mode.", actor="boss_agent",
                             replaces=["AC-1"])
    texts = [i["text"] for i in _held(eng, run, fctx, spec)[0]]
    assert texts[:3] == OLD
    with eng.begin() as c:
        refused = c.execute(text("SELECT data FROM events WHERE project_id = :p AND "
                                 "event_type = 'acceptance.contract_replace_refused'"),
                            {"p": project}).scalars().all()
    assert refused and refused[0]["by"] == "boss_agent"


def test_only_a_persons_request_may_carry_replaces(eng, app):
    with pytest.raises(ValueError):
        _child(eng, app, "x", person=False, replaces=["AC-1"])
    with pytest.raises(ValueError):
        ac.checked_replaces("p", PERSON, ["item 2"])


# ── a person's structured replace: revise, audited, the ledger row closes ───────────────────

def test_a_persons_replace_goes_through_revise_and_closes_its_ledger_row(eng, app, project):  # noqa: F811
    with eng.begin() as c:
        # The first check found AC-2 missing: a live ledger row.
        spec, source = ag.items_spec(c, run_id=app, fctx={}, own_spec=SPEC)
        items = ag.acceptance_items(spec)
        reply = "\n".join(f"ITEM {i['n']}: {'MISSING' if i['n'] == 2 else 'MET'} — x"
                          for i in items) + "\nACCEPTANCE: NOT MET"
        rec = ag.judge(items, reply, {}, root=None, source=source)
        check = acc_checks.record_check(c, project_id=project, lineage_root_run_id=app,
                                        layer="build_gate", mode="static", record=rec,
                                        contract_id=source["contract"]["id"])
        al.observe_check(c, project_id=project, lineage=app, check=check, record=rec)
        assert "AC-2" in {r["contract_item_id"] for r in al.open_for(c, project_id=project)}
        assert ac.checked_replaces(project, PERSON, ["ac-2"]) == ["AC-2"]
    run, spec, fctx = _child(eng, app, "Archive tickets instead of closing them.\n\n"
                             "Must-haves:\n- Tickets can be archived\n", replaces=["AC-2"])
    texts = [i["text"] for i in _held(eng, run, fctx, spec)[0]]
    assert "Tickets can be closed" not in texts and "Tickets can be archived" in texts
    vs = _versions(eng, app)
    assert [v["version"] for v in vs] == [1, 2, 3]
    req = fctx["change_request"]["id"]
    assert vs[2]["created_by"] == PERSON and req in vs[2]["reason"]
    assert "Archive tickets instead of closing them." in vs[2]["reason"]
    with eng.begin() as c:
        revised = c.execute(text("SELECT data FROM events WHERE project_id = :p AND "
                                 "event_type = 'acceptance.contract_revised' "
                                 "ORDER BY created_at DESC LIMIT 1"),
                            {"p": project}).scalar()
        row = c.execute(text("SELECT status, waived_by, waive_reason FROM findings "
                             "WHERE project_id = :p AND fingerprint = 'ac:AC-2'"),
                        {"p": project}).mappings().one()
    assert revised["removed"] == ["AC-2"] and revised["by"] == PERSON
    assert row["status"] == "wont_fix" and row["waived_by"] == PERSON
    assert req in row["waive_reason"] and "superseded" in row["waive_reason"]


def test_a_failure_that_included_a_replaced_item_clears_without_it():
    """C3: a later check that met every item still in the contract clears an earlier failure
    whose items included one a person has since replaced."""
    early = {"verdict": "not_met", "source": {"item_keys": ["a", "b", "gone"]}}
    later = {"verdict": "met", "source": {"item_keys": ["a", "b", "new"]}}
    assert ag._unresolved([("r1", early), ("r2", later)]) == [("r1", early)]
    assert ag._unresolved([("r1", early), ("r2", later)], removed={"gone"}) == []


# ── follow-ups that are not a person's request add nothing ──────────────────────────────────

def test_a_findings_or_review_fix_follow_up_adds_nothing(eng, app, project):  # noqa: F811
    with eng.begin() as c:
        c.execute(text("UPDATE code_task_runs SET status = 'completed' WHERE id = :i"),
                  {"i": app})
    new = governed._spawn_deferred_findings_task(
        eng, get_settings(), _Jobs(), project_id=project, task_id=None, spec=SPEC,
        base_branch="boss/helpdesk", findings=[{"severity": "major", "text": "a finding"}],
        parent_run_id=app, parent_title="HelpDesk", parent_root=app)
    review_fix, rspec, rfctx = _child(eng, app, "Fix these review comments: x", person=False,
                                      finalize_extra={"review_comments": [{"id": "rc1"}]})
    with eng.begin() as c:
        fspec, ffctx = c.execute(text("SELECT spec, finalize_ctx FROM code_task_runs "
                                      "WHERE id = :i"), {"i": new}).one()
    for rid, s, f in ((new, fspec, ffctx), (review_fix, rspec, rfctx)):
        assert "change_request" not in f
        assert [i["text"] for i in _held(eng, rid, f, s)[0]][:3] == OLD
    assert [v["version"] for v in _versions(eng, app)] == [1]


def test_only_person_request_callers_mark_their_follow_ups():
    """C4: New work / Change it (the follow-up endpoint and the workflow build step), extend and
    the feature PRD say is_person_request; the multi-repo send-back, review fixes, incidents and
    the agent's own tool do not (the default is unmarked)."""
    import boss_engine.multirepo.change_sets as change_sets
    from boss_engine.services import (agent_loop, change_review, decisions, local_review,
                                      reliability, workflow_capability_runner as wcr)
    from boss_engine.tools import builtin

    assert inspect.signature(cts.start_followup_task).parameters["is_person_request"].default \
        is False
    for module in (change_sets, change_review, local_review, reliability, agent_loop):
        assert "is_person_request" not in inspect.getsource(module), module.__name__
    assert "is_person_request=True" in inspect.getsource(decisions)
    # The build step's mark is the runner's trusted value, never a tool argument (D1).
    assert 'person_request = trusted_step_arg("person_request") is True' in \
        inspect.getsource(builtin)
    assert 'trusted_step = {"person_request": True,' in inspect.getsource(wcr)
    assert 'trusted={"person_request": True}' in inspect.getsource(wcr)
    assert 'args.get("person_request")' not in inspect.getsource(builtin)
    main = (Path(__file__).resolve().parents[2] / "apps/api/app/main.py").read_text()
    assert "actor=actor, is_person_request=True, replaces=replaces or None" in main


# ── PR #161 re-review D1: no caller can set what a tool's schema does not offer ─────────────

def _recording_registry(name="probe", props=("title",)):
    from boss_engine.tools.registry import ToolRegistry, ToolSpec, trusted_step_arg

    seen = {}

    def handler(ctx, *, project_id, actor, args):
        seen.update(args=dict(args), trusted=trusted_step_arg("person_request"))
        return {"ok": True}

    reg = ToolRegistry()
    reg.register(ToolSpec(name=name, description="probe", handler=handler, kind="read",
                          permission="memory:read",
                          parameters={"type": "object",
                                      "properties": {p: {"type": "string"} for p in props}}))
    return reg, seen


def test_invoke_drops_every_arg_the_schema_does_not_list():
    reg, seen = _recording_registry()
    out = reg.invoke("probe", object(), project_id="p", actor="usr_someone",
                     args={"title": "t", "person_request": True, "replaces": ["AC-1", "AC-2"],
                           "_writes_allowed": True, "_thread_id": "athr_forged"})
    assert seen["args"] == {"title": "t"}
    assert out["ignored_args"] == ["_thread_id", "_writes_allowed", "person_request", "replaces"]
    # BOSS's own context arrives apart from the caller's args, underscore keys only.
    reg.invoke("probe", object(), project_id="p", actor="usr_someone", args={"title": "t"},
               internal={"_thread_id": "athr_1", "person_request": True})
    assert seen["args"] == {"title": "t", "_thread_id": "athr_1"}


def test_a_step_s_trusted_values_never_reach_a_tool_a_caller_invokes():
    from boss_engine.tools.registry import TRUSTED_STEP_ARGS

    reg, seen = _recording_registry()
    token = TRUSTED_STEP_ARGS.set({"person_request": True})     # as if inside a build step
    try:
        reg.invoke("probe", object(), project_id="p", actor="usr_someone", args={})
    finally:
        TRUSTED_STEP_ARGS.reset(token)
    assert seen["trusted"] is None


def test_the_code_task_tools_offer_neither_person_request_nor_replaces():
    from boss_engine.tools.builtin import build_default_registry

    reg = build_default_registry()
    for name in ("create_code_task", "extend_code_task"):
        props = set(reg.get(name).parameters["properties"])
        assert not props & {"person_request", "replaces", "_checked_replaces"}, name


def test_an_mcp_or_agent_extend_with_person_request_is_not_marked(monkeypatch):
    from types import SimpleNamespace

    from boss_engine.tools.builtin import build_default_registry

    seen = {}

    def retry(engine, settings, jobs, run_id, **kw):
        seen.update(kw)
        return {"run_id": "crun_next"}

    monkeypatch.setattr(cts, "retry_code_task", retry)
    monkeypatch.setattr(cts, "extend_title", lambda *a, **k: "t")
    reg = build_default_registry()
    out = reg.invoke("extend_code_task", SimpleNamespace(engine=None, settings=None, jobs=None),
                     project_id="p", actor="usr_someone",
                     args={"run_id": "crun_x", "instructions": "Add CSV export",
                           "person_request": True}, operator_confirmed=True)
    assert "person_request" in out.get("ignored_args", []), out
    assert seen.get("change_request") is None


def test_a_build_steps_replaces_must_be_what_the_start_recorded():
    from contextlib import contextmanager
    from types import SimpleNamespace

    from boss_engine.tools import builtin
    from boss_engine.tools.registry import TRUSTED_STEP_ARGS

    class Conn:
        def execute(self, *a, **k):
            return SimpleNamespace(scalar=lambda: ["AC-1"])

    @contextmanager
    def begin():
        yield Conn()

    token = TRUSTED_STEP_ARGS.set({"person_request": True, "replaces": ["AC-1", "AC-2"]})
    try:
        out = builtin._build_onto_existing_app(
            SimpleNamespace(begin=begin), cts, "p", "usr_ana", {"instructions": "x"}, "crun_x",
            {"workflow_run_id": "wfr_1"})
    finally:
        TRUSTED_STEP_ARGS.reset(token)
    assert out["ok"] is False and "not the ones this run's start recorded" in out["error"]


# ── D2: no empty versions; a real title ─────────────────────────────────────────────────────

def test_a_request_that_adds_nothing_makes_no_version(eng, app):
    run, spec, fctx = _child(eng, app, "Must-haves:\n- Tickets can be created\n")
    texts = [i["text"] for i in _held(eng, run, fctx, spec)[0]]
    assert texts[:3] == OLD
    assert [v["version"] for v in _versions(eng, app)] == [1]


def test_the_title_is_the_requests_first_prose_line():
    assert ac.request_title({"text": "Must-haves:\n- An Export button"}) == "change request"
    assert ac.request_title({"id": "chg_1", "text": "## Change\nAdd CSV export. Then more."}) \
        == "Add CSV export."
    assert ac.request_title({"id": "chg_2", "text": "- only a bullet"}) == "change request chg_2"


# ── PR #161 round 3 ─────────────────────────────────────────────────────────────────────────

def _publish_wf(c, project, key, input_schema=None):  # noqa: F811
    from boss_engine.services import workflows

    definition = workflows.create_definition(
        c, project_id=project, workflow_key=key, name=key, description=None, actor="test",
        namespace="product")
    spec = {"entry_step": "research", "steps": [
        {"id": "research", "kind": "capability",
         "binding": {"capability": "research", "inputs": {"seed": "a todo app"}}, "next": []}]}
    if input_schema:
        spec["input_schema"] = input_schema
    return workflows.publish_version(c, project_id=project, definition_id=definition["id"],
                                     spec=spec, actor="test")


def _run_input(eng, run_id):
    with eng.begin() as c:
        return dict(c.execute(text("SELECT input FROM workflow_runs WHERE id = :i"),
                              {"i": run_id}).scalar() or {})


def test_no_caller_input_can_record_replaces_only_the_checked_list_is(eng, project):  # noqa: F811
    """HIGH: the runner trusted input["replaces"]. Every caller's `replaces` and
    `_checked_replaces` are dropped by start_run; only `checked_replaces=` (the HTTP start, after
    contract.checked_replaces) records the list, under CHECKED_REPLACES_KEY."""
    import uuid

    from boss_engine.services import workflows

    with eng.begin() as c:
        version = _publish_wf(c, project, f"wf_{uuid.uuid4().hex[:6]}")
        forged = workflows.start_run(
            c, project_id=project, actor="test", dedupe_key=uuid.uuid4().hex,
            workflow_version_id=version["id"],
            input_data={"directive": "d", "replaces": ["AC-1"], "_checked_replaces": ["AC-2"]})
        checked = workflows.start_run(
            c, project_id=project, actor="test", dedupe_key=uuid.uuid4().hex,
            workflow_version_id=version["id"], input_data={"directive": "d"},
            checked_replaces=["AC-3"])
    got = _run_input(eng, forged["id"])
    assert "replaces" not in got and workflows.CHECKED_REPLACES_KEY not in got
    got = _run_input(eng, checked["id"])
    assert got[workflows.CHECKED_REPLACES_KEY] == ["AC-3"] and "replaces" not in got


def test_start_workflow_with_inputs_replaces_removes_nothing_and_keeps_declared_args(
        eng, project, monkeypatch):  # noqa: F811
    """A model's start_workflow(inputs={"replaces": [...]}) records nothing the build step would
    trust. And MED: a top-level argument the workflow's input_schema declares (max_rules=15,
    brec_6025901347b6445eb9c1) still reaches the run; one it does not declare is named."""
    import uuid
    from types import SimpleNamespace

    from boss_engine.services import workflows
    from boss_engine.tools.builtin import build_default_registry

    key = f"wf_{uuid.uuid4().hex[:6]}"
    with eng.begin() as c:
        _publish_wf(c, project, key, {"type": "object", "properties": {
            "max_rules": {"type": "integer"}, "budget_usd": {"type": "number"}}})
    args = {"directive": "scan", "workflow_key": key, "max_rules": 15, "budget_usd": 3,
            "bogus": 1, "_checked_replaces": ["AC-1"],
            "inputs": {"replaces": ["AC-1"], "_checked_replaces": ["AC-1"]}}

    def start(extra=None):
        return build_default_registry().invoke(
            "start_workflow", SimpleNamespace(begin=eng.begin, engine=eng, settings=get_settings()),
            project_id=project, actor="usr_someone", operator_confirmed=True,
            args={**args, **(extra or {})})

    out = start()
    if not out.get("run_id") and out.get("missing"):     # no model on the test bench: as told
        out = start({"start_anyway": [str(m.get("key") if isinstance(m, dict) else m)
                                      for m in out["missing"]]})
    assert out.get("run_id"), out
    got = _run_input(eng, out["run_id"])
    assert "replaces" not in got and workflows.CHECKED_REPLACES_KEY not in got
    assert got["max_rules"] == 15 and got["budget_usd"] == 3
    assert out["ignored_args"] == ["_checked_replaces", "bogus"]


def test_extend_offers_allow_test_retirement_and_passes_it_on(monkeypatch):
    from types import SimpleNamespace

    from boss_engine.tools.builtin import build_default_registry

    seen = {}

    def retry(engine, settings, jobs, run_id, **kw):
        seen.update(kw)
        return {"run_id": "crun_next"}

    monkeypatch.setattr(cts, "retry_code_task", retry)
    monkeypatch.setattr(cts, "extend_title", lambda *a, **k: "t")
    reg = build_default_registry()
    assert "allow_test_retirement" in reg.get("extend_code_task").parameters["properties"]
    out = reg.invoke("extend_code_task", SimpleNamespace(engine=None, settings=None, jobs=None),
                     project_id="p", actor="usr_someone", operator_confirmed=True,
                     args={"run_id": "crun_x", "instructions": "Drop the CSV export",
                           "allow_test_retirement": True})
    assert "allow_test_retirement" not in out.get("ignored_args", []), out
    assert seen["allow_test_retirement"] is True


def test_kg_neighbors_takes_node_id_as_an_alias():
    from boss_engine.tools.builtin import build_default_registry
    from boss_engine.tools.registry import schema_args

    reg = build_default_registry()
    if "kg_neighbors" not in reg.names():
        pytest.skip("this edition registers no knowledge tools")
    kept, ignored = schema_args(reg.get("kg_neighbors"), {"node_id": "NPB-DQ-LN-003"})
    assert kept == {"node_id": "NPB-DQ-LN-003"} and not ignored


def test_the_direct_handler_doors_filter_by_schema_too():
    from boss_engine.services import agent_baseline, skill_executors

    for module in (skill_executors, agent_baseline):
        body = inspect.getsource(module)
        assert "args, ignored = schema_args(spec, args)" in body, module.__name__


def test_a_force_merge_is_never_offered_and_a_forcing_offer_says_so():
    """MED: decide.force merges over failing checks; only the console's merge sheet, where the
    person types the reason, sends it. No model/agent offer carries it, and if one ever does its
    button says exactly that, whatever label the model wrote."""
    from types import SimpleNamespace

    from boss_engine.services import intent_router
    from boss_engine.services.offer_label import FORCE_MERGE_LABEL, button_label, offerable_args
    from boss_engine.tools.builtin import build_default_registry

    assert offerable_args("decide", {"decision_id": "dec_1", "action": "approve",
                                     "note": "n", "force": True}) == {
        "decision_id": "dec_1", "action": "approve", "note": "n"}
    assert button_label("Approve the fix", "decide",
                        {"decision_id": "dec_1", "action": "approve", "force": True}) \
        == FORCE_MERGE_LABEL
    assert button_label("Approve the fix", "decide", {"decision_id": "dec_1"}) == "Approve the fix"

    out = build_default_registry().invoke(
        "propose_action", SimpleNamespace(), project_id="p", actor="usr_someone",
        args={"tool": "decide", "label": "Approve the fix",
              "args": {"decision_id": "dec_1", "action": "approve", "note": "ok",
                       "force": True}})
    assert out.get("proposed") is True, out
    assert "force" not in out["args"] and out["label"] == "Approve the fix"

    proposed: list = []
    intent_router.remember_offer(proposed, {
        "id": "act_1", "tool": "decide", "label": "Merge it",
        "args": {"decision_id": "dec_1", "action": "approve", "note": "x", "force": True}})
    assert "force" not in proposed[0]["args"]

    from boss_engine.agent import engine_agent

    assert "offerable_args(name, args)" in inspect.getsource(engine_agent)
