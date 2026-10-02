"""A customer defines their own step, and it runs under the same governance as a built-in one.

The claim being tested is narrow and worth stating plainly: a custom step must be as easy to
add as a standard one and NOT easier to get away with. So most of what follows is about the
risk floor — that a customer cannot declare their own step harmless, that binding a dangerous
tool forces an approval, and that the approval actually gates execution rather than being
recorded and ignored.
"""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine, text

from boss_engine.config import Settings
from boss_engine.services import workflow_capability_runner as runner
from boss_engine.services import workflow_custom_steps as custom_steps
from boss_engine.services import workflows

TEST_DB = os.environ.get(
    "BOSS_TEST_DATABASE_URL", "postgresql+psycopg://boss:boss@localhost:5432/boss_test"
)


@pytest.fixture(scope="module")
def eng():
    engine = create_engine(TEST_DB, future=True)
    try:
        with engine.begin() as c:
            c.execute(text("SELECT 1 FROM workflow_step_runs LIMIT 1"))
    except Exception as error:                      # pragma: no cover
        pytest.skip(f"boss_test unavailable: {error}")
    with engine.begin() as c:
        for ddl in (
            "ALTER TABLE workflow_definitions ADD COLUMN IF NOT EXISTS "
            "namespace TEXT NOT NULL DEFAULT 'system'",
            "ALTER TABLE workflow_definitions ADD COLUMN IF NOT EXISTS "
            "is_catalogue BOOLEAN NOT NULL DEFAULT false",
            "ALTER TABLE workflow_definitions ADD COLUMN IF NOT EXISTS forked_from_version_id TEXT",
            """
            CREATE TABLE IF NOT EXISTS workflow_custom_steps (
              id TEXT PRIMARY KEY,
              project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
              step_key TEXT NOT NULL,
              label TEXT NOT NULL,
              summary TEXT,
              binding_kind TEXT NOT NULL CHECK (binding_kind IN ('skill', 'mcp_tool')),
              skill_id TEXT,
              mcp_server_id TEXT REFERENCES mcp_servers(id) ON DELETE SET NULL,
              mcp_tool_name TEXT,
              inputs JSONB NOT NULL DEFAULT '[]'::jsonb,
              produces JSONB NOT NULL DEFAULT '[]'::jsonb,
              enabled BOOLEAN NOT NULL DEFAULT true,
              created_by TEXT NOT NULL,
              created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
              updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
              UNIQUE(project_id, step_key)
            )
            """,
        ):
            c.exec_driver_sql(ddl)
    return engine


@pytest.fixture()
def project(eng):
    pid = f"p_{uuid.uuid4().hex[:10]}"
    with eng.begin() as c:
        wid = c.execute(text("SELECT workspace_id FROM projects LIMIT 1")).scalar()
        if wid is None:
            pytest.skip("boss_test has no workspace")
        c.execute(
            text("INSERT INTO projects (id, workspace_id, name, slug) "
                 "VALUES (:i, :w, 'custom step test', :s)"),
            {"i": pid, "w": wid, "s": pid},
        )
    yield pid
    with eng.begin() as c:
        c.execute(text("DELETE FROM workflow_step_runs WHERE project_id=:p"), {"p": pid})
        c.execute(text("DELETE FROM workflow_runs WHERE project_id=:p"), {"p": pid})
        for table in ("workflow_custom_steps", "workflow_mappings", "workflow_versions",
                      "workflow_definitions", "project_skills", "mcp_servers",
                      "decisions", "events", "receipts"):
            try:
                c.execute(text(f"DELETE FROM {table} WHERE project_id=:p"), {"p": pid})
            except Exception:
                pass
        c.execute(text("DELETE FROM projects WHERE id=:p"), {"p": pid})


def _skill(c, project_id, skill_id, *, risk_tier, trusted=True, enabled=True):
    import json
    c.execute(
        text("INSERT INTO project_skills (id, project_id, skill_id, enabled, config, provenance) "
             "VALUES (:i, :p, :s, :e, CAST(:c AS JSONB), CAST(:pr AS JSONB))"),
        {"i": f"ps_{uuid.uuid4().hex[:10]}", "p": project_id, "s": skill_id, "e": enabled,
         "c": json.dumps({} if risk_tier is None else {"risk_tier": risk_tier}),
         "pr": json.dumps({"trusted": trusted})},
    )


def _mcp(c, project_id, *, risk_tier, overrides=None, trusted=True, enabled=True):
    import json
    sid = f"mcp_{uuid.uuid4().hex[:10]}"
    c.execute(
        text("INSERT INTO mcp_servers (id, project_id, name, url, risk_tier, enabled, "
             "provenance, tool_risk_overrides) VALUES (:i, :p, 'tracker', "
             "'http://localhost:1/mcp', :r, :e, CAST(:pr AS JSONB), CAST(:o AS JSONB))"),
        {"i": sid, "p": project_id, "r": risk_tier, "e": enabled,
         "pr": json.dumps({"trusted": trusted}), "o": json.dumps(overrides or {})},
    )
    return sid


# ── Defining a step ────────────────────────────────────────────────────────────

def test_a_step_key_is_derived_and_stays_unique(eng, project):
    with eng.begin() as c:
        _skill(c, project, "tracker.file_issue", risk_tier=1)
        first = custom_steps.create(
            c, project_id=project, label="File an issue", binding_kind="skill",
            skill_id="tracker.file_issue", actor="test",
        )
        second = custom_steps.create(
            c, project_id=project, label="File an issue", binding_kind="skill",
            skill_id="tracker.file_issue", actor="test",
        )
    assert first["step_key"] == "file_an_issue"
    # Not an error: two steps can reasonably share a name, and refusing the second would make
    # the customer invent a key by hand.
    assert second["step_key"] == "file_an_issue_2"


def test_a_binding_must_name_the_thing_it_runs(eng, project):
    with eng.begin() as c:
        with pytest.raises(custom_steps.CustomStepError) as error:
            custom_steps.create(
                c, project_id=project, label="Do a thing", binding_kind="mcp_tool",
                mcp_server_id=None, actor="test",
            )
    assert "MCP server" in str(error.value)


def test_an_integration_cannot_be_bound_because_it_cannot_be_called(eng, project):
    """Integrations grant authority to skills; they are not invocable on their own. Offering
    the binding would produce a step that publishes and then has nothing to run."""
    with eng.begin() as c:
        with pytest.raises(custom_steps.CustomStepError):
            custom_steps.create(
                c, project_id=project, label="Post to Slack", binding_kind="integration",
                actor="test",
            )


# ── The risk floor ─────────────────────────────────────────────────────────────

def test_risk_comes_from_the_bound_skill_not_from_the_customer(eng, project):
    """The whole design rests on this. If a customer could label their own step, "harmless"
    beside a tool that drops the database would be the documented way past every gate."""
    with eng.begin() as c:
        _skill(c, project, "prod.truncate", risk_tier=3)
        step = custom_steps.create(
            c, project_id=project, label="Reset prod", binding_kind="skill",
            skill_id="prod.truncate", actor="test",
        )
    assert step["risk_tier"] == 3
    assert step["approval_forced_manual"] is True


def test_an_unlabelled_skill_is_treated_as_dangerous_not_as_safe(eng, project):
    with eng.begin() as c:
        _skill(c, project, "mystery.thing", risk_tier=None)
        step = custom_steps.create(
            c, project_id=project, label="Mystery", binding_kind="skill",
            skill_id="mystery.thing", actor="test",
        )
    assert step["risk_tier"] == custom_steps.UNKNOWN_RISK
    assert step["approval_forced_manual"] is True


def test_a_per_tool_override_beats_the_servers_blanket_tier(eng, project):
    """One dangerous tool on an otherwise ordinary server is exactly what the override is for."""
    with eng.begin() as c:
        server = _mcp(c, project, risk_tier=0, overrides={"delete_everything": 3})
        safe = custom_steps.create(
            c, project_id=project, label="List issues", binding_kind="mcp_tool",
            mcp_server_id=server, mcp_tool_name="list_issues", actor="test",
        )
        dangerous = custom_steps.create(
            c, project_id=project, label="Delete everything", binding_kind="mcp_tool",
            mcp_server_id=server, mcp_tool_name="delete_everything", actor="test",
        )
    assert safe["risk_tier"] == 0 and safe["approval_forced_manual"] is False
    assert dangerous["risk_tier"] == 3 and dangerous["approval_forced_manual"] is True


def test_a_workflow_cannot_publish_a_dangerous_custom_step_as_auto(eng, project):
    with eng.begin() as c:
        _skill(c, project, "prod.truncate", risk_tier=3)
        custom_steps.create(
            c, project_id=project, label="Reset prod", binding_kind="skill",
            skill_id="prod.truncate", actor="test", step_key="reset_prod",
        )
        definition = workflows.create_definition(
            c, project_id=project, workflow_key="wf_auto", name="wf", description=None,
            actor="test", namespace="product",
        )
        with pytest.raises(Exception) as error:
            workflows.publish_version(
                c, project_id=project, definition_id=definition["id"], actor="test",
                spec={"entry_step": "s", "steps": [
                    {"id": "s", "kind": "capability", "next": [],
                     "approval": {"mode": "auto"},
                     "binding": {"custom_step": "reset_prod"}},
                ]},
            )
    assert "requires a person" in str(error.value)


def test_a_dangerous_custom_step_cannot_approve_itself_on_timeout(eng, project):
    """The same ceiling built-in capabilities already have. Without it, a tier-3 custom
    step could publish with on_timeout=approve and, after the SLA, run unattended."""
    with eng.begin() as c:
        _skill(c, project, "prod.truncate", risk_tier=3)
        custom_steps.create(
            c, project_id=project, label="Reset prod", binding_kind="skill",
            skill_id="prod.truncate", actor="test", step_key="reset_prod",
        )
        definition = workflows.create_definition(
            c, project_id=project, workflow_key="wf_sla", name="wf", description=None,
            actor="test", namespace="product",
        )
        with pytest.raises(Exception) as error:
            workflows.publish_version(
                c, project_id=project, definition_id=definition["id"], actor="test",
                spec={"entry_step": "s", "steps": [
                    {"id": "s", "kind": "capability", "next": [],
                     "approval": {"mode": "manual", "sla_hours": 1, "on_timeout": "approve"},
                     "binding": {"custom_step": "reset_prod"}},
                ]},
            )
    assert "cannot approve itself on a timeout" in str(error.value)


def test_an_untrusted_skill_cannot_be_given_reach(eng, project):
    with eng.begin() as c:
        _skill(c, project, "random.download", risk_tier=2, trusted=False)
        custom_steps.create(
            c, project_id=project, label="Run it", binding_kind="skill",
            skill_id="random.download", actor="test", step_key="run_it",
        )
        definition = workflows.create_definition(
            c, project_id=project, workflow_key="wf_untrusted", name="wf", description=None,
            actor="test", namespace="product",
        )
        with pytest.raises(Exception) as error:
            workflows.publish_version(
                c, project_id=project, definition_id=definition["id"], actor="test",
                spec={"entry_step": "s", "steps": [
                    {"id": "s", "kind": "capability", "next": [],
                     "approval": {"mode": "manual"},
                     "binding": {"custom_step": "run_it"}},
                ]},
            )
    assert "untrusted" in str(error.value)


def test_a_step_bound_to_a_disabled_skill_fails_closed(eng, project):
    with eng.begin() as c:
        _skill(c, project, "off.skill", risk_tier=0, enabled=False)
        step = custom_steps.create(
            c, project_id=project, label="Off", binding_kind="skill",
            skill_id="off.skill", actor="test",
        )
    assert step["usable"] is False
    # The reason names the thing to fix. "Custom step is unusable" sends somebody hunting.
    assert "disabled" in step["unusable_reason"]


# ── Running one ────────────────────────────────────────────────────────────────

class Ctx:
    def __init__(self, engine):
        self._engine = engine
        # The grant resolver constructs the entitlement broker with this context's settings.
        # Passing None makes its fail-closed lookup return no spec, so a mocked skill invocation
        # can appear to succeed without ever exercising the caller grant under test.
        self.settings = Settings()

    def begin(self):
        return self._engine.begin()


def _publish_custom(c, project_id, key, step_key, *, mode="auto"):
    definition = workflows.create_definition(
        c, project_id=project_id, workflow_key=key, name=key, description=None,
        actor="test", namespace="product",
    )
    return workflows.publish_version(
        c, project_id=project_id, definition_id=definition["id"], actor="test",
        spec={"entry_step": "s", "steps": [
            {"id": "s", "kind": "capability", "next": [], "approval": {"mode": mode},
             "binding": {"custom_step": step_key, "inputs": {"ticket": "T-1"}}},
        ]},
    )


def _start(c, project_id, version_id):
    return workflows.start_run(
        c, project_id=project_id, actor="test", input_data={},
        dedupe_key=f"dk_{uuid.uuid4().hex[:10]}", workflow_version_id=version_id,
    )


def _step_row(c, run_id):
    return c.execute(
        text("SELECT step_id, status, output, last_error, decision_id FROM workflow_step_runs "
             "WHERE workflow_run_id=:r"),
        {"r": run_id},
    ).mappings().first()


def test_a_safe_custom_step_runs_through_the_governed_skill_path(eng, project, monkeypatch):
    """Not a direct call. Permission, budget and receipts live in SkillRuntimeService, and a
    second ungoverned way to call a customer's tools is what a workflow product must not grow."""
    seen = {}

    def fake_invoke(connection, settings, project_id, skill_id, caller, args, *, actor,
                    skill_spec=None, approved_by=None, approval_about=None):
        seen.update({"skill_id": skill_id, "caller": caller, "args": dict(args)})
        return {"status": "succeeded", "result": {"issue_url": "https://tracker/T-1"}}

    import boss_engine.services.agent_loop as agent_loop
    monkeypatch.setattr(agent_loop, "_invoke_agent_skill", fake_invoke)

    with eng.begin() as c:
        _skill(c, project, "tracker.file_issue", risk_tier=0)
        custom_steps.create(
            c, project_id=project, label="File issue", binding_kind="skill",
            skill_id="tracker.file_issue", actor="test", step_key="file_issue",
            inputs=[{"name": "ticket", "required": True}],
        )
        version = _publish_custom(c, project, "wf_run", "file_issue")
        run = _start(c, project, version["id"])
        runner.tick(c, Ctx(eng), project_id=project)
        row = _step_row(c, run["id"])

    assert seen["skill_id"] == "tracker.file_issue"
    assert seen["caller"] == custom_steps.RUNNER_CALLER
    assert seen["args"] == {"ticket": "T-1"}
    assert row["status"] == "succeeded"
    # Flat, because the next step reads it with {"from_step": …, "field": "issue_url"}.
    assert row["output"]["issue_url"] == "https://tracker/T-1"
    assert row["output"]["_step"]["custom_step"] == "file_issue"


def test_a_denied_skill_fails_the_step_instead_of_succeeding_with_a_denial(eng, project,
                                                                          monkeypatch):
    """The runtime denies by returning, not raising. Without translating that, a denial would
    be recorded as a successful step whose output happens to say "denied"."""
    import boss_engine.services.agent_loop as agent_loop
    monkeypatch.setattr(
        agent_loop, "_invoke_agent_skill",
        lambda *a, **k: {"status": "denied", "reason": "caller not permitted"},
    )
    with eng.begin() as c:
        _skill(c, project, "tracker.file_issue", risk_tier=0)
        custom_steps.create(
            c, project_id=project, label="File issue", binding_kind="skill",
            skill_id="tracker.file_issue", actor="test", step_key="file_issue",
        )
        version = _publish_custom(c, project, "wf_denied", "file_issue")
        run = _start(c, project, version["id"])
        runner.tick(c, Ctx(eng), project_id=project)
        row = _step_row(c, run["id"])

    assert row["status"] != "succeeded"
    assert "caller not permitted" in (row["last_error"] or "")


def test_a_dangerous_custom_step_waits_for_a_person_before_it_runs(eng, project, monkeypatch):
    called = []
    import boss_engine.services.agent_loop as agent_loop
    monkeypatch.setattr(
        agent_loop, "_invoke_agent_skill",
        lambda *a, **k: called.append(1) or {"status": "succeeded", "result": {}},
    )
    with eng.begin() as c:
        _skill(c, project, "prod.reset", risk_tier=3)
        custom_steps.create(
            c, project_id=project, label="Reset prod", binding_kind="skill",
            skill_id="prod.reset", actor="test", step_key="reset_prod",
        )
        version = _publish_custom(c, project, "wf_gate", "reset_prod", mode="manual")
        run = _start(c, project, version["id"])
        runner.tick(c, Ctx(eng), project_id=project)
        row = _step_row(c, run["id"])

    assert row["status"] == "waiting", "a tier-3 step ran without anybody agreeing to it"
    assert row["decision_id"], "it waited, but nobody was asked"
    assert not called, "the tool was called before the approval"


def test_approving_the_gate_actually_lets_the_step_run(eng, project, monkeypatch):
    """The other half, and the one that was missing. A gate that opens, is approved, and then
    never resumes is worse than no gate: the work silently never happens."""
    called = []

    def fake_invoke(connection, settings, project_id, skill_id, caller, args, *, actor,
                    skill_spec=None, approved_by=None, approval_about=None):
        called.append(skill_id)
        return {"status": "succeeded", "result": {"reset": True}}

    import boss_engine.services.agent_loop as agent_loop
    monkeypatch.setattr(agent_loop, "_invoke_agent_skill", fake_invoke)

    with eng.begin() as c:
        _skill(c, project, "prod.reset", risk_tier=3)
        custom_steps.create(
            c, project_id=project, label="Reset prod", binding_kind="skill",
            skill_id="prod.reset", actor="test", step_key="reset_prod",
        )
        version = _publish_custom(c, project, "wf_approved", "reset_prod", mode="manual")
        run = _start(c, project, version["id"])
        runner.tick(c, Ctx(eng), project_id=project)          # opens the gate
        row = _step_row(c, run["id"])
        assert row["status"] == "waiting"

        c.execute(text("UPDATE decisions SET status='approved' WHERE id=:d"),
                  {"d": row["decision_id"]})
        runner.tick(c, Ctx(eng), project_id=project)          # releases it
        runner.tick(c, Ctx(eng), project_id=project)          # runs it
        row = _step_row(c, run["id"])

    assert called == ["prod.reset"]
    assert row["status"] == "succeeded"
    assert row["output"]["reset"] is True
    # The tier the step ACTUALLY ran at, which a tool returning its own `risk_tier` must not
    # be able to overwrite — the evidence would otherwise understate what needed approving.
    assert row["output"]["_step"]["risk_tier"] == 3


def test_a_rejected_gate_blocks_the_run(eng, project, monkeypatch):
    import boss_engine.services.agent_loop as agent_loop
    monkeypatch.setattr(
        agent_loop, "_invoke_agent_skill",
        lambda *a, **k: {"status": "succeeded", "result": {}},
    )
    with eng.begin() as c:
        _skill(c, project, "prod.reset", risk_tier=3)
        custom_steps.create(
            c, project_id=project, label="Reset prod", binding_kind="skill",
            skill_id="prod.reset", actor="test", step_key="reset_prod",
        )
        version = _publish_custom(c, project, "wf_rejected", "reset_prod", mode="manual")
        run = _start(c, project, version["id"])
        runner.tick(c, Ctx(eng), project_id=project)
        row = _step_row(c, run["id"])
        c.execute(text("UPDATE decisions SET status='rejected' WHERE id=:d"),
                  {"d": row["decision_id"]})
        runner.tick(c, Ctx(eng), project_id=project)
        status = c.execute(text("SELECT status FROM workflow_runs WHERE id=:r"),
                           {"r": run["id"]}).scalar()
    assert status == "blocked"


def test_a_step_whose_skill_was_disabled_after_publishing_refuses_to_run(eng, project,
                                                                        monkeypatch):
    """Re-resolved at run time, not trusted from publish time. The morning the step finally
    runs is the moment that matters."""
    import boss_engine.services.agent_loop as agent_loop
    monkeypatch.setattr(
        agent_loop, "_invoke_agent_skill",
        lambda *a, **k: {"status": "succeeded", "result": {}},
    )
    with eng.begin() as c:
        _skill(c, project, "tracker.file_issue", risk_tier=0)
        custom_steps.create(
            c, project_id=project, label="File issue", binding_kind="skill",
            skill_id="tracker.file_issue", actor="test", step_key="file_issue",
        )
        version = _publish_custom(c, project, "wf_stale", "file_issue")
        run = _start(c, project, version["id"])
        c.execute(text("UPDATE project_skills SET enabled=false "
                       "WHERE project_id=:p AND skill_id='tracker.file_issue'"),
                  {"p": project})
        runner.tick(c, Ctx(eng), project_id=project)
        row = _step_row(c, run["id"])

    assert row["status"] != "succeeded"
    assert "disabled" in (row["last_error"] or "")


def test_a_tools_own_output_cannot_overwrite_the_steps_recorded_risk(eng, project,
                                                                    monkeypatch):
    """`toolchain.command.guard` returns its own `risk_tier` — its verdict on the command, not
    the step's. Spreading the two together made the evidence say tier 0 for a step that had
    just stopped for an approval at tier 2."""
    import boss_engine.services.agent_loop as agent_loop
    monkeypatch.setattr(
        agent_loop, "_invoke_agent_skill",
        lambda *a, **k: {"status": "succeeded",
                         "result": {"risk_tier": 0, "label": "something else",
                                    "decision": "allow"}},
    )
    with eng.begin() as c:
        _skill(c, project, "guard.it", risk_tier=3)
        custom_steps.create(
            c, project_id=project, label="Guard it", binding_kind="skill",
            skill_id="guard.it", actor="test", step_key="guard_it",
        )
        version = _publish_custom(c, project, "wf_collide", "guard_it", mode="manual")
        run = _start(c, project, version["id"])
        runner.tick(c, Ctx(eng), project_id=project)
        row = _step_row(c, run["id"])
        c.execute(text("UPDATE decisions SET status='approved' WHERE id=:d"),
                  {"d": row["decision_id"]})
        runner.tick(c, Ctx(eng), project_id=project)
        runner.tick(c, Ctx(eng), project_id=project)
        row = _step_row(c, run["id"])

    assert row["status"] == "succeeded"
    # The tool's own fields survive, because downstream steps read them.
    assert row["output"]["risk_tier"] == 0
    assert row["output"]["decision"] == "allow"
    # And the step's real tier is still recorded, unclobbered.
    assert row["output"]["_step"]["risk_tier"] == 3
    assert row["output"]["_step"]["label"] == "Guard it"


def test_defining_a_step_is_what_grants_the_runner_permission_to_call_the_skill(eng, project,
                                                                               monkeypatch):
    """Skills declare who may call them, and none of them listed a runner that did not exist
    when they were written. Without the grant every custom step is denied on its first run and
    the feature is a demo — so defining the step, by name, in this project, IS the grant."""
    passed = {}

    def fake_invoke(connection, settings, project_id, skill_id, caller, args, *, actor,
                    skill_spec=None, approved_by=None, approval_about=None):
        passed["spec"] = skill_spec
        passed["caller"] = caller
        return {"status": "succeeded", "result": {}}

    import boss_engine.services.agent_loop as agent_loop
    import boss_engine.services.entitlements as entitlements
    monkeypatch.setattr(agent_loop, "_invoke_agent_skill", fake_invoke)
    monkeypatch.setattr(
        entitlements.EntitlementBrokerService, "get_skill",
        lambda self, skill_id, project_id=None, *, connection=None: {
            "id": skill_id, "permissions": {"allowed_callers": ["agent_loop"]},
        },
    )

    with eng.begin() as c:
        _skill(c, project, "tracker.file_issue", risk_tier=0)
        custom_steps.create(
            c, project_id=project, label="File issue", binding_kind="skill",
            skill_id="tracker.file_issue", actor="test", step_key="file_issue",
        )
        version = _publish_custom(c, project, "wf_grant", "file_issue")
        run = _start(c, project, version["id"])
        runner.tick(c, Ctx(eng), project_id=project)
        row = _step_row(c, run["id"])

    assert row["status"] == "succeeded"
    assert passed["caller"] == "workflow_runner"
    # WIDENED, not replaced: the callers the skill already trusted are still there.
    assert passed["spec"]["permissions"]["allowed_callers"] == ["agent_loop", "workflow_runner"]


def test_the_grant_widens_who_may_call_and_never_what_may_be_done(eng, project, monkeypatch):
    """The one thing that would make the grant dangerous. Everything except the caller list
    must reach the runtime exactly as the skill registry wrote it."""
    passed = {}
    registered = {
        "id": "prod.reset",
        "permissions": {"allowed_callers": ["agent_loop"], "required_permissions": ["deploy"]},
        "risk_tier": 3,
        "cost": {"category": "skill", "estimator": "fixed"},
        "policy": {"requires_receipt": True},
        "data_boundary": "restricted",
    }

    import boss_engine.services.agent_loop as agent_loop
    import boss_engine.services.entitlements as entitlements
    monkeypatch.setattr(
        agent_loop, "_invoke_agent_skill",
        lambda *a, **k: passed.update(spec=k.get("skill_spec")) or {
            "status": "succeeded", "result": {}},
    )
    monkeypatch.setattr(
        entitlements.EntitlementBrokerService, "get_skill",
        lambda self, skill_id, project_id=None, *, connection=None: dict(registered),
    )

    with eng.begin() as c:
        _skill(c, project, "prod.reset", risk_tier=3)
        custom_steps.create(
            c, project_id=project, label="Reset prod", binding_kind="skill",
            skill_id="prod.reset", actor="test", step_key="reset_prod",
        )
        version = _publish_custom(c, project, "wf_scope", "reset_prod", mode="manual")
        run = _start(c, project, version["id"])
        runner.tick(c, Ctx(eng), project_id=project)
        row = _step_row(c, run["id"])
        c.execute(text("UPDATE decisions SET status='approved' WHERE id=:d"),
                  {"d": row["decision_id"]})
        runner.tick(c, Ctx(eng), project_id=project)
        runner.tick(c, Ctx(eng), project_id=project)

    spec = passed["spec"]
    for field in ("risk_tier", "cost", "policy", "data_boundary"):
        assert spec[field] == registered[field], f"{field} was altered by the grant"
    assert spec["permissions"]["required_permissions"] == ["deploy"]


# ── Removing a workflow ────────────────────────────────────────────────────────

def test_a_workflow_that_never_ran_is_deleted_outright(eng, project):
    """A mistake should leave nothing behind."""
    from boss_engine.services.workflows import delete_definition

    with eng.begin() as c:
        d = workflows.create_definition(
            c, project_id=project, workflow_key="gone", name="Gone", description=None,
            actor="test", namespace="product",
        )
        workflows.publish_version(
            c, project_id=project, definition_id=d["id"], actor="test",
            spec={"entry_step": "s", "steps": [
                {"id": "s", "kind": "capability", "next": [], "approval": {"mode": "manual"},
                 "binding": {"capability": "spec_review"}}]},
        )
        result = delete_definition(c, project, d["id"], "test")
        left = c.execute(text("SELECT count(*) FROM workflow_definitions WHERE id=:i"),
                         {"i": d["id"]}).scalar()

    assert result["outcome"] == "deleted"
    assert result["runs"] == 0
    assert left == 0


def test_a_workflow_that_has_run_is_retired_so_its_history_survives(eng, project):
    """`workflow_versions` cascades from the definition, so deleting one with history would
    take the specs with it — and a finished run whose spec has vanished cannot be read back."""
    from boss_engine.services.workflows import delete_definition

    with eng.begin() as c:
        d = workflows.create_definition(
            c, project_id=project, workflow_key="ran", name="Ran", description=None,
            actor="test", namespace="product",
        )
        v = workflows.publish_version(
            c, project_id=project, definition_id=d["id"], actor="test",
            spec={"entry_step": "s", "steps": [
                {"id": "s", "kind": "capability", "next": [], "approval": {"mode": "manual"},
                 "binding": {"capability": "spec_review"}}]},
        )
        run = _start(c, project, v["id"])
        # A FINISHED run. A live one is refused outright — that is the next test.
        c.execute(text("UPDATE workflow_runs SET status='completed' WHERE id=:r"),
                  {"r": run["id"]})
        result = delete_definition(c, project, d["id"], "test")

        status = c.execute(text("SELECT status FROM workflow_definitions WHERE id=:i"),
                           {"i": d["id"]}).scalar()
        # The run, and the spec it pins, are both still readable.
        spec_rows = c.execute(text("SELECT count(*) FROM workflow_versions "
                                   "WHERE workflow_definition_id=:i"), {"i": d["id"]}).scalar()
        run_rows = c.execute(text("SELECT count(*) FROM workflow_runs WHERE id=:r"),
                             {"r": run["id"]}).scalar()
        # And it is gone from the builder, which lists non-retired definitions.
        listed = [x["id"] for x in workflows.list_definitions(c, project, namespace="product")]

    assert result["outcome"] == "retired"
    assert status == "retired"
    assert spec_rows == 1 and run_rows == 1
    assert d["id"] not in listed


def test_a_standard_workflow_cannot_be_deleted(eng, project):
    from boss_engine.services.workflows import delete_definition

    with eng.begin() as c:
        d = workflows.create_definition(
            c, project_id=project, workflow_key="std", name="Std", description=None,
            actor="test", namespace="product", is_catalogue=True,
        )
        with pytest.raises(Exception) as error:
            delete_definition(c, project, d["id"], "test")
    assert "cannot be edited in place" in str(error.value)


def test_a_workflow_with_a_live_run_refuses_to_go(eng, project):
    """Deleting the definition under a live run strands it: the runtime reads the spec to
    decide what comes next, so it would be a run that cannot finish and cannot say why."""
    from boss_engine.services.workflows import delete_definition

    with eng.begin() as c:
        d = workflows.create_definition(
            c, project_id=project, workflow_key="live", name="Live", description=None,
            actor="test", namespace="product",
        )
        v = workflows.publish_version(
            c, project_id=project, definition_id=d["id"], actor="test",
            spec={"entry_step": "s", "steps": [
                {"id": "s", "kind": "capability", "next": [], "approval": {"mode": "manual"},
                 "binding": {"capability": "spec_review"}}]},
        )
        _start(c, project, v["id"])
        c.execute(text("UPDATE workflow_runs SET status='running' "
                       "WHERE workflow_definition_id=:i"), {"i": d["id"]})
        with pytest.raises(Exception) as error:
            delete_definition(c, project, d["id"], "test")
    # The count, because "cannot delete" is not actionable and "it has 1 run still going" is.
    assert "still going" in str(error.value)
