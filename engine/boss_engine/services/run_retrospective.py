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
        text("SELECT data FROM events WHERE event_type = 'host.stalled' "
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
             "  FROM events WHERE event_type = 'host.stalled' "
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
        text("SELECT data FROM events WHERE event_type = 'host.stalled' "
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
    advice = operator_advice(host)
    if advice.get("show"):
        advice["gauges"] = gauges.get("bands", {})
        if gauges.get("reasons"):
            advice["detail"] = (advice.get("detail", "") + " Right now: "
                                + ", ".join(gauges["reasons"]) + ".")
    return advice
