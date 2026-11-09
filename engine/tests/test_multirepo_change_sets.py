"""Multi-repo phases 3–5: one change across acme/contracts → acme/api → acme/web.

Real Postgres and the product's own state machine; the builds, GitHub and the planner/reviewer
model are stand-ins that record what they were asked. Adversarial cases first-class: one repository
failing blocks the set; a push after the question (or after the approval) re-asks; a merge that
fails mid-set reverts what landed; a run never reaches a repository outside its app."""
from __future__ import annotations

import itertools
import json
import uuid
from typing import Any

import pytest
from sqlalchemy import text

from boss_engine.config import get_settings
from boss_engine.multirepo import change_sets, components, cross_check, github, grants, merge, project_repos, \
    registry, release
from boss_engine.multirepo.common import MultiRepoError

from ._multirepo_support import ACME, lister_for, make_engine, make_world, prober_for

_sha = itertools.count(1000)


def new_sha() -> str:
    return f"{next(_sha):040x}"


class FakeGitHub:
    """Pull requests, refs and commits per repository; merges at an exact SHA; revert plumbing."""

    def __init__(self):
        self.repos: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, str]] = []
        self.refuse_merge: set[str] = set()

    def repo(self, full):
        return self.repos.setdefault(full, {"pulls": {}, "refs": {"main": new_sha()}, "commits": {}, "next": 1})

    def open_pr(self, full, branch) -> tuple[int, str]:
        r = self.repo(full)
        n = r["next"]
        r["next"] += 1
        sha = new_sha()
        r["pulls"][n] = {"number": n, "head": {"sha": sha, "ref": branch}, "base": {"ref": "main"}, "body": "",
                         "merged": False, "additions": 10, "deletions": 2, "changed_files": 3}
        return n, sha

    def push(self, full, n) -> str:
        sha = new_sha()
        self.repo(full)["pulls"][n]["head"]["sha"] = sha
        return sha

    def __call__(self, method, path, token, body=None):
        self.calls.append((method, path))
        parts = path.split("/")
        full = f"{parts[2]}/{parts[3]}"
        r = self.repo(full)
        rest = "/" + "/".join(parts[4:])
        if rest.startswith("/pulls/") and rest.endswith("/merge") and method == "PUT":
            n = int(rest.split("/")[2])
            pr = r["pulls"][n]
            if full in self.refuse_merge:
                return github.Answer(405, {"message": "Base branch was modified. Review and try the merge again."})
            if body.get("sha") != pr["head"]["sha"]:
                return github.Answer(409, {"message": "Head branch was modified."})
            old = r["refs"][pr["base"]["ref"]]
            merged = new_sha()
            r["commits"][merged] = {"sha": merged, "parents": [{"sha": old}], "tree": {"sha": "t" + merged[:8]}}
            r["commits"].setdefault(old, {"sha": old, "parents": [], "tree": {"sha": "t" + old[:8]}})
            r["refs"][pr["base"]["ref"]] = merged
            pr["merged"] = True
            return github.Answer(200, {"merged": True, "sha": merged})
        if rest.startswith("/pulls/") and rest.endswith("/files"):
            return github.Answer(200, [{"filename": "x.go", "additions": 1, "deletions": 0, "patch": "+ok"}])
        if rest.startswith("/pulls/") and method == "GET":
            return github.Answer(200, r["pulls"][int(rest.split("/")[2])])
        if rest.startswith("/pulls/") and method == "PATCH":
            r["pulls"][int(rest.split("/")[2])]["body"] = body["body"]
            return github.Answer(200, {})
        if rest.startswith("/git/ref/heads/"):
            return github.Answer(200, {"object": {"sha": r["refs"][rest.split("/heads/")[1]]}})
        if rest.startswith("/git/commits/") and method == "GET":
            return github.Answer(200, r["commits"][rest.split("/")[-1]])
        if rest == "/git/commits" and method == "POST":
            sha = new_sha()
            r["commits"][sha] = {"sha": sha, "parents": [{"sha": p} for p in body["parents"]], "tree": {"sha": body["tree"]}}
            return github.Answer(201, {"sha": sha})
        if rest == "/git/refs" and method == "POST":
            r["refs"][body["ref"].split("refs/heads/")[1]] = body["sha"]
            return github.Answer(201, {})
        if rest == "/pulls" and method == "POST":
            n = r["next"]
            r["next"] += 1
            r["pulls"][n] = {"number": n, "head": {"sha": r["refs"][body["head"]], "ref": body["head"]},
                             "base": {"ref": body["base"]}, "body": body.get("body"), "merged": False}
            return github.Answer(201, {"number": n})
        return github.Answer(404, {"message": f"fake: {method} {path}"})


class Jobs:
    def __init__(self):
        self.created: list[dict[str, Any]] = []

    def create_job(self, db, *, project_id, task_id, kind, priority, payload):
        self.created.append({"kind": kind, "payload": payload})
        return {"id": f"job_{len(self.created)}"}


@pytest.fixture(scope="module")
def eng():
    return make_engine()


@pytest.fixture()
def gh(monkeypatch):
    fake = FakeGitHub()
    monkeypatch.setattr(github, "TRANSPORT", fake)
    monkeypatch.setattr(github, "TOKEN_FOR", lambda db, s, p, f: "ghs_scoped_" + f.replace("/", "_"))
    return fake


@pytest.fixture()
def model(monkeypatch):
    """The planner and the cross-repo reviewer. `script` decides each answer."""
    state = {"plan": None, "checks": [{"name": "api implements every new contract endpoint", "detail": "3 of 3",
                                       "pass": True}], "raise": False, "prompts": []}

    def answer(db, settings, project_id, ref, prompt):
        state["prompts"].append(prompt[:40])
        if state["raise"]:
            raise RuntimeError("model down")
        if prompt.startswith("BOSS MULTI-REPO PLAN"):
            return json.dumps(state["plan"] or {"summary": "saved carts", "components": [
                {"name": "Contracts", "change": "Add SavedCart", "interface": "SavedCart message"},
                {"name": "Backend", "change": "PUT/GET /carts/saved"},
                {"name": "Frontend", "change": "Save for later button"}]})
        return json.dumps({"checks": state["checks"]})

    monkeypatch.setattr(change_sets, "MODEL", answer)
    return state


@pytest.fixture()
def builds(monkeypatch, eng):
    """start_code_task records a run row and nothing else; tests finish runs by hand."""
    started: list[dict[str, Any]] = []

    def start_code_task(db, **kw):
        rid = f"crun_{uuid.uuid4().hex[:12]}"
        db.execute(text("INSERT INTO code_task_runs (id, project_id, title, status, phase, spec, finalize_ctx) VALUES "
                        "(:i, :p, :t, 'running', 'provisioning', CAST(:s AS JSONB), CAST(:f AS JSONB))"),
                   {"i": rid, "p": kw["project_id"], "t": kw["title"],
                    "s": json.dumps({"owner": kw["owner"], "repo": kw["repo"], "base_branch": kw["base_branch"]}),
                    "f": json.dumps(kw["finalize_ctx"] or {})})
        started.append({"run_id": rid, "repo": f"{kw['owner']}/{kw['repo']}", "instructions": kw["instructions"]})
        return rid

    from boss_engine.services import code_task_service

    monkeypatch.setattr(code_task_service, "start_code_task", start_code_task)
    return started


def _setup_app(eng, world) -> dict[str, Any]:
    pid = world["projects"][0]
    with eng.begin() as db:
        registry.refresh(db, get_settings(), pid, lister=lister_for(ACME))
        ids = {r["full_name"]: r["id"] for r in db.execute(text("SELECT id, full_name FROM github_repos WHERE org_id = :o"),
                                                          {"o": world["org"]}).mappings()}
        for n in ("acme/contracts", "acme/api", "acme/web", "acme/infra"):
            registry.set_allowed(db, get_settings(), pid, ids[n], True, prober=prober_for({"acme/infra": "read_only"}))
        b = {n: project_repos.add(db, get_settings(), pid, registry_repo_id=ids[n], check=False)["binding_id"]
             for n in ("acme/contracts", "acme/api", "acme/web", "acme/infra")}
        components.put(db, pid, "storefront", [
            {"name": "Contracts", "binding_id": b["acme/contracts"], "ship_target": {"kind": "none"}},
            {"name": "Backend", "binding_id": b["acme/api"], "depends_on": ["Contracts"], "ship_target": {"kind": "none"}},
            {"name": "Frontend", "binding_id": b["acme/web"], "depends_on": ["Contracts", "Backend"],
             "ship_target": {"kind": "none"}},
            {"name": "Deploy config", "binding_id": b["acme/infra"], "kind": "deploy_config"}], app_name="Storefront")
    return {"pid": pid, "bindings": b}


def _start(eng, pid, jobs) -> str:
    with eng.begin() as db:
        return change_sets.start(db, get_settings(), jobs, project_id=pid, app_key="storefront",
                                 title="Storefront: Add saved carts", instructions="Let shoppers save their cart",
                                 actor="test")["id"]


def _advance(eng, cs_id, jobs):
    with eng.begin() as db:
        return change_sets.advance(db, get_settings(), jobs, cs_id)


def _finish(eng, gh, run, *, fail: str | None = None):
    """The lane's build ends: PR opened with gates green, or failed with a reason."""
    with eng.begin() as db:
        if fail:
            db.execute(text("UPDATE code_task_runs SET status = 'failed', phase = 'done', error = :e WHERE id = :i"),
                       {"e": fail, "i": run["run_id"]})
            return None
        n, _sha = gh.open_pr(run["repo"], f"boss/code/{run['run_id']}")
        db.execute(text("UPDATE code_task_runs SET status = 'completed', phase = 'done', pull_request_number = :n, "
                        "pull_request_url = :u WHERE id = :i"),
                   {"n": n, "u": f"https://github.com/{run['repo']}/pull/{n}", "i": run["run_id"]})
        return n


def _view(eng, cs_id):
    with eng.begin() as db:
        return change_sets.view(db, cs_id)


def _ready_set(eng, gh, builds, jobs) -> tuple[str, dict[str, Any]]:
    """Plan, build all three in waves, link, check, ask. Returns (set id, view)."""
    world = make_world(eng)
    app = _setup_app(eng, world)
    cs_id = _start(eng, app["pid"], jobs)
    for _ in range(3):
        _advance(eng, cs_id, jobs)
        for run in [b for b in builds if not b.get("done")]:
            run["done"] = True
            _finish(eng, gh, run)
    _advance(eng, cs_id, jobs)
    return cs_id, _view(eng, cs_id)


# ── the whole path ───────────────────────────────────────────────────────────────────────────

def test_three_repositories_build_in_waves_link_their_prs_and_ask_once(eng, gh, model, builds):
    jobs = Jobs()
    world = make_world(eng)
    app = _setup_app(eng, world)
    cs_id = _start(eng, app["pid"], jobs)
    assert _view(eng, cs_id)["state"] == "planned"

    _advance(eng, cs_id, jobs)                                   # plan + wave 1
    assert [b["repo"] for b in builds] == ["acme/contracts"]     # api waits for contracts
    v = _view(eng, cs_id)
    assert v["state"] == "building" and v["plan"]["by"] == "model"
    lanes = {lane["full_name"]: lane for lane in v["lanes"]}
    assert lanes["acme/web"]["waiting_for"] == ["contracts", "api"]

    n1 = _finish(eng, gh, builds[0])
    _advance(eng, cs_id, jobs)
    assert [b["repo"] for b in builds] == ["acme/contracts", "acme/api"]
    assert f"acme/contracts PR #{n1}" in builds[1]["instructions"]   # built against the dependency
    _finish(eng, gh, builds[1])
    _advance(eng, cs_id, jobs)
    assert [b["repo"] for b in builds][-1] == "acme/web"
    _finish(eng, gh, builds[2])
    _advance(eng, cs_id, jobs)

    v = _view(eng, cs_id)
    assert v["state"] == "in_review" and v["cross_check"]["verdict"] == "pass"
    for full in ("acme/contracts", "acme/api", "acme/web"):         # every PR lists the set
        body = gh.repos[full]["pulls"][1]["body"]
        assert "<!-- boss:change-set -->" in body and "Do not merge this pull request on its own" in body
    with eng.begin() as db:
        decisions = db.execute(text("SELECT id, recommendation FROM decisions WHERE project_id = :p "
                                    "AND recommendation->>'action' = 'approve_change_set'"), {"p": app["pid"]}).all()
    assert len(decisions) == 1                                   # ONE decision for three PRs
    rec = decisions[0][1]
    assert sorted(rec["head_shas"]) == ["acme/api", "acme/contracts", "acme/web"]
    assert rec["action_preview"]["content_hash"] == change_sets.content_hash(rec["head_shas"])
    assert not any(b["repo"] == "acme/infra" for b in builds)   # read-only deploy config is never built


def test_the_approval_merges_the_set_in_order_at_the_approved_shas(eng, gh, model, builds):
    jobs = Jobs()
    cs_id, v = _ready_set(eng, gh, builds, jobs)
    _approve(eng, cs_id, jobs)
    assert jobs.created[-1]["kind"] == "change_set.merge"
    with eng.begin() as db:
        out = merge.merge_set(db, get_settings(), cs_id)
    assert out["status"] == "merged"
    merges = [p for m, p in gh.calls if m == "PUT" and p.endswith("/merge")]
    assert [p.split("/")[3] for p in merges] == ["contracts", "api", "web"]
    v = _view(eng, cs_id)
    assert v["state"] == "live"                                  # everything ships with the merge here
    assert all(lane["lane_state"] == "merged" for lane in v["lanes"])
    assert v["grant"]["ended_at"]                                # access ends with the run


def _approve(eng, cs_id, jobs, *, decision_id=None):
    with eng.begin() as db:
        row = dict(db.execute(text("SELECT * FROM decisions WHERE id = :d"),
                              {"d": decision_id or change_sets._row(db, cs_id)["decision_id"]}).mappings().first())
        # As DecisionService does: the decision is approved first, then the follow-on runs.
        db.execute(text("UPDATE decisions SET status = 'approved' WHERE id = :d"), {"d": row["id"]})
        change_sets.on_decision(db, row, status="approved", actor="usr_approver", jobs=jobs)


# ── adversarial ──────────────────────────────────────────────────────────────────────────────

def test_one_repository_failing_blocks_the_whole_set_and_a_retry_moves_it_again(eng, gh, model, builds):
    jobs = Jobs()
    world = make_world(eng)
    app = _setup_app(eng, world)
    cs_id = _start(eng, app["pid"], jobs)
    _advance(eng, cs_id, jobs)
    _finish(eng, gh, builds[0])
    _advance(eng, cs_id, jobs)
    _finish(eng, gh, builds[1], fail="tests failed: 3 of 130")
    _advance(eng, cs_id, jobs)
    v = _view(eng, cs_id)
    assert v["state"] == "blocked" and "acme/api" in v["blocked_reason"] and "whole set waits" in v["blocked_reason"]
    assert v["decision"] is None                                 # nothing asks for approval
    assert [b["repo"] for b in builds] == ["acme/contracts", "acme/api"]   # web never started
    _advance(eng, cs_id, jobs)
    assert len(builds) == 2                                      # still blocked: nothing new starts
    # Factory's Retry: a new attempt of the failed build.
    with eng.begin() as db:
        retry = f"crun_{uuid.uuid4().hex[:12]}"
        db.execute(text("INSERT INTO code_task_runs (id, project_id, title, status, phase, finalize_ctx) VALUES "
                        "(:i, :p, 't', 'running', 'coding', CAST(:f AS JSONB))"),
                   {"i": retry, "p": app["pid"], "f": json.dumps({"parent_run_id": builds[1]["run_id"]})})
    _advance(eng, cs_id, jobs)
    v = _view(eng, cs_id)
    assert v["state"] == "building"
    assert next(lane for lane in v["lanes"] if lane["full_name"] == "acme/api")["code_task_run_id"] == retry


def test_a_failed_cross_repo_check_blocks_the_set_and_does_not_ask(eng, gh, model, builds):
    model["checks"] = [{"name": "web calls only what the contract defines", "pass": False,
                        "detail": "web calls DELETE /carts/saved/{id}; the contract has no such endpoint"}]
    jobs = Jobs()
    cs_id, v = _ready_set(eng, gh, builds, jobs)
    assert v["state"] == "blocked" and v["blocked_reason"].startswith("Cross-repo check failed")
    assert v["decision"] is None
    checked = len([p for p in model["prompts"] if p.startswith("BOSS CROSS-REPO")])
    _advance(eng, cs_id, jobs)                                   # same commits: not re-reviewed
    assert len([p for p in model["prompts"] if p.startswith("BOSS CROSS-REPO")]) == checked


def test_the_cross_repo_reviewer_fails_closed(eng, gh, model, builds):
    jobs = Jobs()
    world = make_world(eng)
    app = _setup_app(eng, world)
    cs_id = _start(eng, app["pid"], jobs)
    for _ in range(3):
        _advance(eng, cs_id, jobs)
        for run in [b for b in builds if not b.get("done")]:
            run["done"] = True
            _finish(eng, gh, run)
    model["raise"] = True
    _advance(eng, cs_id, jobs)
    v = _view(eng, cs_id)
    assert v["state"] == "blocked" and v["cross_check"]["verdict"] == "fail"
    assert any("could not run" in r["detail"] for r in v["cross_check"]["results"])


def test_a_push_after_the_question_withdraws_it_and_asks_again(eng, gh, model, builds):
    jobs = Jobs()
    cs_id, v = _ready_set(eng, gh, builds, jobs)
    first = v["decision"]["id"]
    gh.push("acme/api", 1)
    _advance(eng, cs_id, jobs)
    v = _view(eng, cs_id)
    assert v["state"] == "in_review" and v["decision"]["id"] != first
    with eng.begin() as db:
        assert db.execute(text("SELECT status FROM decisions WHERE id = :d"), {"d": first}).scalar() == "withdrawn"
    with pytest.raises(MultiRepoError) as err:                   # the old question cannot be approved
        _approve(eng, cs_id, jobs, decision_id=first)
    assert err.value.code == "stale_decision"


def test_a_push_after_the_approval_stops_the_merge_and_re_asks(eng, gh, model, builds):
    jobs = Jobs()
    cs_id, _v = _ready_set(eng, gh, builds, jobs)
    _approve(eng, cs_id, jobs)
    gh.push("acme/web", 1)                                       # a commit lands after "approve"
    with eng.begin() as db:
        out = merge.merge_set(db, get_settings(), cs_id)
    assert out["status"] == "stale" and out["moved"] == ["acme/web"]
    assert not [p for m, p in gh.calls if m == "PUT"]           # nothing merged
    v = _view(eng, cs_id)
    assert v["state"] == "building" and v["approved_shas"] is None
    _advance(eng, cs_id, jobs)
    assert _view(eng, cs_id)["state"] == "in_review"             # asked again, about the new head


def test_a_failed_merge_reverts_the_merges_that_landed(eng, gh, model, builds):
    jobs = Jobs()
    cs_id, _v = _ready_set(eng, gh, builds, jobs)
    _approve(eng, cs_id, jobs)
    gh.refuse_merge.add("acme/web")
    before = {f: dict(gh.repos[f]["refs"]) for f in ("acme/contracts", "acme/api")}
    with eng.begin() as db:
        out = merge.merge_set(db, get_settings(), cs_id)
    assert out["status"] == "failed" and out["failed"] == "acme/web"
    assert out["reverted"] == ["acme/api", "acme/contracts"]     # newest first
    for full in ("acme/contracts", "acme/api"):
        repo = gh.repos[full]
        head = repo["refs"]["main"]
        assert head != before[full]["main"]                      # a revert landed on top...
        # ...whose tree is the tree from before the set's merge: nothing of the set remains.
        assert repo["commits"][head]["tree"]["sha"] == repo["commits"][before[full]["main"]]["tree"]["sha"]
    v = _view(eng, cs_id)
    assert v["state"] == "failed" and "never" not in v["error"] and "Reverted" in v["error"]
    assert {lane["full_name"]: lane["lane_state"] for lane in v["lanes"]} == {
        "acme/contracts": "reverted", "acme/api": "reverted", "acme/web": "ready"}


def test_a_merge_is_refused_before_the_approval(eng, gh, model, builds):
    jobs = Jobs()
    cs_id, _v = _ready_set(eng, gh, builds, jobs)
    with eng.begin() as db:
        with pytest.raises(MultiRepoError) as err:
            merge.merge_set(db, get_settings(), cs_id)
    assert err.value.code == "not_approved"


def test_a_withdrawn_approval_merges_nothing_even_if_the_set_says_approved(eng, gh, model, builds):
    jobs = Jobs()
    cs_id, _v = _ready_set(eng, gh, builds, jobs)
    _approve(eng, cs_id, jobs)
    with eng.begin() as db:
        db.execute(text("UPDATE decisions SET status = 'expired' WHERE id = :d"),
                   {"d": change_sets._row(db, cs_id)["decision_id"]})
        with pytest.raises(MultiRepoError) as err:
            merge.merge_set(db, get_settings(), cs_id)
    assert err.value.code == "not_approved"
    assert not [p for m, p in gh.calls if m == "PUT"]


def test_the_worker_merges_only_under_its_jobs_lease_for_this_set_and_decision(eng, gh, model, builds,
                                                                                 monkeypatch):
    from boss_engine.services import jobs as jobs_module

    jobs = Jobs()
    cs_id, _v = _ready_set(eng, gh, builds, jobs)
    _approve(eng, cs_id, jobs)
    queued = jobs.created[-1]
    with eng.begin() as db:
        project_id = change_sets._row(db, cs_id)["project_id"]
    leased = {"job_1": {"id": "job_1", "kind": "change_set.merge", "project_id": project_id,
                        "payload": queued["payload"]},
              "job_other": {"id": "job_other", "kind": "change_set.merge", "project_id": project_id,
                            "payload": {**queued["payload"], "change_set_id": "chs_other"}},
              "job_old": {"id": "job_old", "kind": "change_set.merge", "project_id": project_id,
                          "payload": {**queued["payload"], "decision_id": "dec_older"}},
              "job_kind": {"id": "job_kind", "kind": "code.task", "project_id": project_id,
                           "payload": queued["payload"]}}

    def lease(self, db, worker_id, job_id, lease_token):
        if lease_token != "live" or job_id not in leased:
            raise jobs_module.LeaseNotFoundError("no live lease")
        return leased[job_id]

    monkeypatch.setattr(jobs_module.JobLeaseService, "require_active_lease", lease)

    def run(job_id, token="live"):
        with eng.begin() as db:
            return merge.execute_job(db, get_settings(), cs_id, job_id=job_id, worker_id="w", lease_token=token)

    for job_id, token, code in (("job_1", "stolen", "lease_lost"), ("job_other", "live", "wrong_job"),
                                ("job_kind", "live", "wrong_job"), ("job_old", "live", "stale_decision")):
        with pytest.raises(MultiRepoError) as err:
            run(job_id, token)
        assert err.value.code == code, job_id
    assert not [p for m, p in gh.calls if m == "PUT"]           # nothing merged on any refusal
    out = run("job_1")
    assert out["status"] == "merged" and out["project_id"] == project_id
    assert run("job_1")["already"] is True                       # a re-delivered job is a no-op


# ── the grant ────────────────────────────────────────────────────────────────────────────────

def test_a_lane_never_reaches_a_repository_outside_its_app(eng, gh, model, builds):
    jobs = Jobs()
    world = make_world(eng)
    app = _setup_app(eng, world)
    cs_id = _start(eng, app["pid"], jobs)
    _advance(eng, cs_id, jobs)
    run = builds[0]["run_id"]
    with eng.begin() as db:
        g = grants.get_for_change_set(db, cs_id)
        assert sorted((r["full_name"], r["access"]) for r in g["repos"]) == [
            ("acme/api", "write"), ("acme/contracts", "write"), ("acme/infra", "read"), ("acme/web", "write")]
        assert grants.outside_run_grant(db, f"run_cs:{cs_id}/ct:{run}", ["acme/contracts"]) is False
        assert grants.outside_run_grant(db, f"run_cs:{cs_id}/ct:{run}", ["acme/mobile"]) is True
        assert grants.outside_run_grant(db, f"run_wf:w1/ct:{run}", ["acme/payments"]) is True
        # read-only repositories are never write-minted for a build
        assert grants.outside_run_grant(db, f"run_ct:{run}", ["acme/infra"]) is True
        # a run that is not part of a multi-repo set is not this check's to refuse
        assert grants.outside_run_grant(db, "run_ct:crun_unrelated", ["acme/mobile"]) is False
        db.execute(text("DELETE FROM repo_run_grants WHERE change_set_id = :c"), {"c": cs_id})
        assert grants.outside_run_grant(db, f"run_ct:{run}", ["acme/contracts"]) is True   # no grant: fail closed


def test_the_identity_mint_refuses_outside_the_app(eng, gh, model, builds):
    """The token mint itself (identity/github_app.mint_for_run) enforces it, not a prompt."""
    from boss_engine.identity import github_app
    from boss_engine.identity.principal import TaskBinding

    jobs = Jobs()
    world = make_world(eng)
    app = _setup_app(eng, world)
    cs_id = _start(eng, app["pid"], jobs)
    _advance(eng, cs_id, jobs)
    run = builds[0]["run_id"]

    class Claims:
        org_id = world["org"]
        grant_id = None

    with eng.begin() as db:
        task = TaskBinding(run_id=f"run_cs:{cs_id}/ct:{run}", project_id=app["pid"],
                           resources={"github": ["acme/mobile"]})
        with pytest.raises(github_app.GitHubRefused) as err:
            github_app.mint_for_run(Claims(), task=task, db=db)
        assert err.value.code in ("repo_not_allowed", "repo_outside_app")
        task = TaskBinding(run_id=f"run_cs:{cs_id}/ct:{run}", project_id=app["pid"],
                           resources={"github": ["acme/legacy-admin"]})
        with pytest.raises(github_app.GitHubRefused):
            github_app.mint_for_run(Claims(), task=task, db=db)


def test_consent_asks_for_exactly_the_apps_repositories(eng, gh, model, builds):
    world = make_world(eng)
    app = _setup_app(eng, world)
    with eng.begin() as db:
        d = grants.consent_details(db, app["pid"], "storefront", run_label="Add saved carts")
    assert [(r["full_name"], r["access"]) for r in d["repos"]] == [
        ("acme/api", "write"), ("acme/contracts", "write"), ("acme/web", "write"), ("acme/infra", "read")]
    assert set(d["unreachable"]) == {"acme/mobile", "acme/legacy-admin"}
    assert "1 hour" in d["lasts"] and "push to a base branch" in d["never"]


# ── single-repo regression ───────────────────────────────────────────────────────────────────

def test_a_single_repo_app_is_not_a_multi_repo_change(eng, gh, model, builds):
    world = make_world(eng)
    app = _setup_app(eng, world)
    with eng.begin() as db:
        components.put(db, app["pid"], "notes", [{"name": "App", "binding_id": app["bindings"]["acme/web"]}],
                       app_name="Notes")
        assert components.is_multi(db, app["pid"], "notes") is False
        assert project_repos.repo_mode(db, app["pid"], "notes") == "single"
        with pytest.raises(MultiRepoError) as err:
            change_sets.start(db, get_settings(), Jobs(), project_id=app["pid"], app_key="notes", title="x",
                              instructions="x")
        assert err.value.code == "not_multi"
        # An ordinary build's PR is recorded as a one-PR change; its own decision stays its own.
        rid = f"crun_{uuid.uuid4().hex[:10]}"
        db.execute(text("INSERT INTO code_task_runs (id, project_id, title, status) VALUES (:i, :p, 'Notes: x', 'completed')"),
                   {"i": rid, "p": app["pid"]})
        cs = change_sets.record_single(db, run_id=rid, project_id=app["pid"], full_name="acme/web", pr_number=7,
                                       pr_url="u", branch=f"boss/code/{rid}", head_sha="abc", decision_id=None,
                                       title="Notes: x")
        assert change_sets.member_change_set(db, rid) is None    # not a lane: keeps its own approval
        assert change_sets.for_run(db, rid) == cs
        assert grants.outside_run_grant(db, f"run_ct:{rid}", ["acme/anything"]) is False
        change_sets.mark_single(db, rid, "merged")
        assert change_sets.view(db, cs)["state"] == "merged"


def test_the_builder_tool_starts_a_change_set_only_for_a_multi_repo_app(eng, gh, model, builds, monkeypatch):
    from boss_engine.tools import builtin

    world = make_world(eng)
    app = _setup_app(eng, world)

    class Ctx:
        settings = get_settings()
        jobs = Jobs()

        def begin(self):
            return eng.begin()

    out = builtin._multirepo_change(Ctx(), app["pid"], "workflow_runner",
                                    {"title": "Storefront: add saved carts", "instructions": "x"}, "", {})
    assert out["multirepo"] is True and out["change_set_id"]
    assert builtin._multirepo_change(Ctx(), app["pid"], "workflow_runner",
                                     {"title": "Some other app", "instructions": "x"}, "", {}) is None


# ── deploy together ──────────────────────────────────────────────────────────────────────────

def test_a_deploy_plan_refuses_to_start_when_a_component_has_nowhere_to_ship(eng, gh, model, builds):
    jobs = Jobs()
    cs_id, _v = _ready_set(eng, gh, builds, jobs)
    with eng.begin() as db:
        cs = change_sets._row(db, cs_id)
        comps = components.get(db, cs["project_id"], "storefront")
        lst = [{**c, "binding_id": c["binding_id"]} for c in comps["components"] + comps["deploy_config"]]
        for c in lst:
            if c["name"] == "Backend":
                c["ship_target"] = {"kind": "host"}
        db.execute(text("UPDATE repo_change_sets SET state = 'merged' WHERE id = :i"), {"i": cs_id})
        db.execute(text("UPDATE app_components SET ship_target = '{\"kind\": \"host\"}' WHERE project_id = :p AND "
                        "app_key = 'storefront' AND name = 'Backend'"), {"p": cs["project_id"]})
        p = release.plan(db, cs_id)
        assert p["ready"] is False and "Backend has nowhere to ship" in p["problems"][0]
        assert [w["kind"] for w in p["waves"]] == ["merge", "host", "merge"]
        with pytest.raises(MultiRepoError) as err:
            release.start(db, get_settings(), cs_id, actor="t")
        assert err.value.code == "plan_not_ready"


def test_a_person_without_a_covering_grant_gets_the_consent_sheet_with_exactly_the_apps_repositories(
        eng, gh, model, builds, monkeypatch):
    from boss_engine.identity import consent

    world = make_world(eng)
    app = _setup_app(eng, world)
    asked: dict[str, Any] = {}

    def require_authority(**kw):
        asked.update(kw)
        raise consent.ConsentRequired({"error": "consent_required", "github": {"repos": kw["repos"]}})

    monkeypatch.setattr(consent, "require_authority", require_authority)
    with pytest.raises(consent.ConsentRequired) as err:
        with eng.begin() as db:
            change_sets.start(db, get_settings(), Jobs(), project_id=app["pid"], app_key="storefront",
                              title="Storefront: Add saved carts", instructions="x")
    assert sorted(asked["repos"]) == ["acme/api", "acme/contracts", "acme/infra", "acme/web"]
    assert asked["run_id"].startswith("run_cs:")
    body = err.value.body["multirepo"]
    assert [r["full_name"] for r in body["repos"] if r["access"] == "write"] == ["acme/api", "acme/contracts", "acme/web"]
    assert set(body["unreachable"]) == {"acme/mobile", "acme/legacy-admin"}
    with eng.begin() as db:                                      # nothing was started
        assert db.execute(text("SELECT count(*) FROM repo_change_sets WHERE project_id = :p"),
                          {"p": app["pid"]}).scalar() == 0
