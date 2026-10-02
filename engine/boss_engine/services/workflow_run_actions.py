"""What an operator may do to a workflow run, decided ONCE, here, from the run's real state.

THE INCIDENT. A cancelled Standard Product Delivery run whose research had FAILED still offered
"Build on this research →". The product owner, looking at the red ✗ on Research and the button
beneath it: "Does it make sense to have Build on this research?" It did not — there was no
research to build on — and the button existed because the console decided it with its own
condition: "is any step succeeded with a discovery id in its output". A research step from
before the runner stopped forgiving failures is exactly that: recorded `succeeded`, output
`failed_continued`, discovery id present, discovery dead.

That was one of a family. Every workflow-management button on the run card was decided by a
separate ad-hoc condition in delivery-workflows-ui.tsx, each a private copy of a rule the engine
already enforces somewhere else, and they disagreed with the engine in exactly the places that
hurt:

* "Remove this run" on a `blocked` run — the console's "not active" included `blocked`, and
  `delete_run` refuses anything that is not finished, so the button could only ever fail.
* No "Stop this run" on a `blocked` run, which `cancel_run` would have stopped.
* "Skip and proceed" on a failed step of a CANCELLED run, which `skip_failed_step` refuses.
* "Try … again" on a cancelled run's steps, which revives a run the Stop button had just
  promised "cannot be resumed".

THE RULE NOW. The API attaches `actions` to every run it serializes for the console, computed
by `run_actions` below from the run's status, its steps and — for research — the dossier
itself. The console renders exactly that list: a button per action, disabled with the reason
as its help when `enabled` is false, and nothing it was not given. It holds no opinion of its
own about which buttons a run should have.

What appears, and what does not:

* An action that does not APPLY to this run is absent — "Build on this research" on a run whose
  research failed is not greyed out, it is not there, because there is nothing to build on.
* An action that applies but cannot be taken RIGHT NOW is present and disabled with a sentence
  saying why — research that is still being written up, a run that is busy compensating.

`research_to_build_on` is shared with the start endpoint's `from_run`, so the button and the
action it triggers can never disagree about whether a run researched anything.
"""
from __future__ import annotations

import json
from typing import Any, Iterable, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

#: A run still doing, or still able to do, work: Stop applies, Remove does not. `blocked` is
#: here because `cancel_run` stops it and `delete_run` refuses it — the console's private list
#: left it out and so offered the one button that could not work and hid the one that could.
ACTIVE_RUN_STATES = frozenset({"queued", "validating", "running", "waiting", "paused", "blocked"})

#: A run that is over. Mirrors `workflows.TERMINAL_RUN_STATES`, which `delete_run` reads; a test
#: holds the two equal.
FINISHED_RUN_STATES = frozenset({"completed", "failed", "deadlettered", "cancelled", "compensated"})

#: A run undoing what it did. Neither stoppable (`cancel_run` would race the compensation) nor
#: removable (`delete_run` refuses it) until that finishes.
COMPENSATING = "compensating"

#: The step states "Try … again" applies to. `workflows.OPERATOR_RECOVERABLE_STEP_STATES`.
RETRYABLE_STEP_STATES = frozenset({"failed", "deadlettered", "cancelled"})

#: The step states "Skip and proceed" applies to. `workflows.SKIPPABLE_STEP_STATES`.
SKIPPABLE_STEP_STATES = frozenset({"failed", "deadlettered"})

#: Step output statuses that mean the work FAILED even though the row says `succeeded`. The
#: runner wrote `failed_continued` for research that died until boss-1 (2026-10-02); those rows
#: are still in the database and are the exact shape of the incident above.
FAILED_OUTPUT_STATUSES = frozenset({"failed", "failed_continued", "not_applicable"})

#: The dossier sections that are FINDINGS, as opposed to the question asked or the verdict on
#: it. A completed discovery with none of these researched nothing anybody could build on.
#: Section names from `workflow_capability_runner._DOSSIER_SECTIONS`, taken from a real run.
DOSSIER_FINDINGS = ("signal_findings", "market_analysis", "candidate_outcomes")

#: The workflow "Build on this research" starts. Named rather than looked up by class: see
#: onBuildOnThis in the console.
BUILD_WORKFLOW_KEY = "standard_product_delivery"

#: Every action key this module can produce. The console holds an explanation per key and a
#: test holds the two lists equal, so a new action cannot ship without its help text.
RUN_ACTION_KEYS = ("recheck", "stop", "stop_and_rerun", "rerun", "remove", "build_on_research")
STEP_ACTION_KEYS = ("retry_step", "skip_step")


def _action(key: str, label: str, *, enabled: bool = True, reason: Optional[str] = None,
            step_id: Optional[str] = None) -> dict[str, Any]:
    out: dict[str, Any] = {"key": key, "label": label, "enabled": bool(enabled),
                           "reason": None if enabled else (reason or "not available right now")}
    if step_id is not None:
        out["step_id"] = step_id
    return out


def _output(step: dict[str, Any]) -> dict[str, Any]:
    out = step.get("output")
    return out if isinstance(out, dict) else {}


def step_capability(step: dict[str, Any]) -> str:
    """The capability a step runs, from whichever shape the caller serialized.

    The console's list endpoint resolves it from the spec into `capability`; `get_run` carries
    the binding snapshot instead. Either is the contract — never the step's id, which a customer
    may rename.
    """
    named = step.get("capability")
    if isinstance(named, str) and named:
        return named
    snapshot = step.get("binding_snapshot")
    if isinstance(snapshot, dict):
        binding = snapshot.get("binding")
        if isinstance(binding, dict) and isinstance(binding.get("capability"), str):
            return binding["capability"]
    return ""


def _is_gate(step: dict[str, Any]) -> bool:
    """An approval: a decision step, or a capability declared `is_gate`. Same test as
    `workflows._is_gate_step`, read off the serialized step rather than the spec."""
    if (step.get("kind") or step.get("step_kind")) == "decision":
        return True
    from .workflow_capabilities import CAPABILITY_KEYS, get as get_capability

    key = step_capability(step)
    return key in CAPABILITY_KEYS and bool(get_capability(key).is_gate)


def _step_label(step: dict[str, Any]) -> str:
    from .workflow_capabilities import CAPABILITY_KEYS, get as get_capability

    key = step_capability(step)
    if key in CAPABILITY_KEYS:
        return get_capability(key).label
    return str(step.get("step_id") or key or "this step")


def _discovery_of(step: dict[str, Any]) -> str:
    """The discovery a step started or adopted — by what it produced, not what it is called."""
    produced = _output(step)
    found = produced.get("discovery_id")
    if isinstance(found, str) and found.strip():
        return found.strip()
    awaited = produced.get("await")
    if isinstance(awaited, dict) and awaited.get("kind") == "discovery" and awaited.get("id"):
        return str(awaited["id"]).strip()
    return ""


def _is_research_step(step: dict[str, Any]) -> bool:
    return step_capability(step) == "research" or bool(_discovery_of(step))


def _research_succeeded(step: dict[str, Any]) -> bool:
    """The step's work succeeded — the row's status AND what the work itself reported."""
    if step.get("status") != "succeeded":
        return False
    return str(_output(step).get("status") or "") not in FAILED_OUTPUT_STATUSES


def research_discovery_ids(steps: Iterable[dict[str, Any]], run_input: Any) -> list[str]:
    """Every discovery a run's research might have produced, for one batched lookup."""
    ids = {_discovery_of(step) for step in steps}
    adopted = (run_input or {}).get("adopt_discovery_run_id") if isinstance(run_input, dict) else None
    if isinstance(adopted, str) and adopted.strip():
        ids.add(adopted.strip())
    ids.discard("")
    return sorted(ids)


def research_to_build_on(
    steps: list[dict[str, Any]], run_input: Any, discoveries: dict[str, dict[str, Any]],
) -> tuple[Optional[str], Optional[str], bool]:
    """The dossier this run's research produced, if there is one worth building on.

    Returns `(discovery_id, why_not, applies)`:

    * `(id, None, True)` — research succeeded and its dossier is complete and has findings.
    * `(None, why, True)` — research succeeded, but the dossier is not ready yet (still being
      written). The action applies and is disabled with `why`.
    * `(None, why, False)` — there is nothing to build on: no research step, research that
      failed, was skipped or cancelled, or a dossier that finished empty or failed.

    `discoveries` maps a discovery id to `{"project_id", "status", "has_findings"}` — see
    `load_discoveries`. An id missing from it is a dossier that does not exist in this project.
    """
    research = [step for step in steps if _is_research_step(step)]
    candidates: list[str] = []
    if research:
        # The most recent successful attempt wins — a retried research step leaves the earlier
        # failure in the list, and the later success is the one that counts.
        done = [step for step in research if _research_succeeded(step) and _discovery_of(step)]
        done.sort(key=lambda step: str(step.get("completed_at") or ""), reverse=True)
        candidates = [_discovery_of(step) for step in done]
        if not candidates:
            return None, "this run's research did not succeed, so there is nothing to build on", False
    else:
        # A run with no research step of its own can still carry one it adopted at start.
        adopted = (run_input or {}).get("adopt_discovery_run_id") if isinstance(run_input, dict) else None
        if isinstance(adopted, str) and adopted.strip():
            candidates = [adopted.strip()]
        else:
            return None, "this run did not research anything", False
    discovery_id = candidates[0]
    facts = discoveries.get(discovery_id)
    if not facts:
        return None, "the research this run produced is not in this project", False
    status = str(facts.get("status") or "")
    if status == "running":
        return None, "the research is still being written up — this is offered when it finishes", True
    if status != "completed":
        return None, f"the research finished as '{status}', so there is nothing to build on", False
    if not facts.get("has_findings"):
        return None, "the research completed with no findings, so there is nothing to build on", False
    return discovery_id, None, True


def run_actions(
    run: dict[str, Any], steps: list[dict[str, Any]],
    discoveries: Optional[dict[str, dict[str, Any]]] = None,
) -> list[dict[str, Any]]:
    """The actions the console offers on one run, in the order it shows them.

    Pure: everything it reads is passed in, so the whole rule set is a table test
    (engine/tests/test_a_runs_actions_come_from_its_state.py).
    """
    status = str(run.get("status") or "")
    active = status in ACTIVE_RUN_STATES
    finished = status in FINISHED_RUN_STATES
    compensating = status == COMPENSATING
    actions: list[dict[str, Any]] = []

    # RE-CHECK: re-tests the recorded blocker of every failed step and re-queues the ones whose
    # blocker is gone. Only where a step HAS a recorded failure, and never on a run somebody
    # stopped or one that finished — re-queueing a step of a cancelled run would quietly revive
    # it, and a completed run has nothing blocked.
    if status not in {"completed", "cancelled", "compensated"} and not compensating \
            and any(step.get("status") in RETRYABLE_STEP_STATES for step in steps):
        actions.append(_action("recheck", "Re-check this workflow"))

    if active:
        actions.append(_action("stop", "Stop this run"))
        actions.append(_action("stop_and_rerun", "Stop and run again"))
    elif compensating:
        why = "it is undoing what it did — wait for that to finish"
        actions.append(_action("stop", "Stop this run", enabled=False, reason=why))
    if finished:
        actions.append(_action("rerun", "Run this workflow again"))
        actions.append(_action("remove", "Remove this run"))
    elif compensating:
        actions.append(_action("remove", "Remove this run", enabled=False,
                               reason="it is undoing what it did — it can be removed after"))

    # BUILD ON THIS RESEARCH: a finished run whose research SUCCEEDED and left a completed
    # dossier with findings. Not on a live run — a second workflow on the same directive while
    # this one is still going is the duplicate "Stop and run again" exists to prevent.
    if finished:
        _, why_not, applies = research_to_build_on(steps, run.get("input"), discoveries or {})
        if applies:
            actions.append(_action("build_on_research", "Build on this research →",
                                   enabled=why_not is None, reason=why_not))

    # PER STEP: Try again, and Skip and proceed. One pair per step that needs a decision.
    for step in steps:
        step_status = str(step.get("status") or "")
        step_id = str(step.get("step_id") or "")
        if step_status not in RETRYABLE_STEP_STATES or not step_id:
            continue
        name = _step_label(step)
        blocked_by = None
        if status == "cancelled":
            # Stop promises "it cannot be resumed; start it again to try afresh". Retrying a
            # step here would resume it — `retry_failed_step` revives the run to `running`.
            blocked_by = ("this run was stopped, and a stopped run is not resumed — "
                          "run the workflow again to try afresh")
        elif status in {"compensating", "compensated"}:
            blocked_by = "this run is undoing what it did, so nothing in it runs again"
        elif status == "completed":
            blocked_by = "this run has finished"
        actions.append(_action("retry_step", f"Try {name} again", enabled=blocked_by is None,
                               reason=blocked_by, step_id=step_id))
        # Skip is narrower: never an approval (a refused gate is somebody's no, and a skip
        # would turn it into a yes), never a cancelled step. `workflows.skip_failed_step`.
        if step_status in SKIPPABLE_STEP_STATES and not _is_gate(step):
            actions.append(_action("skip_step", "Skip and proceed", enabled=blocked_by is None,
                                   reason=blocked_by, step_id=step_id))
    return actions


def load_discoveries(
    connection: Connection, project_id: str, ids: Iterable[str],
) -> dict[str, dict[str, Any]]:
    """The facts `research_to_build_on` needs about each dossier, in one query."""
    wanted = sorted({i for i in ids if i})
    if not wanted:
        return {}
    rows = connection.execute(
        text("SELECT id, project_id, status, state FROM discovery_runs "
             "WHERE id = ANY(:ids) AND project_id = :p"),
        {"ids": wanted, "p": project_id},
    ).mappings().all()
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        state = row["state"]
        if isinstance(state, str):
            try:
                state = json.loads(state)
            except ValueError:
                state = {}
        state = state if isinstance(state, dict) else {}
        out[row["id"]] = {
            "project_id": row["project_id"],
            "status": row["status"],
            "has_findings": any(state.get(key) for key in DOSSIER_FINDINGS),
        }
    return out


def attach_actions(
    connection: Connection, project_id: str, runs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Set `actions` on each serialized run (each must carry `status`, `input`, `steps`)."""
    ids: set[str] = set()
    for run in runs:
        ids.update(research_discovery_ids(run.get("steps") or [], run.get("input")))
    discoveries = load_discoveries(connection, project_id, ids)
    for run in runs:
        run["actions"] = run_actions(run, run.get("steps") or [], discoveries)
    return runs


__all__ = [
    "ACTIVE_RUN_STATES", "FINISHED_RUN_STATES", "RUN_ACTION_KEYS", "STEP_ACTION_KEYS",
    "BUILD_WORKFLOW_KEY", "attach_actions", "load_discoveries", "research_discovery_ids",
    "research_to_build_on", "run_actions", "step_capability",
]
