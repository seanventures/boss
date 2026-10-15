"""THE PRE-CHECK READS BOSS'S RECENT BEHAVIOUR, not only one run's configuration.

WHY THIS EXISTS
---------------
`workflow_precheck` reads the configuration of ONE run: its spec, its consent, its app tree. The
reviewers who read HelpDesk Delivery (D1)'s failures on the laptop (2026-10-13) found what that
cannot see, because they read LOGS, EVENTS and CODE: the crash explained by the wrong variable,
a review panel that ran into its 420 s deadline, warm rounds that changed nothing, approvals
held by step-up, automatic retries refused for lack of a grant, worker restarts under live
builds, thousands of shepherd/captain events, secrets filed under the app's old key, a swallowed
KeyError. All of it was on disk before the next run started.

So the pre-check gains three run-level families, each read-only and cheap (indexed reads over
the last `history_days`, no model call, no network):

  * history.*  — what recent runs of THIS workflow and app did: recurring failure fingerprints
                 (and whether they still occur on the running BOSS), review-panel degradation,
                 warm rounds, step-up holds, refused retries, event noise, restarts under live
                 builds, grants ended under live child work, transactions held open.
  * logs.*     — the app's own start logs from its previous builds (the app_serve_checked
                 records), run through the SAME crash-to-variable matcher the build uses
                 (run_env.RunEnvironment.unset_named_in); BOSS's own service log (app_logs, the
                 table `read_logs` reads) for programming errors a broad except swallowed.
                 Every excerpt goes through the redaction layer (redaction.redact_secrets).
  * data.*     — hygiene that makes the NEXT run fail: the app's key split in two with secrets
                 under the old one, throwaway keys not yet generated, targets that never
                 deployed holding ports, leases and reservations past expiry, pending decisions
                 of runs that are already over.

Each result is the pre-check's shape ({id, verdict, evidence, fix}); a result BOSS can repair
itself carries `details.autofix` and says "fixable: BOSS can do this". The repairs are in
FIXES: safe, reversible, recorded (`precheck.autofix_applied`, actor "boss precheck"),
idempotent, and applied only when the caller asks (`--fix` / `fix: true`). Anything that
changes real infrastructure or deletes data stays a person's choice.
"""
from __future__ import annotations

import hashlib
import re
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Mapping, Optional

from sqlalchemy import text

from . import workflow_precheck as wp

FAMILIES = ("history", "logs", "data")
#: The pseudo-step each family's rows are shown under in the report.
FAMILY_STEP = {f: f"({f})" for f in FAMILIES}
ACTOR = "boss precheck"
DEFAULT_HISTORY_DAYS = 7
#: A pending decision of a finished run older than this is litter, not a question.
STALE_DECISION_DAYS = 1
#: A target this old with no successful and no live deployment is not one a run is about to
#: use: it only holds its port.
STALE_TARGET_AGE = timedelta(hours=2)
#: Events per day above which a kind is noise (the reviewers counted thousands).
NOISE_BUDGET_PER_DAY = {"shepherd.proposed": 500, "captain.coordinated": 500,
                        "code_task.duplicate_suppressed": 50, "shepherd.retry": 50}
#: A review seat this close to the panel deadline is degrading, not merely slow.
NEAR_DEADLINE = 0.85
#: Known BOSS bugs by check id, for --explain ("BOSS bug: <fingerprint>").
KNOWN_ISSUES = {
    "history.warm_rounds": "warm_fix_rounds returns 0 when any non-convergeable gate (a template "
                           "gate) fails, so the judgment seats get no warm round either",
    "history.retry_refused": "a system-started retry has no person's authority "
                             "(identity_runs.start_code_task_run → NoAuthority)",
    "history.step_up": "the console does not replay an approval refused for step_up_required",
    "history.noise": "shepherd/captain emit an event per tick even when nothing changed",
    "history.restarts": "a worker/API restart kills the builds it was running",
    "history.grant_ended_live": "the run's grant ends while its child work is still live",
    "history.idle_in_transaction": "a tick holds one transaction across its whole sweep",
    "logs.crash_blame": "explain_crash blames the first unset name the log mentions, not the "
                        "one the app's error names",
    "logs.swallowed_errors": "a broad except logs and continues past a programming error",
    "data.split_app_keys": "the app key is re-derived from a renamed title; bindings stay under "
                           "the old key",
    "lifecycle.concurrent_push": "pass 2 pushes to the branch while pass 1 deploys it",
}


# ── registry ──────────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class RunCheck:
    id: str
    family: str
    label: str
    fn: Callable[[wp.Context], Any]


RUN_CHECKS: dict[str, RunCheck] = {}


def _run_check(check_id: str, label: str):
    family = check_id.split(".", 1)[0]
    if family not in FAMILIES:
        raise ValueError(check_id)

    def register(fn):
        RUN_CHECKS[check_id] = RunCheck(check_id, family, label, fn)
        return fn
    return register


def run_families(ctx: wp.Context, families: Iterable[str] = FAMILIES) -> list[dict[str, Any]]:
    """The run-level families as report rows: one pseudo-step per family."""
    wanted = [f for f in FAMILIES if f in set(families or ())]
    rows = []
    for family in wanted:
        checks = []
        for check in RUN_CHECKS.values():
            if check.family != family:
                continue
            started = time.monotonic()
            try:
                found = check.fn(ctx)
            except Exception as exc:  # noqa: BLE001 — a check that breaks says so; it never stops the rest
                found = wp.result(check.id, "unknown", f"the check itself failed: "
                                  f"{type(exc).__name__}: {exc}"[:400])
            for one in (found if isinstance(found, list) else [found]):
                if not one:
                    continue
                one["ms"] = int((time.monotonic() - started) * 1000)
                one["skip_allowed"] = False
                checks.append(one)
        if checks:
            rows.append({"step_id": FAMILY_STEP[family], "capability": family, "checks": checks})
    return rows


# ── shared reads ──────────────────────────────────────────────────────────────────────────────

def _days(ctx: wp.Context) -> int:
    return max(1, int(getattr(ctx, "history_days", 0) or DEFAULT_HISTORY_DAYS))


def _rows(ctx: wp.Context, sql: str, params: Optional[Mapping[str, Any]] = None,
          default: Any = None) -> list[dict[str, Any]]:
    out = ctx.savepoint(lambda: [dict(r) for r in ctx.connection.execute(
        text(sql), {"p": ctx.project_id, "d": _days(ctx), **(params or {})}).mappings().all()],
        default=default)
    return out if out is not None else []


def _when(value: Any) -> str:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).strftime("%m-%d %H:%M")
    return str(value or "?")


def _clip(value: Any, n: int = 160) -> str:
    from .redaction import redact_secrets

    s = re.sub(r"\s+", " ", redact_secrets(str(value or ""))).strip()
    return s if len(s) <= n else s[: n - 1] + "…"


_ID = re.compile(r"\b(?:crun|wfr|wsr|job|dlq|qg|tap|deprun|dlg|rcpt|asb|deptgt|dh|tpl|app|mrun)"
                 r"_[0-9a-z]+\b")


def fingerprint(message: str) -> tuple[str, str]:
    """(short hash, normalised text) of a failure message: paths, ids, hex and numbers folded, so
    the same failure in two runs (two sandboxes, two run ids) has one fingerprint."""
    t = str(message or "").lower()
    t = re.sub(r"(?:/[\w.@+-]+){2,}/?", "<path>", t)
    t = _ID.sub("<id>", t)
    t = re.sub(r"\b[0-9a-f]{7,}\b", "<hex>", t)
    t = re.sub(r"\d+", "<n>", t)
    t = re.sub(r"\s+", " ", t).strip()[:200]
    return hashlib.sha1(t.encode()).hexdigest()[:10], t


def known_issue_fingerprint(check: Mapping[str, Any]) -> str:
    """The stable id --explain names a BOSS bug by: the check id plus its evidence's shape."""
    _h, norm = fingerprint(f"{check.get('id')}|{str(check.get('evidence') or '')[:120]}")
    return "pc-" + hashlib.sha1(norm.encode()).hexdigest()[:10]


def _starts(ctx: wp.Context) -> dict[str, list[datetime]]:
    """When each BOSS service (api, worker) last started, from the line log_capture writes at
    every start into app_logs — the table `read_logs` reads. No docker socket."""
    def read() -> dict[str, list[datetime]]:
        out: dict[str, list[datetime]] = {"api": [], "worker": []}
        for r in _rows(ctx, "SELECT service, created_at FROM app_logs WHERE logger = "
                            "'boss_engine.services.log_capture' AND message LIKE "
                            "'activity-log capture installed for %' AND created_at > now() - "
                            "make_interval(days => :d) ORDER BY created_at"):
            out.setdefault(str(r["service"]), []).append(r["created_at"])
        return out
    return ctx.once("starts", read)


def running_since(ctx: wp.Context) -> Optional[datetime]:
    """When the running BOSS started (the last API start): the boundary of 'the current
    version' when the image carries no source stamp."""
    api = _starts(ctx).get("api") or []
    return api[-1] if api else None


def _built_at() -> Optional[datetime]:
    """The image's build time, as the build stamped it (BOSS_BUILT_AT; self_observability's
    `build` stamp reads the same variable). None when the image carries no stamp."""
    import os

    raw = (os.environ.get("BOSS_BUILT_AT") or "").strip()
    if not raw:
        return None
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def version_started(ctx: wp.Context) -> Optional[datetime]:
    """When the BOSS VERSION now running started: its image's first API start (the first start
    at or after BOSS_BUILT_AT — an image cannot have run before it was built), else the last
    API start. A failure before it happened on an earlier version, which may have been fixed:
    D1's pre-check called a 10-13 23:51 app-start failure `will_fail` after the upgrade that
    fixed it."""
    def compute() -> Optional[datetime]:
        api = _starts(ctx).get("api") or []
        if not api:
            return None
        built = _built_at()
        if built is not None:
            after = [t for t in api if isinstance(t, datetime) and t >= built]
            if after:
                return after[0]
        return api[-1]
    return ctx.once("version_started", compute)


def _version_line(ctx: wp.Context) -> str:
    import os

    sha = (os.environ.get("BOSS_SOURCE_SHA") or "").strip()
    since = version_started(ctx)
    return ((f"BOSS {sha[:8]}" if sha and sha != "unknown" else "the running BOSS")
            + (f" (up since {_when(since)})" if since else ""))


def _since_current(ctx: wp.Context, when: Any) -> Optional[bool]:
    since = version_started(ctx)
    if since is None or not isinstance(when, datetime):
        return None
    return when >= since


#: What a past failure from before the running version says instead of `will_fail`.
BEFORE_THIS_VERSION = "seen before this version; may be fixed"


def _before_this_version(ctx: wp.Context, when: Any) -> bool:
    """A failure that happened before the running version started (and so cannot, alone, say
    this version fails the same way). Unknown start time → False: the verdict stands."""
    return _since_current(ctx, when) is False


def scope(ctx: wp.Context) -> dict[str, Any]:
    """Recent runs of this workflow, and the builds of this app: the ones the workflow's steps
    started, and every build that is the same app by identity (the console's own grouping)."""
    def read() -> dict[str, Any]:
        from .delivery_pipeline import app_name_of_run
        from .naming import identity_key

        runs = _rows(ctx, "SELECT r.id, r.status, r.created_at, r.updated_at, r.grant_id FROM "
                          "workflow_runs r JOIN workflow_definitions d ON d.id = "
                          "r.workflow_definition_id WHERE r.project_id = :p AND "
                          "d.workflow_key = :k AND r.created_at > now() - make_interval(days "
                          "=> :d) ORDER BY r.created_at", {"k": ctx.workflow_key})
        run_ids = [r["id"] for r in runs]
        started = {str(r["c"]) for r in _rows(
            ctx, "SELECT DISTINCT output->>'code_task_run_id' AS c FROM workflow_step_runs "
                 "WHERE workflow_run_id = ANY(:ids) AND output ? 'code_task_run_id'",
            {"ids": run_ids})} if run_ids else set()
        if ctx.build_run_id:
            started.add(ctx.build_run_id)
        app = wp._app_name(ctx)  # noqa: SLF001 — the pre-check's own app name
        key = identity_key(app)
        cands = _rows(ctx, "SELECT id, title, status, phase, error, created_at, updated_at, "
                           "finalize_ctx, state->'app_serves' AS app_serves, "
                           "state->'panel_parallel' AS panel_parallel, "
                           "state->'review_panel_size' AS panel_size FROM code_task_runs "
                           "WHERE project_id = :p AND created_at > now() - make_interval(days "
                           "=> :d) ORDER BY created_at")
        app_ids = {str((c.get("finalize_ctx") or {}).get("app_id") or "") for c in cands
                   if c["id"] in started} - {""}
        builds, related = [], set()
        for c in cands:
            fctx = c.get("finalize_ctx") or {}
            name = identity_key(app_name_of_run(c))
            same = (c["id"] in started or (fctx.get("app_id") and fctx.get("app_id") in app_ids)
                    or (key and name == key))
            # The same app under an older or newer name ("HelpDesk" → "HelpDesk AI"): its
            # history is this app's history (data.split_app_keys says why they are two).
            if not same and key and name and related_keys(key, [name]):
                same = True
                related.add(app_name_of_run(c))
            if same:
                builds.append(c)
        return {"runs": runs, "run_ids": run_ids, "builds": builds,
                "build_ids": [b["id"] for b in builds], "app": app, "app_ids": sorted(app_ids),
                "related_names": sorted(related)}
    return ctx.once("behaviour_scope", read)


def _events(ctx: wp.Context, kinds: Iterable[str], *, entity_ids: Optional[list[str]] = None,
            days: Optional[int] = None) -> list[dict[str, Any]]:
    sql = ("SELECT event_type, entity_type, entity_id, created_at, data FROM events WHERE "
           "project_id = :p AND event_type = ANY(:k) AND created_at > now() - "
           "make_interval(days => :dd)")
    params: dict[str, Any] = {"k": list(kinds), "dd": days or _days(ctx)}
    if entity_ids is not None:
        if not entity_ids:
            return []
        sql += " AND entity_id = ANY(:e)"
        params["e"] = list(entity_ids)
    return _rows(ctx, sql + " ORDER BY created_at", params)


def _app_label(sc: Mapping[str, Any], fallback: str) -> str:
    label = f"'{sc['app'] or fallback}'"
    if sc.get("related_names"):
        label += " (with its earlier name(s) " + ", ".join(
            repr(n) for n in sc["related_names"]) + ")"
    return label


def _verdict(found: bool, risk: bool = False) -> str:
    return "unknown" if found or risk else "ready"


# ── history.* ─────────────────────────────────────────────────────────────────────────────────

_TERMINAL_BAD = ("failed", "blocked")


@_run_check("history.recurring_failures",
            "Failures that recur at the same step, and whether they still occur on this BOSS")
def check_recurring(ctx: wp.Context) -> dict[str, Any]:
    sc = scope(ctx)
    occurrences: list[dict[str, Any]] = []
    serve_errors = {}
    for b in sc["builds"]:
        serves = b.get("app_serves") if isinstance(b.get("app_serves"), dict) else {}
        if serves and serves.get("ok") is False and serves.get("error"):
            serve_errors[b["id"]] = serves["error"]
        if b["status"] in _TERMINAL_BAD and b.get("error"):
            occurrences.append({"where": "build", "run": b["id"], "at": b["updated_at"],
                                "message": b["error"]})
    for e in _events(ctx, ["code_task.app_serve_checked"], entity_ids=sc["build_ids"]):
        data = e.get("data") or {}
        if data.get("ok") is False and data.get("error"):
            serve_errors.setdefault(e["entity_id"], data["error"])
    gates = _rows(ctx, "SELECT code_task_run_id AS run, created_at, gates_failed FROM "
                       "quality_gates WHERE code_task_run_id = ANY(:ids) AND status <> 'pass'",
                  {"ids": sc["build_ids"]}) if sc["build_ids"] else []
    for g in gates:
        for gate in g.get("gates_failed") or []:
            msg = serve_errors.get(g["run"]) if gate == "app_serves" else None
            occurrences.append({"where": f"gate {gate}", "run": g["run"], "at": g["created_at"],
                                "message": msg or f"{gate} failed"})
    if sc["run_ids"]:
        for s in _rows(ctx, "SELECT workflow_run_id AS wfr, step_id, last_error, completed_at "
                            "FROM workflow_step_runs WHERE workflow_run_id = ANY(:ids) AND "
                            "status = 'failed' AND last_error IS NOT NULL", {"ids": sc["run_ids"]}):
            occurrences.append({"where": f"step {s['step_id']}", "run": s["wfr"],
                                "at": s["completed_at"], "message": s["last_error"]})
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for o in occurrences:
        fp, norm = fingerprint(o["message"])
        o["fp"], o["norm"] = fp, norm
        groups[(o["where"], fp)].append(o)
    recurring = sorted(((k, v) for k, v in groups.items() if len(v) >= 2),
                       key=lambda kv: (-len(kv[1]), kv[0][0]))
    if not occurrences:
        return wp.result("history.recurring_failures", "ready",
                         f"no failed build or step of {_app_label(sc, ctx.workflow_key)} in the "
                         f"last {_days(ctx)} day(s) ({len(sc['runs'])} run(s), "
                         f"{len(sc['builds'])} build(s))")
    latest_build = max(sc["builds"], key=lambda b: b["updated_at"]) if sc["builds"] else None
    latest_ids = {latest_build["id"]} if latest_build else set()
    if sc["runs"]:
        latest_ids.add(sc["runs"][-1]["id"])
    lines, still = [], []
    for (where, fp), items in recurring[:6]:
        last = max(items, key=lambda o: o["at"] or datetime.min.replace(tzinfo=timezone.utc))
        now_too = _since_current(ctx, last["at"])
        tag = ("still occurs on " + _version_line(ctx) if now_too else
               "not seen since " + _version_line(ctx) + " started — fixed since? unproven"
               if now_too is False else "BOSS's start time unknown")
        if now_too:
            still.append((where, fp, items, last))
        lines.append(f"failed {len(items)}× at {where}: {_clip(last['message'], 110)} "
                     f"[fp {fp}]; last on {last['run']} at {_when(last['at'])}; {tag}")
    head = (f"{len(occurrences)} failure(s) over {len(sc['runs'])} run(s) and "
            f"{len(sc['builds'])} build(s) of {_app_label(sc, ctx.workflow_key)} in "
            f"{_days(ctx)} day(s); {len(recurring)} recur")
    details = {"recurring": [{"where": w, "fingerprint": fp, "count": len(v),
                              "last_run": max(v, key=lambda o: str(o['at']))['run'],
                              "message": _clip(v[-1]['message'], 300)}
                             for (w, fp), v in recurring[:12]]}
    repeats_latest = [s for s in still if any(o["run"] in latest_ids for o in s[2])]
    if repeats_latest:
        where, fp, items, last = repeats_latest[0]
        return wp.result("history.recurring_failures", "will_fail", head + ". " + "; ".join(lines)
                         + f". The latest build ({last['run']}) failed {len(items)}× the same way "
                         f"at {where}, on the running BOSS: expect it again unless its cause "
                         "changed.",
                         action=f"Fix the cause of [{fp}] at {where} before the run (or start "
                                "anyway knowing it recurs).", details=details)
    return wp.result("history.recurring_failures", "unknown" if recurring else "ready",
                     head + (". " + "; ".join(lines) if lines else ""), details=details)


@_run_check("history.review_panel", "The review panel finishes inside its deadline")
def check_review_panel(ctx: wp.Context) -> dict[str, Any]:
    sc = scope(ctx)
    deadline = int(getattr(ctx.settings, "code_agent_review_timeout_seconds", 0) or 420)
    degraded_all = _events(ctx, ["code_task.review_degraded"])
    mine = [e for e in degraded_all if e["entity_id"] in set(sc["build_ids"])]
    for e in degraded_all:
        deadline = int((e.get("data") or {}).get("deadline_s") or deadline)
    seats = _rows(ctx, "SELECT code_task_run_id AS run, stage, latency_ms, ok, created_at FROM "
                       "llm_calls WHERE project_id = :p AND stage LIKE 'review%' AND created_at > "
                       "now() - make_interval(days => :d) AND latency_ms >= :near",
                  {"near": int(deadline * 1000 * NEAR_DEADLINE)})
    serial = [b["id"] for b in sc["builds"] if b.get("panel_parallel") is False
              and int(b.get("panel_size") or 0) > 1]
    again = _events(ctx, ["code_task.pr_review_again_requested", "code_task.pr_review_again_done"])
    open_req: dict[str, datetime] = {}
    rereviews = []
    for e in again:
        if e["event_type"].endswith("requested"):
            open_req[e["entity_id"]] = e["created_at"]
        elif e["entity_id"] in open_req:
            took = (e["created_at"] - open_req.pop(e["entity_id"])).total_seconds()
            rereviews.append((e["entity_id"], took))
    over = [(r, t) for r, t in rereviews if t > deadline]
    parts = [f"panel deadline {deadline}s",
             f"review_degraded {len(degraded_all)}× in the project ({len(mine)}× on this app's "
             "builds" + (f": {', '.join(e['entity_id'] for e in mine[-3:])}" if mine else "") + ")",
             f"{len(seats)} review seat(s) took ≥{int(NEAR_DEADLINE * 100)}% of the deadline"
             + (f" (max {max(s['latency_ms'] for s in seats) // 1000}s, on "
                f"{', '.join(sorted({str(s['run']) for s in seats})[:3])})" if seats else "")]
    if serial:
        parts.append(f"panel ran serially on {len(serial)} build(s) ({', '.join(serial[:3])})")
    if rereviews:
        parts.append(f"{len(rereviews)} PR re-review(s), longest {int(max(t for _, t in rereviews))}s"
                     + (f"; {len(over)} ran past the panel deadline — re-reviews have none"
                        if over else ""))
    if open_req:
        parts.append(f"{len(open_req)} re-review(s) requested and never finished "
                     f"({', '.join(list(open_req)[:3])})")
    found = bool(degraded_all or seats or serial or over or open_req)
    return wp.result("history.review_panel", _verdict(found), "; ".join(parts),
                     action=("A degraded seat's verdict is missing from the gate: expect "
                             "code_review_passed/security_clean to fail on a long change. BOSS "
                             "change: run seats in parallel and give re-reviews the panel's "
                             "deadline.") if found else "",
                     details={"deadline_s": deadline, "degraded": [e["entity_id"] for e in
                                                                    degraded_all]})


@_run_check("history.warm_rounds", "Warm fix rounds run, and change something when they do")
def check_warm_rounds(ctx: wp.Context) -> dict[str, Any]:
    from . import governed_code_task_service as g

    sc = scope(ctx)
    rounds = _events(ctx, ["code_task.inrun_convergence_round", "code_task.fix_round_noop"])
    by_run: dict[str, list[dict]] = defaultdict(list)
    noop_events = []
    for e in rounds:
        if e["event_type"] == "code_task.fix_round_noop":
            noop_events.append(e)
        else:
            by_run[e["entity_id"]].append(e.get("data") or {})
    finals = {r["run"]: set(r["gates_failed"] or []) for r in _rows(
        ctx, "SELECT code_task_run_id AS run, gates_failed FROM quality_gates WHERE "
             "code_task_run_id = ANY(:ids)", {"ids": list(by_run) or [""]})}
    unchanged = []
    for run, rs in by_run.items():
        sets = [frozenset(r.get("failing_gates") or []) for r in rs]
        same_twice = any(a == b for a, b in zip(sets, sets[1:]))
        final = finals.get(run)
        if same_twice or (final is not None and sets and sets[-1] <= final and final):
            unchanged.append((run, sorted(sets[-1])))
    configured = g.review_repair_rounds(ctx.settings)
    no_round = []
    for r in _rows(ctx, "SELECT code_task_run_id AS run, gates_failed FROM quality_gates WHERE "
                        "code_task_run_id = ANY(:ids) AND status <> 'pass'",
                   {"ids": sc["build_ids"] or [""]}):
        failing = set(r["gates_failed"] or [])
        if failing & g._JUDGMENT_GATES and g.warm_fix_rounds(  # noqa: SLF001
                failing, configured=configured, repair_attempts=0, use_microvm=False) == 0:
            no_round.append((r["run"], sorted(failing - g._CONVERGEABLE)))  # noqa: SLF001
    parts = []
    if unchanged:
        parts.append(f"{len(unchanged)} build(s) whose warm rounds left the same gates failing: "
                     + "; ".join(f"{r} ({', '.join(s)})" for r, s in unchanged[:4]))
    if noop_events:
        parts.append(f"{len(noop_events)} fix round(s) recorded as no-ops "
                     f"({', '.join(e['entity_id'] for e in noop_events[:3])}, "
                     f"{wp._money(sum(int((e.get('data') or {}).get('cost_usd_micros') or 0) for e in noop_events))} spent)")  # noqa: SLF001,E501
    if no_round:
        parts.append(f"{len(no_round)} of this app's failed build(s) had a judgment seat failing "
                     "AND a gate no warm round works on, so on the running BOSS they get NO warm "
                     "round at all (warm_fix_rounds → 0): " + "; ".join(
                         f"{r} (blocked by {', '.join(s)})" for r, s in no_round[:4]))
    if not parts:
        return wp.result("history.warm_rounds", "ready",
                         f"{sum(len(v) for v in by_run.values())} warm round(s) in "
                         f"{_days(ctx)} day(s); none repeated the same failing gates")
    return wp.result("history.warm_rounds", "unknown", "; ".join(parts),
                     action="BOSS change: give the judgment seats their round when a template "
                            "gate also fails; stop a round whose failing gates did not change.",
                     details={"no_warm_round": [r for r, _ in no_round],
                              "unchanged": [r for r, _ in unchanged]})


@_run_check("history.step_up", "Approvals are not held by an expired step-up")
def check_step_up(ctx: wp.Context) -> dict[str, Any]:
    from ..identity.authz import DEFAULT_STEP_UP_SECONDS

    denied = _rows(ctx, "SELECT ts, permission, actor_principal_id AS who FROM "
                        "identity_security_events WHERE outcome = 'denied' AND "
                        "details->>'reason' = 'step_up_required' AND event_type = "
                        "'authz.denied' AND ts > now() - make_interval(days => :d) ORDER BY ts")
    if not denied:
        return wp.result("history.step_up", "ready",
                         f"no call refused for step_up_required in {_days(ctx)} day(s)")
    by_perm = Counter(str(d["permission"]) for d in denied)
    approvals = [d for d in denied if d["permission"] == "decision:approve"]
    answered = [e["created_at"] for e in _events(
        ctx, ["decision.approved", "workflow.gate_answered", "decision.rejected"])]
    delays = []
    for d in approvals:
        nxt = next((t for t in answered if t >= d["ts"]), None)
        delays.append((nxt - d["ts"]).total_seconds() if nxt else None)
    waited = [x for x in delays if x is not None]
    evidence = (f"{len(denied)} call(s) refused for step_up_required (window "
                f"{DEFAULT_STEP_UP_SECONDS}s): " + ", ".join(f"{p} ×{n}" for p, n in
                                                             by_perm.most_common(5)))
    if approvals:
        evidence += (f"; {len(approvals)} of them approvals, last {_when(approvals[-1]['ts'])}"
                     + (f"; the next answer came {int(min(waited))}–{int(max(waited))}s later"
                        if waited else "; never answered after")
                     + " — the console does not replay a refused approval, so a gate waits for "
                       "the person to notice and press again")
    return wp.result("history.step_up", "unknown" if approvals else "ready", evidence,
                     action=("Re-confirm (step up) right before answering this run's gates. "
                             "BOSS change: the console re-sends the approval after step-up.")
                     if approvals else "",
                     details={"approvals_refused": len(approvals), "by_permission": dict(by_perm)})


@_run_check("history.retry_refused", "BOSS's automatic retries are allowed to start")
def check_retry_refused(ctx: wp.Context) -> dict[str, Any]:
    sc = scope(ctx)
    refused = [b for b in sc["builds"] if b["status"] == "blocked"
               and str(b.get("error") or "").startswith("No one's authority covers this build")]
    project = _rows(ctx, "SELECT count(*) AS n, max(created_at) AS last FROM code_task_runs "
                         "WHERE project_id = :p AND status = 'blocked' AND error LIKE "
                         "'No one''s authority covers this build%' AND created_at > now() - "
                         "make_interval(days => :d)")
    denied = _rows(ctx, "SELECT ts FROM identity_security_events WHERE event_type = "
                        "'authz.denied' AND permission = 'code_task:retry' AND ts > now() - "
                        "make_interval(days => :d)")
    n_project = int((project[0] if project else {}).get("n") or 0)
    if not refused and not denied and not n_project:
        return wp.result("history.retry_refused", "ready",
                         f"no automatic retry refused for authority in {_days(ctx)} day(s)")
    last = refused[-1] if refused else None
    evidence = (f"{len(refused)} automatic retr{'y' if len(refused) == 1 else 'ies'} of this app "
                f"refused \"No one's authority covers this build\" ({n_project} in the project)"
                + (f"; last {last['id']} at {_when(last['updated_at'])}"
                   + (" on the running BOSS" if _since_current(ctx, last["updated_at"]) else "")
                   if last else "")
                + (f"; {len(denied)} code_task:retry call(s) denied" if denied else ""))
    return wp.result("history.retry_refused", "unknown", evidence + " — a failed build will not "
                     "be retried by BOSS; it waits for a person",
                     action="Be ready to press Retry yourself. BOSS change: a system retry "
                            "carries the original person's grant.",
                     details={"refused": [b["id"] for b in refused]})


@_run_check("history.noise", "Event noise stays inside its budget")
def check_noise(ctx: wp.Context) -> dict[str, Any]:
    counts = {r["event_type"]: r for r in _rows(
        ctx, "SELECT event_type, count(*) AS n, count(DISTINCT entity_id) AS ents FROM events "
             "WHERE project_id = :p AND event_type = ANY(:k) AND created_at > now() - interval "
             "'1 day' GROUP BY 1", {"k": list(NOISE_BUDGET_PER_DAY)})}
    over = {k: v for k, v in counts.items() if int(v["n"]) > NOISE_BUDGET_PER_DAY[k]}
    dup = _rows(ctx, "SELECT entity_id, data->>'reason' AS reason, count(*) AS n FROM events "
                     "WHERE project_id = :p AND event_type = 'shepherd.retry' AND created_at > "
                     "now() - make_interval(days => :d) GROUP BY 1, 2 HAVING count(*) > 3 "
                     "ORDER BY 3 DESC LIMIT 5")
    idle = _rows(ctx, "SELECT count(*) AS n FROM events WHERE project_id = :p AND event_type = "
                      "'captain.coordinated' AND created_at > now() - interval '1 day' AND "
                      "COALESCE((data->>'assignments')::int, 0) = 0")
    evidence = "last 24h: " + ", ".join(
        f"{k} {int(counts.get(k, {}).get('n') or 0)} (budget {b})"
        for k, b in NOISE_BUDGET_PER_DAY.items())
    if idle and counts.get("captain.coordinated"):
        evidence += (f"; {int(idle[0]['n'])} captain.coordinated with 0 assignments")
    if dup:
        evidence += "; the shepherd retried the same run for the same reason " + ", ".join(
            f"{d['entity_id']} ×{d['n']}" for d in dup[:3])
    found = bool(over or dup)
    return wp.result("history.noise", _verdict(found), evidence,
                     action=("Noise buries the events that explain this run's failure. BOSS "
                             "change: emit on change only; suppress a duplicate retry before it "
                             "is proposed.") if found else "",
                     details={"over_budget": sorted(over)})


@_run_check("history.restarts", "No worker/API restart lands inside a build")
def check_restarts(ctx: wp.Context) -> dict[str, Any]:
    sc = scope(ctx)
    starts = _starts(ctx)
    api, worker = starts.get("api") or [], starts.get("worker") or []
    drains = _rows(ctx, "SELECT created_at, message FROM app_logs WHERE logger = 'boss.drain' "
                        "AND created_at > now() - make_interval(days => :d) ORDER BY created_at")
    interrupted = [d for d in drains if re.search(r"\b[1-9]\d* (?:build|process tree)\(s\) "
                                                  r"(?:interrupted|ended)", str(d["message"]))]
    restarts = _clusters(sorted(api + worker))
    hit = []
    for b in sc["builds"]:
        if b["status"] not in ("failed", "blocked", "cancelled"):
            continue
        inside = [t for t in restarts if b["created_at"] < t < b["updated_at"]]
        if inside:
            hit.append((b, inside))
    killed = [b for b in sc["builds"] if re.search(r"exited 143|killed by the platform|"
                                                    r"interrupted by (?:a )?restart",
                                                    str(b.get("error") or ""), re.I)]
    durations = [(b["updated_at"] - b["created_at"]).total_seconds() for b in sc["builds"]
                 if (b["updated_at"] - b["created_at"]).total_seconds() > 120]
    per_day = len(restarts) / _days(ctx)
    typical = sorted(durations)[len(durations) // 2] if durations else 0
    dead = _rows(ctx, "SELECT id, updated_at FROM jobs WHERE project_id = :p AND status = "
                      "'deadlettered' AND last_error::text ILIKE '%killed by the platform%' AND "
                      "updated_at > now() - make_interval(days => :d)")
    evidence = (f"{len(restarts)} restart(s) of BOSS in {_days(ctx)} day(s) (~{per_day:.0f}/day; "
                f"app_logs start lines; last API start {_when(api[-1]) if api else 'unknown'})")
    if typical:
        evidence += (f"; this app's builds run ~{int(typical // 60)} min, so at this rate "
                     f"~{min(100, int(per_day * typical / 864))}% of them overlap a restart")
    if interrupted:
        evidence += f"; {len(interrupted)} drain(s) ended a live build's processes: " + ", ".join(
            f"{_when(d['created_at'])} ({_clip(d['message'], 70)})" for d in interrupted[-3:])
    if hit:
        evidence += f"; {len(hit)} of this app's failed builds spanned a restart: " + ", ".join(
            f"{b['id']} ({_when(b['created_at'])}–{_when(b['updated_at'])}, restart "
            f"{_when(ts[0])})" for b, ts in hit[:4])
    if killed:
        evidence += f"; {len(killed)} build(s) ended killed (exit 143): " + ", ".join(
            b["id"] for b in killed[:3])
    if dead:
        evidence += f"; {len(dead)} job(s) deadlettered 'killed by the platform'"
    found = bool(hit or killed or dead or interrupted or per_day * typical > 0.25 * 86400)
    return wp.result("history.restarts", _verdict(found), evidence,
                     action=("Do not restart or upgrade BOSS while this run builds. BOSS change: "
                             "drain waits for (or resumes) live builds.") if found else "",
                     details={"spanned": [b["id"] for b, _ in hit]})


def _clusters(times: list[datetime], gap_s: int = 300) -> list[datetime]:
    """Start lines within `gap_s` of each other are one restart (api and worker, several
    processes): the first of each cluster."""
    out: list[datetime] = []
    for t in times:
        if not out or (t - out[-1]).total_seconds() > gap_s:
            out.append(t)
    return out


@_run_check("history.grant_ended_live", "A run's grant is not ended under its live child work")
def check_grant_ended(ctx: wp.Context) -> dict[str, Any]:
    ended = _rows(ctx, "SELECT ts, grant_id, details FROM identity_security_events WHERE "
                       "event_type IN ('delegation.expired', 'delegation.revoked') AND grant_id "
                       "IS NOT NULL AND ts > now() - make_interval(days => :d) ORDER BY ts")
    if not ended:
        return wp.result("history.grant_ended_live", "ready",
                         f"no grant ended in {_days(ctx)} day(s)")
    grants = [e["grant_id"] for e in ended]
    jobs = _rows(ctx, "SELECT id, kind, status, grant_id, created_at, updated_at, last_error FROM "
                      "jobs WHERE grant_id = ANY(:g)", {"g": grants})
    by_grant: dict[str, list[dict]] = defaultdict(list)
    for j in jobs:
        by_grant[j["grant_id"]].append(j)
    live_under = []
    for e in ended:
        halted = int(((e.get("details") or {}).get("runs_halted")) or 0)
        for j in by_grant.get(e["grant_id"], []):
            if j["created_at"] < e["ts"] - timedelta(seconds=5) and (
                    j["status"] in ("running", "leased", "queued")
                    or (j["updated_at"] > e["ts"] + timedelta(seconds=5)
                        and j["status"] not in ("succeeded", "cancelled"))):
                live_under.append((e, j))
        if halted:
            live_under.append((e, {"id": f"{halted} run(s) halted", "kind": "halted by the "
                                                                          "expiry", "status": "-"}))
    evidence = f"{len(ended)} grant(s) ended in {_days(ctx)} day(s)"
    if not live_under:
        return wp.result("history.grant_ended_live", "ready",
                         evidence + "; none while a job under it was still live")
    return wp.result("history.grant_ended_live", "unknown", evidence + f"; {len(live_under)} "
                     "ended while child work was live: " + ", ".join(
                         f"{e['grant_id']} at {_when(e['ts'])} → {j['id']} ({j.get('kind')}, "
                         f"{j.get('status')})" for e, j in live_under[:4]),
                     action="BOSS change: end a run's grant only when no child work is live.",
                     details={"grants": sorted({e["grant_id"] for e, _ in live_under})})


@_run_check("history.idle_in_transaction", "No BOSS connection sits idle inside a transaction")
def check_idle_tx(ctx: wp.Context) -> dict[str, Any]:
    def sample() -> dict[str, Any]:
        c = ctx.connection
        timeout = str(c.execute(text("SHOW idle_in_transaction_session_timeout")).scalar())
        seen: dict[int, dict] = {}
        for i in range(3):
            c.execute(text("SELECT pg_stat_clear_snapshot()"))
            for r in c.execute(text(
                    "SELECT pid, application_name, EXTRACT(EPOCH FROM now() - xact_start) AS age, "
                    "left(query, 100) AS query FROM pg_stat_activity WHERE datname = "
                    "current_database() AND state LIKE 'idle in transaction%' AND pid <> "
                    "pg_backend_pid()")).mappings().all():
                seen[int(r["pid"])] = dict(r)
            if i < 2:
                time.sleep(0.4)
        return {"timeout": timeout, "seen": list(seen.values())}
    got = ctx.savepoint(sample, default=None)
    if got is None:
        return wp.result("history.idle_in_transaction", "unknown",
                         "pg_stat_activity could not be read")
    old = [s for s in got["seen"] if float(s.get("age") or 0) > 5]
    unbounded = got["timeout"] in ("0", "0ms")
    evidence = (f"idle_in_transaction_session_timeout = {got['timeout']}"
                + (" (unbounded)" if unbounded else "")
                + f"; {len(got['seen'])} connection(s) idle in a transaction across 3 samples"
                + (": " + "; ".join(f"pid {s['pid']} {int(float(s['age'] or 0))}s "
                                    f"({_clip(s['query'], 60)})" for s in old[:3]) if old else ""))
    if old:
        return wp.result("history.idle_in_transaction", "unknown", evidence + " — a long "
                         "transaction holds row locks the run's steps then wait on",
                         action="BOSS change: commit per item in the tick, not once per sweep.")
    return wp.result("history.idle_in_transaction", "unknown" if unbounded else "ready",
                     evidence + (" — nothing caught now, and nothing would end one that "
                                 "stalled" if unbounded else ""))


# ── logs.* ────────────────────────────────────────────────────────────────────────────────────

_NAMED_IN_ERROR = re.compile(
    r"\b([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+|[A-Z]{4,})\b[^\n\"]{0,40}?\b(?:must be set|is required|"
    r"are required|is not set|must be provided|must be defined|is missing|not set)", re.S)
_MISSING_NAME = re.compile(r"(?:missing|required)\s+(?:env(?:ironment)?\s+(?:var(?:iable)?)?\s*)?"
                           r"[:=]?\s*([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)\b")


def named_by_app(log: str) -> Optional[str]:
    """The variable the app's OWN error names ("DEMO_PASSWORD must be set"), else None."""
    m = _NAMED_IN_ERROR.search(log or "") or _MISSING_NAME.search(log or "")
    return m.group(1) if m else None


def _app_log_part(error: str) -> str:
    """What the APP printed: the part of BOSS's app-serve error after "exited (code N) before
    serving HTTP:" — BOSS's own explanation in front of it is not the app's log."""
    m = re.search(r"exited \(code -?\d+\) before serving HTTP:(.*)", error or "", re.S)
    return m.group(1) if m else (error or "")


def blame(log: str, environment: Mapping[str, Any]) -> Optional[str]:
    """What BOSS's crash explanation blames on this log: run_env.RunEnvironment.unset_named_in,
    the matcher the build itself runs, over the environment the build recorded."""
    from ..templates.run_env import RunEnvironment

    env = RunEnvironment(purpose="proof", unset=list(environment.get("unset") or []),
                         generated=list(environment.get("generated") or []))
    return env.unset_named_in(log)


def _start_logs(ctx: wp.Context) -> list[dict[str, Any]]:
    """The app's start records from its previous builds: every code_task.app_serve_checked
    event, newest last, with the environment the build recorded (or, for an older record that
    has none, the environment the running BOSS would give it now)."""
    sc = scope(ctx)
    out = []
    for e in _events(ctx, ["code_task.app_serve_checked"], entity_ids=sc["build_ids"]):
        data = e.get("data") or {}
        if data.get("ok") is not False or not data.get("error"):
            continue
        out.append({"run": e["entity_id"], "at": e["created_at"], "error": str(data["error"]),
                    "environment": data.get("environment") or None,
                    "needs_secret": data.get("needs_secret") or []})
    # EVERY OTHER START (services.app_start): a multi-service backend in verify or the template
    # gate wrote no app_serve_checked (crun_f246d330), only its code_task.app_start record. The
    # serve proof writes both, so its app_start copy is skipped here.
    for e in _events(ctx, ["code_task.app_start"], entity_ids=sc["build_ids"]):
        data = e.get("data") or {}
        if data.get("ok") is not False or data.get("path") == "serve_proof":
            continue
        env = data.get("env") or {}
        code = data.get("exit_code")
        said = "\n".join(str(x) for x in (data.get("output_tail") or [])[-40:])
        out.append({"run": e["entity_id"], "at": e["created_at"],
                    "error": (f"the app exited (code {code}) before serving HTTP: {said}"
                              if code is not None else str(data.get("error") or said)),
                    "environment": {
                        "unset": [k for k, v in env.items() if str(v).startswith("unset")],
                        "generated": [k for k, v in env.items() if str(v).startswith("generated")]},
                    "needs_secret": [k for k, v in env.items()
                                     if str(v).startswith("needs a secret")]})
    return sorted(out, key=lambda item: str(item.get("at") or ""))


def _current_environment(ctx: wp.Context) -> dict[str, Any]:
    from ..templates import run_env

    def plan() -> dict[str, Any]:
        declared = dict(wp._declared_needs(ctx))  # noqa: SLF001
        p = run_env.resolve(declared, purpose="proof", bound={})
        return {"unset": list(p.unset), "generated": list(p.generated)}
    return ctx.once("current_env", plan)


@_run_check("logs.crash_blame", "BOSS's crash explanation names the variable the app named")
def check_crash_blame(ctx: wp.Context) -> list[dict[str, Any]]:
    logs = _start_logs(ctx)
    if not logs:
        return [wp.result("logs.crash_blame", "ready" if scope(ctx)["build_ids"] else "unknown",
                          "no failed app start recorded for this app's builds"
                          + ("" if scope(ctx)["build_ids"] else " (no builds found)"))]
    wrong, refused_generated, undeclared, rows = [], [], [], []
    for item in logs:
        app_log = _app_log_part(item["error"])
        env = item["environment"] or _current_environment(ctx)
        named = named_by_app(app_log)
        blamed = blame(app_log, env)
        said = (re.search(r"the app needs a secret: ([A-Z][A-Z0-9_]+)", item["error"]) or [None, None])[1]
        said = said or (item["needs_secret"][0] if item["needs_secret"] else None)
        source = "recorded" if item["environment"] else "the running BOSS's"
        rows.append(f"{item['run']} ({_when(item['at'])}): app names {named or '—'}, matcher "
                    f"blames {blamed or '—'}" + (f", BOSS said {said}" if said else ""))
        reads = code_reads_at_throw(ctx, app_log) if named else None
        if named and reads and named not in reads:
            wrong.append((item, named, ", ".join(sorted(reads)), "the code at the throw site "
                          "reads"))
        elif named and blamed and blamed != named:
            wrong.append((item, named, blamed, source))
        elif named and said and said != named:
            # Only what BOSS SAID at the time disagrees; the running matcher reads it right.
            # From before this version, that is history, not this version's verdict.
            wrong.append((item, named, said, "BOSS's explanation at the time"))
        elif named and named in (env.get("generated") or []):
            refused_generated.append((item, named))
        elif named and named not in (env.get("unset") or []) and not blamed:
            undeclared.append((item, named))
    out = []
    evidence = f"{len(logs)} failed start(s) read: " + "; ".join(rows[-5:])
    if wrong:
        item, named, blamed, source = wrong[-1]
        historical = (source == "BOSS's explanation at the time"
                      and _before_this_version(ctx, item["at"]))
        out.append(wp.result(
            "logs.crash_blame", "unknown" if historical else "will_fail",
            evidence + (f". {BEFORE_THIS_VERSION} ({_version_line(ctx)})" if historical else "")
            + f". WRONG BLAME on {item['run']}: the app "
            f"says \"{_clip(named_by_app_context(item['error'], named), 90)}\" and the "
            f"explanation names {blamed} ({source}): the person is sent to set the wrong secret",
            action="BOSS bug: explain_crash must prefer the name in the app's error line over "
                   "the first unset name the log mentions.",
            details={"run": item["run"], "named": named, "blamed": blamed}))
    if refused_generated:
        item, named = refused_generated[-1]
        out.append(wp.result(
            "logs.crash_blame", "unknown", evidence + f". {item['run']} ({_when(item['at'])}): the "
            "app refused "
            f"{named}, which BOSS GENERATES for the run (\"{_clip(named_by_app_context(item['error'], named), 90)}\") — "
            "the generated value did not reach the app, or does not meet its rule",
            action=f"Check that the start command passes {named} through (a `cd` subshell, an "
                   ".env loader that overrides it) and that its length/shape rule is met.",
            details={"run": item["run"], "named": named}))
    if undeclared:
        item, named = undeclared[-1]
        out.append(wp.result(
            "logs.crash_blame", "fixable", evidence + f". {item['run']}: the app refused {named}, "
            "which "
            "nothing BOSS reads declares — BOSS cannot give it or explain it",
            action=f"Declare {named} in the app's .env.example (and set it in Secrets)."))
    return out or [wp.result("logs.crash_blame", "ready", evidence + " — every explanation "
                             "matches what the app named")]


_STACK_FRAME = re.compile(r"([\w@./-]+\.(?:ts|tsx|js|mjs|cjs|py)):(\d+)")
_READS_ENV = re.compile(r"(?:process\.env\.|process\.env\[[\"']|env\.|get\(\s*[\"']|"
                        r"getenv\(\s*[\"']|environ(?:\.get)?\(\s*[\"'])([A-Z][A-Z0-9_]{2,})")


def code_reads_at_throw(ctx: wp.Context, log: str) -> Optional[set[str]]:
    """The variables the app's code READS around the line its stack trace names, when the
    pre-check has the app's tree: the crash names a variable; the code says which it checks.
    A frame's path is a sandbox path; its tail is matched against the tree."""
    if ctx.app_dir is None:
        return None
    for path, line in _STACK_FRAME.findall(log or ""):
        if "node_modules" in path or path.startswith("node:"):
            continue
        parts = [p for p in path.split("/") if p]
        for i in range(len(parts)):
            candidate = ctx.app_dir.joinpath(*parts[i:])
            if not candidate.is_file():
                continue
            try:
                lines = candidate.read_text(errors="ignore").splitlines()
            except OSError:
                return None
            n = int(line)
            found = set(_READS_ENV.findall("\n".join(lines[max(0, n - 10):n + 1])))
            return found or None
    return None


def named_by_app_context(log: str, name: str) -> str:
    m = re.search(rf"[^\n\"{{}}]{{0,20}}\b{re.escape(name)}\b[^\n\"{{}}]{{0,70}}", log or "")
    return m.group(0).strip() if m else name


@_run_check("logs.app_start", "The app's last start failure is no longer there")
def check_app_start(ctx: wp.Context) -> dict[str, Any]:
    logs = _start_logs(ctx)
    if not logs:
        return wp.result("logs.app_start", "ready" if scope(ctx)["build_ids"] else "unknown",
                         "no failed app start among this app's builds")
    last = logs[-1]
    app_log = _app_log_part(last["error"])
    named = named_by_app(app_log)
    key = wp._app_key(ctx)  # noqa: SLF001
    bound = bool(named and key and wp._has_binding(ctx, named, "build"))  # noqa: SLF001
    env = last["environment"] or _current_environment(ctx)
    generated = named in (env.get("generated") or [])
    later_ok = [e for e in _events(ctx, ["code_task.app_serve_checked"],
                                   entity_ids=scope(ctx)["build_ids"])
                if (e.get("data") or {}).get("ok") and e["created_at"] > last["at"]]
    evidence = (f"last failed start: {last['run']} at {_when(last['at'])}: "
                f"{_clip(app_log, 200)}")
    if later_ok:
        return wp.result("logs.app_start", "ready", evidence + f"; started fine since "
                         f"({later_ok[-1]['entity_id']})")
    if _before_this_version(ctx, last["at"]):
        # `last` is the LATEST failed start: nothing failed this way on the running version.
        return wp.result("logs.app_start", "unknown", evidence + f" — {BEFORE_THIS_VERSION} "
                         f"({_version_line(ctx)})" + (
                             f"; {named} still has no build value under '{key or '?'}'"
                             if named and not bound and not generated else ""),
                         action=(f"If the app still needs {named}, set it for env build in "
                                 "the app's Secrets." if named and not bound and not generated
                                 else ""),
                         details={"named": named, "app_key": key, "before_version": True})
    if named and not bound and not generated:
        return wp.result("logs.app_start", "will_fail", evidence + f" — {named} still has no "
                         f"build value under '{key or '?'}', so the app-start proof fails the "
                         "same way", action=f"Set {named} for env build in the app's Secrets "
                         "(or make the app start without it).",
                         details={"named": named, "app_key": key})
    return wp.result("logs.app_start", "unknown", evidence + (
        f" — {named} is now bound/generated; the start is expected to get past it"
        if named else " — no variable named; the cause is in the log"))


_SWALLOWED = re.compile(r"\b(KeyError|AttributeError|TypeError|NameError|IndexError|"
                        r"UnboundLocalError|ProgrammingError)\b")
_FRAME = re.compile(r'File "/app/(?:engine/|apps/)?([\w/.-]+\.py)", line (\d+), in (\w+)')
_EXC_LINE = re.compile(r"(?m)^(\w*(?:Error|Exception)): (.{0,160})$")


@_run_check("logs.swallowed_errors", "No programming error is being caught and logged past")
def check_swallowed(ctx: wp.Context) -> dict[str, Any]:
    rows = _rows(ctx, "SELECT service, logger, message, created_at FROM app_logs WHERE level IN "
                      "('warning', 'error', 'WARNING', 'ERROR') AND created_at > now() - "
                      "make_interval(days => :d) AND (project_id IS NULL OR project_id = :p) AND "
                      "message ~ '(KeyError|AttributeError|TypeError|NameError|IndexError|"
                      "UnboundLocalError|ProgrammingError)' ORDER BY created_at DESC LIMIT 400")
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for r in rows:
        msg = str(r["message"] or "")
        exc = _SWALLOWED.search(msg)
        if not exc:
            continue
        frames = [f for f in _FRAME.findall(msg) if f[0].startswith("boss_")]
        where = (f"{frames[-1][0]} in {frames[-1][2]}" if frames else str(r["logger"]))
        said = _EXC_LINE.findall(msg)
        what = said[-1][1].strip() if said else ""
        _fp, norm = fingerprint(what)
        g = groups.setdefault((exc.group(1), where + ("|" + norm[:60] if norm else "")),
                              {"n": 0, "last": r["created_at"], "service": r["service"],
                               "what": what, "line": frames[-1][1] if frames else "",
                               "where": where})
        g["n"] += 1
    if not groups:
        return wp.result("logs.swallowed_errors", "ready",
                         f"no KeyError/AttributeError/TypeError/ProgrammingError caught in BOSS's "
                         f"log (app_logs) in {_days(ctx)} day(s)")
    top = sorted(groups.items(), key=lambda kv: -kv[1]["n"])
    return wp.result("logs.swallowed_errors", "unknown",
                     f"{sum(g['n'] for g in groups.values())} programming error(s) caught and "
                     "logged past in app_logs: " + "; ".join(
                         f"{exc} at {g['where']}:{g['line']} ×{g['n']}"
                         + (f" ({_clip(g['what'], 80)})" if g["what"] else "")
                         + f", last {_when(g['last'])}" for (exc, _w), g in top[:5]),
                     action="Each is a BOSS bug a broad except hides: what that code was doing "
                            "(a dashboard check, a budget read) silently did not happen.",
                     details={"groups": [{"exception": e, "where": g["where"], "count": g["n"],
                                          "message": _clip(g["what"], 160)}
                                         for (e, _w), g in top[:12]]})


# ── data.* ────────────────────────────────────────────────────────────────────────────────────

def canonical_key(ctx: wp.Context) -> str:
    from .naming import identity_key

    return wp._app_key(ctx) or identity_key(wp._app_name(ctx))  # noqa: SLF001


def related_keys(canonical: str, keys: Iterable[str]) -> list[str]:
    """Other keys that are the same app under an older or newer name: one key's words are the
    leading words of the other ("helpdesk" ⊂ "helpdesk ai"), or they normalise the same."""
    from .naming import identity_key

    want = identity_key(canonical).split()
    out = []
    for k in keys:
        if not k or k == canonical:
            continue
        have = identity_key(k).split()
        if not have or not want:
            continue
        n = min(len(have), len(want))
        if have[:n] == want[:n]:
            out.append(k)
    return sorted(set(out))


@_run_check("data.split_app_keys", "The app's secrets are filed under the key the run uses")
def check_split_keys(ctx: wp.Context) -> dict[str, Any]:
    canonical = canonical_key(ctx)
    if not canonical:
        return wp.result("data.split_app_keys", "unknown", "no app key to compare (no template "
                         "app_key and no app name)")
    keys = {r["app_key"] for r in _rows(
        ctx, "SELECT DISTINCT app_key FROM app_secret_bindings WHERE project_id = :p UNION "
             "SELECT DISTINCT app_key FROM app_template_applications WHERE project_id = :p")}
    related = related_keys(canonical, keys)
    templates = defaultdict(set)
    for r in _rows(ctx, "SELECT app_key, template_id FROM app_template_applications WHERE "
                        "project_id = :p AND app_key = ANY(:k)", {"k": [canonical, *related]}):
        templates[r["app_key"]].add(r["template_id"])
    related = [k for k in related if not templates[canonical] or not templates[k]
               or templates[k] & templates[canonical]]
    if not related:
        return wp.result("data.split_app_keys", "ready", f"'{canonical}' is the only key this "
                         "app's secrets and templates are filed under")
    bindings = _rows(ctx, "SELECT app_key, env, name, mode FROM app_secret_bindings WHERE "
                          "project_id = :p AND app_key = ANY(:k)", {"k": [canonical, *related]})
    have = {(b["env"], b["name"]) for b in bindings if b["app_key"] == canonical}
    stranded = [b for b in bindings if b["app_key"] != canonical
                and (b["env"], b["name"]) not in have]
    movable = [b for b in stranded if b["mode"] in ("test", "stub")]
    person = [b for b in stranded if b["mode"] == "live"]
    splits = _rows(ctx, "SELECT split_part(title, ':', 1) AS name, finalize_ctx->>'app_id' AS "
                        "app_id, count(*) AS n FROM code_task_runs WHERE project_id = :p AND "
                        "finalize_ctx ? 'app_id' GROUP BY 1, 2")
    from .naming import identity_key

    ids = [f"{s['name']} → {s['app_id']} ({s['n']} builds)" for s in splits
           if identity_key(s["name"]) in {identity_key(k) for k in [canonical, *related]}]
    evidence = (f"the run files this app's secrets under '{canonical}'; the same app also has "
                f"key(s) {', '.join(repr(k) for k in related)}"
                + (f" (app identities: {'; '.join(ids)})" if len(ids) > 1 else "")
                + (f"; {len(stranded)} binding(s) only under the old key: " + ", ".join(
                    f"{b['name']} [{b['env']}, {b['mode']}] in '{b['app_key']}'"
                    for b in stranded[:8]) if stranded else "; nothing stranded"))
    if not stranded:
        return wp.result("data.split_app_keys", "unknown", evidence + " — consider merging the "
                         "identities (find_split_apps / merge_apps)")
    if movable:
        return wp.result("data.split_app_keys", "fixable", evidence + " — the run will not "
                         "see them", action="fixable: BOSS can do this — copy the bindings to "
                         f"'{canonical}' (never deletes the old ones)"
                         + (f"; {', '.join(b['name'] for b in person)} is live: a person sets "
                            "it" if person else ""), auto=True,
                         details={"autofix": {"fix": "secrets.refile", "args": {
                             "canonical": canonical, "items": [
                                 {"from": b["app_key"], "env": b["env"], "name": b["name"]}
                                 for b in movable]}}})
    return wp.result("data.split_app_keys", "fixable", evidence,
                     action=f"Set {', '.join(b['name'] for b in person)} under '{canonical}' "
                            "(live keys are a person's choice).")


@_run_check("data.generated_secrets", "The throwaway keys BOSS generates exist for deploy")
def check_generated(ctx: wp.Context) -> Optional[dict[str, Any]]:
    from ..templates.secrets import declared_specs

    key = canonical_key(ctx)
    if not key:
        return None
    specs = ctx.savepoint(lambda: declared_specs(ctx.connection, project_id=ctx.project_id,
                                                 app_key=key), default={}) or {}
    gen = [n for n, d in specs.items() if d.get("test_default") == "generated"]
    if not gen:
        return wp.result("data.generated_secrets", "ready", f"'{key}' declares no secret BOSS "
                         "generates")
    missing = [n for n in gen if not wp._has_binding(ctx, n, "deploy")]  # noqa: SLF001
    if not missing:
        return wp.result("data.generated_secrets", "ready",
                         f"{', '.join(gen)} generated for deploy under '{key}'")
    return wp.result("data.generated_secrets", "fixable", f"{', '.join(missing)} (generated by "
                     f"BOSS) has no deploy value under '{key}' yet; the deploy makes it itself, "
                     "or BOSS can make it now", action="fixable: BOSS can do this — generate "
                     "the throwaway key(s) now (secrets.ensure_deploy_generated)", auto=True,
                     details={"autofix": {"fix": "secrets.generate", "args": {"app_key": key}}})


@_run_check("data.stale_targets", "No target that never deployed holds a port")
def check_stale_targets(ctx: wp.Context) -> dict[str, Any]:
    targets = _rows(ctx, "SELECT t.id, t.app_name, t.environment, t.status, t.domains, "
                         "t.created_at, (SELECT count(*) FROM deployment_runs r WHERE "
                         "r.deployment_target_id = t.id AND r.status IN ('succeeded', 'healthy', "
                         "'superseded')) AS ok_runs, (SELECT count(*) FROM deployment_runs r "
                         "WHERE r.deployment_target_id = t.id AND r.status IN ('queued', "
                         "'running', 'pending', 'applying')) AS live_runs FROM "
                         "deployment_targets t WHERE t.project_id = :p AND t.status <> 'retired'")
    stale = []
    now = datetime.now(timezone.utc)
    for t in targets:
        ports = [d for d in (t.get("domains") or []) if str(d).rpartition(":")[2].isdigit()]
        if ports and not int(t["ok_runs"] or 0) and not int(t["live_runs"] or 0) \
                and t["created_at"] < now - STALE_TARGET_AGE:
            stale.append({**t, "ports": ports})
    if not stale:
        return wp.result("data.stale_targets", "ready", f"{len(targets)} live target(s); none "
                         "holds a port without ever having deployed")
    return wp.result("data.stale_targets", "fixable", f"{len(stale)} target(s) never deployed "
                     "successfully yet hold a port: " + ", ".join(
                         f"{t['id']} ({t['app_name']}, {', '.join(t['ports'])}, since "
                         f"{_when(t['created_at'])})" for t in stale[:5]),
                     action="fixable: BOSS can do this — mark them as not reserving their port "
                            "(status retired; nothing deleted, recorded with the prior status)",
                     auto=True, details={"autofix": {"fix": "targets.release_port", "args": {
                         "ids": [t["id"] for t in stale]}}})


@_run_check("data.leaked_leases", "No lease or reservation is live past its expiry")
def check_leaked(ctx: wp.Context) -> dict[str, Any]:
    held = _rows(ctx, "SELECT id, amount_usd_micros FROM budget_reservations WHERE project_id = "
                      ":p AND status = 'active' AND expires_at <= now()")
    # BudgetGovernor.expire_stale_reservations expires only zero-amount holds: expiry is not
    # proof the provider never took the work, so a hold with money on it waits for a person to
    # reconcile it. Offering the auto-fix for those reported "applied" and changed nothing.
    budget = [r for r in held if not int(r.get("amount_usd_micros") or 0)]
    money = [r for r in held if int(r.get("amount_usd_micros") or 0)]
    gateway = _rows(ctx, "SELECT id FROM model_gateway_leases WHERE project_id = :p AND status = "
                         "'active' AND expires_at <= now()")
    branch = _rows(ctx, "SELECT id, ref_name FROM github_branch_leases WHERE project_id = :p AND "
                        "status = 'active' AND lease_expires_at <= now()")
    total = len(budget) + len(gateway) + len(branch)
    evidence = (f"past expiry and still active: {len(budget)} budget reservation(s), "
                f"{len(gateway)} model-gateway lease(s), {len(branch)} branch lease(s)"
                + (" (" + ", ".join(x["id"] for x in (budget + gateway + branch)[:5]) + ")"
                   if total else ""))
    reconcile = (f"{len(money)} expired budget reservation(s) still hold money ("
                 + ", ".join(f"{r['id']} {wp._money(r['amount_usd_micros'])}"
                             for r in money[:5])
                 + "); expiry is not proof the provider never took the work, so BOSS will not "
                 "release them" if money else "")
    reconcile_action = ("a person reconciles each in Decisions (\"Reconcile expired budget "
                        "reservation …\"): settle it if the provider ran the work, release it "
                        "if not")
    if not total and not money:
        return wp.result("data.leaked_leases", "ready", evidence)
    if not total:
        return wp.result("data.leaked_leases", "fixable", reconcile + " — they still count "
                         "against the budget a new run reserves from", action=reconcile_action)
    return wp.result("data.leaked_leases", "fixable", evidence + " — they still count against "
                     "the budget/capacity a new run reserves from"
                     + (f". Also: {reconcile}" if money else ""),
                     action="fixable: BOSS can do this — release them through each owner's own "
                            "expiry path" + (f"; then {reconcile_action}" if money else ""),
                     auto=True,
                     details={"autofix": {"fix": "leases.release_expired", "args": {
                         "budget": len(budget), "gateway": len(gateway),
                         "branch": [b["id"] for b in branch]}}})


@_run_check("data.dead_run_decisions", "No pending decision belongs to a run that is over")
def check_dead_decisions(ctx: wp.Context) -> dict[str, Any]:
    rows = _rows(ctx, "SELECT d.id, d.question, d.created_at, COALESCE(w.status, c.status) AS "
                      "owner_status, COALESCE(s.workflow_run_id, c.id) AS owner FROM decisions d "
                      "LEFT JOIN workflow_step_runs s ON s.decision_id = d.id LEFT JOIN "
                      "workflow_runs w ON w.id = s.workflow_run_id LEFT JOIN code_task_runs c ON "
                      "c.decision_id = d.id WHERE d.project_id = :p AND d.status = 'pending' AND "
                      "d.created_at < now() - make_interval(days => :sd)",
                 {"sd": STALE_DECISION_DAYS})
    dead = [r for r in rows if r["owner_status"] in ("failed", "cancelled", "completed",
                                                     "succeeded", "blocked", "stopped")]
    if not dead:
        return wp.result("data.dead_run_decisions", "ready",
                         f"{len(rows)} pending decision(s) older than {STALE_DECISION_DAYS} "
                         "day(s); none belongs to a finished run")
    return wp.result("data.dead_run_decisions", "fixable", f"{len(dead)} pending decision(s) "
                     "belong to runs that are over: " + "; ".join(
                         f"{r['id']} ({r['owner']} {r['owner_status']}, "
                         f"{_when(r['created_at'])}): {_clip(r['question'], 60)}"
                         for r in dead[:4]),
                     action="Withdraw them in Decisions (a person's choice: BOSS does not "
                            "answer a question for you).")


# ── auto-fixes ────────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Fix:
    id: str
    label: str
    fn: Callable[..., dict[str, Any]]
    #: True: the fix only reports what it would do, even under --fix (a lockfile regenerated in a
    #: throwaway copy).
    dry_run_only: bool = False


FIXES: dict[str, Fix] = {}


def _fix(fix_id: str, label: str, *, dry_run_only: bool = False):
    def register(fn):
        FIXES[fix_id] = Fix(fix_id, label, fn, dry_run_only)
        return fn
    return register


def _record(c, project_id: str, fix_id: str, entity_type: str, entity_id: str,
            changed: Any) -> None:
    from .core import emit_event

    emit_event(c, project_id, "precheck.autofix_applied", ACTOR, entity_type, entity_id,
               {"fix": fix_id, "changed": changed})


def _org(c, project_id: str) -> Optional[str]:
    from ..multirepo.common import org_of_project

    return org_of_project(c, project_id)


@_fix("secrets.refile", "Copy secret bindings from a split app key to the canonical key")
def fix_refile(c, settings, project_id: str, args: Mapping[str, Any]) -> dict[str, Any]:
    from ..identity import vault
    from .core import make_id

    canonical = str(args["canonical"])
    org = _org(c, project_id)
    copied, skipped = [], []
    for item in args.get("items") or []:
        src, env, name = str(item["from"]), str(item["env"]), str(item["name"])
        if c.execute(text("SELECT 1 FROM app_secret_bindings WHERE project_id = :p AND app_key "
                          "= :a AND env = :e AND name = :n"),
                     {"p": project_id, "a": canonical, "e": env, "n": name}).first():
            skipped.append(f"{name}[{env}]: already under '{canonical}'")
            continue
        row = c.execute(text("SELECT mode, vault_item_id, shape FROM app_secret_bindings WHERE "
                             "project_id = :p AND app_key = :a AND env = :e AND name = :n"),
                        {"p": project_id, "a": src, "e": env, "n": name}).mappings().first()
        if row is None or row["mode"] not in ("test", "stub"):
            skipped.append(f"{name}[{env}]: {'gone' if row is None else row['mode']}")
            continue
        item_id = None
        if row["vault_item_id"]:
            value = vault.read_app_secret(row["vault_item_id"], org_id=org, project_id=project_id,
                                          app_key=src, env=env, name=name, db=c)
            item_id = vault.store_app_secret(org_id=org, project_id=project_id,
                                             app_key=canonical, env=env, name=name, value=value,
                                             db=c)
        c.execute(text("INSERT INTO app_secret_bindings (id, project_id, app_key, env, name, "
                       "vault_item_id, mode, set_by, shape) VALUES (:id, :p, :a, :e, :n, :v, :m, "
                       ":by, :s)"), {"id": make_id("asb"), "p": project_id, "a": canonical,
                                      "e": env, "n": name, "v": item_id, "m": row["mode"],
                                      "by": ACTOR, "s": row["shape"] or "unknown"})
        copied.append({"name": name, "env": env, "mode": row["mode"], "from": src})
    if copied:
        _record(c, project_id, "secrets.refile", "app", canonical,
                {"copied": copied, "deleted": []})
    return {"changed": copied, "skipped": skipped}


@_fix("secrets.generate", "Generate the throwaway keys BOSS makes for deploy")
def fix_generate(c, settings, project_id: str, args: Mapping[str, Any]) -> dict[str, Any]:
    from ..templates.secrets import ensure_deploy_generated

    key = str(args["app_key"])
    made = ensure_deploy_generated(c, org_id=str(_org(c, project_id)), project_id=project_id,
                                   app_key=key, actor_id=None)
    if made:
        _record(c, project_id, "secrets.generate", "app", key, {"generated": made})
    return {"changed": made}


@_fix("targets.release_port", "Mark a never-deployed target as not reserving its port")
def fix_release_port(c, settings, project_id: str, args: Mapping[str, Any]) -> dict[str, Any]:
    changed = []
    for tid in args.get("ids") or []:
        row = c.execute(text(
            "SELECT status, domains FROM deployment_targets WHERE id = :t AND project_id = :p "
            "AND status <> 'retired' AND NOT EXISTS (SELECT 1 FROM deployment_runs r WHERE "
            "r.deployment_target_id = :t AND r.status IN ('succeeded', 'healthy', 'superseded', "
            "'queued', 'running', 'pending', 'applying'))"),
            {"t": tid, "p": project_id}).mappings().first()
        if row is None:
            continue
        c.execute(text("UPDATE deployment_targets SET status = 'retired', updated_at = now() "
                       "WHERE id = :t"), {"t": tid})
        changed.append({"target": tid, "was": row["status"], "ports": row["domains"]})
        _record(c, project_id, "targets.release_port", "deployment_target", tid,
                {"status": {"from": row["status"], "to": "retired"},
                 "undo": f"set status back to {row['status']}"})
    return {"changed": changed}


@_fix("leases.release_expired", "Release leases and reservations past expiry")
def fix_release_leases(c, settings, project_id: str, args: Mapping[str, Any]) -> dict[str, Any]:
    from .budget import BudgetGovernor
    from .model_gateway import ModelGatewayService

    changed: dict[str, Any] = {}
    expired = BudgetGovernor(settings).expire_stale_reservations(c, project_id)
    if expired:
        changed["budget_reservations"] = list(expired)
    reaped = ModelGatewayService(settings).reap_expired(c, project_id, actor=ACTOR)
    if reaped.get("reaped"):
        changed["model_gateway_leases"] = reaped["reaped"]
    branch = []
    for lease_id in args.get("branch") or []:
        n = c.execute(text("UPDATE github_branch_leases SET status = 'reclaimed', released_by = "
                           ":a, released_at = now(), release_reason = 'expired; released by the "
                           "pre-check', updated_at = now() WHERE id = :i AND project_id = :p AND "
                           "status = 'active' AND lease_expires_at <= now()"),
                      {"a": ACTOR, "i": lease_id, "p": project_id}).rowcount
        if n:
            branch.append(lease_id)
    if branch:
        changed["github_branch_leases"] = branch
    if changed:
        _record(c, project_id, "leases.release_expired", "project", project_id, changed)
    return {"changed": changed}


@_fix("cache.playwright", "Download the app's Playwright browser into the shared cache")
def fix_playwright(c, settings, project_id: str, args: Mapping[str, Any]) -> dict[str, Any]:
    from . import playwright_cache

    version = str(args.get("version") or "")
    if playwright_cache.is_cached(version):
        return {"changed": []}
    got = playwright_cache.ensure(version)
    if got.get("ok"):
        _record(c, project_id, "cache.playwright", "project", project_id,
                {"playwright": version, "seconds": got.get("seconds")})
    return {"changed": [version] if got.get("ok") else [], "detail": got.get("detail")}


@_fix("deploy.folder", "Create the app's deploy folder on its host (connector preflight)")
def fix_deploy_folder(c, settings, project_id: str, args: Mapping[str, Any]) -> dict[str, Any]:
    from .deployment_connectors import preflight

    row = c.execute(text("SELECT * FROM deployment_targets WHERE id = :t AND project_id = :p"),
                    {"t": args["target_id"], "p": project_id}).mappings().first()
    if row is None:
        return {"changed": []}
    got = preflight(dict(row), settings, c)
    _record(c, project_id, "deploy.folder", "deployment_target", str(row["id"]),
            {"preflight": {k: got.get(k) for k in ("ok", "problems") if k in got}})
    return {"changed": [str(row["id"])], "result": got}


@_fix("lockfile.regenerate", "Regenerate the lockfile in a throwaway copy and report the diff",
      dry_run_only=True)
def fix_lockfile(c, settings, project_id: str, args: Mapping[str, Any]) -> dict[str, Any]:
    return {"changed": [], "report": args.get("report") or "the lockfile is regenerated only "
            "in the pre-check's own copy; nothing is written to the app"}


def autofix_candidates(report: Mapping[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """(step id, check) for every check whose fix BOSS can make itself."""
    out = []
    for step in report.get("steps") or []:
        for check in step.get("checks") or []:
            spec = (check.get("details") or {}).get("autofix")
            if spec and spec.get("fix") in FIXES and check.get("verdict") != "ready":
                out.append((step["step_id"], check))
    return out


def apply_fixes(engine: Any, settings: Any, project_id: str, report: Mapping[str, Any], *,
                apply: bool) -> list[dict[str, Any]]:
    """Each auto-fix the report names: planned (apply=False) or applied, each in its own
    transaction so one failure leaves the others standing. Idempotent: a fix re-reads the
    state it changes and does nothing when it is already right."""
    out = []
    for step_id, check in autofix_candidates(report):
        spec = check["details"]["autofix"]
        fix = FIXES[spec["fix"]]
        entry = {"check": f"{step_id}:{check['id']}", "fix": fix.id, "label": fix.label,
                 "applied": False, "dry_run": not apply or fix.dry_run_only}
        if not apply or fix.dry_run_only:
            entry["planned"] = spec.get("args")
            if fix.dry_run_only and apply:
                entry["result"] = fix.fn(None, settings, project_id, spec.get("args") or {})
            out.append(entry)
            continue
        try:
            with engine.begin() as c:
                entry["result"] = fix.fn(c, settings, project_id, spec.get("args") or {})
            entry["applied"] = True
        except Exception as exc:  # noqa: BLE001 — a fix that cannot run says why; nothing half-applied
            entry["error"] = f"{type(exc).__name__}: {exc}"[:300]
        out.append(entry)
    return out


# ── explain ───────────────────────────────────────────────────────────────────────────────────

def explain(report: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Per will_fail (then fixable), the SHORTEST path to fix it: an auto-fix BOSS makes, a
    person's action, or a BOSS bug named by its known-issue fingerprint."""
    out = []
    for verdict in ("will_fail", "fixable"):
        for step in report.get("steps") or []:
            for check in step.get("checks") or []:
                if check.get("verdict") != verdict:
                    continue
                action = str((check.get("fix") or {}).get("action") or "")
                autofix = (check.get("details") or {}).get("autofix")
                if autofix and autofix.get("fix") in FIXES:
                    kind, path = "auto-fix", (f"BOSS can do this: {FIXES[autofix['fix']].label} "
                                              "(re-run with --fix)")
                elif re.match(r"\s*BOSS (?:change|bug)", action) or not action:
                    base = check["id"].split(".", 1)[0] + "." + check["id"].split(".", 1)[-1]
                    issue = KNOWN_ISSUES.get(check["id"]) or KNOWN_ISSUES.get(base) or ""
                    kind = "BOSS bug"
                    path = (f"known-issue {known_issue_fingerprint(check)}"
                            + (f" — {issue}" if issue else "")
                            + (f". Meanwhile: {action}" if action else ""))
                else:
                    kind, path = "person", action
                out.append({"step": step["step_id"], "check": check["id"], "verdict": verdict,
                            "kind": kind, "path": path})
    return out


__all__ = ["ACTOR", "FAMILIES", "FIXES", "KNOWN_ISSUES", "RUN_CHECKS", "apply_fixes",
           "autofix_candidates", "blame", "canonical_key", "explain", "fingerprint",
           "known_issue_fingerprint", "named_by_app", "related_keys", "run_families", "scope"]
