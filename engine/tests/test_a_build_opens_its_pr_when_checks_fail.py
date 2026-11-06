"""A build opens its pull request even when its checks still fail after the repair rounds.

THE CASE (HelpDesk AI's first build, 2026-10-16): one SSRF finding stayed open after the fix
rounds, the build parked as "Checks failed · needs waiver", and it sat for hours until a person
came to waive. The operator's rule (services/checks_failing.py): a failing check never stops a
build to wait for a waiver. Past the repair rounds the PR opens anyway — a draft labelled
`checks failing`, every failing gate and finding in it word for word, security first — the
failing checks become the follow-up build, the workflow goes on to the merge decision, and that
card says so and merges only on a person's stated reason. The gates are judged exactly as before.

The project setting `open_pr_when_checks_fail` (default 1) returns an org to the waiver stop.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from boss_engine.config import Settings
from boss_engine.services import checks_failing as cf
from boss_engine.services import ci_gate, decision_actions, factory_view, govern_view
from boss_engine.services import project_settings as ps
from boss_engine.services.governed_code_task_service import resolve_gate_failure_action

from .test_workflow_capability_runner import eng, project  # noqa: F401 - fixtures used by name

SSRF = ("## Findings\n- HIGH: POST /api/fetch-preview fetches any URL the caller gives it, "
        "including http://169.254.169.254/ (SSRF) in server/preview.ts:22\n\nVERDICT: ISSUES")
REVIEW = ("FINDING: MAJOR server/tickets.ts:14 — the ticket list ignores the status filter\n"
          "FINDING: NIT server/tickets.ts:3 — unused import\nVERDICT: REFUTED")
EVIDENCE = {"security_summary": SSRF, "review_summary": REVIEW}


def _action(failing, *, on=True, **kw):
    base = dict(failing=failing, advisory_gate=False, rounds_used=2, max_repair_rounds=2,
                open_pr=True, never_block=True, faults={}, fix_round_ran=True)
    base.update(kw)
    return resolve_gate_failure_action(**base, open_pr_when_failing=on)


# ── the stop moves; nothing else does ───────────────────────────────────────────────────────

@pytest.mark.parametrize("failing,kw", [
    (["security_clean"], {"blockers": ["security_clean"]}),     # the HelpDesk SSRF
    (["written_tests_pass"], {}),                               # tests still red
    (["nothing_broke"], {}),
    (["app_serves"], {}),
    (["code_review_passed"], {"never_block": False}),           # the fail-closed install too
])
def test_past_the_repair_rounds_a_failing_check_opens_the_pr(failing, kw):
    assert _action(failing, **kw) == cf.ACTION
    # The setting off is the waiver stop exactly as it was.
    assert _action(failing, on=False, **kw) == "block"


def test_repair_rounds_still_run_first():
    assert _action(["written_tests_pass"], rounds_used=0) == "repair"
    assert _action(["security_clean"], blockers=["security_clean"], fix_round_ran=False,
                   rounds_used=0) == "repair"


@pytest.mark.parametrize("failing,kw", [
    (["app_serves"], {"faults": {"app_serves": "PERSON"}}),     # a secret only a person sets
    (["template_tests"], {}),                                   # an applied template's gate
    (["no_committed_secrets"], {}),                             # a secret never reaches a PR
    (["written_tests_pass"], {"open_pr": False}),               # a run that opens no PR
])
def test_what_no_rule_opens_a_pr_past_still_stops(failing, kw):
    assert _action(failing, **kw) == "block"


# ── the setting, declared like every other build knob ───────────────────────────────────────

def test_the_setting_is_a_project_knob_on_by_default():
    assert Settings().open_pr_when_checks_fail == 1
    spec = ps.KNOBS[cf.SETTING]
    assert spec["attr"] == cf.SETTING and spec["kind"] == "int"
    assert (spec["min"], spec["max"]) == (0, 1)
    assert cf.SETTING in govern_view.ENFORCED and cf.SETTING in govern_view.SWITCHES
    assert ps.coerce(cf.SETTING, 0) == 0
    with pytest.raises(ValueError):
        ps.coerce(cf.SETTING, 2)
    assert cf.enabled(None, Settings(), "p1") is True
    assert cf.enabled(None, SimpleNamespace(open_pr_when_checks_fail=0), "p1") is False


# ── the PR says what is failing, word for word, security first ──────────────────────────────

def test_every_failing_gate_and_finding_is_listed_verbatim_security_first():
    record = cf.collect(["code_review_passed", "written_tests_pass", "security_clean"], EVIDENCE,
                        details={"written_tests_pass": "2 of 14 tests fail: test_filter"})
    assert [g["gate"] for g in record["gates"]][0] == "security_clean"
    assert record["security"] is True
    assert record["findings"][0]["security"] is True
    body = cf.pr_section(record)
    assert body.startswith("## Checks failing")
    assert "draft" in body and "`checks failing`" in body
    assert "fetches any URL the caller gives it, including http://169.254.169.254/ (SSRF)" in body
    assert "the ticket list ignores the status filter" in body
    assert "unused import" in body                       # every finding, not the blocking ones
    assert "2 of 14 tests fail: test_filter" in body
    assert body.index("SSRF") < body.index("status filter")
    # The follow-up build is told the same, in the same order.
    lines = cf.followup_findings(record)
    assert lines[0]["severity"] == "security" and "security_clean" in lines[0]["text"]
    assert any("SSRF" in x["text"] for x in lines)


# ── the build reads "PR open · checks failing" ──────────────────────────────────────────────

def test_the_build_reads_pr_open_checks_failing_never_needs_waiver():
    record = cf.collect(["security_clean"], EVIDENCE)
    run = {"status": "completed", "phase": "done", "pull_request_url": "https://g.test/a/b/pull/9",
           "pull_request_number": 9, "finalize_ctx": {cf.KEY: record}}
    state = factory_view.build_state(run)
    assert state["state"] == "pr_open"
    assert state["word"] == "PR open · checks failing"
    assert state["tip"].startswith("Security finding open")
    plain = factory_view.build_state({**run, "finalize_ctx": {}})
    assert plain["word"] == "PR open"


# ── the merge card says so, and a merge is a person's stated decision ───────────────────────

def _merge_card(**extra):
    record = {**cf.collect(["security_clean"], EVIDENCE), "draft": True, "label": cf.LABEL}
    return {"id": "dec_1", "project_id": "p1", "status": "pending",
            "recommendation": {"action": "review_pull_request",
                               "pull_request_url": "https://g.test/a/b/pull/9",
                               cf.KEY: record, **extra}}


def test_the_merge_card_asks_for_a_reason_to_merge_with_checks_failing():
    actions = {a["action"]: a for a in decision_actions.allowed_actions(_merge_card())}
    assert actions["approve"]["label"] == "Merge with checks failing"
    assert actions["approve"]["needs"] == "note"
    assert actions["reject"]["label"] == "Keep blocked"
    plain = {**_merge_card(), "recommendation": {"action": "review_pull_request"}}
    assert {a["label"] for a in decision_actions.allowed_actions(plain)} >= {"Approve and merge"}


class NoDb:
    def execute(self, *_a, **_k):
        raise RuntimeError("no database in this test")


PERSON = SimpleNamespace(run_token=None, credential=None, legacy=None, is_human_session=True,
                         principal=SimpleNamespace(id="usr_alice"))


def test_a_checks_failing_merge_is_never_silent():
    with pytest.raises(ci_gate.CiGateRefused) as plain:
        ci_gate.refuse_approval(NoDb(), _merge_card(), "ceo", identity_ctx=PERSON)
    assert plain.value.code == "checks_failing"
    assert "Security clean" in plain.value.message and "security finding" in plain.value.message
    # Never a policy, never an agent — forced or not.
    with pytest.raises(ci_gate.CiGateRefused) as policy:
        ci_gate.refuse_approval(NoDb(), _merge_card(), "ceo", system_policy=True, force=True)
    assert policy.value.code == "person_approval_required"
    with pytest.raises(ci_gate.CiGateRefused) as agent:
        ci_gate.refuse_approval(NoDb(), _merge_card(), "agt_release_manager", force=True)
    assert agent.value.code == "person_approval_required"
    # A person who says why (force, whose note decisions.transition requires) decides it.
    ci_gate.refuse_approval(NoDb(), _merge_card(), "ceo", identity_ctx=PERSON, force=True)


# ── the PR is a draft with the label, and a person's merge marks it ready ──────────────────

class _Github:
    repository_full_name = "acme/web"
    supports_draft_pull_requests = True

    def __init__(self):
        self.calls: list = []

    def add_labels(self, number, labels):
        self.calls.append(("labels", number, labels))

    def mark_ready_for_review(self, number):
        self.calls.append(("ready", number))
        return True


def test_the_pr_is_labelled_checks_failing():
    from boss_engine.services.software_factory import SoftwareFactoryService

    events: list = []
    sf = object.__new__(SoftwareFactoryService)
    sf.github = _Github()
    sf.events = SimpleNamespace(emit=lambda *a: events.append(a))
    sf._flag_checks_failing(object(), "p1", 9, "https://g.test/acme/web/pull/9",  # noqa: SLF001
                            cf.collect(["security_clean"], EVIDENCE), "rcpt_1")
    assert ("labels", 9, ["checks failing"]) in sf.github.calls
    (event,) = events
    assert event[2] == "software_factory.checks_failing_flagged"
    assert event[6]["failing_gates"] == ["security_clean"] and event[6]["security"] is True


def test_a_draft_is_marked_ready_only_for_a_checks_failing_merge():
    import json

    from boss_engine.services.software_factory import SoftwareFactoryService

    class Conn:
        def __init__(self, rec):
            self.rec = rec

        def execute(self, *_a, **_k):
            rec = self.rec
            return SimpleNamespace(mappings=lambda: SimpleNamespace(
                all=lambda: [{"recommendation": json.dumps(rec)}]))

    sf = object.__new__(SoftwareFactoryService)
    sf.github = _Github()
    sf.events = SimpleNamespace(emit=lambda *a: None)
    sf._ready_a_checks_failing_draft(Conn(_merge_card()["recommendation"]), "p1",  # noqa: SLF001
                                     "dec_1", 9, "rcpt")
    assert ("ready", 9) in sf.github.calls
    sf.github = _Github()
    sf._ready_a_checks_failing_draft(Conn({"action": "review_pull_request"}), "p1",  # noqa: SLF001
                                     "dec_1", 9, "rcpt")
    assert sf.github.calls == []


def test_the_github_adapter_opens_a_draft_and_marks_it_ready(monkeypatch):
    from boss_engine.services.github_adapter import GitHubAdapter

    sent: list = []

    class Resp:
        def __init__(self, body):
            self.body = body
            self.content = b"x"

        def json(self):
            return self.body

    def send(method, url, *, headers, json=None):
        sent.append((method, url, json))
        if method == "GET" and url.endswith("/repos/acme/web"):
            return Resp({"default_branch": "main"})
        if method == "GET" and "/git/ref/heads/main" in url:
            return Resp({"object": {"sha": "base"}})
        if method == "GET" and "pulls?state=open" in url:
            return Resp([])
        if method == "POST" and url.endswith("/pulls"):
            return Resp({"html_url": "https://g.test/acme/web/pull/9", "number": 9})
        if method == "GET" and url.endswith("/pulls/9"):
            return Resp({"draft": True, "node_id": "PR_n9"})
        if method == "POST" and url.endswith("/graphql"):
            return Resp({"data": {"markPullRequestReadyForReview": {
                "pullRequest": {"isDraft": False}}}})
        raise AssertionError(f"unexpected {method} {url}")

    adapter = GitHubAdapter(Settings(), owner="acme", repo="web", token="t")
    monkeypatch.setattr(adapter, "_send", send)
    monkeypatch.setattr(adapter, "_headers", lambda: {})
    monkeypatch.setattr(adapter, "default_branch_sha", lambda: ("main", "base"))
    adapter.create_pull_request("boss/code/x", "T", "body", draft=True)
    created = next(j for m, u, j in sent if m == "POST" and u.endswith("/pulls"))
    assert created["draft"] is True
    assert adapter.mark_ready_for_review(9) is True
    graphql = next(j for m, u, j in sent if u.endswith("/graphql"))
    assert graphql["variables"] == {"id": "PR_n9"}
    assert "markPullRequestReadyForReview" in graphql["query"]


# ── the workflow: nothing waits for a waiver; the step goes on to the merge decision ───────

def test_a_build_parked_for_a_waiver_opens_its_pr_instead_of_asking(eng, project, monkeypatch):
    """The recovery agent still goes first (it fixed nothing here: the repair could not
    start); where it would ask a person to waive, the parked build's PR opens with its checks
    failing, and the step completes on it saying so."""
    import json
    import uuid

    from sqlalchemy import text

    from boss_engine.services import code_task_service as cts
    from boss_engine.services import step_recovery as sr
    from boss_engine.services import workflow_capability_runner as runner

    from .test_a_failed_step_stops_the_run import (_insert_awaited, _steps,
                                                    _tick_through_retries, _two_step)
    from .test_open_review_findings_go_to_the_agent_first import _park, _Worker
    from .test_workflow_capability_runner import RecordingRegistry, RecordingTool

    awaited_id = f"crun_{uuid.uuid4().hex[:10]}"
    monkeypatch.setattr(runner, "_tool_registry", lambda: RecordingRegistry(
        [RecordingTool("create_code_task", {"code_task_run_id": awaited_id})]))
    monkeypatch.setattr(runner, "_preview_missing_for_run", lambda *a, **k: None)
    monkeypatch.setattr(sr, "routed_adapter",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no model")))
    monkeypatch.setattr(runner, "_start_recovery_build",
                        lambda *a, **k: ("", "the repair build could not be started here"))
    opened: list = []

    def open_it(engine, settings, jobs, run_id, *, actor="boss", why=""):
        opened.append((run_id, why))
        record = cf.collect(["security_clean"], {"security_summary": SSRF})
        with engine.begin() as c2:
            c2.execute(text(
                "UPDATE code_task_runs SET status = 'completed', phase = 'done', "
                "pull_request_url = 'https://g.test/a/b/pull/9', pull_request_number = 9, "
                "finalize_ctx = COALESCE(finalize_ctx, '{}'::jsonb) "
                "|| jsonb_build_object('checks_failing', CAST(:r AS JSONB)) WHERE id = :i"),
                {"i": run_id, "r": json.dumps(record)})
        return {"status": "completed", "pull_request_url": "https://g.test/a/b/pull/9"}

    monkeypatch.setattr(cts, "open_parked_with_checks_failing", open_it)
    with eng.begin() as c:
        run = _two_step(c, project, "build")
        _tick_through_retries(eng, c, project, run["id"], times=2)
        _insert_awaited(c, project, "code_task_runs", awaited_id, "blocked")
        _park(c, project, awaited_id, ["security_clean"])
    try:
        with eng.begin() as c:
            runner.tick(c, _Worker(eng), project_id=project)
        assert opened and opened[0][0] == awaited_id
        with eng.begin() as c:
            step = _steps(c, run["id"])["s"]
            assert "recovery_question" not in step["output"], "nobody is asked to waive"
            runner.tick(c, _Worker(eng), project_id=project)
            step = _steps(c, run["id"])["s"]
        assert step["status"] == "succeeded", step
        assert step["output"]["checks_failing"]["gates"] == ["security_clean"]
        assert step["output"]["checks_failing"]["security"] is True
        assert step["output"]["note"].startswith("PR open · checks failing: Security clean")
    finally:
        with eng.begin() as c:
            c.execute(text("DELETE FROM code_task_runs WHERE id = :i"), {"i": awaited_id})


def test_a_parked_builds_existing_merge_card_is_told_its_checks_are_failing(eng, project):
    """A build parked before the rule already has its `[partial]` PR and an ordinary merge
    card: the card is flagged, so its merge is a person's stated decision too."""
    import json
    import uuid

    from sqlalchemy import text

    did = f"dec_{uuid.uuid4().hex[:10]}"
    url = f"https://g.test/a/b/pull/{uuid.uuid4().int % 1000}"
    with eng.begin() as c:
        c.execute(text("INSERT INTO decisions (id, project_id, question, recommendation) "
                       "VALUES (:i, :p, 'Approve code change: HelpDesk AI (PR #9)', "
                       "CAST(:r AS JSONB))"),
                  {"i": did, "p": project, "r": json.dumps({
                      "action": "review_pull_request", "pull_request_url": url,
                      "summary": "BOSS built it."})})
        record = cf.collect(["security_clean"], {"security_summary": SSRF})
        assert cf.flag_existing_decision(c, project, url, record) == did
        row = c.execute(text("SELECT question, recommendation FROM decisions WHERE id = :i"),
                        {"i": did}).mappings().first()
        c.execute(text("DELETE FROM decisions WHERE id = :i"), {"i": did})
    assert row["question"].startswith("Checks failing — decide the merge: ")
    rec = row["recommendation"]
    assert rec[cf.KEY]["gates"][0]["gate"] == "security_clean"
    assert rec["summary"].startswith("CHECKS FAILING: Security clean")
    assert cf.of_decision(dict(row)) is not None


def test_the_pipeline_is_wired_to_the_rule():
    """Wiring, checked at the source (the pipeline is too large to drive here): every gate
    disposition reads the project's setting, the PR terminal records the checks and opens the
    PR with them, and the follow-up build is told."""
    from pathlib import Path

    from boss_engine.services import governed_code_task_service as gcts

    body = Path(gcts.__file__).read_text()
    assert body.count("open_pr_when_failing=_open_when_failing") >= 2
    assert "_checks_failing.enabled(c, settings, project_id)" in body
    assert 'elif action in ("ship_with_dissent", _checks_failing.ACTION):' in body
    assert "evidence[_checks_failing.KEY] = checks_record" in body
    assert body.count('**({"checks_failing": _checks} if _checks else {})') >= 2
    assert "_pr_summary = _cf_section(_checks) + _generated_pr_summary(" in body
    assert "checks_failing=_checks" in body


def test_a_draft_whose_checks_a_later_build_fixed_is_still_marked_ready(eng, project):
    """The follow-up build lands on the same branch and PR: its card is a plain one, and the
    PR is still the draft BOSS opened. Merging it marks it ready; GitHub would refuse a draft."""
    import json
    import uuid

    from sqlalchemy import text

    from boss_engine.services.software_factory import SoftwareFactoryService

    url = f"https://g.test/acme/web/pull/{uuid.uuid4().int % 100000}"
    first, later = f"dec_{uuid.uuid4().hex[:10]}", f"dec_{uuid.uuid4().hex[:10]}"
    with eng.begin() as c:
        for did, rec in ((first, _merge_card()["recommendation"]),
                         (later, {"action": "review_pull_request"})):
            c.execute(text("INSERT INTO decisions (id, project_id, question, recommendation) "
                           "VALUES (:i, :p, 'q', CAST(:r AS JSONB))"),
                      {"i": did, "p": project, "r": json.dumps({**rec, "pull_request_url": url})})
        sf = object.__new__(SoftwareFactoryService)
        sf.github = _Github()
        sf.events = SimpleNamespace(emit=lambda *a: None)
        sf._ready_a_checks_failing_draft(c, project, later, 9, "rcpt",  # noqa: SLF001
                                         pull_request_url=url)
        assert ("ready", 9) in sf.github.calls
        sf.github = _Github()
        sf._ready_a_checks_failing_draft(c, project, later, 9, "rcpt",  # noqa: SLF001
                                         pull_request_url=url + "0")
        assert sf.github.calls == [], "another PR's draft is not this one's"
        c.execute(text("DELETE FROM decisions WHERE id IN (:a, :b)"), {"a": first, "b": later})
