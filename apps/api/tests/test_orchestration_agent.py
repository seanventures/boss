"""The orchestration agent's governed paths. A MODEL decides each turn's action
(agent_decision, 2026-10-18); these tests stand in for it with `decides`, a decision model that
answers per message, and assert what BOSS then does with the decision."""
from __future__ import annotations

import pytest
from sqlalchemy import text

from boss_engine.services import agent_decision
from boss_engine.services.intent_router import IntentRouter as EngineRouter, RouterTurn

from apps.api.app.config import get_settings
from apps.api.app.services.capability_manifest import build_manifest
from apps.api.app.services.intent_router import IntentRouter


def _payload(message: str, **overrides):
    payload = {
        "project_id": get_settings().default_project_id,
        "actor": "executive_console",
        "message": message,
    }
    payload.update(overrides)
    return payload


@pytest.fixture()
def decides(monkeypatch):
    """`decides({message: action})`: a model is connected, and it decides these actions. The
    answering agent is a stub that records whether the turn may write."""
    answered: list = []

    def install(actions: dict):
        def decide(connection, settings, project_id, *, message, candidates, **_k):
            action = actions[message]
            assert action in {c.key for c in candidates}, action
            return agent_decision.AgentDecision(agent_decision.DECIDED, action=action,
                                                confidence=0.95, by={"backend": "stub"})

        monkeypatch.setattr(agent_decision, "decide", decide)
        monkeypatch.setattr(EngineRouter, "_a_model_can_answer", lambda *a, **k: True)

        def answer(self, connection, project_id, messages, **kwargs):
            answered.append({"message": messages[-1]["content"], "writes": kwargs.get("writes")})
            return RouterTurn(reply="I can currently answer that.")

        monkeypatch.setattr(EngineRouter, "_answer", answer)
        return answered
    return install


def test_chat_persists_thread_messages_and_turn_events(client, engine, decides):
    decides({"What can you do?": "answer", "What capabilities are unavailable?": "answer"})
    first = client.post("/v1/agent/chat", json=_payload("What can you do?"))
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["thread_id"].startswith("athr_")
    assert [message["role"] for message in body["messages"]] == ["user", "assistant"]
    assert "I can currently" in body["reply"]

    second = client.post(
        "/v1/agent/chat",
        json=_payload("What capabilities are unavailable?", thread_id=body["thread_id"]),
    )
    assert second.status_code == 200, second.text
    assert len(second.json()["messages"]) == 4

    with engine.begin() as connection:
        assert connection.execute(
            text("SELECT count(*) FROM agent_messages WHERE thread_id = :id"),
            {"id": body["thread_id"]},
        ).scalar_one() == 4
        assert connection.execute(
            text(
                "SELECT count(*) FROM events WHERE entity_id = :id "
                "AND event_type = 'agent.turn_completed'"
            ),
            {"id": body["thread_id"]},
        ).scalar_one() == 2


def test_chat_requires_agent_chat_permission(client):
    response = client.post(
        "/v1/agent/chat",
        json=_payload("What can you do?", actor="cao"),
    )
    assert response.status_code == 403
    assert "agent_chat" in response.text


def test_new_code_work_goes_to_the_agent_with_writes_never_straight_to_a_build(
        client, engine, decides):
    """"Fix the typo in the footer" used to start a governed build from the word "fix". Now the
    decision says it is new code work, and the agent — which offers create_code_task as a
    button (EngineAgent.PROPOSE_ONLY) — gets the turn with writes allowed."""
    answered = decides({"Fix the typo in the footer": "new_code_change"})
    response = client.post(
        "/v1/agent/chat", json=_payload("Fix the typo in the footer")
    )
    assert response.status_code == 200, response.text
    assert answered == [{"message": "Fix the typo in the footer", "writes": True}]
    with engine.begin() as connection:
        assert connection.execute(text("SELECT count(*) FROM code_task_runs")).scalar_one() == 0


def test_open_question_routes_to_meeting_not_code(client, engine, decides):
    decides({"Should we use Postgres or Mongo?": "create_meeting"})
    response = client.post(
        "/v1/agent/chat", json=_payload("Should we use Postgres or Mongo?")
    )
    assert response.status_code == 200, response.text
    action = response.json()["actions"][0]
    assert action["kind"] == "meeting_created"
    with engine.begin() as connection:
        assert connection.execute(
            text("SELECT count(*) FROM code_task_runs")
        ).scalar_one() == 0


def test_tool_permission_failure_is_a_clean_refusal(client, engine, decides):
    message = "First research auth providers and then implement the login page"
    decides({message: "propose_command"})
    response = client.post(
        "/v1/agent/chat",
        json=_payload(message, actor="cto"),
    )
    assert response.status_code == 200, response.text
    assert response.json()["actions"] == []
    assert "lacks permission 'command_submit'" in response.json()["reply"]
    with engine.begin() as connection:
        assert connection.execute(text("SELECT count(*) FROM tasks")).scalar_one() == 0


def test_disabled_manifest_capability_is_not_actuated(engine, decides):
    decides({"Write a PRD for a customer portal": "create_prd"})
    settings = get_settings().model_copy(
        update={"openai_api_key": None, "agent_router_llm_enabled": False}
    )
    project_id = settings.default_project_id
    with engine.begin() as connection:
        manifest = build_manifest(connection, settings, project_id)
        turn = IntentRouter(settings).route_turn(
            connection,
            project_id=project_id,
            actor="executive_console",
            messages=[{"role": "user", "content": "Write a PRD for a customer portal"}],
            manifest=manifest,
        )
        assert turn.actions == []
        assert "No model backend" in turn.reply
        assert connection.execute(
            text("SELECT count(*) FROM meeting_runs")
        ).scalar_one() == 0


def test_community_discovery_from_the_agent_is_a_governed_job_with_no_run_wide_reservation(
    engine, monkeypatch, decides
):
    """In Community each research stage reserves, leases and receipts itself
    (discovery_spend.py). A run-wide reservation here would never be settled."""
    settings = get_settings().model_copy(
        update={"openai_api_key": "test-model-key", "plan_budget_usd_micros": 10_000,
                "boss_edition": "community"}
    )
    decides({"Research auth providers": "run_discovery"})
    started: list[dict] = []
    monkeypatch.setattr(
        "apps.api.app.services.intent_router.start_discovery",
        lambda **kwargs: started.append(kwargs) or "drun_governed",
    )
    with engine.begin() as connection:
        before = connection.execute(text("SELECT count(*) FROM budget_reservations")).scalar_one()
        manifest = build_manifest(connection, settings, settings.default_project_id)
        turn = IntentRouter(settings).route_turn(
            connection,
            project_id=settings.default_project_id,
            actor="executive_console",
            messages=[{"role": "user", "content": "Research auth providers"}],
            manifest=manifest,
        )
        assert turn.actions[0]["kind"] == "discovery_started"
        assert "governance" not in started[0]
        assert started[0]["actor"] == "executive_console"
        assert connection.execute(
            text("SELECT count(*) FROM budget_reservations")).scalar_one() == before


def test_discovery_tool_reserves_budget_and_creates_receipt_before_start(
    engine, monkeypatch, decides
):
    """Outside Community the legacy in-process path keeps its run-wide reservation."""
    settings = get_settings().model_copy(
        update={"openai_api_key": "test-model-key", "plan_budget_usd_micros": 10_000,
                "boss_edition": "enterprise"}
    )
    decides({"Research auth providers": "run_discovery"})
    started: list[dict] = []
    monkeypatch.setattr(
        "apps.api.app.services.intent_router.start_discovery",
        lambda **kwargs: started.append(kwargs) or "drun_governed",
    )
    with engine.begin() as connection:
        manifest = build_manifest(connection, settings, settings.default_project_id)
        turn = IntentRouter(settings).route_turn(
            connection,
            project_id=settings.default_project_id,
            actor="executive_console",
            messages=[{"role": "user", "content": "Research auth providers"}],
            manifest=manifest,
        )
        assert turn.actions[0]["kind"] == "discovery_started"
        governance = started[0]["governance"]
        assert connection.execute(
            text("SELECT status FROM budget_reservations WHERE id = :id"),
            {"id": governance["reservation_id"]},
        ).scalar_one() == "active"
        assert connection.execute(
            text("SELECT status FROM receipts WHERE id = :id"),
            {"id": governance["receipt_id"]},
        ).scalar_one() == "in_progress"


def test_broad_ask_proposes_then_explicit_confirmation_dispatches(client, engine, decides):
    decides({"First research auth providers and then implement the login page":
             "propose_command"})
    proposed_response = client.post(
        "/v1/agent/chat",
        json=_payload("First research auth providers and then implement the login page"),
    )
    assert proposed_response.status_code == 200, proposed_response.text
    proposed = proposed_response.json()
    pending = proposed["pending_confirmation"]
    assert pending["phase_count"] == 2
    assert proposed["actions"][0]["requires_confirmation"] is True

    with engine.begin() as connection:
        parent_id = proposed["actions"][0]["task_id"]
        assert connection.execute(
            text("SELECT count(*) FROM tasks WHERE parent_task_id = :parent"),
            {"parent": parent_id},
        ).scalar_one() == 0

    confirmed_response = client.post(
        "/v1/agent/chat",
        json=_payload(
            "Confirm this plan",
            thread_id=proposed["thread_id"],
            confirm_plan_id=pending["plan_id"],
        ),
    )
    assert confirmed_response.status_code == 200, confirmed_response.text
    confirmed = confirmed_response.json()
    assert confirmed["actions"][0]["kind"] == "plan_dispatched"
    assert confirmed["pending_confirmation"] is None

    with engine.begin() as connection:
        children = connection.execute(
            text(
                "SELECT id, dependency_task_ids FROM tasks "
                "WHERE parent_task_id = :parent ORDER BY created_at, id"
            ),
            {"parent": parent_id},
        ).mappings().all()
    assert len(children) == 2
    first = next(child for child in children if child["dependency_task_ids"] == [])
    second = next(child for child in children if child["dependency_task_ids"])
    assert second["dependency_task_ids"] == [first["id"]]


def test_revision_and_discussion_replies_never_dispatch(client, engine, decides):
    decides({"First research auth providers and then implement login": "propose_command",
             "What does this plan cover?": "answer",
             "No, change it to passwordless auth": "revise_plan"})
    proposed = client.post(
        "/v1/agent/chat",
        json=_payload("First research auth providers and then implement login"),
    ).json()
    old_plan_id = proposed["pending_confirmation"]["plan_id"]
    parent_id = proposed["actions"][0]["task_id"]

    ambiguous = client.post(
        "/v1/agent/chat",
        json=_payload("What does this plan cover?", thread_id=proposed["thread_id"]),
    )
    assert ambiguous.status_code == 200, ambiguous.text
    assert ambiguous.json()["actions"] == []
    assert ambiguous.json()["pending_confirmation"] is None
    with engine.begin() as connection:
        assert connection.execute(
            text("SELECT pending_plan_id FROM agent_threads WHERE id = :thread_id"),
            {"thread_id": proposed["thread_id"]},
        ).scalar_one() == old_plan_id

    revised = client.post(
        "/v1/agent/chat",
        json=_payload(
            "No, change it to passwordless auth",
            thread_id=proposed["thread_id"],
        ),
    )
    assert revised.status_code == 200, revised.text
    new_pending = revised.json()["pending_confirmation"]
    assert new_pending["plan_id"] != old_plan_id
    with engine.begin() as connection:
        assert connection.execute(
            text("SELECT count(*) FROM tasks WHERE parent_task_id = :parent"),
            {"parent": parent_id},
        ).scalar_one() == 0
