"""THE PROJECT PAGE SAYS ONE TRUTH (BOSSAPP-913/914, round 3 of the operator's walk).

* The backlog has ONE count: the Journey said "Backlog · 10 proposed" while Onboarding said
  "5 proposed" and the Roadmap "5 new" — onboarding's proposals ARE the Roadmap's open
  recommendations and were counted twice. Journey, Onboarding's stage and the Roadmap tab now read
  onboarding.backlog_counts.
* The tests have ONE statement (test_runs.suite_words): a suite BOSS could not run says
  "40 test files · full suite on next build" everywhere, never an old pass rate.
* A Roadmap card is evidence → detector → agent built from the recommendation's own record
  (ambient/roadmap_card.py), never the provenance chain "Code graph → Roadmap → Onboarding".
Real Postgres for the counts; the statements and the cards are pure.
"""
from __future__ import annotations

import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.app import main
from apps.api.tests.identity_fixtures import IdentityWorld
from apps.api.tests.project_experience_seed import seed
from boss_engine.services.ambient import roadmap_card
from boss_engine.services.project_experience import onboarding
from boss_engine.services.project_experience.test_runs import suite_words


# ── the tests' one statement ────────────────────────────────────────────────────────────────

def test_a_suite_boss_could_not_run_says_its_files_and_the_next_build():
    env_run = {"passed": 71, "total": 190, "failing": 0, "not_run": 119,
               "env": {"reason": "the test database refused the login BOSS gave it"}}
    got = suite_words(env_run, 40)
    assert got == {"label": "40 test files", "line": "40 test files · full suite on next build",
                   "state": "env"}
    assert "71" not in got["line"] and "190" not in got["line"]
    assert suite_words(env_run, 0)["label"] == "Suite not run"
    assert suite_words(env_run, 1)["label"] == "1 test file"


def test_a_suite_that_ran_says_its_counts():
    assert suite_words({"passed": 142, "total": 148, "failing": 6}, 40) == {
        "label": "142/148 tests", "line": "6 failing → backlog", "state": "failing"}
    assert suite_words({"passed": 148, "total": 148, "failing": 0}, 40)["line"] == "148 / 148 pass"
    assert suite_words(None, 12)["line"] == "12 test files · first build runs them"
    assert suite_words(None, 0) == {"label": "No tests yet", "line": "No tests", "state": "none"}


# ── the Roadmap card ────────────────────────────────────────────────────────────────────────

def _onb(ref: str, *, why: str = "", where: str | None = None, **extra) -> dict:
    return {"detector": "onboarding", "subject": ref, "kind": "test_gap",
            "proposed_shape": "ticket", "estimated_cost_usd_micros": 0,
            "facts": {"ref": ref, "why_short": why, "where": where}, **extra}


def test_an_onboarding_test_gap_names_the_hot_file_the_detector_and_the_work():
    rec = _onb("signal:sig_1", why="changes often, no tests", where="server/service.mjs")
    card = roadmap_card.card(rec, kinds={"sig_1": "scan.untested_hotspot"}, build_usd=21.64)
    assert [e["label"] for e in card["evidence"]] == ["service.mjs hot", "No tests"]
    assert card["detector"] == {"label": "Test gap", "icon": "test-tube"}
    assert card["does"] == "Add tests"
    assert (card["cost_usd"], card["cost_basis"]) == (21.64, "a ticket here, from past runs")


def test_no_card_draws_the_provenance_chain():
    for kind in ("scan.no_ci", "scan.missing_tests", "scan.untested_hotspot", "scan.todo_cluster"):
        card = roadmap_card.card(_onb("signal:s", where="a/b.py"), kinds={"s": kind})
        words = {e["label"] for e in card["evidence"]} | {(card["detector"] or {}).get("label")}
        assert not words & {"Code graph", "Roadmap", "Onboarding", "Boss"}, (kind, words)
    no_ci = roadmap_card.card(_onb("signal:s"), kinds={"s": "scan.no_ci"})
    assert ([e["label"] for e in no_ci["evidence"]], no_ci["detector"]["label"], no_ci["does"]) == \
        (["No CI on main"], "CI gap", "Set up CI")


def test_a_proposal_s_own_words_are_its_evidence_never_cut_mid_phrase():
    card = roadmap_card.card(_onb("deferred:crun_1", why="deferred on PR #3"))
    assert ([e["label"] for e in card["evidence"]], card["detector"]["label"], card["does"]) == \
        (["Deferred on PR #3"], "Review follow-up", "Fix findings")
    issue = roadmap_card.card(_onb("issue:12", why="bug #12 · 5d open"))
    assert [e["label"] for e in issue["evidence"]] == ["Bug #12", "5d open"]
    # No estimate and no history: no cost chip, never a made-up number.
    assert roadmap_card.card(_onb("issue:12"), build_usd=None)["cost_usd"] is None


def test_a_learning_detector_card_reads_its_facts():
    from boss_engine.services.ambient import detectors  # noqa: F401 — fills the registry

    rec = {"detector": "incident_cause", "subject": "file:server/sla.py", "kind": "hardening",
           "proposed_shape": "ticket", "estimated_cost_usd_micros": 2_000_000,
           "facts": {"incident_cause": {"incidents": 2, "cause": "weekend pause"}},
           "evidence": [{"kind": "incident", "ref": "inc_1"}, {"kind": "incident", "ref": "inc_2"}]}
    card = roadmap_card.card(rec)
    assert [e["label"] for e in card["evidence"]] == ["2 incidents", "sla.py hot"]
    assert card["detector"]["label"] == "Incident hotspot" and card["does"] == "Test + fix"
    assert (card["cost_usd"], card["cost_basis"]) == (2.0, "this item's estimate")
    slow = dict(rec, detector="slow_stage", subject="stage:review", kind="process",
                facts={"slow_stage": {"median_min": 14.2, "target_min": 5}}, evidence=[])
    assert [e["label"] for e in roadmap_card.card(slow)["evidence"]] == ["Review 14.2 min"]


def test_an_onboarding_proposal_with_no_specific_ref_is_named_by_its_source():
    card = roadmap_card.card(_onb("todo:web", why="7 TODOs in web/src", source="roadmap"))
    card2 = roadmap_card.card({**_onb("todo:web", why="7 TODOs in web/src"),
                               "facts": {"ref": "todo:web", "why_short": "7 TODOs", "source": "bug"}})
    assert card2["detector"]["label"] == "Open bug" and card2["does"] == "Fix bug"
    assert card["detector"] is not None


def test_an_item_without_a_detector_says_what_its_kind_does():
    card = roadmap_card.card({"detector": "seed", "kind": "feature", "proposed_shape": "ticket",
                              "facts": {}, "evidence": []})
    # Never the agent alone (tester 07-20): its kind names the node.
    assert card["detector"] == {"label": "Feature", "icon": "trend-up"}
    assert card["evidence"] == [] and card["does"] == "Build it"
    counted = roadmap_card.card({"detector": "seed", "kind": "hardening", "facts": {},
                                 "evidence": [], "evidence_count": 3})
    assert [e["label"] for e in counted["evidence"]] == ["3 signals"]


# ── the backlog's one count, over HTTP ──────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def world(engine) -> IdentityWorld:
    w = IdentityWorld.create(engine)
    w.owner = w.person("truth-owner")
    w.bind(w.owner, "owner")
    return w


@pytest.fixture(scope="module")
def client():
    return TestClient(main.app, raise_server_exceptions=False)


@pytest.fixture
def project(world, client, engine, monkeypatch):
    from apps.api.tests import project_experience_seed as px_seed

    monkeypatch.setattr(px_seed, "seed_alert_history", lambda *a, **k: {})
    slug = f"truth-{uuid.uuid4().hex[:6]}"
    with world.acting_as(world.ctx(world.owner)):
        made = client.post("/v1/projects", json={"name": "HelpDesk AI Demo", "slug": slug})
    assert made.status_code == 201, made.text
    with engine.begin() as db:
        seed(db, slug)
    return slug


def test_journey_onboarding_and_roadmap_count_the_backlog_once(world, client, engine, project):
    with engine.connect() as c:
        counts = onboarding.backlog_counts(c, project)
        recs = dict(c.execute(text(
            "SELECT status, count(*) FROM ambient_recommendations WHERE project_id = :p "
            "GROUP BY status"), {"p": project}).all())
    assert counts["open"] == recs.get("open", 0) and counts["accepted"] == recs.get("accepted", 0)
    words = onboarding.backlog_words(counts)
    assert words, "the seed proposes a backlog"
    with world.acting_as(world.ctx(world.owner)):
        ov = client.get(f"/v1/projects/{project}/overview").json()
        ob = client.get(f"/v1/projects/{project}/onboarding").json()
        rm = client.get(f"/v1/ambient/roadmap?project_id={project}&status=open").json()
    journey = {j["key"]: j for j in ov["journey"]}["backlog"]
    stage = {s["key"]: s for s in ob["stages"]}["backlog"]
    assert journey["sub"] == stage["sub"] == words
    # Ready counts the same rows on both (tester 04-03: 7/7 there, 6/6 here).
    ready_j = {j["key"]: j for j in ov["journey"]}["ready"]["sub"]
    ready_o = {s["key"]: s for s in ob["stages"]}["ready"]["sub"]
    assert ready_j.replace("/", " of ") == ready_o
    # Onboarded on the day the run says (tester 04-02), not the day the project was made.
    from datetime import datetime
    at = datetime.fromisoformat((ob.get("finished_at") or ob["started_at"]).replace("Z", "+00:00"))
    assert {j["key"]: j for j in ov["journey"]}["onboarding"]["sub"] == f"{at:%b} {at.day}"
    assert ov["roadmap_count"] == counts["total"]
    assert rm["counts"]["open"] == counts["open"]
    # A candidate and its recommendation are one item: the old sum (candidates + open + accepted)
    # is never what the Journey says.
    doubled = len(ob["backlog"]) + counts["open"] + counts["accepted"]
    assert str(doubled) not in journey["sub"].split()


def test_a_roadmap_card_comes_with_every_recommendation(world, client, engine, project):
    with world.acting_as(world.ctx(world.owner)):
        rm = client.get(f"/v1/ambient/roadmap?project_id={project}&status=all").json()
    assert rm["items"]
    for item in rm["items"]:
        card = item["card"]
        assert set(card) == {"evidence", "detector", "does", "cost_usd", "cost_basis"}
        assert card["does"] and len(card["evidence"]) <= 2
        assert json.dumps(card).count("Onboarding") == 0
