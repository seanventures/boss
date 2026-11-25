"""BOSSAPP-1292: preview success metrics from what BOSS already records — PR open-to-approval
time, regressions caught before and after merge, post-deploy alerts vs auto-remediated — with a
typed-in before baseline and a CSV export whose every row names its source."""
from __future__ import annotations

import csv
import io
import json
import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from boss_engine.services import pilot_metrics as pm

URL = os.environ.get("BOSS_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not URL or "test" not in URL.lower(),
                                reason="needs BOSS_TEST_DATABASE_URL naming a disposable database")

NOW = datetime.now(timezone.utc)


@pytest.fixture(scope="module")
def eng():
    from boss_engine.db import ensure_schema

    engine = create_engine(URL, future=True)
    ensure_schema(engine)
    yield engine
    engine.dispose()


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


@pytest.fixture()
def world(eng):
    sfx = uuid4().hex[:8]
    w = {"org": f"org_1292_{sfx}", "ws": f"ws_1292_{sfx}", "p": f"p_1292_{sfx}"}
    with eng.begin() as c:
        c.execute(text("INSERT INTO orgs (id, name) VALUES (:o, 'o')"), {"o": w["org"]})
        c.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES (:w, :o, 'w')"),
                  {"w": w["ws"], "o": w["org"]})
        c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES (:p, :w, :p, :p)"),
                  {"p": w["p"], "w": w["ws"]})
    return w


def _event(c, project, kind, entity_type, entity_id, data, at):
    c.execute(text("INSERT INTO events (id, project_id, event_type, actor, entity_type, entity_id, data, "
                   "created_at) VALUES (:i, :p, :t, 'boss', :et, :e, CAST(:d AS JSONB), :at)"),
              {"i": _id("evt"), "p": project, "t": kind, "et": entity_type, "e": entity_id,
               "d": json.dumps(data), "at": at})


def _pr(c, project, *, url, opened_hours_ago, approved_after_hours=None):
    run, dec = _id("ctr"), _id("dec")
    opened = NOW - timedelta(hours=opened_hours_ago)
    approved = opened + timedelta(hours=approved_after_hours) if approved_after_hours is not None else None
    c.execute(text("INSERT INTO decisions (id, project_id, question, status, approved_at) VALUES "
                   "(:d, :p, 'Merge?', :s, :a)"),
              {"d": dec, "p": project, "s": "approved" if approved else "pending", "a": approved})
    _event(c, project, "code_task.pr_opened", "code_task_run", run,
           {"pull_request_url": url, "decision_id": dec}, opened)
    return run


def test_review_time_counts_each_pr_once_from_first_open_to_first_approval(eng, world):
    p = world["p"]
    with eng.begin() as c:
        _pr(c, p, url="https://example.test/pr/1", opened_hours_ago=30, approved_after_hours=2)
        _pr(c, p, url="https://example.test/pr/2", opened_hours_ago=20, approved_after_hours=6)
        # a second attempt re-opened PR 2 later; it is the same PR
        _pr(c, p, url="https://example.test/pr/2", opened_hours_ago=10, approved_after_hours=1)
        _pr(c, p, url="https://example.test/pr/3", opened_hours_ago=5)              # waiting
        _pr(c, p, url="https://example.test/pr/old", opened_hours_ago=24 * 40, approved_after_hours=1)
        got = pm.review_time(c, [p], NOW - timedelta(days=30), NOW)
    assert got["opened"] == 3 and got["approved"] == 2 and got["waiting"] == 1
    assert got["median_hours"] == 4.0 and got["mean_hours"] == 4.0
    hours = {i["pull_request_url"]: i["hours"] for i in got["items"]}
    assert hours == {"https://example.test/pr/1": 2.0, "https://example.test/pr/2": 6.0,
                     "https://example.test/pr/3": None}
    assert all(i["run_id"] for i in got["items"])


def test_regressions_before_and_after_merge(eng, world):
    p = world["p"]
    with eng.begin() as c:
        for status, tests_passed in (("fail", False), ("fail", True), ("pass", True)):
            run = _id("ctr")
            c.execute(text("INSERT INTO code_task_runs (id, project_id, title) VALUES (:r, :p, 'Change')"),
                      {"r": run, "p": p})
            c.execute(text("INSERT INTO quality_gates (id, project_id, code_task_run_id, status, tests_passed, "
                           "gates_failed) VALUES (:i, :p, :r, :s, :t, CAST(:g AS JSONB))"),
                      {"i": _id("qg"), "p": p, "r": run, "s": status, "t": tests_passed,
                       "g": json.dumps([] if tests_passed else ["tests_passed"])})
        # A gate that failed on tests, then passed after a repair round: the row now says pass,
        # the event still remembers the failure (review M6).
        repaired = _id("ctr")
        c.execute(text("INSERT INTO quality_gates (id, project_id, code_task_run_id, status, tests_passed) "
                       "VALUES (:i, :p, :r, 'pass', true)"), {"i": _id("qg"), "p": p, "r": repaired})
        _event(c, p, "quality_gate.recorded", "quality_gate", _id("qg"),
               {"status": "fail", "gates_failed": ["tests_passed"], "code_task_run_id": repaired}, NOW)
        _event(c, p, "quality_gate.recorded", "quality_gate", _id("qg"),
               {"status": "fail", "gates_failed": ["review_clean"], "code_task_run_id": _id("ctr")}, NOW)
        _event(c, p, "deploy.rolled_back", "code_task_run", "ctr_deployed", {"health": "500"}, NOW)
        _event(c, p, "deploy.rolled_back", "deployment", "dep_7", {}, NOW)
        _event(c, p, "change_set.reverted", "change_set", "cs_1", {}, NOW)
        got = pm.regressions(c, [p], NOW - timedelta(days=30), NOW + timedelta(days=1))
    assert got["blocked_before_merge"] == 2 and got["stopped_after_merge"] == 3 and got["total"] == 5
    assert repaired in {i["run_id"] for i in got["before_merge_items"]}
    assert all(i["gates_failed"] == ["tests_passed"] for i in got["before_merge_items"])
    # every after-merge item names its source, whatever its entity type (review M7)
    assert {(i["entity_type"], i["entity_id"]) for i in got["after_merge_items"]} == {
        ("code_task_run", "ctr_deployed"), ("change_set", "cs_1"), ("deployment", "dep_7")}


def _alert(c, project, *, incident=None, notified=True, acked=None):
    c.execute(text("INSERT INTO app_alerts (id, project_id, rule_kind, fingerprint, alertname, state, "
                   "active_at, fired_at, notified_at, incident_id, acked_by) VALUES (:i, :p, 'error_rate', "
                   ":f, 'HighErrorRate', 'resolved', :at, :at, :n, :inc, :ack)"),
              {"i": _id("alr"), "p": project, "f": _id("fp"), "at": NOW - timedelta(hours=1),
               "n": NOW if notified else None, "inc": incident, "ack": acked})


def _incident(c, project):
    inc = _id("inc")
    c.execute(text("INSERT INTO incidents (id, project_id, dedupe_key, title) VALUES (:i, :p, :i, 'errors')"),
              {"i": inc, "p": project})
    return inc


def test_post_deploy_alerts_vs_auto_remediated(eng, world):
    p = world["p"]
    with eng.begin() as c:
        fixed, recovered, open_ = _incident(c, p), _incident(c, p), _incident(c, p)
        c.execute(text("INSERT INTO remediations (id, project_id, incident_id, code_task_run_id, status) "
                       "VALUES (:i, :p, :inc, 'ctr_fix', 'completed')"), {"i": _id("rem"), "p": p, "inc": fixed})
        c.execute(text("INSERT INTO incident_recovery_checks (id, project_id, incident_id, target_id, since, "
                       "deadline, status) VALUES (:i, :p, :inc, 't', now(), now(), 'recovered')"),
                  {"i": _id("irc"), "p": p, "inc": recovered})
        _alert(c, p, incident=fixed)
        _alert(c, p, incident=recovered)
        _alert(c, p, incident=open_, acked="usr_oncall")
        _alert(c, p, notified=False)
        got = pm.post_deploy_errors(c, [p], NOW - timedelta(days=30), NOW)
    assert got["alerted"] == 4 and got["notified"] == 3 and got["auto_remediated"] == 2
    assert got["auto_remediated_pct"] == 50.0
    assert [i["fix_run_id"] for i in got["items"] if i["fix_run_id"]] == ["ctr_fix"]
    assert sum(1 for i in got["items"] if i["handled_by_person"]) == 1


def test_baseline_is_typed_in_and_checked(eng, world):
    with eng.begin() as c:
        saved = pm.set_baseline(c, world["org"], {"review_hours_median": 30, "auto_remediated_pct": ""},
                                "from the team's tracker", "ceo")
        assert saved["values"] == {"review_hours_median": 30.0} and saved["note"] == "from the team's tracker"
        with pytest.raises(ValueError):
            pm.set_baseline(c, world["org"], {"made_up": 1}, "", "ceo")
        with pytest.raises(ValueError):
            pm.set_baseline(c, world["org"], {"auto_remediated_pct": 140}, "", "ceo")


def test_report_and_csv_export(eng, world):
    p = world["p"]
    with eng.begin() as c:
        _pr(c, p, url="https://example.test/pr/9", opened_hours_ago=3, approved_after_hours=2)
        pm.set_baseline(c, world["org"], {"review_hours_median": 30}, "", "ceo")
        report = pm.compute(c, world["org"], days=30)
        with pytest.raises(ValueError):
            pm.compute(c, world["org"], days=12)
    assert report["label"] == "Whole org" and report["projects"] == [p]
    assert report["after"]["review_hours_median"] == 2.0
    assert report["baseline"]["values"]["review_hours_median"] == 30.0
    rows = list(csv.DictReader(io.StringIO(pm.to_csv(report))))
    summary = {r["metric"]: r for r in rows if r["section"] == "summary"}
    assert summary["review_hours_median"]["before"] == "30.0" and summary["review_hours_median"]["after"] == "2.0"
    review = [r for r in rows if r["section"] == "review"]
    assert len(review) == 1 and review[0]["source"].startswith("ctr_")


def test_the_csv_never_carries_a_formula():
    """CSV injection (review S2): a string cell a spreadsheet would run is made text."""
    evil = '=HYPERLINK("http://evil.test","open")'
    report = {
        "label": "+Team", "days": 30, "until": "2026-11-25T00:00:00+00:00",
        "baseline": {"values": {}}, "after": {k: None for k in pm.BASELINE_FIELDS},
        "review": {"items": [{"hours": 1.0, "run_id": "ctr_1", "project_id": "p", "opened_at": None,
                              "pull_request_url": evil}]},
        "regressions": {"before_merge_items": [{"run_id": "ctr_2", "project_id": "p", "at": None,
                                                "gates_failed": ["-x"]}],
                        "after_merge_items": []},
        "post_deploy": {"items": [{"auto_remediated": False, "incident_id": None, "alert_id": "@a",
                                   "project_id": "p", "fired_at": None, "alertname": "\tcmd"}]},
    }
    cells = [c for row in csv.reader(io.StringIO(pm.to_csv(report))) for c in row]
    assert "'" + evil in cells and "'-x" in cells and "'@a" in cells and "'\tcmd" in cells
    assert not any(c.startswith(("=", "+", "-", "@", "\t", "\r")) for c in cells)
    assert pm.safe_cell(-3) == -3 and pm.safe_cell("ok") == "ok"
