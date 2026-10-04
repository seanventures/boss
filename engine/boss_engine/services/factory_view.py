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
    "write_tests": "tests",
    "implement": "build", "solo": "build", "guest": "build", "repair": "build", "coding": "build",
    "run_tests": "check", "check_tests": "check", "run_all_tests": "check", "iac_verify": "check",
    "serve_check": "check", "validating": "check", "test_arbitration": "check",
    "review_code": "review", "security_check": "review", "consistency_check": "review",
    "regression": "review", "review_panel": "review", "convergence": "review",
    "awaiting_gate_override": "review",
    "opening_pr": "pr",
}
StationStatus = Literal["pending", "working", "needs_you", "done", "stuck", "skipped", "waiting"]
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
    word: str                      # "Done" | "Coding" | "Next" | "Stuck" | "Skipped" | "Waiting" | "Needs you"
    tip: Optional[str]             # e.g. "Lean profile skips Tests", "BOSS machinery. No agent at this station."
    started_at: Optional[str]
    ended_at: Optional[str]
    edge_label: Optional[str]      # on the edge INTO this station: "Next stage", "Assigned by Helm"
    rounds: int                    # repair rounds through this station (0 = none)
    loop_with: Optional[str]       # the other role in a repair loop (`Forge ↔ Rook ×2`)
    links: list[StationLink]       # PR / preview / per-env deploy marks


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
    building: int
    needs_you: int
    shipped_7d: int
    stopped: int
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
    word: str                      # "Done" | "Working" | "Stuck" | verdict word
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
    lineage: list[dict[str, Any]]  # [{run_id, attempt, state, word, at}]
    origin: Optional[Origin]
    agents: list[dict[str, Any]]   # [{role_key, state}] live first
    stations: list[Station]
    attention: list[dict[str, Any]]
    story: list[dict[str, Any]]    # StoryAct | StoryYou | StoryBoss, oldest first
    now: Optional[Now]
    outputs: dict[str, Any]        # see apps/web/lib/cockpit.ts CockpitOutputs


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
}
#: Gates a waiver can never open a PR past.
UNWAIVABLE_GATES = frozenset({"app_serves"})


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
