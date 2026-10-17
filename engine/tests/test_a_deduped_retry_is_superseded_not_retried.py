"""A retry that start_code_task dedupes onto the live run is a supersession, not a new attempt.

2026-10-13 18:39–19:11: shepherd_watch retried crun_37f4ccef (a failed build of the older run
wfr_a7ba) every two minutes. Each time start_code_task found the same app already building as
crun_247435e1 and returned THAT id; `_retry_run` counted any returned id as a new attempt, and
`run_liveness.superseded_by` judged "same work" by lineage/fingerprint only — not by the same-app
rule the start dedupes on — so the next sweep retried again: 17 retries, 17 duplicate_suppressed.

  (1) start_code_task reports whether it created a run (`outcome`), and retry_code_task carries
      it (`created`);
  (2) `_retry_run` counts a dedupe hit as superseded (0), never as a remedy;
  (3) `superseded_by` uses the start's own rule (`in_flight_same_work`), so the next sweep sees
      the dead run as superseded and does not retry it at all.
"""
from __future__ import annotations

import os
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text

from boss_engine.services import code_task_service as cts
from boss_engine.services import run_liveness, shepherd_service

URL = os.environ.get("BOSS_TEST_DATABASE_URL", "")

TABLE = """
CREATE TABLE code_task_runs (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, task_id TEXT,
  status TEXT NOT NULL DEFAULT 'running', phase TEXT NOT NULL DEFAULT 'queued',
  title TEXT NOT NULL, spec JSONB NOT NULL DEFAULT '{}'::jsonb,
  state JSONB NOT NULL DEFAULT '{}'::jsonb, usage JSONB NOT NULL DEFAULT '{}'::jsonb,
  finalize_ctx JSONB NOT NULL DEFAULT '{}'::jsonb, reservation_id TEXT,
  pull_request_url TEXT, pull_request_number INTEGER, decision_id TEXT, error TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now())
"""


@pytest.fixture()
def db():
    """A private schema with just the run table: nothing another suite uses is touched."""
    if not URL:
        pytest.skip("BOSS_TEST_DATABASE_URL is not set")
    assert "test" in URL.rsplit("/", 1)[-1]
    schema = f"t_dedupe_{uuid.uuid4().hex[:8]}"
    base = create_engine(URL.replace("postgresql://", "postgresql+psycopg://"))
    with base.begin() as c:
        c.execute(text(f"CREATE SCHEMA {schema}"))
    eng = create_engine(URL.replace("postgresql://", "postgresql+psycopg://"),
                        connect_args={"options": f"-csearch_path={schema}"})
    with eng.begin() as c:
        c.execute(text(TABLE))
    try:
        yield eng
    finally:
        eng.dispose()
        with base.begin() as c:
            c.execute(text(f"DROP SCHEMA {schema} CASCADE"))
        base.dispose()


def _run(c, rid, *, status, title, fp, minutes_ago, root=None):
    c.execute(text(
        "INSERT INTO code_task_runs (id, project_id, status, title, spec, finalize_ctx, created_at) "
        "VALUES (:i, 'p1', :s, :t, CAST(:spec AS jsonb), CAST(:f AS jsonb), "
        "        now() - make_interval(mins => :m))"),
        {"i": rid, "s": status, "t": title, "m": minutes_ago,
         "spec": f'{{"repo": "apps", "work_fingerprint": "{fp}"}}',
         "f": f'{{"root_run_id": "{root}"}}' if root else "{}"})


def test_superseded_by_uses_the_starts_same_work_rule(db):
    with db.begin() as c:
        # The incident: a failed build of the older workflow run, and the live build of the SAME
        # APP from the newer one — different briefs, so different fingerprints, different lineages.
        _run(c, "crun_37f4ccef", status="failed", title="HelpDesk AI — support app",
             fp="fp-old", minutes_ago=60)
        _run(c, "crun_247435e1", status="running", title="HelpDesk AI — support app, take 2",
             fp="fp-new", minutes_ago=20)
        _run(c, "crun_other", status="failed", title="Shorty — URL shortener", fp="fp-s",
             minutes_ago=50)
    with db.begin() as c:
        # The start would dedupe this work onto the live run…
        assert cts.in_flight_same_work(c, "p1", fingerprint="fp-old", repo="apps",
                                       title="HelpDesk AI — support app") == "crun_247435e1"
        # …so supersession says the same.
        assert run_liveness.superseded_by(c, "crun_37f4ccef") == "crun_247435e1"
        # Different app: not superseded.
        assert run_liveness.superseded_by(c, "crun_other") is None
        # A live run is never "superseded" by an older keeper, nor by itself.
        _run(c, "crun_late", status="queued", title="HelpDesk AI — again", fp="fp-x", minutes_ago=1)
        assert run_liveness.superseded_by(c, "crun_247435e1") == "crun_late"
        assert run_liveness.superseded_by(c, "crun_late") is None


def test_start_code_task_reports_a_dedupe_hit(db, monkeypatch):
    monkeypatch.setattr(cts, "emit_event", lambda *a, **k: None)
    with db.begin() as c:
        _run(c, "crun_live", status="running", title="HelpDesk AI — support app", fp="fp-live",
             minutes_ago=5)
    outcome: dict = {}
    with db.begin() as c:
        got = cts.start_code_task(
            c, project_id="p1", task_id=None, title="HelpDesk AI — support app · retry 2",
            instructions="Build it again", acceptance_criteria=[], test_commands=[],
            owner="o", repo="apps", base_branch="main", finalize_ctx={},
            jobs=SimpleNamespace(), outcome=outcome)
    assert got == "crun_live"
    assert outcome == {"created": False, "run_id": "crun_live"}


def test_a_dedupe_hit_is_not_counted_as_a_retry(monkeypatch):
    """`_retry_run` returns 0 — no shepherd.retry, no "acted" — when the retry was deduped."""
    from boss_engine.services import code_task_service

    class _Begin:
        def __enter__(self):
            return object()

        def __exit__(self, *a):
            return False

    monkeypatch.setattr("boss_engine.db.begin", lambda engine: _Begin())
    monkeypatch.setattr(run_liveness, "superseded_by", lambda c, rid: None)
    monkeypatch.setattr(code_task_service, "retry_code_task",
                        lambda *a, **k: {"status": "queued", "run_id": "crun_247435e1",
                                         "created": False})
    assert shepherd_service._retry_run(None, None, None, "crun_37f4ccef", "killed") == 0
    monkeypatch.setattr(code_task_service, "retry_code_task",
                        lambda *a, **k: {"status": "queued", "run_id": "crun_new", "created": True})
    assert shepherd_service._retry_run(None, None, None, "crun_37f4ccef", "killed") == 1


def test_a_next_attempt_is_not_deduped_onto_the_attempt_it_replaces(db):
    """HelpDesk AI crun_f18bdf2db6144534ac27: a review-repair round started its child from
    inside the parent's pipeline, while the parent was still `running`. The same-app guard found
    the parent itself and suppressed the child; the parent went to `repairing` with no child and
    the build stopped for good. The attempt being replaced is never "the same work in flight"."""
    with db.begin() as c:
        _run(c, "crun_parent", status="running", title="HelpDesk AI: a vivid look",
             fp="fp-1", minutes_ago=5)
        assert cts.in_flight_same_work(c, "p1", fingerprint="fp-1", repo="apps",
                                       title="HelpDesk AI: a vivid look · retry 1") == "crun_parent"
        assert cts.in_flight_same_work(c, "p1", fingerprint="fp-1", repo="apps",
                                       title="HelpDesk AI: a vivid look · retry 1",
                                       exclude="crun_parent") is None
        # Another LIVE run of the same app is still caught.
        _run(c, "crun_other", status="running", title="HelpDesk AI: a vivid look",
             fp="fp-2", minutes_ago=3)
        assert cts.in_flight_same_work(c, "p1", fingerprint="fp-1", repo="apps",
                                       title="HelpDesk AI: a vivid look · retry 1",
                                       exclude="crun_parent") == "crun_other"


def test_retry_tells_the_start_which_attempt_it_supersedes():
    import inspect
    src = inspect.getsource(cts.retry_code_task)
    assert "supersedes=run_id" in src
    assert "supersedes" in inspect.signature(cts.start_code_task).parameters
