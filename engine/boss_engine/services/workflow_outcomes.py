"""Whether a capability's work HAPPENED — one verdict per capability, beside the catalogue.

WHY THIS EXISTS. A failed step now stops its run (boss-1, 2026-10-02: research died, was
recorded succeeded, and the workflow walked on into a panel and a build). But "failed" was still
decided by a handful of generic checks in the runner — a returned `status` in a short list, or
`ok: false` — and the real tools do not all speak that way. The review of that change found:

  * `smoke_test` calls `http_probe`, which reports a dead app as `{"status": 503}` or
    `{"status": None, "error": "ConnectionRefusedError..."}`. Neither is `ok: false`, neither
    status is in the list, so a smoke test of an app that was down went GREEN and the run went
    on. The capability's own `expect_status` input was read by nothing at all.
  * `deploy`, `redeploy` and `retry_build` start work that finishes later, and nothing waited
    for it. `deploy_app` answers `{"status": "planned"}` and `retry_code_task` answers
    `{"status": "retried"}`, so those steps went green the moment the work was ACCEPTED, and a
    deployment or rebuild that then failed never reached the step.

So every capability in the catalogue now has an explicit verdict here: a function that reads
what that capability's work really returned and says SUCCEEDED, FAILED or AWAITING. The runner
applies it to every capability step (`workflow_capability_runner.execute_step`), and
`test_a_failed_step_stops_the_run.py` refuses a catalogue entry without one — so a new
capability cannot ship until somebody has written down how its failure looks.

`FAILURE_SHAPES` records, beside each tool-backed capability, what its tool ACTUALLY returns when
the work did not happen. The enforcing test drives the runner with those shapes rather than a
generic `{"status": "failed"}` no real tool produces, and a second test proves each recorded
shape is what the real tool returns.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

SUCCEEDED = "succeeded"
FAILED = "failed"
#: Started, not finished: the step parks against `await` and `resume_awaited` decides later.
AWAITING = "awaiting"


@dataclass(frozen=True)
class Verdict:
    outcome: str
    #: The sentence the step records when it failed. Empty otherwise.
    reason: str = ""
    #: Fields the capability declares it produces, read out of the tool's own shape.
    produced: Mapping[str, Any] = field(default_factory=dict)


#: Statuses that mean THE WORK DID NOT HAPPEN, however politely the work says so.
DID_NOT_SHIP = frozenset({"failed", "blocked", "refused", "error"})


def _reason(result: Mapping[str, Any]) -> str:
    """The cause, from wherever this tool put it. `deploy_build` says `reason`, the deploy
    connector says `problems`, most tools say `error`."""
    problems = result.get("problems")
    if isinstance(problems, (list, tuple)) and problems:
        return "; ".join(
            str(p.get("detail") or p.get("message") or p.get("reason") or p)
            if isinstance(p, dict) else str(p) for p in problems)
    for key in ("error", "reason", "message", "note"):
        value = result.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _status(result: Mapping[str, Any]) -> str:
    value = result.get("status")
    return value.strip().lower() if isinstance(value, str) else ""


def _refused(result: Mapping[str, Any]) -> Verdict | None:
    """The failure every tool may report, however it reports it."""
    if result.get("ok") is False or _status(result) in DID_NOT_SHIP:
        return Verdict(FAILED, _reason(result) or "the work reported that it did not happen")
    return None


#: The answers an awaited capability may give when there was nothing to wait for: the work was
#: found already finished (the runner's `_DONE_STATUSES`), adopted from the run, or the phase
#: does not apply to this run at all ("no research phase — the build was already running").
FINISHED_ALREADY = frozenset({"completed", "succeeded", "observed", "skipped",
                              "not_applicable", "adopted"})


# ── The verdicts ──────────────────────────────────────────────────────────────────────────────

def awaited_work(result: Mapping[str, Any], inputs: Mapping[str, Any]) -> Verdict:
    """Work that is started now and finishes later: research, a panel, a build, a preview,
    PLRR's repair, a deployment, a redeploy, a rebuild.

    The only success available at this moment is "nothing needed doing" — a phase that does not
    apply, or work that was adopted already finished. Anything else must hand back something to
    WAIT on; work that was accepted and gave no handle has not succeeded, it has vanished.
    """
    refused = _refused(result)
    if refused:
        return refused
    status = _status(result)
    if status == AWAITING and isinstance(result.get("await"), dict):
        return Verdict(AWAITING)
    if status in FINISHED_ALREADY:
        return Verdict(SUCCEEDED)
    return Verdict(FAILED, _reason(result) or (
        f"the work was accepted ({status or 'no status'}) but handed back nothing to wait "
        f"on, so this step cannot tell whether it ever finished"))


def answered_gate(result: Mapping[str, Any], inputs: Mapping[str, Any]) -> Verdict:
    """An approval reaches the runner only when the run already holds the answer."""
    if _status(result) in ("adopted", "not_applicable"):
        return Verdict(SUCCEEDED)
    return _refused(result) or Verdict(FAILED, "nobody has answered this approval")


def observed(result: Mapping[str, Any], inputs: Mapping[str, Any]) -> Verdict:
    """review, test, monitor: a reading of what the build's own pipeline recorded. Findings are
    reported on a SUCCEEDED step (the inner pipeline already decided they do not block); only a
    reading that failed is a failure."""
    return _refused(result) or Verdict(SUCCEEDED)


def written(result: Mapping[str, Any], inputs: Mapping[str, Any]) -> Verdict:
    """report, document: a record the runner writes itself. Written means it has an id."""
    refused = _refused(result)
    if refused:
        return refused
    if result.get("artifact_id") or result.get("document_id"):
        return Verdict(SUCCEEDED)
    return Verdict(FAILED, "nothing was written — the record has no id")


def commissions_nothing(result: Mapping[str, Any], inputs: Mapping[str, Any]) -> Verdict:
    """video_edit, social_publish as bare capabilities: no tool performs them, so the runner
    records an honest `recorded` no-op that says so in its note. The work, when there is any,
    belongs to the skill or custom step the workflow binds instead."""
    return _refused(result) or Verdict(SUCCEEDED)


def notified(result: Mapping[str, Any], inputs: Mapping[str, Any]) -> Verdict:
    """notify: sent, skipped visibly, or failed — never a green no-op.

    It used to be judged by `commissions_nothing`: no tool, `status: recorded`, succeeded. Three
    catalogue templates ended by telling nobody and Deprecation / Sunset's whole announcement
    went nowhere before a seven-day wait (boss-1 map, 2026-10-02). Now the runner sends it:
    `posted` means a receipt exists; `not_applicable` means no channel was given and the step
    says so (the console shows it as skipped, not ✓); anything else did not reach anybody.
    """
    refused = _refused(result)
    if refused:
        return refused
    if _status(result) == "not_applicable":
        return Verdict(SUCCEEDED)
    if result.get("posted") is True:
        return Verdict(SUCCEEDED)
    return Verdict(FAILED, _reason(result) or "nothing was sent, so nobody was told")


def smoke_test(result: Mapping[str, Any], inputs: Mapping[str, Any]) -> Verdict:
    """`http_probe` reports an HTTP status, or `status: None` with an `error` when nothing
    answered. It never says `ok: false` for a dead app, which is how a smoke test of an app that
    was down went green. The capability declares `expect_status`; without one, any 2xx passes."""
    if result.get("ok") is False:
        return Verdict(FAILED, _reason(result) or "the probe could not run")
    code = result.get("status")
    if isinstance(code, str):
        # Not http_probe's shape at all — judged by the words every tool may use.
        return _refused(result) or Verdict(FAILED, f"the probe reported {code!r}, not a status")
    produced = {"status_code": code if isinstance(code, int) else None,
                "latency_ms": result.get("ms"),
                "reachable": isinstance(code, int)}
    url = result.get("url") or inputs.get("url") or "the app"
    if not isinstance(code, int):
        return Verdict(FAILED, f"{url} did not answer: "
                               f"{result.get('error') or 'no response'}", produced)
    expected = inputs.get("expect_status")
    try:
        wanted = int(expected) if expected not in (None, "") else None
    except (TypeError, ValueError):
        return Verdict(FAILED, f"expect_status {expected!r} is not an HTTP status code", produced)
    ok = code == wanted if wanted is not None else 200 <= code < 300
    if not ok:
        return Verdict(FAILED, f"{url} answered {code}, expected "
                               f"{wanted if wanted is not None else 'a 2xx'}", produced)
    return Verdict(SUCCEEDED, "", produced)


def finished_tool(result: Mapping[str, Any], inputs: Mapping[str, Any]) -> Verdict:
    """teardown_preview, cost_check: one call that finishes in the call."""
    return _refused(result) or Verdict(SUCCEEDED)


#: Every capability in the catalogue, and how to tell whether its work happened. A capability
#: missing from here fails `test_every_capability_has_an_outcome_verdict`.
VERDICTS: dict[str, Callable[[Mapping[str, Any], Mapping[str, Any]], Verdict]] = {
    "research": awaited_work,
    "meeting": awaited_work,
    "build": awaited_work,
    "architecture": awaited_work,
    "preview": awaited_work,
    "plrr": awaited_work,
    "deploy": awaited_work,
    "redeploy": awaited_work,
    "retry_build": awaited_work,
    "spec_review": answered_gate,
    "ux_review": answered_gate,
    "security_review": answered_gate,
    "review": observed,
    "test": observed,
    "monitor": observed,
    "report": written,
    "document": written,
    "notify": notified,
    "video_edit": commissions_nothing,
    "social_publish": commissions_nothing,
    "smoke_test": smoke_test,
    "wait_for_ci": awaited_work,
    "teardown_preview": finished_tool,
    "cost_check": finished_tool,
    "security_scan": awaited_work,
    "security_panel": awaited_work,
    "security_decide": answered_gate,
    "security_fix": awaited_work,
    "security_rereview": observed,
    "security_report": written,
}


#: What each tool-backed capability's tool REALLY returns when the work did not happen, with
#: where it comes from. Not a generic `{"status": "failed"}`: the point is that a shape a real
#: tool produces is judged a failure. `test_a_recorded_failure_shape_is_what_the_tool_returns`
#: proves each one against the tool itself.
FAILURE_SHAPES: dict[str, tuple[dict[str, Any], ...]] = {
    # tools/builtin.py http_probe: an HTTP error status, and an exception caught as a RESULT.
    "smoke_test": (
        {"url": "http://127.0.0.1:9/", "status": 503, "ms": 3, "body_excerpt": "down"},
        {"url": "http://127.0.0.1:9/", "status": None, "ms": 1,
         "error": "URLError: <urlopen error [Errno 61] Connection refused>"},
        # Community refuses URL probes outright.
        {"ok": False, "error": "Community URL probes require a project-bound destination and "
                               "a network policy; use recorded deployment health."},
    ),
    # tools/builtin.py stop_preview: an unknown preview, or the service raising.
    "teardown_preview": ({"ok": False, "error": "no preview prv_missing"},),
    # delivery_pipeline.deploy_build: a refusal is `status: failed` with a `reason` — not an
    # `error`, which is why `_reason` reads both. (A policy block, `status: blocked`, reaches
    # the step as the deployment run's own status and is covered by the awaited cases.)
    "deploy": ({"status": "failed", "reason": "no such run in this project: crun_missing"},),
    # tools/builtin.py redeploy: nothing to re-ship.
    "redeploy": ({"ok": False, "run_id": "crun_missing", "outcome": "no_action: never deployed",
                  "reason": "operator asked BOSS to retry the deployment"},),
    # tools/builtin.py retry_code_task: an unknown run.
    "retry_build": ({"ok": False, "error": "no code task run crun_missing"},),
    # tools/builtin.py get_costs: it reads the spend ledger and has no refusal to return; the
    # only way it fails is by RAISING, which the runner records like any other raise.
    "cost_check": (),
}


def judge(key: str, result: Mapping[str, Any], inputs: Mapping[str, Any]) -> Verdict:
    """The verdict for capability `key`. Raises KeyError for a capability with none — which is
    a defect the catalogue test exists to catch before it ships, never something to default."""
    return VERDICTS[key](result, inputs or {})


def apply(key: str, result: dict[str, Any], inputs: Mapping[str, Any]) -> dict[str, Any]:
    """The step's output, rewritten to say what the verdict says.

    A FAILED verdict becomes `status: failed` with the reason in `error`, which the runner
    already records as a failed step — so the runner has one way to fail, whatever the tool's
    dialect. Declared outputs the verdict read out of the tool's shape are added; nothing the
    tool returned is removed, because it is the evidence.
    """
    verdict = judge(key, result, inputs)
    out = dict(result)
    for name, value in verdict.produced.items():
        out.setdefault(name, value)
    out["verdict"] = verdict.outcome
    if verdict.outcome == FAILED:
        out["status"] = "failed"
        out["error"] = verdict.reason or out.get("error") or "the work did not happen"
    return out


__all__ = [
    "AWAITING", "DID_NOT_SHIP", "FAILED", "FAILURE_SHAPES", "SUCCEEDED",
    "VERDICTS", "Verdict", "apply", "judge",
]
