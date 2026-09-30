"""HTTP surface for LearningLoopService + IntentRouter.

Covers:
- POST /v1/learning/examples
- GET  /v1/learning/corpus
- GET  /v1/learning/evaluate
- POST /v1/learning/suggest-tuning
- POST /v1/intent/classify
- POST /v1/intent/route

Fail-closed validation returns 422 on blank / missing required fields.
"""

from __future__ import annotations

PROJECT_ID = "boss-dogfood"
ACTOR = "executive_console"
AGENT = "ll_api_test"


def test_learning_record_corpus_evaluate_and_tuning(client):
    # Record a small labeled corpus.
    for outcome, label, sigs in (
        ("confirmed", "real", ["sig_good"]),
        ("confirmed", "real", ["sig_good"]),
        ("dismissed", "false_alarm", ["noisy_sig"]),
        ("dismissed", "false_alarm", ["noisy_sig"]),
        ("fix_good", "fix", []),
    ):
        r = client.post(
            "/v1/learning/examples",
            json={
                "project_id": PROJECT_ID,
                "actor": ACTOR,
                "agent": AGENT,
                "kind": "incident_feedback",
                "label": label,
                "outcome": outcome,
                "ref": f"ref-{outcome}",
                "payload": {"signatures": sigs},
            },
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["id"].startswith("lex")
        assert body["agent"] == AGENT
        assert body["kind"] == "incident_feedback"

    corpus = client.get(
        "/v1/learning/corpus",
        params={"project_id": PROJECT_ID, "agent": AGENT},
    )
    assert corpus.status_code == 200, corpus.text
    cbody = corpus.json()
    assert cbody["count"] == 5
    assert len(cbody["items"]) == 5
    assert all(item["agent"] == AGENT for item in cbody["items"])

    filtered = client.get(
        "/v1/learning/corpus",
        params={"project_id": PROJECT_ID, "agent": AGENT, "kind": "incident_feedback"},
    )
    assert filtered.status_code == 200
    assert filtered.json()["count"] == 5

    empty_kind = client.get(
        "/v1/learning/corpus",
        params={"project_id": PROJECT_ID, "agent": AGENT, "kind": "other"},
    )
    assert empty_kind.status_code == 200
    assert empty_kind.json()["count"] == 0

    evaluate = client.get(
        "/v1/learning/evaluate",
        params={"project_id": PROJECT_ID, "agent": AGENT},
    )
    assert evaluate.status_code == 200, evaluate.text
    ebody = evaluate.json()
    assert ebody["total"] == 5
    assert ebody["by_outcome"]["confirmed"] == 2
    assert ebody["by_outcome"]["dismissed"] == 2
    assert ebody["false_alarm_rate"] == round(2 / 4, 3)
    assert ebody["fix_success_rate"] == 1.0

    tuning = client.post(
        "/v1/learning/suggest-tuning",
        json={"project_id": PROJECT_ID, "agent": AGENT, "actor": ACTOR},
    )
    assert tuning.status_code == 200, tuning.text
    tbody = tuning.json()
    raised = {row["signature"] for row in tbody["raise_threshold"]}
    assert "noisy_sig" in raised
    assert any(f.get("ref") == "ref-confirmed" for f in tbody["few_shot_candidates"])


def test_learning_record_fails_closed_on_bad_input(client):
    missing = client.post("/v1/learning/examples", json={})
    assert missing.status_code == 422, missing.text

    blank = client.post(
        "/v1/learning/examples",
        json={
            "project_id": "  ",
            "actor": ACTOR,
            "agent": AGENT,
            "kind": "incident_feedback",
        },
    )
    assert blank.status_code == 422, blank.text

    blank_agent = client.post(
        "/v1/learning/examples",
        json={
            "project_id": PROJECT_ID,
            "actor": ACTOR,
            "agent": "",
            "kind": "incident_feedback",
        },
    )
    assert blank_agent.status_code == 422, blank_agent.text

    blank_kind = client.post(
        "/v1/learning/examples",
        json={
            "project_id": PROJECT_ID,
            "actor": ACTOR,
            "agent": AGENT,
            "kind": "   ",
        },
    )
    assert blank_kind.status_code == 422, blank_kind.text


def test_learning_get_fails_closed_on_missing_query(client):
    no_project = client.get("/v1/learning/corpus", params={"agent": AGENT})
    assert no_project.status_code == 422, no_project.text

    no_agent = client.get("/v1/learning/evaluate", params={"project_id": PROJECT_ID})
    assert no_agent.status_code == 422, no_agent.text

    blank_project = client.get(
        "/v1/learning/corpus",
        params={"project_id": "", "agent": AGENT},
    )
    assert blank_project.status_code == 422, blank_project.text

    blank_agent = client.get(
        "/v1/learning/evaluate",
        params={"project_id": PROJECT_ID, "agent": ""},
    )
    assert blank_agent.status_code == 422, blank_agent.text


def test_learning_suggest_tuning_fails_closed(client):
    missing = client.post("/v1/learning/suggest-tuning", json={})
    assert missing.status_code == 422, missing.text

    blank = client.post(
        "/v1/learning/suggest-tuning",
        json={"project_id": " ", "agent": " ", "actor": ACTOR},
    )
    assert blank.status_code == 422, blank.text


def test_intent_classify_deterministic(client):
    code = client.post(
        "/v1/intent/classify",
        json={"message": "Fix the typo in the footer"},
    )
    assert code.status_code == 200, code.text
    body = code.json()
    assert body["intent"] == "create_code_task"
    assert "instructions" in body["arguments"]

    meeting = client.post(
        "/v1/intent/classify",
        json={"message": "Should we use Postgres or Mongo?"},
    )
    assert meeting.status_code == 200, meeting.text
    assert meeting.json()["intent"] == "create_meeting"

    discuss = client.post(
        "/v1/intent/classify",
        json={"message": "hello there"},
    )
    assert discuss.status_code == 200, discuss.text
    assert discuss.json()["intent"] is None
    assert discuss.json()["arguments"] == {}


def test_intent_classify_fails_closed(client):
    missing = client.post("/v1/intent/classify", json={})
    assert missing.status_code == 422, missing.text

    blank = client.post("/v1/intent/classify", json={"message": "   "})
    assert blank.status_code == 422, blank.text


def test_intent_route_discuss_and_capability(client):
    caps = client.post(
        "/v1/intent/route",
        json={
            "project_id": PROJECT_ID,
            "actor": ACTOR,
            "messages": [{"role": "user", "content": "What can you do?"}],
        },
    )
    assert caps.status_code == 200, caps.text
    body = caps.json()
    assert "I can currently" in body["reply"]
    assert body["actions"] == []
    assert "tool_calls" in body

    # Explicit code intent actuates when the actor has task_create (executive_console).
    code = client.post(
        "/v1/intent/route",
        json={
            "project_id": PROJECT_ID,
            "actor": ACTOR,
            "messages": [{"role": "user", "content": "Fix the typo in the footer"}],
        },
    )
    assert code.status_code == 200, code.text
    cbody = code.json()
    assert cbody["actions"], cbody
    assert cbody["actions"][0]["kind"] == "code_task_created"


def test_intent_route_fails_closed(client):
    missing = client.post("/v1/intent/route", json={})
    assert missing.status_code == 422, missing.text

    blank_project = client.post(
        "/v1/intent/route",
        json={
            "project_id": "  ",
            "actor": ACTOR,
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert blank_project.status_code == 422, blank_project.text

    empty_messages = client.post(
        "/v1/intent/route",
        json={"project_id": PROJECT_ID, "actor": ACTOR, "messages": []},
    )
    assert empty_messages.status_code == 422, empty_messages.text

    blank_content = client.post(
        "/v1/intent/route",
        json={
            "project_id": PROJECT_ID,
            "actor": ACTOR,
            "messages": [{"role": "user", "content": "  "}],
        },
    )
    assert blank_content.status_code == 422, blank_content.text


def test_intent_route_requires_agent_chat_permission(client):
    response = client.post(
        "/v1/intent/route",
        json={
            "project_id": PROJECT_ID,
            "actor": "cao",
            "messages": [{"role": "user", "content": "What can you do?"}],
        },
    )
    assert response.status_code == 403, response.text
    assert "agent_chat" in response.text
