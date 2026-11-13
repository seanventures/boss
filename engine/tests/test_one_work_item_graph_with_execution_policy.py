"""ONE WORK-ITEM GRAPH WITH EXECUTION POLICY (BOSSAPP-829: 830, 831, 832).

* 830 — the canonical node: CRUD, the tree, human keys, labels, relations (blocked_by is
  blocks read the other way, no cycles), lazy linking at each creation door, and the blocker
  wake: an item's blocker finishing un-blocks it and resumes its paused run.
* 831 — the stage machine: done → in_review; approve walks the stages; a send-back returns the
  work with the comment; the third send-back of a stage escalates to the responsible person.
* 832 — typed questions: a form decision validates its typed answer on approve, accepted task
  suggestions become work items in the same transaction (all or none), and an agent run parked
  on ask_human resumes with the answer as a structured message.
"""
from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import text

from boss_engine.services import execution_policy, typed_forms, work_items, workflows

from ._ambient_support import eng, project  # noqa: F401 — fixtures used by name


def _decisions(eng):
    from boss_engine.config import get_settings
    from boss_engine.services.decisions import DecisionService

    return DecisionService(get_settings())


def _publish(c, project_id):
    key = f"wi_{uuid.uuid4().hex[:6]}"
    definition = workflows.create_definition(
        c, project_id=project_id, workflow_key=key, name=key, description=None, actor="test",
        namespace="product")
    return workflows.publish_version(
        c, project_id=project_id, definition_id=definition["id"],
        spec={"entry_step": "a", "steps": [{"id": "a", "kind": "task"}]}, actor="test")


def _start(c, project_id, version_id, **input_data):
    return workflows.start_run(
        c, project_id=project_id, actor="test", input_data={"directive": "x", **input_data},
        dedupe_key=f"dk_{uuid.uuid4().hex[:10]}", workflow_version_id=version_id)


def _finish(c, run_id):
    workflows._succeed_internal(c, run_id, "a", 0, {"ok": True})
    workflows._after_success(c, run_id, "a", 0)
    assert c.execute(text("SELECT status FROM workflow_runs WHERE id = :r"),
                     {"r": run_id}).scalar() == "completed"


def _status(c, item_id):
    return c.execute(text("SELECT status FROM work_items WHERE id = :i"), {"i": item_id}).scalar()


def _ceo(c, pid):
    c.execute(text(
        "INSERT INTO project_members (id, project_id, member_id, display_name, member_type, "
        "role_keys, permissions) VALUES (:i, :p, 'ceo', 'ceo', 'human', '[\"ceo\"]'::jsonb, "
        "'[\"decision_resolve\"]'::jsonb) ON CONFLICT DO NOTHING"),
        {"i": f"pm_{pid}_ceo", "p": pid})


# ── 830: the canonical node ──────────────────────────────────────────────────────────────────

def test_schema_applies_twice_without_a_failure(eng):
    from boss_engine.db import ensure_schema

    ensure_schema(eng)
    ensure_schema(eng)
    with eng.begin() as c:
        last = c.execute(text(
            "SELECT failed, failures FROM schema_apply_runs ORDER BY applied_at DESC LIMIT 1")).first()
        cols = set(c.execute(text(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'work_items'"
        )).scalars())
    assert last[0] == 0, last[1]
    assert {"parent_id", "goal_id", "human_key", "assignee", "responsible_person",
            "execution_policy", "execution_state"} <= cols


def test_items_form_a_tree_with_keys_labels_and_goals(eng, project):
    with eng.begin() as c:
        goal = work_items.create_goal(c, project_id=project, title="Ship v2")
        epic = work_items.create_item(c, project_id=project, title="Checkout epic",
                                      goal_id=goal["id"], labels=["Payments"], actor="person:ceo")
        story = work_items.create_item(c, project_id=project, title="Card form",
                                       parent_id=epic["id"], assignee="agent:frontend",
                                       responsible_person="person:ceo", actor="person:ceo")
        assert epic["human_key"] == "WI-1" and story["human_key"] == "WI-2"
        assert epic["labels"] == [{"name": "payments", "color": "gray"}]
        with pytest.raises(work_items.WorkItemError):
            work_items.update_item(c, project_id=project, item_id=epic["id"],
                                   fields={"parent_id": story["id"]}, actor="person:ceo")
        with pytest.raises(work_items.WorkItemError):
            work_items.create_item(c, project_id=project, title="x", assignee="frontend")
        with pytest.raises(work_items.WorkItemError):
            work_items.update_item(c, project_id=project, item_id=story["id"],
                                   fields={"status": "in_review"}, actor="person:ceo")
        edited = work_items.update_item(c, project_id=project, item_id=story["id"],
                                        fields={"title": "Card form v2", "status": "active"},
                                        actor="person:ceo")
        assert edited["title"] == "Card form v2" and edited["status"] == "active"
        forest = work_items.tree(c, project_id=project)
        assert [n["id"] for n in forest["items"]] == [epic["id"]]
        assert [n["id"] for n in forest["items"][0]["children"]] == [story["id"]]
        assert forest["goals"][0]["items"] == 1
        assert work_items.tree(c, project_id=project, label="payments")["count"] == 1
        work_items.delete_item(c, project_id=project, item_id=epic["id"], actor="person:ceo")
        assert work_items.get_item(c, project, story["id"])["parent_id"] is None


def test_blocked_by_is_blocks_read_the_other_way_and_cycles_are_refused(eng, project):
    with eng.begin() as c:
        a = work_items.create_item(c, project_id=project, title="Schema")
        b = work_items.create_item(c, project_id=project, title="API")
        cc = work_items.create_item(c, project_id=project, title="UI")
        work_items.add_relation(c, project_id=project, item_id=b["id"], other_id=a["id"],
                                kind="blocked_by", actor="person:ceo")
        work_items.add_relation(c, project_id=project, item_id=b["id"], other_id=cc["id"],
                                kind="blocks", actor="person:ceo")
        assert {(r["kind"], r["item"]["id"]) for r in work_items.relations(c, b["id"])} == {
            ("blocked_by", a["id"]), ("blocks", cc["id"])}
        assert _status(c, b["id"]) == "blocked" and _status(c, cc["id"]) == "blocked"
        assert [x["id"] for x in work_items.get_item(c, project, b["id"])["blocked_by"]] == [a["id"]]
        with pytest.raises(work_items.WorkItemError):
            work_items.add_relation(c, project_id=project, item_id=cc["id"], other_id=a["id"],
                                    kind="blocks", actor="person:ceo")
        work_items.remove_relation(c, project_id=project, item_id=b["id"], other_id=a["id"],
                                   kind="blocked_by", actor="person:ceo")
        assert _status(c, b["id"]) == "todo"


def test_a_blocker_finishing_wakes_the_blocked_item_and_resumes_its_run(eng, project):
    with eng.begin() as c:
        version = _publish(c, project)
        blocker = work_items.create_item(c, project_id=project, title="Migrate DB")
        second = work_items.create_item(c, project_id=project, title="Other blocker")
        blocked = work_items.create_item(c, project_id=project, title="New report")
        for b in (blocker, second):
            work_items.add_relation(c, project_id=project, item_id=blocked["id"],
                                    other_id=b["id"], kind="blocked_by", actor="person:ceo")
        # A run started FOR the blocked item joins it, and starts paused.
        run = _start(c, project, version["id"], work_item_id=blocked["id"])
        assert c.execute(text("SELECT status, work_item_id FROM workflow_runs WHERE id = :r"),
                         {"r": run["id"]}).first() == ("paused", blocked["id"])
        # A later start waits for the blockers.
        deferred = work_items.defer_start(c, project_id=project, item_id=blocked["id"],
                                          workflow_version_id=version["id"],
                                          input_data={"directive": "later"}, actor="person:ceo")
        assert deferred["status"] == "deferred"
        # One blocker done: still blocked by the other.
        verdict = work_items.complete_item(c, project_id=project, item_id=blocker["id"],
                                           actor="person:ceo")
        assert verdict["status"] == "done" and verdict["woke"] == []
        assert _status(c, blocked["id"]) == "blocked"
        # The last one cancelled: the item wakes, its paused run resumes, its start launches.
        work_items.set_status(c, project_id=project, item_id=second["id"], status="cancelled",
                              actor="person:ceo")
        assert _status(c, blocked["id"]) == "todo"
        assert c.execute(text("SELECT status FROM workflow_runs WHERE id = :r"),
                         {"r": run["id"]}).scalar() != "paused"
        runs = c.execute(text("SELECT count(*) FROM workflow_runs WHERE work_item_id = :w"),
                         {"w": blocked["id"]}).scalar()
        assert runs == 2
        assert c.execute(text(
            "SELECT count(*) FROM events WHERE event_type = 'work_item.unblocked' "
            "AND entity_id = :w"), {"w": blocked["id"]}).scalar() == 1


def test_each_creation_door_links_lazily_and_a_primary_run_completes_its_item(eng, project):
    from boss_engine.services.meeting_service import start_meeting_run
    from boss_engine.services.work_plane import WorkPlaneService

    with eng.begin() as c:
        version = _publish(c, project)
        run = _start(c, project, version["id"], title="Quarterly report")
        item_id = c.execute(text("SELECT work_item_id FROM workflow_runs WHERE id = :r"),
                            {"r": run["id"]}).scalar()
        assert item_id and work_items.get_item(c, project, item_id)["subject_type"] == "workflow_run"
        # A panel the run convenes joins the run's item instead of making its own.
        mrun = start_meeting_run(c, project_id=project, task_id=None, directive="argue it",
                                 url="", constraints={}, role_keys=[],
                                 finalize_ctx={"workflow_run_id": run["id"]})
        assert c.execute(text("SELECT work_item_id FROM meeting_runs WHERE id = :m"),
                         {"m": mrun}).scalar() == item_id
        # A planning ticket gets its own node.
        ticket = WorkPlaneService().create_roadmap_item(c, project_id=project, title="Ticket")
        assert ticket["work_item_id"] and ticket["work_item_id"] != item_id
        # The child (the panel) completing does not complete the item; the run does.
        assert work_items.on_run_completed(c, kind="meeting", run_id=mrun) is None
        assert _status(c, item_id) == "active"
        _finish(c, run["id"])
        assert _status(c, item_id) == "done"


def test_a_build_links_to_the_workflow_that_spawned_it(eng, project):
    with eng.begin() as c:
        version = _publish(c, project)
        run = _start(c, project, version["id"])
        item_id = c.execute(text("SELECT work_item_id FROM workflow_runs WHERE id = :r"),
                            {"r": run["id"]}).scalar()
        crun = f"crun_{uuid.uuid4().hex[:12]}"
        c.execute(text(
            "INSERT INTO code_task_runs (id, project_id, status, phase, title, spec, state, usage, "
            "finalize_ctx) VALUES (:i, :p, 'running', 'queued', 't', '{}', '{}', '{}', "
            "CAST(:f AS JSONB))"), {"i": crun, "p": project,
                                    "f": json.dumps({"workflow_run_id": run["id"]})})
        linked = work_items.link_run(c, kind="code_task", run_id=crun, project_id=project,
                                     title="t", inherit=[("workflow_run", run["id"])])
        assert linked == item_id
        # A spawned build finishing is not the item's done.
        assert work_items.on_run_completed(c, kind="code_task", run_id=crun) is None


# ── 831: the stage machine ───────────────────────────────────────────────────────────────────

POLICY = {"stages": [
    {"kind": "review", "participants": ["agent:qa_lead"]},
    {"kind": "approval", "participants": ["person:ceo"], "comment_required": True},
]}


def test_policy_validation_fills_defaults_and_refuses_nonsense():
    assert execution_policy.normalize_policy(POLICY)["stages"][0]["max_rounds"] == 3
    for bad in ({"stages": [{"kind": "vote", "participants": ["agent:x"]}]},
                {"stages": [{"kind": "review", "participants": []}]},
                {"stages": [{"kind": "review", "participants": ["qa"]}]}, "nope"):
        with pytest.raises(execution_policy.PolicyError):
            execution_policy.normalize_policy(bad)


def test_done_walks_review_then_approval_and_wakes_what_it_blocked(eng, project):
    svc = _decisions(eng)
    with eng.begin() as c:
        _ceo(c, project)
        item = work_items.create_item(c, project_id=project, title="Write the runbook",
                                      execution_policy=POLICY, responsible_person="person:ceo")
        after = work_items.create_item(c, project_id=project, title="Publish it")
        work_items.add_relation(c, project_id=project, item_id=after["id"], other_id=item["id"],
                                kind="blocked_by", actor="person:ceo")
        first = work_items.update_item(c, project_id=project, item_id=item["id"],
                                       fields={"status": "done"}, actor="person:writer")
        assert first["status"] == "in_review"
        review_id = first["execution_state"]["review"]["decision_id"]
        card = svc.get_row(c, review_id)
        assert card["recommendation"]["action"] == "execution_stage"
        svc.transition(c, review_id, action="approve", actor="ceo", note="looks right")
        state = work_items.get_item(c, project, item["id"])
        assert state["status"] == "in_review" and state["execution_state"]["review"]["stage"] == 1
        approval_id = state["execution_state"]["review"]["decision_id"]
        # The approval stage needs a comment: refused (and nothing recorded) without one.
        with pytest.raises(Exception):
            with c.begin_nested():
                svc.transition(c, approval_id, action="approve", actor="ceo", note="")
        svc.transition(c, approval_id, action="approve", actor="ceo", note="ship it")
        assert _status(c, item["id"]) == "done"
        assert _status(c, after["id"]) == "todo"
        events = [r["event"] for r in work_items.stage_log(c, item["id"])]
        assert events == ["raised", "approved", "raised", "approved", "completed"]


def test_send_back_returns_with_the_comment_and_the_third_escalates(eng, project):
    svc = _decisions(eng)
    with eng.begin() as c:
        _ceo(c, project)
        item = work_items.create_item(
            c, project_id=project, title="Pricing page copy", responsible_person="person:cmo",
            execution_policy={"stages": [{"kind": "review", "participants": ["agent:editor"]}]})
        for round_ in (1, 2, 3):
            verdict = work_items.complete_item(c, project_id=project, item_id=item["id"],
                                               actor="agent:writer")
            assert verdict["status"] == "in_review" and verdict["round"] == round_
            svc.transition(c, verdict["decision_id"], action="revise", actor="ceo",
                           note=f"tighten it ({round_})")
            now = work_items.get_item(c, project, item["id"])
            if round_ < 3:
                assert now["status"] == "active"
                assert now["execution_state"]["review"]["feedback"] == f"tighten it ({round_})"
        escalated = now["execution_state"]["review"]
        assert now["status"] == "in_review" and escalated["escalated"] is True
        card = svc.get_row(c, escalated["decision_id"])
        assert card["recommendation"]["participants"] == ["person:cmo"]
        assert card["question"].endswith("(escalated)")
        critiques = c.execute(text(
            "SELECT count(*) FROM work_item_messages WHERE work_item_id = :w AND kind = 'critique'"),
            {"w": item["id"]}).scalar()
        assert critiques == 2
        svc.transition(c, escalated["decision_id"], action="approve", actor="ceo", note="fine")
        assert _status(c, item["id"]) == "done"
        assert [r["event"] for r in work_items.stage_log(c, item["id"])].count("escalated") == 1


def test_a_run_policy_wins_and_a_run_without_stages_is_done_at_once(eng, project):
    with eng.begin() as c:
        version = _publish(c, project)
        run = _start(c, project, version["id"])
        item_id = c.execute(text("SELECT work_item_id FROM workflow_runs WHERE id = :r"),
                            {"r": run["id"]}).scalar()
        c.execute(text("UPDATE workflow_runs SET execution_policy = CAST(:p AS JSONB) "
                       "WHERE id = :r"), {"p": json.dumps(POLICY), "r": run["id"]})
        _finish(c, run["id"])
        assert c.execute(text("SELECT status FROM workflow_runs WHERE id = :r"),
                         {"r": run["id"]}).scalar() == "completed"
        assert _status(c, item_id) == "in_review"


# ── 832: typed questions ─────────────────────────────────────────────────────────────────────

FORM = {"fields": [
    {"key": "db", "type": "select", "label": "Database", "options": ["postgres", "mysql"]},
    {"key": "why", "type": "text", "label": "Why", "required": False},
    {"key": "urgent", "type": "checkbox", "label": "Urgent"},
    {"key": "findings", "type": "item_verdicts", "label": "Findings",
     "items": [{"id": "f1", "label": "SQL injection"}, {"id": "f2", "label": "Weak hash"}]},
    {"key": "next", "type": "task_suggestions", "label": "Follow-ups",
     "suggestions": [{"title": "Add rate limit"}, {"title": "Rotate keys"}]},
]}


def test_answers_are_typed_against_the_form():
    schema = typed_forms.normalize_form(FORM)
    ok = typed_forms.validate_answer(schema, {
        "db": "postgres", "findings": {"f1": "accept", "f2": "reject"},
        "next": [{"index": 0, "accepted": True}]})
    assert ok["urgent"] is False and ok["findings"] == {"f1": "accept", "f2": "reject"}
    for bad in ({"db": "oracle", "findings": {"f1": "accept", "f2": "accept"}},
                {"db": "postgres", "findings": {"f1": "accept"}},
                {"db": "postgres", "findings": {"f1": "accept", "f2": "x"}},
                {"db": "postgres", "findings": {"f1": "accept", "f2": "accept"}, "typo": 1},
                {"db": "postgres", "findings": {"f1": "accept", "f2": "accept"},
                 "next": [{"index": 7, "accepted": True}]}):
        with pytest.raises(typed_forms.FormError):
            typed_forms.validate_answer(schema, bad)


def _agent_run(c, project_id, decision_id):
    run_id = f"arun_{uuid.uuid4().hex[:10]}"
    messages = [{"role": "user", "content": "pick a db"},
                {"role": "assistant", "content": "", "tool_calls": []},
                {"role": "tool", "tool_call_id": "t1",
                 "content": json.dumps({"status": "deferred_for_approval",
                                        "decision_id": decision_id})}]
    c.execute(text(
        "INSERT INTO agent_runs (id, project_id, status, messages, transcript, usage, "
        "finalize_ctx, step, pending) VALUES (:i, :p, 'awaiting_approval', CAST(:m AS JSONB), "
        "'[]', '{}', '{}', 3, CAST(:pe AS JSONB))"),
        {"i": run_id, "p": project_id, "m": json.dumps(messages),
         "pe": json.dumps({"tool": "ask_human", "args": {}, "decision_id": decision_id,
                           "kind": "ask_human"})})
    return run_id


def test_a_typed_answer_round_trips_creates_suggestions_and_resumes_the_agent(eng, project,
                                                                             monkeypatch):
    from boss_engine.services import agent_loop

    svc = _decisions(eng)
    with eng.begin() as c:
        _ceo(c, project)
        parent = work_items.create_item(c, project_id=project, title="Security review")
        asked = typed_forms.ask_human(c, project_id=project, question="Which way?", form=FORM,
                                      asker="agent_run:x", work_item_id=parent["id"])
        run_id = _agent_run(c, project, asked["decision_id"])
        c.execute(text("UPDATE decisions SET recommendation = recommendation || "
                       "jsonb_build_object('run_id', CAST(:r AS TEXT)) WHERE id = :d"),
                  {"r": run_id, "d": asked["decision_id"]})
        # Without the form, or with an answer that does not fit, nothing is recorded.
        with pytest.raises(Exception):
            with c.begin_nested():
                svc.transition(c, asked["decision_id"], action="approve", actor="ceo",
                               note=None)
        with pytest.raises(Exception):
            with c.begin_nested():
                svc.transition(c, asked["decision_id"], action="approve", actor="ceo", note=None,
                               answer={"db": "oracle"})
        svc.transition(c, asked["decision_id"], action="approve", actor="ceo", note=None,
                       answer={
                           "db": "postgres", "urgent": True,
                           "findings": {"f1": "accept", "f2": "reject"},
                           "next": [{"index": 0, "accepted": True, "title": "Add a rate limit"},
                                    {"index": 1, "accepted": False}]})
        row = c.execute(text("SELECT answer, answered_by, status FROM decisions WHERE id = :d"),
                        {"d": asked["decision_id"]}).mappings().one()
        assert row["status"] == "approved" and row["answered_by"] == "ceo"
        assert row["answer"]["db"] == "postgres"
        kids = work_items.get_item(c, project, parent["id"])["children"]
        assert [k["title"] for k in kids] == ["Add a rate limit"]
        job = c.execute(text("SELECT kind, payload FROM jobs WHERE project_id = :p "
                             "AND kind = 'agent.resume'"), {"p": project}).mappings().one()
        assert job["payload"]["run_id"] == run_id
        # The worker's resume: the typed answer arrives as a structured message.
        driven = {}
        monkeypatch.setattr(agent_loop, "_drive_run",
                            lambda conn, settings, rid, steps: driven.setdefault("run", rid) or {})
        agent_loop.resume_agent_run(c, None, run_id)
        assert driven["run"] == run_id
        run = agent_loop.load_agent_run(c, run_id)
        assert run["status"] == "running" and run["pending"] is None
        tool_payload = json.loads(run["messages"][2]["content"])
        assert tool_payload["status"] == "answered"
        assert tool_payload["answer"]["findings"] == {"f1": "accept", "f2": "reject"}
        assert tool_payload["created_work_items"][0]["title"] == "Add a rate limit"
        assert run["messages"][-1]["content"].startswith("HUMAN ANSWER (structured): ")


def test_suggestions_are_accepted_all_or_none(eng, project, monkeypatch):
    svc = _decisions(eng)
    with eng.begin() as c:
        _ceo(c, project)
        asked = typed_forms.ask_human(c, project_id=project, question="Which follow-ups?",
                                      form={"fields": [FORM["fields"][4]]}, asker="person:x")
        real = work_items.create_item
        calls = {"n": 0}

        def flaky(*a, **kw):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("database hiccup")
            return real(*a, **kw)

        monkeypatch.setattr(work_items, "create_item", flaky)
        before = c.execute(text("SELECT count(*) FROM work_items WHERE project_id = :p"),
                           {"p": project}).scalar()
        with pytest.raises(RuntimeError):
            with c.begin_nested():
                svc.transition(c, asked["decision_id"], action="approve", actor="ceo", note=None,
                               answer={"next": [
                                   {"index": 0, "accepted": True}, {"index": 1, "accepted": True}]})
        assert c.execute(text("SELECT count(*) FROM work_items WHERE project_id = :p"),
                         {"p": project}).scalar() == before
        assert c.execute(text("SELECT status FROM decisions WHERE id = :d"),
                         {"d": asked["decision_id"]}).scalar() == "pending"


def test_declining_resumes_the_agent_with_the_reason(eng, project):
    svc = _decisions(eng)
    with eng.begin() as c:
        _ceo(c, project)
        asked = typed_forms.ask_human(c, project_id=project, question="Which db?",
                                      form={"fields": [FORM["fields"][0]]}, asker="agent_run:y")
        run_id = _agent_run(c, project, asked["decision_id"])
        c.execute(text("UPDATE decisions SET recommendation = recommendation || "
                       "jsonb_build_object('run_id', CAST(:r AS TEXT)) WHERE id = :d"),
                  {"r": run_id, "d": asked["decision_id"]})
        svc.transition(c, asked["decision_id"], action="reject", actor="ceo",
                       note="not my call")
        decision = c.execute(text("SELECT * FROM decisions WHERE id = :d"),
                             {"d": asked["decision_id"]}).mappings().one()
        body = typed_forms.answer_message(dict(decision))
        assert body == {**body, "status": "declined", "reason": "not my call"}
        assert c.execute(text("SELECT count(*) FROM jobs WHERE project_id = :p "
                              "AND kind = 'agent.resume'"), {"p": project}).scalar() == 1


# ── the MCP / chat tools ─────────────────────────────────────────────────────────────────────

class _Ctx:
    def __init__(self, engine):
        self.engine = engine
        self.settings = None

    def begin(self):
        return self.engine.begin()


def test_the_tools_list_create_link_and_ask(eng, project):
    from boss_engine.tools.builtin import build_default_registry

    tools = {s.name: s for s in build_default_registry().specs()}

    def call(name, **args):
        return tools[name].handler(_Ctx(eng), project_id=project, actor="agent:planner",
                                   args=args)

    a = call("create_work_item", title="Design", labels=["ux"])["item"]
    b = call("create_work_item", title="Build", parent_id=a["id"], assignee="agent:frontend")
    assert b["ok"] and b["item"]["parent_id"] == a["id"]
    assert call("create_work_item", title="x", assignee="nobody") == {
        "ok": False, "error": "assignee must be agent:<role> or person:<id>"}
    linked = call("link_work_items", item_id=b["item"]["id"], other_id=a["id"],
                  kind="blocked_by")
    assert linked["ok"] and linked["relations"][0]["kind"] == "blocked_by"
    listed = call("list_work_items", label="ux")
    assert listed["count"] == 1 and listed["items"][0]["id"] == a["id"]
    one = call("list_work_items", item_id=b["item"]["id"])["item"]
    assert one["blocked_by"][0]["id"] == a["id"]
    asked = call("ask_human", question="Which framework?", work_item_id=a["id"],
                 fields=[{"key": "fw", "type": "select", "options": ["react", "vue"]}])
    assert asked["ok"] and asked["fields"] == ["fw"]
    assert call("ask_human", question="?", fields=[{"type": "nope"}])["ok"] is False
    # Done through the tool: no stages, so done at once, and what it blocked wakes.
    finished = call("complete_work_item", item_id=a["id"])
    assert finished["ok"] and finished["status"] == "done"
    assert finished["woke"][0]["id"] == b["item"]["id"]


def test_a_resubmission_after_a_send_back_stays_under_the_runs_own_stages(eng, project):
    svc = _decisions(eng)
    with eng.begin() as c:
        _ceo(c, project)
        version = _publish(c, project)
        run = _start(c, project, version["id"])
        item_id = c.execute(text("SELECT work_item_id FROM workflow_runs WHERE id = :r"),
                            {"r": run["id"]}).scalar()
        c.execute(text("UPDATE workflow_runs SET execution_policy = CAST(:p AS JSONB) "
                       "WHERE id = :r"), {"p": json.dumps({"stages": [
                           {"kind": "review", "participants": ["agent:qa"]}]}), "r": run["id"]})
        _finish(c, run["id"])
        first = work_items.get_item(c, project, item_id)["execution_state"]["review"]
        svc.transition(c, first["decision_id"], action="revise", actor="ceo", note="add tests")
        assert _status(c, item_id) == "active"
        # The worker re-submits by hand (no run origin now): the run's stage is asked again.
        again = work_items.complete_item(c, project_id=project, item_id=item_id,
                                         actor="person:dev", origin="person:dev")
        assert again["status"] == "in_review" and again["round"] == 2


def test_a_chat_turn_that_asked_carries_the_question_for_the_form(eng, project):
    from boss_engine.services import turn_cards
    from boss_engine.services.intent_router import tool_call_record

    record = tool_call_record({"tool": "ask_human", "args": {"question": "?"},
                               "result": {"ok": True, "decision_id": "dec_123"}})
    assert record["decision_id"] == "dec_123"
    with eng.begin() as c:
        cites = turn_cards.citations(c, project, "I asked you which framework to use.", [record])
    assert cites[0]["kind"] == "ask" and cites[0]["ref"] == "dec_123"
