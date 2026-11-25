"""A merge card reports the latest build's checks, and a newer build's card replaces the older
ones (BOSSAPP-1320, AR-26).

northgate-gateway, change #2 (2026-11-16): crun_f5868a67 passed Security clean, Review passed
and Consistent and failed only its must-haves, yet its card dec_81fa4797 listed all three as
failing, "shipped failing by an earlier build of this pull request (crun_5c4f…, crun_9a9a…,
crun_a20439…)". A judgment seat's failure never cleared, even when a later build re-judged the
whole change. And the change had three merge cards pending at once (dec_6e13d604, dec_29974abe,
dec_81fa4797), each speaking for a different build: the run-keyed sweep never saw a `[partial]`
rescue card or a checks-failing card, which are not written to code_task_runs.decision_id.

Now: an earlier failure counts only while no later build of the PR re-ran that check over the
whole change (a follow-up re-judges only its delta, so from a follow-up only the whole-tree
gates clear), and a new merge card withdraws every older open card for the same pull request,
with a note naming the new one.
"""
from __future__ import annotations

import json
import uuid

from sqlalchemy import text

from boss_engine.services import acceptance_gate as ag
from boss_engine.services import checks_failing as cf
from boss_engine.services import run_decisions

from .test_acceptance_gate_holds_the_build_to_its_must_haves import PR, _cleanup, _run
from .test_workflow_capability_runner import eng, project  # noqa: F401 - fixtures used by name

JUDGED = ["security_clean", "code_review_passed", "consistent"]
EVERYTHING = ["code_written", "written_tests_pass", "existing_tests_intact", "nothing_broke",
              "app_serves", *JUDGED]


def _lineage(c, pid, *, latest_fctx=None, latest_passed=None):
    root = _run(c, pid, pr=PR, fctx={})
    c.execute(text("UPDATE code_task_runs SET finalize_ctx = finalize_ctx || "
                   "jsonb_build_object('root_run_id', id) WHERE id = :i"), {"i": root})
    shipped = {"checks_failing": {"gates": [{"gate": g} for g in JUDGED]}}
    early = _run(c, pid, pr=PR, fctx={"root_run_id": root, **shipped},
                 passed=["code_written", "nothing_broke"])
    follow = _run(c, pid, pr=PR, fctx={"root_run_id": root, "is_followup": True,
                                       "advisory_failing_gates": ["code_review_passed"]},
                  passed=["code_written"])
    latest = _run(c, pid, status="blocked", pr=PR,
                  fctx={"root_run_id": root, "attempt": 4, **(latest_fctx or {})},
                  passed=latest_passed)
    return root, early, follow, latest


def test_the_card_reports_the_latest_builds_checks(eng, project):  # noqa: F811
    with eng.begin() as c:
        _, early, follow, latest = _lineage(c, project, latest_passed=EVERYTHING)
    try:
        with eng.begin() as c:
            card, _ = ag.merge_card_checks(c, run_id=latest, evidence={},
                                           checks_failing=None, pr_url=PR)
        names = set(cf.gate_names(card)) if card else set()
        assert not names & set(JUDGED), (
            f"the latest build passed {JUDGED}; the card still lists {sorted(names)}")
    finally:
        _cleanup(eng, project)


def test_an_earlier_failure_stands_while_no_later_build_re_ran_it(eng, project):  # noqa: F811
    # The latest build re-ran only the whole-tree gates (its seats did not judge): the earlier
    # failures of the judgment seats still show, naming the builds that shipped past them.
    with eng.begin() as c:
        _, early, follow, latest = _lineage(c, project, latest_passed=["code_written",
                                                                       "nothing_broke"])
    try:
        with eng.begin() as c:
            card, _ = ag.merge_card_checks(c, run_id=latest, evidence={},
                                           checks_failing=None, pr_url=PR)
        assert set(JUDGED) <= set(cf.gate_names(card))
        review = next(g for g in card["gates"] if g["gate"] == "code_review_passed")
        assert early in review["detail"] and follow in review["detail"]
    finally:
        _cleanup(eng, project)


def test_a_follow_ups_delta_review_clears_nothing_it_did_not_judge(eng, project):  # noqa: F811
    with eng.begin() as c:
        root = _run(c, project, pr=PR, fctx={"checks_failing": {"gates": [
            {"gate": "code_review_passed"}, {"gate": "nothing_broke"}]}})
        c.execute(text("UPDATE code_task_runs SET finalize_ctx = finalize_ctx || "
                       "jsonb_build_object('root_run_id', id) WHERE id = :i"), {"i": root})
        follow = _run(c, project, status="running", pr=PR,
                      fctx={"root_run_id": root, "is_followup": True},
                      passed=["code_review_passed", "nothing_broke"])
    try:
        with eng.begin() as c:
            card, _ = ag.merge_card_checks(c, run_id=follow, evidence={},
                                           checks_failing=None, pr_url=PR)
        assert cf.gate_names(card) == ["code_review_passed"]
    finally:
        _cleanup(eng, project)


def _card(c, pid, *, run_id, url=PR, status="pending", action="review_pull_request"):
    did = f"dec_{uuid.uuid4().hex[:12]}"
    c.execute(text(
        "INSERT INTO decisions (id, project_id, question, recommendation, status, created_at) "
        "VALUES (:i, :p, 'Approve code change', CAST(:r AS JSONB), :s, clock_timestamp())"),
        {"i": did, "p": pid, "s": status,
         "r": json.dumps({"action": action, "pull_request_url": url, "run_id": run_id})})
    return did


def test_a_newer_builds_card_withdraws_the_cards_it_supersedes(eng, project):  # noqa: F811
    with eng.begin() as c:
        root, early, follow, latest = _lineage(c, project, latest_passed=EVERYTHING)
        partial = _card(c, project, run_id=root)                 # a [partial] rescue card
        failing = _card(c, project, run_id=early)                # a checks-failing card
        other_pr = _card(c, project, run_id=early, url=PR + "0")
        answered = _card(c, project, run_id=follow, status="approved")
        not_merge = _card(c, project, run_id=early, action="resume_code_task")
        new = _card(c, project, run_id=latest)
    try:
        with eng.begin() as c:
            gone = run_decisions.withdraw_superseded_merge_cards(
                c, project, PR, new, run_id=latest)
            rows = {r["id"]: r for r in c.execute(text(
                "SELECT id, status, resolution_note FROM decisions WHERE project_id = :p"),
                {"p": project}).mappings()}
            # An older build's sweep never withdraws the newer card.
            assert run_decisions.withdraw_superseded_merge_cards(
                c, project, PR, failing, run_id=early) == []
        assert sorted(gone) == sorted([partial, failing])
        for did in (partial, failing):
            assert rows[did]["status"] == "withdrawn"
            assert new in rows[did]["resolution_note"] and latest in rows[did]["resolution_note"]
        assert rows[new]["status"] == "pending"
        assert rows[other_pr]["status"] == "pending"
        assert rows[answered]["status"] == "approved"
        assert rows[not_merge]["status"] == "pending"
    finally:
        with eng.begin() as c:
            c.execute(text("DELETE FROM decisions WHERE project_id = :p"), {"p": project})
        _cleanup(eng, project)


def test_every_new_merge_card_withdraws_the_older_ones():
    """Wiring, at the source: the one place a merge card is created withdraws the cards it
    supersedes, by the pull request's URL, right after creating it."""
    import inspect

    from boss_engine.services.software_factory import SoftwareFactoryService

    src = inspect.getsource(SoftwareFactoryService)
    made = src.index('"merge_decision_required",')
    sweep = src.index("withdraw_superseded_merge_cards(connection, project_id, pr.url,")
    assert made < sweep < made + 900



# ── PR #158 review 1: a SCOPED re-review never clears an earlier judgment failure ───────────

def _scoped_lineage(c, pid, *, latest_fctx):
    root = _run(c, pid, pr=PR, fctx={})
    c.execute(text("UPDATE code_task_runs SET finalize_ctx = finalize_ctx || "
                   "jsonb_build_object('root_run_id', id) WHERE id = :i"), {"i": root})
    early = _run(c, pid, pr=PR, fctx={"root_run_id": root, "checks_failing": {"gates": [
        {"gate": "security_clean"}, {"gate": "nothing_broke"}]}}, passed=["code_written"])
    latest = _run(c, pid, status="blocked", pr=PR,
                  fctx={"root_run_id": root, **latest_fctx}, passed=EVERYTHING)
    return early, latest


def test_a_scoped_security_pass_does_not_clear_an_earlier_security_failure(eng, project):  # noqa: F811
    """A retry's security_check re-read only its repair delta (repair_review_scope): it never
    re-read auth/handler.go, so its pass is no word on the earlier failure there."""
    with eng.begin() as c:
        early, latest = _scoped_lineage(c, project, latest_fctx={
            "parent_run_id": "crun_parent", "review_scope": {
                "security_check": "scoped", "review_code": "full",
                "consistency_check": "full"}})
    try:
        with eng.begin() as c:
            card, _ = ag.merge_card_checks(c, run_id=latest, evidence={},
                                           checks_failing=None, pr_url=PR)
        # security stays (scoped); nothing_broke clears (a whole-tree gate, always re-run).
        assert cf.gate_names(card) == ["security_clean"]
        assert early in card["gates"][0]["detail"]
    finally:
        _cleanup(eng, project)


def test_a_full_security_re_review_clears_it(eng, project):  # noqa: F811
    with eng.begin() as c:
        _, latest = _scoped_lineage(c, project, latest_fctx={
            "parent_run_id": "crun_parent", "review_scope": {
                "security_check": "full", "review_code": "full", "consistency_check": "full"}})
    try:
        with eng.begin() as c:
            card, _ = ag.merge_card_checks(c, run_id=latest, evidence={},
                                           checks_failing=None, pr_url=PR)
        assert card is None or "security_clean" not in cf.gate_names(card)
    finally:
        _cleanup(eng, project)


def test_a_build_with_a_parent_and_no_recorded_mode_clears_no_seat(eng, project):  # noqa: F811
    with eng.begin() as c:
        _, latest = _scoped_lineage(c, project, latest_fctx={"parent_run_id": "crun_parent"})
    try:
        with eng.begin() as c:
            card, _ = ag.merge_card_checks(c, run_id=latest, evidence={},
                                           checks_failing=None, pr_url=PR)
        assert cf.gate_names(card) == ["security_clean"]
    finally:
        _cleanup(eng, project)


def test_each_seats_mode_is_kept_on_the_run_and_scoped_wins(eng, project):  # noqa: F811
    from boss_engine.services import governed_code_task_service as governed

    with eng.begin() as c:
        rid = _run(c, project, pr=PR, fctx={"parent_run_id": "crun_parent"})
    try:
        governed._record_review_scope(eng, rid, {"security_check": "scoped",
                                                 "review_code": "full"})
        governed._record_review_scope(eng, rid, {"security_check": "full",
                                                 "review_code": "full",
                                                 "consistency_check": "full"})
        with eng.begin() as c:
            kept = c.execute(text("SELECT finalize_ctx->'review_scope' FROM code_task_runs "
                                  "WHERE id = :i"), {"i": rid}).scalar()
        assert kept == {"security_check": "scoped", "review_code": "full",
                        "consistency_check": "full"}
    finally:
        _cleanup(eng, project)


def test_the_scope_decision_is_recorded_on_the_run_on_every_path():
    import inspect

    from boss_engine.services import governed_code_task_service as governed

    src = inspect.getsource(governed._repair_review_scope_blocks)
    assert src.count("_record_review_scope(") == 2      # the early FULL return and the plan


# ── PR #158 review 4: one PR, however its URL was written ──────────────────────────────────

def test_cards_for_the_same_pr_under_another_spelling_are_withdrawn(eng, project):  # noqa: F811
    variants = [PR.replace("https://", "http://") + "/", PR.upper().replace("HTTPS://", "https://"),
                PR.replace("/pull/", ".git/pull/"), "https://www." + PR.split("://", 1)[1]]
    with eng.begin() as c:
        root, early, _, latest = _lineage(c, project, latest_passed=EVERYTHING)
        olds = [_card(c, project, run_id=early, url=u) for u in variants]
        other = _card(c, project, run_id=early, url=PR + "0/")
        new = _card(c, project, run_id=latest)
    try:
        with eng.begin() as c:
            gone = run_decisions.withdraw_superseded_merge_cards(c, project, PR + "/", new,
                                                                  run_id=latest)
            status = dict(c.execute(text("SELECT id, status FROM decisions WHERE project_id = :p"),
                                    {"p": project}).all())
        assert sorted(gone) == sorted(olds)
        assert status[other] == "pending" and status[new] == "pending"
    finally:
        with eng.begin() as c:
            c.execute(text("DELETE FROM decisions WHERE project_id = :p"), {"p": project})
        _cleanup(eng, project)


def test_normalize_pr_url():
    n = run_decisions.normalize_pr_url
    assert n("http://www.GitHub.com/Acme/GW.git/pull/7/?x=1#c") == "https://github.com/acme/gw/pull/7"
    assert n("https://github.com/acme/gw/pull/7") != n("https://github.com/acme/gw/pull/70")


class _FakeGitHub:
    """One open pull request on GitHub, as the adapter's REST calls see it."""

    def __init__(self):
        self.pr = None

    def request(self, method, path, payload=None):
        if method == "GET" and "/pulls?state=open" in path:
            return [self.pr] if self.pr else []
        if method == "POST" and path.endswith("/pulls"):
            self.pr = {"number": 9, "html_url": PR, "title": payload["title"],
                       "body": payload["body"]}
            return self.pr
        if method == "PATCH" and "/pulls/" in path:
            self.pr.update(payload)
            return self.pr
        raise AssertionError(f"unexpected {method} {path}")


def _adapter(fake):
    from boss_engine.config import get_settings
    from boss_engine.services.github_adapter import GitHubAdapter

    gh = object.__new__(GitHubAdapter)
    gh.settings = get_settings()
    gh.owner, gh.repo = "acme", "shop"
    gh._request = fake.request
    gh.default_branch_sha = lambda: ("main", "abc")
    return gh


def test_a_three_attempt_lineage_leaves_one_live_card_and_a_clean_title(eng, project):  # noqa: F811
    """BOSSAPP-1309: partial -> checks-failing -> full on ONE pull request. After every attempt
    exactly one merge card is live, and once the full build lands the PR says nothing about
    "[partial]" (the rescue's title used to stay for ever: only the body was refreshed)."""
    fake = _FakeGitHub()
    gh = _adapter(fake)
    with eng.begin() as c:
        root, early, _follow, latest = _lineage(c, project, latest_passed=EVERYTHING)
    try:
        for run_id, title in ((root, "[partial] Checkout page"), (early, "Checkout page"),
                              (latest, "Checkout page")):
            gh.create_pull_request("boss/code/x", title, f"body from {run_id}")
            with eng.begin() as c:
                new = _card(c, project, run_id=run_id)
                run_decisions.withdraw_superseded_merge_cards(c, project, PR, new,
                                                              run_id=run_id)
                live = c.execute(text(
                    "SELECT id FROM decisions WHERE project_id = :p AND status = 'pending' "
                    "AND recommendation->>'action' = 'review_pull_request'"),
                    {"p": project}).scalars().all()
            assert live == [new], f"after {run_id}: {len(live)} live merge cards"
        assert fake.pr["title"] == "Checkout page"
        assert fake.pr["body"] == f"body from {latest}"
        # A later partial rescue never re-marks a finished PR, and a person's own title stays.
        gh.create_pull_request("boss/code/x", "[partial] Checkout page", "rescue")
        assert fake.pr["title"] == "Checkout page"
    finally:
        with eng.begin() as c:
            c.execute(text("DELETE FROM decisions WHERE project_id = :p"), {"p": project})
        _cleanup(eng, project)


def test_only_the_partial_mark_is_ever_cleared():
    from boss_engine.services.github_adapter import retitle_for

    assert retitle_for("[partial] Checkout", "Checkout") == "Checkout"
    assert retitle_for("Checkout", "[partial] Checkout") is None
    assert retitle_for("My own title", "Checkout") is None
    assert retitle_for("[partial] Checkout", "[partial] Checkout") is None
    assert retitle_for("[partial] Checkout", "") is None
