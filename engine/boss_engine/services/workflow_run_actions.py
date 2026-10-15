"""What an operator may do to a workflow run, decided ONCE, here, from the run's real state —
and enforced from the same place.

THE INCIDENT. A cancelled Standard Product Delivery run whose research had FAILED still offered
"Build on this research →". The product owner, looking at the red ✗ on Research and the button
beneath it: "Does it make sense to have Build on this research?" It did not — there was no
research to build on — and the button existed because the console decided it with its own
condition: "is any step succeeded with a discovery id in its output". A research step from
before the runner stopped forgiving failures is exactly that: recorded `succeeded`, output
`failed_continued`, discovery id present, discovery dead.

That was one of a family. Every workflow-management button on the run card was decided by a
separate ad-hoc condition in delivery-workflows-ui.tsx, each a private copy of a rule the engine
already enforced somewhere else, and they disagreed with the engine in exactly the places that
hurt:

* "Remove this run" on a `blocked` run — the console's "not active" included `blocked`, and
  `delete_run` refuses anything that is not finished, so the button could only ever fail.
* No "Stop this run" on a `blocked` run, which `cancel_run` would have stopped.
* "Skip and proceed" on a failed step of a CANCELLED run, which `skip_failed_step` refuses.
* "Try … again" on a cancelled run's steps, which revives a run the Stop button had just
  promised "cannot be resumed".

THE FIRST FIX MOVED THE BUTTONS AND LEFT THE ACTIONS ALONE. The list was computed here, but the
endpoints kept their own preconditions: Try again was hidden on a cancelled run while
POST retry-step, `retry_failed_step` and the agent's `retry_workflow_step` all still accepted it;
Skip's "is this an approval" test was a hand copy of `workflows._is_gate_step`; and `from_run`
built on a run that had not finished, which the button never offered. The review that found it
put it plainly: the button and the action could still disagree — the very class of bug this
module exists to end.

THE RULE NOW. One function, `_verdicts`, decides every action on a run from its status, its
steps (the LATEST attempt of each — the row `_latest_step_row` acts on) and, for research, the
dossier itself. Everything else reads it:

* `run_actions` — the list the API sends with every run (the console's list, `get_run`,
  `list_runs`). The console renders exactly that list and holds no opinion of its own.
* `require` — what every operation that changes a run calls before it does anything:
  `retry_failed_step`, `skip_failed_step`, `cancel_run`, `delete_run`, `recheck_run` and the
  start endpoint's `from_run`. The console's buttons, the agent's tools and a raw API call all
  reach those operations, so none of them can do what the button would not have offered, and a
  refusal says the same sentence the button's help does.

What appears, and what does not:

* An action that does not APPLY to this run is ABSENT — "Build on this research" on a run whose
  research failed is not greyed out, it is not there, because there is nothing to build on. So
  is Try again on a run that was stopped or has finished: it will never become possible, and a
  greyed button under the red ✗ is the incident again with less colour.
* An action that applies but cannot be taken RIGHT NOW is present and disabled with a sentence
  saying why — research that is still being written up, a run busy compensating.
"""
from __future__ import annotations

import json
from typing import Any, Iterable, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .workflows import WorkflowConflictError, WorkflowNotFoundError

#: A run still doing, or still able to do, work: Stop applies, Remove does not. `blocked` is
#: here because `cancel_run` stops it and `delete_run` refuses it — the console's private list
#: left it out and so offered the one button that could not work and hid the one that could.
ACTIVE_RUN_STATES = frozenset({"queued", "validating", "running", "waiting", "paused", "blocked"})

#: A run that is over. Mirrors `workflows.TERMINAL_RUN_STATES`, which `delete_run`'s waiter
#: check still reads; a test holds the two equal.
FINISHED_RUN_STATES = frozenset({"completed", "failed", "deadlettered", "cancelled", "compensated"})

#: Finished runs NOTHING in which runs again. `failed` and `deadlettered` are finished too, but a
#: run stopped by a failure is exactly the one Try again and Skip exist for; these three are not:
#: somebody stopped it (Stop promises "it cannot be resumed"), it completed, or it was undone.
CLOSED_RUN_STATES = frozenset({"cancelled", "completed", "compensated"})

#: A run STOPPED AT A FAILED STEP, waiting for the operator — NOT finished.
#:
#: THE CONTRADICTION THIS ENDS (review of 534a1b9/c8de36b, 2026-10-02). Once a failed step began
#: stopping its run, `failed` meant "stopped and waiting for you" on the run card and under
#: "Needs you" — but Stop and Remove still read it as "over": Stop was not offered and
#: `cancel_run` returned early, so a parallel branch still working under a stopped run could not
#: be stopped (the 2026-09-02 shape: a cancel that does not stop the work), Remove deleted the
#: run's records while that branch went on, and the only way to abandon the run was to delete it.
#: One definition now: a stopped run is still the operator's to decide about. Stop applies (it
#: cancels the live steps and the work they started), Remove waits until nothing in it is live.
STOPPED_RUN_STATES = frozenset({"failed", "deadlettered"})

#: Step states that mean work is still in flight (or queued to be) — `workflows.ACTIVE_STEP_STATES`.
LIVE_STEP_STATES = frozenset({"ready", "leased", "running", "waiting"})

#: A run undoing what it did. Neither stoppable (`cancel_run` would race the compensation) nor
#: removable (`delete_run` refuses it) until that finishes — the one TEMPORARY state, and so the
#: one where an action is shown disabled rather than left out.
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

#: A verdict's three states. Only the first two reach the console; all three reach `require`.
ENABLED, DISABLED, ABSENT = "enabled", "disabled", "absent"


def standing(status: Any) -> str:
    """Where a run stands, in the one vocabulary the console files runs by: `active`, `stopped`
    (at a failed step, waiting for the operator), `closed` (over: nothing in it runs again) or
    `compensating` (undoing itself).

    Sent with every run so the console holds no private list of run states. It used to: its
    `LIVE` list left out `paused` and `compensating`, so a paused run — which the server offers
    Stop on — was filed under "Done", and its closed list was a hand copy of CLOSED_RUN_STATES
    that nothing held equal to this one."""
    value = str(status or "")
    if value in STOPPED_RUN_STATES:
        return "stopped"
    if value in CLOSED_RUN_STATES:
        return "closed"
    if value == COMPENSATING:
        return "compensating"
    return "active"


class RunActionRefused(WorkflowConflictError):
    """An operation was asked for that this run's actions do not offer, or offer disabled.

    A `WorkflowConflictError`, so every endpoint and tool that already turns a conflict into a
    409 or an `{"ok": False}` says this one too, in the action's own words."""

    def __init__(self, key: str, reason: str, *, step_id: Optional[str] = None):
        super().__init__(reason)
        self.key = key
        self.step_id = step_id
        self.reason = reason


def is_gate(kind: Any, capability: Any) -> bool:
    """An approval: a decision step, or a capability declared `is_gate`.

    THE ONE TEST. `workflows._is_gate_step` reads a spec template through this, and the action
    list reads a step through it. They were two hand copies, and a gate one of them missed would
    be an approval somebody could skip past — a person's no turned into a yes."""
    if kind == "decision":
        return True
    key = str(capability or "")
    if not key:
        return False
    from .workflow_capabilities import CAPABILITY_KEYS, get as get_capability

    return key in CAPABILITY_KEYS and bool(get_capability(key).is_gate)


def is_refused_approval(step: dict[str, Any]) -> bool:
    """A failed step whose APPROVAL a person refused — a stack deploy whose deployment decision
    was rejected. Not a gate by kind, but skipping it turns the same no into a yes, so Skip
    treats it exactly like one. Retry stays: it plans afresh and asks again."""
    return bool(_output(step).get("rejected_by_approver"))


#: What a refused standalone approval offers instead of Skip (agents-home D3, lead's rule).
SKIP_PAST_A_NO = ("Skipping this approval would run what you just refused. "
                  "Retry asks again; Stop ends the run.")


def is_operator_skip(step: dict[str, Any]) -> bool:
    """A step a PERSON skipped after it failed (Skip and proceed) — not one the run's own path
    left out. Its Retry stays offered: the operator can change their mind once the cause is
    fixed. Without it a skipped preview was a dead end — deploy requires a reviewed preview, so
    deploy failed, and the preview that would now serve could never be run again on this run."""
    return str(step.get("status") or "") == "skipped" and bool(_output(step).get("skipped_by_operator"))


def skip_refusal(step: dict[str, Any]) -> Optional[str]:
    """THE ONE RULE for whether a failed step may be skipped past a person's no.

    A STANDALONE GATE — a decision step, or a capability declared `is_gate` (resume_workflow
    and its kind) — exists to release the steps AFTER it. Skipping it carries the run into
    exactly the work the operator refused: a no becomes a yes. So it is never skipped; it
    offers Retry (ask again) and Stop. Returns the sentence the refusal says.

    A step whose OWN action was held at its approval (a capability with `approval: manual`, a
    stack deploy whose deployment decision was rejected) may be skipped: skipping means that
    action never runs, which is what the no asked for, and the run carries on without it.
    Returns None for those, and for every step that is not an approval at all."""
    if is_gate(_step_kind(step), step_capability(step)):
        return SKIP_PAST_A_NO
    return None


def _verdict(key: str, label: str, state: str, reason: Optional[str] = None,
             step_id: Optional[str] = None, **extra: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"key": key, "label": label, "state": state,
                           "reason": None if state == ENABLED else (reason or "not available")}
    if step_id is not None:
        out["step_id"] = step_id
    out.update(extra)
    return out


def _public(verdict: dict[str, Any]) -> dict[str, Any]:
    """The shape the API sends — `enabled` and `reason` — never the internal three-way state."""
    out = {k: v for k, v in verdict.items() if k != "state"}
    out["enabled"] = verdict["state"] == ENABLED
    return out


def _output(step: dict[str, Any]) -> dict[str, Any]:
    out = step.get("output")
    if isinstance(out, str):
        try:
            out = json.loads(out)
        except ValueError:
            out = {}
    return out if isinstance(out, dict) else {}


def step_capability(step: dict[str, Any]) -> str:
    """The capability a step runs, from whichever shape the caller serialized.

    `load_runs` resolves it from the run's pinned spec into `capability`; a serialized step from
    elsewhere carries the binding snapshot instead. Either is the contract — never the step's
    id, which a customer may rename.
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


def _step_kind(step: dict[str, Any]) -> str:
    return str(step.get("kind") or step.get("step_kind") or "")


def _step_label(step: dict[str, Any]) -> str:
    from .workflow_capabilities import CAPABILITY_KEYS, get as get_capability

    key = step_capability(step)
    if key in CAPABILITY_KEYS:
        return get_capability(key).label
    return str(step.get("step_id") or key or "this step")


def latest_attempts(steps: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per step id — the latest attempt, which is the row the operations act on
    (`workflows._latest_step_row`: newest `created_at`, then highest iteration).

    A loop body or a retried branch leaves older rows behind; offering Try again for an old
    failed iteration that a later one superseded would be a button for a row nothing acts on.
    In the order the steps were first met, so the list reads in run order.
    """
    ordered = sorted(
        enumerate(steps),
        key=lambda pair: (str(pair[1].get("created_at") or ""),
                          int(pair[1].get("iteration") or 0), pair[0]),
    )
    latest: dict[str, dict[str, Any]] = {}
    first_seen: dict[str, int] = {}
    for position, step in ordered:
        step_id = str(step.get("step_id") or "")
        if not step_id:
            continue
        latest[step_id] = step
        first_seen.setdefault(step_id, position)
    return [latest[s] for s in sorted(latest, key=lambda s: first_seen[s])]


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


def _verdicts(
    run: dict[str, Any], steps: list[dict[str, Any]],
    discoveries: Optional[dict[str, dict[str, Any]]] = None,
) -> list[dict[str, Any]]:
    """EVERY action this module knows, for one run: enabled, disabled or absent, and why.

    The one decision. `run_actions` shows the ones that are not absent; `require` refuses any
    that is not enabled, with the same sentence. Pure: everything it reads is passed in, so the
    whole rule set is a table test (engine/tests/test_a_runs_actions_come_from_its_state.py).
    """
    status = str(run.get("status") or "")
    active = status in ACTIVE_RUN_STATES
    finished = status in FINISHED_RUN_STATES
    closed = status in CLOSED_RUN_STATES
    stopped = status in STOPPED_RUN_STATES
    compensating = status == COMPENSATING
    undoing = "it is undoing what it did — wait for that to finish"
    current = latest_attempts(steps)
    out: list[dict[str, Any]] = []

    # RE-CHECK: re-tests the recorded blocker of every failed step and re-queues the ones whose
    # blocker is gone — each through `retry_failed_step`, so it applies exactly where Try again
    # would. Only where a step HAS a recorded failure; never on a run somebody stopped or one
    # that finished, where re-queueing a step would quietly revive it.
    recheck = "Re-check this workflow"
    if closed:
        out.append(_verdict("recheck", recheck, ABSENT,
                            f"this run is {status}, so nothing in it is put back in the queue "
                            "— run the workflow again to try afresh"))
    elif compensating:
        out.append(_verdict("recheck", recheck, ABSENT, undoing))
    elif not any(step.get("status") in RETRYABLE_STEP_STATES for step in current):
        out.append(_verdict("recheck", recheck, ABSENT,
                            "no step on this run has a recorded failure to re-check"))
    else:
        out.append(_verdict("recheck", recheck, ENABLED))

    # A STOPPED run is stoppable too: it is waiting for the operator, not over, and "I am not
    # going to retry or skip this — abandon it" is one of the answers. `cancel_run` then cancels
    # whatever is still live under it (a parallel branch still working) and the work that
    # branch started.
    if active or stopped:
        out.append(_verdict("stop", "Stop this run", ENABLED))
        out.append(_verdict("stop_and_rerun", "Stop and run again", ENABLED))
    elif compensating:
        out.append(_verdict("stop", "Stop this run", DISABLED, undoing))
        out.append(_verdict("stop_and_rerun", "Stop and run again", ABSENT, undoing))
    else:
        why = f"this run is already {status or 'over'}, so there is nothing to stop"
        out.append(_verdict("stop", "Stop this run", ABSENT, why))
        out.append(_verdict("stop_and_rerun", "Stop and run again", ABSENT,
                            f"{why} — run it again instead"))

    live = [str(step.get("step_id") or "") for step in current
            if step.get("status") in LIVE_STEP_STATES]
    if closed:
        out.append(_verdict("rerun", "Run this workflow again", ENABLED))
        out.append(_verdict("remove", "Remove this run", ENABLED))
    elif stopped:
        # Not "Run this workflow again" beside a run still waiting for you: that left two runs
        # for one directive, the stopped one under "Needs you" for ever. Stop and run again
        # replaces it — exactly as on a run that is still going.
        out.append(_verdict("rerun", "Run this workflow again", ABSENT,
                            "it is stopped at a failed step and still waiting for you — Stop "
                            "and run again replaces it rather than starting a second workflow "
                            "beside it"))
        if live:
            # Deleting the records while a branch is still working is the failure `cancel_run`
            # exists to prevent, one table further down. Temporary — so shown, and disabled.
            out.append(_verdict(
                "remove", "Remove this run", DISABLED,
                f"'{live[0]}' is still working under this run. Stop the run first — that stops "
                "the work — and then it can be removed."))
        else:
            out.append(_verdict("remove", "Remove this run", ENABLED))
    else:
        out.append(_verdict("rerun", "Run this workflow again", ABSENT,
                            "it is still going — Stop and run again replaces it rather than "
                            "starting a second workflow beside it"))
        if compensating:
            out.append(_verdict("remove", "Remove this run", DISABLED,
                                "it is undoing what it did — it can be removed after"))
        else:
            # The sentence `delete_run` has always refused with: what to do instead.
            out.append(_verdict(
                "remove", "Remove this run", ABSENT,
                f"That run is {status}, and work that is still going cannot be deleted. "
                "Cancel it first — that stops the work — and then it can be removed."))

    # BUILD ON THIS RESEARCH: a finished run whose research SUCCEEDED and left a completed
    # dossier with findings. Not on a live run — a second workflow on the same directive while
    # this one is still going is the duplicate "Stop and run again" exists to prevent.
    build = "Build on this research →"
    if not finished:
        out.append(_verdict("build_on_research", build, ABSENT,
                            "this run is still going — its research can be built on once it "
                            "has finished"))
    else:
        found, why_not, applies = research_to_build_on(steps, run.get("input"), discoveries or {})
        state = ENABLED if found else DISABLED if applies else ABSENT
        out.append(_verdict("build_on_research", build, state, why_not,
                            **({"discovery_id": found} if found else {})))

    # PER STEP: Try again, and Skip and proceed — on the latest attempt of each step.
    for step in current:
        step_status = str(step.get("status") or "")
        step_id = str(step.get("step_id") or "")

        if step_status not in RETRYABLE_STEP_STATES and not is_operator_skip(step):
            # Re-queueing a step that is running would run it twice.
            retry = (ABSENT, f"'{step_id}' is {step_status}, so there is nothing to retry.")
        elif status == "cancelled":
            # Stop promises "it cannot be resumed; start it again to try afresh". Retrying a
            # step here would resume it — `retry_failed_step` revives the run to `running`.
            retry = (ABSENT, "this run was stopped, and a stopped run is not resumed — "
                             "run the workflow again to try afresh")
        elif closed:
            retry = (ABSENT, f"this run is {status}, so nothing in it runs again — "
                             "run the workflow again to try afresh")
        elif compensating:
            retry = (DISABLED, undoing)
        else:
            retry = (ENABLED, None)
        # SHORT LABELS, the step named in `title` (agents-home D20): "Try Set up monitoring
        # again" read as a sentence on a button. The card already names the step.
        refused_gate = (is_gate(_step_kind(step), step_capability(step))
                        or is_refused_approval(step))
        out.append(_verdict("retry_step", "Retry", retry[0], retry[1], step_id=step_id,
                            title=(f"Ask again: {_step_label(step)}" if refused_gate
                                   else f"Run {_step_label(step)} again")))

        # Skip is narrower: never an approval (a refused gate is somebody's no, and a skip
        # would turn it into a yes), never a cancelled step.
        if step_status not in SKIPPABLE_STEP_STATES:
            skip = (ABSENT, f"'{step_id}' is {step_status}. Only a step that failed can be "
                            "skipped — a live step is still working, and a finished one has "
                            "nothing to skip.")
        elif closed:
            skip = (ABSENT, f"this run is {status}, so there is nothing to proceed to.")
        elif compensating:
            skip = (DISABLED, undoing)
        elif skip_refusal(step):
            skip = (ABSENT, skip_refusal(step))
        else:
            skip = (ENABLED, None)
        # A STEP WHOSE OWN ACTION WAS REFUSED CAN BE SKIPPED (agents-home D3): the action never
        # runs and the run goes on without it; the record keeps who chose that and why — Skip
        # always asks for a reason. A standalone gate never can (`skip_refusal`).
        out.append(_verdict("skip_step", "Skip", skip[0], skip[1], step_id=step_id,
                            title=f"Carry on without {_step_label(step)}"))
    return out


def run_actions(
    run: dict[str, Any], steps: list[dict[str, Any]],
    discoveries: Optional[dict[str, dict[str, Any]]] = None,
) -> list[dict[str, Any]]:
    """The actions the console offers on one run, in the order it shows them: every verdict
    that is not absent, as `{key, label, enabled, reason[, step_id]}`."""
    return [_public(v) for v in _verdicts(run, steps, discoveries) if v["state"] != ABSENT]


#: Step states a run stops at — `workflows.STOPPED_STEP_STATES`; a test holds the two equal.
STOPPED_STEP_STATES = frozenset({"failed", "deadlettered"})


def stopped_for_operator(
    run: dict[str, Any], steps: list[dict[str, Any]],
    discoveries: Optional[dict[str, dict[str, Any]]] = None,
) -> list[tuple[dict[str, Any], list[dict[str, Any]]]]:
    """The steps this run is stopped at FOR ITS OPERATOR, each with the answers it offers.

    THE ONE RULE for "a step failed and somebody has to decide". The console's run card and
    "Needs you" bucket, and the Command Center's "Stopped — a step failed" list, all mean this.
    The briefing had its own query — every failed step in the project, whatever its run — so it
    listed failed steps of COMPLETED and CANCELLED runs (every legacy `failed_continued` row the
    schema rewrite turned into `failed`) and offered only "Re-check", which those runs refuse
    (review of c8de36b). Here a step counts when it is the latest attempt, failed or
    dead-lettered, not a refused approval (that is a person's no, already answered), not a
    branch an `any` join went on without, and the run offers an answer on it right now.
    """
    status = str(run.get("status") or "")
    if status in CLOSED_RUN_STATES or status == COMPENSATING:
        return []
    verdicts = _verdicts(run, steps, discoveries)
    out: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    for step in latest_attempts(steps):
        if step.get("status") not in STOPPED_STEP_STATES:
            continue
        if _output(step).get("tolerated_by_join"):
            continue
        # A step that failed into its own `on_error` path: handled, and the run went on.
        if _output(step).get("error_path_taken"):
            continue
        # A REFUSED GATE IS STOPPED FOR ITS OPERATOR TOO (agents-home D3): somebody said no,
        # the step failed, and Retry (ask again) or Skip (carry on without it) is the choice.
        step_id = str(step.get("step_id") or "")
        answers = [_public(v) for v in verdicts
                   if v.get("step_id") == step_id and v["state"] == ENABLED]
        # A refused standalone gate cannot be skipped, so its other answer is Stop: the run
        # does not go on into what the person refused (`skip_refusal`).
        if answers and skip_refusal(step):
            answers += [{**_public(v), "step_id": step_id} for v in verdicts
                        if v["key"] == "stop" and v["state"] == ENABLED]
        if answers:
            out.append((step, answers))
    return out


def decide(
    run: dict[str, Any], steps: list[dict[str, Any]],
    discoveries: Optional[dict[str, dict[str, Any]]], key: str, step_id: Optional[str] = None,
) -> dict[str, Any]:
    """The verdict on ONE action — enabled, disabled or absent, and why."""
    if key not in RUN_ACTION_KEYS and key not in STEP_ACTION_KEYS:
        raise ValueError(f"unknown run action {key!r}")
    for verdict in _verdicts(run, steps, discoveries):
        if verdict["key"] == key and verdict.get("step_id") == step_id:
            return verdict
    return _verdict(key, key, ABSENT, f"no step '{step_id}' on this run", step_id=step_id)


def research_to_adopt(
    connection: Connection, project_id: str, *, from_run: Any = None, from_discovery: Any = None,
) -> str:
    """The dossier a new run may build on, from a source the caller NAMED — checked here.

    ONE RULE FOR EVERY DOOR. The HTTP start endpoint resolved `from_run` through the
    "Build on this research" action and `from_discovery` through a status check of its own; the
    agent's `start_workflow` had a third copy. The two `from_discovery` copies accepted a
    COMPLETED dossier with no findings, which the run rule refuses ("the research completed with
    no findings"), and a request naming BOTH sources skipped the `from_run` rule entirely and
    copied a live run's input (review of 0fd6c45). Every start now asks this.

    Raises `WorkflowValidationError` for both sources at once, `WorkflowNotFoundError` for a
    source not in this project, and `RunActionRefused` (a 409) with the sentence the console
    would show when there is nothing finished to build on.
    """
    from .workflows import WorkflowValidationError

    run_id = str(from_run or "").strip()
    discovery_id = str(from_discovery or "").strip()
    if run_id and discovery_id:
        raise WorkflowValidationError(
            "name one source of research to build on — from_run or from_discovery, not both. "
            "A run carries the research it produced; a dossier is that research directly.")
    if run_id:
        try:
            offered = require(connection, project_id, run_id, "build_on_research")
        except RunActionRefused as error:
            raise RunActionRefused(
                "build_on_research",
                f"{run_id} has no completed research to carry forward: {error.reason}",
            ) from error
        return str(offered["discovery_id"])
    if not discovery_id:
        raise WorkflowValidationError("name the research to build on")
    facts = load_discoveries(connection, project_id, [discovery_id]).get(discovery_id)
    if not facts:
        raise WorkflowNotFoundError("research run not found")
    status = str(facts.get("status") or "")
    if status != "completed":
        raise RunActionRefused(
            "build_on_research",
            f"that research is {status}, not completed — there is nothing finished to build on "
            "yet")
    if not facts.get("has_findings"):
        raise RunActionRefused(
            "build_on_research",
            "that research completed with no findings, so there is nothing to build on — run "
            "the research again, or start without it")
    return discovery_id


# ── Reading a run's state, once, for the list and for the gate ─────────────────────────────

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


def _iso(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


def load_runs(
    connection: Connection, project_id: str, run_ids: Iterable[str],
) -> dict[str, tuple[dict[str, Any], list[dict[str, Any]]]]:
    """`{run_id: (run, steps)}` for every named run in this project — the state `_verdicts`
    reads, in two queries however many runs are asked for.

    THE SAME READ FOR THE BUTTON AND THE GATE. The console's list used to hand `run_actions`
    steps it had shaped itself while the endpoints read the database their own way; two readers
    of one fact is how a button and its action drift. Each step's kind and capability come from
    the run's PINNED spec — what `skip_failed_step` has always read — falling back to the row's
    own `step_kind` for a step the spec does not name.
    """
    from .workflows import _json, _step_map

    wanted = sorted({str(i) for i in run_ids if i})
    if not wanted:
        return {}
    runs = connection.execute(
        text("SELECT r.id, r.status, r.input, v.spec FROM workflow_runs r "
             "JOIN workflow_versions v ON v.id = r.workflow_version_id "
             "WHERE r.id = ANY(:ids) AND r.project_id = :p"),
        {"ids": wanted, "p": project_id},
    ).mappings().all()
    if not runs:
        return {}
    rows = connection.execute(
        text("SELECT id, workflow_run_id, step_id, step_kind, status, output, binding_snapshot, "
             "iteration, created_at, completed_at, updated_at, last_error FROM workflow_step_runs "
             "WHERE workflow_run_id = ANY(:ids) ORDER BY created_at, iteration"),
        {"ids": [r["id"] for r in runs]},
    ).mappings().all()
    templates = {r["id"]: _step_map(_json(r["spec"], {}) or {}) for r in runs}
    by_run: dict[str, list[dict[str, Any]]] = {r["id"]: [] for r in runs}
    for row in rows:
        template = templates.get(row["workflow_run_id"], {}).get(str(row["step_id"])) or {}
        # A step the run added (two_pass: More tests) is not in the stored version: its row's
        # own binding says what it is.
        binding = template.get("binding") if isinstance(template.get("binding"), dict) \
            else (_json(row["binding_snapshot"], {}) or {})
        by_run[row["workflow_run_id"]].append({
            "id": row.get("id"),
            "step_id": row["step_id"],
            "kind": template.get("kind") or row["step_kind"],
            "capability": str(binding.get("capability") or ""),
            "status": row["status"],
            "output": _json(row["output"], {}),
            "binding_snapshot": _json(row["binding_snapshot"], {}),
            "iteration": int(row["iteration"] or 0),
            "created_at": _iso(row["created_at"]),
            "completed_at": _iso(row["completed_at"]),
            "updated_at": _iso(row.get("updated_at")),
            "last_error": row.get("last_error"),
        })
    return {
        r["id"]: ({"id": r["id"], "status": r["status"], "input": _json(r["input"], {}) or {}},
                  by_run[r["id"]])
        for r in runs
    }


def actions_for(
    connection: Connection, project_id: str, run_ids: Iterable[str],
) -> dict[str, list[dict[str, Any]]]:
    """`{run_id: actions}` for many runs, batched: the field every run serializer attaches."""
    loaded = load_runs(connection, project_id, run_ids)
    ids: set[str] = set()
    for run, steps in loaded.values():
        ids.update(research_discovery_ids(steps, run.get("input")))
    discoveries = load_discoveries(connection, project_id, ids)
    return {run_id: run_actions(run, steps, discoveries)
            for run_id, (run, steps) in loaded.items()}


def require(
    connection: Connection, project_id: str, run_id: str, key: str,
    *, step_id: Optional[str] = None,
) -> dict[str, Any]:
    """Refuse unless this run offers `key` (on `step_id`) ENABLED right now.

    Called by every operation that acts on a run, before it changes anything, so what the
    console shows and what the server does are one decision. Returns the action — for
    `build_on_research`, carrying the `discovery_id` to adopt. Raises `WorkflowNotFoundError`
    for a run that is not in this project, and `RunActionRefused` (a 409) with the action's own
    reason otherwise.
    """
    # THE CHECK HOLDS THE RUN. Try again read the run unlocked and then set it running, so a
    # Stop committing in between was undone — a cancelled run revived (review of 0fd6c45).
    # Taking the row lock here serializes EVERY gated operation against Stop, whichever caller
    # forgot to lock first; for one that already holds it, this is a no-op.
    connection.execute(
        text("SELECT id FROM workflow_runs WHERE id = :id AND project_id = :p FOR UPDATE"),
        {"id": run_id, "p": project_id},
    )
    loaded = load_runs(connection, project_id, [run_id]).get(run_id)
    if loaded is None:
        raise WorkflowNotFoundError("Workflow run not found.")
    run, steps = loaded
    discoveries = (load_discoveries(connection, project_id,
                                    research_discovery_ids(steps, run.get("input")))
                   if key == "build_on_research" else {})
    verdict = decide(run, steps, discoveries, key, step_id)
    if verdict["state"] != ENABLED:
        raise RunActionRefused(key, str(verdict["reason"]), step_id=step_id)
    return _public(verdict)


__all__ = [
    "ACTIVE_RUN_STATES", "CLOSED_RUN_STATES", "FINISHED_RUN_STATES", "RUN_ACTION_KEYS",
    "STOPPED_RUN_STATES", "LIVE_STEP_STATES", "STOPPED_STEP_STATES", "standing",
    "stopped_for_operator", "research_to_adopt",
    "STEP_ACTION_KEYS", "BUILD_WORKFLOW_KEY", "RunActionRefused", "actions_for", "decide",
    "is_gate", "latest_attempts", "load_discoveries", "load_runs", "require",
    "research_discovery_ids", "research_to_build_on", "run_actions", "step_capability",
]
