"""Every field a workflow step can name an agent in protects that agent from being deleted.

THE INCIDENT (review of milestone/agents, round 1). The delete guard read a step's `captain`,
`binding.agent` and `binding.agents`. A gate names its approver in `approval.approver_role`
(the catalogue gates name release_manager, product_owner, design_lead and tech_lead there), and
a binding can name a role in `binding.role_key`, which start_run refuses when that role is
missing. A probe published a workflow naming a fresh agent in each place and DELETE
/v1/agents/<key> answered 200 both times. The delete also tombstones the key, so no seed brings
the agent back and the gate stays broken.

ENFORCED BY ENUMERATION. This scans the engine for every step field read as an agent (any
`step`/`binding`/`approval` key whose name says role, agent, captain or approver) and fails
until each is in agents.STEP_AGENT_FIELDS or exempted below with the reason it does not name an
agent on a workflow step.
"""
from __future__ import annotations

import re
from pathlib import Path

from boss_engine.services import agents

ENGINE = Path(__file__).resolve().parents[1] / "boss_engine"

_FIELD = r"[\"']([a-z_]*(?:role|agent|captain|approver)[a-z_]*)[\"']"
#: `step.get("captain")`, `binding["agents"]`, `approval.get("approver_role")` …
READ = re.compile(r"\b(step|binding|approval)(?:\.get\(\s*|\[\s*)" + _FIELD)
#: … and the inline form `(step.get("approval") or {}).get("approver_role")`.
READ_INLINE = re.compile(
    r"\(\s*step\.get\(\s*[\"'](binding|approval)[\"']\s*\)\s*or\s*\{\}\s*\)\.get\(\s*" + _FIELD)

#: (path, file) pairs that are read as an agent-ish field but are NOT an agent named on a
#: workflow step, with why. Keyed by file too, so a new reader of the same name in workflow
#: code is not waved through.
NOT_AN_AGENT_ON_A_STEP = {
    (("binding", "agent_work"), "services/workflows.py"): "a flag: the step is an instruction",
    (("binding", "agent_work"), "services/workflow_capability_runner.py"): "the same flag",
    # The same flag, read where the steps stream decides who runs a step, where the templates
    # stream checks readiness, and where draft_workflow builds an agent step.
    (("binding", "agent_work"), "services/step_agents.py"): "the same flag",
    (("binding", "agent_work"), "services/workflow_readiness.py"): "the same flag",
    (("binding", "agent_work"), "tools/builtin.py"): "the same flag",
    # Where a drafted agent step is built (the tool's draft and the editor's Describe).
    (("binding", "agent_work"), "services/workflow_drafting.py"): "the same flag",
    (("binding", "agent_work"), "services/command_center.py"):
        "the same flag, read to say BOSS will pick the agent when the step starts",
    # Agent World: read to name the step "Agent task" on its signed work, never as an agent.
    (("binding", "agent_work"), "services/agent_acts.py"): "the same flag",
    # Agents › Live: read to show a step BOSS will pick for as `Auto` (no face), never an agent.
    (("binding", "agent_work"), "services/agents_live_board.py"): "the same flag",
    (("binding", "agent_group_version_id"), "services/workflows.py"):
        "an agent GROUP version id, not a role key",
    (("role_key",), "services/agent_loop.py"):
        "the bounded run's own context (finalize_ctx.agent_step), built from the resolved step",
    (("role",), "services/agent_process.py"): "an agent PROCESS template step, not a workflow",
    (("roles",), "services/agent_process.py"): "an agent PROCESS template step, not a workflow",
    (("role",), "services/console_v2_platform.py"): "displays agent PROCESS template steps",
    (("roles",), "services/console_v2_platform.py"): "displays agent PROCESS template steps",
    # A MARK ON `captain`, not a second name: it counts only while it equals the captain, which
    # the guard reads. A step named only by it is a soft catalogue default — a delete re-staffs
    # it (agents.delete_agent → workflow_catalogue.restaff_without) instead of being refused.
    (("default_captain",), "services/step_agents.py"): "marks the captain as a soft default",
    (("default_captain",), "services/workflow_catalogue.py"): "sets and re-staffs that mark",
    # A conversation's start card reads the Library OUTLINE of a step (workflows._step_outline:
    # `agent` is the captain the guard already reads), never a workflow step of its own.
    (("agent",), "services/agent_converse.py"): "the library outline's copy of the captain",
}


def _reads() -> set[tuple[tuple[str, ...], str]]:
    found: set[tuple[tuple[str, ...], str]] = set()
    for path in ENGINE.rglob("*.py"):
        if "__pycache__" in str(path):
            continue
        rel = str(path.relative_to(ENGINE))
        source = path.read_text(encoding="utf-8")
        for container, field in READ.findall(source) + READ_INLINE.findall(source):
            found.add(((field,) if container == "step" else (container, field), rel))
    return found


def test_every_step_field_read_as_an_agent_is_known_to_the_delete_guard():
    covered = {path for path, _, _ in agents.STEP_AGENT_FIELDS}
    unknown = sorted(
        f"{'.'.join(path)} in {rel}" for path, rel in _reads()
        if path not in covered and (path, rel) not in NOT_AN_AGENT_ON_A_STEP)
    assert not unknown, (
        f"a step names an agent in {unknown}, and the delete guard does not read it — so an "
        "agent named there can be deleted from under the step. Add the field to "
        "agents.STEP_AGENT_FIELDS, or list it in NOT_AN_AGENT_ON_A_STEP with the reason.")


def test_the_scan_sees_the_fields_the_guard_reads():
    """A scan that finds nothing passes vacuously; it must see the known readers."""
    reads = _reads()
    seen = {path for path, _ in reads}
    for path in (("captain",), ("approval", "approver_role"), ("binding", "role_key")):
        assert path in seen, f"the scan no longer finds {'.'.join(path)}"
    # The inline form too — agent_assignment reads the approver that way.
    assert (("approval", "approver_role"), "services/agent_assignment.py") in reads


def test_a_gate_approver_and_a_binding_role_both_name_the_agent():
    step = {"id": "ship", "captain": "Builder",
            "binding": {"role_key": "auditor", "agents": ["a", " B "], "agent": "runner"},
            "approval": {"approver_role": "release_manager"}}
    assert agents._names_in_step(step) == {
        "builder": "runs it", "runner": "runs it", "auditor": "runs it",
        "a": "is on its shortlist", "b": "is on its shortlist",
        "release_manager": "approves it"}


def test_a_malformed_step_names_nobody_rather_than_failing():
    assert agents._names_in_step({"binding": "x", "approval": None, "captain": None}) == {}
