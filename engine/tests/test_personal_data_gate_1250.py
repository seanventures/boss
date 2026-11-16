"""A builder message is checked for personal data before any budget reservation (BOSSAPP-1250).

The check is local and deterministic: an email address, a phone number, a card number
(Luhn), and a national-id-like pattern. A normal app prompt is allowed. A card or a
national id is a block and does not reserve, even if the caller says it is allowed.
An email or a phone is an ask: the gate returns a decision and does not reserve until
that ask is allowed. Nothing here calls an external classifier.
"""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from boss_engine.services.personal_data_gate import before_builder_reserve


class _Budget:
    """A stand-in for the budget edge. Records a reservation only when called."""

    def __init__(self) -> None:
        self.calls = 0
        self.created: list[dict[str, str]] = []

    def reserve(self) -> dict[str, str]:
        self.calls += 1
        reservation = {"reservation_id": f"res-{self.calls}", "status": "reserved"}
        self.created.append(reservation)
        return reservation


@pytest.mark.parametrize(
    ("message", "action", "findings", "hidden"),
    [
        ("Build a notes app with folders, search, and a share link.", "allow", [], "notes app"),
        (
            "Build a form that asks for an email, a phone, a card, and a national id.",
            "allow",
            [],
            "national id",
        ),
        ("Track order 1234567890123456 in the admin view.", "allow", [], "1234567890123456"),
        ("Reference 4111111111111112 is an internal id.", "allow", [], "4111111111111112"),
        (
            "Start from git@github.com:acme/notes.git and bind port 55432.",
            "allow",
            [],
            "git@github.com",
        ),
        ("Ship the 2024-11-15 notes build.", "allow", [], "2024-11-15"),
        ("Build v1.2.3 and listen on port 8080.", "allow", [], "8080"),
        ("Send the build log to ada@example.com.", "ask", ["email"], "ada@example.com"),
        (
            "Reach ada+boss@example.com about the notes app.",
            "ask",
            ["email"],
            "ada+boss@example.com",
        ),
        ("Call the on-call line at 415-555-0134 before launch.", "ask", ["phone"], "415-555-0134"),
        ("The desk line is +44 20 7946 0958.", "ask", ["phone"], "+44 20 7946 0958"),
        (
            "Email ada@example.com or call 415-555-0134.",
            "ask",
            ["email", "phone"],
            "ada@example.com",
        ),
        ("The customer's card is 4111 1111 1111 1111.", "block", ["card"], "4111"),
        ("Charge 4111111111111111 now.", "block", ["card"], "4111111111111111"),
        ("The signer national id is 123-45-6789.", "block", ["national_id"], "123-45-6789"),
        (
            "Mail ada@example.com about card 4111111111111111.",
            "block",
            ["card", "email"],
            "4111111111111111",
        ),
    ],
)
def test_a_builder_message_is_allowed_asked_or_blocked(message, action, findings, hidden):
    """Classification is local. The matched value is not copied into the decision."""
    result = before_builder_reserve(message)
    assert result["action"] == action
    assert result["findings"] == findings
    assert result["reserved"] is False
    assert result["reservation"] is None
    if action == "allow":
        assert result["decision"] is None
    elif action == "ask":
        assert result["decision"]["kind"] == "personal_data"
        assert result["decision"]["status"] == "pending"
        assert result["decision"]["findings"] == findings
    else:
        assert result["decision"]["kind"] == "personal_data"
        assert result["decision"]["status"] == "blocked"
        assert result["decision"]["findings"] == findings
        assert result["error"] == "personal_data_blocked"
    assert hidden not in json.dumps(result)
    assert message not in json.dumps(result)


def test_an_empty_builder_message_is_allowed():
    result = before_builder_reserve("")
    assert result["action"] == "allow"
    assert result["findings"] == []
    assert result["decision"] is None
    assert result["reserved"] is False
    assert result["reservation"] is None


def test_a_missing_message_is_allowed_and_can_reserve():
    budget = _Budget()
    result = before_builder_reserve(None, reserve=budget.reserve)
    assert result["action"] == "allow"
    assert result["findings"] == []
    assert result["reserved"] is True
    assert result["reservation"] == {"reservation_id": "res-1", "status": "reserved"}
    assert budget.calls == 1


def test_a_normal_app_prompt_is_allowed_to_reserve():
    budget = _Budget()
    message = "Build a notes app with folders, search, and a share link."
    result = before_builder_reserve(message, reserve=budget.reserve)
    assert result["action"] == "allow"
    assert result["findings"] == []
    assert result["decision"] is None
    assert result["reserved"] is True
    assert result["reservation"]["status"] == "reserved"
    assert budget.calls == 1
    assert budget.created == [result["reservation"]]


def test_an_ask_returns_a_decision_and_does_not_reserve_until_allowed():
    budget = _Budget()
    message = "Send the build log to ada@example.com."
    asked = before_builder_reserve(message, reserve=budget.reserve)
    assert asked["action"] == "ask"
    assert asked["decision"] == {
        "kind": "personal_data",
        "status": "pending",
        "findings": ["email"],
    }
    assert asked["reserved"] is False
    assert asked["reservation"] is None
    assert budget.calls == 0
    assert budget.created == []
    assert "ada@example.com" not in json.dumps(asked)

    allowed = before_builder_reserve(message, allowed=True, reserve=budget.reserve)
    assert allowed["action"] == "allow"
    assert allowed["decision"] == {
        "kind": "personal_data",
        "status": "allowed",
        "findings": ["email"],
    }
    assert allowed["reserved"] is True
    assert allowed["reservation"] == {"reservation_id": "res-1", "status": "reserved"}
    assert budget.calls == 1
    assert "ada@example.com" not in json.dumps(allowed)


def test_a_phone_ask_does_not_reserve_until_allowed():
    budget = _Budget()
    message = "Call the on-call line at 415-555-0134 before launch."
    asked = before_builder_reserve(message, allowed=True, reserve=budget.reserve)
    assert asked["action"] == "allow"
    assert asked["decision"]["status"] == "allowed"
    assert asked["decision"]["findings"] == ["phone"]
    assert budget.calls == 1
    held = _Budget()
    pending = before_builder_reserve(message, reserve=held.reserve)
    assert pending["action"] == "ask"
    assert pending["reserved"] is False
    assert held.calls == 0
    assert "415-555-0134" not in json.dumps(pending)


@pytest.mark.parametrize(
    ("message", "findings"),
    [
        ("The customer's card is 4111 1111 1111 1111.", ["card"]),
        ("Charge 4111111111111111 now.", ["card"]),
        ("The signer national id is 123-45-6789.", ["national_id"]),
        ("Mail ada@example.com about card 4111111111111111.", ["card", "email"]),
        ("Call 415-555-0134 about national id 123-45-6789.", ["national_id", "phone"]),
    ],
)
def test_a_block_does_not_create_a_reservation(message, findings):
    budget = _Budget()

    def boom():
        budget.calls += 1
        raise RuntimeError("budget down")

    result = before_builder_reserve(message, allowed=True, reserve=boom)
    assert result["action"] == "block"
    assert result["findings"] == findings
    assert result["decision"]["status"] == "blocked"
    assert result["error"] == "personal_data_blocked"
    assert result["reserved"] is False
    assert result["reservation"] is None
    assert budget.calls == 0
    assert budget.created == []


def test_a_reserve_failure_is_not_swallowed_once_the_message_is_allowed():
    def boom():
        raise RuntimeError("budget down")

    with pytest.raises(RuntimeError, match="budget down"):
        before_builder_reserve(
            "Build a notes app with folders, search, and a share link.",
            reserve=boom,
        )


def test_the_check_stays_on_this_machine(monkeypatch):
    import socket

    def refuse(*_args, **_kwargs):
        raise AssertionError("personal data check opened a socket")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    result = before_builder_reserve("Send the build log to ada@example.com.")
    assert result["action"] == "ask"
    assert result["findings"] == ["email"]


def _client() -> TestClient:
    from apps.api.app.routers.gap_1250 import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_the_gap_router_is_the_personal_data_gate():
    from apps.api.app.routers.gap_1250 import router

    paths = {getattr(route, "path", "") for route in router.routes}
    assert "/v1/builder/personal-data" in paths


def test_a_normal_prompt_over_http_is_allowed_and_does_not_reserve():
    response = _client().post(
        "/v1/builder/personal-data",
        json={"message": "Build a notes app with folders, search, and a share link."},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["action"] == "allow"
    assert body["decision"] is None
    assert body["findings"] == []
    assert body["reserved"] is False
    assert body["reservation"] is None


def test_an_ask_over_http_returns_the_decision_and_does_not_reserve():
    response = _client().post(
        "/v1/builder/personal-data",
        json={"message": "Send the build log to ada@example.com."},
    )
    assert response.status_code == 409
    body = response.json()
    assert body["action"] == "ask"
    assert body["decision"] == {
        "kind": "personal_data",
        "status": "pending",
        "findings": ["email"],
    }
    assert body["reserved"] is False
    assert body["reservation"] is None
    assert "ada@example.com" not in response.text


def test_allowing_an_ask_over_http_still_does_not_reserve():
    response = _client().post(
        "/v1/builder/personal-data",
        json={"message": "Send the build log to ada@example.com.", "allowed": True},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["action"] == "allow"
    assert body["decision"]["status"] == "allowed"
    assert body["decision"]["findings"] == ["email"]
    assert body["reserved"] is False
    assert body["reservation"] is None
    assert "ada@example.com" not in response.text


def test_a_block_over_http_is_a_visible_refusal_and_does_not_reserve():
    response = _client().post(
        "/v1/builder/personal-data",
        json={"message": "The customer's card is 4111 1111 1111 1111.", "allowed": True},
    )
    assert response.status_code == 409
    body = response.json()
    assert body["action"] == "block"
    assert body["error"] == "personal_data_blocked"
    assert body["reserved"] is False
    assert body["reservation"] is None
    assert "4111" not in response.text
