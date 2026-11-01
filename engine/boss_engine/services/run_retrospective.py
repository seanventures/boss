"""What a finished run actually cost, delivered, and lost — assembled from evidence only.

WHY THIS EXISTS. A build took 85 minutes that had taken under 20 the day before. Answering
"what went wrong" took two hours of reading container logs, job leases, sandbox file mtimes and
gate events by hand. Every fact needed was already recorded. Nothing assembled them.

What that investigation eventually found, none of which was visible anywhere:

  * 62% of the elapsed time was the host refusing to schedule BOSS, not the pipeline working
  * the primary reviewer ran out of turns and judged nothing, and the run shipped anyway
  * three gates were recorded advisory, including the one saying the code was never reviewed
  * the implementer finished one turn short of its cap
  * the pull request carried no review comments at all, silently

DETERMINISTIC, AND NO MODEL. Same rule as sre_postmortem: assembled from existing evidence,
never inventing a cause. A retrospective that costs a dollar and occasionally invents a reason
is worse than none — this run cost $11.76, and a narrator would be both a tax and a risk.

THREE SCORES, NEVER ONE. Mixing them is how "BOSS is regressing" got said about a machine that
was thrashing. Delivery is what the pipeline produced. Efficiency is what it spent getting
there. Host is whether the machine let it run at all — and it is reported separately precisely
so it can never drag down the other two.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .self_observability import host_is_degraded

#: A stage this close to its cap did not choose to stop — it was cut off, or nearly was.
_NEAR_CAP_TURNS = 2


def _as_dict(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return json.loads(value)
        except ValueError:
            return {}
    return {}


def score_delivery(state: dict, *, pr_number: Optional[int],
                   review_comments_posted: bool) -> dict[str, Any]:
    """What the pipeline PRODUCED, judged against what it is for.

    Shipping is necessary and not sufficient: a pull request nobody reviewed and that carries
    no findings is the shape of the failure this exists to catch.
    """
    problems: list[str] = []
    if not pr_number:
        problems.append("no pull request was opened")
    if state.get("tests_passed") is not True:
        problems.append("tests did not pass")

    verdicts = {
        "adversarial": str(state.get("adversarial_verdict") or ""),
        "security": str(state.get("security_verdict") or ""),
        "consistency": str(state.get("consistency_verdict") or ""),
    }
    never_ran = [name for name, v in verdicts.items() if v in {"", "not_run"}]
    if never_ran:
        problems.append("reviewer(s) never produced a verdict: " + ", ".join(sorted(never_ran)))
    if pr_number and not review_comments_posted:
        problems.append("the pull request carries no review findings")

    if not problems:
        grade = "good"
    elif pr_number and state.get("tests_passed") is True:
        grade = "fair"          # it shipped and it works; the governance around it did not
    else:
        grade = "poor"
    return {"grade": grade, "problems": problems, "verdicts": verdicts,
            "pull_request": pr_number, "review_comments_posted": review_comments_posted}


def score_efficiency(*, working_minutes: float, cost_usd: float,
                     near_cap_stages: list[str],
                     comparable_minutes: Optional[float] = None) -> dict[str, Any]:
    """What it SPENT, with the host's stalls already removed.

    Judged against this project's own history where there is any — an absolute minute count
    means nothing across tasks of different size, and the PRD reaching the builder legitimately
    made builds larger.
    """
    problems: list[str] = []
    if near_cap_stages:
        problems.append("stage(s) finished at or near the turn cap, so the work may be "
                        "truncated: " + ", ".join(near_cap_stages))
    ratio = None
    if comparable_minutes and comparable_minutes > 0:
        ratio = round(working_minutes / comparable_minutes, 2)
        if ratio >= 2.0:
            problems.append(f"working time was {ratio}x this project's recent median "
                            f"({working_minutes:.0f}m vs {comparable_minutes:.0f}m)")
    grade = "good" if not problems else ("fair" if len(problems) == 1 else "poor")
    return {"grade": grade, "problems": problems, "working_minutes": round(working_minutes, 1),
            "cost_usd": round(cost_usd, 2), "vs_project_median": ratio}


def findings(delivery: dict, efficiency: dict, host: dict,
             *, advisory_gates: list[str]) -> list[dict[str, str]]:
    """What to change, in the order it is worth changing. Facts only, each with its evidence."""
    out: list[dict[str, str]] = []
    if host.get("state") in {"degraded", "critical"}:
        out.append({
            "severity": "blocker" if host["state"] == "critical" else "warn",
            "area": "host",
            "finding": host.get("detail", "the host did not schedule BOSS reliably"),
            "action": "Fix the machine before the next run — free memory or restart. "
                      "This time is not the pipeline's and re-running will not improve it.",
        })
    for problem in delivery.get("problems", []):
        out.append({"severity": "blocker" if "never produced a verdict" in problem else "warn",
                    "area": "delivery", "finding": problem,
                    "action": "A check that did not run is not an opinion to ship past; "
                              "raise its budget or escalate it."
                              if "verdict" in problem else
                              "The review findings belong on the pull request."})
    if advisory_gates:
        out.append({"severity": "warn", "area": "governance",
                    "finding": "shipped with gate(s) advisory: " + ", ".join(advisory_gates),
                    "action": "Advisory means a reviewer's opinion cannot block the PR. "
                              "Confirm each of these was a judgement, not a check that "
                              "never ran."})
    for problem in efficiency.get("problems", []):
        # The action has to match the FINDING. One blanket remedy per area is how a
        # retrospective becomes noise: "raise the turn budget" is the right answer to a
        # truncated stage and a non-sequitur for a run that was simply larger.
        if "turn cap" in problem:
            action = ("Raise that stage's turn budget — a stage stopped at its cap did not "
                      "choose to stop, and its output may be incomplete.")
        elif host.get("state") in {"degraded", "critical"}:
            action = ("Compare again after the host is fixed: this comparison includes time "
                      "the machine took, so it is not yet evidence about the pipeline.")
        else:
            action = ("Check whether the task was genuinely larger — a fuller spec produces "
                      "more work — before treating this as a regression.")
        out.append({"severity": "warn", "area": "efficiency", "finding": problem,
                    "action": action})
    return out


def assemble(connection: Connection, project_id: str, run_id: str) -> dict[str, Any]:
    """The retrospective for one finished run. Read-only; never raises on missing evidence."""
    row = connection.execute(
        text("SELECT id, status, phase, title, error, state, finalize_ctx, "
             "       pull_request_number, created_at, updated_at "
             "  FROM code_task_runs WHERE id = :r AND project_id = :p"),
        {"r": run_id, "p": project_id},
    ).mappings().first()
    if row is None:
        return {"ok": False, "error": f"no code task run {run_id}"}

    state, fctx = _as_dict(row["state"]), _as_dict(row["finalize_ctx"])
    started, ended = row["created_at"], row["updated_at"]
    elapsed_s = max(0.0, (ended - started).total_seconds()) if started and ended else 0.0

    calls = connection.execute(
        text("SELECT stage, role, engine, model, turns, total_tokens, cost_usd_micros, "
             "       latency_ms, ok FROM llm_calls WHERE code_task_run_id = :r "
             " ORDER BY created_at"),
        {"r": run_id},
    ).mappings().all()
    cost_usd = sum(int(c["cost_usd_micros"] or 0) for c in calls) / 1_000_000

    # Host stalls that OVERLAPPED this run — the question is what this run lost, not whether
    # the machine has ever been slow.
    stalls = connection.execute(
        text("SELECT data FROM events WHERE event_type = 'host.stalled' AND (data->>'source') IS NOT NULL "
             "   AND created_at BETWEEN :a AND :b"),
        {"a": started, "b": ended},
    ).mappings().all()
    seconds = [float(_as_dict(s["data"]).get("stalled_seconds") or 0) for s in stalls]
    host = host_is_degraded(
        {"stalled_seconds": round(sum(seconds), 2), "stall_events": len(seconds),
         "longest_stall_seconds": max(seconds, default=0.0)},
        elapsed_seconds=elapsed_s)

    advisory: list[str] = []
    posted = False
    for event in connection.execute(
            text("SELECT event_type, data FROM events WHERE entity_id = :r "
                 "   AND event_type IN ('code_task.gate_advisory', "
                 "       'code_task.review_comments_posted', "
                 "       'code_task.review_comments_absent')"),
            {"r": run_id}).mappings().all():
        data = _as_dict(event["data"])
        if event["event_type"] == "code_task.gate_advisory":
            advisory = [str(g) for g in (data.get("advisory_failing_gates") or [])]
        elif event["event_type"] == "code_task.review_comments_posted":
            posted = True

    review_cap = int(state.get("review_max_turns") or 0)
    near_cap = [str(c["stage"]) for c in calls
                if c["turns"] and review_cap and int(c["turns"]) >= review_cap - _NEAR_CAP_TURNS]
    near_cap += [str(c["stage"]) for c in calls
                 if c["stage"] == "implement" and int(c["turns"] or 0) >= 88]

    working_minutes = max(0.0, (elapsed_s - float(host.get("stalled_seconds") or 0))) / 60
    median = connection.execute(
        text("SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY "
             "       EXTRACT(EPOCH FROM (updated_at - created_at))/60) "
             "  FROM code_task_runs WHERE project_id = :p AND status = 'completed' "
             "   AND id <> :r"),
        {"p": project_id, "r": run_id}).scalar()

    delivery = score_delivery(state, pr_number=row["pull_request_number"],
                              review_comments_posted=posted)
    efficiency = score_efficiency(working_minutes=working_minutes, cost_usd=cost_usd,
                                  near_cap_stages=sorted(set(near_cap)),
                                  comparable_minutes=float(median) if median else None)

    return {
        "ok": True,
        "run_id": run_id,
        "title": row["title"],
        "status": row["status"],
        "elapsed_minutes": round(elapsed_s / 60, 1),
        "delivery": delivery,
        "efficiency": efficiency,
        "host": host,
        "stages": [{"stage": c["stage"], "role": c["role"], "engine": c["engine"],
                    "model": c["model"], "turns": c["turns"],
                    "usd": round(int(c["cost_usd_micros"] or 0) / 1_000_000, 3),
                    "seconds": int((c["latency_ms"] or 0) / 1000), "ok": bool(c["ok"])}
                   for c in calls],
        "advisory_gates": advisory,
        "findings": findings(delivery, efficiency, host, advisory_gates=advisory),
    }


def record(connection: Connection, project_id: str, run_id: str) -> dict[str, Any]:
    """PHASE 2 — assemble the retrospective and keep it, when a run reaches a terminal state.

    Kept as an event so it is durable, queryable and carries no new table. Never raises: a
    retrospective that can fail the run it describes is worse than none.
    """
    try:
        report = assemble(connection, project_id, run_id)
        if not report.get("ok"):
            return report
        from .core import emit_event
        blockers = [f for f in report["findings"] if f["severity"] == "blocker"]
        emit_event(connection, project_id, "code_task.retrospective", "boss",
                   "code_task_run", run_id,
                   {"delivery": report["delivery"]["grade"],
                    "efficiency": report["efficiency"]["grade"],
                    "host": report["host"]["state"],
                    "elapsed_minutes": report["elapsed_minutes"],
                    "cost_usd": report["efficiency"]["cost_usd"],
                    "blockers": len(blockers),
                    "findings": report["findings"][:12]})
        return report
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:200]}


def preflight(connection: Connection, project_id: str) -> dict[str, Any]:
    """PHASE 3 — what the LAST run learned, before the next one is started.

    The failure this prevents: starting an expensive run into a machine that is known to be
    thrashing, then reading the result as the pipeline degrading. A run that already told us
    the host lost 53 of its 85 minutes is evidence, and spending it again to rediscover that
    is the most expensive way to learn nothing.

    Returns {"ready": bool, ...}. Advisory by design — it informs, it does not refuse.
    """
    row = connection.execute(
        text("SELECT entity_id, data, created_at FROM events "
             " WHERE project_id = :p AND event_type = 'code_task.retrospective' "
             " ORDER BY created_at DESC LIMIT 1"),
        {"p": project_id},
    ).mappings().first()

    # Stalls since the last retrospective are the freshest signal about the machine, and they
    # exist even when no run has finished since.
    recent = connection.execute(
        text("SELECT COALESCE(SUM((data->>'stalled_seconds')::numeric), 0) "
             "  FROM events WHERE event_type = 'host.stalled' AND (data->>'source') IS NOT NULL "
             "   AND created_at > now() - interval '1 hour'"),
    ).scalar()
    stalled_recently = float(recent or 0)

    warnings: list[str] = []
    if stalled_recently >= 60:
        warnings.append(
            f"the host stopped scheduling BOSS for {stalled_recently:.0f}s in the last hour — "
            f"a run started now will look slow, and that will not be the pipeline")
    if row is not None:
        data = _as_dict(row["data"])
        for finding in data.get("findings", []):
            if finding.get("severity") == "blocker":
                warnings.append(f"unresolved from the last run: {finding.get('finding')}")
        if data.get("host") in {"degraded", "critical"}:
            warnings.append(f"the last run's host verdict was {data['host']}")

    return {
        "ready": not warnings,
        "warnings": warnings,
        "last_run": row["entity_id"] if row is not None else None,
        "stalled_seconds_last_hour": round(stalled_recently, 1),
    }


def host_advice(connection: Connection, project_id: str) -> dict[str, Any]:
    """Should the person at the screen be told their machine needs attention, and what to do.

    Looks at the recent past rather than one run: a customer opening the console wants to know
    whether to start work now, and the last hour is the honest window for that.
    """
    from .self_observability import operator_advice, pressure_now

    # TWO SIGNALS, AND THEY ANSWER DIFFERENT QUESTIONS. Pressure says what is wrong with the
    # machine right now and is only trustworthy where the container shares the host's kernel —
    # true on a Linux VM, false on Docker Desktop for Mac, where it read 51% used through a
    # total freeze. Stalls say whether BOSS was scheduled at all, which is true everywhere.
    # Pressure explains; stalls prove. Either can raise the alarm.
    gauges = pressure_now()

    rows = connection.execute(
        text("SELECT data FROM events WHERE event_type = 'host.stalled' AND (data->>'source') IS NOT NULL "
             "   AND created_at > now() - interval '1 hour'"),
    ).mappings().all()
    seconds = [float(_as_dict(r["data"]).get("stalled_seconds") or 0) for r in rows]
    if not seconds and gauges.get("state") == "ok":
        return {"show": False}

    host = host_is_degraded(
        {"stalled_seconds": round(sum(seconds), 2), "stall_events": len(seconds),
         "longest_stall_seconds": max(seconds, default=0.0)},
        elapsed_seconds=3600.0)
    if host.get("state") == "ok" and gauges.get("state") != "ok":
        # Nothing has stalled YET, but the gauges say it is about to. Warning the operator
        # before the first slow run is the whole point of having gauges as well as evidence.
        host = {"state": gauges["state"], "stalled_seconds": 0.0, "share_of_elapsed": 0.0,
                "detail": "the machine is running low on " + " and ".join(gauges["reasons"])}
    # The gauges go IN, not just alongside: they decide what the headline may claim (memory
    # only when memory is actually unavailable) and what to advise (never "restart the
    # computer" to a Linux server).
    advice = operator_advice(host, gauges=gauges)
    if advice.get("show"):
        advice["gauges"] = gauges.get("bands", {})
    return advice


# --- the whole workflow, not just the build ----------------------------------------------
#
# `assemble` above answers for ONE code task. A workflow run is research, then a panel, then a
# build, then previews and deploys, and the question an operator actually asks at the end is
# about all of it at once:
#
#     "at the end of the workflow I want to generate the report about the issues during the
#      research, meeting and build - summary will be good, what went wrong including
#      functionality and system issues, cost consumption, which agent contributed"
#
# Cost lives in three different places because the three kinds of work are recorded by three
# different services -- `discovery_runs.usage`, `meeting_runs.usage`, and `llm_calls` rows tied
# to a code task. Nothing joined them, so "what did this workflow cost" had no answer at all:
# the console could show $2.29 for the build while the research and the panel that preceded it,
# $1.74 between them, were invisible.

#: How a step's awaited work is found. Each kind is recorded by a different service, so each
#: needs its own table and its own idea of where the money is written down.
_WORK_SOURCES: dict[str, dict[str, str]] = {
    "discovery": {"table": "discovery_runs", "label": "Research"},
    "meeting": {"table": "meeting_runs", "label": "Panel"},
    "code_task": {"table": "code_task_runs", "label": "Build"},
    "preview": {"table": "app_previews", "label": "Preview"},
}


def _step_label(step_id: str, kind: str) -> str:
    """The step's name as the console shows it.

    Falls back to the raw id rather than inventing one: a report that renames `plrr` to
    something friendlier than the screen the operator just looked at costs them the match.
    """
    try:
        from .workflow_capabilities import get as _capability

        return _capability(step_id).label
    except Exception:  # noqa: BLE001 — a label is never worth failing a report for
        pass
    # A step id the catalogue does not carry -- `preview_final` is the `preview` capability
    # used twice -- still reads better as words than as snake_case.
    source = (_WORK_SOURCES.get(kind) or {}).get("label")
    return source or step_id.replace("_", " ").strip().capitalize() or step_id


def _usage_usd(usage: Any) -> float:
    return round(int(_as_dict(usage).get("cost_usd_micros") or 0) / 1_000_000, 4)


def _awaited(step_output: Any) -> tuple[str, str]:
    """The (kind, id) of the work a step started, or ("", "")."""
    awaited = _as_dict(step_output).get("await")
    if isinstance(awaited, dict):
        return str(awaited.get("kind") or ""), str(awaited.get("id") or "")
    return "", ""


def _build_detail(connection: Connection, run_id: str) -> dict[str, Any]:
    """Per-agent cost and turns for one code task, from the calls it actually made."""
    calls = connection.execute(
        text("SELECT stage, role, engine, model, turns, total_tokens, cost_usd_micros, "
             "       latency_ms, ok FROM llm_calls WHERE code_task_run_id = :r "
             " ORDER BY created_at"),
        {"r": run_id},
    ).mappings().all()
    return {
        "usd": round(sum(int(c["cost_usd_micros"] or 0) for c in calls) / 1_000_000, 4),
        "calls": [
            {"stage": c["stage"], "role": c["role"], "engine": c["engine"],
             "model": c["model"], "turns": int(c["turns"] or 0),
             "usd": round(int(c["cost_usd_micros"] or 0) / 1_000_000, 4),
             "seconds": int((c["latency_ms"] or 0) / 1000), "ok": bool(c["ok"])}
            for c in calls
        ],
    }


def _stalls_between(connection: Connection, started, ended) -> dict[str, Any]:
    """Host stalls that OVERLAPPED this run. Same rule as `assemble`: the question is what
    THIS run lost, not whether the machine has ever been slow."""
    if not started or not ended:
        return {"stalled_seconds": 0.0, "stall_events": 0, "longest_stall_seconds": 0.0}
    rows = connection.execute(
        text("SELECT data FROM events WHERE event_type = 'host.stalled' AND (data->>'source') IS NOT NULL "
             "   AND created_at BETWEEN :a AND :b"),
        {"a": started, "b": ended},
    ).mappings().all()
    seconds = [float(_as_dict(r["data"]).get("stalled_seconds") or 0) for r in rows]
    return {"stalled_seconds": round(sum(seconds), 2), "stall_events": len(seconds),
            "longest_stall_seconds": round(max(seconds, default=0.0), 2)}


def assemble_workflow(
    connection: Connection, project_id: str, workflow_run_id: str
) -> dict[str, Any]:
    """One workflow run end to end: every step, what it cost, who did it, and what broke.

    Read-only and deterministic, for the same reason as `assemble`: a retrospective that
    occasionally invents a cause is worse than none, and this one runs after every workflow.
    Missing evidence is reported as missing rather than guessed at.
    """
    run = connection.execute(
        text("""
            SELECT r.id, r.status, r.actor, r.input, r.blocked_reason, r.error,
                   r.created_at, r.updated_at,
                   d.name AS workflow_name, d.workflow_key, v.version
              FROM workflow_runs r
              LEFT JOIN workflow_definitions d ON d.id = r.workflow_definition_id
              LEFT JOIN workflow_versions v ON v.id = r.workflow_version_id
             WHERE r.id = :r AND r.project_id = :p
        """),
        {"r": workflow_run_id, "p": project_id},
    ).mappings().first()
    if run is None:
        return {"ok": False, "error": f"no workflow run {workflow_run_id}"}

    started, ended = run["created_at"], run["updated_at"]
    elapsed_s = max(0.0, (ended - started).total_seconds()) if started and ended else 0.0

    # ORDERED BY WHEN THEY RAN, not when they were written. Every step of a run is INSERTED in
    # one transaction, so created_at is identical across all ten and ordering by it returned
    # them arbitrarily -- the first report listed six pending steps above the three that had
    # actually happened. Steps that have not started sort last, which is where they belong.
    #
    # COALESCE rather than started_at alone: an approval gate is completed by an UPDATE that
    # sets completed_at and never sets started_at, so ordering on started_at put the approved
    # spec AFTER the build it had unblocked.
    step_rows = connection.execute(
        text("SELECT id, step_id, step_kind, status, attempt, output, last_error, decision_id, "
             "       evidence, started_at, completed_at, created_at "
             "  FROM workflow_step_runs WHERE workflow_run_id = :r "
             " ORDER BY COALESCE(started_at, completed_at) ASC NULLS LAST, created_at"),
        {"r": workflow_run_id},
    ).mappings().all()

    steps: list[dict[str, Any]] = []
    contributors: dict[str, dict[str, Any]] = {}
    total_usd = 0.0
    list_usd = 0.0
    # THE MODEL CALLS A STEP MADE ITSELF (a role agent judging a case, not a build it started):
    # attributed to the workflow run and its step (llm_telemetry run_id / stage). They used to
    # be invisible here, so a run of four model calls read "$0.00" (2026-10-31).
    own_calls: dict[str, dict[str, Any]] = {}
    try:
        for c in connection.execute(
                text("SELECT stage, role, model, COALESCE(cost_usd_micros, 0) AS micros, "
                     "       COALESCE(included_in_subscription, false) AS included "
                     "  FROM llm_calls WHERE code_task_run_id = :r"),
                {"r": workflow_run_id}).mappings().all():
            slot = own_calls.setdefault(str(c["stage"] or ""), {"calls": [], "list": 0.0,
                                                                "billed": 0.0})
            usd_ = int(c["micros"] or 0) / 1_000_000
            slot["list"] += usd_
            slot["billed"] += 0.0 if c["included"] else usd_
            slot["calls"].append({"role": c["role"], "model": c["model"], "usd": usd_})
    except Exception:  # noqa: BLE001 — a report without this line is still a report
        own_calls = {}

    for row in step_rows:
        kind, work_id = _awaited(row["output"])
        source = _WORK_SOURCES.get(kind)
        step: dict[str, Any] = {
            "step_id": row["step_id"],
            "status": row["status"],
            "attempt": int(row["attempt"] or 0),
            "error": row["last_error"],
            "kind": kind or None,
            "work_id": work_id or None,
            "label": _step_label(row["step_id"], kind),
            "usd": 0.0,
            "seconds": None,
            "agents": [],
        }
        if row["started_at"] and row["completed_at"]:
            step["seconds"] = int((row["completed_at"] - row["started_at"]).total_seconds())

        # A step that ADOPTED a preview records the id in its output rather than on an
        # `await` handle, because it did not start the work it is reporting on.
        adopted_preview = _as_dict(row["output"]).get("preview_id")
        if adopted_preview:
            step["preview_id"] = str(adopted_preview)

        # WHO APPROVED IT, IF ANYONE. Read from the evidence the gate wrote rather than
        # assumed — the same lesson that produced `decided_by` on the approval record.
        for item in (row["evidence"] if isinstance(row["evidence"], list) else []):
            entry = _as_dict(item)
            if entry.get("kind") in {"approval", "auto_approval"}:
                step["approved_by"] = entry.get("decided_by")
                step["approved_automatically"] = bool(entry.get("automatic")) or (
                    entry.get("kind") == "auto_approval")

        if source and work_id:
            if kind == "code_task":
                detail = _build_detail(connection, work_id)
                step["usd"] = detail["usd"]
                step["calls"] = detail["calls"]
                for call in detail["calls"]:
                    role = str(call["role"] or "unattributed")
                    slot = contributors.setdefault(
                        role, {"role": role, "calls": 0, "turns": 0, "usd": 0.0,
                               "models": set()})
                    slot["calls"] += 1
                    slot["turns"] += call["turns"]
                    slot["usd"] = round(slot["usd"] + call["usd"], 4)
                    if call["model"]:
                        slot["models"].add(call["model"])
                work = connection.execute(
                    text("SELECT status, pull_request_number, error FROM code_task_runs "
                         " WHERE id = :i"), {"i": work_id}).mappings().first()
                if work:
                    step["work_status"] = work["status"]
                    step["pull_request_number"] = work["pull_request_number"]
                    step["work_error"] = work["error"]
            elif kind in {"discovery", "meeting"}:
                work = connection.execute(
                    text(f"SELECT status, usage FROM {source['table']} WHERE id = :i"),
                    {"i": work_id}).mappings().first()
                if work:
                    step["work_status"] = work["status"]
                    step["usd"] = _usage_usd(work["usage"])
                    model = _as_dict(work["usage"]).get("model")
                    # ONE LINE PER AGENT, and research and the panel are each one agent's
                    # work as far as the ledger is concerned: their services record a single
                    # usage total rather than per-call rows, so attributing them per persona
                    # would be inventing a split that was never measured.
                    role = source["label"].lower()
                    slot = contributors.setdefault(
                        role, {"role": role, "calls": 0, "turns": 0, "usd": 0.0,
                               "models": set()})
                    slot["calls"] += 1
                    slot["usd"] = round(slot["usd"] + step["usd"], 4)
                    if model:
                        slot["models"].add(str(model))
        mine = own_calls.get(str(row["step_id"])) if not (source and work_id) else None
        if mine:
            step["usd"] = round(mine["billed"], 4)
            step["list_usd"] = round(mine["list"], 4)
            for call in mine["calls"]:
                role = str(call["role"] or "unattributed")
                slot = contributors.setdefault(
                    role, {"role": role, "calls": 0, "turns": 0, "usd": 0.0, "models": set()})
                slot["calls"] += 1
                slot["usd"] = round(slot["usd"] + call["usd"], 4)
                if call["model"]:
                    slot["models"].add(str(call["model"]))
        list_usd = round(list_usd + float(step.get("list_usd", step["usd"]) or 0), 4)
        total_usd = round(total_usd + float(step["usd"] or 0), 4)
        steps.append(step)

    work_ends = [r["completed_at"] for r in step_rows
                 if r["completed_at"] and str(r.get("step_kind") or "") != "decision"
                 and str(r["status"]) in ("succeeded", "skipped", "failed")]
    last_work = max(work_ends) if work_ends else ended
    if last_work and ended and last_work > ended:
        last_work = ended
    host = host_is_degraded(_stalls_between(connection, started, ended),
                            elapsed_seconds=elapsed_s)
    stalled_s = float(host.get("stalled_seconds") or 0)
    working_s = max(0.0, ((last_work - started).total_seconds() if (last_work and started)
                          else elapsed_s) - stalled_s)

    run_findings = workflow_findings(steps, host)
    product = _product_and_build(connection, project_id, steps)

    return {
        "product": product,
        "next_steps": next_steps(product, run_findings),
        "ok": True,
        "run_id": workflow_run_id,
        "workflow": run["workflow_name"] or run["workflow_key"],
        "version": run["version"],
        "status": run["status"],
        "directive": str(_as_dict(run["input"]).get("directive") or "").strip(),
        "blocked_reason": run["blocked_reason"],
        "error": run["error"],
        "started_at": started.isoformat() if started else None,
        "elapsed_minutes": round(elapsed_s / 60, 1),
        # WORKING TIME ENDS WHERE THE WORK DID. A run waiting at an approval gate kept counting
        # "working" until it was cancelled three hours later (192 min for 3 min of work).
        "working_minutes": round(working_s / 60, 1),
        "waiting_minutes": round(max(0.0, elapsed_s - stalled_s - working_s) / 60, 1),
        "total_usd": total_usd,
        "list_usd": list_usd,
        "steps": steps,
        "contributors": sorted(
            ({**c, "models": sorted(c["models"])} for c in contributors.values()),
            key=lambda c: c["usd"], reverse=True),
        "host": host,
        "findings": run_findings,
    }


def workflow_findings(steps: list[dict[str, Any]], host: dict) -> list[dict[str, str]]:
    """What went wrong across the run — functionality and system, kept apart.

    Kept apart deliberately. Mixing them is how "BOSS is regressing" got said about a machine
    that was thrashing, and it is why `host` is reported as its own area rather than folded
    into the pipeline's score.
    """
    out: list[dict[str, str]] = []

    if host.get("state") in {"degraded", "critical"}:
        out.append({
            "severity": "blocker" if host["state"] == "critical" else "warn",
            "area": "system",
            "finding": host.get("detail", "the machine did not schedule BOSS reliably"),
            "action": "Fix the machine before reading anything below as a pipeline problem. "
                      "That time is not the pipeline's and re-running will not recover it.",
        })

    for step in steps:
        label = step.get("label") or step.get("step_id")
        if step.get("status") == "failed" or step.get("work_status") == "failed":
            out.append({
                "severity": "blocker", "area": "functionality",
                "finding": f"{label} failed: "
                           f"{step.get('error') or step.get('work_error') or 'no reason recorded'}",
                "action": "Read the step's own record before re-running; a failure with no "
                          "reason recorded is itself the thing to fix.",
            })
        elif int(step.get("attempt") or 0) > 1:
            out.append({
                "severity": "warn", "area": "functionality",
                "finding": f"{label} needed {step['attempt']} attempts",
                "action": "A step that only succeeds on a retry is a flake until proven "
                          "otherwise. Check what differed between the attempts.",
            })
        if step.get("approved_automatically"):
            out.append({
                "severity": "info", "area": "governance",
                "finding": f"{label} was approved automatically by "
                           f"{step.get('approved_by') or 'policy'}, not by a person",
                "action": "That is the configured behaviour, not a fault. It is recorded here "
                          "so a reader never has to assume a human saw it.",
            })

    return out


def workflow_markdown(report: dict[str, Any]) -> str:
    """The run as a document somebody would actually read.

    ORDERED FOR A READER, NOT FOR THE DATABASE. The first version of this led with cost and a
    step table, which answers a question almost nobody has at the end of a build. What people
    want, in this order: what is this thing, can I try it, what did it ship, does it work, and
    what should I do next. Money and elapsed time are real but they are the footnote.

    Concise on purpose. Agent summaries run to pages; every one of them is trimmed to a
    paragraph, because a report nobody finishes is a report nobody reads.

    Markdown because the artifact pipeline already turns markdown into a PDF anyone can open,
    mail, or file.
    """
    if not report.get("ok"):
        return f"# Run report\n\n{report.get('error') or 'nothing to report'}\n"

    product = report.get("product") or {}
    prd = product.get("prd") or {}
    build = product.get("build") or {}
    criteria = product.get("acceptance") or []
    lines: list[str] = []

    # THE THING'S NAME, not the brief that asked for it. A directive is a paragraph, and
    # slicing it produced "Build an incident timeline builder. Who it is for: an on-call
    # engineer writing the post-mortem the morning aft…" as an H1 — which names nothing and
    # reads as a broken sentence. The panel titles its PRD; use that when there is one.
    directive = " ".join(str(report.get("directive") or "").split())
    title = product.get("name") or directive or report.get("workflow") or "Workflow run"
    lines += [f"# {title[:110]}{'…' if len(title) > 110 else ''}", ""]
    lines += [f"*{report.get('workflow')} · {report.get('status')} · "
              f"{report.get('elapsed_minutes')} min · ${report.get('total_usd'):.2f}*", ""]

    # 1. WHAT IT IS — the panel's own summary, not the directive repeated back.
    if prd.get("summary"):
        lines += ["## What it is", "", _trim(prd["summary"], 600), ""]
    if product.get("preview_url"):
        state = product.get("preview_status") or "unknown"
        lines += [f"**Try it:** `{product['preview_url']}` ({state})", ""]

    # 2. WHAT SHIPPED
    if build:
        lines += ["## What shipped", ""]
        facts = []
        if build.get("files_changed"):
            facts.append(f"{build['files_changed']} files changed")
        if build.get("test_files"):
            facts.append(f"{len(build['test_files'])} test files")
        if build.get("pull_request_url"):
            facts.append(f"[pull request]({build['pull_request_url']})")
        if facts:
            lines += [" · ".join(facts), ""]
        # LABELLED, AND SECOND. This is the implementing agent's own prose and it opens
        # wherever that agent chose — on this run, with a paragraph explaining why a failing
        # test was not its fault. Presented as the section's summary it reads as what the
        # build delivered; presented as the engineer's note it reads as what it is.
        if build.get("summary"):
            lines += ["**Engineer's note.** " + build["summary"], ""]
    # 3. HOW TO USE IT — the screens in the order a person meets them. The journey section is
    # usually a flowchart and cannot answer this; the screens can.
    screens = product.get("screens") or []
    if screens:
        lines += ["## How to use it", ""]
        for index, screen in enumerate(screens, start=1):
            lines.append(f"{index}. **{screen['name']}** — {screen['does']}")
        lines.append("")
    elif prd.get("journey"):
        lines += ["## How to use it", "", _trim(prd["journey"], 700), ""]

    # 4. DOES IT WORK — the honest version, which is usually the useful one.
    lines += ["## Does it work", ""]
    verdicts = build.get("verdicts") or {}
    if verdicts:
        lines += ["| Check | Verdict |", "| --- | --- |"]
        for label, key in (("Adversarial review", "review"), ("Security", "security"),
                           ("Consistency", "consistency")):
            lines.append(f"| {label} | {verdicts.get(key) or 'not recorded'} |")
        lines.append("")
    if build.get("review_summary"):
        lines += ["**What the reviewer found.** " + build["review_summary"], ""]
    commands = build.get("commands_run") or []
    written = len(build.get("test_files") or [])
    if written or commands:
        ran = ", ".join(f"`{c['command']}`" for c in commands) or "nothing"
        lines += [f"**Tests.** {written} test file(s) written. "
                  f"Recorded verification: {ran}.", ""]

    # 5. ACCEPTANCE CRITERIA — listed, never ticked without evidence.
    if criteria:
        lines += ["## Acceptance criteria", ""]
        for item in criteria:
            lines.append(f"- **{item['id']}** — {item['text']}")
        lines.append("")
        # A tick beside a criterion nobody checked is the most expensive kind of wrong: it is
        # the exact claim a reader repeats to somebody else.
        lines += ["> Not verified against the build — nothing in this run tested them.", ""]

    # 6. WHAT TO DO NEXT
    steps_next = report.get("next_steps") or []
    if steps_next:
        lines += ["## What to do next", ""]
        for priority in ("P1", "P2", "P3"):
            at_level = [n for n in steps_next if n["priority"] == priority]
            for item in at_level:
                lines.append(f"- **{priority}** — {item['item']}")
                lines.append(f"  - {item['why']}")
        lines.append("")

    # 7. WHAT WENT WRONG IN THE RUN ITSELF — the pipeline, not the product.
    findings = report.get("findings") or []
    serious = [f for f in findings if f["severity"] != "info"]
    if serious:
        lines += ["## Problems during the run", ""]
        for finding in serious:
            lines.append(f"- **{finding['area']}** — {finding['finding']}")
        lines.append("")

    # 8. THE FOOTNOTE.
    done = len([s for s in report["steps"] if s["status"] in {"succeeded", "skipped"}])
    lines += ["## Run facts", ""]
    lines.append(f"{done} of {len(report['steps'])} steps · "
                 f"{report['working_minutes']} min working of {report['elapsed_minutes']} "
                 f"elapsed · ${report['total_usd']:.2f}")
    lines.append("")
    if report.get("contributors"):
        lines += ["| Agent | Turns | Cost |", "| --- | --- | --- |"]
        for who in report["contributors"]:
            lines.append(f"| {who['role']} | {who['turns'] or '—'} | ${who['usd']:.2f} |")
        lines.append("")
    auto = [f for f in findings if f["severity"] == "info"]
    for note in auto:
        lines.append(f"*{note['finding']}.*")
    if auto:
        lines.append("")
    # THE BENCHMARK RECORD (services.benchmark), last: measured values, null where unmeasured.
    if isinstance(report.get("benchmark"), dict):
        from .benchmark import as_yaml

        lines += ["## Benchmark record", "", "```yaml", as_yaml(report["benchmark"]), "```", ""]

    return "\n".join(lines) + "\n"


# --- what was actually built ---------------------------------------------------------------
#
# The first version of this report answered "what did it cost". That is a fraction of what
# somebody wants at the end of a build, and the operator said so:
#
#     "What I want in the report is the details of what was done from research, meeting
#      outcome, what feature was builded and tested, test pass results, even end user
#      acceptance criteria, and how to use the app.. these are very useful for the user who
#      interested to see after building the app, then what else we could build on top of the
#      existing implementation - what can be improved, P1, P2 and other guidance for next
#      steps"
#
# All of it is already recorded. The panel's PRD carries the summary, the screens, the user
# journey and numbered acceptance criteria; the code task's state carries what was implemented,
# which test files exist, what actually ran, three reviewer verdicts and the findings that were
# deferred rather than fixed. Nothing here is generated — it is joined.

#: PRD headings, matched on WORDS rather than numbers. The panel numbers its sections and the
#: numbering moves between runs ("## 9. Acceptance Criteria" one time, "## 8." the next), so
#: anchoring on the number is how a section silently goes missing from every later report.
_PRD_SECTIONS: dict[str, tuple[str, ...]] = {
    "summary": ("summary",),
    "journey": ("user journey", "journey"),
    "screens": ("screens",),
    "acceptance": ("acceptance criteria",),
}


def _prd_sections(markdown: str) -> dict[str, str]:
    """Split a PRD into the handful of sections a reader of the report actually wants."""
    if not markdown:
        return {}
    import re as _re

    found: dict[str, str] = {}
    headings = list(_re.finditer(r"^(#{1,4})\s+(.+)$", markdown, _re.MULTILINE))
    for index, heading in enumerate(headings):
        title = heading.group(2).strip().lower()
        # Strip the leading "9." / "5.3" so matching is on the words alone.
        title = _re.sub(r"^[\d.]+\s*", "", title)
        for key, needles in _PRD_SECTIONS.items():
            if key in found:
                continue
            if any(title == n or title.startswith(n) for n in needles):
                end = (headings[index + 1].start() if index + 1 < len(headings)
                       else len(markdown))
                found[key] = markdown[heading.end():end].strip()
    return found


def _acceptance_criteria(section: str) -> list[dict[str, str]]:
    """The numbered criteria, as id + text.

    Reported WITHOUT a pass/fail mark unless something actually checked them. A tick beside a
    criterion nobody verified is the most expensive kind of wrong: it is the exact claim a
    reader will repeat to somebody else.
    """
    import re as _re

    out: list[dict[str, str]] = []
    for line in (section or "").splitlines():
        line = line.strip()
        if not line or not _re.match(r"^\d+[.)]\s", line):
            continue
        body = _re.sub(r"^\d+[.)]\s*", "", line)
        label = _re.match(r"\*\*(AC\d+)\s*[—\-–:]?\s*([^*]*)\*\*", body)
        if label:
            out.append({"id": label.group(1),
                        "text": (label.group(2).strip().rstrip(".") or "").strip(),
                        "detail": body[label.end():].strip()})
        else:
            out.append({"id": f"AC{len(out) + 1}", "text": body[:160], "detail": ""})
    return out


def _trim(text: Any, limit: int = 420) -> str:
    """One readable paragraph. Agent summaries run to pages and the report is meant to be read.

    Cut on a sentence boundary where there is one near the limit, because a paragraph that
    stops mid-clause reads as truncated data rather than as a summary.
    """
    cleaned = " ".join(str(text or "").split())
    if len(cleaned) <= limit:
        return cleaned
    window = cleaned[:limit]
    stop = max(window.rfind(". "), window.rfind("; "))
    return (window[:stop + 1] if stop > limit * 0.6 else window.rstrip()) + " …"


def _prose(markdown: str) -> str:
    """PRD text with the diagrams taken out.

    Panels draw. A PRD section is as likely to be a mermaid flowchart or an ASCII box sketch
    as a sentence, and pasting either into a report gives the reader a wall of pipes and
    arrows where an explanation should be. The first version did exactly that: "How to use it"
    rendered a raw ```mermaid block, and "Screens" rendered a box diagram.
    """
    import re as _re

    without_fences = _re.sub(r"```.*?```", " ", markdown or "", flags=_re.S)
    # Loose ASCII sketches escape the fences often enough to be worth dropping too.
    kept = [ln for ln in without_fences.splitlines()
            if not _re.match(r"^\s*[|+\-]{4,}", ln) and ln.count("|") < 3]
    return " ".join(" ".join(kept).split())


def _screens(section: str) -> list[dict[str, str]]:
    """The screens, each with its one-line description — which is what "how do I use it" is.

    The journey section is frequently nothing but a flowchart, so it cannot answer that
    question. The screens section can: the panel writes one bolded name per screen followed by
    what happens on it, in the order a person meets them.
    """
    import re as _re

    out: list[dict[str, str]] = []
    text = _prose(section)
    parts = _re.split(r"\*\*([A-Z][A-Za-z /&-]{2,40})\*\*", text)
    # re.split with one group yields [pre, name, body, name, body, ...]
    for index in range(1, len(parts) - 1, 2):
        name = parts[index].strip()
        body = _trim(parts[index + 1].lstrip(" —-–:"), 220)
        if body:
            out.append({"name": name, "does": body})
    return out


def _product_and_build(
    connection: Connection, project_id: str, steps: list[dict[str, Any]]
) -> dict[str, Any]:
    """What was specified, what was built, and what actually verified it.

    Joined from three places that already hold it: the panel's PRD artifact, the code task's
    own state, and the preview the run started.
    """
    out: dict[str, Any] = {"prd": {}, "acceptance": [], "build": {}, "preview_url": ""}

    # THIS RUN'S PANEL, NOT THE PROJECT'S NEWEST.
    #
    # This looked up the most recent PRD in the whole project and used the meeting id only as
    # a guard, so every run in a project was titled after whatever was built LAST: five
    # different runs all reported themselves as "Incident Timeline Builder", each with that
    # app's summary, screens and acceptance criteria attached to somebody else's build. A
    # report that confidently describes the wrong product is worse than one that describes
    # nothing. `meeting_runs.minutes_artifact_id` is the direct link and always has been.
    meeting_id = next((s["work_id"] for s in steps if s.get("kind") == "meeting"), None)
    if meeting_id:
        row = connection.execute(
            text("SELECT a.content FROM meeting_runs m "
                 "  JOIN artifacts a ON a.id = m.minutes_artifact_id "
                 " WHERE m.id = :m AND a.content->>'prd_markdown' IS NOT NULL"),
            {"m": meeting_id},
        ).mappings().first()
        if row:
            prd = _as_dict(row["content"]).get("prd_markdown") or ""
            import re as _re

            heading = _re.search(r"^#\s+(.+)$", prd, _re.MULTILINE)
            if heading:
                out["name"] = _re.sub(r"^(PRD|Spec)\s*[:\-–]\s*", "",
                                      heading.group(1).strip()).strip()
            sections = _prd_sections(prd)
            out["prd"] = {k: _prose(v) for k, v in sections.items()
                          if k != "acceptance"}
            out["screens"] = _screens(sections.get("screens", ""))
            out["acceptance"] = _acceptance_criteria(sections.get("acceptance", ""))

    build_id = next((s["work_id"] for s in steps if s.get("kind") == "code_task"), None)
    if build_id:
        row = connection.execute(
            text("SELECT state, pull_request_url, pull_request_number, status "
                 "  FROM code_task_runs WHERE id = :i"),
            {"i": build_id},
        ).mappings().first()
        if row:
            state = _as_dict(row["state"])
            # WHAT RAN, NOT WHAT WAS WRITTEN. These are different numbers and the gap between
            # them is the single most useful line in the report: this build wrote twelve test
            # files and its only recorded verification was `npm install`.
            ran = [t for t in (state.get("tests") or []) if isinstance(t, dict)]
            out["build"] = {
                "run_id": build_id,
                "status": row["status"],
                "summary": _trim(state.get("impl_summary")),
                # The reviewer's own words. `adversarial_verdict` alone says "refuted" and
                # hides that what was refuted was a BLOCKER in the core feature.
                "review_summary": _trim(state.get("review_summary"), 500),
                "files_changed": state.get("files_changed"),
                "test_files": list(state.get("test_files") or []),
                "commands_run": [
                    {"command": str(t.get("command") or ""), "ok": bool(t.get("ok")),
                     "exit_code": t.get("exit_code")}
                    for t in ran
                ],
                "tests_passed": state.get("tests_passed"),
                "verdicts": {
                    "review": state.get("adversarial_verdict"),
                    "security": state.get("security_verdict"),
                    "consistency": state.get("consistency_verdict"),
                },
                "deferred_findings": list(state.get("deferred_findings") or []),
                "has_e2e": bool(_as_dict(state.get("ux_evidence")).get("has_e2e")),
                "pull_request_url": row["pull_request_url"],
            }

    # THE ADDRESS THAT ACTUALLY OPENS, and the id is not always on the `await` handle: a
    # preview the run ADOPTED records `preview_id` in its output instead, because it did not
    # start the work it is reporting on.
    preview_id = next((s["work_id"] for s in steps if s.get("kind") == "preview"), None)
    if not preview_id:
        preview_id = next((s["preview_id"] for s in steps if s.get("preview_id")), None)
    if preview_id:
        # NOT the url column, and not the one in the step output. Both hold the preview's
        # INTERNAL address — this run recorded three different ports for one preview
        # (52401, 34625) and neither opened. The preview binds to loopback inside the API
        # host, so the gateway path is the only address a browser can use.
        from .preview_service import operator_preview_open_url

        try:
            out["preview_url"] = operator_preview_open_url(preview_id)
        except Exception:  # noqa: BLE001 — a link is never worth failing a report for
            out["preview_url"] = f"/v1/previews/{preview_id}/web/"
        row = connection.execute(
            text("SELECT status FROM app_previews WHERE id = :i"), {"i": preview_id},
        ).mappings().first()
        out["preview_status"] = row["status"] if row else None
    return out


def next_steps(product: dict[str, Any], findings_so_far: list[dict[str, str]]
               ) -> list[dict[str, str]]:
    """What to do next, ranked — P1 is "do not build on this until it is fixed".

    Derived, never invented. Every entry points at something recorded: a test file that was
    written and never run, a reviewer verdict that was not clean, a finding the build chose to
    defer. The point of the ranking is that "what should I do next" has an answer that is not
    an opinion.
    """
    build = product.get("build") or {}
    out: list[dict[str, str]] = []

    written = len(build.get("test_files") or [])
    commands = build.get("commands_run") or []
    looks_like_a_test = any(
        any(word in (c["command"] or "").lower()
            for word in ("pytest", "unittest", "jest", "vitest", "npm test", "npm run test",
                         "go test", "cargo test", "playwright"))
        for c in commands
    )
    if written and not looks_like_a_test:
        ran = ", ".join(f"`{c['command']}`" for c in commands) or "nothing"
        out.append({
            "priority": "P1", "area": "verification",
            "item": f"{written} test file(s) were written and never run — the recorded "
                    f"verification was {ran}.",
            "why": "The build is green on a command that installs dependencies. Nothing here "
                   "has been shown to work, including the parts the summary says work.",
        })

    if build.get("verdicts", {}).get("consistency") == "drift":
        out.append({
            "priority": "P2", "area": "consistency",
            "item": "The consistency reviewer reported drift.",
            "why": "Something in the change does not match the rest of the codebase. Cheap to "
                   "settle now, expensive once more code copies it.",
        })
    if build.get("verdicts", {}).get("review") == "refuted":
        # A REFUTED BLOCKER IS NOT A P2. The verdict field says "refuted" and nothing else;
        # what was refuted, on this run, was a reviewer reporting "a confirmed, reproducible
        # defect ... that silently corrupts the core merge feature". Whether the refutation is
        # right or not, that is the first thing to settle, not the third.
        blocking = "blocker" in str(build.get("review_summary") or "").lower()
        out.append({
            "priority": "P1" if blocking else "P2", "area": "review",
            "item": ("A reviewer finding marked BLOCKER was refuted rather than fixed."
                     if blocking else
                     "The adversarial reviewer's objections were refuted rather than fixed."),
            "why": "A refutation is an argument, not a test — and no test was run here to "
                   "settle it. Read the finding before building on this."
                   if blocking else
                   "A refutation is an argument, not a test. Worth a second look at the "
                   "specific claims before building on top of them.",
        })

    for finding in build.get("deferred_findings") or []:
        severity = str(finding.get("severity") or "minor").lower()
        where = str(finding.get("path") or "")
        line = finding.get("line")
        out.append({
            "priority": "P1" if severity in {"major", "critical", "blocker"} else "P2",
            "area": "deferred",
            "item": _trim(finding.get("text"), 260)
                    + (f"  ({where}:{line})" if where else ""),
            "why": "Found during review and deliberately not fixed in this build.",
        })

    if written and not build.get("has_e2e"):
        out.append({
            "priority": "P3", "area": "coverage",
            "item": "No end-to-end test exercises the app through its interface.",
            "why": "Unit tests prove the pieces. Only an end-to-end run proves a user can "
                   "complete the journey the PRD describes.",
        })

    if any(f["area"] == "system" for f in findings_so_far):
        out.append({
            "priority": "P3", "area": "environment",
            "item": "The machine stalled during this run.",
            "why": "Timings above are not a fair reading of the pipeline until that is fixed.",
        })
    return out
