"""An agent can see why egress refused, without the raw identity log.

deny_reason, the path template and the status live on identity_egress_log. That table also
holds the run's jti and chain, so query_project_data refuses the whole table. The read an
agent gets is redacted and fenced to this project's runs.
"""
from __future__ import annotations

from sqlalchemy import create_engine, text

from boss_engine.identity import egress_read
from boss_engine.tools.builtin import _query_project_data


class _Ctx:
    def __init__(self, engine):
        self.engine = engine

    def begin(self):
        return self.engine.begin()


def _world():
    engine = create_engine("sqlite://")
    token = "boss_egr_" + "a" * 40
    with engine.begin() as c:
        c.execute(text(
            "CREATE TABLE code_task_runs (id TEXT, project_id TEXT)"))
        c.execute(text(
            "CREATE TABLE identity_egress_log ("
            "id INTEGER, ts TEXT, org_id TEXT, run_id TEXT, jti TEXT, chain TEXT, "
            "provider TEXT, method TEXT, path_template TEXT, status INTEGER, "
            "bytes_out INTEGER, bytes_in INTEGER, decision TEXT, deny_reason TEXT, "
            "authorization TEXT)"))
        c.execute(text(
            "INSERT INTO code_task_runs VALUES ('crun_mine', 'p1'), ('crun_theirs', 'p2')"))
        c.execute(text(
            "INSERT INTO identity_egress_log VALUES "
            "(1, '2026-11-05T00:00:01Z', 'org', 'crun_mine', 'jti-secret', "
            "'[\"chain-secret\"]', 'github', 'GET', '/{o}/{r}.git/info/refs', 403, "
            "0, 12, 'denied', :reason, :auth), "
            "(2, '2026-11-05T00:00:02Z', 'org', 'crun_theirs', 'jti-other', "
            "'[\"other\"]', 'github', 'GET', '/{o}/{r}/pulls', 403, "
            "0, 0, 'denied', 'repo_not_allowed', 'Bearer other')"),
            {"reason": f"repo_not_allowed Authorization: Bearer {token}",
             "auth": f"Bearer {token}"})
    return engine, token


def test_query_project_data_still_refuses_the_raw_egress_log():
    engine, _token = _world()
    out = _query_project_data(_Ctx(engine), "p1", {"table": "identity_egress_log"})
    assert out.get("ok") is False and "rows" not in out
    assert "identity_egress_log" in out["error"]


def test_a_project_reads_only_its_own_egress_decisions_and_no_secret():
    engine, token = _world()
    with engine.begin() as c:
        rows = egress_read.for_project(c, "p1")
    assert len(rows) == 1
    row = rows[0]
    assert row["run_id"] == "crun_mine"
    assert row["path_template"] == "/{o}/{r}.git/info/refs"
    assert row["status"] == 403
    assert row["decision"] == "denied"
    assert "repo_not_allowed" in row["deny_reason"]
    dumped = repr(row)
    assert token not in dumped
    assert "jti-secret" not in dumped and "chain-secret" not in dumped
    for hidden in ("jti", "chain", "authorization", "bytes_out", "bytes_in", "org_id"):
        assert hidden not in row
    with engine.begin() as c:
        assert egress_read.for_project(c, "p1", run_id="crun_theirs") == []
        assert egress_read.for_project(c, "p2")[0]["run_id"] == "crun_theirs"


def test_query_project_data_reads_the_redacted_decisions_for_this_project():
    engine, token = _world()
    out = _query_project_data(_Ctx(engine), "p1", {"table": "egress_decisions"})
    assert out.get("ok") is not False
    rows = out["rows"]
    assert len(rows) == 1
    assert rows[0]["run_id"] == "crun_mine"
    assert rows[0]["status"] == 403
    assert "repo_not_allowed" in rows[0]["deny_reason"]
    dumped = repr(rows)
    assert token not in dumped
    assert "jti-secret" not in dumped and "chain-secret" not in dumped


def test_a_denial_an_agent_reads_names_the_reason_and_nothing_else():
    said = egress_read.finding({
        "method": "GET", "path_template": "/{o}/{r}.git/info/refs", "status": 403,
        "decision": "denied", "deny_reason": "repo_not_allowed",
    })
    assert "repo_not_allowed" in said
    assert "/{o}/{r}.git/info/refs" in said
    assert "403" in said
    quiet = egress_read.finding({
        "method": "GET", "path_template": "/{o}/{r}", "status": 200,
        "decision": "allowed", "deny_reason": None,
    })
    assert quiet is None
