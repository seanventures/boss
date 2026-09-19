"""An operator can see what BOSS knows, and decide what it is allowed to follow.

Phase F0.3 / F0.4. The memory plane shipped first and was invisible: a rule BOSS proposed sat
in a table with nothing to show it, and a rule that was wrong could only be removed with SQL.
A store an operator cannot inspect is one they cannot trust, and a `proposed` lesson nobody can
see is a queue that never empties.

These exercise the HTTP surface the console calls, not the service underneath it — the service
already has its own suite, and the gap that mattered was between them.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from apps.api.app.config import get_settings
from boss_engine.services import agent_memory as mem


@pytest.fixture(autouse=True)
def _clean(engine):
    with engine.begin() as c:
        c.execute(text("DELETE FROM agent_memory"))
        c.execute(text("DELETE FROM agent_session_ledger"))
    yield


def _pid():
    return get_settings().default_project_id


def _seed(engine, **kw):
    with engine.begin() as c:
        return mem.remember(c, project_id=_pid(), actor="ceo", **kw)


class TestTheListRead:
    def test_it_returns_what_is_recorded(self, client, engine):
        _seed(engine, kind="project", body="the channel is Makkal Sinthanai")
        r = client.get(f"/v1/memory?project_id={_pid()}&actor=ceo")
        assert r.status_code == 200, r.text
        bodies = [m["body"] for m in r.json()["memories"]]
        assert "the channel is Makkal Sinthanai" in bodies

    def test_proposed_rules_come_back_flagged_not_hidden(self, client, engine):
        """One read serves the browse view and the review queue. A second endpoint for the
        queue is a second endpoint nobody opens."""
        _seed(engine, kind="lesson", body="always run the frontend checks first")
        body = client.get(f"/v1/memory?project_id={_pid()}&actor=ceo").json()
        assert body["awaiting_review"] == 1
        assert [m["status"] for m in body["memories"]] == ["proposed"]

    def test_the_count_a_badge_needs(self, client, engine):
        _seed(engine, kind="lesson", body="rule one")
        _seed(engine, kind="lesson", body="rule two")
        _seed(engine, kind="project", body="some context")
        body = client.get(f"/v1/memory?project_id={_pid()}&actor=ceo").json()
        assert body["awaiting_review"] == 2
        assert body["counts"]["project"] == 1

    def test_an_empty_project_is_not_an_error(self, client):
        r = client.get(f"/v1/memory?project_id={_pid()}&actor=ceo")
        assert r.status_code == 200
        assert r.json()["memories"] == []
        assert r.json()["awaiting_review"] == 0


class TestTheReviewGate:
    def test_accepting_makes_a_rule_bind(self, client, engine):
        made = _seed(engine, kind="lesson", body="never deploy on a Friday")
        r = client.post(f"/v1/memory/{made['id']}/review",
                        json={"project_id": _pid(), "actor": "ceo", "decision": "active"})
        assert r.status_code == 200, r.text
        with engine.begin() as c:
            assert "never deploy on a Friday" in mem.pack_for_turn(
                c, project_id=_pid(), actor="ceo")

    def test_rejecting_leaves_it_silent(self, client, engine):
        made = _seed(engine, kind="lesson", body="skip the tests")
        client.post(f"/v1/memory/{made['id']}/review",
                    json={"project_id": _pid(), "actor": "ceo", "decision": "rejected"})
        with engine.begin() as c:
            assert "skip the tests" not in mem.pack_for_turn(
                c, project_id=_pid(), actor="ceo")

    def test_a_nonsense_decision_is_refused(self, client, engine):
        made = _seed(engine, kind="lesson", body="x")
        r = client.post(f"/v1/memory/{made['id']}/review",
                        json={"project_id": _pid(), "actor": "ceo", "decision": "maybe"})
        assert r.status_code == 400

    def test_reviewing_twice_is_refused_rather_than_silently_ignored(self, client, engine):
        made = _seed(engine, kind="lesson", body="y")
        first = client.post(f"/v1/memory/{made['id']}/review",
                            json={"project_id": _pid(), "actor": "ceo", "decision": "active"})
        second = client.post(f"/v1/memory/{made['id']}/review",
                             json={"project_id": _pid(), "actor": "ceo",
                                   "decision": "rejected"})
        assert first.status_code == 200
        assert second.status_code == 400

    def test_it_will_not_act_without_an_actor(self, client, engine):
        """Accepting a rule changes what every future session is told; it is not anonymous."""
        made = _seed(engine, kind="lesson", body="z")
        r = client.post(f"/v1/memory/{made['id']}/review",
                        json={"project_id": _pid(), "decision": "active"})
        assert r.status_code == 400


class TestRetiring:
    def test_a_wrong_memory_is_removable(self, client, engine):
        made = _seed(engine, kind="project", body="no longer true")
        r = client.post(f"/v1/memory/{made['id']}/forget",
                        json={"project_id": _pid(), "actor": "ceo"})
        assert r.status_code == 200, r.text
        with engine.begin() as c:
            assert "no longer true" not in mem.pack_for_turn(
                c, project_id=_pid(), actor="ceo")

    def test_retired_not_deleted(self, client, engine):
        made = _seed(engine, kind="project", body="keep the audit trail")
        client.post(f"/v1/memory/{made['id']}/forget",
                    json={"project_id": _pid(), "actor": "ceo"})
        with engine.begin() as c:
            status = c.execute(text("SELECT status FROM agent_memory WHERE id=:i"),
                               {"i": made["id"]}).scalar_one()
        assert status == "retired"

    def test_forgetting_something_that_is_not_there(self, client):
        r = client.post("/v1/memory/mem_nope/forget",
                        json={"project_id": _pid(), "actor": "ceo"})
        assert r.status_code == 400


class TestTheLedgerRead:
    def test_an_open_ledger_is_returned(self, client, engine):
        with engine.begin() as c:
            mem.write_ledger(c, project_id=_pid(), thread_id="athr_ui", actor="ceo",
                             goal="ship the console for phase 0",
                             next_step="mount the strip")
        body = client.get("/v1/agent/threads/athr_ui/ledger").json()
        assert body["ledger"]["goal"] == "ship the console for phase 0"
        assert body["ledger"]["next_step"] == "mount the strip"

    def test_a_finished_one_is_not(self, client, engine):
        """What stops a completed ledger steering a session that has moved on."""
        with engine.begin() as c:
            mem.write_ledger(c, project_id=_pid(), thread_id="athr_ui", actor="ceo", goal="g")
            mem.finish_ledger(c, thread_id="athr_ui", actor="ceo")
        assert client.get("/v1/agent/threads/athr_ui/ledger").json()["ledger"] is None

    def test_a_thread_without_one_is_not_an_error(self, client):
        """Most conversations have no long-horizon work; the strip renders nothing."""
        r = client.get("/v1/agent/threads/athr_none/ledger")
        assert r.status_code == 200
        assert r.json()["ledger"] is None
