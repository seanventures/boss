"""A local pull request never waits on its own row lock (crun_ce0f6332, 2026-11-15).

THE CASE: a build on a local project finished its work with checks still failing. Its PR step
(software_factory.propose_pull_request) ran inside ONE transaction `c`: local_review's
on_change_proposed UPDATEd the change set's row on `c`, then the `checks failing` label was
written by LocalGitAdapter.add_labels on a SECOND connection with `SELECT … FOR UPDATE` on that
same row. The second connection waited on `c`'s lock, `c` sat idle waiting on it, and Postgres
ended `c` after the API's 300 s idle-in-transaction limit: the build failed after every gate,
with its PR half-recorded (also crun_496feb42, 2026-11-11).

Now the adapter's change-set reads and writes run on the caller's connection while it holds one
(local_repo_host.writes_on), its own transactions carry a lock timeout, and opening its own
transaction while the caller holds a written one is an error in tests (the guard below).

Separately, the [partial] PR a failed build opens (_rescue_pr) asked for a FRESH reservation
(`reservation_id=None`), which always failed: every failure raised a blocking "Missing active
reservation for PR proposal" card and never opened the PR. A rescue now needs none
(`rescue_of_run`), touches no hold, and records the waiver as an event.

Real git in temporary bare repositories and the real database; each transaction under test is
given a 2 s idle-in-transaction limit, so a self-wait fails the test in seconds.
"""
from __future__ import annotations

import inspect
import json
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from apps.api.tests.identity_fixtures import IdentityWorld
from boss_engine import db as boss_db
from boss_engine.config import get_settings
from boss_engine.schemas import FileChange
from boss_engine.services import checks_failing as cf
from boss_engine.services import local_projects, local_repo, local_repo_host
from boss_engine.services.local_repo_host import LocalGitAdapter, LocalHostTransactionCycle


@pytest.fixture(scope="module")
def world(engine) -> IdentityWorld:
    w = IdentityWorld.create(engine)
    w.owner = w.person("ll-owner")
    w.bind(w.owner, "owner")
    return w


@pytest.fixture
def repo_root(tmp_path, monkeypatch):
    root = tmp_path / "repos"
    root.mkdir()
    monkeypatch.setattr(local_repo, "root", lambda settings=None: root)
    return root


@pytest.fixture(autouse=True)
def strict_guard(monkeypatch):
    """The guard is an error here: an adapter transaction opened beside a written one fails."""
    monkeypatch.setenv(local_repo_host.STRICT_GUARD_ENV, "1")


def _idle_limit(c) -> None:
    c.execute(text("SET LOCAL idle_in_transaction_session_timeout = '2s'"))


def _project(engine, world):
    """A fresh local project with a build's branch (two commits) and the build's run row."""
    pid = f"ll-{uuid.uuid4().hex[:8]}"
    with engine.begin() as c:
        ws = c.execute(text("SELECT workspace_id FROM projects WHERE id = :p"),
                       {"p": world.project_a}).scalar()
        c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES "
                       "(:id, :ws, :n, :id)"), {"id": pid, "ws": ws, "n": f"Desk {pid[-4:]}"})
        bound = local_projects.create_for_project(c, get_settings(), pid, name="desk", actor="t")
    bare = local_repo.bare_path(bound["owner"], bound["repo"])
    local_repo.commit_files(bare, "main", {"src/app.js": "one\n"}, "base")
    run_id = "crun_ll" + uuid.uuid4().hex[:10]
    branch = f"boss/code/{run_id}"
    local_repo.ensure_branch(bare, branch)
    local_repo.commit_files(bare, branch, {"src/app.js": "two\n"}, "change")
    with engine.begin() as c:
        c.execute(text("INSERT INTO code_task_runs (id, project_id, title, status, phase, spec, "
                       "finalize_ctx) VALUES (:id, :p, 'Desk timer', 'running', 'opening_pr', "
                       "CAST(:s AS JSONB), '{}'::jsonb)"),
                  {"id": run_id, "p": pid,
                   "s": json.dumps({"owner": bound["owner"], "repo": bound["repo"]})})
    return pid, bound, branch, run_id


def _factory(engine, pid):
    from boss_engine.services.software_factory import SoftwareFactoryService

    sf = SoftwareFactoryService(get_settings())
    with engine.begin() as c:
        sf.bind_project_github(c, pid)
    assert isinstance(sf.github, LocalGitAdapter)
    return sf


def _reserve(engine, sf, pid) -> str:
    with engine.begin() as c:
        made = sf.budget.reserve(c, pid, category="code_task", amount_usd_micros=1000,
                                 reason="local PR lock test", expires_in_minutes=30)
    assert made.get("status") == "reserved", made
    return made["reservation_id"]


def _change(engine, bound, number=1):
    with engine.connect() as c:
        return c.execute(text("SELECT * FROM local_change_sets WHERE repository = :r AND "
                              "number = :n"), {"r": f"{bound['owner']}/{bound['repo']}",
                                               "n": number}).mappings().one()


def _propose(sf, c, pid, branch, build, **kw):
    return sf.propose_pull_request(
        c, pid, None, dedupe_key=f"code:task:{build}", title="Desk timer", branch_name=branch,
        summary="An SLA timer.", files=[FileChange(path="src/app.js", content="two\n")],
        pr_base="main", **kw)


# ── the PR step with checks failing completes inside its one transaction ───────────────────────

def test_a_checks_failing_pr_on_a_local_host_completes_labels_and_commits_its_receipt(
        engine, world, repo_root):
    pid, bound, branch, run_id = _project(engine, world)
    sf = _factory(engine, pid)
    reservation_id = _reserve(engine, sf, pid)
    record = cf.collect(["written_tests_pass"], {},
                        details={"written_tests_pass": "1 of 3 tests fail: test_timer"})
    with boss_db.begin(engine) as c:
        _idle_limit(c)
        proposal = _propose(sf, c, pid, branch, run_id, reservation_id=reservation_id,
                            run_id=run_id, checks_failing=record)
    assert proposal["status"] == "created", proposal
    change = _change(engine, bound)
    assert cf.LABEL in list(change["labels"] or [])
    assert change["run_id"] == run_id                         # on_change_proposed, same transaction
    with engine.connect() as c:
        receipt = c.execute(text("SELECT status FROM receipts WHERE id = :i"),
                            {"i": proposal["receipt_id"]}).scalar()
        flagged = c.execute(text(
            "SELECT data FROM events WHERE project_id = :p "
            "AND event_type = 'software_factory.checks_failing_flagged'"),
            {"p": pid}).scalar()
    assert receipt == "succeeded"
    flagged = flagged if isinstance(flagged, dict) else json.loads(flagged)
    assert flagged.get("label") is True and "label_error" not in flagged


# ── every change-set write joins the caller's transaction, never waits on its lock ─────────────

def test_mark_ready_label_body_and_close_join_a_transaction_that_holds_the_row(
        engine, world, repo_root):
    pid, bound, branch, run_id = _project(engine, world)
    adapter = LocalGitAdapter(get_settings(), owner=bound["owner"], repo=bound["repo"],
                              project_id=pid)
    pr = adapter.create_pull_request(branch, "Desk timer", "", draft=True, run_id=run_id)
    with boss_db.begin(engine) as c:
        _idle_limit(c)
        c.execute(text("UPDATE local_change_sets SET title = 'Desk timer v2' WHERE repository = :r "
                       "AND number = :n"), {"r": adapter.repository_full_name, "n": pr.number})
        with local_repo_host.writes_on(c):
            assert adapter.mark_ready_for_review(pr.number) is True
            adapter.add_labels(pr.number, [cf.LABEL])
            adapter.update_pull_request_body(pr.number, "new body")
            assert adapter.change_set(pr.number)["body"] == "new body"   # reads see our writes
    change = _change(engine, bound, pr.number)
    assert change["draft"] is False and change["body"] == "new body"
    assert change["title"] == "Desk timer v2" and cf.LABEL in list(change["labels"] or [])
    with boss_db.begin(engine) as c:
        _idle_limit(c)
        c.execute(text("UPDATE local_change_sets SET updated_at = now() WHERE repository = :r "
                       "AND number = :n"), {"r": adapter.repository_full_name, "n": pr.number})
        with local_repo_host.writes_on(c):
            adapter.close_change_set(pr.number)
    assert _change(engine, bound, pr.number)["state"] == "closed"


def test_the_merge_path_marks_a_checks_failing_draft_ready_on_its_own_transaction(
        engine, world, repo_root):
    """software_factory._ready_a_checks_failing_draft (a person's merge decision) marks the draft
    ready on the merge's connection, which may hold the change set's row."""
    from boss_engine.services.core import create_blocking_decision

    pid, bound, branch, run_id = _project(engine, world)
    sf = _factory(engine, pid)
    pr = sf.github.create_pull_request(branch, "Desk timer", "", draft=True, run_id=run_id)
    record = {**cf.collect(["written_tests_pass"], {}), "draft": True}
    with engine.begin() as c:
        decision_id = create_blocking_decision(
            c, pid, None, "Approve code change: Desk timer",
            {"action": "review_pull_request", "pull_request_url": pr.url, cf.KEY: record},
            {}, "review")
    with boss_db.begin(engine) as c:
        _idle_limit(c)
        c.execute(text("UPDATE local_change_sets SET updated_at = now() WHERE repository = :r "
                       "AND number = :n"), {"r": sf.github.repository_full_name, "n": pr.number})
        sf._ready_a_checks_failing_draft(c, pid, decision_id, pr.number, "rcpt_x",  # noqa: SLF001
                                         pull_request_url=pr.url)
    assert _change(engine, bound, pr.number)["draft"] is False


# ── the guard, and the lock timeout behind it ──────────────────────────────────────────────────

def test_the_adapter_refuses_its_own_transaction_beside_a_written_one(engine, world, repo_root):
    pid, bound, branch, run_id = _project(engine, world)
    adapter = LocalGitAdapter(get_settings(), owner=bound["owner"], repo=bound["repo"],
                              project_id=pid)
    pr = adapter.create_pull_request(branch, "Desk timer", "", run_id=run_id)
    # A caller that only READ holds no lock: the adapter's own transaction is fine.
    with boss_db.begin(engine) as c:
        c.execute(text("SELECT 1 FROM local_change_sets WHERE repository = :r"),
                  {"r": adapter.repository_full_name})
        adapter.add_labels(pr.number, ["read-only caller"])
    # A caller that WROTE: refused at once, in words, before a second connection is opened.
    with boss_db.begin(engine) as c:
        c.execute(text("UPDATE local_change_sets SET updated_at = now() WHERE repository = :r "
                       "AND number = :n"), {"r": adapter.repository_full_name, "n": pr.number})
        with pytest.raises(LocalHostTransactionCycle, match="writes_on"):
            adapter.add_labels(pr.number, [cf.LABEL])
        for write in (lambda: adapter.mark_ready_for_review(pr.number),
                      lambda: adapter.update_pull_request_body(pr.number, "x"),
                      lambda: adapter.close_change_set(pr.number)):
            with pytest.raises(LocalHostTransactionCycle):
                write()
    # No transaction at all: the adapter's own, as before.
    adapter.add_labels(pr.number, ["no caller"])
    assert {"read-only caller", "no caller"} <= set(_change(engine, bound)["labels"] or [])


def test_a_cycle_left_anywhere_fails_fast_on_the_lock_timeout(engine, world, repo_root,
                                                              monkeypatch):
    """Guard off (production logs it): the adapter's own transaction still never waits long."""
    monkeypatch.delenv(local_repo_host.STRICT_GUARD_ENV)
    monkeypatch.setattr(local_repo_host, "LOCK_TIMEOUT", "300ms")
    pid, bound, branch, run_id = _project(engine, world)
    adapter = LocalGitAdapter(get_settings(), owner=bound["owner"], repo=bound["repo"],
                              project_id=pid)
    pr = adapter.create_pull_request(branch, "Desk timer", "", run_id=run_id)
    with boss_db.begin(engine) as c:
        c.execute(text("UPDATE local_change_sets SET updated_at = now() WHERE repository = :r "
                       "AND number = :n"), {"r": adapter.repository_full_name, "n": pr.number})
        with pytest.raises(OperationalError, match="lock timeout"):
            adapter.add_labels(pr.number, [cf.LABEL])


# ── the [partial] PR of a failed build needs no fresh reservation ──────────────────────────────

def _missing_reservation_cards(engine, pid) -> int:
    with engine.connect() as c:
        return int(c.execute(text("SELECT count(*) FROM decisions WHERE project_id = :p AND "
                                  "question = 'Missing active reservation for PR proposal'"), {"p": pid}).scalar())


def test_a_rescue_pr_opens_without_a_reservation_and_raises_no_card(engine, world, repo_root):
    pid, bound, branch, run_id = _project(engine, world)
    sf = _factory(engine, pid)
    # The build's hold already settled or lapsed: what _rescue_pr meets on a late failure.
    with boss_db.begin(engine) as c:
        _idle_limit(c)
        proposal = _propose(sf, c, pid, branch, run_id, reservation_id=None,
                            rescue_of_run=run_id, checks_failing=cf.collect(["nothing_broke"], {}))
    assert proposal["status"] == "created", proposal
    assert _missing_reservation_cards(engine, pid) == 0
    with engine.connect() as c:
        waived = c.execute(text(
            "SELECT data FROM events WHERE project_id = :p "
            "AND event_type = 'software_factory.rescue_pr_without_reservation'"),
            {"p": pid}).scalar()
    waived = waived if isinstance(waived, dict) else json.loads(waived)
    assert waived["reservation_active"] is False
    assert cf.LABEL in list(_change(engine, bound)["labels"] or [])
    # Without the rescue flag the same call is still refused with the card, as before.
    pid2, _bound2, branch2, run2 = _project(engine, world)
    sf2 = _factory(engine, pid2)
    with boss_db.begin(engine) as c:
        refused = _propose(sf2, c, pid2, branch2, run2, reservation_id=None)
    assert refused["status"] == "blocked" and _missing_reservation_cards(engine, pid2) == 1


def test_a_rescue_leaves_an_active_hold_to_the_builds_exit(engine, world, repo_root):
    """The build's exit settles its money on its own hold (settle_or_release), once: the rescue
    neither settles nor releases it."""
    pid, bound, branch, run_id = _project(engine, world)
    sf = _factory(engine, pid)
    reservation_id = _reserve(engine, sf, pid)
    with engine.begin() as c:
        c.execute(text("UPDATE code_task_runs SET reservation_id = :res WHERE id = :r"),
                  {"res": reservation_id, "r": run_id})
    with boss_db.begin(engine) as c:
        proposal = _propose(sf, c, pid, branch, run_id, reservation_id=reservation_id,
                            rescue_of_run=run_id)
    assert proposal["status"] == "created", proposal
    with engine.connect() as c:
        status = c.execute(text("SELECT status FROM budget_reservations WHERE id = :i"),
                           {"i": reservation_id}).scalar()
    assert status == "active"
    assert _missing_reservation_cards(engine, pid) == 0


def test_a_rescue_naming_another_build_or_hold_gets_no_waiver(engine, world, repo_root):
    pid, _bound, branch, run_id = _project(engine, world)
    other_pid, _b2, _br2, other_run = _project(engine, world)
    sf = _factory(engine, pid)
    # A build of ANOTHER project: the ordinary rule, the card, and the refusal on record.
    with boss_db.begin(engine) as c:
        refused = _propose(sf, c, pid, branch, run_id, reservation_id=None,
                           rescue_of_run=other_run)
    assert refused["status"] == "blocked" and _missing_reservation_cards(engine, pid) == 1
    # A hold that is not the build's (nor one it renewed): no waiver either.
    stranger = _reserve(engine, sf, pid)
    with engine.connect() as c:
        assert sf._is_rescue_of(c, pid, run_id, stranger) is False  # noqa: SLF001
    with engine.begin() as c:
        c.execute(text(
            "INSERT INTO events (id, project_id, event_type, actor, entity_type, entity_id, data) "
            "VALUES (:id, :p, 'code_task.reservation_renewed', 'boss', 'code_task_run', :r, "
            "CAST(:d AS JSONB))"),
            {"id": "evt_" + uuid.uuid4().hex[:20], "p": pid, "r": run_id,
             "d": json.dumps({"reservation_id": stranger})})
        assert sf._is_rescue_of(c, pid, run_id, stranger) is True     # noqa: SLF001 — renewed
    with engine.connect() as c:
        events = c.execute(text("SELECT count(*) FROM events WHERE project_id = :p AND "
                                "event_type = 'software_factory.rescue_pr_refused'"),
                           {"p": pid}).scalar()
    assert events == 1 and other_pid != pid


def test_the_whole_merge_of_a_checks_failing_draft_runs_on_the_merges_transaction(
        engine, world, repo_root):
    """software_factory.merge_pull_request marks the draft ready (the row is then locked on the
    merge's connection) and merges the change set: both on that connection, or the merge would
    wait on the lock the draft-ready step just took."""
    from boss_engine.services.core import create_blocking_decision

    pid, bound, branch, run_id = _project(engine, world)
    sf = _factory(engine, pid)
    pr = sf.github.create_pull_request(branch, "Desk timer", "", draft=True, run_id=run_id)
    record = {**cf.collect(["written_tests_pass"], {}), "draft": True}
    with engine.begin() as c:
        decision_id = create_blocking_decision(
            c, pid, None, "Approve code change: Desk timer",
            {"action": "review_pull_request", "pull_request_url": pr.url, cf.KEY: record},
            {}, "review")
        c.execute(text("UPDATE decisions SET status = 'approved', approved_by = 'owner', "
                       "approved_at = now() WHERE id = :d"), {"d": decision_id})
    with boss_db.begin(engine) as c:
        _idle_limit(c)
        merged = sf.merge_pull_request(c, pid, None, decision_id, f"merge:{run_id}", pr.number,
                                       pr.url, "Desk timer", branch)
    assert merged.get("status") not in ("failed", "blocked"), merged
    change = _change(engine, bound, pr.number)
    assert change["state"] == "merged" and change["draft"] is False and change["merged_sha"]


def test_rescue_pr_passes_the_runs_hold_and_records_a_failure_as_an_event():
    from boss_engine.services import governed_code_task_service as g

    src = inspect.getsource(g)
    rescue = src[src.index("    def _rescue_pr(reason: str)"):src.index("    def _mark_stage_done(")]
    assert "reservation_id=None" not in rescue
    assert "reservation_id=reservation_id" in rescue and "rescue_of_run=run_id" in rescue
    assert "code_task.partial_pr_failed" in rescue and "create_blocking" not in rescue


@pytest.mark.parametrize("method", ["merge", "squash"])
def test_a_rolled_back_caller_leaves_merge_and_guarded_retry_idempotent(
        engine, world, repo_root, method):
    """BOSSAPP-1239: if a caller's DB transaction rolls back after local_repo.merge mutated git
    on disk, local_change_sets stays 'open' while main already holds the merge/squash. A retry
    (both merge_pull_request and merge_pull_request_guarded) must record 'merged' idempotently."""
    from boss_engine.services.github_adapter import GitHubMergeRejectedError

    pid, bound, branch, run_id = _project(engine, world)
    settings = get_settings().model_copy(update={"sandbox_merge_method": method})
    adapter = LocalGitAdapter(settings, owner=bound["owner"], repo=bound["repo"],
                              project_id=pid, engine=engine)
    pr = adapter.create_pull_request(branch, f"Desk timer ({method})", "", run_id=run_id)
    snap_before = adapter.pull_request_snapshot(pr.number)

    with pytest.raises(RuntimeError, match="forced caller rollback"):
        with boss_db.begin(engine) as c:
            with local_repo_host.writes_on(c):
                adapter.merge_pull_request(pr.number, f"Merge ({method})",
                                           head_sha=snap_before["head_sha"])
                raise RuntimeError("forced caller rollback")

    assert _change(engine, bound, pr.number)["state"] == "open"
    assert adapter.read_text_file("main", "src/app.js") == "two\n"

    # Retrying via merge_pull_request_guarded with the pre-rollback expected_base_sha must succeed.
    res = adapter.merge_pull_request_guarded(
        pull_number=pr.number,
        expected_head_sha=snap_before["head_sha"],
        expected_base_sha=snap_before["base_sha"],
        expected_head_ref=snap_before["head_ref"],
        expected_base_ref=snap_before["base_ref"],
        merge_method=method,
        commit_title=f"Merge ({method})",
    )
    assert res["merged"] is True
    assert _change(engine, bound, pr.number)["state"] == "merged"

    # Re-calling merge_pull_request_guarded on the now-merged PR stays idempotent.
    again = adapter.merge_pull_request_guarded(
        pull_number=pr.number,
        expected_head_sha=snap_before["head_sha"],
        expected_base_sha=snap_before["base_sha"],
        expected_head_ref=snap_before["head_ref"],
        expected_base_ref=snap_before["base_ref"],
        merge_method=method,
        commit_title=f"Merge ({method})",
    )
    assert again["merged"] is True

    # Moving base via an UNRELATED commit must still reject guarded merge.
    pid2, bound2, branch2, run2 = _project(engine, world)
    adapter2 = LocalGitAdapter(settings, owner=bound2["owner"], repo=bound2["repo"],
                               project_id=pid2, engine=engine)
    pr2 = adapter2.create_pull_request(branch2, "Other", "", run_id=run2)
    snap2 = adapter2.pull_request_snapshot(pr2.number)
    local_repo.commit_files(adapter2.git_dir, "main", {"other.txt": "moved\n"}, "unrelated")
    with pytest.raises(GitHubMergeRejectedError, match="moved after it was approved"):
        adapter2.merge_pull_request_guarded(
            pull_number=pr2.number,
            expected_head_sha=snap2["head_sha"],
            expected_base_sha=snap2["base_sha"],
            expected_head_ref=snap2["head_ref"],
            expected_base_ref=snap2["base_ref"],
            merge_method=method,
            commit_title="Other",
        )
