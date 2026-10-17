"""The Factory's one view of a build (factory spec §4.4, §6.1-§6.3).

THE CONTRACT, frozen in Phase 0. apps/web/lib/factory-floor.ts and apps/web/lib/cockpit.ts
parse exactly these shapes; a field changed here is changed under the Floor, the cockpit, the
MCP `get_code_task` and Agents › Live at once. Change it with the lead, never inside a stream.

Pure derivations live here so every reader says the same thing about a build: one state
(`build_state`), one line of stations (`stations_for`), one label per quality gate
(`gate_label`) and one failure sentence (`failure_line`). The web maps none of these.

NEVER A GUESS: no percent, no ETA, no "almost". `k of n` counts stations done; a face is an
act's role, or a hollow face only when the role is certain, or the BOSS glyph for machinery.
"""

from __future__ import annotations

from typing import Any, Literal, Optional, TypedDict

# ── the stations (§4.3) ─────────────────────────────────────────────────────────────────────

StationKey = Literal["plan", "you_plan", "tests", "build", "check", "review", "pr", "preview",
                     "ship"]
#: In order. `you_plan` appears only when the plan needs approval.
STATION_ORDER: tuple[str, ...] = ("plan", "you_plan", "tests", "build", "check", "review", "pr",
                                  "preview", "ship")
STATION_LABELS: dict[str, str] = {
    "plan": "Plan", "you_plan": "You", "tests": "Tests", "build": "Build", "check": "Check",
    "review": "Review", "pr": "PR", "preview": "Preview", "ship": "Ship",
}
#: Build stage keys (agent_acts.STAGE_PHASES + the governed phases) → their station.
STAGE_STATION: dict[str, str] = {
    "standards": "plan", "spec": "plan", "plan": "plan", "review_plan": "plan", "design": "plan",
    "provisioning": "plan", "queued": "plan",
    "awaiting_plan_approval": "you_plan",
    "write_tests": "tests", "fixture_repair": "tests",
    "implement": "build", "solo": "build", "guest": "build", "repair": "build", "coding": "build",
    "run_tests": "check", "check_tests": "check", "run_all_tests": "check", "iac_verify": "check",
    "serve_check": "check", "validating": "check", "test_arbitration": "check",
    "review_code": "review", "security_check": "review", "consistency_check": "review",
    "regression": "review", "review_panel": "review", "convergence": "review",
    "awaiting_gate_override": "review",
    "opening_pr": "pr",
}
#: HONEST ENDINGS (operator, 2026-10-13). `failed`: the station's work errored (its tip says why).
#: `stopped`: its work was left open because the build stopped. `stuck`: ONLY a live build's
#: station with no activity for QUIET_AFTER_MINUTES. A pending station after a stopped build is
#: `not_reached` and says so, never "Next".
StationStatus = Literal["pending", "working", "needs_you", "done", "stuck", "skipped", "waiting",
                        "failed", "stopped"]
#: Who stands at a station. `agent`: role_key's face in `status`. `hollow`: hollow_role's face,
#: pending (the role is certain). `boss`: the BOSS glyph (machinery). `you`: the You glyph.
#: `none`: an empty slot (pending, nobody certain).
StationFace = Literal["agent", "hollow", "boss", "you", "none"]


class Seat(TypedDict, total=False):
    act_id: str
    role_key: Optional[str]
    seat: int                      # 1-based
    status: StationStatus
    verdict: Optional[str]         # approved | changes_asked | blocked | None


class StationLink(TypedDict, total=False):
    label: str                     # "#842 ↗" | "Open ↗" | "dev ✓"
    url: Optional[str]


class Station(TypedDict, total=False):
    key: str                       # StationKey
    label: str                     # STATION_LABELS
    status: str                    # StationStatus
    face: str                      # StationFace
    role_key: Optional[str]        # the act's role (face == "agent")
    hollow_role: Optional[str]     # face == "hollow" only
    act_id: Optional[str]          # the latest act at this station
    act_ids: list[str]             # every act at this station, oldest first
    seats: list[Seat]              # review only: one per seat act
    word: str                      # "Done" | "Coding" | "Next" | "Not reached" | "Failed" |
    #                                "Stopped" | "Stuck" | "Skipped" | "Waiting" | "Needs you"
    not_reached: bool              # after the station a stopped build ended at
    tip: Optional[str]             # e.g. "Lean profile skips Tests", "BOSS machinery. No agent at this station."
    started_at: Optional[str]
    ended_at: Optional[str]
    edge_label: Optional[str]      # on the edge INTO this station: "Next stage", "Assigned by Helm"
    rounds: int                    # repair rounds through this station (0 = none)
    loop_with: Optional[str]       # the other role in a repair loop (`Forge ↔ Rook ×2`)
    links: list[StationLink]       # PR / preview / per-env deploy marks
    stopped: bool                  # skipped because the build was stopped (still planned)
    kept: bool                     # carried from an earlier attempt's workspace, not re-run
    kept_from: Optional[int]       # that attempt's number (`Kept from #4`)


# ── one state per build (§4.4) ──────────────────────────────────────────────────────────────

BuildStateKey = Literal["cancelled", "failed", "needs_plan", "needs_waiver", "budget_refused",
                        "blocked", "budget_wait", "quiet", "queued", "building", "pr_open",
                        "merged", "deployed", "done_no_pr"]
#: Which Floor section each state is drawn in (needs-you states also have a Building row).
SECTION_OF: dict[str, str] = {
    "cancelled": "stopped", "failed": "stopped",
    "needs_plan": "building", "needs_waiver": "building", "budget_refused": "building",
    "blocked": "building", "budget_wait": "building", "quiet": "building", "queued": "building",
    "building": "building",
    "pr_open": "shipped", "merged": "shipped", "deployed": "shipped", "done_no_pr": "shipped",
}
QUIET_AFTER_MINUTES = 20
#: A build whose attempt went no further: failed, stopped by a person, blocked, or refused its
#: budget. Its open work is Stopped and the stations after it are Not reached (a retry is a new
#: attempt with its own line). Plan approval and a checks waiver wait on the operator instead.
STOPPED_STATES = frozenset({"failed", "cancelled", "blocked", "budget_refused"})

#: The one state that means a worker is running the build right now.
RUNNING_STATES = frozenset({"building"})


def running_build_count(rows: list[Mapping[str, Any]]) -> int:
    """How many of these build rows are really running, by their one state (§4.4)."""
    return sum(1 for r in rows if str(r.get("state") or "") in RUNNING_STATES)


class BuildState(TypedDict, total=False):
    state: str                     # BuildStateKey
    word: str                      # the one word every surface shows ("Coding", "Checks failed")
    tip: Optional[str]             # its tooltip (≤ 14 words)
    verb: str                      # agent_acts.work_verb of the open act, else the state's verb
    station: Optional[str]         # StationKey the build is at
    k: int                         # stations done
    n: int                         # stations planned for this build's profile
    rounds: int                    # repair rounds so far
    preview: Optional[str]         # starting | running | failed | None  (a flag, never a state)


class Origin(TypedDict, total=False):
    kind: str                      # workflow_run | meeting | discovery
    id: str
    label: str                     # "Ship it #182" | "PRD" | "research"


class PreviewFlag(TypedDict, total=False):
    status: str                    # starting | running | failed
    url: Optional[str]


# ── GET /v1/factory/floor (§6.2) ────────────────────────────────────────────────────────────

_BUILDING_ORDER = {"needs_plan": 0, "needs_waiver": 0, "budget_refused": 0, "quiet": 1,
                   "blocked": 1, "budget_wait": 2, "building": 2, "queued": 3}


def _sort_building(rows: list[BuildRow], now: datetime) -> list[BuildRow]:
    """Waiting on you → quiet → building (newest station first) → queued (§4.2.2)."""
    return sorted((b for b in rows if section_of(b["state"]) == "building"),
                  key=lambda b: (_BUILDING_ORDER.get(b["state"], 2),
                                 -(_aware(b.get("station_since")) or now).timestamp()))


def building_rows(connection: Any, project_id: str, attention: list[Mapping[str, Any]], *,
                  viewer: Optional[str] = None, now: Optional[datetime] = None) -> list[BuildRow]:
    """The Floor's BUILDING rows alone — what Agents › Live shows of the builds, so the two
    say the same rows with the same counts (factory spec §10 decision 2)."""
    from .code_task_service import (list_code_task_runs, list_internal_code_task_runs,
                                    overlay_stop_requests)

    now = now or _now()
    runs = overlay_stop_requests(
        connection, list_code_task_runs(connection, project_id, FLOOR_RUNS, exclude_internal=True)
        # BOSS's own builds are builds (D14): read apart so they never crowd out the window.
        + list_internal_code_task_runs(connection, project_id, INTERNAL_RUNS))
    runs.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
    latest: dict[str, Mapping[str, Any]] = {}
    for r in runs:
        latest.setdefault(_lineage_key(r), r)
    live = [r for r in latest.values()
            if str(r.get("status") or "") in ("running", "repairing", "blocked")][:ACT_RUNS_CAP]
    facts = load_facts(connection, project_id, live)
    names = _workflow_names(connection, project_id, live)
    attention_of = {str((a.get("build") or {}).get("run_id")): str(a["id"]) for a in attention
                    if isinstance(a.get("build"), dict)}
    return _sort_building([build_row(r, facts, now=now, viewer=viewer,
                                     attention_id=attention_of.get(str(r["id"])),
                                     origin=_origin(None, project_id, r, names))
                           for r in live], now)


class BuildRow(TypedDict, total=False):
    run_id: str
    app: str
    title: str
    attempt: int
    root_run_id: str
    actor: Optional[str]
    state: str
    word: str
    tip: Optional[str]
    verb: str
    station: Optional[str]
    stations: list[Station]
    k: int
    n: int
    rounds: int
    started_at: Optional[str]
    station_since: Optional[str]
    updated_at: Optional[str]
    cost_usd: float                # metered only; 0 when unmetered (and then never shown)
    metered: bool
    attention_id: Optional[str]
    preview: Optional[PreviewFlag]
    origin: Optional[Origin]
    internal: bool                 # BOSS's own housekeeping build (D14)


class FloorOutcome(TypedDict, total=False):
    kind: str                      # pr_opened | pr_merged | preview | deployed | done_no_pr
    run_id: str
    app: str
    role_key: Optional[str]        # the builder; None for deploys (no act yet) → BOSS glyph
    text: str                      # "PR #842 merged"
    url: Optional[str]
    at: str


class StoppedRow(TypedDict, total=False):
    run_id: str
    app: str
    title: str
    state: str                     # failed | cancelled
    word: str                      # "Failed · Build" | "Stopped by you"
    tip: Optional[str]
    role_key: Optional[str]        # the stuck station's role
    station: Optional[str]
    retryable: bool
    retry_reason: Optional[str]
    at: str


class PreBuild(TypedDict, total=False):
    meeting_run_id: str
    title: str
    seats: list[str]               # role keys
    at: Optional[str]


class FloorCounts(TypedDict):
    building: int                  # builds a worker is running now (state `building`)
    waiting: int                   # the rest of the BUILDING section: blocked, queued, quiet…
    needs_you: int                 # decisions listed (command_center.attention_counts)
    shipped_7d: int
    stopped: int                   # stopped work listed in NEEDS YOU (same rule)
    stopped_builds: int            # the STOPPED section's rows
    apps: int


class Floor(TypedDict, total=False):
    project_id: str
    etag: str
    generated_at: str
    counts: FloorCounts
    attention: list[dict[str, Any]]   # agents_live_board.AttentionItem (+ `build`, §6.5)
    builds: list[BuildRow]
    outcomes: list[FloorOutcome]
    stopped: list[StoppedRow]
    pre_build: list[PreBuild]


# ── GET /v1/code-tasks/{id}/cockpit (§6.3) ──────────────────────────────────────────────────


class StoryAct(TypedDict, total=False):
    id: str                        # act id
    type: Literal["act"]
    station: str
    act: dict[str, Any]            # agent_acts.serialize (+ verb)
    title: str                     # stage title: "Write tests", "Code review"
    seat: Optional[int]
    round: int
    word: str                      # "Done" | "Working" | "Failed" | "Stopped" | verdict word
    verdict: Optional[str]         # approved | changes_asked | blocked
    files: Optional[list[str]]
    tool_calls: int
    comments_authored: int
    comments_fixed: int
    said: Optional[str]            # summary / last completion excerpt, redacted, trimmed
    failure: Optional[dict[str, str]]   # {line, tip} on the act where a failed build stopped


class StoryYou(TypedDict, total=False):
    id: str
    type: Literal["you"]
    station: Optional[str]
    what: str                      # steered | approved_plan | waived | retried | stopped
    actor: str
    text: Optional[str]            # the guidance / the reason
    at: str
    applied_at: Optional[str]      # steer: when an agent read it
    applied_act_id: Optional[str]
    applied_role: Optional[str]    # steer: who read it ("Read by <name>")
    count: Optional[int]           # waived: how many checks


class StoryBoss(TypedDict, total=False):
    id: str
    type: Literal["boss"]
    station: Optional[str]
    what: str                      # cloned | ran_tests | opened_pr | started_preview | deployed
    text: str                      # "Cloned the repo" | "Opened PR #842" | "Deployed to dev"
    at: str
    ms: Optional[int]
    detail: Optional[dict[str, Any]]


class Now(TypedDict, total=False):
    act_id: str
    role_key: Optional[str]
    tool: str
    arg: Optional[str]             # a path, or ≤ 40 chars of a command — never content
    at: str


class Cockpit(TypedDict, total=False):
    etag: str
    run: dict[str, Any]            # the GET /v1/code-tasks/{id} shape + created_at, updated_at
    app: str
    state: BuildState
    elapsed_from: Optional[str]    # started_at; the web ticks elapsed from it
    station_since: Optional[str]
    cost_usd: float
    metered: bool
    live: bool                     # poll at 4s (True) or 30s
    stoppable: bool
    retryable: bool
    retry_reason: Optional[str]
    failure: Optional[dict[str, str]]   # failure_line(): {line, tip}
    lineage: list[dict[str, Any]]  # [{run_id, attempt, state, word, at, kind, title, pr_number,
    #                                 cost_usd, same_lineage}] every build of this app
    origin: Optional[Origin]
    brief: dict[str, Any]          # brief_of(): criteria, checks, repo, branch, workflow, engines
    agents: list[dict[str, Any]]   # [{role_key, state}] live first
    stations: list[Station]
    attention: list[dict[str, Any]]
    story: list[dict[str, Any]]    # StoryAct | StoryYou | StoryBoss, oldest first
    now: Optional[Now]
    outputs: dict[str, Any]        # see apps/web/lib/cockpit.ts CockpitOutputs
    summary: dict[str, Any]        # build_detail.now_summary: what it is doing now


# ── labels and sentences (§5.1) ─────────────────────────────────────────────────────────────

GATE_LABELS: dict[str, tuple[str, str]] = {
    # gate name: (label, owner station)
    "code_written": ("Code written", "build"),
    "written_tests_pass": ("New tests pass", "check"),
    "existing_tests_intact": ("Old tests pass", "check"),
    "tests_written_first": ("Tests came first", "tests"),
    "coder_did_not_edit_tests": ("Tests untouched", "build"),
    "tests_strong_enough": ("Tests catch bugs", "check"),
    "code_review_passed": ("Review passed", "review"),
    "security_clean": ("Security clean", "review"),
    "consistent": ("Consistent", "review"),
    "nothing_broke": ("Nothing broke", "check"),
    "app_serves": ("App starts", "check"),
    "web_package_honest": ("Package honest", "build"),
    # An applied template's two gates (F2 §2.3; unwaivable, see UNWAIVABLE_GATES).
    "template_tests": ("Template tests", "check"),
    "no_committed_secrets": ("No secrets", "check"),
}
#: Gates a waiver can never open a PR past: the app must start, and an applied template's own
#: tests must pass and its diff must hold no secret (governed_code_task_service.TEMPLATE_GATES).
UNWAIVABLE_GATES = frozenset({"app_serves", "template_tests", "no_committed_secrets"})
#: Why each unwaivable gate cannot be waived, in the words the refusal uses.
UNWAIVABLE_WHY = {
    "app_serves": "the app must start",
    "template_tests": "an applied template's tests must pass",
    "no_committed_secrets": "a committed secret never reaches a pull request",
}


def gate_label(name: str) -> str:
    """The one label for a quality gate. `iac_*` gates read `Infra: <name>`."""
    key = str(name or "")
    if key in GATE_LABELS:
        return GATE_LABELS[key][0]
    if key.startswith("iac_"):
        return "Infra: " + key[4:].replace("_", " ")
    return key.replace("_", " ").capitalize()


def gate_station(name: str) -> str:
    key = str(name or "")
    if key in GATE_LABELS:
        return GATE_LABELS[key][1]
    return "check"


def failing_gate_stop(fctx: Optional[Mapping[str, Any]]) -> Optional[tuple[str, str]]:
    """WHERE A BUILD THAT FAILED ITS CHECKS STOPPED (rc10.5 walk M2): the first failing gate in
    pipeline order, and the station that owns it. When the app does not start EVERY gate after
    it fails too (review, consistency, …), so a gate's failure only names the station when it is
    the first one; the rest are consequences. None when no gate failed."""
    failing = [str(g) for g in ((fctx or {}).get("failing_gates") or [])]
    if not failing:
        return None
    order = {key: i for i, key in enumerate(CORE_STATIONS)}
    first = min(enumerate(failing),
                key=lambda ig: (order.get(gate_station(ig[1]), len(order)), ig[0]))[1]
    return first, gate_station(first)


def stop_reason(run: Mapping[str, Any], fctx: Optional[Mapping[str, Any]]) -> str:
    """Why a stopped build stopped, short: the run's own error in its first sentence, without the
    `Blocked:` it opens with (`the app did not start: …`); else the first failing gate
    (`Failed check: App starts`); else the failure class's words. The error first: it says what
    actually went wrong, and the first failing gate in pipeline order may only be a symptom."""
    stop = failing_gate_stop(fctx)
    error = _first_line(re.sub(r"^\s*(Blocked|Failed)\s*:\s*", "", str(run.get("error") or ""),
                               flags=re.I))
    error = re.split(r"(?<=\.)\s+(?=[A-Z])", error, maxsplit=1)[0] if error else ""
    if error:
        return error
    if stop:
        return f"Failed check: {gate_label(stop[0])}"
    said = failure_line(dict(fctx or {}), run.get("error"))
    return error or said["tip"] or (said["line"] if said["line"] != "Failed" else "")


_FAILURE_WORDS = {
    "recoverable": "Failed · retry may fix",
    "terminal": "Failed · needs a change",
    "needs_human": "Needs a decision",
    "infra": "Host problem",
}


def failure_line(fctx: Optional[dict[str, Any]], error: Optional[str] = None) -> dict[str, str]:
    """{line, tip}: ≤ 6 visible words from failure_class, the first line of the error as tip."""
    ctx = fctx if isinstance(fctx, dict) else {}
    cls = str(ctx.get("failure_class") or "")
    line = _FAILURE_WORDS.get(cls, "Failed")
    tip = ""
    for value in (ctx.get("host_advice"), error, ctx.get("error")):
        if value and str(value).strip():
            tip = str(value).strip().splitlines()[0][:160]
            break
    return {"line": line, "tip": tip}


# ══ Implementation (S1) ═════════════════════════════════════════════════════════════════════

import hashlib  # noqa: E402
from typing import Mapping  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402

from sqlalchemy import text  # noqa: E402

from .build_detail import kept_stations, kept_word, now_summary  # noqa: E402

CORE_STATIONS: tuple[str, ...] = ("plan", "tests", "build", "check", "review", "pr", "preview",
                                  "ship")
#: Stations where an agent stands. The rest are BOSS's machinery (Check when no qa act ran it).
AGENT_STATIONS = frozenset({"plan", "tests", "build", "review"})
MACHINE_STATIONS = frozenset({"check", "pr", "preview"})
#: The role a pending station's hollow face may show: only where the role is certain.
CERTAIN_ROLE: dict[str, str] = {
    "plan": "chief_architect", "tests": "qa", "review": "adversarial_reviewer",
}
STATION_VERB: dict[str, str] = {
    "plan": "Planning", "you_plan": "Needs you", "tests": "Testing", "build": "Coding",
    "check": "Testing", "review": "Reviewing", "pr": "Working", "preview": "Deploying",
    "ship": "Deploying",
}
BOSS_TIP = "BOSS machinery. No agent at this station."
OUTCOME_WINDOW = timedelta(days=7)
FLOOR_RUNS = 200
#: BOSS's own builds read beside the window (never inside it: they used to fill it).
INTERNAL_RUNS = 10
ACT_RUNS_CAP = 40
DEPLOY_OK = frozenset({"healthy", "succeeded", "completed", "passed"})
DEPLOY_BAD = frozenset({"failed", "rejected", "rolled_back", "rollback_pending"})
PREVIEW_FLAGS = frozenset({"starting", "running", "failed"})
VERDICT_WORDS = {"approved": "Approved", "changes_asked": "Changes asked", "blocked": "Blocked"}
_VERDICT_OF = {"survived": "approved", "clean": "approved", "consistent": "approved",
               "refuted": "changes_asked", "issues": "changes_asked", "drift": "changes_asked"}
_SEAT_VERDICT_KEY = {"review_code": "adversarial_verdict", "security_check": "security_verdict",
                     "consistency_check": "consistency_verdict"}
_SEAT_GATE = {"review_code": "code_review_passed", "security_check": "security_clean",
              "consistency_check": "consistent"}
_SAID_KEY = {"write_tests": "qa_summary", "review_code": "review_summary",
             "security_check": "security_summary", "consistency_check": "consistency_summary"}
_FILE_TOOLS = frozenset({"read", "edit", "write", "multiedit", "notebookedit", "str_replace",
                         "create", "view", "str_replace_editor", "apply_patch"})
_SHELL_TOOLS = frozenset({"bash", "shell", "run", "exec", "terminal"})


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: Any) -> Optional[datetime]:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _iso(value: Any) -> Optional[str]:
    found = _aware(value)
    return found.isoformat() if found else None


def _j(value: Any, default: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return default
    return value if value is not None else default


def _minutes(delta: timedelta) -> str:
    """`24m`, `3h`, `2d` — a duration as the Floor says it."""
    mins = max(0, int(delta.total_seconds() // 60))
    if mins < 60:
        return f"{mins}m"
    if mins < 60 * 48:
        return f"{mins // 60}h"
    return f"{mins // (60 * 24)}d"


def _first_line(value: Any, limit: int = 160) -> str:
    for line in str(value or "").splitlines():
        if line.strip():
            return line.strip()[:limit]
    return ""


def _pr_number(run: Mapping[str, Any]) -> Optional[int]:
    number = run.get("pull_request_number")
    if number:
        return int(number)
    found = re.search(r"/pull/(\d+)", str(run.get("pull_request_url") or ""))
    return int(found.group(1)) if found else None


# ── naming an app (the engine's half of app-identity.ts / app-grouping.ts) ──────────────────


def _is_internal(run: Mapping[str, Any]) -> bool:
    from .code_task_service import is_internal_run

    return is_internal_run(dict(run))


def app_name(run: Mapping[str, Any]) -> str:
    """What a person calls the app this build makes — a name, never a sentence: no trailing
    `.`/`!`/`?`/`:`/`;`/`,` and no wrapping quotes (boss-1 read "Lead Triage Inbox.", the
    brief's own sentence)."""
    return _a_name(_app_name(run))


_NAME_EDGES = " \t\"'“”‘’`.,;:!?…"


def _a_name(name: str) -> str:
    # Markdown emphasis is not part of a name ("Re-build of *Path Verification*").
    text_ = re.sub(r"[*_`]+", "", str(name or ""))
    cleaned = text_.strip().strip(_NAME_EDGES).strip()
    return cleaned or str(name or "").strip()


def _app_name(run: Mapping[str, Any]) -> str:
    """The benchmark's name, the name the brief gives itself (naming.project_name), else the
    directive that asked for it."""
    from .naming import project_name

    fctx = _j(run.get("finalize_ctx"), {}) or {}
    bench = fctx.get("qa_benchmark") if isinstance(fctx.get("qa_benchmark"), dict) else {}
    if bench.get("name"):
        return str(bench["name"])[:46]
    from .code_task_service import is_internal_run

    if is_internal_run(dict(run)):
        # BOSS's own build is named for the app it works on, never "Captain" (D14).
        spec = _j(run.get("spec"), {}) or {}
        repo = str(spec.get("repo") or fctx.get("repo") or "").strip()
        if repo and repo != "_fixture":
            return repo[:46]
    title = re.sub(r"^\[\d+/\d+\]\s*", "", str(run.get("title") or ""))
    name = project_name(title)[0]
    if name == "Untitled app":
        origin = fctx.get("origin") if isinstance(fctx.get("origin"), dict) else {}
        directive = origin.get("title") or origin.get("directive")
        if directive:
            alt = project_name(str(directive))[0]
            if alt != "Untitled app":
                return alt
    return name


def _identity_tokens(run: Mapping[str, Any]) -> list[str]:
    from .naming import identity_key

    fctx = _j(run.get("finalize_ctx"), {}) or {}
    spec = _j(run.get("spec"), {}) or {}
    tokens = [f"id:{run['id']}"]
    if fctx.get("app_id"):
        tokens.append(f"app:{fctx['app_id']}")
    name = app_name(run)
    if name and name != "Untitled app":
        tokens.append(f"app:{str(spec.get('repo') or '').strip().lower()}␟"
                      f"{identity_key(name)}")
    bench = fctx.get("qa_benchmark") if isinstance(fctx.get("qa_benchmark"), dict) else {}
    if bench.get("id"):
        tokens.append(f"qa:{bench['id']}")
    for field in ("root_run_id", "deferred_from_run", "parent_run_id"):
        if fctx.get(field):
            tokens.append(f"id:{fctx[field]}")
    matched = re.match(r"^boss/code/(crun_[a-z0-9]+)$", str(spec.get("base_branch") or ""), re.I)
    if matched:
        tokens.append(f"id:{matched.group(1)}")
    return tokens


def group_into_apps(runs: list[Mapping[str, Any]]) -> list[list[Mapping[str, Any]]]:
    """Runs grouped by IDENTITY into apps, in the order apps first appear (groupRunsIntoApps)."""
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    tokens = [_identity_tokens(r) for r in runs]
    for toks in tokens:
        for t in toks[1:]:
            ra, rb = find(toks[0]), find(t)
            if ra != rb:
                parent[rb] = ra
    groups: dict[str, list[Mapping[str, Any]]] = {}
    order: list[str] = []
    for run, toks in zip(runs, tokens):
        root = find(toks[0])
        if root not in groups:
            groups[root] = []
            order.append(root)
        groups[root].append(run)
    return [groups[k] for k in order]


# ── what one build is made of: its acts, decision, preview, deploys, cost ───────────────────


class Facts:
    """Everything the derivations read about a set of builds, loaded in a fixed number of
    queries whatever the number of builds (one per kind of fact)."""

    def __init__(self) -> None:
        self.acts: dict[str, list[dict[str, Any]]] = {}
        self.decisions: dict[str, dict[str, Any]] = {}
        self.previews: dict[str, dict[str, Any]] = {}
        self.deploys: dict[str, list[dict[str, Any]]] = {}
        self.cost: dict[str, dict[str, Any]] = {}
        self.gates: dict[str, dict[str, Any]] = {}
        #: The build's newest code.task job: a job the budget holds is `queued` with a later
        #: run_after and `last_error.kind = budget_hold` — not "waiting for a worker".
        self.jobs: dict[str, dict[str, Any]] = {}
        self.tool_calls: dict[str, int] = {}


def load_facts(connection: Any, project_id: str, runs: list[Mapping[str, Any]], *,
               acts: bool = True, deploys: bool = True,
               gates: bool = False, tool_calls: bool = False) -> Facts:
    facts = Facts()
    ids = [str(r["id"]) for r in runs]
    if not ids:
        return facts
    if acts:
        for row in connection.execute(
            text("SELECT * FROM agent_acts WHERE project_id = :p AND code_task_run_id = ANY(:ids) "
                 "AND kind IN ('stage', 'run') ORDER BY started_at, act_id"),
            {"p": project_id, "ids": ids},
        ).mappings().all():
            facts.acts.setdefault(str(row["code_task_run_id"]), []).append(dict(row))
    decision_ids = [str(r["decision_id"]) for r in runs if r.get("decision_id")]
    if decision_ids:
        for row in connection.execute(
            text("SELECT id, status, risk_tier, default_action, deadline_at, recommendation, "
                 "approved_by, approved_at, executed_at, updated_at, created_at, question, "
                 "task_id, delegated_to FROM decisions WHERE project_id = :p AND id = ANY(:ids)"),
            {"p": project_id, "ids": decision_ids},
        ).mappings().all():
            item = dict(row)
            item["recommendation"] = _j(item.get("recommendation"), {}) or {}
            facts.decisions[str(row["id"])] = item
        # THE MERGES BOSS PERFORMED, by merge question (pr_merge): what makes a PR "merged".
        from .pr_merge import merged_shas

        for did, sha in merged_shas(connection, project_id, decision_ids).items():
            if did in facts.decisions:
                facts.decisions[did]["merged_sha"] = sha
    for row in connection.execute(
        text("SELECT DISTINCT ON (run_id) id, run_id, status, url, error, expires_at, steps, "
             "created_at, substrate FROM app_previews WHERE project_id = :p AND run_id = ANY(:ids) "
             "ORDER BY run_id, created_at DESC"),
        {"p": project_id, "ids": ids},
    ).mappings().all():
        facts.previews[str(row["run_id"])] = dict(row)
    if deploys:
        names = {str(r["id"]): app_name(r) for r in runs}
        from .naming import identity_key

        wanted = {identity_key(n): rid for rid, n in names.items() if n != "Untitled app"}
        for row in connection.execute(
            text("SELECT r.id, r.app_name, r.environment, r.status, r.commit_ref, r.started_at, "
                 "r.finished_at, r.failure_reason, r.trigger, r.deployment_target_id, r.steps, "
                 "r.created_at, r.release_packet->>'code_task_run_id' AS run_id "
                 "FROM deployment_runs r WHERE r.project_id = :p "
                 "ORDER BY r.created_at DESC LIMIT 200"),
            {"p": project_id},
        ).mappings().all():
            rid = str(row["run_id"] or "")
            if rid not in names:
                rid = wanted.get(identity_key(str(row["app_name"] or "")), "")
            if rid:
                facts.deploys.setdefault(rid, []).append(dict(row))
    for row in connection.execute(
        text("SELECT code_task_run_id AS run_id, "
             "COALESCE(sum(cost_usd_micros) FILTER (WHERE NOT included_in_subscription), 0) "
             "AS metered, count(*) FILTER (WHERE included_in_subscription) AS included, "
             "count(*) AS calls FROM llm_calls WHERE code_task_run_id = ANY(:ids) "
             "GROUP BY code_task_run_id"),
        {"ids": ids},
    ).mappings().all():
        facts.cost[str(row["run_id"])] = {"metered_usd": int(row["metered"] or 0) / 1e6,
                                          "included": int(row["included"] or 0),
                                          "calls": int(row["calls"] or 0)}
    for row in connection.execute(
        text("SELECT DISTINCT ON (payload->>'run_id') payload->>'run_id' AS run_id, id, status, "
             "last_error, run_after FROM jobs WHERE project_id = :p AND kind = 'code.task' "
             "AND payload->>'run_id' = ANY(:ids) ORDER BY payload->>'run_id', created_at DESC"),
        {"p": project_id, "ids": ids},
    ).mappings().all():
        job = dict(row)
        job["last_error"] = _j(job.get("last_error"), {}) or {}
        facts.jobs[str(row["run_id"])] = job
    if gates:
        for row in connection.execute(
            text("SELECT DISTINCT ON (code_task_run_id) code_task_run_id, id, status, "
                 "gates_passed, gates_failed, evidence, created_at FROM quality_gates "
                 "WHERE project_id = :p AND code_task_run_id = ANY(:ids) "
                 "ORDER BY code_task_run_id, created_at DESC"),
            {"p": project_id, "ids": ids},
        ).mappings().all():
            facts.gates[str(row["code_task_run_id"])] = dict(row)
    if tool_calls:
        act_ids = [a["act_id"] for acts_ in facts.acts.values() for a in acts_]
        if act_ids:
            for row in connection.execute(
                text("SELECT act_id, count(*) AS n FROM agent_turns WHERE act_id = ANY(:a) "
                     "AND direction = 'tool_call' GROUP BY act_id"),
                {"a": act_ids},
            ).mappings().all():
                facts.tool_calls[str(row["act_id"])] = int(row["n"] or 0)
    return facts


# ── one state per build (§4.4) ──────────────────────────────────────────────────────────────


def _stage_acts(acts: list[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [a for a in acts if a.get("kind") == "stage"]


def _open_act(acts: list[Mapping[str, Any]]) -> Optional[Mapping[str, Any]]:
    live = [a for a in _stage_acts(acts) if a.get("ended_at") is None]
    return live[-1] if live else None


def _last_act_at(acts: list[Mapping[str, Any]]) -> Optional[datetime]:
    times = [t for a in acts for t in (_aware(a.get("started_at")), _aware(a.get("ended_at")))
             if t]
    return max(times) if times else None


def _station_of_stage(stage: Any) -> Optional[str]:
    return STAGE_STATION.get(str(stage or "").strip().lower())


def _current_station(run: Mapping[str, Any], acts: list[Mapping[str, Any]]) -> Optional[str]:
    """Where the build is: its phase's station, else the station of its newest act."""
    phase = str(run.get("phase") or "")
    if phase in ("queued",):
        return None
    if phase == "provisioning":
        return "plan"
    here = _station_of_stage(phase)
    if here:
        return here
    stages = _stage_acts(acts)
    if stages:
        return _station_of_stage(stages[-1].get("step_id"))
    return None


def _next_station(after: Optional[str]) -> str:
    if after is None or after not in CORE_STATIONS:
        return "plan"
    i = CORE_STATIONS.index(after)
    return CORE_STATIONS[min(i + 1, len(CORE_STATIONS) - 1)]


def _rounds(acts: list[Mapping[str, Any]]) -> int:
    """Repair rounds: the build station worked again after its first act."""
    build = [a for a in _stage_acts(acts) if _station_of_stage(a.get("step_id")) == "build"]
    return max(0, len(build) - 1)


def _decision_state(decision: Optional[Mapping[str, Any]], now: datetime) -> Optional[str]:
    """`waiting` (the operator's), `approved`, or `other` — read through decision_actions, the
    one rule for what waits on the operator. APPROVED IS NOT MERGED: whether the pull request
    merged is `pr_merge.state` (a merge sha on record, or GitHub's own state)."""
    if not decision:
        return None
    from . import decision_actions

    if decision_actions.attention(dict(decision), now) == "needs_action":
        return "waiting"
    if str(decision.get("status") or "") in ("approved", "executed"):
        return "approved"
    return "other"


def _latest_deploy(deploys: list[Mapping[str, Any]]) -> Optional[Mapping[str, Any]]:
    return deploys[0] if deploys else None


def preview_flag(preview: Optional[Mapping[str, Any]], now: Optional[datetime] = None
                 ) -> Optional[PreviewFlag]:
    if not preview:
        return None
    status = str(preview.get("status") or "")
    if status not in PREVIEW_FLAGS:
        return None
    expires = _aware(preview.get("expires_at"))
    if status == "running" and expires and expires < (now or _now()):
        return None
    # LIVE ONLY WHEN IT ANSWERS (D10): the row says `running` while its steps still boot the
    # app; until every step is done it is starting, and nobody is offered a link to it.
    # A FAILED STEP IS NOT A BOOTING ONE (tester P1-4): `auto` tries substrates in order, and a
    # substrate that crashed is recorded `failed` before the next one serves. Counting it as
    # unfinished held a serving Java preview at "Starting… 1 of 3" forever, with no Open link.
    steps = [x for x in _j(preview.get("steps"), []) or [] if isinstance(x, dict)]
    if status == "running" and steps and any(
            x.get("status") not in ("ok", "skipped", "failed") for x in steps):
        status = "starting"
    return {"status": status, "url": preview.get("url") if status == "running" else None}


def _who_stopped(fctx: Mapping[str, Any], viewer: Optional[str]) -> str:
    from .people_names import who

    actor = str(fctx.get("cancelled_by") or "").strip()
    if not actor:
        return "Stopped"
    # A name, never an id: the build list said "Stopped by usr_kdzhawzy7mxmp5hpb3bqlgqojs".
    return f"Stopped by {who(actor, viewer=viewer)}"


def build_state(run: Mapping[str, Any], *, acts: Optional[list[Mapping[str, Any]]] = None,
                decision: Optional[Mapping[str, Any]] = None,
                preview: Optional[Mapping[str, Any]] = None,
                deploys: Optional[list[Mapping[str, Any]]] = None,
                now: Optional[datetime] = None, viewer: Optional[str] = None,
                job: Optional[Mapping[str, Any]] = None) -> BuildState:
    """THE ONE STATE of a build (§4.4), top-down precedence. The Floor, the cockpit, MCP
    get_code_task and Agents › Live all read this; none derives its own."""
    from . import agent_acts

    now = now or _now()
    acts = list(acts or [])
    fctx = _j(run.get("finalize_ctx"), {}) or {}
    status = str(run.get("status") or "")
    phase = str(run.get("phase") or "")
    station = _current_station(run, acts)
    rounds = _rounds(acts)
    flag = preview_flag(preview, now)
    open_act = _open_act(acts)

    def out(state: str, word: str, tip: Optional[str], verb: str,
            at: Optional[str] = "") -> BuildState:
        return {"state": state, "word": word, "tip": tip, "verb": verb,
                "station": station if at == "" else at,
                "rounds": rounds, "preview": flag["status"] if flag else None}

    # A BUILD THAT FAILED ITS CHECKS STOPPED AT THE FIRST FAILING GATE'S STATION, not at the
    # last stage that happened to run (the build line and NOW must name the same station).
    if status in ("failed", "blocked"):
        gate_stop = failing_gate_stop(fctx)
        if gate_stop:
            station = gate_stop[1]
    label = STATION_LABELS.get(station or "", "")
    if status == "cancelled":
        when = _aware(fctx.get("cancelled_at")) or _aware(run.get("updated_at"))
        tip = "Cancelled" + (f" at {label}" if label else "") + (
            f" · {when.strftime('%H:%M')}" if when else "")
        return out("cancelled", _who_stopped(fctx, viewer), tip, "Stopped")
    if status == "failed":
        return out("failed", f"Failed · {label}" if label else "Failed",
                   failure_line(fctx, run.get("error"))["line"], "Failed")
    if status == "blocked":
        if phase == "awaiting_plan_approval":
            return out("needs_plan", "Needs you · plan", "Approve the plan to start building",
                       "Needs you", "you_plan")
        if phase == "awaiting_gate_override":
            failing = [gate_label(g) for g in fctx.get("failing_gates") or []]
            return out("needs_waiver", "Checks failed", " · ".join(failing)[:120] or None,
                       "Failed", station or "review")
        if str(fctx.get("reason_code") or "") == "budget_denied":
            last = _stage_acts(acts)
            here = _next_station(_station_of_stage(last[-1].get("step_id")) if last else None) \
                if last else "plan"
            return out("budget_refused", "Budget refused",
                       "Over the build budget. Raise it or retry.", "Blocked", here)
        return out("blocked", "Blocked", _first_line(run.get("error")) or None, "Blocked")
    if status in ("running", "repairing"):
        if fctx.get("budget_wait_since") or budget_held(job, now):
            return out("budget_wait", "Waiting · budget",
                       "Waiting for budget to free up. Costs nothing meanwhile.", "Waiting")
        if phase == "queued":
            return out("queued", "Queued", "Waiting for a worker", "Waiting", None)
        if phase == "provisioning":
            return out("queued", "Starting", "Cloning the repo", "Working", "plan")
        moved = max([t for t in (_aware(run.get("updated_at")), _last_act_at(acts)) if t],
                    default=None)
        if moved and now - moved > timedelta(minutes=QUIET_AFTER_MINUTES):
            gap = _minutes(now - moved)
            verb = agent_acts.work_verb(dict(open_act)) if open_act else \
                STATION_VERB.get(station or "", "Working")
            return out("quiet", f"Quiet · {gap}", f"No progress for {gap}. It may be stuck.", verb)
        verb = agent_acts.work_verb(dict(open_act)) if open_act else \
            STATION_VERB.get(station or "", "Working")
        word = verb + (f" · round {rounds + 1}" if rounds and station in ("build", "review")
                       else "")
        stage = (open_act or {}).get("step_id")
        tip = f"{label} · {agent_acts.STAGE_TITLES.get(str(stage), '')}".strip(" ·") \
            if label else None
        return out("building", word, tip or None, verb)
    # Finished: completed / simulated.
    ok_deploys = [d for d in deploys or [] if str(d.get("status") or "") in DEPLOY_OK]
    if ok_deploys:
        return out("deployed", f"Deployed · {ok_deploys[0].get('environment') or 'dev'}", None,
                   "Working", "ship")
    if run.get("pull_request_url") or run.get("pull_request_number"):
        from . import pr_merge

        merge = pr_merge.state(run, decision)
        if merge == pr_merge.MERGED:
            return out("merged", "Merged", None, "Working", "ship")
        if merge == pr_merge.MERGE_PENDING:
            # #229: the merge was approved, and BOSS does not merge here — still open.
            return out("pr_open", "Approved · not merged",
                       "The merge was approved, but the pull request is still open: merge it "
                       "in GitHub", "Needs you", "ship")
        checks = fctx.get("checks_failing")
        if isinstance(checks, dict) and checks.get("gates"):
            # THE PR OPENED WITH ITS CHECKS FAILING (checks_failing.py): open, never parked for
            # a waiver, and never read as a passing change. The merge is the person's.
            from .checks_failing import tip as _checks_tip

            return out("pr_open", "PR open · checks failing", _checks_tip(checks),
                       "Needs you", "ship")
        return out("pr_open", "PR open", None, "Needs you", "ship")
    index, total = fctx.get("decomposition_index"), fctx.get("decomposition_total")
    tip = f"Part {index} of {total}" if index and total else None
    return out("done_no_pr", "Done · no PR", tip, "Working", "pr")


def budget_held(job: Optional[Mapping[str, Any]], now: Optional[datetime] = None) -> bool:
    """The job the budget is holding (jobs._defer_for_budget_state): queued, its last error a
    `budget_hold`, its run_after still ahead. Read as `Waiting · budget`, never as a worker
    that has not come (factory test D3: it read "Queued · Waiting for a worker" for 12 min)."""
    if not job or str(job.get("status") or "") != "queued":
        return False
    err = _j(job.get("last_error"), {}) or {}
    if str(err.get("kind") or "") != "budget_hold":
        return False
    after = _aware(job.get("run_after"))
    return after is None or after > (now or _now())


def section_of(state: str) -> str:
    return SECTION_OF.get(state, "building")


# ── the line of stations (§4.3) ─────────────────────────────────────────────────────────────


def _act_station_status(act: Mapping[str, Any]) -> str:
    # An act that errored FAILED; one closed stale was left open when its build stopped. Neither
    # is stuck: that is a live build gone quiet, which an act's record cannot say.
    return {"active": "working", "pending": "working", "awaiting_input": "needs_you",
            "complete": "done", "error": "failed", "stale": "stopped",
            "skipped": "skipped"}.get(str(act.get("status") or ""), "pending")


def _failed_tip(run: Mapping[str, Any], fctx: Mapping[str, Any], state_key: str,
                bstate: Mapping[str, Any]) -> str:
    """Why a station failed, short: the first failing gate and the run's own error
    (stop_reason), else the failing checks, else plain words."""
    reason = stop_reason(run, fctx)
    if reason:
        return reason
    if state_key == "needs_waiver" and bstate.get("tip"):
        return f"Checks failed: {bstate['tip']}"
    return "This stage failed"


def _verdict(stage: str, state: Mapping[str, Any], failing: set[str],
             gates_are_its_own: bool = True) -> Optional[str]:
    """A seat's verdict: what the seat itself recorded. Its gate failing says `blocked` only when
    review is where the build stopped (`gates_are_its_own`): when the app did not start, every
    review gate fails as a consequence, and a seat that said "clean" did not block anything."""
    raw = _VERDICT_OF.get(str(state.get(_SEAT_VERDICT_KEY.get(stage, "")) or ""))
    gate = _SEAT_GATE.get(stage)
    if gate and gate in failing and gates_are_its_own and raw != "approved":
        return "blocked"
    return raw


def _skip_tip(station: str, state: Mapping[str, Any],
              spec: Optional[Mapping[str, Any]] = None) -> str:
    """WHY a station was passed, from what the run recorded (factory test D15: a running build
    said "No Plan act"). The tier the run chose, else the profile, else the plain fact."""
    tier = state.get("risk_tier")
    if not isinstance(tier, int):
        tier = (spec or {}).get("risk_tier")
    if isinstance(tier, int):
        if station == "plan" and tier < 2:
            return "Small change: no planning step"
        if station == "tests" and tier == 0:
            return "Small change: tests written with the code"
        if station == "review" and tier == 0:
            return "Small change: no review panel"
    profile = str(state.get("profile") or "").strip()
    name = STATION_LABELS[station]
    if profile:
        return f"{profile.capitalize()} profile skips {name}"
    return f"This build skipped {name}"


def stations_for(run: Mapping[str, Any], *, acts: Optional[list[Mapping[str, Any]]] = None,
                 decision: Optional[Mapping[str, Any]] = None,
                 preview: Optional[Mapping[str, Any]] = None,
                 deploys: Optional[list[Mapping[str, Any]]] = None,
                 bstate: Optional[BuildState] = None, now: Optional[datetime] = None,
                 viewer: Optional[str] = None) -> list[Station]:
    """The build's line (§4.3). Faces come from acts; a pending station shows a hollow face only
    when its role is certain; machinery shows the BOSS glyph; nothing is drawn working without
    an act that is."""
    from . import agent_acts

    now = now or _now()
    acts = list(acts or [])
    deploys = list(deploys or [])
    st = bstate or build_state(run, acts=acts, decision=decision, preview=preview,
                               deploys=deploys, now=now, viewer=viewer)
    state_key = st["state"]
    fctx = _j(run.get("finalize_ctx"), {}) or {}
    spec = _j(run.get("spec"), {}) or {}
    evidence = _j(run.get("state"), {}) or {}
    failing = {str(g) for g in fctx.get("failing_gates") or []}
    by_station: dict[str, list[Mapping[str, Any]]] = {}
    for a in _stage_acts(acts):
        key = _station_of_stage(a.get("step_id"))
        if key:
            by_station.setdefault(key, []).append(a)
    here = st.get("station")
    finished = state_key in ("pr_open", "merged", "deployed", "done_no_pr")
    pr_n = _pr_number(run)
    flag = preview_flag(preview, now)
    ok_deploys = [d for d in deploys if str(d.get("status") or "") in DEPLOY_OK]
    order = list(CORE_STATIONS)
    plan_gate = bool(evidence.get("awaiting_plan_approval")) or state_key == "needs_plan" or (
        decision is not None and str((decision.get("recommendation") or {}).get("action") or "")
        == "resume_code_task")
    if plan_gate:
        order.insert(1, "you_plan")
    if here in order:
        here_i = order.index(here)
    elif here is None and state_key == "queued":
        here_i = -1
    else:
        here_i = len(order) if finished else -1
    # WHERE A STOPPED BUILD ENDED (rc10.5 walk M2, m3). A build that failed its checks stopped
    # at the station owning the first failing gate (build_state already says so); one that
    # stopped before any station names none, so its first station carries the failure.
    gate_stop = failing_gate_stop(fctx) if state_key in ("failed", "blocked", "needs_waiver") \
        else None
    stopped_line = state_key in STOPPED_STATES
    no_station = stopped_line and state_key != "cancelled" and here_i < 0
    if no_station:
        here_i = 1 if order[0] == "plan" and order[1:2] == ["you_plan"] else 0
    fails_here = state_key in ("failed", "needs_waiver") or no_station or (
        state_key == "blocked" and gate_stop is not None)
    #: Review seats' gates are their own verdict only when review is where the build stopped.
    review_gates_own = gate_stop is None or gate_stop[1] == "review"
    rounds = _rounds(acts)
    kept = kept_stations(fctx)
    review_acts = by_station.get("review", [])
    out: list[Station] = []
    for i, key in enumerate(order):
        mine = by_station.get(key, [])
        latest = mine[-1] if mine else None
        s: Station = {"key": key, "label": STATION_LABELS[key], "status": "pending",
                      "face": "none", "role_key": None, "hollow_role": None,
                      "act_id": latest["act_id"] if latest else None,
                      "act_ids": [a["act_id"] for a in mine], "seats": [], "word": "Next",
                      "tip": None, "started_at": _iso(mine[0].get("started_at")) if mine else None,
                      "ended_at": _iso(latest.get("ended_at")) if latest else None,
                      "edge_label": agent_acts._label_for(dict(mine[0])) if mine else None,
                      "rounds": 0, "loop_with": None, "links": []}
        before = i < here_i
        at_here = i == here_i
        if key == "you_plan":
            s.update(face="you", tip="Approve the plan to start building")
            if state_key == "needs_plan":
                s.update(status="needs_you", word="Needs you")
            elif before or finished or here_i > i:
                s.update(status="done", word="Done")
            out.append(s)
            continue
        if mine:
            status = _act_station_status(latest)
            if key in AGENT_STATIONS or latest.get("role_key"):
                s.update(face="agent" if latest.get("role_key") else "boss",
                         role_key=latest.get("role_key"))
            else:
                s.update(face="boss", tip=BOSS_TIP)
            if status == "needs_you":
                # A stage set to awaiting_input waits on the operator's answer elsewhere (the
                # plan, the checks): its own work is done, or — its checks failed — failed.
                status = "failed" if state_key == "needs_waiver" else "done"
            if status == "working" and state_key in STOPPED_STATES:
                # LEFT OPEN BECAUSE THE BUILD STOPPED (a blocked build does not close its
                # stages): nothing is working here, and nothing failed here either.
                status = "stopped"
            if stopped_line and i > here_i and status != "done":
                # PAST WHERE THE BUILD STOPPED: work that finished keeps its own word; work that
                # never finished (stale, left open, closed by the stop) was not reached.
                status = "pending"
                s.update(face="none", role_key=None)
            s["status"] = status
            if status == "failed":
                s["tip"] = _failed_tip(run, fctx, state_key, st) if (
                    state_key in STOPPED_STATES or state_key == "needs_waiver") \
                    else "This attempt failed; the build went on"
            elif status == "stopped":
                s["tip"] = "Left open when the build stopped"
        elif before or (finished and key in ("plan", "tests", "build", "check", "review")):
            # Passed without an act of its own.
            if key in AGENT_STATIONS and key != "build" and key in kept:
                # CARRIED, NOT SKIPPED: a retry seeded from a saved workspace (or handed the
                # last plan) has this station's work already; "Skipped" read as never done.
                s.update(status="done", face="none", tip=kept[key]["tip"],
                         kept_from=kept[key].get("attempt"), kept=True)
            elif key in AGENT_STATIONS and key != "build":
                s.update(status="skipped", face="none", tip=_skip_tip(key, evidence, spec))
            else:
                s.update(status="done", face="boss" if key in MACHINE_STATIONS else "none",
                         tip=BOSS_TIP if key in MACHINE_STATIONS else None)
        elif at_here:
            s.update(status="working", face="boss", tip=BOSS_TIP)
        else:
            role = CERTAIN_ROLE.get(key)
            if key == "review" and spec.get("review_panel_size") and not role:
                role = "adversarial_reviewer"
            if role:
                s.update(face="hollow", hollow_role=role,
                         tip=f"Next: {agent_acts.STAGE_TITLES.get(key, STATION_LABELS[key])}")
            elif key in MACHINE_STATIONS and key != "check":
                s.update(face="boss", tip=BOSS_TIP)
            # A pending Check is an empty slot: whether QA or BOSS runs it is known only when it
            # runs, and drawing the BOSS glyph first made the bay flip to QA's face (D15).
        # The state at the build's own station overrides what its act says.
        if at_here:
            if state_key in ("budget_wait", "queued") and s["status"] in ("working", "pending"):
                s["status"] = "waiting"
            elif state_key == "quiet" and s["status"] in ("working", "pending"):
                # STUCK, AND ONLY THIS: a live build whose station has had no activity for
                # QUIET_AFTER_MINUTES. The tip says for how long.
                s.update(status="stuck", tip=st.get("tip") or None)
            elif fails_here:
                # The station that owns the first failing gate: Failed, with the reason — never
                # "left open", whatever its own act's record says.
                s.update(status="failed", tip=_failed_tip(run, fctx, state_key, st))
            elif state_key in ("blocked", "budget_refused"):
                if s["status"] != "failed":
                    s.update(status="stopped", tip=st.get("tip") or "The build stopped here")
                if state_key == "budget_refused" and not mine:
                    s.update(face="boss")
            elif state_key == "cancelled":
                if s["status"] in ("working", "pending", "skipped", "stopped"):
                    s.update(status="stopped", stopped=True, tip=st.get("tip") or "Stopped here")
        # Review seats: every act at the review station of the latest round.
        if key == "review" and review_acts:
            seats: list[Seat] = []
            latest_start = None
            for a in review_acts:
                if a.get("step_id") == "review_code" and (a.get("seat") or 1) == 1:
                    latest_start = a.get("started_at")
            current = [a for a in review_acts
                       if latest_start is None or (_aware(a.get("started_at")) or now)
                       >= (_aware(latest_start) or now)]
            for n, a in enumerate(current, start=1):
                seat_status = _act_station_status(a)
                if seat_status == "needs_you":
                    seat_status = "failed" if state_key == "needs_waiver" else "done"
                if seat_status == "working" and state_key in STOPPED_STATES:
                    seat_status = "stopped"
                if stopped_line and i > here_i and seat_status != "done":
                    seat_status = "pending"   # a seat the stopped build never reached
                verdict = _verdict(str(a.get("step_id")), evidence, failing, review_gates_own) \
                    if a.get("ended_at") or state_key == "needs_waiver" else None
                if verdict == "blocked":
                    seat_status = "failed"
                seats.append({"act_id": a["act_id"], "role_key": a.get("role_key"), "seat": n,
                              "status": seat_status, "verdict": verdict})
            s["seats"] = seats
            if any(x["status"] == "failed" for x in seats):
                if s["status"] != "failed":
                    s["tip"] = _failed_tip(run, fctx, state_key, st)
                s["status"] = "failed"
        if key == "build" and rounds:
            s["rounds"] = rounds
            loop = next((a.get("role_key") for a in reversed(review_acts) if a.get("role_key")),
                        None)
            s["loop_with"] = loop
        if key == "pr":
            if pr_n or run.get("pull_request_url"):
                s.update(status="done", face="boss", tip=BOSS_TIP)
                s["links"] = [{"label": f"#{pr_n} ↗" if pr_n else "PR ↗",
                               "url": run.get("pull_request_url")}]
            elif state_key == "done_no_pr":
                s.update(status="skipped", face="none",
                         tip=st.get("tip") or "This build opens no PR")
        if key == "preview":
            if flag:
                s.update(face="boss", tip=BOSS_TIP,
                         status={"running": "done", "starting": "working",
                                 "failed": "stuck"}[flag["status"]])
                if flag["status"] == "running" and flag.get("url"):
                    s["links"] = [{"label": "Open ↗", "url": flag["url"]}]
            elif finished:
                s.update(status="pending", face="boss", tip=BOSS_TIP)
        if key == "ship":
            if state_key == "pr_open":
                s.update(status="needs_you", face="you", word="Needs you",
                         tip="The merge needs you")
            elif state_key in ("merged", "deployed"):
                s.update(status="done", face="you" if state_key == "merged" else "boss",
                         tip=None if state_key == "merged" else BOSS_TIP)
                s["links"] = [{"label": f"{d.get('environment') or 'dev'} ✓",
                               "url": None} for d in _one_per_env(ok_deploys)]
            elif state_key == "done_no_pr":
                s.update(status="skipped", face="none", tip="Nothing to merge")
        # NOT REACHED: past where a stopped build ended, nothing will happen in this attempt.
        # "Next" there promised work that is not coming; a hollow face did too.
        if state_key in STOPPED_STATES and i > here_i and s["status"] == "pending":
            s.update(face="none", hollow_role=None, not_reached=True,
                     tip="The build stopped before this")
        # The word under the bay.
        s["word"] = "Not reached" if s.get("not_reached") else {
            "done": "Done", "pending": "Next", "stuck": "Stuck", "skipped": "Skipped",
            "waiting": "Waiting", "needs_you": "Needs you", "failed": "Failed",
            "stopped": "Stopped"}.get(
            s["status"],
            agent_acts.work_verb(dict(latest)) if latest and s["status"] == "working"
            else STATION_VERB.get(key, "Working"))
        if key == "ship" and state_key == "merged":
            s["word"] = "Merged"
        if s.get("kept") and s["status"] == "done":
            s["word"] = kept_word(kept[key])
        out.append(s)
    if state_key in ("queued", "budget_wait") and not any(
            x["status"] in ("working", "waiting", "stuck", "needs_you", "failed", "stopped")
            for x in out):
        # NOTHING HAS STARTED: the build waits at its first station for a worker (or budget),
        # drawn as BOSS's hourglass — never an agent face claiming work (walk D22).
        first = next((x for x in out if x["status"] == "pending"), None)
        if first is not None:
            first.update(status="waiting", face="boss", role_key=None, hollow_role=None,
                         word="Waiting", tip=("Waiting for budget to free up"
                                              if state_key == "budget_wait"
                                              else "Waiting for a worker"))
    return out


def _one_per_env(deploys: list[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    seen: set[str] = set()
    out = []
    for d in deploys:
        env = str(d.get("environment") or "dev")
        if env not in seen:
            seen.add(env)
            out.append(d)
    return out


def k_of_n(stations: list[Station]) -> tuple[int, int]:
    """Stations done of stations planned. `You` is not a station of the work."""
    core = [s for s in stations if s["key"] != "you_plan"]
    # A station a STOP skipped was still planned: `2 of 8`, never `2 of 2`. Only a station the
    # build's profile never runs leaves the count.
    n = sum(1 for s in core if s["status"] != "skipped" or s.get("stopped"))
    k = sum(1 for s in core if s["status"] == "done")
    return min(k, n), n


_GATE_STAGE = {"code_review_passed": "review_code", "security_clean": "security_check",
               "consistent": "consistency_check"}


def gate_owner(name: str, acts: list[Mapping[str, Any]]) -> Optional[str]:
    """The agent whose stage owns a gate, from the build's acts — None (BOSS) when no act ran
    it. app_serves and infra checks are BOSS's own."""
    if name == "app_serves" or str(name).startswith("iac_"):
        return None
    stage = _GATE_STAGE.get(name)
    stages = _stage_acts(acts)
    if stage:
        found = [a for a in stages if a.get("step_id") == stage]
    else:
        station = gate_station(name)
        found = [a for a in stages if _station_of_stage(a.get("step_id")) == station]
        if not found and station == "check":
            found = [a for a in stages if _station_of_stage(a.get("step_id")) == "tests"]
    return (found[-1].get("role_key") if found else None) or None


def _round_word(fctx: Mapping[str, Any], evidence: Mapping[str, Any]) -> int:
    attempts = evidence.get("repair_attempts") or fctx.get("repair_attempts") or 0
    try:
        return int(attempts) + 1
    except (TypeError, ValueError):
        return 1


# ── attention: the builds stopped for you (§6.5) ────────────────────────────────────────────


def build_attention_rows(connection: Any, project_id: str,
                         decisions: list[Mapping[str, Any]]) -> dict[str, Any]:
    """What command_center.read hands assemble about builds: the blocked builds that wait on
    the operator (failed checks, a refused budget) and, for each decision whose subject is a
    build, which build. Two queries, plus one act query for the blocked ones."""
    blocked = [dict(r) for r in connection.execute(
        text(
            """
            SELECT r.id, r.title, r.status, r.phase, r.spec, r.state, r.finalize_ctx, r.error,
                   r.created_at, r.updated_at, r.pull_request_url, r.pull_request_number,
                   r.decision_id
            FROM code_task_runs r
            WHERE r.project_id = :p AND r.status = 'blocked'
              AND (r.phase = 'awaiting_gate_override'
                   OR r.finalize_ctx->>'reason_code' = 'budget_denied')
              AND NOT EXISTS (SELECT 1 FROM code_task_runs n WHERE n.project_id = r.project_id
                              AND n.finalize_ctx->>'parent_run_id' = r.id)
            ORDER BY r.updated_at DESC LIMIT 50
            """
        ),
        {"p": project_id},
    ).mappings().all()]
    acts: dict[str, list[dict[str, Any]]] = {}
    if blocked:
        for row in connection.execute(
            text("SELECT act_id, code_task_run_id, step_id, role_key, started_at, kind "
                 "FROM agent_acts WHERE project_id = :p AND code_task_run_id = ANY(:ids) "
                 "AND kind = 'stage' ORDER BY started_at"),
            {"p": project_id, "ids": [b["id"] for b in blocked]},
        ).mappings().all():
            acts.setdefault(str(row["code_task_run_id"]), []).append(dict(row))
    rows = []
    for b in blocked:
        fctx = _j(b.get("finalize_ctx"), {}) or {}
        evidence = _j(b.get("state"), {}) or {}
        mine = acts.get(b["id"], [])
        if b["phase"] == "awaiting_gate_override":
            failing = [str(g) for g in fctx.get("failing_gates") or []]
            role = next((gate_owner(g, mine) for g in failing if gate_owner(g, mine)), None)
            round_n = _round_word(fctx, evidence)
            where = gate_station(failing[0]) if failing else "review"
            rows.append({
                "kind": "gate_override", "run_id": b["id"], "app": app_name(b),
                "role_key": role, "failing": failing,
                "where": STATION_LABELS.get(where, "Review")
                + (f" · round {round_n}" if round_n > 1 else ""),
                "at": _iso(b.get("updated_at")), "error": b.get("error"),
            })
        else:
            last = mine[-1] if mine else None
            nxt = _next_station(_station_of_stage(last.get("step_id")) if last else None) \
                if last else "plan"
            rows.append({
                "kind": "budget_refused", "run_id": b["id"], "app": app_name(b),
                "role_key": None, "failing": [], "where": f"before {STATION_LABELS[nxt]}",
                "at": _iso(b.get("updated_at")), "error": b.get("error"),
            })
    by_decision: dict[str, dict[str, Any]] = {}
    ids = [str(d.get("id")) for d in decisions if d.get("id")]
    plan_runs = {str(_j(d.get("recommendation"), {}).get("run_id") or ""): str(d.get("id"))
                 for d in decisions
                 if str((_j(d.get("recommendation"), {}) or {}).get("action") or "")
                 == "resume_code_task"}
    plan_runs.pop("", None)
    if ids:
        for r in connection.execute(
            text("SELECT r.id, r.title, r.spec, r.finalize_ctx, r.decision_id, r.pull_request_url, "
                 "r.pull_request_number, r.status, r.phase, "
                 "(SELECT url FROM app_previews p WHERE p.run_id = r.id AND p.status = 'running' "
                 " ORDER BY p.created_at DESC LIMIT 1) AS preview_url "
                 "FROM code_task_runs r WHERE r.project_id = :p "
                 "AND (r.decision_id = ANY(:ids) OR r.id = ANY(:runs))"),
            {"p": project_id, "ids": ids, "runs": list(plan_runs)},
        ).mappings().all():
            row = dict(r)
            plan_decision = plan_runs.get(str(row["id"]))
            if plan_decision:
                by_decision[plan_decision] = {
                    "kind": "plan", "run_id": row["id"], "app": app_name(row), "where": "Plan",
                    "plan_url": f"/v1/code-tasks/{row['id']}"}
            if row.get("decision_id") and row["decision_id"] != plan_decision and (
                    row.get("pull_request_url") or row.get("pull_request_number")):
                by_decision[str(row["decision_id"])] = {
                    "kind": "pr", "run_id": row["id"], "app": app_name(row), "where": "PR open",
                    "pr_number": _pr_number(row), "pr_url": row.get("pull_request_url"),
                    "preview_url": row.get("preview_url")}
    return {"blocked": rows, "by_decision": by_decision}


def attention_build(info: Mapping[str, Any]) -> dict[str, Any]:
    """The `build` an attention item carries (lib/agents-live-board.ts AttentionBuild)."""
    failing = [{"name": g, "label": gate_label(g), "waivable": g not in UNWAIVABLE_GATES,
                **({"reason": "Can't be waived: " + UNWAIVABLE_WHY[g]}
                   if g in UNWAIVABLE_GATES else {})}
               for g in info.get("failing") or []]
    out: dict[str, Any] = {"run_id": info.get("run_id"), "app": info.get("app") or "",
                           "where": info.get("where") or "", "failing": failing,
                           "retryable": info.get("kind") in ("gate_override", "budget_refused"),
                           "retry_reason": None}
    for key in ("pr_url", "preview_url", "plan_url"):
        if info.get(key):
            out[key] = info[key]
    return out


def build_decision_owners(connection: Any, project_id: str,
                          wanted: Mapping[str, tuple[str, str]]) -> dict[str, Optional[str]]:
    """{decision_id: role_key} for a build's plan and merge decisions, from its acts: the plan
    is the planning act's agent, the pull request its builder's. None when no act says."""
    if not wanted:
        return {}
    runs = list({run for run, _ in wanted.values()})
    acts: dict[str, list[dict[str, Any]]] = {}
    for row in connection.execute(
        text("SELECT code_task_run_id, step_id, role_key FROM agent_acts WHERE project_id = :p "
             "AND code_task_run_id = ANY(:ids) AND kind = 'stage' ORDER BY started_at"),
        {"p": project_id, "ids": runs},
    ).mappings().all():
        acts.setdefault(str(row["code_task_run_id"]), []).append(dict(row))
    out: dict[str, Optional[str]] = {}
    for decision_id, (run_id, where) in wanted.items():
        mine = [a for a in acts.get(run_id, []) if a.get("role_key")]
        station = "plan" if where == "Plan" else "build"
        hit = [a for a in mine if _station_of_stage(a.get("step_id")) == station]
        out[decision_id] = hit[-1]["role_key"] if hit else None
    return out


# ── composing one build's row ───────────────────────────────────────────────────────────────


def _lineage_key(run: Mapping[str, Any]) -> str:
    fctx = _j(run.get("finalize_ctx"), {}) or {}
    return str(fctx.get("root_run_id") or run["id"])


def _origin(connection: Any, project_id: str, run: Mapping[str, Any],
            names: Optional[dict[str, str]] = None) -> Optional[Origin]:
    fctx = _j(run.get("finalize_ctx"), {}) or {}
    origin = fctx.get("origin") if isinstance(fctx.get("origin"), dict) else {}
    wfr = origin.get("workflow_run_id") or fctx.get("workflow_run_id")
    if wfr:
        label = (names or {}).get(str(wfr))
        if label is None and connection is not None:
            label = connection.execute(
                text("SELECT d.name FROM workflow_runs r JOIN workflow_definitions d "
                     "ON d.id = r.workflow_definition_id WHERE r.id = :r AND r.project_id = :p"),
                {"r": str(wfr), "p": project_id},
            ).scalar()
        return {"kind": "workflow_run", "id": str(wfr), "label": str(label or "Workflow")}
    meeting = origin.get("meeting_run_id") or fctx.get("meeting_run_id") or fctx.get("prd_meeting")
    if meeting:
        return {"kind": "meeting", "id": str(meeting), "label": "PRD"}
    discovery = origin.get("discovery_id") or fctx.get("discovery_id")
    if discovery:
        return {"kind": "discovery", "id": str(discovery), "label": "research"}
    return None


def lineage_origin(connection: Any, project_id: str,
                   run: Mapping[str, Any]) -> Optional[Origin]:
    """Where a build came from when it does not say: a RETRY CARRIES NO ORIGIN OF ITS OWN, the
    build it retries does (boss-1's attempt 2 of the freshness board named no workflow run,
    while attempt 1 named the one that asked for it)."""
    root = _lineage_key(run)
    if root == str(run["id"]):
        return None
    from .code_task_service import load_code_task_run

    first = load_code_task_run(connection, root)
    if not first or first.get("project_id") != project_id:
        return None
    return _origin(connection, project_id, first)


def _workflow_names(connection: Any, project_id: str,
                    runs: list[Mapping[str, Any]]) -> dict[str, str]:
    ids = []
    for r in runs:
        fctx = _j(r.get("finalize_ctx"), {}) or {}
        origin = fctx.get("origin") if isinstance(fctx.get("origin"), dict) else {}
        wfr = origin.get("workflow_run_id") or fctx.get("workflow_run_id")
        if wfr:
            ids.append(str(wfr))
    if not ids:
        return {}
    return {str(r["id"]): str(r["name"]) for r in connection.execute(
        text("SELECT r.id, d.name FROM workflow_runs r JOIN workflow_definitions d "
             "ON d.id = r.workflow_definition_id WHERE r.id = ANY(:ids) AND r.project_id = :p"),
        {"ids": ids, "p": project_id},
    ).mappings().all()}


def _retry_offer(run: Mapping[str, Any]) -> tuple[bool, Optional[str]]:
    status = str(run.get("status") or "")
    fctx = _j(run.get("finalize_ctx"), {}) or {}
    if status in ("running", "repairing"):
        return False, "Still running"
    if status == "simulated":
        return False, "A fixture can't be retried"
    if status == "completed" and not (fctx.get("advisory_failing_gates")
                                      or fctx.get("failing_gates")):
        return False, "Already passed its checks"
    return True, None


def build_row(run: Mapping[str, Any], facts: Facts, *, now: datetime,
              viewer: Optional[str] = None, attention_id: Optional[str] = None,
              origin: Optional[Origin] = None) -> BuildRow:
    rid = str(run["id"])
    acts = facts.acts.get(rid, [])
    decision = facts.decisions.get(str(run.get("decision_id") or ""))
    preview = facts.previews.get(rid)
    deploys = facts.deploys.get(rid, [])
    st = build_state(run, acts=acts, decision=decision, preview=preview, deploys=deploys,
                     now=now, viewer=viewer, job=facts.jobs.get(rid))
    stations = stations_for(run, acts=acts, decision=decision, preview=preview,
                            deploys=deploys, bstate=st, now=now, viewer=viewer)
    k, n = k_of_n(stations)
    fctx = _j(run.get("finalize_ctx"), {}) or {}
    cost = facts.cost.get(rid, {})
    metered = float(cost.get("metered_usd") or 0)
    return {
        "run_id": rid, "app": app_name(run), "title": str(run.get("title") or ""),
        "attempt": int(fctx.get("attempt") or 1), "root_run_id": _lineage_key(run),
        "actor": (fctx.get("origin") or {}).get("actor") if isinstance(fctx.get("origin"), dict)
        else fctx.get("actor"),
        "state": st["state"], "word": st["word"], "tip": st.get("tip"), "verb": st["verb"],
        "station": st.get("station"), "stations": stations, "k": k, "n": n,
        "rounds": st.get("rounds") or 0,
        "started_at": _iso(run.get("created_at")),
        "station_since": _iso(fctx.get("phase_started_at")) or _iso(run.get("updated_at")),
        "updated_at": _iso(run.get("updated_at")),
        "cost_usd": round(metered, 4) if metered > 0 else 0, "metered": metered > 0,
        "attention_id": attention_id,
        "preview": preview_flag(preview, now),
        "origin": origin,
        # BOSS's own housekeeping build (the Captain's quality pass): drawn, and marked (D14).
        "internal": _is_internal(run),
    }


def _attention_for_builds(connection: Any, settings: Any, project_id: str,
                          now: datetime) -> list[dict[str, Any]]:
    """`command_center.assemble().needs_you`, attributed (agents_live_board.attention_items),
    filtered to the items whose subject is a build — the same list the pill counts."""
    from . import agents_live_board, command_center

    read = command_center.read(connection, settings, project_id, now)
    cc = command_center.assemble(**read)
    items = agents_live_board.attention_items(connection, project_id, cc["needs_you"],
                                              read["decisions"])
    _attach_builds_to_their_runs(connection, project_id, items)
    return [i for i in items if isinstance(i.get("build"), dict)]


def _attach_builds_to_their_runs(connection: Any, project_id: str,
                                 items: list[dict[str, Any]]) -> None:
    """A gate or a failed step OF THE WORKFLOW RUN A BUILD BELONGS TO is that build's too
    (spec §4.2.1): the Floor dropped them, so its NEEDS YOU disagreed with the pill and
    Approvals. Each such item gains the build it holds up — one query for all of them."""
    from .code_task_service import load_code_task_run

    # A job the budget held raises its own decision ("Budget state red deferred job") — about
    # that build, so it is that build's card (factory test D3), keyed through the held job.
    held = [str(i.get("decision_id") or i.get("id")) for i in items
            if not isinstance(i.get("build"), dict) and i.get("kind") == "approval"]
    by_decision: dict[str, str] = {}
    if held:
        for r in connection.execute(
            text("SELECT d.id, j.payload->>'run_id' AS run_id FROM decisions d "
                 "JOIN jobs j ON j.id = d.cost_summary->>'job_id' "
                 "WHERE d.project_id = :p AND d.id = ANY(:ids) AND j.kind = 'code.task' "
                 "AND d.recommendation->>'action' = 'review_budget_policy'"),
            {"p": project_id, "ids": held},
        ).mappings().all():
            if r["run_id"]:
                by_decision[str(r["id"])] = str(r["run_id"])
    for item in items:
        rid = by_decision.get(str(item.get("decision_id") or item.get("id")))
        if rid and not isinstance(item.get("build"), dict):
            run = load_code_task_run(connection, rid)
            if run:
                item["build"] = {"run_id": rid, "app": app_name(run), "where": "Budget",
                                 "failing": [], "retryable": False}
    wanted = {str((i.get("run") or {}).get("id")) for i in items
              if not isinstance(i.get("build"), dict) and (i.get("run") or {}).get("id")}
    if not wanted:
        return
    found: dict[str, str] = {}
    for r in connection.execute(
        text("SELECT id, COALESCE(finalize_ctx->'origin'->>'workflow_run_id', "
             "finalize_ctx->>'workflow_run_id') AS wfr FROM code_task_runs "
             "WHERE project_id = :p AND COALESCE(finalize_ctx->'origin'->>'workflow_run_id', "
             "finalize_ctx->>'workflow_run_id') = ANY(:w) ORDER BY created_at DESC"),
        {"p": project_id, "w": sorted(wanted)},
    ).mappings().all():
        found.setdefault(str(r["wfr"]), str(r["id"]))
    runs: dict[str, Mapping[str, Any]] = {}
    for item in items:
        if isinstance(item.get("build"), dict):
            continue
        wf = item.get("run") or {}
        build_id = found.get(str(wf.get("id")))
        if not build_id:
            continue
        run = runs.get(build_id) or load_code_task_run(connection, build_id)
        if not run:
            continue
        runs[build_id] = run
        item["build"] = {"run_id": build_id, "app": app_name(run),
                         "where": str(wf.get("step_label") or "Workflow"),
                         "failing": [], "retryable": False}


def _etag(material: Any) -> str:
    digest = hashlib.sha256(json.dumps(material, sort_keys=True, default=str).encode())
    return f'"fx-{digest.hexdigest()[:24]}"'


# ── GET /v1/factory/floor (§6.2) ────────────────────────────────────────────────────────────


def floor(connection: Any, settings: Any, project_id: str, *, viewer: Optional[str] = None,
          now: Optional[datetime] = None) -> Floor:
    from . import command_center
    from .code_task_service import (list_code_task_runs, list_internal_code_task_runs,
                                    overlay_stop_requests)

    now = now or _now()
    runs = overlay_stop_requests(
        connection, list_code_task_runs(connection, project_id, FLOOR_RUNS, exclude_internal=True)
        # BOSS's own builds are builds (D14): read apart so they never crowd out the window.
        + list_internal_code_task_runs(connection, project_id, INTERNAL_RUNS))
    runs.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
    latest: dict[str, Mapping[str, Any]] = {}
    for r in runs:  # newest first: the first of a lineage is its latest attempt
        latest.setdefault(_lineage_key(r), r)
    building, recent = [], []
    for r in latest.values():
        status = str(r.get("status") or "")
        if status in ("running", "repairing", "blocked"):
            building.append(r)
        else:
            moved = _aware(r.get("updated_at")) or _aware(r.get("created_at"))
            if moved and now - moved <= OUTCOME_WINDOW:
                recent.append(r)
    chosen = (building + recent)[:ACT_RUNS_CAP]
    facts = load_facts(connection, project_id, chosen)
    names = _workflow_names(connection, project_id, chosen)
    attention = _attention_for_builds(connection, settings, project_id, now)
    attention_of = {str((a.get("build") or {}).get("run_id")): str(a["id"]) for a in attention}
    rows = [build_row(r, facts, now=now, viewer=viewer, attention_id=attention_of.get(str(r["id"])),
                      origin=_origin(None, project_id, r, names))
            for r in chosen]
    builds = _sort_building(rows, now)
    outcomes: list[FloorOutcome] = []
    stopped: list[StoppedRow] = []
    for b, r in ((b, r) for b, r in zip(rows, chosen)):
        sec = section_of(b["state"])
        builder = next((s.get("role_key") for s in b["stations"] if s["key"] == "build"
                        and s.get("role_key")), None)
        if sec == "shipped":
            outcomes.extend(_outcomes_of(b, r, facts, builder))
        elif sec == "stopped":
            stuck = next((s for s in b["stations"] if s["status"] in ("failed", "stopped", "stuck",
                                                                     "skipped")
                          and s.get("role_key")), None)
            ok, why = _retry_offer(r)
            stopped.append({"run_id": b["run_id"], "app": b["app"], "title": b["title"],
                            "state": b["state"], "word": b["word"], "tip": b.get("tip"),
                            "role_key": (stuck or {}).get("role_key"),
                            "station": b.get("station"), "retryable": ok, "retry_reason": why,
                            "at": b.get("updated_at") or ""})
        if b.get("preview") and b["preview"]["status"] == "running" and sec != "shipped":
            outcomes.append({"kind": "preview", "run_id": b["run_id"], "app": b["app"],
                             "role_key": builder, "text": "Preview live",
                             "url": b["preview"].get("url"), "at": b.get("updated_at") or ""})
    outcomes.sort(key=lambda o: str(o.get("at") or ""), reverse=True)
    stopped.sort(key=lambda o: str(o.get("at") or ""), reverse=True)
    pre_build = _pre_build(connection, project_id)
    apps = len(group_into_apps(runs))
    out: Floor = {
        "project_id": project_id, "etag": "", "generated_at": now.isoformat(),
        # ONE NUMBER (agents-home D2): decisions, as the pill and Approvals count them. A build
        # stopped at its checks is a card too, counted with stopped work.
        # The cards' counts by the one rule every surface uses (command_center.attention_counts):
        # `needs_you` decisions, `stopped` the stopped work NEEDS YOU lists. The STOPPED section
        # (failed builds with no newer attempt) is `stopped_builds`.
        # "N building" is builds a worker is ACTUALLY running (the `building` state). It was
        # len(builds) — the whole BUILDING section, which also holds blocked, needs-you,
        # queued, budget-waiting and quiet rows — so an idle install read "7 building" while
        # nothing ran. The rest are `waiting`.
        "counts": {"building": running_build_count(builds),
                   "waiting": len(builds) - running_build_count(builds),
                   **{k: v for k, v in command_center.attention_counts(attention).items()
                      if k in ("needs_you", "stopped")},
                   "shipped_7d": len({o["run_id"] for o in outcomes if o["kind"] != "preview"}),
                   "stopped_builds": len(stopped), "apps": apps},
        "attention": attention, "builds": builds, "outcomes": outcomes[:8],
        "stopped": stopped[:20], "pre_build": pre_build,
    }
    out["etag"] = _etag({
        "b": [(b["run_id"], b["state"], b["word"], b["k"], b["n"], b.get("cost_usd"),
               b.get("preview"), [(s["key"], s["status"], s.get("role_key"),
                                   [x["status"] for x in s.get("seats") or []])
                                  for s in b["stations"]]) for b in builds],
        "a": [(a["id"], a.get("title"), a.get("role_key")) for a in attention],
        "o": [(o["run_id"], o["kind"], o["text"]) for o in outcomes[:8]],
        "s": [(s["run_id"], s["word"], s["retryable"]) for s in stopped[:20]],
        "p": [(p["meeting_run_id"], tuple(p["seats"])) for p in pre_build],
        "c": out["counts"],
    })
    return out


def _outcomes_of(b: BuildRow, run: Mapping[str, Any], facts: Facts,
                 builder: Optional[str]) -> list[FloorOutcome]:
    n = _pr_number(run)
    at = b.get("updated_at") or ""
    base = {"run_id": b["run_id"], "app": b["app"], "role_key": builder}
    if b["state"] == "deployed":
        ok = [d for d in facts.deploys.get(b["run_id"], [])
              if str(d.get("status") or "") in DEPLOY_OK]
        when = _iso(ok[0].get("finished_at") or ok[0].get("created_at")) if ok else at
        # A deploy carries no act yet: BOSS's glyph, never a guessed agent (§4.2.3).
        return [{**base, "kind": "deployed", "role_key": None, "text": b["word"],
                 "url": None, "at": when or at}]
    if b["state"] == "merged":
        decision = facts.decisions.get(str(run.get("decision_id") or "")) or {}
        when = _iso(decision.get("executed_at") or decision.get("approved_at")
                    or decision.get("updated_at")) or at
        return [{**base, "kind": "pr_merged", "text": f"PR #{n} merged" if n else "PR merged",
                 "url": run.get("pull_request_url"), "at": when}]
    if b["state"] == "pr_open":
        return [{**base, "kind": "pr_opened", "text": f"PR #{n} opened" if n else "PR opened",
                 "url": run.get("pull_request_url"), "at": at}]
    return [{**base, "kind": "done_no_pr", "text": "Done · no PR", "url": None, "at": at}]


def _pre_build(connection: Any, project_id: str) -> list[PreBuild]:
    """Panels sitting on a PRD: pre-build work on the same Floor (§4.6)."""
    try:
        with connection.begin_nested():
            rows = connection.execute(
                text("SELECT id, title, directive, role_keys, created_at FROM meeting_runs "
                     "WHERE project_id = :p AND status = 'running' "
                     "ORDER BY created_at DESC LIMIT 6"),
                {"p": project_id},
            ).mappings().all()
    except Exception:  # noqa: BLE001 — a panel probe must never blank the Floor
        return []
    out: list[PreBuild] = []
    from .meeting_service import meeting_title

    for r in rows:
        seats = [str(k) for k in (_j(r["role_keys"], []) or []) if k]
        out.append({"meeting_run_id": str(r["id"]),
                    "title": meeting_title(r["title"], r["directive"]),
                    "seats": seats, "at": _iso(r["created_at"])})
    return out


def state_of(connection: Any, project_id: str, run_id: str, *,
             viewer: Optional[str] = None) -> Optional[dict[str, Any]]:
    """One build's state, as every reader says it (MCP get_code_task, the code-task list)."""
    from .code_task_service import load_code_task_run, overlay_stop_requests

    run = load_code_task_run(connection, run_id)
    if not run or run.get("project_id") != project_id:
        return None
    overlay_stop_requests(connection, [run])
    facts = load_facts(connection, project_id, [run])
    now = _now()
    row = build_row(run, facts, now=now, viewer=viewer)
    return {"state": row["state"], "word": row["word"], "tip": row.get("tip"),
            "verb": row["verb"], "station": row.get("station"), "k": row["k"], "n": row["n"],
            "rounds": row["rounds"], "preview": (row.get("preview") or {}).get("status")}


# ── GET /v1/code-tasks/{id}/cockpit (§6.3) ──────────────────────────────────────────────────

_ACT_WORD = {"active": "Working", "pending": "Working", "awaiting_input": "Needs you",
             "complete": "Done", "error": "Failed", "stale": "Stopped", "skipped": "Skipped"}
ENGINEERING = frozenset({"implement", "solo", "guest", "repair"})


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _trim(value: Any, limit: int = 300) -> Optional[str]:
    lines = [ln.strip() for ln in str(value or "").splitlines() if ln.strip()]
    if not lines:
        return None
    out = " ".join(lines[:3])
    return out[:limit].rstrip() + ("…" if len(out) > limit else "")


def _now_arg(tool: str, content: Any) -> Optional[str]:
    """A tool call's short argument: a path for file tools, ≤ 40 chars of a command for a
    shell. Never the content a tool wrote or read."""
    name = str(tool or "").strip().lower()
    data = _j(content, None)
    if not isinstance(data, dict):
        return None
    if name in _SHELL_TOOLS or "command" in data and name not in _FILE_TOOLS:
        cmd = data.get("command") or data.get("cmd")
        if isinstance(cmd, list):
            cmd = " ".join(str(c) for c in cmd)
        return str(cmd).strip().splitlines()[0][:40] if cmd else None
    for key in ("file_path", "path", "notebook_path", "filename"):
        if data.get(key):
            return str(data[key])[:160]
    return None


#: How far back the cockpit looks for the other builds of the same app.
SIBLING_SCAN = FLOOR_RUNS
SIBLINGS_CAP = 20


def _app_siblings(connection: Any, project_id: str, run: Mapping[str, Any]) -> list[str]:
    """Every build of the app this build makes — retries, follow-ups, changes on its branch —
    newest first: the old page's run list (`groupRunsIntoApps`), not only this lineage. Reads
    identity columns only, never whole evidence blobs."""
    rows = [dict(r) for r in connection.execute(
        text("SELECT id, title, created_at, "
             "jsonb_build_object('app_id', finalize_ctx->'app_id', "
             "'qa_benchmark', finalize_ctx->'qa_benchmark', "
             "'root_run_id', finalize_ctx->'root_run_id', "
             "'deferred_from_run', finalize_ctx->'deferred_from_run', "
             "'parent_run_id', finalize_ctx->'parent_run_id', "
             "'origin', jsonb_build_object('title', finalize_ctx->'origin'->'title', "
             "'directive', finalize_ctx->'origin'->'directive')) AS finalize_ctx, "
             "jsonb_build_object('repo', spec->'repo', 'base_branch', spec->'base_branch') AS spec "
             "FROM code_task_runs WHERE project_id = :p ORDER BY created_at DESC LIMIT :n"),
        {"p": project_id, "n": SIBLING_SCAN},
    ).mappings().all()]
    rid = str(run["id"])
    if not any(str(r["id"]) == rid for r in rows):
        rows.append({"id": rid, "title": run.get("title"), "created_at": run.get("created_at"),
                     "finalize_ctx": run.get("finalize_ctx"), "spec": run.get("spec")})
    mine_internal = _is_internal(run)
    rows = [r for r in rows if str(r["id"]) == rid or _is_internal(r) == mine_internal]
    for r in rows:
        r["finalize_ctx"] = _j(r.get("finalize_ctx"), {}) or {}
        r["spec"] = _j(r.get("spec"), {}) or {}
    for group in group_into_apps(rows):
        if any(str(r["id"]) == rid for r in group):
            return [str(r["id"]) for r in group][:SIBLINGS_CAP]
    return [rid]


def _lineage(connection: Any, project_id: str, run: Mapping[str, Any], now: datetime,
             viewer: Optional[str]) -> list[dict[str, Any]]:
    root = _lineage_key(run)
    try:
        siblings = _app_siblings(connection, project_id, run)
    except Exception:  # noqa: BLE001 — a missing sibling list is not a missing build
        siblings = [str(run["id"])]
    rows = [dict(r) for r in connection.execute(
        text("SELECT id, project_id, status, phase, title, spec, state, finalize_ctx, error, "
             "decision_id, pull_request_url, pull_request_number, created_at, updated_at "
             "FROM code_task_runs WHERE project_id = :p "
             "AND (id = :root OR finalize_ctx->>'root_run_id' = :root OR id = ANY(:ids)) "
             "ORDER BY created_at DESC LIMIT :n"),
        {"p": project_id, "root": root, "ids": siblings, "n": SIBLINGS_CAP},
    ).mappings().all()]
    for r in rows:
        r["finalize_ctx"] = _j(r.get("finalize_ctx"), {}) or {}
        r["state"] = _j(r.get("state"), {}) or {}
        r["spec"] = _j(r.get("spec"), {}) or {}
    facts = load_facts(connection, project_id, rows, deploys=False)
    out = []
    for r in rows:
        acts = facts.acts.get(str(r["id"]), [])
        st = build_state(r, acts=acts, decision=facts.decisions.get(str(r.get("decision_id") or "")),
                         preview=facts.previews.get(str(r["id"])), now=now, viewer=viewer,
                         job=facts.jobs.get(str(r["id"])))
        metered = float((facts.cost.get(str(r["id"])) or {}).get("metered_usd") or 0)
        out.append({"run_id": r["id"], "attempt": int(r["finalize_ctx"].get("attempt") or 1),
                    "state": st["state"], "word": st["word"], "at": _iso(r.get("created_at")),
                    # What the old run list said about each one: which kind of attempt, its
                    # title, its PR and what it cost.
                    "kind": _attempt_kind(r, r["finalize_ctx"]),
                    "title": str(r.get("title") or "")[:200],
                    "pr_number": _pr_number(r), "pr_url": r.get("pull_request_url"),
                    "cost_usd": round(metered, 4) if metered > 0 else 0,
                    "same_lineage": _lineage_key(r) == root})
    return out


def _comment_rows(run: Mapping[str, Any], acts: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    from .code_task_service import review_comments_of

    evidence = _j(run.get("state"), {}) or {}
    rep_seat = evidence.get("review_seat")
    stages = _stage_acts(acts)

    def signer(stage: str) -> Optional[Mapping[str, Any]]:
        found = [a for a in stages if a.get("step_id") == stage
                 and (stage != "review_code" or not rep_seat or a.get("seat") in (None, rep_seat))]
        return found[-1] if found else None

    out = []
    for c in review_comments_of(dict(run)):
        human = str(c.get("author_kind") or "") == "human"
        act = None if human else signer(str(c.get("stage") or ""))
        role = None if human else (c.get("author_role_key") or (act or {}).get("role_key"))
        out.append({
            "id": str(c.get("id") or ""), "round": int(c.get("round") or 0) + 1,
            "severity": str(c.get("severity") or "minor"), "body": str(c.get("body") or ""),
            "path": c.get("path"), "line": c.get("line"), "end_line": c.get("end_line"),
            "author": c.get("author") if human else None,
            "author_role_key": role,
            "act_id": c.get("act_id") or (act or {}).get("act_id"),
            "stage": c.get("stage"), "status": str(c.get("status") or "open"),
        })
    return out


def _said(connection: Any, acts: list[Mapping[str, Any]]) -> dict[str, str]:
    ids = [a["act_id"] for a in _stage_acts(acts)]
    if not ids:
        return {}
    return {str(r["act_id"]): str(r["content"] or "") for r in connection.execute(
        text("SELECT DISTINCT ON (act_id) act_id, content FROM agent_turns "
             "WHERE act_id = ANY(:a) AND direction = 'completion' "
             "ORDER BY act_id, created_at DESC"),
        {"a": ids},
    ).mappings().all()}


def _story(connection: Any, project_id: str, run: Mapping[str, Any], facts: Facts,
           st: BuildState, comments: list[dict[str, Any]], plan_decision: Optional[Mapping[str, Any]],
           now: datetime, viewer: Optional[str]) -> list[dict[str, Any]]:
    from . import agent_acts

    rid = str(run["id"])
    acts = facts.acts.get(rid, [])
    fctx = _j(run.get("finalize_ctx"), {}) or {}
    evidence = _j(run.get("state"), {}) or {}
    failing = {str(g) for g in fctx.get("failing_gates") or []}
    rep_seat = evidence.get("review_seat")
    stages = _stage_acts(acts)
    excerpts = _said(connection, acts)
    rows: list[dict[str, Any]] = []
    counts: dict[tuple, int] = {}
    last_of_step = {a.get("step_id"): a["act_id"] for a in stages}
    last_eng = next((a["act_id"] for a in reversed(stages) if a.get("step_id") in ENGINEERING),
                    None)
    for a in stages:
        step = str(a.get("step_id") or "")
        seat = a.get("seat")
        counts[(step, seat)] = counts.get((step, seat), 0) + 1
        rnd = counts[(step, seat)]
        station = _station_of_stage(step)
        verdict = None
        if station == "review" and (a.get("ended_at") or st["state"] == "needs_waiver"):
            if step != "review_code" or not seat or not rep_seat or seat == rep_seat:
                verdict = _verdict(step, evidence, failing) if a["act_id"] == last_of_step.get(step) \
                    or seat else None
        word = VERDICT_WORDS.get(verdict or "", _ACT_WORD.get(str(a.get("status")), "Working"))
        files = None
        if step == "write_tests" and a["act_id"] == last_of_step.get(step):
            files = [str(f) for f in evidence.get("test_files") or []] or None
        if step in ENGINEERING and a["act_id"] == last_eng:
            files = [str(f) for f in evidence.get("impl_files") or []] or None
        said = None
        key = _SAID_KEY.get(step)
        if key and a["act_id"] == last_of_step.get(step) and (
                step != "review_code" or not rep_seat or seat == rep_seat):
            said = _trim(evidence.get(key))
        if step in ENGINEERING and a["act_id"] == last_eng:
            said = _trim(evidence.get("summary") or evidence.get("impl_summary"))
        said = said or _trim(excerpts.get(a["act_id"]))
        authored = sum(1 for c in comments if c.get("act_id") == a["act_id"])
        fixed = sum(1 for c in comments if c["status"] == "addressed") \
            if step in ENGINEERING and rnd > 1 and a["act_id"] == last_eng else 0
        wire = agent_acts.serialize(dict(a))
        rows.append({
            "id": a["act_id"], "type": "act", "station": station, "act": wire,
            "title": agent_acts.STAGE_TITLES.get(step, step.replace("_", " ").capitalize()),
            # `round n` only when the build actually repaired (D15: "round 3" with 0 rounds —
            # a stage that simply ran again, like the check stages, is not a round).
            "seat": seat, "round": rnd if st.get("rounds") else 1, "word": word,
            "verdict": verdict, "files": files,
            "tool_calls": facts.tool_calls.get(a["act_id"], 0),
            "comments_authored": authored, "comments_fixed": fixed, "said": said,
            "failure": None, "_at": _aware(a.get("started_at")) or now,
        })
    if st["state"] == "failed":
        target = next((r for r in reversed(rows)), None)
        if target is not None:
            target["failure"] = failure_line(fctx, run.get("error"))
    # The people.
    you: list[dict[str, Any]] = []
    for i, g in enumerate(list(fctx.get("applied_guidance") or [])
                          + list(fctx.get("pending_guidance") or [])):
        if not isinstance(g, dict):
            continue
        applied = g.get("applied_act_id")
        sent = _aware(g.get("at"))
        # FILED WHERE IT WAS SAID (D8): the station that was working when the guidance was
        # sent — not the one that later read it.
        then = _station_at(stages, sent) if sent else None
        if applied:
            word = "Read by " + _person_name(connection, project_id, g.get("applied_role"))
        elif st["state"] in TERMINAL_STATES:
            word = "Not read · build ended"
        else:
            word = "Queued for next station"
        you.append({"id": f"steer:{i}", "type": "you",
                    "station": then or st.get("station"),
                    "what": "steered", "actor": str(g.get("actor") or ""),
                    "text": str(g.get("text") or "")[:400], "at": _iso(g.get("at")) or "",
                    "applied_at": _iso(g.get("applied_at")), "applied_act_id": applied,
                    "applied_role": g.get("applied_role"), "word": word})
    if plan_decision and str(plan_decision.get("status") or "") in ("approved", "executed"):
        you.append({"id": "plan", "type": "you", "station": "you_plan", "what": "approved_plan",
                    "actor": str(plan_decision.get("approved_by") or ""),
                    "at": _iso(plan_decision.get("approved_at") or plan_decision.get("updated_at"))
                    or ""})
    waived = fctx.get("waived") if isinstance(fctx.get("waived"), dict) else None
    if waived:
        you.append({"id": "waived", "type": "you", "station": "review", "what": "waived",
                    "actor": str(waived.get("by") or ""), "text": waived.get("reason") or None,
                    "count": len(waived.get("gates") or []), "at": _iso(waived.get("at")) or ""})
    if fctx.get("parent_run_id"):
        origin = fctx.get("origin") if isinstance(fctx.get("origin"), dict) else {}
        you.append({"id": "retried", "type": "you", "station": "plan", "what": "retried",
                    "actor": str(origin.get("actor") or fctx.get("actor") or ""),
                    "at": _iso(run.get("created_at")) or ""})
    late = fctx.get("stop_too_late") if isinstance(fctx.get("stop_too_late"), dict) else None
    if late:
        # Stop was pressed while the build was finishing; it finished first (D1).
        you.append({"id": "stopped", "type": "you", "station": st.get("station"),
                    "what": "stopped", "actor": str(late.get("actor") or ""),
                    "word": "Too late · it had finished", "at": str(late.get("at") or "")})
    if fctx.get("cancelled_by"):
        you.append({"id": "stopped", "type": "you", "station": st.get("station"),
                    "what": "stopped", "actor": str(fctx.get("cancelled_by")),
                    "at": _iso(fctx.get("cancelled_at")) or ""})
    for y in you:
        y["_at"] = _aware(y.get("at")) or now
    # BOSS's machinery.
    boss: list[dict[str, Any]] = []
    timings = [t for t in fctx.get("phase_timings") or [] if isinstance(t, dict)]
    # THE WALL CLOCK OF BOSS'S OWN STEPS (the old Steps table): cloning, running the tests,
    # starting the app and opening the PR take minutes no agent is billed for.
    spent = phase_ms(fctx)
    cloned = next((t for t in timings if t.get("phase") == "provisioning"), None)
    if cloned or str(run.get("phase") or "") not in ("queued", "provisioning"):
        boss.append({"id": "cloned", "type": "boss", "station": "plan", "what": "cloned",
                     "text": "Cloned the repo", "at": _iso(run.get("created_at")) or "",
                     "ms": spent.get("provisioning") if cloned else None})
    tests = test_commands(evidence)
    if tests:
        check = [a for a in stages if _station_of_stage(a.get("step_id")) == "check"]
        at = (check[-1].get("ended_at") if check else None) or run.get("updated_at")
        boss.append({"id": "ran_tests", "type": "boss", "station": "check", "what": "ran_tests",
                     "text": "Ran tests", "at": _iso(at) or "", "ms": spent.get("run_tests"),
                     "detail": {"commands": [{"command": t["command"], "exit_code": t["exit_code"],
                                              "ok": t["ok"], "duration_ms": t["duration_ms"]}
                                             for t in tests]}})
    if "serve_check" in spent:
        served = evidence.get("app_serves") if isinstance(evidence.get("app_serves"), dict) else {}
        check = [a for a in stages if _station_of_stage(a.get("step_id")) == "check"]
        at = (check[-1].get("ended_at") if check else None) or run.get("updated_at")
        boss.append({"id": "serve_check", "type": "boss", "station": "check",
                     "what": "serve_check",
                     "text": "Started the app" if served.get("ok") or not served
                     else "App didn't start", "at": _iso(at) or "", "ms": spent["serve_check"],
                     "detail": {"command": served.get("command"),
                                "error": _first_line(served.get("error"), 200) or None}
                     if served else None})
    if not stages:
        # A BUILD WITH NO ACTS STILL RAN (older builds, before acts were recorded): one row
        # per phase it timed, so its Story says what happened instead of "Nothing yet".
        began = _aware(run.get("created_at"))
        offset = 0
        for i, t in enumerate(timings):
            phase, ms = str(t.get("phase") or ""), int(t.get("ms") or 0)
            if phase in _AGENT_PHASES:
                boss.append({"id": f"phase:{i}", "type": "boss",
                             "station": _station_of_stage(phase) or "build", "what": "phase",
                             "text": _AGENT_PHASES[phase],
                             "at": _iso(began + timedelta(milliseconds=offset)) if began else "",
                             "ms": ms})
            offset += ms
    n = _pr_number(run)
    if n or run.get("pull_request_url"):
        opened = connection.execute(
            text("SELECT created_at FROM events WHERE project_id = :p AND entity_id = :r "
                 "AND event_type IN ('code_task.pr_opened', 'code_task.gate_overridden') "
                 "ORDER BY created_at LIMIT 1"),
            {"p": project_id, "r": rid},
        ).scalar()
        boss.append({"id": "opened_pr", "type": "boss", "station": "pr", "what": "opened_pr",
                     "text": f"Opened PR #{n}" if n else "Opened the PR",
                     "at": _iso(opened or run.get("updated_at")) or "",
                     "ms": spent.get("opening_pr")})
    preview = facts.previews.get(rid)
    if preview:
        steps = [s for s in _j(preview.get("steps"), []) or [] if isinstance(s, dict)]
        boss.append({"id": "preview", "type": "boss", "station": "preview",
                     "what": "started_preview", "text": "Started preview",
                     "at": _iso(preview.get("created_at")) or "",
                     "detail": {"steps_done": sum(1 for s in steps
                                                  if s.get("status") in ("ok", "skipped")),
                                "steps_total": len(steps)} if steps else None})
    for d in facts.deploys.get(rid, []):
        if str(d.get("status") or "") in DEPLOY_OK:
            boss.append({"id": f"deploy:{d['id']}", "type": "boss", "station": "ship",
                         "what": "deployed", "text": f"Deployed to {d.get('environment') or 'dev'}",
                         "at": _iso(d.get("finished_at") or d.get("created_at")) or "",
                         "ms": d.get("duration_ms")})
    for b in boss:
        b["_at"] = _aware(b.get("at")) or now
    # IN THE LINE'S ORDER (D8): each station's rows together, under its head, the way the Line
    # reads left to right; time order inside a station. A row with no station is filed under
    # the station that was working when it happened.
    rank = {k: i for i, k in enumerate(STATION_ORDER)}
    for r in rows + you + boss:
        if not r.get("station"):
            r["station"] = _station_at(stages, r["_at"]) or st.get("station") or "plan"
    story = sorted(rows + you + boss,
                   key=lambda r: (rank.get(str(r.get("station")), len(rank)), r["_at"]))
    for r in story:
        r.pop("_at", None)
    return story


TERMINAL_STATES = frozenset({"cancelled", "failed", "pr_open", "merged", "deployed",
                             "done_no_pr"})


def _station_at(stages: list[Mapping[str, Any]], when: Optional[datetime]) -> Optional[str]:
    """The station whose act was working at `when` (the newest one started by then)."""
    if when is None:
        return None
    found = None
    for a in stages:
        start = _aware(a.get("started_at"))
        if start and start <= when:
            found = _station_of_stage(a.get("step_id")) or found
    return found


def _person_name(connection: Any, project_id: str, role_key: Any) -> str:
    """The agent's name as every surface says it (callsign, else display name)."""
    if not role_key:
        return "an agent"
    try:
        from .agent_acts import _agent_name

        return _agent_name(connection, project_id, str(role_key))
    except Exception:  # noqa: BLE001
        return str(role_key).replace("_", " ").title()


def _now_line(connection: Any, acts: list[Mapping[str, Any]]) -> Optional[Now]:
    open_act = _open_act(acts)
    if not open_act:
        return None
    row = connection.execute(
        text("SELECT tool_name, content, created_at FROM agent_turns WHERE act_id = :a "
             "AND direction = 'tool_call' ORDER BY created_at DESC LIMIT 1"),
        {"a": open_act["act_id"]},
    ).mappings().first()
    if not row or not row["tool_name"]:
        return None
    return {"act_id": open_act["act_id"], "role_key": open_act.get("role_key"),
            "tool": str(row["tool_name"]), "arg": _now_arg(row["tool_name"], row["content"]),
            "at": _iso(row["created_at"]) or ""}


# ── what a build's page said before the redesign, kept (parity) ──────────────────────────────
#
# THE OLD PAGE WAS READ FROM THE RUN ROW; THE COCKPIT IS READ FROM ACTS AND GATE ROWS. Every
# fact below was on the old per-build page (code-tasks-ui.tsx before the Factory redesign) and
# was dropped on the way: the brief's acceptance criteria and checks, the repo and branch, the
# workflow and its version, which engines built and reviewed it, each gate's reason, each test
# command's time, the diff's size. They are all on the run row already; these derive them.

#: Machinery phases that the Story shows as BOSS rows, with the wall clock they took.
_MACHINE_PHASES: dict[str, tuple[str, str, str]] = {
    # phase: (story id, station, sentence)
    "provisioning": ("cloned", "plan", "Cloned the repo"),
    "run_tests": ("ran_tests", "check", "Ran tests"),
    "serve_check": ("serve_check", "check", "Started the app"),
    "opening_pr": ("opened_pr", "pr", "Opened the PR"),
}
#: The agent phases, for a build whose work was never recorded as acts (older builds): one
#: BOSS row per phase so its Story is never empty while its phases have timings.
_AGENT_PHASES: dict[str, str] = {
    "design": "Planned", "write_tests": "Wrote tests", "implement": "Coded", "repair": "Repaired",
    "review_panel": "Reviewed", "review_code": "Reviewed the code",
    "security_check": "Checked security", "consistency_check": "Checked consistency",
    "check_tests": "Checked the tests",
}


def phase_ms(fctx: Mapping[str, Any]) -> dict[str, int]:
    """Wall clock per phase, summed over repeats (a repair re-runs a phase)."""
    out: dict[str, int] = {}
    for t in fctx.get("phase_timings") or []:
        if isinstance(t, dict) and t.get("phase"):
            out[str(t["phase"])] = out.get(str(t["phase"]), 0) + int(t.get("ms") or 0)
    return out


def test_commands(evidence: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The commands that ran, as the old page judged them: a command without an `ok` passed when
    it exited 0 and did not time out (older records carry only the exit code)."""
    out = []
    for t in evidence.get("tests") or []:
        if not isinstance(t, dict):
            continue
        timed_out = bool(t.get("timed_out"))
        code = t.get("exit_code")
        ok = bool(t["ok"]) if isinstance(t.get("ok"), bool) else (code == 0 and not timed_out)
        out.append({"command": str(t.get("command") or ""), "exit_code": code, "ok": ok,
                    "timed_out": timed_out,
                    "duration_ms": int(t["duration_ms"]) if isinstance(t.get("duration_ms"),
                                                                        (int, float)) else None})
    return out


def gate_reason(name: str, ok: bool, evidence: Mapping[str, Any],
                run: Optional[Mapping[str, Any]] = None) -> Optional[str]:
    """Why a gate passed or failed, in ≤ 6 words, from the run's own evidence.

    The gate row's `evidence` column is the WHOLE evidence dict, keyed by evidence names
    (`adversarial_verdict`, `tests`, `app_serves` …), never by gate names — so looking a gate up
    in it by name found nothing, and every Checks row said ✓ or ✗ with no reason. The old page
    said `exit 1`, `refuted`, `drift`, `mutation caught`. This says the same, from the same facts.
    """
    ev = evidence if isinstance(evidence, Mapping) else {}
    cmds = test_commands(ev)
    hollow = ev.get("review_hollow") if isinstance(ev.get("review_hollow"), Mapping) else {}
    seat = {"code_review_passed": "review_code", "security_clean": "security_check",
            "consistent": "consistency_check"}.get(name)
    if not ok and seat and seat in hollow:
        from .review_input import HOLLOW_GATE_REASON

        return HOLLOW_GATE_REASON
    if name == "written_tests_pass":
        bad = [c for c in cmds if not c["ok"]]
        if bad:
            c = bad[0]
            return "timed out" if c["timed_out"] else f"exit {c['exit_code']}"
        code = ev.get("test_exit_code")
        if cmds or code is not None:
            return f"exit {code if code is not None else 0}"
        return None
    if name == "code_review_passed":
        verdict = ev.get("adversarial_verdict")
        conf = ev.get("review_confidence")
        if isinstance(conf, (int, float)) and (ev.get("review_panel_size") or 0) > 1:
            return f"{verdict or 'not run'} · {round(conf * 100)}% held"
        if ok and verdict and verdict not in _GOOD_VERDICTS:
            # Graded findings: the reviewer refuted, but nothing it found met the blocking bar.
            return f"{verdict} · none blocking"
        return str(verdict) if verdict else None
    if name == "security_clean":
        return str(ev["security_verdict"]) if ev.get("security_verdict") else None
    if name == "consistent":
        return str(ev["consistency_verdict"]) if ev.get("consistency_verdict") else None
    if name == "tests_strong_enough":
        caught = ev.get("mutation_caught")
        return None if caught is None else ("mutation caught" if caught else "mutation missed")
    if name == "nothing_broke":
        good = ev.get("regressions_ok")
        return None if good is None else ("no regressions" if good else "a regression")
    if name == "existing_tests_intact":
        weak = ev.get("weakened_existing_tests") or []
        return f"{_plural(len(weak), 'test file', 'test files')} weakened" if weak else (
            None if ok else "a test was weakened")
    if name == "code_written":
        files = len(ev.get("impl_files") or []) or int(ev.get("files_changed") or 0)
        return _plural(files, "file", "files") if files else (None if ok else "no code changed")
    if name == "tests_written_first":
        n = len(ev.get("test_files") or [])
        return _plural(n, "test file", "test files") if n else (None if ok else "no tests first")
    if name == "coder_did_not_edit_tests":
        return None if ok else "coder edited tests"
    if name == "web_package_honest":
        contract = ev.get("web_package_contract") if isinstance(ev.get("web_package_contract"),
                                                               dict) else {}
        if ok or not contract:
            return None
        stubs = contract.get("stubs") or []
        missing = contract.get("missing_runners") or []
        return (f"stubs: {', '.join(map(str, stubs))[:40]}" if stubs else
                f"missing: {', '.join(map(str, missing))[:40]}" if missing else "not honest")
    if name == "app_serves":
        rec = ev.get("app_serves") if isinstance(ev.get("app_serves"), dict) else {}
        if rec.get("skipped"):
            return "skipped"
        return None if ok else (_first_line(rec.get("error"), 60) or "did not answer")
    return None


#: The gates a run's own verdicts decide, for a build with no gate row (the old page's GateRows).
_EVIDENCE_GATES: tuple[tuple[str, str], ...] = (
    ("written_tests_pass", "tests_passed"), ("code_review_passed", "adversarial_verdict"),
    ("security_clean", "security_verdict"), ("nothing_broke", "regressions_ok"),
    ("consistent", "consistency_verdict"), ("tests_strong_enough", "mutation_caught"),
)
_GOOD_VERDICTS = {"survived", "clean", "consistent", "pass"}


def _checks_from_evidence(run: Mapping[str, Any], evidence: Mapping[str, Any],
                          fctx: Mapping[str, Any], acts: list[Mapping[str, Any]],
                          waived: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    """Checks for a finished build whose gate row is missing: from the run's verdicts, the way
    the old page drew them. None when the run recorded no verdict at all."""
    failing = {str(g) for g in (fctx.get("failing_gates") or [])
               + (fctx.get("advisory_failing_gates") or [])}
    gates = []
    for name, key in _EVIDENCE_GATES:
        value = evidence.get(key)
        if value is None or value in ("skipped", "not_run"):
            continue
        ok = (value in _GOOD_VERDICTS) if isinstance(value, str) else bool(value)
        if name in failing:
            ok = False
        gates.append({"name": name, "label": gate_label(name), "ok": ok,
                      "waived_by": waived.get("by") if name in (waived.get("gates") or []) else None,
                      "role_key": gate_owner(name, list(acts)), "station": gate_station(name),
                      "evidence": gate_reason(name, ok, evidence, run),
                      "waivable": name not in UNWAIVABLE_GATES})
    if not gates:
        return None
    failed = [g for g in gates if not g["ok"]]
    advisory = bool(failed) and bool(run.get("pull_request_url")) and not waived and bool(
        fctx.get("advisory_failing_gates"))
    word = (f"All {len(gates)} passed" if not failed else
            f"{len(failed)} failed" + (" · shipped anyway" if advisory else ""))
    return {"word": word, "advisory": advisory, "gates": gates,
            "tip": "First pass: checks advise, they don't block" if advisory else None}



def brief_of(run: Mapping[str, Any]) -> dict[str, Any]:
    """What the build was asked to do and how it was built — the old page's head, contract and
    meta rows (repo, engine, model, turns, where it ran, the workflow, the review setup)."""
    spec = _j(run.get("spec"), {}) or {}
    ev = _j(run.get("state"), {}) or {}
    fctx = _j(run.get("finalize_ctx"), {}) or {}
    usage = _j(run.get("usage"), {}) or {}
    wf = fctx.get("workflow") if isinstance(fctx.get("workflow"), dict) else {}
    owner, repo = str(spec.get("owner") or ""), str(spec.get("repo") or "")
    substrate = fctx.get("execution_substrate") or ev.get("substrate")
    sandbox = fctx.get("sandbox_task_id")
    built_on = (f"micro-VM {sandbox}".strip() if substrate == "microvm" else
                "API host" if substrate in (None, "", "local", "local_subprocess", "host") else
                str(substrate))
    # WHAT RAN, NOT WHAT WAS CONFIGURED. `build_engine` is the run's base lane; the model router
    # can route a stage elsewhere (OpenRouter near a subscription's limit, an escalation), and
    # `stage_engines` / `stage_models` record where each stage actually ran.
    stage_engines = ev.get("stage_engines") if isinstance(ev.get("stage_engines"), dict) else {}
    stage_models = ev.get("stage_models") if isinstance(ev.get("stage_models"), dict) else {}
    build_stage = "implement" if "implement" in stage_engines else "solo"
    build_engine = (stage_engines.get(build_stage) or ev.get("engine")
                    or ev.get("build_engine"))
    review_engine = stage_engines.get("review_code") or ev.get("review_engine")
    build_model = stage_models.get(build_stage) or ev.get("model") or usage.get("model")
    review_model = stage_models.get("review_code") or ev.get("review_model")
    bench = fctx.get("qa_benchmark") if isinstance(fctx.get("qa_benchmark"), dict) else {}
    deferred = fctx.get("deferred_findings") or ev.get("deferred_findings") or []
    return {
        "repo": f"{owner}/{repo}" if owner and repo and owner != "_fixture" else None,
        "branch": fctx.get("work_branch") or None,
        "base_branch": spec.get("base_branch") or None,
        # The build's database decision (build_services.decide), made before any agent ran.
        "database": ({"postgres": bool(ev["build_database"].get("postgres")),
                      "mode": ev["build_database"].get("mode"),
                      "why": [str(w) for w in ev["build_database"].get("why") or []][:5]}
                     if isinstance(ev.get("build_database"), dict) else None),
        "criteria": [str(c) for c in spec.get("acceptance_criteria") or [] if str(c).strip()],
        "test_commands": [str(c) for c in spec.get("test_commands") or [] if str(c).strip()],
        "workflow": ({"name": str(wf["name"]), "version": wf.get("version"),
                      "default": wf.get("source") == "default"} if wf.get("name") else None),
        "engine": build_engine or None,
        "model": build_model or None,
        "review_engine": review_engine or None,
        "review_model": review_model or None,
        "cross_model": bool(review_engine and build_engine
                            and (review_engine, review_model) != (build_engine, build_model)),
        "turns": ev.get("turns"),
        "built_on": built_on,
        "profile": ev.get("profile") or None,
        "risk_tier": ev.get("risk_tier"),
        "reviewer_independent": ev.get("reviewer_independent"),
        "test_author_independent": ev.get("test_author_independent"),
        "qa_benchmark": {"name": str(bench.get("name") or ""), "id": bench.get("id")}
        if bench.get("name") else None,
        "deferred_findings": len(deferred) if isinstance(deferred, list) else 0,
        "advisory_failing": len(fctx.get("advisory_failing_gates") or []),
        "mutation_caught": ev.get("mutation_caught"),
        "not_run": [k for k, v in (("adversarial review", ev.get("adversarial_verdict")),
                                   ("security review", ev.get("security_verdict")),
                                   ("consistency check", ev.get("consistency_verdict")))
                    if v == "not_run"],
        "redelivered": len(fctx.get("redelivered_attempts") or []),
        "error": _first_line(run.get("error"), 300) or None,
    }


def _attempt_kind(run: Mapping[str, Any], fctx: Mapping[str, Any]) -> str:
    """What kind of attempt a build is (the old sibling list's words)."""
    title = str(run.get("title") or "")
    if re.match(r"^Follow-up:\s*deferred review findings", title, re.I) or fctx.get("is_followup"):
        return "Follow-up"
    retry = re.search(r"·\s*retry (\d+)\s*$", title, re.I)
    if retry:
        return f"Retry {retry.group(1)}"
    attempt = int(fctx.get("attempt") or 1)
    if attempt > 1:
        return f"Attempt {attempt}"
    return "First build"


def _cost(connection: Any, run_id: str, st: BuildState) -> dict[str, Any]:
    rows = connection.execute(
        text("SELECT COALESCE(a.role_key, l.role) AS role, l.stage, count(*) AS calls, "
             "COALESCE(sum(l.turns), 0) AS turns, COALESCE(sum(l.total_tokens), 0) AS tokens, "
             "COALESCE(sum(l.cost_usd_micros) FILTER (WHERE NOT l.included_in_subscription), 0) "
             "AS micros, count(*) FILTER (WHERE l.included_in_subscription) AS included "
             "FROM llm_calls l LEFT JOIN agent_acts a ON a.act_id = l.act_id "
             "WHERE l.code_task_run_id = :r GROUP BY 1, 2"),
        {"r": run_id},
    ).mappings().all()
    by_agent: dict[str, dict[str, Any]] = {}
    by_station: dict[str, float] = {}
    metered, included, calls = 0.0, 0, 0
    for r in rows:
        usd = int(r["micros"] or 0) / 1e6
        metered += usd
        included += int(r["included"] or 0)
        calls += int(r["calls"] or 0)
        key = str(r["role"] or "")
        agent = by_agent.setdefault(key, {"role_key": r["role"] or None, "turns": 0,
                                          "tokens": 0, "usd": 0.0})
        agent["turns"] += int(r["turns"] or 0)
        agent["tokens"] += int(r["tokens"] or 0)
        agent["usd"] = round(agent["usd"] + usd, 6)
        station = _station_of_stage(r["stage"])
        if station and usd > 0:
            by_station[station] = round(by_station.get(station, 0.0) + usd, 6)
    agents = sorted(by_agent.values(), key=lambda a: -a["usd"])
    named = [a for a in agents if a["role_key"]]
    if st["state"] == "budget_wait":
        word = "Paused for budget"
    elif metered > 0:
        word = f"${metered:.2f} · {_plural(len(named), 'agent', 'agents')}"
    elif calls and included == calls:
        word = "Included in plan"
    else:
        word = "Not yet"
    return {"word": word, "metered_usd": round(metered, 6), "subscription_calls": included,
            "by_agent": agents, "by_station": by_station}


def pr_review_line(fctx: Mapping[str, Any]) -> dict[str, Any]:
    """The review on the pull request, for the build page: posted (with its link), refused (in
    the words GitHub's refusal earned — a token without "Pull requests: write" is said, never
    skipped), and the state of a "Review this PR again"."""
    posted = fctx.get("pr_review_post_result") if isinstance(
        fctx.get("pr_review_post_result"), Mapping) else {}
    again = fctx.get("pr_review_again") if isinstance(fctx.get("pr_review_again"), Mapping) else {}
    rounds = len([r for r in fctx.get("review_rounds") or [] if isinstance(r, Mapping)])
    status = str(again.get("status") or "")
    if status in ("queued", "running"):
        word = "Reviewing again…"
    elif status == "failed":
        word = "Review again failed"
    elif posted.get("posted"):
        word = f"{rounds} review round{'s' if rounds != 1 else ''} on the PR" if rounds else \
            "Review posted on the PR"
    elif posted.get("reason"):
        word = "Review not posted"
    else:
        word = "No review on the PR yet"
    reason = (again.get("reason") if status == "failed" else None) or (
        None if posted.get("posted") else posted.get("reason"))
    return {"word": word, "url": posted.get("review_url") or again.get("review_url"),
            "reason": str(reason)[:400] if reason else None, "again": status or None,
            "permission": bool(posted.get("permission")), "rounds": rounds}


def _outputs(connection: Any, run: Mapping[str, Any], facts: Facts, st: BuildState,
             stations: list[Station], comments: list[dict[str, Any]],
             decision: Optional[Mapping[str, Any]], now: datetime) -> dict[str, Any]:
    rid = str(run["id"])
    acts = facts.acts.get(rid, [])
    fctx = _j(run.get("finalize_ctx"), {}) or {}
    evidence = _j(run.get("state"), {}) or {}
    role_at = {s["key"]: s.get("role_key") for s in stations}
    running = st["state"] in ("building", "queued", "quiet", "budget_wait")
    # Pull request.
    n = _pr_number(run)
    agents = len({a.get("role_key") for a in _stage_acts(acts) if a.get("role_key")})
    pr: Optional[dict[str, Any]] = None
    from . import pr_merge

    dstate = _decision_state(decision, now) if (n or run.get("pull_request_url")) else None
    merge = pr_merge.state(run, decision)
    if n or run.get("pull_request_url"):
        word = (f"#{n} · Merged" if merge == pr_merge.MERGED else
                f"#{n} · Waiting on you" if dstate == "waiting" else
                f"#{n} · Approved, not merged" if merge == pr_merge.MERGE_PENDING else
                f"#{n} · Open")
        url = run.get("pull_request_url")
        pr = {"number": n, "url": url, "files_url": f"{url}/files" if url else None,
              "decision_id": run.get("decision_id"),
              "decision_status": (decision or {}).get("status"), "made_by": agents,
              "word": word, "role_key": role_at.get("build"),
              "review": pr_review_line(fctx)}
    elif st["state"] == "done_no_pr":
        pr = {"number": None, "url": None, "made_by": agents, "role_key": role_at.get("build"),
              "word": "No PR" + (f" · {st['tip'].lower()}" if st.get("tip") else "")}
    # Checks.
    gate = facts.gates.get(rid)
    waived = fctx.get("waived") if isinstance(fctx.get("waived"), dict) else {}
    checks = None
    if gate:
        passed = [str(g) for g in _j(gate.get("gates_passed"), []) or []]
        failed = [str(g) for g in _j(gate.get("gates_failed"), []) or []]
        gate_evidence = _j(gate.get("evidence"), {}) or {}
        gates = []
        for name, ok in [(g, True) for g in passed] + [(g, False) for g in failed]:
            ev = gate_evidence.get(name)
            if not isinstance(ev, str) or not ev.strip():
                # The row's evidence is keyed by evidence names, not gate names (gate_reason).
                ev = gate_reason(name, ok, gate_evidence or evidence, run)
            gates.append({
                "name": name, "label": gate_label(name), "ok": ok,
                "waived_by": waived.get("by") if name in (waived.get("gates") or []) else None,
                "role_key": gate_owner(name, acts), "station": gate_station(name),
                "evidence": ev[:160] if isinstance(ev, str) else None,
                "waivable": name not in UNWAIVABLE_GATES,
                **({"reason": "Can't be waived: " + UNWAIVABLE_WHY[name]}
                   if name in UNWAIVABLE_GATES else {})})
        advisory = bool(failed) and bool(run.get("pull_request_url")) and not waived and bool(
            fctx.get("advisory_failing_gates"))
        word = (f"All {len(gates)} passed" if not failed else
                f"{len(failed)} failed" + (" · shipped anyway" if advisory else ""))
        checks = {"word": word, "advisory": advisory, "gates": gates,
                  "tip": "First pass: checks advise, they don't block" if advisory else None}
    elif running:
        checks = {"word": "After review", "advisory": False, "gates": []}
    else:
        checks = _checks_from_evidence(run, evidence, fctx, acts, waived)
    # Tests.
    commands = test_commands(evidence)
    tail = "\n".join(str(evidence.get("last_verify_output") or "").splitlines()[-40:]) or None
    if commands:
        bad = sum(1 for c in commands if not c["ok"])
        word = (f"{bad} of {len(commands)} failed" if bad else
                f"{len(commands)} of {_plural(len(commands), 'command', 'commands')} passed")
        tests = {"word": word, "role_key": role_at.get("tests"), "commands": commands,
                 "output_tail": tail}
    else:
        tests = {"word": "Not run", "role_key": None, "commands": [], "output_tail": None}
    # Changes.
    test_files = [{"path": str(f)} for f in evidence.get("test_files") or [] if f]
    code_files = [{"path": str(f)} for f in evidence.get("impl_files") or [] if f]
    if not test_files and not code_files:
        # A solo/inline build names its diff's paths only (the old Diff summary's list).
        code_files = [{"path": str(f)} for f in evidence.get("paths") or [] if f]
    adds, dels = evidence.get("additions"), evidence.get("deletions")
    counted = int(evidence.get("files_changed") or 0)
    changes = None
    if test_files or code_files or counted:
        url = run.get("pull_request_url")
        n_files = max(counted, len(test_files) + len(code_files))
        size = (f" · +{int(adds or 0)} −{int(dels or 0)}"
                if isinstance(adds, int) or isinstance(dels, int) else "")
        changes = {"word": _plural(n_files, "file", "files") + size,
                   "tests": test_files, "code": code_files,
                   "files_changed": n_files, "additions": adds, "deletions": dels,
                   "tests_role_key": role_at.get("tests"), "code_role_key": role_at.get("build"),
                   "diff_url": f"{url}/files" if url else None}
    # Comments.
    open_ = sum(1 for c in comments if c["status"] == "open")
    fixed = sum(1 for c in comments if c["status"] == "addressed")
    cword = f"{open_} open · {fixed} fixed" if comments else "None"
    # Preview.
    p = facts.previews.get(rid)
    preview = None
    if p:
        status = str(p.get("status") or "")
        flag = preview_flag(p, now)
        steps = [s for s in _j(p.get("steps"), []) or [] if isinstance(s, dict)]
        # LIVE ONLY WHEN IT ANSWERS (D10): preview_flag says `starting` while it boots.
        shown = flag["status"] if flag else status
        preview = {"id": str(p["id"]), "status": shown,
                   "word": {"running": "Live ↗", "starting": "Starting…",
                            "failed": "Failed"}.get(shown if flag else "", "Not running"),
                   "url": p.get("url") if flag and shown == "running" else None,
                   "expires_at": _iso(p.get("expires_at")),
                   "steps_done": sum(1 for s in steps if s.get("status") in ("ok", "skipped")),
                   "steps_total": len(steps), "error": _first_line(p.get("error")) or None,
                   # Where it runs and how far each step got (the old preview pill and ledger).
                   "substrate": {"preview_rootfs": "retained rootfs", "preview_docker": "docker",
                                 "preview_host": "host process"}.get(str(p.get("substrate") or ""))
                   if p.get("substrate") else None,
                   "steps": [{"label": str(s.get("label") or s.get("step") or ""),
                              "status": str(s.get("status") or ""),
                              "duration_ms": s.get("duration_ms"),
                              "log": _first_line(s.get("log"), 120) or None} for s in steps][:12]}
    # Deploy.
    deploys = facts.deploys.get(rid, [])
    ok = [d for d in deploys if str(d.get("status") or "") in DEPLOY_OK]
    bad = [d for d in deploys if str(d.get("status") or "") in DEPLOY_BAD]
    if ok:
        dword = " · ".join(f"{d.get('environment') or 'dev'} ✓" for d in _one_per_env(ok))
    elif bad:
        dword = f"Failed · {bad[0].get('environment') or 'dev'}"
    elif deploys:
        dword = "Deploying"
    else:
        dword = "Not set up"
    items = [{k: (_iso(v) if k.endswith("_at") else v) for k, v in d.items()
              if k not in ("run_id",)} for d in deploys[:5]]
    # Merge readiness.
    blockers = sum(1 for c in comments if c["status"] == "open" and c["severity"] == "blocker")
    if running:
        ready = {"ready": False, "word": "Building"}
    else:
        failed_checks = bool(checks and any(not g["ok"] and not g.get("waived_by")
                                            for g in checks["gates"]))
        tests_ok = not commands or all(c["ok"] for c in commands)
        n_failed = sum(1 for g in (checks or {}).get("gates") or []
                       if not g["ok"] and not g.get("waived_by"))
        # MERGED ONLY WHEN IT WAS. A deployed build is not a merged one: the OUTPUTS header said
        # "Merged" while PR #223 was open, because the build had been deployed from its branch.
        if st["state"] == "merged" or (st["state"] == "deployed" and merge == pr_merge.MERGED):
            ready = {"ready": False, "word": "Merged"}
        elif st["state"] == "deployed" and not n:
            ready = {"ready": False, "word": "Deployed"}
        elif (st["state"] == "deployed" and not failed_checks and tests_ok
              and not blockers):
            ready = {"ready": True, "word": "Deployed · ready to merge"}
        elif st["state"] == "pr_open" and not failed_checks and tests_ok and not blockers:
            ready = {"ready": True, "word": "Ready to merge"}
        elif blockers:
            ready = {"ready": False,
                     "word": f"Not ready · {_plural(blockers, 'open blocker', 'open blockers')}"}
        elif n_failed:
            # THE REASON, NOT A BARE "Not ready" (lead's ask a).
            ready = {"ready": False,
                     "word": f"Not ready · {_plural(n_failed, 'check failed', 'checks failed')}"}
        elif not tests_ok:
            ready = {"ready": False, "word": "Not ready · tests failed"}
        elif st["state"] != "pr_open":
            ready = {"ready": False, "word": "Not ready · no PR"}
        else:
            ready = {"ready": False, "word": "Not ready"}
    return {"merge_ready": ready, "pr": pr, "checks": checks, "tests": tests, "changes": changes,
            "comments": {"word": cword, "items": comments}, "preview": preview,
            "deploy": {"word": dword, "items": items}, "cost": _cost(connection, rid, st)}


def _plan_decision(connection: Any, project_id: str, run_id: str) -> Optional[dict[str, Any]]:
    row = connection.execute(
        text("SELECT id, status, approved_by, approved_at, updated_at FROM decisions "
             "WHERE project_id = :p AND recommendation->>'action' = 'resume_code_task' "
             "AND recommendation->>'run_id' = :r ORDER BY created_at DESC LIMIT 1"),
        {"p": project_id, "r": run_id},
    ).mappings().first()
    return dict(row) if row else None


def cockpit(connection: Any, settings: Any, project_id: str, run_id: str, *,
            viewer: Optional[str] = None, now: Optional[datetime] = None) -> Optional[Cockpit]:
    """One build, whole: header state, line, story, outputs (§4.5). None when not found."""
    from .code_task_service import load_code_task_run, overlay_stop_requests

    now = now or _now()
    run = load_code_task_run(connection, run_id)
    if not run or run.get("project_id") != project_id:
        return None
    overlay_stop_requests(connection, [run])
    facts = load_facts(connection, project_id, [run], gates=True, tool_calls=True)
    acts = facts.acts.get(run_id, [])
    decision = facts.decisions.get(str(run.get("decision_id") or ""))
    plan_decision = _plan_decision(connection, project_id, run_id)
    row = build_row(run, facts, now=now, viewer=viewer,
                    origin=_origin(connection, project_id, run))
    st: BuildState = {"state": row["state"], "word": row["word"], "tip": row.get("tip"),
                      "verb": row["verb"], "station": row.get("station"), "k": row["k"],
                      "n": row["n"], "rounds": row["rounds"],
                      "preview": (row.get("preview") or {}).get("status")}
    comments = _comment_rows(run, acts)
    attention = [a for a in _attention_for_builds(connection, settings, project_id, now)
                 if str((a.get("build") or {}).get("run_id")) == run_id]
    agents: dict[str, str] = {}
    for a in _stage_acts(acts):
        if a.get("role_key"):
            live = a.get("ended_at") is None
            state = "working" if live and a.get("status") == "active" else (
                "failed" if a.get("status") == "error" else
                "stopped" if a.get("status") == "stale" else
                "needs_you" if a.get("status") == "awaiting_input" else "done")
            agents[str(a["role_key"])] = state if live or agents.get(a["role_key"]) != "working" \
                else agents[a["role_key"]]
    agent_list = sorted(({"role_key": k, "state": v} for k, v in agents.items()),
                        key=lambda a: a["state"] != "working")
    ok, why = _retry_offer(run)
    live = row["state"] in ("building", "queued", "quiet", "budget_wait", "needs_plan")
    fctx = run.get("finalize_ctx") or {}
    origin = row.get("origin") or lineage_origin(connection, project_id, run)
    out: Cockpit = {
        "etag": "", "run": {**run, "attempt": row["attempt"], "root_run_id": row["root_run_id"],
                            "finalize_ctx": None, "spec": None, "state": None},
        "app": row["app"], "state": st,
        "started_at": row.get("started_at"), "station_since": row.get("station_since"),
        "cost_usd": row["cost_usd"], "metered": row["metered"], "live": live,
        "stoppable": str(run.get("status")) in ("running", "repairing", "blocked"),
        "retryable": ok, "retry_reason": why,
        "failure": failure_line(fctx, run.get("error")) if row["state"] == "failed" else None,
        "lineage": _lineage(connection, project_id, run, now, viewer),
        "origin": origin, "brief": brief_of(run),
        "agents": agent_list, "stations": row["stations"],
        "attention": attention,
        "story": _story(connection, project_id, run, facts, st, comments, plan_decision, now,
                        viewer),
        "now": _now_line(connection, acts) if live else None,
        "outputs": _outputs(connection, run, facts, st, row["stations"], comments, decision, now),
        # WHAT IT IS DOING NOW, said at the top of the page (build_detail.now_summary).
        "summary": now_summary(connection, settings, project_id, run, acts=acts,
                               gate_labels=gate_label),
        # WHY THIS MODEL (docs/product/MODEL_ROUTER.md): the router's decision per stage.
        "routes": _routes(connection, run_id),
    }
    for key in ("finalize_ctx", "spec", "state"):
        out["run"].pop(key, None)
    out["etag"] = _etag({k: v for k, v in out.items() if k != "etag"})
    return out


def _routes(connection: Any, run_id: str) -> list[dict[str, Any]]:
    """Each stage's route, newest per stage last — compact, for "Why this model"."""
    try:
        from .model_router.store import routes_for

        with connection.begin_nested():
            rows = routes_for(connection, run_id)
    except Exception:  # noqa: BLE001 — a store without the router's tables has none
        return []
    keep = ("id", "step", "ref", "mode", "tier", "classified_tier", "classifier", "confidence",
            "engine", "model", "reason", "why", "escalation", "moved_by_headroom",
            "budget_lite", "fallback", "created_at", "label", "step_label", "tier_label")
    return [{k: r.get(k) for k in keep} for r in rows]


# ── GET /v1/factory/apps (§4.6) ─────────────────────────────────────────────────────────────


class AppDeploy(TypedDict):
    env: str
    ok: bool
    word: str                      # "dev ✓" | "prod —" | "Failed · dev"


class AppRow(TypedDict, total=False):
    key: str                       # the newest run id of the app (stable while it lives)
    app: str
    builders: list[str]            # role keys of every agent that built it, newest first
    run_id: str                    # its newest build (the cockpit `↗` opens)
    app_id: Optional[str]          # finalize_ctx.app_id: App › Security addresses this
    app_key: str                   # the name's identity key: templates, secrets, links
    # What its connectors still need a person for (templates.setup_needs), or None:
    # {"providers": ["google", …], "keys": ["STRIPE_SECRET_KEY", …]} — names only.
    setup: Optional[dict[str, list[str]]]
    state: str                     # BuildStateKey of that build
    word: str
    preview: Optional[PreviewFlag]
    deploys: list[AppDeploy]
    builds: int                    # how many builds (attempts included)
    run_ids: list[str]             # every build, newest first (the row expands to these)
    pr_number: Optional[int]       # the newest attempt's PR that opened one
    pr_url: Optional[str]
    pr_state: Optional[str]        # open | merged | closed — from that PR's merge question
    cost_usd: float                # metered spend over every build of the app
    included: bool                 # it ran, all on a subscription: no metered spend ("Included")
    repo: Optional[str]
    brief: Optional[str]           # the newest build's brief (the App column's tooltip)
    # Every build as the Floor's row, newest first, each with `order` (1 = the app's first
    # build), `at`, `pr_number`, `pr_state`: the numbering is the build's place, not
    # finalize_ctx.attempt (that counts retries of ONE build, and was "Attempt 1" on every row).
    attempts: list[dict[str, Any]]
    at: Optional[str]


class Apps(TypedDict):
    project_id: str
    etag: str
    apps: list[AppRow]


def _attempt_of(run: Mapping[str, Any], *, now: datetime,
                viewer: Optional[str]) -> dict[str, Any]:
    st = build_state(run, now=now, viewer=viewer)
    fctx = run.get("finalize_ctx") if isinstance(run.get("finalize_ctx"), dict) else {}
    return {"run_id": str(run["id"]), "attempt": int((fctx or {}).get("attempt") or 1),
            "state": st["state"], "word": st["word"],
            "at": _iso(run.get("created_at"))}


APPS_RUNS_CAP = 120


def _attempt_row(run: Mapping[str, Any], facts: Facts, *, now: datetime,
                 viewer: Optional[str]) -> dict[str, Any]:
    """One attempt of an app as the Floor's row (stations, state, k of n …) plus `at`."""
    row: dict[str, Any] = dict(build_row(run, facts, now=now, viewer=viewer))
    row["at"] = _iso(run.get("created_at"))
    row["pr_number"] = _pr_number(run)
    row["pr_url"] = run.get("pull_request_url")
    row["pr_state"] = _pr_state(run, facts)
    return row


def _pr_state(run: Optional[Mapping[str, Any]], facts: Facts) -> Optional[str]:
    """open | merged | closed — merged only from a real merge (pr_merge.state: a merge sha on
    record or GitHub's state), never from an approval; None without a PR."""
    if not run or not _pr_number(run):
        return None
    from . import pr_merge

    decision = facts.decisions.get(str(run.get("decision_id") or "")) or {}
    merge = pr_merge.state(run, decision)
    # `merge_pending` (approved, not merged) and `withdrawn` (a newer attempt asks about the same
    # PR, run_decisions) are both a PR that is still open.
    return merge if merge in (pr_merge.MERGED, pr_merge.CLOSED) else "open"


def _app_key_of_name(name: str) -> str:
    from .agent_assignment import app_key

    return app_key(name)


def apps(connection: Any, project_id: str, *, viewer: Optional[str] = None,
         now: Optional[datetime] = None) -> Apps:
    """One row per app, grouped by identity (the logic of app-grouping.ts, server-side)."""
    from .code_task_service import list_code_task_runs, overlay_stop_requests

    now = now or _now()
    runs = overlay_stop_requests(
        connection, list_code_task_runs(connection, project_id, FLOOR_RUNS, exclude_internal=True))
    groups = group_into_apps(runs)
    newest = [g[0] for g in groups][:ACT_RUNS_CAP]
    # Every attempt is drawn as a compact BuildRow (D21), so every attempt's facts are read —
    # still one query per kind of fact, bounded.
    every = [r for g in groups for r in g][:APPS_RUNS_CAP]
    facts = load_facts(connection, project_id, every)
    builders_of: dict[str, list[str]] = {}
    ids = [str(r["id"]) for r in runs]
    if ids:
        for row in connection.execute(
            text("SELECT code_task_run_id, role_key FROM agent_acts WHERE project_id = :p "
                 "AND code_task_run_id = ANY(:ids) AND kind = 'stage' AND role_key IS NOT NULL "
                 "ORDER BY started_at DESC"),
            {"p": project_id, "ids": ids},
        ).mappings().all():
            builders_of.setdefault(str(row["code_task_run_id"]), []).append(str(row["role_key"]))
    # WHAT ITS CONNECTORS STILL NEED A PERSON FOR (the row's "Set up sign-in · N providers"
    # chip): names only, two queries for the whole project (templates.setup_needs).
    from ..templates.setup_needs import needs as setup_needs

    to_set_up = setup_needs(connection, project_id)
    rows: list[AppRow] = []
    for group in groups:
        head = group[0]
        rid = str(head["id"])
        if head in newest:
            b = build_row(head, facts, now=now, viewer=viewer)
            state, word, flag = b["state"], b["word"], b.get("preview")
        else:
            st = build_state(head, now=now, viewer=viewer)
            state, word, flag = st["state"], st["word"], None
        roles: list[str] = []
        for r in group:
            for role in builders_of.get(str(r["id"]), []):
                if role not in roles:
                    roles.append(role)
        deploys: list[AppDeploy] = []
        seen: set[str] = set()
        for d in facts.deploys.get(rid, []):
            env = str(d.get("environment") or "dev")
            if env in seen:
                continue
            seen.add(env)
            good = str(d.get("status") or "") in DEPLOY_OK
            deploys.append({"env": env, "ok": good,
                            "word": f"{env} ✓" if good else
                            (f"Failed · {env}" if str(d.get("status") or "") in DEPLOY_BAD
                             else f"{env} —")})
        # THE APP'S PR AND WHAT IT COST (the old card's foot): the newest attempt that opened
        # a PR, and the metered spend of every build of the app.
        with_pr = next((r for r in group if _pr_number(r)), None)
        spend = sum(float((facts.cost.get(str(r["id"])) or {}).get("metered_usd") or 0)
                    for r in group)
        spec = _j(head.get("spec"), {}) or {}
        calls = sum(int((facts.cost.get(str(r["id"])) or {}).get("calls") or 0) for r in group)
        attempts = [_attempt_row(r, facts, now=now, viewer=viewer) for r in group]
        for order, a in enumerate(reversed(attempts), start=1):
            a["order"] = order
        # THE APP'S ID (the one builds stamp: finalize_ctx.app_id), so an app-level surface —
        # App › Security — can address the app, not one of its builds.
        app_id = str((_j(head.get("finalize_ctx"), {}) or {}).get("app_id") or "") or None
        # AND ITS KEY (the name's identity key): what the app-level APIs keyed by app —
        # templates, secrets, setup guide, links — address. One row carries all three of the
        # app's handles (its newest run, its id, its key) so no surface has to guess another.
        key_of_app = _app_key_of_name(app_name(head))
        rows.append({"key": rid, "app": app_name(head), "builders": roles, "run_id": rid,
                     "app_id": app_id, "app_key": key_of_app,
                     "setup": to_set_up.get(key_of_app),
                     "pr_number": _pr_number(with_pr) if with_pr else None,
                     "pr_url": (with_pr or {}).get("pull_request_url"),
                     "pr_state": _pr_state(with_pr, facts),
                     "cost_usd": round(spend, 4) if spend > 0 else 0,
                     "included": spend <= 0 and calls > 0,
                     "brief": _trim(head.get("title"), 200),
                     "repo": f"{spec.get('owner')}/{spec.get('repo')}"
                     if spec.get("owner") and spec.get("repo") and spec.get("owner") != "_fixture"
                     else None,
                     "state": state, "word": word, "preview": flag, "deploys": deploys,
                     "builds": len(group), "run_ids": [str(r["id"]) for r in group],
                     "attempts": attempts,
                     "at": _iso(head.get("updated_at") or head.get("created_at"))})
    rank = {"needs_plan": 0, "needs_waiver": 0, "budget_refused": 0, "pr_open": 0}
    rows.sort(key=lambda r: (rank.get(r["state"], 1 if section_of(r["state"]) == "building"
                                      else 2), -(_aware(r.get("at")) or now).timestamp()))
    out: Apps = {"project_id": project_id, "etag": "", "apps": rows}
    out["etag"] = _etag([(r["run_id"], r["state"], r["word"], r["builders"], r["deploys"],
                          r["builds"], r.get("preview"), r.get("setup")) for r in rows])
    return out
