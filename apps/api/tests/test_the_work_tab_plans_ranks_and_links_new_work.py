"""Screen 05 (Work), the items the first pass left open (BOSSAPP-913 / 914, ACCEPTANCE 05-*).

* 05-01..07, 05-31  Helm's plan: GET …/work/plan draws the workflow Start would run for the
  kind (or the one picked with "change") as its real steps, with time and cost from this
  project's own history; POST …/work runs the picked workflow.
* 05-18/19/23/33   Planned is in Marshal's rank, the first is Next up with the planner's face,
  and a drag (POST …/plan/{id} lane+before) re-ranks it for good.
* 05-20            Work from an issue carries its link: planned items, Tickets pasted as a link
  and work started from a planned item all show the #12 chip.
* 05-21            A CI item says CI and the file its failing check names.
* 05-27            Onboarding is a row: Setup, Discover → Ready → Backlog, Helm and Marshal, its
  cost, and the only-you items hang on it.
* Needs you        Every Needs-you row of the Work tab is a card of the header bar (one source),
  with "Pick a prod host" whenever production waits on a person.

Real Postgres; no model, no GitHub.
"""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.app import main
from apps.api.tests.identity_fixtures import IdentityWorld
from apps.api.tests.project_experience_seed import REPO, seed


@pytest.fixture(scope="module")
def world(engine) -> IdentityWorld:
    w = IdentityWorld.create(engine)
    w.owner = w.person("wk-owner")
    w.bind(w.owner, "owner")
    w.viewer = w.person("wk-viewer")
    w.bind(w.viewer, "viewer")
    return w


@pytest.fixture(scope="module")
def client():
    return TestClient(main.app, raise_server_exceptions=False)


@pytest.fixture
def project(world, client, engine, monkeypatch):
    from apps.api.tests import project_experience_seed as px_seed

    monkeypatch.setattr(px_seed, "seed_alert_history", lambda *a, **k: {})
    slug = f"wk-demo-{uuid.uuid4().hex[:6]}"
    with world.acting_as(world.ctx(world.owner)):
        made = client.post("/v1/projects", json={"name": "HelpDesk AI Demo", "slug": slug})
    assert made.status_code == 201, made.text
    with engine.begin() as db:
        story = seed(db, slug)
        db.execute(text(
            "INSERT INTO github_repos (id, org_id, full_name, language, default_branch, allowed, "
            "checks, checked_at) VALUES (:i, :o, :f, 'TypeScript', 'main', true, "
            "CAST(:c AS JSONB), now()) ON CONFLICT DO NOTHING"),
            {"i": f"ghr_{uuid.uuid4().hex[:8]}", "f": REPO, "o": world.org,
             "c": '{"read": {"ok": true}, "write": {"ok": true}, "pull_requests": {"ok": true}}'})
    return {"id": slug, **story}


def _as(world, who):
    return world.acting_as(world.ctx(who))


def _work(world, client, pid, who=None):
    with _as(world, who or world.owner):
        r = client.get(f"/v1/projects/{pid}/work")
    assert r.status_code == 200, r.text
    return r.json()


def _start(world, client, pid, body):
    """POST …/work through consent and readiness, as the console's sheets would answer them."""
    with _as(world, world.owner):
        r = client.post(f"/v1/projects/{pid}/work", json=body)
        for _ in range(3):
            if r.status_code == 200:
                break
            got = r.json() if r.status_code == 409 else {}
            if got.get("error") == "consent_required":
                grant = {"agents": [a["id"] for a in got["agents"]],
                         "permissions": got["permissions"], "projects": got["projects"],
                         "kind": "standing", "days": 30, "purpose": got["task"]}
                if got.get("github"):
                    grant["github"] = {"repos": got["github"]["repos"],
                                       **got["github"]["permissions"]}
                assert client.post("/v1/identity/me/delegations", json=grant).status_code in (
                    200, 201)
            else:
                detail = got.get("detail") if isinstance(got.get("detail"), dict) else got
                ready = detail.get("readiness") if isinstance(detail.get("readiness"),
                                                              dict) else detail
                missing = [m["key"] for m in (ready or {}).get("missing") or []]
                assert missing, r.text
                body = {**body, "start_anyway": missing}
            r = client.post(f"/v1/projects/{pid}/work", json=body)
    return r


@pytest.fixture
def no_repo_guard(monkeypatch):
    from boss_engine.identity import grants

    monkeypatch.setattr(grants, "repo_guard_active", lambda *a, **k: False)


# ── 05-18 / 05-19 / 05-20 / 05-21 / 05-22: Planned, in Marshal's rank ──────────────────────

def test_planned_is_in_marshals_rank_with_next_up_its_issue_and_its_file(world, client, project):
    body = _work(world, client, project["id"])
    planned = [w for w in body["items"] if w["state"] == "planned"]
    assert [w["title"] for w in planned] == ["Email-to-ticket intake", "SLA timer ignores weekends",
                                             "Fix 6 failing tests", "Reply suggestions",
                                             "Status index"]
    assert [w["lane"] for w in planned] == ["next", "next", "next", "later", "later"]
    first, bug, ci, _reply, index = planned
    # Next up: the top of the rank, with the face of the agent who plans it (Juno).
    assert first["next_up"] is True and [a["role_key"] for a in first["agents"]] == ["pm"]
    assert not any(w["next_up"] for w in planned[1:]) and not bug["agents"]
    # Work from an issue carries its link: the #12 chip.
    assert bug["kind"] == "bug" and bug["origin"] == "bug"
    assert bug["links"]["issue"] == {"provider": "github", "number": 12, "label": "#12",
                                     "kind": "issue",
                                     "external_url": f"https://github.com/{REPO}/issues/12"}
    assert bug["start"]["body"]["source_url"] == f"https://github.com/{REPO}/issues/12"
    # CI: its own tag and the file its failing check names.
    assert ci["origin"] == "ci" and ci["origin_label"] == "CI" and ci["kind"] == "bug"
    assert ci["where"] == "tests/test_notify.py"
    assert index["origin_label"] == "from Roadmap"
    assert all(w["move"]["href"] == f"/v1/projects/{project['id']}/plan/{w['id']}"
               for w in planned)
    assert body["counts"]["planned"] == 5


def test_a_drag_re_ranks_the_backlog_and_the_order_is_kept(world, client, project):
    pid = project["id"]
    planned = [w for w in _work(world, client, pid)["items"] if w["state"] == "planned"]
    ids = [w["id"] for w in planned]
    # Drag "Fix 6 failing tests" to the top (before Email-to-ticket intake).
    with _as(world, world.owner):
        moved = client.post(f"/v1/projects/{pid}/plan/{ids[2]}",
                            json={"lane": "next", "before": ids[0]})
    assert moved.status_code == 200, moved.text
    after = [w for w in _work(world, client, pid)["items"] if w["state"] == "planned"]
    assert [w["id"] for w in after] == [ids[2], ids[0], ids[1], ids[3], ids[4]]
    assert after[0]["next_up"] and after[0]["title"] == "Fix 6 failing tests"
    # Moving down past the last Next item lands it at the top of Later (the combined order).
    with _as(world, world.owner):
        assert client.post(f"/v1/projects/{pid}/plan/{ids[1]}",
                           json={"lane": "later", "before": ids[3]}).status_code == 200
    again = [w["id"] for w in _work(world, client, pid)["items"] if w["state"] == "planned"]
    assert again == [ids[2], ids[0], ids[1], ids[3], ids[4]]
    # The Roadmap's lanes read the same rank.
    with _as(world, world.owner):
        lanes = client.get(f"/v1/projects/{pid}/plan").json()
    assert [c["id"] for c in lanes["next"]] == [ids[2], ids[0]]
    assert [c["id"] for c in lanes["later"]] == [ids[1], ids[3], ids[4]]
    # A viewer reads the rank but cannot change it.
    with _as(world, world.viewer):
        refused = client.post(f"/v1/projects/{pid}/plan/{ids[0]}",
                              json={"lane": "next", "before": ids[2]})
    assert refused.status_code == 403


# ── 05-27: Onboarding is a piece of work ────────────────────────────────────────────────────

def test_onboarding_is_a_row_with_its_flow_faces_cost_and_what_waits_on_you(world, client,
                                                                             project):
    pid = project["id"]
    body = _work(world, client, pid)
    row = next(w for w in body["items"] if w["source"] == "onboarding")
    assert row["id"] == project["plan"]["onboarding_run_id"]
    assert row["label"] == "Onboarding" and row["kind"] == "setup"
    assert row["source_label"] == "Setup"
    assert [f["label"] for f in row["flow"]] == ["Discover", "Ready", "Backlog"]
    assert [a["role_key"] for a in row["agents"]] == ["engineering_captain", "chief_of_staff"]
    assert row["cost_usd"] == 1.2
    # The seeded run waits on the person for the app's secret: it needs you.
    assert row["state"] == "needs_you" and [i["key"] for i in row["waiting"]] == ["secrets"]
    assert row["links"]["onboarding"] == f"?project_id={pid}&area=project&tab=work&drawer=onboarding"
    with _as(world, world.owner):
        overview = client.get(f"/v1/projects/{pid}/overview").json()
    secrets = next(n for n in overview["needs_you"] if n["id"] == "onb:secrets")
    assert secrets["work_id"] == row["id"]
    # Setup is not work: the journey and the Running tile still speak of the builds.
    assert overview["tiles"]["running"]["current"]["label"] == "Ticket search filters"


def test_a_finished_onboarding_reads_done_with_every_stage_done(world, client, engine, project):
    pid = project["id"]
    with engine.begin() as db:
        db.execute(text(
            "UPDATE project_onboarding_runs SET state = 'ready', checklist = checklist || "
            "jsonb_build_object('secrets', (checklist->'secrets') || '{\"state\": \"done\"}'"
            "::jsonb, 'tests', (checklist->'tests') || '{\"state\": \"done\"}'::jsonb) "
            "WHERE project_id = :p"), {"p": pid})
    row = next(w for w in _work(world, client, pid)["items"] if w["source"] == "onboarding")
    assert row["state"] == "done" and row["waiting"] == []
    assert {f["state"] for f in row["flow"]} == {"done"}


# ── Needs you: one source ───────────────────────────────────────────────────────────────────

def test_every_needs_you_row_is_a_card_of_the_header_bar(world, client, project):
    pid = project["id"]
    work = _work(world, client, pid)
    with _as(world, world.owner):
        overview = client.get(f"/v1/projects/{pid}/overview").json()
    carded = {n["work_id"] for n in overview["needs_you"] if n.get("work_id")}
    waiting = {w["id"] for w in work["items"] if w["state"] == "needs_you"}
    assert waiting and waiting <= carded
    # Staging is live and production has no host: "Pick a prod host" waits on the person.
    assert any(n["id"] == "prod:host" and n["label"] == "Pick a prod host"
               for n in overview["needs_you"])


# ── 05-05..07 / 05-31: Helm's plan ──────────────────────────────────────────────────────────

def test_helms_plan_is_the_workflows_real_steps_with_history_estimates(world, client, engine,
                                                                       project):
    from boss_engine.services.project_experience import new_work

    pid = project["id"]
    with engine.connect() as db:
        spec = db.execute(text(
            "SELECT v.spec FROM workflow_definitions d JOIN workflow_versions v ON "
            "v.id = d.active_version_id WHERE d.project_id = :p AND d.workflow_key = "
            "'minor_feature'"), {"p": pid}).scalar()
    # The seed's runs took 5 minutes a step; the agent steps up to the PR are counted.
    to_pr = 5 * sum(1 for st in spec["steps"] if new_work._cap(st)  # noqa: SLF001
                    not in new_work._AFTER_THE_PR and st.get("kind") != "decision")  # noqa: SLF001
    with _as(world, world.owner):
        r = client.get(f"/v1/projects/{pid}/work/plan", params={"kind": "feature"})
    assert r.status_code == 200, r.text
    plan = r.json()
    assert plan["workflow"]["key"] == "minor_feature" and plan["workflow"]["name"] == "Minor Feature"
    keys = [n["key"] for n in plan["nodes"]]
    # Minor Feature: meeting → build (reviews and opens the PR itself) → deploy (a person approves).
    assert keys[0] == "plan" and keys[-1] == "ship"
    assert [k for k in keys if k != "approve"] == ["plan", "build", "review", "pr", "ship"]
    assert plan["nodes"][0]["agent"]["role_key"] == "pm"
    ship = plan["nodes"][-1]
    assert ship["label"] == "Staging" and ship["type"] == "you"
    # Three completed Minor Feature runs: 5 minutes a step to the PR each, $2.10 each.
    est = plan["estimate"]
    assert est["minutes"] == new_work.round_minutes(to_pr) and est["minutes_basis"] == "history" and est["minutes_runs"] == 3
    assert est["usd"] == 2.1 and est["usd_basis"] == "history" and est["usd_runs"] == 3
    assert plan["planner"]["role_key"] == "engineering_captain"
    opts = {o["key"]: o for o in plan["options"]}
    assert opts["minor_feature"]["fits"] is True
    assert all(o["work_class"] != "project.new" for o in plan["options"])


def test_the_plan_changes_with_the_kind_and_the_picked_workflow(world, client, project):
    pid = project["id"]
    with _as(world, world.owner):
        bug = client.get(f"/v1/projects/{pid}/work/plan", params={"kind": "bug"}).json()
        options = [o["key"] for o in bug["options"]]
        picked_key = next((k for k in ("fast_fix", "major_feature") if k in options), None)
        unknown = client.get(f"/v1/projects/{pid}/work/plan",
                             params={"kind": "feature", "workflow_key": "nope"})
        odd = client.get(f"/v1/projects/{pid}/work/plan", params={"kind": "epic"})
    # No bugfix mapping in this project: a bug goes the feature way (what Start really runs).
    assert bug["workflow"]["key"] == "minor_feature"
    assert unknown.status_code == 422 and unknown.json()["detail"]["error"] == "unknown_workflow"
    assert odd.status_code == 422
    assert picked_key, options
    with _as(world, world.owner):
        picked = client.get(f"/v1/projects/{pid}/work/plan",
                            params={"kind": "bug", "workflow_key": picked_key}).json()
    assert picked["workflow"]["key"] == picked_key and picked["workflow"]["chosen"] == "picked"
    # Never run here: the time is the steps' typical minutes, and there is no cost figure.
    assert picked["estimate"]["minutes_basis"] == "typical"
    assert picked["estimate"]["usd"] is None
    assert [n["key"] for n in picked["nodes"]] != [n["key"] for n in bug["nodes"]]


def test_a_mapped_bugfix_workflow_is_what_a_bug_runs(world, client, engine, project):
    pid = project["id"]
    with engine.begin() as db:
        row = db.execute(text(
            "SELECT d.workflow_key, d.active_version_id FROM workflow_definitions d WHERE "
            "d.project_id = :p AND d.namespace = 'product' AND d.status = 'active' AND "
            "d.workflow_key IN ('fast_fix', 'major_feature') ORDER BY d.workflow_key DESC LIMIT 1"),
            {"p": pid}).mappings().first()
        assert row is not None
        db.execute(text(
            "INSERT INTO workflow_mappings (id, project_id, workflow_version_id, selector_kind, "
            "selector_value, scope_type, scope_id, priority, created_by) VALUES (:i, :p, :v, "
            "'work_class', 'bugfix', 'project', :p, 200, 'test')"),
            {"i": f"wfm_{uuid.uuid4().hex[:10]}", "p": pid, "v": row["active_version_id"]})
    with _as(world, world.owner):
        bug = client.get(f"/v1/projects/{pid}/work/plan", params={"kind": "bug"}).json()
        feature = client.get(f"/v1/projects/{pid}/work/plan", params={"kind": "feature"}).json()
    assert bug["workflow"]["key"] == row["workflow_key"] and bug["workflow"]["chosen"] == "mapped"
    assert feature["workflow"]["key"] == "minor_feature"


# ── 05-02..04 / 05-07 / 05-20: Start sends kind, the picked workflow and the issue link ────

def test_a_ticket_pasted_as_a_link_keeps_its_issue_and_runs_the_picked_workflow(
        world, client, engine, project, no_repo_guard):
    pid = project["id"]
    with _as(world, world.owner):
        options = client.get(f"/v1/projects/{pid}/work/plan",
                             params={"kind": "ticket"}).json()["options"]
    picked = next(o["key"] for o in options if o["key"] != "minor_feature")
    link = f"https://github.com/{REPO}/issues/31"
    r = _start(world, client, pid, {"kind": "ticket", "text": link, "workflow_key": picked})
    assert r.status_code == 200, r.text
    out = r.json()
    with engine.connect() as db:
        key, run_input = db.execute(text(
            "SELECT d.workflow_key, r.input FROM workflow_runs r JOIN workflow_definitions d "
            "ON d.id = r.workflow_definition_id WHERE r.id = :r"), {"r": out["run_id"]}).one()
    assert key == picked
    assert run_input["source_url"] == link and run_input["work_kind"] == "ticket"
    assert run_input["title"] == "Issue #31"
    row = next(w for w in _work(world, client, pid)["items"] if w["id"] == out["run_id"])
    assert row["links"]["issue"]["number"] == 31 and row["links"]["issue"]["external_url"] == link


def test_starting_a_planned_issue_carries_its_link_onto_the_run(world, client, engine, project,
                                                                no_repo_guard):
    pid = project["id"]
    bug = next(w for w in _work(world, client, pid)["items"]
               if w["title"] == "SLA timer ignores weekends")
    r = _start(world, client, pid, bug["start"]["body"])
    assert r.status_code == 200, r.text
    items = _work(world, client, pid)["items"]
    run = next(w for w in items if w["id"] == r.json()["run_id"])
    assert run["links"]["issue"]["number"] == 12
    # It left Planned: one row per piece of work.
    assert not any(w["state"] == "planned" and w["id"] == bug["id"] for w in items)


def test_an_unknown_picked_workflow_is_refused_before_anything_starts(world, client, engine,
                                                                       project):
    pid = project["id"]
    with engine.connect() as db:
        before = db.execute(text("SELECT count(*) FROM workflow_runs WHERE project_id = :p"),
                            {"p": pid}).scalar()
    with _as(world, world.owner):
        r = client.post(f"/v1/projects/{pid}/work",
                        json={"kind": "feature", "text": "Add CSV export", "workflow_key": "nope"})
    assert r.status_code == 422 and r.json()["detail"]["error"] == "unknown_workflow"
    with engine.connect() as db:
        assert db.execute(text("SELECT count(*) FROM workflow_runs WHERE project_id = :p"),
                          {"p": pid}).scalar() == before


# ── the issue reader ────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("ref,url,expected", [
    ("issue:12", None, ("github", 12, f"https://github.com/{REPO}/issues/12")),
    ("pr:7", None, ("github", 7, f"https://github.com/{REPO}/pull/7")),
    (None, "https://github.com/acme/x/issues/3", ("github", 3, "https://github.com/acme/x/issues/3")),
    ("rec:wi_1", "https://github.com/acme/x/issues/9", ("github", 9,
                                                         "https://github.com/acme/x/issues/9")),
    (None, "https://plane.example.test/acme/browse/HELP-14", ("plane", None,
                                                              "https://plane.example.test/acme/browse/HELP-14")),
])
def test_an_issue_is_read_from_a_link_or_a_ref(ref, url, expected):
    from boss_engine.services.project_experience.new_work import issue_of

    got = issue_of(ref, url, REPO)
    assert (got["provider"], got["number"], got["external_url"]) == expected


def test_no_issue_without_a_link_or_an_issue_ref():
    from boss_engine.services.project_experience.new_work import issue_of, ticket_link

    assert issue_of("ci:main", None, REPO) is None and issue_of("rec:wi_1", None, REPO) is None
    assert ticket_link("see https://github.com/acme/x/issues/4, please") == \
        "https://github.com/acme/x/issues/4"
    assert ticket_link("no link here") is None


def test_a_failing_check_names_its_file_only_when_it_is_one():
    from boss_engine.services.project_experience import onboarding

    assert onboarding._FILE.fullmatch("tests/test_notify.py")  # noqa: SLF001
    assert not onboarding._FILE.fullmatch("test (3.11)")  # noqa: SLF001
    cand = onboarding._candidate("Fix 1 failing check", source="ci", task_class="red_main",  # noqa: SLF001
                                 severity="high", kind="bug", ref="ci:main",
                                 where="tests/test_notify.py")
    assert cand["where"] == "tests/test_notify.py"


def test_the_plan_rounds_time_like_a_person_says_it():
    from boss_engine.services.project_experience.new_work import round_minutes

    assert [round_minutes(m) for m in (None, 0, 4, 9, 12, 23, 35)] == [None, None, 4, 9, 10, 25, 35]


def test_a_number_finds_the_pr_or_the_issue_never_a_digit_in_words(world, client, project):
    with _as(world, world.owner):
        by_issue = client.get(f"/v1/projects/{project['id']}/work", params={"q": "#12"}).json()
        by_pr = client.get(f"/v1/projects/{project['id']}/work", params={"q": "#2"}).json()
    assert [w["title"] for w in by_issue["items"]] == ["SLA timer ignores weekends"]
    assert [w["title"] for w in by_pr["items"]] == ["UI refresh"]
