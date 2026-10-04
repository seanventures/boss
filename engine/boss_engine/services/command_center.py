"""What the Command Center shows: the factory as workflow runs.

WHY THIS EXISTS. The Command Center was built when work meant `tasks`, and it kept reading
them after the product moved to workflows:

  * "Live coordination" derived who was busy from `tasks.owner_role_id`, a field the page's own
    comment called unpopulated, so it animated idle orbs whatever was happening and disagreed
    with Agent work.
  * "In flight" listed `tasks` and mapped their stage to five areas, never to Workflow. A run
    started by Build creates no task, so the thing the operator had just started never
    appeared on the landing page.
  * "Running now" counted only code-task runs; "Shipped · 7d" counted PRs opened.
  * "Pending decisions" duplicated the Awaiting-you number with rows that linked nowhere.

An operator opens this page to answer three questions: what is running, what needs me, and
how did the recent work turn out. Every answer here is a workflow run (or something that stops
one), with the step it is on, the agent on that step, and how long it has been there, so each
row can link straight into the run.

Assembly is pure (`assemble`) so the rules are testable without a database; `build` does the
reading.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Optional

from sqlalchemy import text

from . import decision_actions, workflow_capabilities

#: A run in one of these is in flight.
LIVE_RUN = frozenset({"queued", "validating", "running", "waiting", "paused", "blocked"})
#: A run in one of these has finished, one way or another.
DONE_RUN = frozenset({"completed", "failed", "deadlettered", "cancelled", "compensated"})
#: A run that stopped on its own (not one an operator stopped) and can be retried.
STOPPED_RUN = frozenset({"failed", "deadlettered", "blocked"})
#: A step that is the run's current position.
CURRENT_STEP = ("waiting", "running", "leased", "ready", "queued")
FAILED_STEP = frozenset({"failed", "deadlettered"})

#: How far back "recent" and "stopped and waiting for a retry" reach. A run that failed a month
#: ago is history, not something that needs the operator today.
RECENT = timedelta(days=7)
STALE_FAILURE = timedelta(days=14)


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _aware(value: Any) -> Optional[datetime]:
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def humanize(key: str) -> str:
    """`release_manager` -> `Release manager`. The fallback when a role has no display name."""
    words = str(key or "").replace("_", " ").replace("-", " ").strip()
    return words[:1].upper() + words[1:] if words else ""


def _first_line(value: Any, limit: int = 220) -> str:
    line = str(value or "").strip().splitlines()[0] if str(value or "").strip() else ""
    return line if len(line) <= limit else line[: limit - 1].rstrip() + "…"


def run_subject(run: Mapping[str, Any]) -> str:
    """What the run is delivering, in the operator's words — not the workflow's name."""
    data = run.get("input") if isinstance(run.get("input"), dict) else {}
    for key in ("title", "name", "app_name", "subject", "brief", "directive", "topic"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return _first_line(value, 120)
    return ""


def run_name(run: Mapping[str, Any]) -> str:
    """THE ONE NAME for a run on every surface (agents-home D9): what it is delivering, in the
    operator's words (`run_subject`), else its workflow's name. Never `<workflow> #<hash>`: a
    hash is an id, and the operator typed a title for this run."""
    return (run_subject(run) or str(run.get("workflow_name") or "").strip()
            or humanize(str(run.get("workflow_key") or "")) or "this run")


def _spec_steps(run: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    spec = run.get("spec") if isinstance(run.get("spec"), dict) else {}
    return {str(s.get("id")): s for s in (spec.get("steps") or []) if isinstance(s, dict)}


def step_label(spec_step: Mapping[str, Any], step_id: str) -> str:
    for key in ("label", "name", "title"):
        value = spec_step.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    binding = spec_step.get("binding") if isinstance(spec_step.get("binding"), dict) else {}
    capability = binding.get("capability") or spec_step.get("capability")
    if isinstance(capability, str) and capability in workflow_capabilities.CAPABILITIES:
        return workflow_capabilities.CAPABILITIES[capability].label
    return humanize(step_id)


def step_agent(spec_step: Mapping[str, Any], step: Mapping[str, Any],
               names: Mapping[str, str]) -> dict[str, Optional[str]]:
    """Who is on this step, and how they came to be — the question an operator asks first.

    `how` is one of: picked (BOSS chose for an agent step, recorded in its output), named (the
    workflow says who), default (the capability's own captain), person (a gate waiting on you),
    auto (an agent step BOSS will pick for when it starts) or none.
    """
    output = step.get("output") if isinstance(step.get("output"), dict) else {}
    binding = spec_step.get("binding") if isinstance(spec_step.get("binding"), dict) else {}

    def who(key: str, how: str) -> dict[str, Optional[str]]:
        key = key.strip().lower()
        return {"role_key": key, "name": names.get(key) or humanize(key), "how": how}

    picked = output.get("agent")
    ran_as = output.get("ran_as") if isinstance(output.get("ran_as"), dict) else {}
    # WHO RAN IT, as the steps stream records it on every step kind (`output.ran_as`). Not an
    # agent when the step ran as the run's actor, the workflow itself or a decision model.
    if ran_as.get("agent") and ran_as.get("how") not in {
            "run_actor", "workflow_runner", "legacy_workflow_runner", "decision_model"}:
        picked = ran_as.get("agent")
    if isinstance(picked, str) and picked.strip():
        return who(picked, "picked")
    named = spec_step.get("captain") or binding.get("agent")
    if isinstance(named, str) and named.strip():
        if named.strip().lower() == "human":
            return {"role_key": None, "name": "You", "how": "person"}
        return who(named, "named")
    if binding.get("agent_work"):
        return {"role_key": None, "name": "BOSS picks the agent when the step starts",
                "how": "auto"}
    if str(step.get("step_kind") or spec_step.get("kind") or "") == "decision":
        return {"role_key": None, "name": "You", "how": "person"}
    capability = binding.get("capability") or spec_step.get("capability")
    cap = workflow_capabilities.CAPABILITIES.get(str(capability or ""))
    if cap is not None and cap.captain:
        return who(cap.captain, "default")
    return {"role_key": None, "name": None, "how": "none"}


def _rec_reason(decision: Mapping[str, Any]) -> str:
    rec = decision.get("recommendation")
    if isinstance(rec, str):
        try:
            import json as _j
            rec = _j.loads(rec)
        except ValueError:
            rec = {}
    return str((rec or {}).get("reason") or "") if isinstance(rec, dict) else ""


def _latest_per_step(steps: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """One row per step id: the newest attempt or iteration decides its state."""
    latest: dict[str, Mapping[str, Any]] = {}
    order: list[str] = []
    for step in steps:
        sid = str(step.get("step_id"))
        if sid not in latest:
            order.append(sid)
        latest[sid] = step
    return [latest[sid] for sid in order]


def step_index(steps_done: int, step_count: int, status: Any = None) -> int:
    """Which step a run is on, 1-based: the one after those done (succeeded or skipped).

    A finished run is on its last step. Never past `step_count`, never an estimate."""
    count = max(int(step_count or 0), 0)
    if count == 0:
        return 0
    if str(status or "") == "completed":
        return count
    return max(1, min(int(steps_done or 0) + 1, count))


def assemble(*, project_id: str, runs: list[Mapping[str, Any]],
             steps: list[Mapping[str, Any]], decisions: list[Mapping[str, Any]],
             readiness: list[Mapping[str, Any]], names: Mapping[str, str],
             now: datetime,
             stopped: Optional[Mapping[str, list]] = None,
             notes: Optional[Mapping[str, str]] = None) -> dict[str, Any]:
    """Shape the read rows into the three answers. Pure: no I/O, `now` is passed in.

    `stopped` is `{run_id: workflow_run_actions.stopped_for_operator(run, steps)}` from
    `load_runs` — the steps a run is stopped at for its operator, each with the answers the run
    offers. Left out, it is worked out from `runs`/`steps` with the same function.
    """
    from .workflow_run_actions import stopped_for_operator
    now = _aware(now) or datetime.now(timezone.utc)
    by_run: dict[str, list[Mapping[str, Any]]] = {}
    for step in steps:
        by_run.setdefault(str(step.get("workflow_run_id")), []).append(step)

    decision_by_id = {str(d.get("id")): d for d in decisions}
    run_of_decision: dict[str, Mapping[str, Any]] = {}

    running: list[dict[str, Any]] = []
    needs_you: list[dict[str, Any]] = []
    recent: list[dict[str, Any]] = []

    for run in runs:
        run_id = str(run.get("id"))
        status = str(run.get("status") or "")
        spec = _spec_steps(run)
        order = {sid: i for i, sid in enumerate(spec)}
        run_steps = sorted(_latest_per_step(by_run.get(run_id, [])),
                           key=lambda s: order.get(str(s.get("step_id")), len(order)))
        done = sum(1 for s in run_steps if s.get("status") in ("succeeded", "skipped"))
        step_count = max(len(spec), len(run_steps))
        base = {
            "run_id": run_id,
            "workflow_key": run.get("workflow_key"),
            "workflow_name": run.get("workflow_name") or humanize(str(run.get("workflow_key"))),
            "subject": run_subject(run),
            # What every title and row calls the run: its subject, else its workflow's name.
            "name": run_name(run),
            "status": status,
            "steps_done": done,
            "step_count": step_count,
            "started_at": _iso(run.get("created_at")),
        }
        # `step k of n` everywhere a run is named: the step after the ones done. Counted, not
        # estimated — the same number on the attention card and on the run's Live row.
        here = step_index(done, step_count, status)
        for step in run_steps:
            if step.get("decision_id"):
                run_of_decision[str(step.get("decision_id"))] = {
                    **base, "step_id": step.get("step_id"), "step_index": here,
                    "step_label": step_label(spec.get(str(step.get("step_id")), {}),
                                             str(step.get("step_id"))),
                }

        if status in LIVE_RUN:
            current = next((s for s in run_steps if s.get("status") in CURRENT_STEP), None)
            spec_step = spec.get(str(current.get("step_id")), {}) if current else {}
            since = _aware((current or {}).get("started_at")) or _aware(run.get("created_at"))
            running.append({
                **base,
                "step_id": current.get("step_id") if current else None,
                "step_label": (step_label(spec_step, str(current.get("step_id")))
                               if current else None),
                "step_status": current.get("status") if current else None,
                "agent": step_agent(spec_step, current, names) if current else None,
                "since": _iso(since),
                "elapsed_seconds": (int((now - since).total_seconds()) if since else None),
                "blocked_reason": _first_line(run.get("blocked_reason")) or None,
            })

        updated = _aware(run.get("updated_at")) or _aware(run.get("created_at"))
        # THE ONE RULE FOR "A STEP FAILED AND SOMEBODY HAS TO DECIDE" is
        # workflow_run_actions.stopped_for_operator, which the Workflow screen's run card, its
        # "Needs you" bucket and the factory briefing already read. The console-a stream built
        # this list from its own status sets and offered Retry on every row, from a button the
        # Command Center made up — so it could offer Try again where the run offers none (an
        # approval somebody refused, a superseded attempt). Each row now carries the run's own
        # answers, and the screen shows only those (integration of console-a with main's
        # 0fd6c45 "what a run offers is what the server allows").
        pairs = (stopped.get(run_id, []) if stopped is not None
                 else stopped_for_operator(dict(run), [dict(s) for s in by_run.get(run_id, [])]))
        if updated and now - updated <= STALE_FAILURE:
            for step, answers in pairs:
                spec_step = spec.get(str(step.get("step_id")), {})
                label = step_label(spec_step, str(step.get("step_id")))
                output = _output_of(step)
                # A GATE SOMEBODY SAID NO TO (agents-home D3): the step failed on a person's
                # answer, so the card says so, and its reason is their note.
                refused = bool(output.get("rejected_by_approver"))
                note = (notes or {}).get(str(output.get("decision_id") or "")) or ""
                needs_you.append({
                    "id": f"run:{run_id}:{step.get('step_id')}",
                    "kind": "failed_step",
                    "refused": refused,
                    "title": (f"“{label}” was rejected on {base['name']}" if refused
                              else f"“{label}” failed on {base['name']}"),
                    "detail": ((_first_line(note) or "You rejected it.") if refused else (
                               _first_line(step.get("last_error"))
                               or _first_line(run.get("error"))
                               or "The step stopped without recording why. Open the run to "
                                  "see its log.")),
                    "what_to_do": ("Retry asks again. Skip carries on without it." if refused
                                   else "Fix the cause if the message names one, then Retry "
                                        "the step. The run carries on from there."),
                    "run_id": run_id, "step_id": step.get("step_id"),
                    "workflow_name": base["workflow_name"],
                    "actions": [str(a["key"]) for a in answers],
                    # The run's own answers, as the Workflow screen gets them: what each is
                    # called, whether it can be taken now, and why not.
                    "answers": list(answers),
                    "run": {"id": run_id, "subject": base["name"],
                            "step_id": step.get("step_id"), "step_label": label,
                            "step_index": here, "step_count": step_count},
                    "at": _iso(step.get("completed_at") or updated),
                })

        if status in DONE_RUN and updated and now - updated <= RECENT:
            recent.append({
                **base,
                "outcome": "succeeded" if status == "completed" else status,
                "finished_at": _iso(run.get("completed_at") or run.get("updated_at")),
                "error": _first_line(run.get("error")) or None,
            })

    # WHAT NEEDS THE OPERATOR IS decision_actions.attention, the rule the Approvals inbox
    # filters on. This used to be its own status list, and the landing page and the inbox
    # disagreed: a pull request parked in a dead-end "Request changes" was waiting in one and
    # not the other. Expired hold-default decisions are counted apart, not listed here: most
    # are about work that has long moved on, and on the live project there were 127 of them.
    expired_answerable = 0
    for decision in decisions:
        where = decision_actions.attention(decision, now)
        if where == "expired":
            expired_answerable += 1
        if where != "needs_action":
            continue
        described = decision_actions.describe(decision, now)
        run = run_of_decision.get(str(decision.get("id")))
        hint = described["guidance"] or {}
        if run:
            title = f"“{run['step_label']}” is waiting for you on {run['name']}"
        else:
            title = _first_line(decision.get("question"), 160)
        needs_you.append({
            "id": str(decision.get("id")),
            "kind": "approval",
            "decision_kind": described["kind"],
            "title": title,
            # WHY IT ASKS, in the server's words (agents-home D6) — never the approve button's
            # effect, which the card already shows on the button.
            "detail": (hint.get("what_happened") or _first_line(decision.get("reason"))
                       or _first_line(_rec_reason(decision)) or ""),
            "what_to_do": hint.get("what_to_do"),
            "fix_area": hint.get("area"),
            "fix_label": hint.get("link_label"),
            "decision_id": decision.get("id"),
            "run_id": run["run_id"] if run else None,
            "step_id": run["step_id"] if run else None,
            "workflow_name": run["workflow_name"] if run else None,
            "actions": [a["action"] for a in described["allowed_actions"]],
            # The whole offer — label, tone, effect, what it needs — so a card elsewhere draws
            # exactly the inbox's buttons. `actions` stays the list of names older readers use.
            "allowed_actions": [dict(a) for a in described["allowed_actions"]],
            "run": ({"id": run["run_id"], "subject": run.get("name") or "",
                     "step_id": run["step_id"], "step_label": run["step_label"],
                     "step_index": run.get("step_index"), "step_count": run.get("step_count")}
                    if run else None),
            "risk_tier": decision.get("risk_tier"),
            "at": _iso(decision.get("created_at")),
        })

    for gap in readiness:
        if gap.get("done"):
            continue
        needs_you.append({
            "id": f"ready:{gap.get('id') or gap.get('key') or _first_line(gap.get('title'), 40)}",
            "kind": "readiness",
            "title": str(gap.get("title") or "Finish setting up"),
            "detail": str(gap.get("detail") or ""),
            "what_to_do": gap.get("what_to_do") or (
                "Nothing can be built until this is done. It takes a minute."),
            "fix_area": "setup",
            # Lands on the row that needs it (`?area=setup&focus=<row>`), not the page top.
            "fix_focus": gap.get("focus"),
            "fix_label": "Fix in Repo and models",
            "actions": [],
            "at": None,
        })

    # WHAT STOPS WORK FIRST. A gate holds a run that is otherwise ready; a failed step has
    # already stopped one; setup gaps stop the next one; a refusal is a notice.
    # A STOPPED RUN FIRST (agents-home D12): it has already stopped, and its card was hidden
    # under "+6 more" behind routine gates. Then gates, riskiest first; then notices; setup last.
    def rank(item: Mapping[str, Any]) -> tuple[int, int, str]:
        if item["kind"] == "failed_step":
            first = 0
        elif item["kind"] == "approval" and item.get("decision_kind") != "refusal":
            first = 1
        elif item["kind"] == "approval":
            first = 2
        else:
            first = 3
        return (first, -int(item.get("risk_tier") or 0), str(item.get("at") or ""))

    needs_you.sort(key=rank)
    running.sort(key=lambda r: (r["status"] not in {"waiting", "blocked"},
                                -(r.get("elapsed_seconds") or 0)))
    recent.sort(key=lambda r: str(r.get("finished_at") or ""), reverse=True)
    return {
        "project_id": project_id,
        "observed_at": now.isoformat(),
        "running": running,
        "needs_you": needs_you,
        "recent": recent[:8],
        "counts": {
            "running": len(running),
            # ONE NUMBER (agents-home D2): DECISIONS waiting on the operator —
            # decision_actions.attention, what Approvals' badge and Needs action count. A failed
            # step and a setup gap are listed, and counted apart (`stopped`, `setup`).
            "needs_you": sum(1 for n in needs_you if n["kind"] == "approval"),
            "stopped": sum(1 for n in needs_you if n["kind"] == "failed_step"),
            "setup": sum(1 for n in needs_you if n["kind"] == "readiness"),
            "approvals": sum(1 for n in needs_you if n["kind"] == "approval"),
            "expired_answerable": expired_answerable,
            "failed_steps": sum(1 for n in needs_you if n["kind"] == "failed_step"),
            "readiness": sum(1 for n in needs_you if n["kind"] == "readiness"),
            "succeeded_7d": sum(1 for r in recent if r["outcome"] == "succeeded"),
            "failed_7d": sum(1 for r in recent if r["outcome"] in ("failed", "deadlettered")),
        },
    }


def read(connection, settings, project_id: str,
         now: Optional[datetime] = None) -> dict[str, Any]:
    """Read what `assemble` needs. Two run queries and one decision query, whatever the size.

    Returned as `assemble`'s keyword arguments, so another view of the same factory (Agents ›
    Live: agents_live_board) reads these rows ONCE and passes them through `assemble` rather
    than writing its own query that could disagree."""
    now = now or datetime.now(timezone.utc)
    # Apply the deadlines first, as GET /v1/status and the Approvals inbox do. Without it an
    # overdue decision whose default is reject or approve dropped off this page (attention
    # calls it resolved) while nothing had yet recorded the expiry or applied the default, so
    # the operator saw it vanish with no "auto-rejected" line anywhere (review round 3).
    from .decisions import DecisionService

    DecisionService(settings).expire_overdue(connection, project_id)
    runs = [dict(r) for r in connection.execute(
        text(
            """
            SELECT r.id, r.status, r.actor, r.input, r.blocked_reason, r.error,
                   r.created_at, r.updated_at, r.completed_at,
                   d.workflow_key, d.name AS workflow_name, v.version, v.spec
            FROM workflow_runs r
            JOIN workflow_definitions d ON d.id = r.workflow_definition_id
            JOIN workflow_versions v ON v.id = r.workflow_version_id
            WHERE r.project_id = :project_id
              AND COALESCE(d.namespace, 'system') = 'product'
              AND r.parent_run_id IS NULL
              AND (r.status = ANY(:live) OR r.updated_at >= :since)
            ORDER BY r.updated_at DESC
            LIMIT 60
            """
        ),
        {"project_id": project_id, "live": sorted(LIVE_RUN),
         "since": now - STALE_FAILURE},
    ).mappings()]
    steps: list[dict[str, Any]] = []
    if runs:
        steps = [dict(s) for s in connection.execute(
            text(
                """
                SELECT s.workflow_run_id, s.step_id, s.step_kind, s.status, s.output,
                       s.last_error, s.decision_id, s.started_at, s.completed_at,
                       s.updated_at, s.binding_snapshot->'approval' AS approval,
                       dd.status AS decision_status
                FROM workflow_step_runs s
                LEFT JOIN decisions dd ON dd.id = s.decision_id
                WHERE s.workflow_run_id = ANY(:ids)
                ORDER BY s.created_at, s.iteration
                """
            ),
            {"ids": [r["id"] for r in runs]},
        ).mappings()]
    decisions = [dict(d) for d in connection.execute(
        text(
            """
            SELECT id, task_id, question, status, risk_tier, default_action,
                   recommendation, deadline_at, created_at, delegated_to
            FROM decisions
            WHERE project_id = :project_id AND status = ANY(:candidates)
            ORDER BY risk_tier DESC, created_at DESC
            """
        ),
        # No LIMIT: the count has to equal the inbox's, which reads every row. The old cap of
        # 50 made "Approvals" stop at 50 while the inbox said more.
        {"project_id": project_id,
         "candidates": list(decision_actions.ATTENTION_CANDIDATE_STATUSES)},
    ).mappings()]
    names = {
        str(row["role_key"]).lower(): str(row["display_name"])
        for row in connection.execute(
            text("SELECT role_key, display_name FROM roles WHERE project_id = :project_id"),
            {"project_id": project_id},
        ).mappings()
    }
    readiness: list[dict[str, Any]] = []
    try:
        with connection.begin_nested():
            readiness = setup_gaps(connection, settings, project_id)
    except Exception:  # noqa: BLE001 - a setup read must never blank the landing page
        readiness = []
    from .workflow_run_actions import load_runs, stopped_for_operator

    loaded = load_runs(connection, project_id, [r["id"] for r in runs])
    stopped = {run_id: stopped_for_operator(run, run_steps)
               for run_id, (run, run_steps) in loaded.items()}
    # The note somebody wrote when they said no to a gate: the reason on its failed-step card.
    refused_ids = [str(d) for d in (
        (_output_of(st).get("decision_id")) for pairs in stopped.values() for st, _ in pairs
        if _output_of(st).get("rejected_by_approver")) if d]
    notes = {str(r["id"]): str(r["resolution_note"] or "") for r in connection.execute(
        text("SELECT id, resolution_note FROM decisions WHERE id = ANY(:ids)"),
        {"ids": refused_ids},
    ).mappings()} if refused_ids else {}
    return {"project_id": project_id, "runs": runs, "steps": steps, "decisions": decisions,
            "readiness": readiness, "names": names, "now": now, "stopped": stopped,
            "notes": notes}


def _output_of(step: Mapping[str, Any]) -> dict[str, Any]:
    out = step.get("output")
    if isinstance(out, str):
        try:
            import json as _j
            out = _j.loads(out)
        except ValueError:
            out = {}
    return out if isinstance(out, dict) else {}


def build(connection, settings, project_id: str,
          now: Optional[datetime] = None) -> dict[str, Any]:
    """The Command Center: `assemble` over `read`, each card attributed and each running row
    given its verb — by the same functions Agents › Live uses (agents_live_board.enrich), so a
    card here and its twin on Live show the same face and the same buttons."""
    rows = read(connection, settings, project_id, now)
    out = assemble(**rows)
    from .agents_live_board import enrich

    enrich(connection, project_id, out, rows)
    return out


def needs_you_count(connection, settings, project_id: str,
                    now: Optional[datetime] = None) -> int:
    """HOW MANY DECISIONS WAIT ON THE OPERATOR (decision_actions.attention) — the Command
    Center's `counts.needs_you`, Approvals' badge and Needs action (agents-home D2). The top-bar pill, Agents › Live and
    the Command Center all say this number; none of them counts its own (spec §6.3)."""
    return int(build(connection, settings, project_id, now)["counts"]["needs_you"])


def setup_gaps(connection: Any, settings: Any, project_id: str) -> list[dict[str, Any]]:
    """What Repo and models says still stops work: unfinished rows, failing rows, and a token
    that stops working within three days. Read from the same view the page renders, so the
    Command Center and the page cannot disagree (and nothing here calls GitHub)."""
    from . import setup_view

    view = setup_view.build_basics(connection, settings, project_id)
    found = setup_view.basics(view)
    gaps: list[dict[str, Any]] = []
    if not found["github"]:
        failing = next((r for r in view["repository"]["repos"]
                        if r["pill"]["tone"] == "red"), None)
        gaps.append({
            "id": "github", "done": False, "focus": "repository",
            "title": (f"Repository: {failing['pill']['label']}" if failing
                      else "Connect a repository"),
            "detail": failing["pill"]["tip"] if failing else
            "BOSS needs a repository to open pull requests."})
    if not found["model"]:
        failing = next((m for m in view["models"] if m["pill"]["tone"] == "red"), None)
        gaps.append({
            "id": "model", "done": False, "focus": "models",
            "title": (f"{failing['label']}: {failing['pill']['label']}" if failing
                      else "Connect a model"),
            "detail": failing["pill"]["tip"] if failing else
            "A subscription, an API key or a local model."})
    soon = setup_view.expiring_within({"repository": view["repository"], "models": []}, 3)
    if soon:
        days = soon["days"]
        gaps.append({
            "id": "expiring", "done": False, "focus": "repository",
            "title": f"Token expires in {days} day{'s' if days != 1 else ''}",
            "detail": "Builds stop when it expires. Make a new token now.",
            "what_to_do": "Paste a new token in Repo and models before it expires."})
    return gaps
