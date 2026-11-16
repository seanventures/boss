"""A dead fix run must not leave "Missing active reservation for PR proposal" pending.

The rescue that runs when a code task dies called propose_pull_request with no reservation.
That always opened a blocking decision the dead run can never answer. A live run that is
actually missing a reservation still blocks and still records the decision.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from boss_engine.config import Settings
from boss_engine.services.software_factory import SoftwareFactoryService

TITLE = "Missing active reservation for PR proposal"


def _settings() -> Settings:
    return Settings(
        BOSS_TARGET_GITHUB_OWNER="default-owner",
        BOSS_TARGET_GITHUB_REPO="default-repo",
        BOSS_GITHUB_TOKEN="legacy-token",
    )


class _Rows:
    def __init__(self, row):
        self._row = row

    def mappings(self):
        return self

    def first(self):
        return self._row


class _Conn:
    """Answers the run and job status lookups and nothing else."""

    def __init__(self, *, run: str | None = None, job: str | None = None):
        self.run = run
        self.job = job

    def execute(self, statement, _params=None):
        sql = str(statement)
        if "code_task_runs" in sql:
            return _Rows({"status": self.run} if self.run else None)
        if "FROM jobs" in sql or "jobs " in sql:
            return _Rows({"status": self.job} if self.job else None)
        return _Rows(None)


def _factory(monkeypatch):
    sf = SoftwareFactoryService(_settings(), owner="acme", repo="app")

    def fake_bind(self, connection, project_id, **kwargs):
        self.github_plane = "mission_control_binding"
        self.github_binding_id = "bind_x"
        self.github_connection_id = "conn_x"
        return {
            "github_plane": "mission_control_binding",
            "github_binding_id": "bind_x",
            "github_connection_id": "conn_x",
        }

    monkeypatch.setattr(SoftwareFactoryService, "bind_project_github", fake_bind)
    opened: list[str] = []

    class Budget:
        def assert_active_reservation(self, *_a, **_k):
            return None

    class Decisions:
        def create_blocking(self, _connection, _project, _task, title, *_a, **_k):
            opened.append(title)
            return "dec_should_not_open"

    sf.budget = Budget()
    sf.decisions = Decisions()
    return sf, opened


def _propose(sf, connection, *, run_id="crun_fix", job_id=None):
    return sf.propose_pull_request(
        connection,
        "boss-dogfood",
        "tsk_fix",
        None,
        "dedupe:dead-fix",
        "Fix the ledger",
        "boss/fix",
        "summary",
        [],
        created_by_job_id=job_id,
        run_id=run_id,
    )


def test_a_failed_fix_run_does_not_open_the_reservation_decision(monkeypatch):
    sf, opened = _factory(monkeypatch)
    result = _propose(sf, _Conn(run="failed"))
    assert opened == []
    assert result.get("decision_id") is None
    assert result["status"] == "blocked"
    assert TITLE not in str(result)


def test_a_deadlettered_fix_job_does_not_open_the_reservation_decision(monkeypatch):
    sf, opened = _factory(monkeypatch)
    result = _propose(sf, _Conn(run="running", job="deadlettered"), job_id="job_dead")
    assert opened == []
    assert result.get("decision_id") is None
    assert result["status"] == "blocked"


def test_a_live_run_missing_a_reservation_still_opens_the_decision(monkeypatch):
    sf, opened = _factory(monkeypatch)
    result = _propose(sf, _Conn(run="running"))
    assert opened == [TITLE]
    assert result["status"] == "blocked"
    assert result["decision_id"] == "dec_should_not_open"


def test_the_failure_rescue_does_not_propose_with_an_empty_reservation():
    """The exit that runs when the fix dies must not pass reservation_id=None."""
    from pathlib import Path

    import boss_engine.services.governed_code_task_service as governed

    source = Path(governed.__file__).read_text()
    start = source.index("    def _rescue_pr(")
    rescue = source[start:source.index("\n    def _mark_stage_done(", start)]
    propose = rescue[rescue.index("sf.propose_pull_request"):rescue.index(")", rescue.index("sf.propose_pull_request"))]
    assert "reservation_id=None" not in propose
    assert "run_id=run_id" in propose
