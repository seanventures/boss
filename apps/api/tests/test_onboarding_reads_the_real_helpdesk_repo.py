"""The onboarding agent, replayed on the REAL case it got wrong (BOSSAPP-913/914, run
onb_4ff778ddfdbe43ae on helpdesk-ai-demo).

The fixture is that repository's own files (package.json with four scripts, a README with a
getting-started block, boss.deploy.yaml, docker-compose.yml and the server files that read the
environment) and the tails of its two newest builds' verify output: a blocked follow-up whose
full suite ran 190 tests (71 pass, 119 failed in setup: the sandbox database refused the test
login) and the repair that ran 13 targeted tests.

What went wrong, and what each test pins:
  * the code graph was mapped (96 files) but the index's checkout was gone after an API restart,
    so the dossier read the graph's parsed-file list: "the repo declares no commands", "no
    README", and the secrets item said "the code is not mapped here" while the item above said it
    was → the agent now FETCHES the repository and reads its files; when it cannot, every item
    gives the same reason;
  * "71 / 190 · 119 failing" had no source → the baseline names it (the last full run, a blocked
    build);
  * those 119 were not the app's: one setup error, SQLSTATE 28P01, the test database BOSS gave
    the run refused its login (crun_de96bf12) → "BOSS couldn't run the full suite", owned by BOSS
    (Self-fixes), never "Fix the 119 failing tests" (suite_environment);
  * every real signal is scored by the Chief of Staff and Marshal picks the first work, which
    says why; an app's OWN red suite still comes first;
  * What's next showed 2 items and the Roadmap 0 → the proposals ARE the Roadmap's open
    recommendations, and accepting puts them in Next; a re-run never duplicates them.
Real Postgres; GitHub and the model are stubs (the seam the agent calls them through).
"""
from __future__ import annotations

import json
import shutil
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.app import main
from apps.api.tests.identity_fixtures import IdentityWorld
from apps.api.tests.test_the_onboarding_agent_readies_a_project import StubProbes
from boss_engine.config import get_settings
from boss_engine.services.project_experience import onboarding

FIXTURE = Path(__file__).parent / "fixtures" / "helpdesk_onboarding"
REPO = "acme/helpdesk-ai-demo"


class RealRepoProbes(StubProbes):
    """The HelpDesk repo as GitHub would answer: no CI, no open issues or PRs."""

    def __init__(self, root: Path, **kw):
        kw.setdefault("ci", "none")
        kw.setdefault("kept", False)       # the index's checkout is gone, as on the laptop
        kw.setdefault("fetched", True)
        super().__init__(root, **kw)

    def issues(self, reader):
        return []

    def pulls(self, reader):
        return []

    def tree_paths(self, connection, project_id, repo, branch):
        return (FIXTURE / "files.txt").read_text().split()


@pytest.fixture(scope="module")
def world(engine) -> IdentityWorld:
    w = IdentityWorld.create(engine)
    w.owner = w.person("onb-real-owner")
    w.bind(w.owner, "owner")
    return w


@pytest.fixture
def client():
    return TestClient(main.app, raise_server_exceptions=False)


@pytest.fixture
def repo(tmp_path) -> Path:
    """The repository as a checkout: the fixture's files at their paths, every other file of
    the real tree present (empty) so counts and paths are the real ones."""
    root = tmp_path / "helpdesk-ai-demo"
    for rel in (FIXTURE / "files.txt").read_text().split():
        dest = root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        src = FIXTURE / "repo" / rel
        if src.is_file():
            shutil.copyfile(src, dest)
        else:
            dest.write_text("")
    return root


def _project(world, client, engine) -> str:
    slug = f"hd-real-{uuid.uuid4().hex[:8]}"
    with world.acting_as(world.ctx(world.owner)):
        made = client.post("/v1/projects", json={"name": "HelpDesk AI Demo", "slug": slug})
    assert made.status_code == 201, made.text
    with engine.begin() as db:
        db.execute(text(
            "INSERT INTO github_connections (id, project_id, connection_label, auth_method, "
            "credential_ref, status, created_by) VALUES (:c, :p, 'test', 'personal_access_token', "
            "'secret:none', 'active', 'test')"), {"c": f"ghc_{slug}", "p": slug})
        db.execute(text(
            "INSERT INTO github_repository_bindings (id, project_id, connection_id, owner, name, "
            "full_name, role, default_branch, created_by) VALUES (:b, :p, :c, 'acme', "
            "'helpdesk-ai-demo', :r, 'primary', 'main', 'test')"),
            {"b": f"ghb_{slug}", "p": slug, "c": f"ghc_{slug}", "r": REPO})
        # The code graph is mapped (96 files, 419 nodes), as it was.
        db.execute(text("UPDATE kg_repo_index SET status = 'indexed', files = 96, parsed = 96, "
                        "nodes = 419, indexed_at = now(), due = false WHERE project_id = :p"),
                   {"p": slug})
    return slug


def _builds(engine, pid: str) -> None:
    """The two newest builds' verify output, as they were (paths and the test user anonymised)."""
    now = datetime.now(timezone.utc)
    rows = json.loads((FIXTURE / "verify_runs.json").read_text())
    with engine.begin() as db:
        for i, r in enumerate(rows):
            db.execute(text(
                "INSERT INTO code_task_runs (id, project_id, status, phase, title, "
                "pull_request_url, pull_request_number, state, created_at, updated_at) VALUES "
                "(:i, :p, :s, 'done', :t, :u, :n, CAST(:st AS JSONB), :at, :at)"),
                {"i": f"{r['id']}_{pid[-8:]}", "p": pid, "s": r["status"], "t": r["title"],
                 "u": r["pull_request_url"], "n": r["pull_request_number"],
                 "st": json.dumps({"last_verify_output": r["output"]}),
                 # The blocked full run is the newer one, 9 hours ago.
                 "at": now - timedelta(hours=9 + i)})


def _run_id(engine, pid: str) -> str:
    with engine.connect() as db:
        return db.execute(text("SELECT id FROM project_onboarding_runs WHERE project_id = :p "
                               "ORDER BY started_at DESC LIMIT 1"), {"p": pid}).scalar()


def _view(engine, pid: str) -> dict:
    with engine.connect() as db:
        return onboarding.view(db, pid)


def _onboard(engine, pid: str, probes) -> dict:
    out = onboarding.advance(engine, get_settings(), _run_id(engine, pid), probes=probes,
                             requeue=False)
    assert out["status"] == "succeeded", out
    return _view(engine, pid)


def test_it_reads_the_repository_itself_when_the_index_checkout_is_gone(world, client, engine,
                                                                         repo):
    pid = _project(world, client, engine)
    run = _onboard(engine, pid, RealRepoProbes(repo))
    items = {i["key"]: i for i in run["checklist"]}
    # Code mapped AND the repository read: one story, told once.
    assert items["knowledge"]["state"] == "done"
    assert "Code mapped (96 files, 419 graph nodes)" in items["knowledge"]["detail"]
    assert "read the repository's 109 files" in items["knowledge"]["detail"]
    # The commands the repository declares (package.json scripts; README for the rest).
    with engine.connect() as db:
        from boss_engine.services.project_experience import knowledge

        dossier = knowledge.dossier(db, pid)
    assert dossier["commands"]["install"] == "npm ci"
    assert dossier["commands"]["test"] == "npm test"
    assert dossier["commands"]["build"] == "npm run build"
    assert dossier["commands"]["run"] == "npm start"
    assert dossier["read_from"] == "checkout"
    # The secrets: read from the app's own files, by name only, none of them the person's.
    secrets = items["secrets"]
    assert secrets["state"] == "done" and secrets["owner"] == "agent"
    assert "not mapped" not in secrets["detail"] and "not read" not in secrets["detail"]
    assert "SESSION_SECRET" in secrets["detail"] and "DEMO_PASSWORD" in secrets["detail"]
    assert secrets["sub"] == "22 read · 2 generated"
    with engine.connect() as db:
        facts = db.execute(text("SELECT facts FROM project_onboarding_runs WHERE id = :i"),
                           {"i": run["run_id"]}).scalar()
    env = facts["secrets"]
    assert env["yours"] == [] and "BOSS_SIGNALS_TOKEN" in env["read_in_code"]
    assert {"GOOGLE_CLIENT_SECRET", "DATABASE_URL"} <= set(env["optional"])
    assert all("=" not in n and n.isupper() for n in env["names"]), "names only, never values"
    assert [u["user"] for u in facts["demo_users"]][:3] == ["customer@demo.test",
                                                            "agent@demo.test", "admin@demo.test"]
    # No README finding: the README is there.
    assert not any(b["label"] == "Write a README" for b in run["backlog"])


def test_when_nothing_can_be_fetched_every_item_gives_the_same_reason(world, client, engine,
                                                                      repo):
    pid = _project(world, client, engine)
    run = _onboard(engine, pid, RealRepoProbes(repo, fetched=False))
    items = {i["key"]: i for i in run["checklist"]}
    reason = "could not fetch main: network is unreachable"
    assert reason in items["knowledge"]["detail"] and reason in items["secrets"]["detail"]
    assert "GitHub's list of 109 files" in items["knowledge"]["detail"]
    assert "not mapped" not in items["secrets"]["detail"]


def test_a_suite_boss_could_not_run_is_said_as_boss_s_not_as_119_failing(world, client, engine,
                                                                         repo):
    """crun_de96bf12: the full suite's 119 "failures" were one setup error — the test database
    BOSS gave the run refused its login (SQLSTATE 28P01). That is BOSS's environment: the Tests
    item says what happened and that BOSS re-runs the suite, the counts say "not run", and the
    cause goes where BOSS's own faults go (Self-fixes)."""
    pid = _project(world, client, engine)
    _builds(engine, pid)
    run = _onboard(engine, pid, RealRepoProbes(repo))
    tests = {i["key"]: i for i in run["checklist"]}["tests"]
    assert tests["state"] == "done" and tests["sub"] == "Re-runs on the next build"
    assert tests["detail"].startswith(
        "BOSS couldn't run the full suite in the last full run, a blocked build, 9h ago: the test "
        "database refused the login BOSS gave it.")
    assert "not your app" in tests["detail"] and "119 of 190 tests never ran" in tests["detail"]
    assert "BOSS re-runs the full suite on the next build" in tests["detail"]
    assert "failing" not in tests["detail"] and "backlog" not in tests["detail"]
    assert "boss_test_run" not in tests["detail"], "a generated name is not news"
    # Not the 13 / 13 of the targeted repair run: the full suite is the baseline, not run.
    with engine.connect() as db:
        from boss_engine.services.project_experience import knowledge

        d = knowledge.dossier(db, pid)["tests"]
        recs = db.execute(text(
            "SELECT signature, title, refs FROM boss_recommendations WHERE project_id = :p"),
            {"p": pid}).mappings().all()
    assert (d["passed"], d["total"], d["failing"], d["not_run"]) == (71, 190, 0, 119)
    assert d["env"]["owner"] == "boss" and d["env"]["code"] == "28P01"
    assert d["source"] == "the last full run, a blocked build, 9h ago"
    assert d["files"] == 40
    node = next(n for n in run["discover"]["nodes"] if n["key"] == "tests")
    assert node["label"] == "40 test files", "no pass rate for a suite that did not run"
    # Self-fixes sees it: one recommendation for the cause, naming the build.
    assert [r["signature"] for r in recs] == ["suite_environment:login:28P01"]
    assert recs[0]["title"].startswith("BOSS couldn't run the app's full suite")
    assert [r["build_id"] for r in recs[0]["refs"]] == [f"crun_de96bf12772a4fde88b7_{pid[-8:]}"]


def test_the_backlog_never_proposes_fixing_tests_boss_could_not_run(world, client, engine, repo):
    pid = _project(world, client, engine)
    _builds(engine, pid)
    probes = RealRepoProbes(repo)
    run = _onboard(engine, pid, probes)
    labels = [b["label"] for b in run["backlog"]]
    assert not any("failing test" in label.lower() for label in labels), labels
    assert all("password authentication" not in b["text"] for b in run["backlog"])
    # The real top work comes first: here the only real signal left (no CI on the default
    # branch), and nothing for Marshal to weigh it against.
    assert labels == ["Add CI"]
    assert probes.asked == []
    first = run["first_work"]
    assert first["label"] == "Add CI" and first["first_by"] == "score"
    assert first["why_first"] == "the only candidate"
    assert first["start"]["body"]["source_ref"] == f"rec:{run['backlog'][0]['rec_id']}"
    assert "estimate_min" in first and "estimate_usd" in first and first["estimate_basis"]


def test_a_real_red_suite_still_outranks_chores_and_marshal_says_why(world, client, engine,
                                                                     repo):
    """The app's OWN failures (assertions, not the environment) are still the first work."""
    pid = _project(world, client, engine)
    red = "".join(f"not ok {i} - case {i}\n  ---\n  failureType: 'testCodeFailure'\n"
                  f"  error: 'expected 200, got 500'\n  code: 'ERR_ASSERTION'\n"
                  f"  name: 'AssertionError'\n  ...\n" for i in range(1, 7))
    red += "1..148\n# tests 148\n# pass 142\n# fail 6\n"
    with engine.begin() as db:
        db.execute(text(
            "INSERT INTO code_task_runs (id, project_id, status, phase, title, state, "
            "pull_request_number, created_at, updated_at) VALUES (:i, :p, 'completed', 'done', "
            "'A change', CAST(:st AS JSONB), 7, now(), now())"),
            {"i": f"crun_red_{pid[-8:]}", "p": pid,
             "st": json.dumps({"last_verify_output": red})})
    probes = RealRepoProbes(repo, pick="Fix the 6 failing tests")
    run = _onboard(engine, pid, probes)
    labels = [b["label"] for b in run["backlog"]]
    assert labels[0] == "Fix the 6 failing tests"
    assert "Add CI" in labels and labels.index("Add CI") > 0
    top = run["backlog"][0]
    assert top["why"] == "142/148 pass · PR #7" and top["kind"] == "bug"
    assert "npm test" in top["text"]
    asked = probes.asked[0]
    assert {c["label"] for c in asked["choices"]} >= {"Fix the 6 failing tests", "Add CI"}
    assert any("Chief-of-Staff score" in h for h in asked["hints"])
    first = run["first_work"]
    assert first["label"] == "Fix the 6 failing tests" and first["first_by"] == "marshal"
    assert first["why_first"] == "A red suite blocks every later change"


def test_marshal_spends_under_the_onboarding_stage(monkeypatch, engine):
    """The Work tab's Onboarding row reads the agent's cost from llm_calls with stage
    `onboarding`: Marshal's decision runs with that stage set, and puts it back after."""
    from boss_engine.services import agent_service, llm_telemetry
    from boss_engine.services.agent_decision import AgentDecision

    seen = {}

    def decide(self, connection, **kw):
        seen.update(dict(llm_telemetry._llm_ctx.get()))
        seen["choices"] = [c.key for c in kw["choices"]]
        return AgentDecision("decided", action="b", confidence=0.9, reason="It unblocks the rest.")

    monkeypatch.setattr(agent_service.AgentService, "decide", decide)
    llm_telemetry.set_llm_context(stage="build")
    got = onboarding.Probes().marshal(
        engine, get_settings(), "p", "onb_x", message="m", context={}, hints=[],
        choices=[{"key": "a", "means": "A", "label": "A"}, {"key": "b", "means": "B", "label": "B"}])
    assert got["decided"] and got["action"] == "b"
    assert seen["stage"] == "onboarding" and seen["role_key"] == "chief_of_staff"
    assert seen["choices"] == ["a", "b"]
    assert llm_telemetry._llm_ctx.get().get("stage") == "build"
    llm_telemetry.set_llm_context(stage=None)


def test_the_proposals_are_the_roadmaps_and_accepting_puts_them_in_next(world, client, engine,
                                                                       repo):
    from boss_engine.services.ambient import roadmap
    from boss_engine.services.project_experience import overview, plan

    pid = _project(world, client, engine)
    _builds(engine, pid)
    run = _onboard(engine, pid, RealRepoProbes(repo, pick="Add CI"))
    proposed = [b for b in run["backlog"] if b.get("rec_id")]
    assert proposed and len(proposed) == len(run["backlog"][:onboarding.PROPOSE_TOP])
    with engine.connect() as db:
        listed = roadmap.list_roadmap(db, pid, status="open")
        nxt = overview.next_items(db, pid, onboarding.view(db, pid))
    assert {i["id"] for i in listed["items"]} == {b["rec_id"] for b in proposed}
    assert all(i["badges"]["detector"] == "Onboarding" for i in listed["items"])
    # What's next shows the same items, Marshal's order, each accepted onto the Roadmap.
    assert [n["id"] for n in nxt][:1] == [run["backlog"][0]["rec_id"]]
    assert all(n["accept"]["href"].startswith("/v1/ambient/roadmap/") for n in nxt)
    # Accept backlog (screen 10-28): all of it onto Next, in order; nothing starts.
    with world.acting_as(world.ctx(world.owner)):
        r = client.post(f"/v1/projects/{pid}/onboarding/backlog/accept",
                        json={"ids": [b["id"] for b in proposed]})
    assert r.status_code == 200, r.text
    with engine.connect() as db:
        lanes = plan.lanes(db, pid)
        after = onboarding.view(db, pid)
        started = db.execute(text("SELECT count(*) FROM workflow_runs WHERE project_id = :p"),
                             {"p": pid}).scalar()
    assert [c["id"] for c in lanes["next"]] == [b["rec_id"] for b in proposed]
    assert lanes["next"][0]["label"] == "Add CI"
    assert all(b["state"] == "accepted" for b in after["backlog"] if b.get("rec_id"))
    assert started == 0


def test_a_rerun_is_idempotent_and_says_when_it_read(world, client, engine, repo):
    pid = _project(world, client, engine)
    _builds(engine, pid)
    first = _onboard(engine, pid, RealRepoProbes(repo))
    keep = first["backlog"][0]
    with world.acting_as(world.ctx(world.owner)):
        client.post(f"/v1/projects/{pid}/onboarding/backlog/accept", json={"ids": [keep["id"]]})
        again = client.post(f"/v1/projects/{pid}/onboarding/start")
        same = client.post(f"/v1/projects/{pid}/onboarding/start")
    assert again.status_code == 200 and again.json()["run_id"] != first["run_id"]
    assert same.json()["run_id"] == again.json()["run_id"], "a run still moving is returned"
    second = _onboard(engine, pid, RealRepoProbes(repo))
    with engine.connect() as db:
        recs = db.execute(text(
            "SELECT fingerprint, status FROM ambient_recommendations WHERE project_id = :p AND "
            "detector = 'onboarding'"), {"p": pid}).all()
    fps = [r[0] for r in recs]
    assert len(fps) == len(set(fps)), "a re-run never proposes the same signal twice"
    assert [b["label"] for b in second["backlog"]] == [b["label"] for b in first["backlog"]]
    assert next(b for b in second["backlog"] if b["id"] == keep["id"])["state"] == "accepted"
    assert second["runs"] == 2 and second["read_ago_s"] is not None and second["read_ago_s"] < 60
    assert second["trigger"] == "manual"
