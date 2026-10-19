"""A release approval never ships code the person can't see; a fix never rides another run's branch.

THE D2 CASE (incident run wfr_d43ec9d0). The Order API's incident fix landed on the app's shared
PR #231 branch. Its release would have shipped that branch's head: 15 commits beyond the serving
commit, 14 of them from two runs whose release approvals had been REJECTED (crun_1f5f2883,
crun_6722492b). The run's reviewers saw only its own one-line change, and the release card said
nothing of the other fourteen.

These reproduce it and pin the fix (services/release_scope.py):

  * the fix is cut from the serving commit on a branch of its own, and its PR is opened against
    a base at that commit — never the lineage branch that carries the rejected commits;
  * the release card lists every commit beyond the serving commit, its run, that run's approval
    and whether it was reviewed, and says plainly that 14 come from rejected runs and that this
    run's reviewers saw only its own change;
  * a plain approval of that card is refused: it needs the hash of the list it showed, and a
    stated reason (forced, as a merge with checks failing);
  * no environment policy ships such a release unasked, and the deploy ships exactly the head
    the approved card listed.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import text

from ._obs_seed import pg_engine, project
from boss_engine.services import code_task_service as cts
from boss_engine.services import decision_actions as da
from boss_engine.services import release_scope as rs

SERVING = "5" * 40


def _sha(i: int) -> str:
    return f"{i:040x}"


# ── 1. The fix's own branch ─────────────────────────────────────────────────────────────────
PARENT = {
    "id": "crun_root", "project_id": "p", "task_id": None, "title": "Order API",
    "spec": {"owner": "acme", "repo": "sandbox", "risk_tier": 1, "base_branch": "main"},
    "finalize_ctx": {"root_run_id": "crun_root"},
}


@pytest.fixture()
def started(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(cts, "start_code_task", lambda c, **kw: captured.update(kw) or "crun_fix")
    monkeypatch.setattr(cts, "load_code_task_run", lambda _c, _r: json.loads(json.dumps(PARENT)))
    return captured


def test_an_incident_fix_is_cut_from_the_serving_commit_not_the_shared_branch(started):
    """Without release_base_sha the follow-up builds on boss/code/crun_root — PR #231's branch,
    with the rejected runs' commits on it. That is the D2 bug, kept as the contrast."""
    shared = cts.start_followup_task(None, parent_run_id="crun_root", instructions="x",
                                     jobs=None, workflow_run_id="wfr_other")
    assert shared["base_branch"] == "boss/code/crun_root"

    out = cts.start_followup_task(None, parent_run_id="crun_root", instructions="fix pricing",
                                  jobs=None, workflow_run_id="wfr_d43ec9d0",
                                  release_base_sha=SERVING)
    rb = started["finalize_ctx"][rs.FCTX_KEY]
    assert rb == {"branch": "boss/fix/wfr_d43ec9d0", "base_sha": SERVING,
                  "pr_base": "boss/serving/wfr_d43ec9d0"}
    assert started["base_branch"] == out["base_branch"] == "boss/fix/wfr_d43ec9d0"
    # Still the same app: lineage and app id are kept, only the branch is its own.
    assert started["finalize_ctx"]["root_run_id"] == "crun_root"


class FakeHost:
    def __init__(self, existing=()):
        self.refs = {b: "lineage-head" for b in existing}

    def branch_exists(self, name):
        return name in self.refs

    def create_branch_ref(self, name, sha):
        self.refs[name] = sha
        return {"ref": name, "commit_sha": sha}


def test_the_fix_branch_and_its_pr_base_both_start_at_the_serving_commit():
    host = FakeHost(existing=["boss/code/crun_root"])
    rb = rs.release_branch("wfr_d43ec9d0", SERVING)
    assert rs.prepare_branch(host, rb) == "boss/fix/wfr_d43ec9d0"
    assert host.refs["boss/fix/wfr_d43ec9d0"] == SERVING
    assert host.refs["boss/serving/wfr_d43ec9d0"] == SERVING
    assert host.refs["boss/code/crun_root"] == "lineage-head"     # the shared branch untouched
    # A host that cannot cut a branch at a commit refuses: never a fall back to the shared one.
    with pytest.raises(rs.ReleaseBranchError):
        rs.prepare_branch(SimpleNamespace(), rb)


def test_the_pr_of_a_release_fix_is_opened_against_the_serving_base(monkeypatch):
    from boss_engine.services.github_adapter import GitHubAdapter

    sent = {}
    gh = object.__new__(GitHubAdapter)
    gh.owner, gh.repo = "acme", "sandbox"
    gh.settings = SimpleNamespace(open_pr_as_draft=False)
    monkeypatch.setattr(GitHubAdapter, "repository_full_name", property(lambda s: "acme/sandbox"))
    monkeypatch.setattr(gh, "default_branch_sha", lambda: ("main", "0" * 40), raising=False)

    def req(method, path, payload=None):
        if method == "GET":
            return []
        sent.update(payload or {})
        return {"html_url": "https://github.com/acme/sandbox/pull/240", "number": 240}
    monkeypatch.setattr(gh, "_request", req, raising=False)
    gh.create_pull_request("boss/fix/wfr_d43ec9d0", "Fix", "body",
                           base="boss/serving/wfr_d43ec9d0")
    assert sent["base"] == "boss/serving/wfr_d43ec9d0" and sent["head"] == "boss/fix/wfr_d43ec9d0"


def test_a_retry_of_the_fix_stays_on_the_fix_branch():
    src = open(cts.__file__, encoding="utf-8").read()
    retry = src[src.index("def retry_code_task("):]
    assert '"release_branch": dict(fctx_prev["release_branch"])' in retry


# ── 2. The release card (needs Postgres) ────────────────────────────────────────────────────
@pytest.fixture()
def pg():
    eng = pg_engine()
    with eng.begin() as c:
        pid = project(c)
    return eng, pid


def _run(c, pid, run_id, *, wfr=None, reviewed=True, title="build"):
    c.execute(text("INSERT INTO code_task_runs (id, project_id, title, status, state, finalize_ctx) "
                   "VALUES (:i, :p, :t, 'completed', CAST(:s AS jsonb), CAST(:f AS jsonb))"),
              {"i": run_id, "p": pid, "t": title,
               "s": json.dumps({"review_summary": "Looks right."} if reviewed else {}),
               "f": json.dumps({"root_run_id": "crun_root",
                                **({"workflow_run_id": wfr} if wfr else {})})})


def _release_decision(c, pid, run_id, status, tid):
    """The run's release: a stack deployment whose approval a person answered."""
    did = f"dec_{uuid4().hex[:10]}"
    c.execute(text("INSERT INTO decisions (id, project_id, question, status) "
                   "VALUES (:d, :p, 'Deploy?', :s)"), {"d": did, "p": pid, "s": status})
    c.execute(text("INSERT INTO stack_deployments (id, project_id, target_id, repository, ref, "
                   " commit_sha, manifest, status, created_by, decision_id, code_task_run_id) "
                   "VALUES (:i, :p, :t, 'acme/sandbox', 'pull/231', :c, '{}'::jsonb, :st, 'op', "
                   " :d, :r)"),
              {"i": f"sd_{uuid4().hex[:10]}", "p": pid, "t": tid, "c": _sha(99),
               "st": "rejected" if status == "rejected" else "healthy", "d": did, "r": run_id})


def _recorded_head(c, pid, run_id, sha):
    """The head BOSS recorded when the build opened or refreshed its PR."""
    cs = f"chs_{uuid4().hex[:8]}"
    c.execute(text("INSERT INTO repo_change_sets (id, project_id, app_key, title, mode, state) "
                   "VALUES (:i, :p, 'order-api', 'x', 'single', 'in_review')"), {"i": cs, "p": pid})
    c.execute(text("INSERT INTO change_set_prs (id, change_set_id, full_name, lane_state, "
                   " code_task_run_id, pr_number, pr_url, branch, head_sha) VALUES (:i, :c, "
                   " 'acme/sandbox', 'ready', :r, 231, 'u', 'boss/code/crun_root', :s)"),
              {"i": f"csp_{uuid4().hex[:8]}", "c": cs, "r": run_id, "s": sha})


def _d2(c, pid):
    """PR #231's branch beyond the serving commit: 7 commits of rejected run A (traced by its
    recorded head), 7 of rejected run B (traced by their Boss-Lineage trailer), 1 of this fix."""
    tid = f"t_{uuid4().hex[:10]}"
    c.execute(text(
        "INSERT INTO deployment_targets (id, project_id, app_name, repo, environment, target_kind, "
        " target_ref, status, created_by, domains) VALUES (:t, :p, 'order-api', 'acme/sandbox', "
        " 'staging', 'stack', 'order-api', 'active', 'test', '[]'::jsonb)"), {"t": tid, "p": pid})
    sfx = uuid4().hex[:6]
    a, b, fix = f"crun_1f5f2883{sfx}", f"crun_6722492b{sfx}", f"crun_fix{sfx}"
    _run(c, pid, a, wfr=f"wfr_a{sfx}")
    _run(c, pid, b, wfr=f"wfr_b{sfx}")
    _run(c, pid, fix, wfr=f"wfr_d43ec9d0{sfx}", title="Fix order-api: TypeError")
    _release_decision(c, pid, a, "rejected", tid)
    _release_decision(c, pid, b, "rejected", tid)
    commits = [{"sha": _sha(i), "message": f"stage {i}"} for i in range(1, 8)]
    _recorded_head(c, pid, a, _sha(7))
    commits += [{"sha": _sha(i), "message": f"stage {i}",
                 "full_message": f"stage {i}\n\nBoss-Lineage: autonomous/{b}"}
                for i in range(8, 15)]
    commits.append({"sha": _sha(15), "message": "fix: price a bulk SKU"})
    _recorded_head(c, pid, fix, _sha(15))
    return {"a": a, "b": b, "fix": fix, "wfr": f"wfr_d43ec9d0{sfx}", "commits": commits,
            "tid": tid}


def test_the_d2_release_card_lists_all_fifteen_commits_and_says_whose(pg):
    eng, pid = pg
    with eng.begin() as c:
        d2 = _d2(c, pid)
        card = rs.ship_list(c, lambda base, head: {"commits": d2["commits"]},
                            serving_sha=SERVING, head_sha=_sha(15),
                            own_workflow_run_id=d2["wfr"], repository="acme/sandbox",
                            project_id=pid)
    assert len(card["commits"]) == 15 and card["own_count"] == 1
    by_run = {}
    for commit in card["commits"]:
        by_run.setdefault(commit["run_id"], []).append(commit)
    assert len(by_run[d2["a"]]) == 7 and len(by_run[d2["b"]]) == 7 and len(by_run[d2["fix"]]) == 1
    assert {c_["approval"] for c_ in by_run[d2["a"]] + by_run[d2["b"]]} == {"rejected"}
    assert card["rejected_runs"] == sorted([d2["a"], d2["b"]])
    assert card["beyond_reviewers"] and card["needs_reason"]
    words = card["sentence"]
    assert "Ships 15 commits beyond the serving commit 5555555: 1 from this run, 14 from 2 " \
           "other runs." in words
    assert "14 come from runs whose release was REJECTED" in words
    assert "reviewers saw only its own 1 commit; the release ships 14 they did not see" in words
    # On the console, as rows bound by the list's hash.
    rec = rs.attach({"capability": "release_approval"}, card, step_label="Approve the release")
    assert rec["action_preview"]["content_hash"] == card["content_hash"]
    assert len(rec["action_preview"]["fields"]) == 16
    assert "REJECTED" in rec["action_preview"]["fields"][0]["value"]


def test_a_fix_on_its_own_branch_ships_only_itself_and_needs_no_reason(pg):
    eng, pid = pg
    with eng.begin() as c:
        d2 = _d2(c, pid)
        card = rs.ship_list(c, lambda base, head: {"commits": d2["commits"][-1:]},
                            serving_sha=SERVING, head_sha=_sha(15),
                            own_workflow_run_id=d2["wfr"], project_id=pid)
    assert len(card["commits"]) == 1 and card["own_count"] == 1
    assert not card["beyond_reviewers"] and not card["needs_reason"]
    assert card["sentence"] == ("Ships 1 commit beyond the serving commit 5555555: "
                                "1 from this run.")


def test_a_list_that_cannot_be_read_is_said_and_needs_a_reason(pg):
    eng, pid = pg

    def broken(base, head):
        raise RuntimeError("GitHub answered 502")
    with eng.begin() as c:
        card = rs.ship_list(c, broken, serving_sha=SERVING, head_sha=_sha(15))
    assert card["known"] is False and card["needs_reason"]
    assert "could not list what this release would ship" in card["sentence"]


# ── 3. Approving the card ───────────────────────────────────────────────────────────────────
def _card_decision(c, pid, card) -> str:
    from boss_engine.services.core import create_blocking_decision

    rec = rs.attach({"capability": "release_approval", "step_label": "Approve the release"},
                    card, step_label="Approve the release")
    return create_blocking_decision(c, pid, None, f"Approve the release {uuid4().hex[:6]}", rec,
                                    {}, "workflow_gate", risk_tier=2)


def _service():
    from boss_engine.config import get_settings
    from boss_engine.services.decisions import DecisionService

    service = DecisionService(get_settings())
    service.members = SimpleNamespace(require_decision_actor=lambda *_a, **_k: None)
    return service


def test_a_plain_approval_of_the_d2_card_is_refused(pg):
    from boss_engine.services.decisions import InvalidDecisionTransitionError

    eng, pid = pg
    with eng.begin() as c:
        d2 = _d2(c, pid)
        card = rs.ship_list(c, lambda b, h: {"commits": d2["commits"]}, serving_sha=SERVING,
                            head_sha=_sha(15), own_workflow_run_id=d2["wfr"], project_id=pid)
        did = _card_decision(c, pid, card)
        offered = da.allowed_actions(dict(c.execute(text(
            "SELECT * FROM decisions WHERE id = :d"), {"d": did}).mappings().one()))
    assert offered[0]["label"] == "Ship with unreviewed commits"
    assert offered[0]["needs"] == "note"
    service = _service()
    for kwargs, why in (({}, "was not of that list"),
                        ({"content_hash": "sha256:stale"}, "was not of that list"),
                        ({"content_hash": card["content_hash"]}, "Say why")):
        with eng.begin() as c, pytest.raises(InvalidDecisionTransitionError, match=why):
            service.transition(c, did, action="approve", actor="ceo",
                               note="Resolved from the BOSS console.", **kwargs)
    with eng.begin() as c:
        service.transition(c, did, action="approve", actor="ceo", force=True,
                           note="Order API is down; the rejected work is reverted next.",
                           content_hash=card["content_hash"])
        status = c.execute(text("SELECT status FROM decisions WHERE id = :d"),
                           {"d": did}).scalar()
        said = c.execute(text("SELECT data FROM events WHERE entity_id = :d AND event_type = :k"),
                         {"d": did, "k": rs.FORCED_EVENT}).scalar()
    assert status == "approved"
    assert said["rejected_runs"] == sorted([d2["a"], d2["b"]]) and said["reason"]


def test_a_clean_release_card_is_approved_with_its_hash(pg):
    eng, pid = pg
    with eng.begin() as c:
        d2 = _d2(c, pid)
        card = rs.ship_list(c, lambda b, h: {"commits": d2["commits"][-1:]},
                            serving_sha=SERVING, head_sha=_sha(15),
                            own_workflow_run_id=d2["wfr"], project_id=pid)
        did = _card_decision(c, pid, card)
    with eng.begin() as c:
        _service().transition(c, did, action="approve", actor="ceo", note=None,
                              content_hash=card["content_hash"])
        assert c.execute(text("SELECT status FROM decisions WHERE id = :d"),
                         {"d": did}).scalar() == "approved"


# ── 4. What the run feeds the build, and what the deploy ships ──────────────────────────────
def test_an_incident_runs_build_is_given_the_serving_commit():
    from boss_engine.services.workflow_capability_runner import release_base_for

    ctx = {"input": {"incident_id": "inc_1"},
           "steps": {"investigate": {"serving_commit_sha": SERVING}}}
    assert release_base_for(None, ctx) == SERVING
    assert release_base_for(None, {"input": {"release_base_sha": "a" * 40}}) == "a" * 40
    assert release_base_for(None, {"input": {"title": "a feature"}}) is None


def test_no_policy_ships_a_release_with_rejected_commits_and_the_deploy_pins_the_head():
    """Read off the code, as the plumbing is: the stack plan drops a policy's approval when the
    card needs a reason (unless a person's card listed exactly this head), and the workflow's
    deploy ships the approved card's head."""
    from boss_engine.services import stack_deploy, workflow_capability_runner

    plan = open(stack_deploy.__file__, encoding="utf-8").read()
    assert "NO POLICY SHIPS REJECTED OR UNREVIEWED COMMITS" in plan
    assert 'gate.get("head_sha") == commit' in plan
    runner = open(workflow_capability_runner.__file__, encoding="utf-8").read()
    assert "release_scope.approved_head(connection, run_id)" in runner
