"""The workflow driver reports every run honestly, however the run or the harness misbehaves.

The incidents, from the first review of the harness:
  * live mode left gates open by design, then polled each gated run until the 900s timeout and
    counted it a FAILURE — seventeen templates, over four hours of real-model spend, all red —
    and left every one of those runs waiting on boss-1 with nothing to say so;
  * one 500 on GET /v1/workflow-runs/{id} (the API once answered DeadlockDetected), or one
    fixture whose response changed shape, ended the whole suite in a traceback, and
    workflows.json — written only after the last run — was never written;
  * a run whose code-agent prompt was answered by the script's generic default reply was
    reported PASS.
These tests drive the real Driver against a scripted fake API: no network, no database.
"""
from __future__ import annotations

import json
import urllib.error
from typing import Any

import pytest

from e2e.harness import drive
from e2e.harness.client import ApiError
from e2e.harness.target import Target


def _target(kind: str = "live") -> Target:
    return Target(kind=kind, api_url="http://boss.test", owner_key="k", worker_key=None)


class FakeApi:
    """Answers GET /v1/workflow-runs/{id} from a script of (run, steps) or exceptions."""

    project_id = "boss-dogfood"

    def __init__(self, reads: list[Any], decisions: dict[str, dict] | None = None,
                 fail_post: dict[str, Exception] | None = None):
        self.reads = list(reads)
        self.decisions = decisions or {}
        self.fail_post = fail_post or {}
        self.posts: list[tuple[str, Any]] = []

    def get(self, path: str, **_: Any) -> Any:
        if path.startswith("/v1/workflow-runs/"):
            item = self.reads.pop(0) if len(self.reads) > 1 else self.reads[0]
            if isinstance(item, Exception):
                raise item
            run, steps = item
            return {"run": run, "steps": steps}
        if path == "/v1/workflow-runs":
            return {"items": []}
        if path.startswith("/v1/decisions/"):
            return self.decisions[path.rsplit("/", 1)[1]]
        raise KeyError(path)

    def post(self, path: str, body: Any = None, **_: Any) -> Any:
        self.posts.append((path, body))
        for prefix, exc in self.fail_post.items():
            if path.startswith(prefix):
                raise exc
        if path == "/v1/delivery-workflows/start":
            return {"run": {"id": f"run-{body['workflow_key']}", "status": "running"}}
        return {}


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(drive, "POLL_SECONDS", 0.01)
    monkeypatch.setattr(drive, "_stub_log", lambda target: [])
    monkeypatch.setattr(drive, "_fakes_log", lambda target: [])
    monkeypatch.setattr(drive, "_fake_prs", lambda target: [])


GREEN = {"step_id": "plan", "status": "succeeded", "step_kind": "capability"}
GATE = {"step_id": "approve_release", "status": "waiting", "decision_id": "dec-1",
        "step_kind": "approval"}
LATER = {"step_id": "deploy", "status": "pending", "step_kind": "capability"}


def test_a_gate_left_open_on_a_live_install_is_an_expected_stop_not_a_timeout():
    api = FakeApi([({"status": "running"}, [GREEN, GATE, LATER])],
                  decisions={"dec-1": {"status": "pending", "question": "Ship it?"}})
    driver = drive.Driver(_target("live"), approve_gates=False, api=api)
    outcome = drive.Outcome("template", "fast_fix", run_id="r1")
    driver.follow(outcome, timeout=30)
    assert outcome.seconds < 5, "it stopped at the gate instead of polling to the timeout"
    assert outcome.verdict == drive.AT_GATE and outcome.passed
    assert outcome.gates[0].answered == drive.LEFT_OPEN
    assert "approve_release" in outcome.status and not outcome.problems
    assert not any(path.endswith("/approve") for path, _ in api.posts)


def test_a_gate_reached_after_a_red_step_is_still_a_failure():
    red = {**GREEN, "status": "failed", "last_error": "boom"}
    api = FakeApi([({"status": "running"}, [red, GATE])],
                  decisions={"dec-1": {"status": "pending"}})
    driver = drive.Driver(_target("live"), approve_gates=False, api=api)
    outcome = drive.Outcome("template", "x", run_id="r1")
    driver.follow(outcome, timeout=2)
    assert not outcome.passed and outcome.verdict == drive.FAIL


def test_runs_the_harness_stops_following_are_cancelled_or_listed_with_the_command():
    held = FakeApi([({"status": "running"}, [GREEN, GATE])],
                   decisions={"dec-1": {"status": "pending"}})
    driver = drive.Driver(_target("live"), approve_gates=False, api=held)
    gated = drive.Outcome("template", "gated", run_id="r-gate")
    driver.follow(gated, timeout=30)
    stuck = drive.Outcome("template", "stuck", run_id="r-stuck")
    held.reads = [({"status": "running"}, [{**GREEN, "status": "running"}])]
    driver.follow(stuck, timeout=0.3)
    assert "still running" in stuck.problems[0] and stuck.abandoned.startswith("timed out")
    lines = driver.settle_abandoned()
    cancelled = [path for path, _ in held.posts if path.endswith("/cancel")]
    assert cancelled == ["/v1/workflow-runs/r-stuck/cancel"], \
        "a timed-out run is cancelled; a run held at a gate on a live install is left"
    gate_line = next(line for line in lines if "r-gate" in line)
    assert "/v1/workflow-runs/r-gate/cancel" in gate_line and "curl" in gate_line
    assert any("r-stuck" in line and "cancelled" in line for line in lines)


def test_on_a_stack_the_harness_booted_every_abandoned_run_is_cancelled():
    api = FakeApi([({"status": "running"}, [GREEN, GATE])],
                  decisions={"dec-1": {"status": "pending"}})
    driver = drive.Driver(_target("local"), approve_gates=False, api=api)
    driver.follow(drive.Outcome("template", "g", run_id="r-gate"), timeout=30)
    driver.settle_abandoned()
    assert ("/v1/workflow-runs/r-gate/cancel" in [p for p, _ in api.posts])


def test_a_run_that_cannot_be_cancelled_says_how_to_cancel_it():
    api = FakeApi([({"status": "running"}, [{**GREEN, "status": "running"}])],
                  fail_post={"/v1/workflow-runs/": ApiError("POST", "/cancel", 403, "nope")})
    driver = drive.Driver(_target("local"), api=api)
    driver.follow(drive.Outcome("template", "t", run_id="r9"), timeout=0.2)
    [line] = driver.settle_abandoned()
    assert "could NOT be cancelled" in line and "/v1/workflow-runs/r9/cancel" in line


def test_a_transient_5xx_while_polling_is_read_again():
    done = ({"status": "completed"}, [GREEN])
    api = FakeApi([ApiError("GET", "/v1/workflow-runs/r1", 500, "DeadlockDetected"),
                   urllib.error.URLError("connection reset"), done])
    driver = drive.Driver(_target("live"), api=api)
    outcome = drive.Outcome("template", "t", run_id="r1")
    driver.follow(outcome, timeout=30)
    assert outcome.verdict == drive.PASS, outcome.problems


def test_a_run_that_stays_unreadable_is_that_runs_failure_not_the_suites():
    api = FakeApi([ApiError("GET", "/v1/workflow-runs/r1", 503, "down")])
    driver = drive.Driver(_target("live"), api=api)
    outcome = drive.Outcome("template", "t", run_id="r1")
    driver.follow(outcome, timeout=60)
    assert not outcome.passed and "could not read the run" in outcome.problems[0]
    assert outcome.abandoned == "unreadable"


def test_a_completed_run_fed_by_the_default_reply_is_unproven(monkeypatch):
    log = [{"matched": True, "prompt_head": "earlier"}]
    monkeypatch.setattr(drive, "_stub_log", lambda target: log)
    api = FakeApi([({"status": "completed"}, [GREEN])])
    target = _target("local")
    target.model_stub_url = "http://stub.test"
    driver = drive.Driver(target, api=api)
    outcome = drive.Outcome("template", "fast_fix", run_id="r1")
    original = driver._read

    def read(run_id):   # the code agent's prompt arrives while the run is followed
        log[1:] = [{"matched": False,
                    "prompt_head": "You are a senior software engineer working"}]
        return original(run_id)

    driver._read = read
    driver.follow(outcome, timeout=5)
    assert outcome.verdict == drive.UNPROVEN and not outcome.passed
    assert "senior software engineer" in outcome.unscripted_prompts[0]
    assert "model_script.json" in outcome.warnings[0]


def _follow_with_prompts(monkeypatch, arriving: list[dict]) -> drive.Outcome:
    log: list[dict] = []
    monkeypatch.setattr(drive, "_stub_log", lambda target: log)
    api = FakeApi([({"status": "completed"}, [GREEN])])
    target = _target("local")
    target.model_stub_url = "http://stub.test"
    driver = drive.Driver(target, api=api)
    outcome = drive.Outcome("template", "fast_fix", run_id="r1",
                            markers=[drive.run_marker("fast_fix", "101500")])
    original = driver._read

    def read(run_id):
        log[:] = arriving
        return original(run_id)

    driver._read = read
    driver.follow(outcome, timeout=5)
    return outcome


def test_a_background_loops_unscripted_prompt_does_not_make_a_scripted_run_unproven(monkeypatch):
    """Round-two review: every unmatched prompt in the window was charged to the run being
    followed, so the Captain's quality loop (or another run) calling the model meanwhile marked
    a fully scripted run UNPROVEN. Positively foreign prompts are side effects now."""
    outcome = _follow_with_prompts(monkeypatch, [
        {"matched": False, "prompt_head": "Captain: QUALITY IMPROVE PASS on boss-e2e/app"},
        {"matched": False, "prompt_head": "Write the newsletter for E2E weekly-newsletter 101533"},
    ])
    assert outcome.verdict == drive.PASS and outcome.passed
    assert outcome.unscripted_prompts == []
    assert len(outcome.prompts_from_elsewhere) == 2
    assert sum("from outside this run" in e for e in outcome.side_effects) == 2


def test_a_prompt_carrying_this_runs_marker_or_no_marker_still_counts_against_it(monkeypatch):
    """The other half: attribution must never turn the default reply answering one of the run's
    own steps into a pass. Its own marker counts, and so does a prompt with no marker at all."""
    outcome = _follow_with_prompts(monkeypatch, [
        {"matched": False, "prompt_head": "Plan the change for E2E fast_fix 101500"},
        {"matched": False, "prompt_head": "You are a senior software engineer working"},
        {"matched": False, "prompt_head": "Captain: QUALITY IMPROVE PASS"},
    ])
    assert outcome.verdict == drive.UNPROVEN and not outcome.passed
    assert len(outcome.unscripted_prompts) == 2
    assert outcome.prompts_from_elsewhere == ["Captain: QUALITY IMPROVE PASS"]


def test_another_runs_prompt_is_not_charged_to_this_run_for_sharing_a_step_name(monkeypatch):
    """Round-three review: the driver passed this run's step ids as its own markers, and step
    ids are ordinary words. Another run's "Plan the newsletter for E2E weekly-newsletter 101533"
    contains "plan" — this run's step — so it was charged here and a fully scripted run was
    marked UNPROVEN. Only the run marker and the run id are this run's words now."""
    outcome = _follow_with_prompts(monkeypatch, [
        {"matched": False, "prompt_head": "Plan the newsletter for E2E weekly-newsletter 101533"},
    ])
    assert outcome.steps and outcome.steps[0]["step"] == "plan"   # the shared word is real
    assert outcome.verdict == drive.PASS and outcome.unscripted_prompts == []
    assert len(outcome.prompts_from_elsewhere) == 1
    mine, elsewhere = drive.attribute_prompts(
        [{"prompt_head": "Plan the newsletter for E2E weekly-newsletter 101533"},
         {"prompt_head": "Resume run wfr_abc at plan"}],
        [drive.run_marker("fast_fix", "101500")], run_id="wfr_abc")
    assert [e["prompt_head"][:6] for e in mine] == ["Resume"]
    assert len(elsewhere) == 1


def test_only_a_run_marker_can_be_named_as_a_runs_own_words():
    """The same incident, held centrally: a caller cannot widen 'own' back to a common word."""
    with pytest.raises(ValueError, match="run markers"):
        drive.attribute_prompts([], [drive.run_marker("fast_fix", "101500"), "plan"])


def test_every_template_and_example_gets_a_marker_attribution_recognises():
    """A key or example directory RUN_MARKER cannot match would make every one of that run's
    prompts look unmarked — enumerated from the code, not from a list."""
    from e2e.harness.discover import catalogue_keys_from_source, examples

    names = [*catalogue_keys_from_source(), *(e.directory for e in examples())]
    assert names
    bad = [n for n in names if not drive.RUN_MARKER.fullmatch(drive.run_marker(n, "101500"))]
    assert not bad, bad


def test_every_free_text_input_a_run_is_given_carries_its_marker():
    """Attribution only works if the run's prompts can carry the marker: every input that is
    text, not an address or a link, gets it — for templates and for examples alike."""
    marker = drive.run_marker("weekly-newsletter", "101500")
    spec = {"input_schema": {"required": ["topic", "to", "brief_url", "subject"]}}
    values = drive.example_input(spec, "http://fakes.test", marker)
    assert marker in values["topic"] and marker in values["subject"]
    assert values["to"] == "e2e-inbox@example.test" and marker not in values["brief_url"]
    template = drive.template_input("fast_fix", "101500")
    assert drive.run_marker("fast_fix", "101500") in template["title"]
    assert drive.run_marker("fast_fix", "101500") in template["directive"]
    assert drive.RUN_MARKER.search(marker)


def test_extra_prs_opened_while_a_run_is_followed_are_reported(monkeypatch):
    prs: list[dict] = [{"number": 1, "title": "older"}]
    monkeypatch.setattr(drive, "_fake_prs", lambda target: list(prs))
    api = FakeApi([({"status": "completed"}, [GREEN])])
    driver = drive.Driver(_target("local"), api=api)
    outcome = drive.Outcome("template", "fast_fix", run_id="r1")
    original = driver._read

    def read(run_id):
        prs[1:] = [{"number": 2, "title": "E2E fast_fix"},
                   {"number": 3, "title": "Captain: QUALITY IMPROVE PASS"}]
        return original(run_id)

    driver._read = read
    driver.follow(outcome, timeout=5)
    assert any("#3" in e and "QUALITY IMPROVE" in e for e in outcome.side_effects)
    assert any("2 PRs" in w for w in outcome.warnings)


def test_a_broken_fixture_and_a_crashing_run_still_leave_a_full_report(tmp_path, monkeypatch):
    class SuiteApi(FakeApi):
        def get(self, path, **query):
            if path == "/v1/delivery-workflows/catalogue":
                return {"items": [{"key": "crashes"}, {"key": "works"}]}
            if path == "/v1/deployment-hosts":
                return {"unexpected": "shape"}       # _deploy_host: KeyError on 'id'
            return super().get(path, **query)

        def post(self, path, body=None, **query):
            if path == "/v1/channels/youtube/connect":
                return {"error": "changed shape"}    # no authorization_url
            if path == "/v1/delivery-workflows/start" and body["workflow_key"] == "crashes":
                raise RuntimeError("the harness tripped over something")
            return super().post(path, body, **query)

    api = SuiteApi([({"status": "completed"}, [GREEN])])
    monkeypatch.setattr(drive, "owner_api", lambda target: api)
    monkeypatch.setattr(drive, "_preflight", lambda target, api: [])
    monkeypatch.setattr(drive, "_principal_findings", lambda target, api, worker: [])
    monkeypatch.setattr(drive, "examples", lambda: [])
    target = _target("local")
    target.model_stub_url = "http://stub.test"   # scripted, so no --real-models needed
    code = drive.run_all(target, tmp_path)
    report = json.loads((tmp_path / "workflows.json").read_text())
    assert code == 1 and report["finished"] is True
    assert [o["key"] for o in report["outcomes"]] == ["crashes", "works"]
    crashed, worked = report["outcomes"]
    assert "could not drive" in crashed["problems"][0] and worked["verdict"] == drive.PASS
    assert any("deployment host" in f for f in report["fixture_failures"])
    assert any("YouTube" in f and "authorization_url" in f for f in report["fixture_failures"])
    assert "RuntimeError" in (tmp_path / "harness-errors.log").read_text()
