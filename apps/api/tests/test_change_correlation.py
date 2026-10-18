"""RB-17 — change-correlation: recent changes near onset become ranked candidate triggers, and the
investigation engine weights a change by how close to onset it landed. Against boss_test."""
from datetime import timedelta

from sqlalchemy import text

from apps.api.app.db import begin
from apps.api.app.services.change_correlation import ChangeCorrelationService
from apps.api.app.services.core import make_id, now_utc
from apps.api.app.services.investigation import InvestigationEngine
from apps.api.app.services.reliability import ReliabilityService

PID = "boss-dogfood"
CC = ChangeCorrelationService()
RS = ReliabilityService()
ENG = InvestigationEngine()


def _code_task(c, *, title, minutes_before, onset, pr=None):
    c.execute(
        text(
            """
            INSERT INTO code_task_runs (id, project_id, status, phase, title, spec, finalize_ctx,
                                        pull_request_number, created_at)
            VALUES (:id, :p, 'completed', 'done', :title, '{}'::jsonb, '{}'::jsonb, :pr, :ts)
            """
        ),
        {"id": make_id("crun"), "p": PID, "title": title, "pr": pr,
         "ts": onset - timedelta(minutes=minutes_before)},
    )


def _errors(service, n):
    return [{"ts": i, "message": f"{service} boom {i}", "service": service, "level": "error"}
            for i in range(n)]


# --- correlate ---------------------------------------------------------------
def test_correlate_finds_recent_changes_closest_first(engine):
    with begin(engine) as c:
        onset = now_utc()
        _code_task(c, title="risky change", minutes_before=5, onset=onset, pr=101)
        _code_task(c, title="older change", minutes_before=40, onset=onset, pr=100)
        _code_task(c, title="too old", minutes_before=180, onset=onset, pr=99)   # outside lookback
        changes = CC.correlate(c, PID, onset, lookback_minutes=60)
        keys = [ch["key"] for ch in changes]
        assert keys[:2] == ["pr-101", "pr-100"]              # closest-to-onset first
        assert "pr-99" not in keys                           # beyond lookback excluded
        assert changes[0]["evidence"]["proximity_minutes"] < changes[1]["evidence"]["proximity_minutes"]


def test_correlate_empty_when_no_changes(engine):
    with begin(engine) as c:
        assert CC.correlate(c, PID, now_utc(), lookback_minutes=60) == []


# --- engine proximity weighting ---------------------------------------------
def test_engine_scores_closer_change_higher():
    sub = {"root": None, "nodes": [], "edges": []}
    near = [{"kind": "deployment", "key": "d1", "name": "deploy 1",
             "evidence": {"proximity_minutes": 2}}]
    far = [{"kind": "deployment", "key": "d2", "name": "deploy 2",
            "evidence": {"proximity_minutes": 55}}]
    cn = ENG.investigate(service="s", signals=[], known_causes=[], subgraph=sub, changes=near)["winner"]
    cf = ENG.investigate(service="s", signals=[], known_causes=[], subgraph=sub, changes=far)["winner"]
    assert cn["basis"] == "change" and cf["basis"] == "change"
    assert cn["confidence"] > cf["confidence"]               # closer to onset = stronger


# --- diagnose integration ----------------------------------------------------
def test_diagnose_surfaces_a_recent_deploy_as_the_trigger(engine):
    with begin(engine) as c:
        rep = RS.scan(c, PID, {"kind": "memory", "logs": _errors("cc-svc", 12)},
                      {"start": 0, "end": 9e9}, persist=True)
        inc_id = rep["incidents"][0]["id"]
        onset = c.execute(text("SELECT first_seen FROM incidents WHERE id = :id"),
                          {"id": inc_id}).scalar_one()
        _code_task(c, title="bad deploy", minutes_before=3, onset=onset, pr=777)
        out = RS.diagnose(c, PID, inc_id)
        assert out["changes"] and out["changes"][0]["key"] == "pr-777"
        # with no prior memory, the fresh deploy is the leading hypothesis
        assert out["hypothesis"]["basis"] == "change"
        assert out["hypothesis"]["cause_key"] == "pr-777"


def test_a_stack_release_is_a_candidate_with_its_commit(engine):
    """D2: an app shipped by its boss.deploy.yaml lands in stack_deployments, not deployment_runs;
    the release that triggered an incident was invisible to the correlation that names it."""
    import json as _json

    onset = now_utc()
    tid, sdep = make_id("deptgt"), make_id("sdep")
    with begin(engine) as c:
        c.execute(text("INSERT INTO deployment_targets (id, project_id, app_name, repo, "
                       "environment, target_kind, target_ref, status, created_by) VALUES "
                       "(:t, :p, 'order-api', 'acme/sandbox', 'staging', 'stack', 'order-api', "
                       "'active', 'test')"), {"t": tid, "p": PID})
        c.execute(text("INSERT INTO stack_deployments (id, project_id, target_id, repository, "
                       "ref, commit_sha, manifest, status, created_by, created_at, finished_at) "
                       "VALUES (:i, :p, :t, 'acme/sandbox', 'main', 'feedc0de1234567', "
                       "CAST(:m AS JSONB), 'healthy', 'test', :ts, :ts)"),
                  {"i": sdep, "p": PID, "t": tid, "m": _json.dumps({}),
                   "ts": onset - timedelta(minutes=4)})
        changes = CC.correlate(c, PID, onset, lookback_minutes=30)
        c.execute(text("DELETE FROM stack_deployments WHERE id = :i"), {"i": sdep})
        c.execute(text("DELETE FROM deployment_targets WHERE id = :t"), {"t": tid})
    release = [ch for ch in changes if ch["key"] == "release-feedc0de1234"]
    assert release and release[0]["evidence"]["commit_sha"] == "feedc0de1234567"
    assert release[0]["evidence"]["proximity_minutes"] == 4.0
    assert "order-api" in release[0]["name"]
