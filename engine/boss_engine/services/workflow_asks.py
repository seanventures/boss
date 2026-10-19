"""Every time the operator will be asked, per path — one server answer the console shows.

The composer told an operator what they would approve from its own copy of the rules, and the
copy was wrong in both directions: it never mentioned the merge approval every build raises
("Approve code change … approve to merge", software_factory), and it assumed the panel's PRD is
always approved automatically, which is a setting (BOSS_MEETING_PRD_AUTO_APPROVE). This reads
the same rules the runtime acts on:

- spec  — the panel's PRD decision (a `meeting` step, adopted by `spec_review` when there is
          one). BOSS answers it when the PRD is auto-approved, else you (prd_auto_approved).
- gate  — a step a person answers (stops_for_a_person), e.g. the door to production. A Deploy
          step is a gate for you unless it follows the environment's policy AND that policy
          ships without asking (deploy_policy.step_answer — the answer the runner acts on);
          then it is listed as BOSS's, with the policy named, and `says` carries the words.
- merge — the pull request a build opens. Always you: the merge decision is raised with
          `no_auto_merge: True` and nothing in BOSS approves it on anyone's behalf. Every
          build-like step of one run writes the SAME branch and PR, and a later attempt's merge
          question withdraws the earlier one (run_decisions.withdraw_superseded_pr_questions),
          so a run asks once — after its last build-like step.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .workflow_capabilities import CAPABILITIES, prd_auto_approved, stops_for_a_person

#: Capabilities that write code into a pull request (each a code task on the run's branch).
BUILD_CAPABILITIES = frozenset({"build", "retry_build", "plrr"})

SPEC_LABEL = "Approve the spec (the panel's PRD)"
MERGE_LABEL = "Approve the code change (merge its pull request)"


def _capability(step: dict[str, Any]) -> str:
    return str((step.get("binding") or {}).get("capability") or "")


def _label(step: dict[str, Any]) -> str:
    from .workflows import step_title

    titled = step_title(step)
    if titled:
        return titled
    key = _capability(step)
    if key in CAPABILITIES:
        return CAPABILITIES[key].label
    return str(step.get("label") or step.get("name") or step.get("id") or "")


def _effective_steps(spec: dict[str, Any],
                     roster: Optional[frozenset[str]] = None) -> list[dict[str, Any]]:
    """The steps with the approval each will REALLY run under (the risk floor applied).

    `roster` is the project's (workflows.project_roster): without it a step captained by an
    agent the operator created fails validation and its risk floor is never applied."""
    from . import workflows

    steps = [dict(s) for s in (spec.get("steps") or []) if isinstance(s, dict)]
    try:
        effective = workflows.validate_workflow_spec(
            {"entry_step": spec.get("entry_step") or (steps[0]["id"] if steps else ""),
             "steps": steps}, roster)
        return [dict(s) for s in effective.get("steps") or steps]
    except Exception:  # noqa: BLE001 — an unvalidatable spec still lists what it declares
        return steps


def spec_ask_step_ids(steps: list[dict[str, Any]]) -> frozenset[str]:
    """The steps that carry a workflow's "Approve the spec" ask: its `spec_review` steps, or,
    in a workflow without one, its `meeting` steps (the panel's own PRD decision is the ask).

    THE ONE SOURCE for both the promise and the behaviour. `asks_for_spec` lists these as the
    spec ask the start form shows ("Approve the spec — you read the spec before any code"), and
    the runner holds a `meeting` step listed here until its PRD decision is approved
    (`holds_for_spec`). Before this, the promise was made here and nothing kept it: Minor
    Feature v6 (meeting → build) told the operator they would read the spec first, its panel's
    PRD decision sat pending, and the build had already started (wfr_723213d8, 2026-10-19)."""
    keys = [_capability(s) for s in steps]
    wanted = "spec_review" if "spec_review" in keys else "meeting"
    return frozenset(str(s.get("id")) for s, key in zip(steps, keys) if key == wanted)


def holds_for_spec(steps: list[dict[str, Any]], step_id: str) -> bool:
    """Whether this step is a `meeting` that carries the spec ask, so it completes only once
    the panel's PRD decision is approved (by a person, or by BOSS when the PRD is
    auto-approved — the same decision either way)."""
    for step in steps:
        if str(step.get("id")) == str(step_id):
            return _capability(step) == "meeting" and str(step_id) in spec_ask_step_ids(steps)
    return False


def asks_for_spec(spec: dict[str, Any], *, prd_auto_approve: bool,
                  roster: Optional[frozenset[str]] = None,
                  deploy_answer: Optional[Callable[[dict[str, Any]], dict[str, Any]]] = None,
                  ) -> list[dict[str, Any]]:
    """The asks one workflow version will raise, in run order.

    `deploy_answer(step)` is deploy_policy.step_answer bound to the project; without one (no
    project to read a policy from) every Deploy step is asked, as it is where no policy is set.
    """
    from .deploy_policy import PERSON_SAYS, promises_a_person
    steps = _effective_steps(spec, roster)
    keys = [_capability(s) for s in steps]
    spec_asks = spec_ask_step_ids(steps)
    last_build = max((i for i, k in enumerate(keys) if k in BUILD_CAPABILITIES), default=-1)
    spec_who = "boss" if prd_auto_approve else "you"
    out: list[dict[str, Any]] = []
    for index, step in enumerate(steps):
        key = keys[index]
        mode = (step.get("approval") or {}).get("mode")
        if key == "deploy":
            answer = deploy_answer(step) if deploy_answer else {"who": "you",
                                                                 "says": PERSON_SAYS}
            ask = {"step_id": step["id"], "label": _label(step), "kind": "gate",
                   "who": "you" if answer.get("who") != "policy" else "boss",
                   "says": answer.get("says") or PERSON_SAYS,
                   "promised": promises_a_person(step)}
            if answer.get("who") == "policy":
                ask["policy"] = answer.get("environment")
            out.append(ask)
        elif str(step.get("id")) in spec_asks:
            out.append({"step_id": step["id"], "label": SPEC_LABEL, "kind": "spec",
                        "who": spec_who})
        elif mode == "manual" and (key not in CAPABILITIES
                                   or stops_for_a_person(key, mode,
                                                         prd_auto_approve=prd_auto_approve)):
            out.append({"step_id": step["id"], "label": _label(step), "kind": "gate",
                        "who": "you"})
        if index == last_build:
            out.append({"step_id": step["id"], "label": MERGE_LABEL, "kind": "merge",
                        "who": "you"})
    return out


def route_asks(*, prd_auto_approve: bool) -> dict[str, list[dict[str, Any]]]:
    """The two paths that are not workflows. A quick change is a proposal you confirm
    (feedback_agent.confirm_feedback) and then a build; spec-first is a panel whose PRD is
    approved under the same rule as a workflow's, and then a build."""
    merge = {"step_id": "merge", "label": MERGE_LABEL, "kind": "merge", "who": "you"}
    return {
        "@quick": [{"step_id": "confirm", "label": "Confirm the proposed change",
                    "kind": "gate", "who": "you"}, dict(merge)],
        "@spec": [{"step_id": "spec", "label": SPEC_LABEL, "kind": "spec",
                   "who": "boss" if prd_auto_approve else "you"}, dict(merge)],
    }


def project_asks(connection: Connection, settings: Any, project_id: str,
                 *, prd_auto_approve: Optional[bool] = None) -> dict[str, Any]:
    """GET /v1/delivery-workflows/asks: every workflow the project can run, by the version
    that runs (the active one), plus the two non-workflow routes."""
    from .deploy_policy import step_answer
    from .workflows import project_roster, running_version

    auto = prd_auto_approved(settings) if prd_auto_approve is None else bool(prd_auto_approve)
    keys = connection.execute(text(
        "SELECT DISTINCT workflow_key FROM workflow_definitions WHERE project_id = :p "
        "AND status <> 'retired' ORDER BY workflow_key"),
        {"p": project_id}).scalars().all()
    roster = project_roster(connection, project_id)
    workflows: dict[str, list[dict[str, Any]]] = {}
    for key in keys:
        running = running_version(connection, project_id, str(key))
        if not running:
            continue
        spec = running.get("spec") or {}
        if isinstance(spec, str):
            import json

            try:
                spec = json.loads(spec)
            except ValueError:
                spec = {}
        workflows[str(key)] = asks_for_spec(
            spec, prd_auto_approve=auto, roster=roster,
            deploy_answer=lambda step: step_answer(connection, project_id, step))
    return {"project_id": project_id, "prd_auto_approve": auto, "workflows": workflows,
            "routes": route_asks(prd_auto_approve=auto)}
