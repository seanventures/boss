"""A workflow's research step skips only when governed research genuinely cannot run.

Community research is a `discovery.run` job whose every stage is a model lease on the Claude
Code subscription (discovery_spend.RESEARCH_SOURCE_ID). `research_unavailable` asks the router
the same question the job's first stage asks, pinned to that source. These tests hold the
check to that: the edition alone never decides, the question is the job's question, and the
answer names the router's reason and leaves a routing record behind.
"""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine, text

from boss_engine.config import Settings
from boss_engine.services import discovery_service, discovery_spend
from boss_engine.services import entitlements as entitlements_module

TEST_DB = os.environ.get(
    "BOSS_TEST_DATABASE_URL", "postgresql+psycopg://boss:boss@localhost:5432/boss_test"
)


class _Edition:
    def __init__(self, edition):
        self.boss_edition = edition


class _FakeBroker:
    calls: list[dict] = []
    answer: dict = {}

    def __init__(self, settings):
        self.settings = settings

    def simulate_model_route_decision(self, connection, project_id, **kwargs):
        _FakeBroker.calls.append({"project_id": project_id, **kwargs})
        return dict(_FakeBroker.answer)


@pytest.fixture()
def fake_broker(monkeypatch):
    _FakeBroker.calls = []
    monkeypatch.setattr(entitlements_module, "EntitlementBrokerService", _FakeBroker)
    return _FakeBroker


def test_other_editions_are_never_skipped_and_never_asked(fake_broker):
    assert discovery_service.research_unavailable(object(), _Edition("enterprise"), "p1") is None
    assert fake_broker.calls == []


def test_research_runs_when_the_router_grants_the_research_source(fake_broker):
    fake_broker.answer = {"id": "route_1", "selected_source_id": "anthropic_claude_code",
                          "policy_result": {"allowed": True}, "rejected_sources": []}
    assert discovery_service.research_unavailable(
        object(), _Edition("community"), "p1", "ceo") is None
    (call,) = fake_broker.calls
    # The job's own question: same skill, job kind, workload, boundary, pinned source.
    assert call["project_id"] == "p1"
    assert call["skill_id"] == discovery_spend.SKILL_ID
    assert call["task_kind"] == discovery_spend.JOB_KIND
    assert call["workload_class"] == "owner_sandbox"
    assert call["data_boundary"] == "internal"
    assert call["preferred_capabilities"] == ["research"]
    assert call["only_source_id"] == discovery_spend.RESEARCH_SOURCE_ID
    assert call["actor"] == "ceo"


def test_research_is_unavailable_with_the_routers_reason(fake_broker):
    fake_broker.answer = {
        "id": "route_2", "selected_source_id": None,
        "policy_result": {"allowed": False, "denial_reason": "no_eligible_source"},
        "rejected_sources": [{"source_id": "anthropic_claude_code",
                              "reason": "not_connected"}],
    }
    result = discovery_service.research_unavailable(object(), _Edition("community"), "p1")
    assert result["reason"] == discovery_service.RESEARCH_SOURCE_UNAVAILABLE
    assert result["source_id"] == "anthropic_claude_code"
    assert result["source_status"] == "not_connected"
    assert result["route_decision_log_id"] == "route_2"
    assert "Claude Code" in result["detail"] and "it is not connected" in result["detail"]


def test_an_undeclared_research_source_is_unavailable(fake_broker):
    fake_broker.answer = {"id": "route_3", "selected_source_id": None,
                          "policy_result": {"allowed": False}, "rejected_sources": []}
    result = discovery_service.research_unavailable(object(), _Edition("community"), "p1")
    assert result["source_status"] == "not_declared"


@pytest.fixture(scope="module")
def eng():
    engine = create_engine(TEST_DB, future=True)
    try:
        with engine.begin() as c:
            c.execute(text("SELECT 1 FROM routing_logs LIMIT 1"))
            if c.execute(text("SELECT count(*) FROM projects")).scalar() == 0:
                pytest.skip("boss_test has no project")
    except Exception as error:                      # pragma: no cover
        pytest.skip(f"boss_test unavailable: {error}")
    return engine


def test_a_fresh_community_project_without_claude_code_skips_with_a_routing_record(eng):
    """Real router, real database: a project with no Claude Code subscription connected (the
    Ollama-only install) cannot research, and the reason is on a routing log."""
    with eng.begin() as c:
        wid = c.execute(text("SELECT workspace_id FROM projects LIMIT 1")).scalar()
        pid = f"p_{uuid.uuid4().hex[:10]}"
        c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) "
                       "VALUES (:i, :w, 'research availability', :s)"),
                  {"i": pid, "w": wid, "s": pid})
    # Explicit, so a Claude login on the machine running the suite (or another test's
    # environment) cannot make the research source look connected.
    settings = Settings(boss_edition="community", anthropic_claude_oauth_enabled=False,
                        anthropic_claude_oauth_session_ref="")
    with eng.begin() as c:
        result = discovery_service.research_unavailable(c, settings, pid, "ceo")
        assert result is not None
        assert result["reason"] == discovery_service.RESEARCH_SOURCE_UNAVAILABLE
        log = c.execute(text("SELECT selected_source_id, denial_reason FROM routing_logs "
                             "WHERE id = :i"), {"i": result["route_decision_log_id"]}
                        ).mappings().first()
        assert log is not None and log["selected_source_id"] is None
        denied = c.execute(text("SELECT count(*) FROM events WHERE project_id = :p "
                                "AND event_type = 'routing.denied'"), {"p": pid}).scalar()
        assert denied == 1
