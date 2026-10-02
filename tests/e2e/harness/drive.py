"""Drive every catalogue template and every opt-in example through the real workflow runner.

THE CLAIM UNDER TEST (R5): every bundled template and example runs end to end. Before this, the
only evidence was three runs on boss-1 — two cancelled, one failed — and a test that published
the examples' specs to a fake server and asserted that NO run started. Nothing anywhere started
a workflow and watched it finish.

What "finish" means here, per run:
  * every step reaches `succeeded` (or `skipped` by the runtime's own decision), and
  * every human gate the run stops at is RECORDED — which step, which decision, what it asked —
    and then answered the way an operator would: approved, after giving a deploy step a host
    to ship to, exactly as the release gate in the console does.
A run that ends `failed`, `deadlettered` or `cancelled`, or is still moving when its time is up,
is a failure, reported with each step's own error so the reader knows what to fix without
opening a database.

WHERE GATES ARE LEFT OPEN (a live install, by default) a run that reaches a pending gate with
every earlier step green has done all it can without a human: it is reported "at an expected
human gate" and passes. It used to be polled until the 900s timeout and counted a failure —
seventeen templates, over four hours of real-model runs, all reported FAIL.

NOTHING IS ABANDONED SILENTLY. Every run the harness stops following before it ends — at a gate,
on a timer, timed out, or unreadable — is listed at the end. Timed-out and unreadable runs are
cancelled (they would keep the one worker busy under the next template); runs held at a gate on
a live install are listed with the command that cancels each, because an operator may want to
look at them first. On a stack the harness booted, everything left over is cancelled.

ONE BROKEN THING NEVER COSTS THE REST. A fixture that fails is a failed precondition in the
report; a run the harness cannot drive is that run's failure; a transient 5xx while polling is
retried (the API once answered a GET with DeadlockDetected). workflows.json is rewritten after
every run, so an interrupted two-hour live run still leaves every result it collected.

NO SILENT FALSE GREEN. A run that completes while some prompt was answered by the script's
generic default reply proves nothing about that step: its verdict is "unproven", which fails the
suite on a scripted target. Only the run's OWN prompts count: one that is positively somebody
else's (a background loop's, another run's) is a side effect (attribute_prompts). PRs and
workflow runs that appear while a run is followed, beyond the run itself, are side effects too.

The harness speaks as the OWNER (principal ceo) for everything an operator does and never
as the worker; the install's own worker advances the runs. That keeps the 403s an operator would
hit in front of the harness instead of hidden behind an all-powerful key.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from . import REPO_ROOT
from .client import Api, ApiError, owner_api, worker_api
from .discover import Example, catalogue_from_api, examples
from .target import Target

RUN_TIMEOUT_SECONDS = float(os.environ.get("BOSS_E2E_RUN_TIMEOUT", "900"))
POLL_SECONDS = 3.0
POLL_RETRIES = 5                  # consecutive failed reads of a run before giving up on it
TERMINAL = {"completed", "failed", "deadlettered", "cancelled", "compensated"}
GREEN_STEPS = {"succeeded", "skipped"}
DEPLOY_HOST_NAME = "e2e-deploy-host"
DEPLOY_HOST_ADDRESS = "deploy.e2e.test"
LEFT_OPEN = "left open (gates are not approved on this target)"

PASS, AT_GATE, UNPROVEN, FAIL = "pass", "at an expected human gate", "unproven", "fail"


@dataclass
class Gate:
    step: str
    decision_id: str
    question: str
    answered: str = ""            # "approved", LEFT_OPEN, or why it could not be


@dataclass
class Outcome:
    kind: str                     # "template" | "example"
    key: str
    run_id: Optional[str] = None
    status: str = "not started"
    passed: bool = False
    seconds: float = 0.0
    steps: list[dict[str, Any]] = field(default_factory=list)
    gates: list[Gate] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    unscripted_prompts: list[str] = field(default_factory=list)
    blocked_egress: list[str] = field(default_factory=list)
    timers: list[str] = field(default_factory=list)   # timers the harness fast-forwarded
    verdict: str = FAIL
    warnings: list[str] = field(default_factory=list)
    side_effects: list[str] = field(default_factory=list)
    abandoned: str = ""           # why the harness stopped following a run that had not ended
    # Text this run's prompts carry (its input's "E2E <key> <stamp>"): how a prompt answered by
    # the default reply is told apart from one a background loop sent meanwhile.
    markers: list[str] = field(default_factory=list)
    prompts_from_elsewhere: list[str] = field(default_factory=list)


# ----------------------------------------------------------------------------- fixtures
class Fixtures:
    """Project-level prerequisites a real operator sets up once, set up the same way — through
    the public API — so a broken setup screen's API is caught too.

    Only on a stack the harness booted. On a live install the harness creates no
    infrastructure; it reports what is missing instead (README: "What live mode writes").
    """

    def __init__(self, target: Target, api: Api, *, install_catalogue: bool = True):
        self.target = target
        self.api = api
        self.install_catalogue = install_catalogue
        self.deploy_host_id: Optional[str] = None
        self.notes: list[str] = []
        self.failed: list[str] = []   # preconditions that could not be set up, as sentences

    def prepare(self) -> None:
        """Each precondition on its own: one that fails is reported and the others still run.
        The first harness runs lost every result to one fixture's traceback."""
        if self.install_catalogue:
            self.attempt("installing the workflow catalogue", lambda: self.api.post(
                "/v1/delivery-workflows/catalogue/install",
                {"project_id": self.target.project_id, "actor": self.target.owner_principal}))
        if self.target.kind == "local":
            self.deploy_host_id = self.attempt("adding and verifying the e2e deployment host",
                                               self._deploy_host)
            self.attempt("connecting the work tracker (the Plane fake)", self._work_tracker)
            self.attempt("connecting the YouTube channel through its OAuth callback",
                         self._youtube_channel)
        else:
            self.deploy_host_id = self.attempt("reading the install's deployment hosts",
                                               self._existing_host)

    def attempt(self, what: str, step: Callable[[], Any]) -> Any:
        try:
            return step()
        except Exception as exc:  # noqa: BLE001 — a broken precondition is a finding
            self.failed.append(f"{what} failed: {_one_line(exc)}. Templates that need it will "
                               f"fail at that step.")
            return None

    def _youtube_channel(self) -> None:
        """The video example publishes to the connected channel. Connected the way an operator
        does it — start the consent, come back to the callback with a code — with Google played
        by the fakes. This is the path c6bf5e0c fixed after a tunnelled install could not
        connect at all; it is exercised on every run now."""
        import urllib.parse

        started = self.api.post("/v1/channels/youtube/connect", {
            "project_id": self.target.project_id, "actor": self.target.owner_principal})
        url = started.get("authorization_url") if isinstance(started, dict) else None
        state = (urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("state") or [""])[0] \
            if url else ""
        if not state:
            raise RuntimeError("POST /v1/channels/youtube/connect did not answer an "
                               "authorization_url carrying a state; it answered "
                               f"{json.dumps(started, default=str)[:300]}")
        callback = (f"{self.target.api_url}/v1/channels/youtube/callback?"
                    + urllib.parse.urlencode({"code": "e2e-consent-code", "state": state}))
        try:
            with urllib.request.urlopen(callback, timeout=60) as response:
                page = response.read().decode(errors="replace")
        except urllib.error.HTTPError as exc:
            page = exc.read().decode(errors="replace")
        if "Channel connected" not in page:
            text = re.sub(r"<[^>]+>", " ", re.sub(r"(?s)<style.*?</style>", "", page))
            raise RuntimeError("the OAuth callback did not say the channel is connected: "
                               f"{' '.join(text.split())[:300]}")

    def _work_tracker(self) -> None:
        """meeting-to-tickets files its tickets in the project's default tracker. Bootstrap seeds
        every provider `active` with an EMPTY configuration, so without this the example fails
        only after a human approves ticket creation. Connected through the console's own route,
        pointed at the fakes' Plane."""
        self.api.post("/v1/work-trackers/connect", {
            "project_id": self.target.project_id, "actor": self.target.owner_principal,
            "provider_key": "plane", "set_as_default": True, "test": True,
            "api_token": "e2e-plane-key",
            "configuration": {"api_base_url": f"{self.target.fakes_url}/plane",
                              "workspace_slug": "e2e", "project_id": "proj_e2e"}})

    def _existing_host(self) -> Optional[str]:
        hosts = self.api.get("/v1/deployment-hosts", project_id=self.target.project_id)
        items = hosts.get("items") if isinstance(hosts, dict) else hosts
        ready = [h for h in items or [] if h.get("status") in ("verified", "ready", "active")]
        if not ready:
            self.notes.append("This install has no verified deployment host, so every template "
                              "that ends in deploy will stop at its deploy step. Add one under "
                              "Infra → Hosts.")
            return None
        return ready[0]["id"]

    def _deploy_host(self) -> str:
        actor = self.target.owner_principal
        hosts = self.api.get("/v1/deployment-hosts", project_id=self.target.project_id)
        items = hosts.get("items") if isinstance(hosts, dict) else hosts
        host = next((h for h in items or [] if h.get("name") == DEPLOY_HOST_NAME), None)
        if host is None:
            host = self.api.post("/v1/deployment-hosts", {
                "project_id": self.target.project_id, "name": DEPLOY_HOST_NAME,
                "address": DEPLOY_HOST_ADDRESS, "ssh_user": "deploy", "ssh_port": 22,
                "actor": actor, "notes": "the e2e harness's fake host (tests/e2e/stubs/bin/ssh)"})
            host = host.get("host") or host
        host_id = host["id"]
        if host.get("status") not in ("verified", "ready", "active"):
            self.api.post(f"/v1/deployment-hosts/{host_id}/key", {"actor": actor,
                                                                    "backend": "database"})
            verified = self.api.post(f"/v1/deployment-hosts/{host_id}/verify", {"actor": actor})
            if verified.get("status") != "verified":
                raise RuntimeError("the e2e deployment host did not verify: "
                                   f"{json.dumps(verified)[:600]}")
        return host_id


# ----------------------------------------------------------------------------- inputs
def run_marker(key: str, stamp: str) -> str:
    """The words every input of one run carries, so its prompts can be recognised (RUN_MARKER)."""
    return f"E2E {key} {stamp}"


def template_input(key: str, stamp: str) -> dict[str, Any]:
    marker = run_marker(key, stamp)
    return {
        "title": marker,
        "directive": (f"End-to-end harness run of the {key} workflow ({marker}): add "
                      f"e2e_feature with answer() returning 42 and label(value) returning "
                      f"'e2e:' + value."),
        "acceptance_criteria": ["answer() returns 42", "label('x') returns 'e2e:x'"],
    }


def example_input(spec: dict[str, Any], fakes_url: Optional[str],
                  marker: str = "") -> dict[str, Any]:
    """A value for every field the example's input_schema asks for, named for what it is.
    Every free-text value carries `marker`, so the prompts it feeds can be attributed to this
    run (an address or a link cannot carry it, and does not need to)."""
    schema = spec.get("input_schema") or {}
    fields = set(schema.get("required") or []) | set((schema.get("properties") or {}).keys())
    values: dict[str, Any] = {}
    for name in sorted(fields):
        lowered = name.lower()
        if lowered in ("to", "email", "recipient") or "email" in lowered:
            values[name] = "e2e-inbox@example.test"
        elif "url" in lowered or "link" in lowered:
            media = ("clip.mp4" if "video" in lowered
                     else "logo.png" if ("asset" in lowered or "image" in lowered)
                     else "brief.txt")
            # NOT the fakes' 127.0.0.1 address: media.fetch_link refuses a link into BOSS's own
            # network, rightly. A public-looking host the egress proxy answers instead.
            values[name] = (f"https://media.e2e.test/media/{media}" if fakes_url
                            else f"https://example.com/{media}")
        elif lowered == "instructions" and "video_url" in fields:
            values[name] = ("Trim the video to its first three seconds and publish it titled "
                            "'E2E clip'.")
        elif lowered == "transcript":
            values[name] = ("Alice: we need CSV export of invoices by Friday. Bob: I will write "
                            "the exporter. Carol: I will update the docs.")
        elif lowered in ("subject", "title"):
            values[name] = "E2E harness run"
        else:
            values[name] = f"e2e {name}: CSV export of invoices"
        if marker and isinstance(values[name], str) and not (
                "@" in values[name] or values[name].startswith(("http://", "https://"))):
            values[name] = f"{values[name]} ({marker})"
    return values


# ----------------------------------------------------------------------------- observing
def _one_line(exc: BaseException) -> str:
    return " ".join(f"{type(exc).__name__}: {exc}".split())[:600]


def _transient(exc: BaseException) -> bool:
    """Worth reading again: a 5xx, a refused or reset connection, a timeout. A 4xx is not —
    the answer will not change by asking twice."""
    if isinstance(exc, ApiError):
        return exc.status >= 500
    return isinstance(exc, (urllib.error.URLError, ConnectionError, TimeoutError, OSError))


def _get_json(url: Optional[str]) -> dict[str, Any]:
    if not url:
        return {}
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return json.loads(r.read()) or {}
    except Exception:  # noqa: BLE001 — diagnostics only
        return {}


# WHOSE PROMPT WAS IT. The stub's log is one stream for the whole install, and a run is followed
# for minutes while the Captain's quality loop, a chat or a meeting may call the model too. Every
# unmatched prompt in the window used to be charged to the run being followed, so a background
# loop could mark a fully scripted run UNPROVEN and fail the suite. A prompt is charged to the
# run unless it is POSITIVELY someone else's: it carries a background loop's own words, or
# another run's marker and not this one's. A prompt that carries no marker at all is still
# charged to the run: the default reply answering one of the run's own steps must never pass.
RUN_MARKER = re.compile(r"\bE2E [A-Za-z0-9_.-]+ \d{6}\b")
BACKGROUND_PROMPTS = (
    # factory_quality_loop.py: the Captain's own code tasks, which start whenever the factory
    # is idle enough. Round two found them opening PRs in the middle of a template run.
    "QUALITY IMPROVE PASS",
)


# WHAT COUNTS AS "THIS RUN'S" WORDS. Round three passed this run's step ids as own markers, and
# step ids are ordinary words (plan, implement, review, quote): another run's "Plan the
# newsletter for E2E weekly-newsletter 101533" contains "plan", so it was charged to the run
# being followed and a fully scripted run was marked UNPROVEN. Only a run marker (RUN_MARKER's
# shape, unique per run) and the run's own id identify a run; anything else is refused here, so
# no caller can widen "own" back to a common word.
def attribute_prompts(entries: list[dict[str, Any]], markers: list[str], run_id: str = ""
                      ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(this run's, somebody else's): see the comments above."""
    loose = [m for m in markers if m and not RUN_MARKER.fullmatch(m)]
    if loose:
        raise ValueError(f"own markers must be run markers ('E2E <key> <6 digits>'), not "
                         f"{loose[:3]}: a plain word also appears in other runs' prompts")
    own_markers = [m.lower() for m in markers if m] + ([run_id.lower()] if run_id else [])
    mine: list[dict[str, Any]] = []
    elsewhere: list[dict[str, Any]] = []
    for entry in entries:
        text = str(entry.get("prompt") or entry.get("prompt_head") or "")
        lowered = text.lower()
        if any(b.lower() in lowered for b in BACKGROUND_PROMPTS):
            elsewhere.append(entry)
        elif any(m in lowered for m in own_markers):
            mine.append(entry)
        elif RUN_MARKER.search(text):
            elsewhere.append(entry)
        else:
            mine.append(entry)
    return mine, elsewhere


def _stub_log(target: Target) -> list[dict[str, Any]]:
    url = f"{target.model_stub_url}/_log" if target.model_stub_url else None
    return _get_json(url).get("entries") or []


def _fakes_log(target: Target) -> list[dict[str, Any]]:
    url = f"{target.fakes_url}/_log" if target.fakes_url else None
    return _get_json(url).get("entries") or []


def _fake_prs(target: Target) -> list[dict[str, Any]]:
    url = f"{target.fakes_url}/_world" if target.fakes_url else None
    return _get_json(url).get("prs") or []


def _run_detail(api: Api, run_id: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    body = api.get(f"/v1/workflow-runs/{run_id}", project_id=api.project_id)
    run = body.get("run", body)
    steps = body.get("steps") or run.get("steps") or []
    return run, steps


def _capability(step: dict[str, Any]) -> str:
    binding = step.get("binding_snapshot") or {}
    return str(binding.get("capability") or binding.get("custom_step") or step.get("step_kind") or "")


def _held_at_open_gate(steps: list[dict[str, Any]], open_decisions: set[str]) -> Optional[str]:
    """The step a run waits on when the ONLY thing between it and progress is a gate the
    harness deliberately left open, and everything before it is green. Else None."""
    waiting = [s for s in steps if s.get("status") not in GREEN_STEPS | {"pending"}]
    if not waiting:
        return None
    if all(s.get("status") == "waiting" and s.get("decision_id") in open_decisions
           for s in waiting):
        return ", ".join(str(s.get("step_id")) for s in waiting)
    return None


# ----------------------------------------------------------------------------- one run
class Driver:
    def __init__(self, target: Target, *, approve_gates: bool = True,
                 install_catalogue: bool = True, api: Optional[Api] = None):
        self.target = target
        self.api = api or owner_api(target)
        self.worker = worker_api(target) if api is None else None
        self.fixtures = Fixtures(target, self.api, install_catalogue=install_catalogue)
        self.approve_gates = approve_gates
        self.abandoned: list[Outcome] = []

    def start(self, workflow_key: str, run_input: dict[str, Any], outcome: Outcome) -> None:
        try:
            body = self.api.post("/v1/delivery-workflows/start", {
                "project_id": self.target.project_id, "actor": self.target.owner_principal,
                "workflow_key": workflow_key, "input": run_input,
                "dedupe_key": f"e2e-{workflow_key}-{time.time_ns()}"})
        except ApiError as exc:
            outcome.status = "refused at start"
            outcome.problems.append(str(exc))
            return
        run = body.get("run") or body
        outcome.run_id = run.get("id")
        outcome.status = run.get("status") or "started"

    def _read(self, run_id: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """The run, read again through transient failures; raises after POLL_RETRIES."""
        last: Optional[BaseException] = None
        for attempt in range(POLL_RETRIES):
            try:
                return _run_detail(self.api, run_id)
            except Exception as exc:  # noqa: BLE001 — classified below
                if not _transient(exc):
                    raise
                last = exc
                time.sleep(min(POLL_SECONDS * (attempt + 1), 15))
        assert last is not None
        raise last

    def _runs_now(self) -> set[str]:
        try:
            body = self.api.get("/v1/workflow-runs", project_id=self.target.project_id,
                                limit=200)
        except Exception:  # noqa: BLE001 — diagnostics only
            return set()
        return {str(r.get("id")) for r in (body or {}).get("items") or [] if r.get("id")}

    def follow(self, outcome: Outcome, timeout: float = RUN_TIMEOUT_SECONDS) -> None:
        assert outcome.run_id
        started = time.time()
        stub_mark = len(_stub_log(self.target))
        egress_mark = len(_fakes_log(self.target))
        prs_before = len(_fake_prs(self.target))
        runs_before = self._runs_now()
        answered: set[str] = set()
        left_open: set[str] = set()
        run: dict[str, Any] = {}
        steps: list[dict[str, Any]] = []
        held: Optional[str] = None
        unreadable: Optional[str] = None
        while time.time() - started < timeout:
            try:
                run, steps = self._read(outcome.run_id)
            except Exception as exc:  # noqa: BLE001 — persistent: this run's problem, not all
                unreadable = _one_line(exc)
                break
            if run.get("status") in TERMINAL:
                break
            for step in steps:
                decision = step.get("decision_id")
                if step.get("status") == "waiting" and decision and decision not in answered:
                    answered.add(decision)
                    gate = self._answer_gate(outcome, step, decision)
                    outcome.gates.append(gate)
                    if gate.answered == LEFT_OPEN:
                        left_open.add(decision)
                if step.get("step_kind") == "timer" and step.get("status") == "waiting":
                    waited = self._play_the_clock(outcome, step)
                    if waited:
                        outcome.timers.append(waited)
            if self._only_a_timer_is_left(steps):
                break
            held = _held_at_open_gate(steps, left_open)
            if held:
                break
            time.sleep(POLL_SECONDS)
        outcome.seconds = round(time.time() - started, 1)
        outcome.status = str(run.get("status") or "unknown")
        outcome.steps = [{"step": s.get("step_id"), "capability": _capability(s),
                          "status": s.get("status"), "attempt": s.get("attempt"),
                          "error": (str(s.get("last_error"))[:600] if s.get("last_error") else None)}
                         for s in steps]
        on_timer = self._only_a_timer_is_left(steps)
        if unreadable:
            outcome.problems.append(f"the harness could not read the run after {POLL_RETRIES} "
                                    f"tries: {unreadable}")
            outcome.abandoned = "unreadable"
        elif on_timer:
            outcome.status = "waiting on its timer (expected; not followed on a live install)"
            outcome.abandoned = "waiting on its timer"
        elif held:
            outcome.status = f"waiting at the human gate on {held} (left open on this target)"
            outcome.abandoned = f"held at the gate on {held}"
        elif outcome.status not in TERMINAL:
            moving = [f"{s['step']}={s['status']}" for s in outcome.steps
                      if s["status"] not in GREEN_STEPS]
            outcome.problems.append(
                f"still {outcome.status} after {int(timeout)}s; unfinished steps: "
                f"{', '.join(moving) or 'none'}")
            outcome.abandoned = f"timed out after {int(timeout)}s"
        overdue = [s.get("step_id") for s in steps if s.get("step_kind") == "timer"
                   and s.get("status") == "waiting"
                   and any(t.startswith(f"{s.get('step_id')}:") for t in outcome.timers)]
        for step_id in overdue:
            # Found by the first run: workflows.advance_run (which wakes an elapsed timer) is
            # only called when a gate is answered, so a timer with no gate after it never fires.
            outcome.problems.append(
                f"timer step {step_id} reached its wake time (the harness moved it to now) and "
                f"nothing woke it, so the run sat 'waiting' until the harness gave up — an "
                f"elapsed timer is only advanced when some gate is answered")
        never = [s["step"] for s in outcome.steps if s["status"] == "pending"]
        for s in outcome.steps:
            if (s["status"] not in GREEN_STEPS and s["status"] != "pending"
                    and outcome.status in TERMINAL):
                outcome.problems.append(f"step {s['step']} ({s['capability']}) ended "
                                        f"{s['status']}: {s['error'] or 'no error recorded'}")
        if never and outcome.status in TERMINAL:
            outcome.problems.append(f"never reached: {', '.join(never)}")
        run_error = str(run.get("error") or "")[:600]
        if run_error and not any(run_error[:120] in p for p in outcome.problems):
            outcome.problems.append(f"run error: {run_error}")
        unmatched = [e for e in _stub_log(self.target)[stub_mark:] if not e.get("matched")]
        unscripted, elsewhere = attribute_prompts(unmatched, outcome.markers,
                                                  run_id=str(outcome.run_id or ""))
        outcome.unscripted_prompts = sorted({e["prompt_head"][:160].replace("\n", " ")
                                             for e in unscripted})[:12]
        outcome.prompts_from_elsewhere = sorted({e["prompt_head"][:160].replace("\n", " ")
                                                 for e in elsewhere})[:12]
        for head in outcome.prompts_from_elsewhere:
            outcome.side_effects.append(f"a prompt from outside this run was answered by the "
                                        f"script's default reply: {head[:120]}")
        outcome.blocked_egress = sorted({f"{e.get('method')} {e.get('egress')}{e.get('path')}"
                                         for e in _fakes_log(self.target)[egress_mark:]
                                         if e.get("blocked")})[:12]
        self._side_effects(outcome, prs_before, runs_before)
        green_so_far = all(s["status"] in GREEN_STEPS | {"pending", "waiting"}
                           for s in outcome.steps)
        if outcome.problems:
            outcome.verdict = FAIL
        elif outcome.status == "completed" and all(s["status"] in GREEN_STEPS
                                                   for s in outcome.steps):
            outcome.verdict = PASS
        elif (on_timer or held) and green_so_far:
            outcome.verdict = AT_GATE if held else PASS
        else:
            outcome.verdict = FAIL
        if outcome.verdict in (PASS, AT_GATE) and outcome.unscripted_prompts:
            # The generic default reply answered something: the step it fed is not proven.
            outcome.verdict = UNPROVEN
            outcome.warnings.append(
                f"{len(unscripted)} prompt(s) were answered by the script's generic default, so "
                f"the steps they fed are not proven; add rules to tests/e2e/stubs/"
                f"model_script.json (curl {self.target.model_stub_url}/_log has them whole)")
        outcome.passed = outcome.verdict in (PASS, AT_GATE)
        if outcome.abandoned:
            self.abandoned.append(outcome)

    def _side_effects(self, outcome: Outcome, prs_before: int, runs_before: set[str]) -> None:
        """What appeared while this run was followed, besides the run itself. A template that
        opens one PR is doing its job; three PRs, or a 'Captain: QUALITY IMPROVE PASS' PR from a
        background loop, is something the operator would see and should be told about."""
        prs = _fake_prs(self.target)[prs_before:]
        for pr in prs:
            outcome.side_effects.append(f"opened PR #{pr.get('number')} "
                                        f"'{str(pr.get('title') or '')[:100]}'")
        if len(prs) > 1:
            outcome.warnings.append(f"{len(prs)} PRs were opened while this run was followed "
                                    f"(see side_effects); a template opens at most one")
        if runs_before:
            extra = sorted(self._runs_now() - runs_before - {str(outcome.run_id)})
            for run_id in extra:
                outcome.side_effects.append(f"another workflow run started: {run_id}")

    def _play_the_clock(self, outcome: Outcome, step: dict[str, Any]) -> Optional[str]:
        """A timer step (deprecation_sunset's 7-day grace) is a WAIT, not a gate. On a stack the
        harness booted it moves the step's own `run_after` to now — the runner then fires it
        exactly as it would in a week. On a live install it never touches the database; the run
        is reported as waiting on its timer, which is the expected state, not a failure."""
        key = f"{step.get('step_id')}: was due {step.get('run_after')}"
        if any(t.startswith(f"{step.get('step_id')}:") for t in outcome.timers) \
                or not step.get("run_after"):
            return None
        if not self.target.database_url:
            return None
        import psycopg

        url = self.target.database_url.replace("postgresql+psycopg", "postgresql")
        with psycopg.connect(url) as conn:
            conn.execute("UPDATE workflow_step_runs SET run_after = now() "
                         "WHERE id = %s AND status = 'waiting' AND run_after > now()",
                         (step["id"],))
        return key

    def _only_a_timer_is_left(self, steps: list[dict[str, Any]]) -> bool:
        """On a live install: stop following a run whose only open step is a far-off timer."""
        if self.target.database_url:
            return False
        open_steps = [s for s in steps if s.get("status") not in GREEN_STEPS | {"pending"}]
        return bool(open_steps) and all(s.get("step_kind") == "timer" and s.get("status") == "waiting"
                                        for s in open_steps)

    def _answer_gate(self, outcome: Outcome, step: dict[str, Any], decision: str) -> Gate:
        try:
            card = self.api.get(f"/v1/decisions/{decision}", project_id=self.target.project_id)
        except ApiError as exc:
            return Gate(step.get("step_id") or "?", decision, "", f"could not read: {exc}")
        gate = Gate(step.get("step_id") or "?", decision, str(card.get("question") or ""))
        status = str(card.get("status") or "")
        if status and status != "pending":
            # The product answered it first — spec_review adopts the panel's PRD decision, which
            # BOSS_MEETING_PRD_AUTO_APPROVE approves on its own. Recorded, not re-answered.
            gate.answered = f"already {status} by {card.get('approved_by') or 'the product'}"
            return gate
        if not self.approve_gates:
            gate.answered = LEFT_OPEN
            return gate
        if _capability(step) == "deploy" and self.fixtures.deploy_host_id:
            # What the release gate's host picker does: give this run somewhere to ship to.
            try:
                self.api.post(f"/v1/delivery-workflows/runs/{outcome.run_id}/deploy-target", {
                    "project_id": self.target.project_id, "actor": self.target.owner_principal,
                    "host_id": self.fixtures.deploy_host_id, "environment": "staging"})
            except ApiError as exc:
                outcome.problems.append(f"choosing a deploy host for {gate.step} failed: {exc}")
        try:
            self.api.post(f"/v1/decisions/{decision}/approve",
                          {"actor": self.target.owner_principal, "note": "approved by the e2e harness"})
            gate.answered = "approved"
        except ApiError as exc:
            if exc.status == 409 and "already" in str(exc):
                gate.answered = "already answered by the product (between read and press)"
                return gate
            gate.answered = f"approve refused: {exc}"
            outcome.problems.append(f"approving the {gate.step} gate failed: {exc}")
        return gate

    # ------------------------------------------------------------------ abandoned runs
    def cancel_command(self, run_id: str) -> str:
        body = json.dumps({"project_id": self.target.project_id,
                           "actor": self.target.owner_principal,
                           "reason": "abandoned by the e2e harness", "compensate": False})
        return (f"curl -sS -X POST -H 'Authorization: Bearer $BOSS_E2E_OWNER_KEY' -H "
                f"'Content-Type: application/json' -d '{body}' "
                f"{self.target.api_url}/v1/workflow-runs/{run_id}/cancel")

    def settle_abandoned(self) -> list[str]:
        """Cancel what must not keep running; list what an operator may want to look at.
        Returns one sentence per abandoned run."""
        lines: list[str] = []
        for outcome in self.abandoned:
            keep = (self.target.kind == "live"
                    and outcome.abandoned.startswith(("held at the gate", "waiting on its timer")))
            if keep:
                lines.append(f"{outcome.kind} {outcome.key}: run {outcome.run_id} is "
                             f"{outcome.abandoned} and was left for you to look at. Cancel it "
                             f"with: {self.cancel_command(str(outcome.run_id))}")
                continue
            try:
                self.api.post(f"/v1/workflow-runs/{outcome.run_id}/cancel", {
                    "project_id": self.target.project_id, "actor": self.target.owner_principal,
                    "reason": f"abandoned by the e2e harness: {outcome.abandoned}",
                    "compensate": False})
                lines.append(f"{outcome.kind} {outcome.key}: run {outcome.run_id} was "
                             f"{outcome.abandoned}; cancelled")
            except Exception as exc:  # noqa: BLE001 — say so and give the command
                lines.append(f"{outcome.kind} {outcome.key}: run {outcome.run_id} was "
                             f"{outcome.abandoned} and could NOT be cancelled ({_one_line(exc)}). "
                             f"Cancel it with: {self.cancel_command(str(outcome.run_id))}")
        return lines

    # ------------------------------------------------------------------ templates & examples
    def run_template(self, item: dict[str, Any], stamp: str) -> Outcome:
        outcome = Outcome("template", item["key"], markers=[run_marker(item["key"], stamp)])
        self.start(item["key"], template_input(item["key"], stamp), outcome)
        if outcome.run_id:
            self.follow(outcome)
        return outcome

    def install_example(self, example: Example) -> Optional[str]:
        """Run the example's own setup.py against the target, exactly as its README says."""
        env = {**os.environ, "BOSS_API": self.target.api_url, "BOSS_API_KEY": self.target.owner_key,
               "BOSS_PROJECT": self.target.project_id, "BOSS_ACTOR": self.target.owner_principal}
        try:
            done = subprocess.run([sys.executable, str(example.setup)], cwd=str(REPO_ROOT),
                                  env=env, capture_output=True, text=True, timeout=300)
        except subprocess.TimeoutExpired:
            return f"examples/{example.directory}/setup.py did not finish within 300s"
        if done.returncode != 0:
            return (f"examples/{example.directory}/setup.py exited {done.returncode}: "
                    f"{(done.stderr or done.stdout)[-1500:]}")
        return None

    def run_example(self, example: Example) -> Outcome:
        outcome = Outcome("example", example.directory,
                          markers=[run_marker(example.directory, time.strftime("%H%M%S"))])
        failure = self.install_example(example)
        if failure:
            outcome.status = "setup failed"
            outcome.problems.append(failure)
            return outcome
        try:
            spec = self.api.get(f"/v1/delivery-workflows/{example.workflow_key}/spec",
                                project_id=self.target.project_id)
        except ApiError as exc:
            outcome.status = "not published"
            outcome.problems.append(f"setup.py succeeded but {example.workflow_key} has no "
                                    f"published spec: {exc}")
            return outcome
        spec_body = spec.get("spec") if isinstance(spec.get("spec"), dict) else spec
        self.start(example.workflow_key,
                   example_input(spec_body, self.target.fakes_url, outcome.markers[0]), outcome)
        if outcome.run_id:
            self.follow(outcome)
        return outcome

    def contained(self, kind: str, key: str, drive: Callable[[], Outcome],
                  report_dir: Path) -> Outcome:
        """One run, whatever happens inside it: an exception is THIS run's failure, with the
        traceback in a file, never the end of the suite."""
        try:
            return drive()
        except Exception as exc:  # noqa: BLE001
            log = report_dir / "harness-errors.log"
            with open(log, "a") as handle:
                handle.write(f"--- {kind} {key}\n{traceback.format_exc()}\n")
            outcome = Outcome(kind, key, status="harness error")
            outcome.problems.append(f"the harness could not drive this {kind}: {_one_line(exc)} "
                                    f"(traceback in {log})")
            return outcome


# ----------------------------------------------------------------------------- the suite
def _preflight(target: Target, api: Api) -> list[str]:
    """Is this target what boss-1 is: auth on, owner key bound to its principal?"""
    problems: list[str] = []
    anonymous = Api(target.api_url, None, "anonymous", target.project_id)
    try:
        anonymous.get("/v1/edition", project_id=target.project_id)
        problems.append("an unauthenticated request to /v1/edition succeeded: transport auth is "
                        "OFF on this target, so it cannot reproduce what an installed BOSS does")
    except ApiError as exc:
        if exc.status not in (401, 403):
            problems.append(f"unauthenticated /v1/edition answered {exc.status}, not 401")
    except Exception as exc:  # noqa: BLE001
        problems.append(f"the API at {target.api_url} did not answer: {_one_line(exc)}")
        return problems
    try:
        api.get("/v1/edition", project_id=target.project_id)
    except Exception as exc:  # noqa: BLE001
        problems.append(f"the owner key was refused by /v1/edition ({_one_line(exc)}); check "
                        f"BOSS_E2E_OWNER_KEY")
    return problems


def _principal_findings(target: Target, api: Api, worker: Optional[Api]) -> list[str]:
    """Who may advance a run. Reported, not failed: the owner key being refused here is the
    "Advance now answers 403" defect on boss-1. The console walk's Advance-now journey is what
    fails on it (console.py); this line explains why."""
    findings: list[str] = []
    body = {"project_id": target.project_id}
    try:
        api.post("/v1/delivery-workflows/run-capabilities", body)
        findings.append("the owner key (console) may advance runs directly")
    except ApiError as exc:
        findings.append(f"the owner key (console) is refused by run-capabilities with "
                        f"{exc.status}: 'Advance now' in the console cannot work on this install")
    except Exception as exc:  # noqa: BLE001
        findings.append(f"run-capabilities could not be asked: {_one_line(exc)}")
    if worker is not None:
        try:
            worker.post("/v1/delivery-workflows/run-capabilities", body)
        except ApiError as exc:
            findings.append(f"the WORKER key is refused by run-capabilities ({exc.status}): runs "
                            f"on this install cannot advance at all")
        except Exception as exc:  # noqa: BLE001
            findings.append(f"run-capabilities could not be asked as the worker: {_one_line(exc)}")
    return findings


class Report:
    """workflows.json, rewritten after every outcome so an interrupted run keeps its results."""

    def __init__(self, path: Path, target: Target):
        self.path = path
        self.target = target
        self.fixture_notes: list[str] = []
        self.fixture_failures: list[str] = []
        self.auth_findings: list[str] = []
        self.outcomes: list[Outcome] = []
        self.abandoned: list[str] = []
        self.finished = False

    def write(self) -> None:
        body = {"target": self.target.describe(), "finished": self.finished,
                "fixture_notes": self.fixture_notes, "fixture_failures": self.fixture_failures,
                "auth_findings": self.auth_findings, "abandoned_runs": self.abandoned,
                "outcomes": [asdict(o) for o in self.outcomes]}
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(body, indent=2, default=str))
        tmp.replace(self.path)


def run_all(target: Target, report_dir: Path, *, only: list[str] | None = None,
            real_models_ok: bool = False, with_examples: bool = True,
            install_catalogue: bool = True) -> int:
    if target.real_models and not real_models_ok:
        print("e2e: this target runs REAL models and will spend. Re-run with --real-models to "
              "accept that.", file=sys.stderr)
        return 2
    driver = Driver(target, approve_gates=target.kind == "local"
                    or os.environ.get("BOSS_E2E_APPROVE_GATES") == "1",
                    install_catalogue=install_catalogue)
    problems = _preflight(target, driver.api)
    if problems:
        for p in problems:
            print(f"PREFLIGHT FAIL  {p}")
        return 1
    report = Report(report_dir / "workflows.json", target)
    report.auth_findings = _principal_findings(target, driver.api, driver.worker)
    for finding in report.auth_findings:
        print(f"auth: {finding}")
    driver.fixtures.prepare()
    report.fixture_notes = driver.fixtures.notes
    report.fixture_failures = driver.fixtures.failed
    for failure in driver.fixtures.failed:
        print(f"PRECONDITION FAIL  {failure}")
    report.write()
    stamp = time.strftime("%H%M%S")
    wanted = set(only or [])
    try:
        catalogue = catalogue_from_api(driver.api)
    except Exception as exc:  # noqa: BLE001
        catalogue = []
        report.fixture_failures.append(f"reading the catalogue failed: {_one_line(exc)}; no "
                                       f"template was driven")
        print(f"PRECONDITION FAIL  {report.fixture_failures[-1]}")
    templates = [t for t in catalogue if not wanted or t["key"] in wanted]
    chosen_examples = [e for e in (examples() if with_examples else [])
                       if not wanted or e.directory in wanted or e.workflow_key in wanted]
    try:
        for item in templates:
            print(f"→ template {item['key']}", flush=True)
            report.outcomes.append(driver.contained(
                "template", item["key"], lambda item=item: driver.run_template(item, stamp),
                report_dir))
            _print_outcome(report.outcomes[-1])
            report.write()
        for example in chosen_examples:
            print(f"→ example {example.directory}", flush=True)
            report.outcomes.append(driver.contained(
                "example", example.directory, lambda example=example: driver.run_example(example),
                report_dir))
            _print_outcome(report.outcomes[-1])
            report.write()
    finally:
        # Even on Ctrl-C: nothing the harness started keeps running unannounced.
        report.abandoned = driver.settle_abandoned()
        report.write()
    report.finished = True
    report.write()
    failed = [o for o in report.outcomes if not o.passed]
    by_verdict: dict[str, int] = {}
    for o in report.outcomes:
        by_verdict[o.verdict] = by_verdict.get(o.verdict, 0) + 1
    summary = ", ".join(f"{n} {v}" for v, n in sorted(by_verdict.items()))
    print(f"\nworkflows: {len(report.outcomes) - len(failed)} of {len(report.outcomes)} passed "
          f"({summary or 'nothing driven'}); report {report.path}")
    for note in driver.fixtures.notes:
        print(f"note: {note}")
    for line in report.abandoned:
        print(f"abandoned: {line}")
    return 1 if failed or not report.outcomes or report.fixture_failures else 0


def _print_outcome(o: Outcome) -> None:
    mark = {PASS: "PASS", AT_GATE: "GATE", UNPROVEN: "UNPROVEN"}.get(o.verdict, "FAIL")
    gates = ", ".join(f"{g.step}:{g.answered}" for g in o.gates) or "no gates"
    print(f"  {mark} {o.kind} {o.key} — {o.status} in {o.seconds}s ({gates}) run {o.run_id}")
    for timer in o.timers:
        print(f"      timer fast-forwarded: {timer}")
    for problem in o.problems:
        print(f"      • {problem}")
    for warning in o.warnings:
        print(f"      ! {warning}")
    if o.unscripted_prompts:
        print(f"      unscripted prompts: {o.unscripted_prompts[:4]}")
    for effect in o.side_effects[:6]:
        print(f"      side effect: {effect}")
    if o.blocked_egress:
        print(f"      blocked egress: {o.blocked_egress[:4]}")
