"""Architecture approval: the architect designs from the APPROVED PRD, and only a person signs it.

Gap report d1-helpdesk §3 item 4: BOSS had no architecture capability, so the design was a second
panel and a manual gate. This is the capability, end to end through the real runner on the real
database — only the architect's model is scripted, the way the PRD path's tests script theirs.
"""
from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import text

from boss_engine.config import get_settings
from boss_engine.services import architecture_docs as arch
from boss_engine.services import workflow_capability_runner as runner
from boss_engine.services import workflows
from boss_engine.services.decisions import (
    DecisionService,
    InvalidDecisionTransitionError,
    approve_by_policy,
)
from boss_engine.services.workflow_capabilities import palette, stops_for_a_person

from .test_workflow_capability_runner import (  # noqa: F401 - fixtures used by name
    Ctx,
    RecordingRegistry,
    RecordingTool,
    _publish,
    _start,
    _steps,
    eng,
    project,
)

PRD = "# PRD: Helpdesk\n\nAgents answer tickets. REQUIREMENT-HD-7: tickets are tenant-isolated."


class ScriptedArchitect:
    """Stands in for the architect's model. Writes every section, numbered per version, and
    records each prompt so a test can read what the architect was given."""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def create_text_response(self, prompt, system_prompt=None):
        self.prompts.append(prompt)
        v = len(self.prompts)
        body = "\n\n".join(
            f"## {heading}\nArchitecture text for {key}, draft {v}."
            for key, heading, _what in arch.SECTIONS)
        return "ref", body, {"model": "scripted-architect", "prompt_tokens": 1,
                             "completion_tokens": 1}


@pytest.fixture(autouse=True)
def _clean(eng, project):
    """What these tests add beyond the shared fixture's teardown list."""
    yield
    with eng.begin() as c:
        for table in ("architecture_reviews", "artifacts", "decisions", "meeting_runs",
                      "events", "project_members", "agent_acts"):
            try:
                with c.begin_nested():
                    c.execute(text(f"DELETE FROM {table} WHERE project_id = :p"),  # noqa: S608
                              {"p": project})
            except Exception:  # noqa: BLE001 — a table this install does not have
                pass
        c.execute(text("DELETE FROM tasks WHERE project_id = :p"), {"p": project})


@pytest.fixture()
def architect(monkeypatch):
    model = ScriptedArchitect()
    monkeypatch.setattr(arch, "architect_adapter", lambda *a, **k: model)
    return model


@pytest.fixture()
def tools(monkeypatch):
    meeting = RecordingTool("request_meeting", {"meeting_run_id": "x"})
    build = RecordingTool("create_code_task", {"code_task_run_id": f"crun_{uuid.uuid4().hex[:8]}"})
    monkeypatch.setattr(runner, "_tool_registry", lambda: RecordingRegistry([meeting, build]))
    return {"meeting": meeting, "build": build}


def _person(c, project_id, member="ceo"):
    c.execute(
        text("INSERT INTO project_members (id, project_id, member_id, display_name, member_type, "
             "role_keys, permissions) VALUES (:i, :p, :m, :m, 'human', CAST(:r AS JSONB), "
             "CAST(:perm AS JSONB)) ON CONFLICT DO NOTHING"),
        {"i": f"pm_{project_id}_{member}", "p": project_id, "m": member,
         "r": json.dumps([member]), "perm": json.dumps(["decision_resolve"])},
    )


def _panel_finished(c, project_id, meeting_run_id, *, decision_status="approved"):
    """What a finished PRD panel leaves: its run, its PRD, and the PRD decision on its task."""
    task_id = f"tsk_{uuid.uuid4().hex[:8]}"
    c.execute(text("INSERT INTO tasks (id, project_id, title, status, dedupe_key) "
                   "VALUES (:t, :p, 'helpdesk', 'open', :t)"), {"t": task_id, "p": project_id})
    c.execute(
        text("INSERT INTO meeting_runs (id, project_id, task_id, status, phase, directive, state) "
             "VALUES (:i, :p, :t, 'completed', 'done', 'helpdesk', CAST(:s AS JSONB))"),
        {"i": meeting_run_id, "p": project_id, "t": task_id,
         "s": json.dumps({"prd_markdown": PRD})},
    )
    c.execute(
        text("INSERT INTO decisions (id, project_id, task_id, question, status, approved_by) "
             "VALUES (:i, :p, :t, 'Publish the PRD?', :s, 'ceo')"),
        {"i": f"dec_{uuid.uuid4().hex[:8]}", "p": project_id, "t": task_id,
         "s": decision_status},
    )


def _settle(c, eng, project_id, run_id, times=4):
    for _ in range(times):
        runner.tick(c, Ctx(eng), project_id=project_id)
        workflows.advance_run(c, project_id, run_id)


def _delivery(c, project_id, tools, key):
    tools["meeting"]._returns = {"meeting_run_id": f"mrun_{uuid.uuid4().hex[:8]}"}
    _, version = _publish(c, project_id, key, [
        {"id": "panel", "kind": "capability",
         "binding": {"capability": "meeting", "inputs": {"directive": "Build a helpdesk"}},
         "next": ["arch"]},
        {"id": "arch", "kind": "capability",
         "binding": {"capability": "architecture",
                     "inputs": {"constraints": "Postgres only."}},
         "next": ["build"]},
        {"id": "build", "kind": "capability",
         "binding": {"capability": "build",
                     "inputs": {"title": "Helpdesk", "instructions": "Build the helpdesk",
                                "spec_markdown": {"from_step": "panel",
                                                  "field": "prd_markdown"}}},
         "next": []},
    ], "panel")
    return _start(c, project_id, version["id"], {"title": "Helpdesk"}), \
        tools["meeting"]._returns["meeting_run_id"]


def _review_of(c, run_id):
    return dict(c.execute(text("SELECT * FROM architecture_reviews WHERE workflow_run_id = :r"),
                          {"r": run_id}).mappings().one())


def _decision(c, decision_id):
    return dict(c.execute(text("SELECT * FROM decisions WHERE id = :d"),
                          {"d": decision_id}).mappings().one())


# ── The step ───────────────────────────────────────────────────────────────────────────────

def test_it_is_a_palette_step_that_stops_for_a_person():
    entry = next(c for c in palette() if c["key"] == "architecture")
    assert entry["label"] == "Architecture approval"
    assert entry["captain"] == "chief_architect"
    assert entry["stops_for_a_person"] is True and entry["reviewed_by_a_person"] is True
    assert "architecture_markdown" in entry["produces"]
    assert stops_for_a_person("architecture")


def test_the_architect_designs_from_the_approved_prd_and_waits(eng, project, tools, architect):
    with eng.begin() as c:
        run, meeting_run = _delivery(c, project, tools, "arch_flow")
        runner.tick(c, Ctx(eng), project_id=project)
        _panel_finished(c, project, meeting_run)
        _settle(c, eng, project, run["id"])
        steps = _steps(c, run["id"])
        review = _review_of(c, run["id"])
        doc = c.execute(text("SELECT content FROM artifacts WHERE id = :a"),
                        {"a": review["current_artifact_id"]}).scalar()
        decision = _decision(c, review["decision_id"])

    assert steps["arch"]["status"] == "waiting"
    assert steps["arch"]["output"]["await"] == {"kind": "architecture", "id": review["id"]}
    assert "build" not in steps or steps["build"]["status"] == "pending"
    assert not tools["build"].calls, "the build started before a person approved the design"
    # Designed FROM the approved PRD, with the step's constraints.
    assert "REQUIREMENT-HD-7" in architect.prompts[0]
    assert "Postgres only." in architect.prompts[0]
    # A first-class, versioned, structured document.
    assert review["status"] == "awaiting_approval" and review["current_version"] == 1
    assert doc["version"] == 1 and set(doc["sections"]) == set(arch.SECTION_KEYS)
    assert doc["missing_sections"] == []
    for _key, heading, _what in arch.SECTIONS:
        assert f"## {heading}" in doc["markdown"]
    # The question a person answers, showing the document, with Request changes on it.
    assert decision["status"] == "pending"
    assert decision["recommendation"]["action"] == "review_architecture"
    assert "draft 1" in decision["recommendation"]["document_markdown"]


def test_without_a_prd_upstream_it_refuses_in_words(eng, project, tools, architect):
    with eng.begin() as c:
        _, version = _publish(c, project, "arch_alone", [
            {"id": "arch", "kind": "capability",
             "binding": {"capability": "architecture", "inputs": {}}, "next": []},
        ], "arch")
        run = _start(c, project, version["id"])
        _settle(c, eng, project, run["id"], times=1)
        steps = _steps(c, run["id"])
        reviews = c.execute(text("SELECT count(*) FROM architecture_reviews "
                                 "WHERE workflow_run_id = :r"), {"r": run["id"]}).scalar()

    assert steps["arch"]["status"] in {"failed", "deadlettered"}
    assert "no PRD to design from" in steps["arch"]["last_error"]
    assert reviews == 0 and not architect.prompts


def test_a_prd_nobody_approved_is_refused(eng, project, tools, architect):
    with eng.begin() as c:
        run, meeting_run = _delivery(c, project, tools, "arch_unapproved")
        runner.tick(c, Ctx(eng), project_id=project)
        _panel_finished(c, project, meeting_run, decision_status="pending")
        _settle(c, eng, project, run["id"])
        steps = _steps(c, run["id"])

    assert steps["arch"]["status"] in {"failed", "deadlettered"}
    assert "has not been approved" in steps["arch"]["last_error"]
    assert not architect.prompts and not tools["build"].calls


# ── The gate ───────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("actor", ["chief_architect", "qa", "auto_approval", "agent:helper"])
def test_an_agent_can_never_approve_it(eng, project, tools, architect, actor):
    with eng.begin() as c:
        run, meeting_run = _delivery(c, project, tools, f"arch_agent_{actor.replace(':', '_')}")
        runner.tick(c, Ctx(eng), project_id=project)
        _panel_finished(c, project, meeting_run)
        _settle(c, eng, project, run["id"])
        decision_id = _review_of(c, run["id"])["decision_id"]
        with pytest.raises(InvalidDecisionTransitionError, match="only by a person"):
            DecisionService(get_settings()).transition(
                c, decision_id, action="approve", actor=actor, note=None)
        assert approve_by_policy(c, decision_id, project_id=project, actor="auto_approval",
                                 note="policy") is False
        with pytest.raises(InvalidDecisionTransitionError, match="only by a person"):
            DecisionService(get_settings()).transition(
                c, decision_id, action="approve", actor="auto_approval", note=None,
                system_policy=True)
        _settle(c, eng, project, run["id"], times=2)
        assert _decision(c, decision_id)["status"] == "pending"
        assert _review_of(c, run["id"])["status"] == "awaiting_approval"
        assert _steps(c, run["id"])["arch"]["status"] == "waiting"
        assert not tools["build"].calls


def test_an_approval_from_inside_an_agents_run_is_refused(eng, project, tools, architect):
    from boss_engine.identity import lineage

    with eng.begin() as c:
        _person(c, project)
        run, meeting_run = _delivery(c, project, tools, "arch_run_token")
        runner.tick(c, Ctx(eng), project_id=project)
        _panel_finished(c, project, meeting_run)
        _settle(c, eng, project, run["id"])
        decision_id = _review_of(c, run["id"])["decision_id"]
        on_behalf = lineage.Acting(principal_id="usr_ceo", org_id="org", run_id="arun_1",
                                   chain=("agt_chief_architect",))
        with lineage.acting(on_behalf), pytest.raises(InvalidDecisionTransitionError,
                                                      match="agent's run"):
            DecisionService(get_settings()).transition(
                c, decision_id, action="approve", actor="ceo", note=None)


def test_request_changes_revises_and_the_approved_version_reaches_the_build(
        eng, project, tools, architect):
    decisions = DecisionService(get_settings())
    with eng.begin() as c:
        _person(c, project)
        run, meeting_run = _delivery(c, project, tools, "arch_revise")
        runner.tick(c, Ctx(eng), project_id=project)
        _panel_finished(c, project, meeting_run)
        _settle(c, eng, project, run["id"])
        first = _review_of(c, run["id"])

        decisions.transition(c, first["decision_id"], action="revise", actor="ceo",
                             note="Use row-level security for tenant isolation.")
        assert _review_of(c, run["id"])["status"] == "revision_requested"
        _settle(c, eng, project, run["id"], times=1)
        second = _review_of(c, run["id"])
        assert second["status"] == "awaiting_approval" and second["current_version"] == 2
        assert second["decision_id"] != first["decision_id"]
        assert _decision(c, first["decision_id"])["status"] == "revision_requested"
        # The architect revised FROM the previous version and the note.
        assert "row-level security" in architect.prompts[1]
        assert "draft 1" in architect.prompts[1]
        assert not tools["build"].calls

        approved = decisions.transition(c, second["decision_id"], action="approve",
                                        actor="ceo", note="Good.")
        assert approved["status"] == "approved"
        _settle(c, eng, project, run["id"])
        steps = _steps(c, run["id"])
        versions = arch.get_review(c, project, second["id"])["versions"]

    assert steps["arch"]["status"] == "succeeded"
    assert steps["arch"]["output"]["architecture_approved_by"] == "ceo"
    assert steps["arch"]["output"]["architecture_version"] == 2
    assert [v["version"] for v in versions] == [1, 2]
    assert versions[1]["revised_for"] == "Use row-level security for tenant isolation."
    # Downstream: the build's brief carries the PRD AND the approved architecture (v2, not v1).
    assert tools["build"].calls, "the build never ran after the architecture was approved"
    brief = tools["build"].calls[0]["args"]["instructions"]
    assert "REQUIREMENT-HD-7" in brief
    assert "THE APPROVED ARCHITECTURE BELOW IS BINDING" in brief
    assert "draft 2" in brief and "draft 1" not in brief


def test_rejecting_it_stops_the_run_and_cannot_be_skipped(eng, project, tools, architect):
    with eng.begin() as c:
        _person(c, project)
        run, meeting_run = _delivery(c, project, tools, "arch_reject")
        runner.tick(c, Ctx(eng), project_id=project)
        _panel_finished(c, project, meeting_run)
        _settle(c, eng, project, run["id"])
        review = _review_of(c, run["id"])
        DecisionService(get_settings()).transition(
            c, review["decision_id"], action="reject", actor="ceo", note="Wrong stack.")
        _settle(c, eng, project, run["id"], times=2)
        steps = _steps(c, run["id"])
        status = c.execute(text("SELECT status FROM workflow_runs WHERE id = :r"),
                           {"r": run["id"]}).scalar()

    assert steps["arch"]["status"] in {"failed", "deadlettered"}
    assert "Wrong stack." in steps["arch"]["last_error"]
    assert steps["arch"]["output"].get(runner.REJECTED_BY_APPROVER) is True
    assert status in {"failed", "deadlettered"}
    assert not tools["build"].calls


def test_a_model_that_omits_a_section_is_named_not_hidden():
    sections = arch.parse_sections("## 1. Context\nWhy.\n## APIs\nGET /tickets\n")
    assert set(sections) == {"context", "apis"}
    doc = arch.render("Helpdesk", sections, 3)
    assert "_Version 3_" in doc
    assert "Not covered by the architect" in doc.split("## Data model")[1]
