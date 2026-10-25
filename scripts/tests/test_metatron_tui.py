"""Unit tests for the Metatron captain TUI (stdlib, mocked HTTP, no live API)."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError

import pytest

ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "scripts" / "metatron_tui.py"
SECRET = "sk-test-never-print-this-key"


def _load_module():
    spec = importlib.util.spec_from_file_location("metatron_tui", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


tui = _load_module()


class FakeResponse:
    def __init__(self, status: int, payload: Any):
        self.status = status
        if isinstance(payload, (bytes, bytearray)):
            self._body = bytes(payload)
        else:
            self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class FakeOpener:
    def __init__(self, responses: list[tuple[int, Any]] | None = None):
        self.responses = list(responses or [])
        self.calls: list[dict[str, Any]] = []

    def __call__(self, request, timeout=None):
        headers = {k.lower(): v for k, v in request.header_items()}
        body = request.data.decode("utf-8") if request.data else None
        self.calls.append(
            {
                "url": request.full_url,
                "method": request.get_method(),
                "headers": headers,
                "body": json.loads(body) if body else None,
                "timeout": timeout,
            }
        )
        if not self.responses:
            raise URLError("no fake response queued")
        status, payload = self.responses.pop(0)
        if status >= 400:
            raise HTTPError(
                request.full_url,
                status,
                "error",
                {},
                io.BytesIO(
                    payload if isinstance(payload, (bytes, bytearray))
                    else json.dumps(payload).encode("utf-8")
                ),
            )
        return FakeResponse(status, payload)


def _config(**overrides: Any):
    values = {
        "api_url": "https://boss-api.test",
        "api_key": SECRET,
        "project_id": "boss-dogfood",
        "actor": "ceo",
    }
    values.update(overrides)
    return tui.Config(**values)


def _openapi() -> dict[str, Any]:
    return {
        "paths": {
            "/v1/tasks": {
                "get": {
                    "operationId": "unstable_fastapi_id",
                    "summary": "List tasks",
                    "parameters": [{"name": "project_id", "in": "query", "required": True}],
                },
                "post": {
                    "operationId": "unstable_fastapi_id",
                    "summary": "Create task",
                    "requestBody": {
                        "required": True,
                        "content": {"application/json": {"schema": {"$ref": "#/components/schemas/TaskCreate"}}},
                    },
                },
            },
            "/v1/tasks/{task_id}": {
                "get": {
                    "operationId": "get_task_runtime",
                    "summary": "Get task",
                    "parameters": [
                        {"name": "task_id", "in": "path", "required": True},
                        {"name": "project_id", "in": "query", "required": True},
                    ],
                }
            },
            "/health": {"get": {"operationId": "health_runtime", "summary": "Health"}},
        },
        "components": {
            "schemas": {
                "TaskCreate": {
                    "type": "object",
                    "properties": {"project_id": {"type": "string"}, "title": {"type": "string"}},
                    "required": ["project_id", "title"],
                }
            }
        },
    }


def _catalog() -> dict[str, Any]:
    return {
        "domains": [
            {
                "id": "core", "title": "Core work", "route_count": 4,
                "routes": [
                    {"method": "GET", "path": "/v1/tasks"},
                    {"method": "POST", "path": "/v1/tasks"},
                    {"method": "GET", "path": "/v1/tasks/{task_id}"},
                    {"method": "GET", "path": "/v1/undocumented"},
                ],
            },
            {"id": "system", "title": "System", "route_count": 1,
             "routes": [{"method": "GET", "path": "/health"}]},
        ]
    }


def test_parse_natural_language_is_session():
    cmd = tui.parse_line("what changed this morning?")
    assert cmd.kind == "session"
    assert cmd.message == "what changed this morning?"
    assert cmd.auto is False


def test_parse_slash_commands():
    assert tui.parse_line("/watch").kind == "watch"
    assert tui.parse_line("/overwatch").kind == "overwatch"
    assert tui.parse_line("/goals").kind == "goals"
    assert tui.parse_line("/quit").kind == "quit"
    drive = tui.parse_line("/drive")
    assert drive.kind == "drive"
    assert drive.auto is False


def test_parse_direct_task_commands_and_reject_unsafe_input():
    assert tui.parse_line("/tasks").page == 1
    assert tui.parse_line("/tasks 3").page == 3
    assert tui.parse_line("/task task-123").message == "task-123"
    create = tui.parse_line("/task create launch-plan-1 Plan the community launch")
    assert (create.kind, create.dedupe_key, create.message) == (
        "task_create", "launch-plan-1", "Plan the community launch"
    )
    for line in (
        "/tasks 0", "/tasks 1 extra", "/task", "/task ../private",
        "/task task%2Fsecret", "/task create", "/task create bad/key Title",
        "/task create same-key",
    ):
        assert tui.parse_line(line).kind == "error", line


def test_direct_task_flow_uses_selected_project_actor_and_api_controls():
    opener = FakeOpener([
        (200, {"project_id": "project-two", "items": [
            {"id": "task-1", "status": "queued", "title": "Plan launch"}
        ]}),
        (200, {"id": "task-1", "project_id": "project-two", "status": "queued", "title": "Plan launch"}),
        (201, {"id": "task-2", "status": "queued", "title": "Ship launch"}),
        (409, {"detail": "dedupe_key already exists"}),
    ])
    client = tui.MetatronClient(_config(), urlopen=opener)
    client.dispatch(tui.parse_line("/project project-two"))
    listed = client.handle(tui.parse_line("/tasks"))
    detail = client.handle(tui.parse_line("/task task-1"))
    created = client.dispatch(tui.parse_line("/task create launch-2 Ship launch"))
    duplicate = client.handle(tui.parse_line("/task create launch-2 Ship launch"))
    assert "task-1  [queued]  Plan launch" in listed
    assert "/task ID" in listed
    assert "Task task-1: Plan launch" in detail
    assert "project: project-two" in detail
    assert created["id"] == "task-2"
    assert "HTTP 409" in duplicate
    assert opener.calls[0]["url"].endswith("/v1/tasks?project_id=project-two&limit=25&offset=0")
    assert opener.calls[1]["url"].endswith("/v1/tasks/task-1")
    assert opener.calls[2]["method"] == "POST"
    assert opener.calls[2]["url"].endswith("/v1/tasks")
    assert opener.calls[2]["body"] == {
        "project_id": "project-two", "actor": "ceo", "title": "Ship launch",
        "dedupe_key": "launch-2",
    }
    assert all(call["headers"]["x-boss-api-key"] == SECRET for call in opener.calls)


def test_direct_task_pagination_and_server_denial_are_visible():
    opener = FakeOpener([
        (200, {"project_id": "boss-dogfood", "items": [
            {"id": f"task-{i}", "status": "queued", "title": f"Task {i}"}
            for i in range(25)
        ]}),
        (403, {"detail": "task_create denied"}),
    ])
    client = tui.MetatronClient(_config(), urlopen=opener)
    listed = client.handle(tui.parse_line("/tasks 2"))
    denied = client.handle(tui.parse_line("/task create denied-1 Denied task"))
    assert "Next: /tasks 3" in listed
    assert opener.calls[0]["url"].endswith("/v1/tasks?project_id=boss-dogfood&limit=25&offset=25")
    assert "HTTP 403" in denied
    assert "task_create denied" in denied


def test_direct_task_details_do_not_present_another_project_as_selected_work():
    opener = FakeOpener([(200, {
        "id": "task-in-other-project", "project_id": "project-two", "title": "Other work"
    })])
    client = tui.MetatronClient(_config(), urlopen=opener)
    result = client.dispatch(tui.parse_line("/task task-in-other-project"))
    assert result["__command_error__"] is True
    assert "outside the selected project" in result["message"]


def test_direct_task_create_works_in_one_shot_json_mode(monkeypatch, capsys):
    opener = FakeOpener([(201, {
        "id": "task-4", "project_id": "boss-dogfood", "title": "Ship docs",
        "dedupe_key": "docs-4",
    })])
    monkeypatch.setenv("BOSS_API_URL", "https://boss-api.test")
    monkeypatch.setenv("BOSS_API_KEY", SECRET)
    monkeypatch.setenv("BOSS_PROJECT_ID", "boss-dogfood")
    monkeypatch.setenv("BOSS_ACTOR", "ceo")
    monkeypatch.setattr(tui, "urlopen", opener)
    assert tui.main(["--exec", "/task create docs-4 Ship docs", "--json"]) == 0
    output = capsys.readouterr()
    assert json.loads(output.out)["id"] == "task-4"
    assert output.err == ""
    assert opener.calls[0]["body"]["dedupe_key"] == "docs-4"
    assert SECRET not in output.out + output.err


def _decision(decision_id: str = "dec-1", status: str = "pending") -> dict[str, Any]:
    return {
        "id": decision_id, "status": status, "risk_tier": 2,
        "question": "Approve the launch plan?", "reason": "Release gate",
        "recommendation": {"action": "hold"}, "options": [{"id": "approve"}],
        "cost_summary": {"estimate_usd": 10},
    }


def test_parse_direct_decision_commands_requires_explicit_action_note():
    assert (tui.parse_line("/decisions").message, tui.parse_line("/decisions").page) == ("open", 1)
    assert (tui.parse_line("/decisions all 3").message, tui.parse_line("/decisions all 3").page) == ("all", 3)
    assert tui.parse_line("/decision dec-1").kind == "decision"
    action = tui.parse_line("/decision approve dec-1 Reviewed the release gates")
    assert (action.kind, action.method, action.message, action.body) == (
        "decision_action", "approve", "dec-1", {"note": "Reviewed the release gates"}
    )
    for line in (
        "/decisions unknown", "/decisions all 0", "/decisions open 1 extra",
        "/decision", "/decision ../other", "/decision reject dec-1",
        "/decision approve dec%2F2 note", "/decision execute dec-1 note",
    ):
        assert tui.parse_line(line).kind == "error", line


def test_decision_inbox_is_project_scoped_filtered_and_paged():
    decisions = [_decision(f"dec-{index}") for index in range(26)] + [_decision("done", "approved")]
    opener = FakeOpener([
        (200, {"project_id": "project-two", "items": decisions}),
        (200, {"project_id": "project-two", "items": decisions}),
        (200, {"project_id": "project-two", "items": decisions}),
    ])
    client = tui.MetatronClient(_config(), urlopen=opener)
    client.dispatch(tui.parse_line("/project project-two"))
    first = client.handle(tui.parse_line("/decisions"))
    second = client.dispatch(tui.parse_line("/decisions open 2"))
    all_items = client.dispatch(tui.parse_line("/decisions all"))
    assert "26 total, page 1/2" in first
    assert "Next: /decisions open 2" in first
    assert second["items"][0]["id"] == "dec-25"
    assert all_items["total_count"] == 27
    assert opener.calls[0]["url"].endswith("/v1/decisions/inbox?project_id=project-two")
    assert all(call["headers"]["x-boss-api-key"] == SECRET for call in opener.calls)


def test_decision_review_shows_rationale_and_uses_selected_project_filter():
    opener = FakeOpener([(200, _decision())])
    client = tui.MetatronClient(_config(), urlopen=opener)
    rendered = client.handle(tui.parse_line("/decision dec-1"))
    assert "Decision dec-1: Approve the launch plan?" in rendered
    assert "risk tier: 2" in rendered
    assert 'recommendation: {"action": "hold"}' in rendered
    assert "cost summary" in rendered
    assert opener.calls[0]["url"].endswith("/v1/decisions/dec-1?project_id=boss-dogfood")


def test_decision_commands_do_not_present_mismatched_project_or_id():
    opener = FakeOpener([
        (200, {"project_id": "another-project", "items": [_decision()]}),
        (200, _decision("different-decision")),
    ])
    client = tui.MetatronClient(_config(), urlopen=opener)
    inbox = client.dispatch(tui.parse_line("/decisions"))
    action = client.dispatch(tui.parse_line("/decision approve dec-1 Reviewed"))
    assert inbox["__command_error__"] is True
    assert action["__command_error__"] is True
    assert all(call["method"] == "GET" for call in opener.calls)


def test_decision_action_preflight_enforces_selected_project_and_server_denial():
    opener = FakeOpener([
        (404, {"detail": "Decision not found"}),
        (200, _decision()),
        (409, {"detail": "mcp_confirmation_required"}),
    ])
    client = tui.MetatronClient(_config(), urlopen=opener)
    out_of_project = client.handle(tui.parse_line("/decision approve dec-1 Reviewed and approved"))
    denied = client.handle(tui.parse_line("/decision approve dec-1 Reviewed and approved"))
    assert "HTTP 404" in out_of_project
    assert "HTTP 409" in denied and "mcp_confirmation_required" in denied
    assert [call["method"] for call in opener.calls] == ["GET", "GET", "POST"]
    assert opener.calls[2]["url"].endswith("/v1/decisions/dec-1/approve")
    assert opener.calls[2]["body"] == {"actor": "ceo", "note": "Reviewed and approved"}


def test_direct_decision_reject_uses_governed_api_and_returns_record():
    opener = FakeOpener([(200, _decision()), (200, _decision(status="rejected"))])
    client = tui.MetatronClient(_config(), urlopen=opener)
    rejected = client.dispatch(tui.parse_line("/decision reject dec-1 Keep the guardrail in place"))
    assert rejected["status"] == "rejected"
    assert opener.calls[0]["url"].endswith("/v1/decisions/dec-1?project_id=boss-dogfood")
    assert opener.calls[1]["url"].endswith("/v1/decisions/dec-1/reject")
    assert opener.calls[1]["body"] == {"actor": "ceo", "note": "Keep the guardrail in place"}


def test_api_command_parses_method_path_and_json():
    command = tui.parse_line('/api POST /v1/metatron/watch {"project_id":"boss-dogfood","actor":"ceo"}')
    assert command.kind == "api"
    assert command.method == "POST"
    assert command.path == "/v1/metatron/watch"
    assert command.body == {"project_id": "boss-dogfood", "actor": "ceo"}


def test_api_command_rejects_external_paths_and_bad_json_before_http():
    for line in (
        "/api GET https://evil.example/v1/tasks",
        "/api GET //evil.example/v1/tasks",
        "/api GET /v1/../secrets",
        "/api GET /v1/%2e%2e/secrets",
        "/api GET /v1/tasks#fragment",
        "/api POST /v1/tasks {bad-json}",
        '/api GET /v1/tasks {"unexpected":"body"}',
    ):
        assert tui.parse_line(line).kind == "error", line


def test_drive_auto_and_go_set_auto_true():
    assert tui.parse_line("/drive auto").auto is True
    assert tui.parse_line("/drive go").auto is True
    go = tui.parse_line("/go")
    assert go.kind == "drive"
    assert go.auto is True


def test_plain_text_is_never_a_captain_power():
    """BOSSAPP-759: a captain power runs only when typed as a command. "go", "overwatch", "drive
    auto" or "launch it" as plain text are words for the BOSS agent, never a drive or overwatch."""
    for said in ("go", "overwatch", "drive auto", "launch it", "run overwatch now",
                 "set up three projects and tell me what you can't do"):
        cmd = tui.parse_line(said)
        assert cmd.kind == "session", said
        assert cmd.message == said


def test_load_config_reads_env_and_never_echoes_key():
    env = {
        "BOSS_API_URL": "https://boss-api.example",
        "BOSS_API_KEY": SECRET,
        "BOSS_PROJECT_ID": "proj-1",
    }
    cfg = tui.load_config(env)
    assert cfg.api_url == "https://boss-api.example"
    assert cfg.project_id == "proj-1"
    assert cfg.api_key == SECRET
    rendered = tui.format_banner(cfg)
    assert SECRET not in rendered
    assert "boss-api.example" in rendered
    assert "proj-1" in rendered


def test_load_config_requires_api_key_without_leaking_it():
    try:
        tui.load_config({"BOSS_API_URL": "http://127.0.0.1:8000", "BOSS_PROJECT_ID": "p"})
    except tui.ConfigError as exc:
        message = str(exc)
        assert "BOSS_API_KEY" in message
        assert SECRET not in message
        assert "required" in message.lower()
    else:
        raise AssertionError("expected ConfigError when BOSS_API_KEY is missing")


def test_session_posts_natural_language_and_remembers_thread():
    opener = FakeOpener(
        [
            (200, {"reply": "status ok", "thread_id": "athr_1", "intent": "status"}),
            (200, {"reply": "remembered", "thread_id": "athr_1", "intent": "remember"}),
        ]
    )
    client = tui.MetatronClient(_config(), urlopen=opener)
    first = client.handle(tui.parse_line("what is blocked?"))
    assert "status ok" in first
    second = client.handle(tui.parse_line("remember sandbox root is a volume"))
    assert "remembered" in second
    assert opener.calls[0]["method"] == "POST"
    assert opener.calls[0]["url"].endswith("/v1/metatron/session")
    assert opener.calls[0]["body"]["message"] == "what is blocked?"
    assert opener.calls[0]["body"]["project_id"] == "boss-dogfood"
    assert opener.calls[0]["headers"].get("x-boss-api-key") == SECRET
    assert opener.calls[1]["body"]["thread_id"] == "athr_1"


def test_watch_goals_and_drive_hit_their_endpoints():
    opener = FakeOpener(
        [
            (200, {"id": "mwt_1", "findings": [{"title": "steady"}]}),
            (200, {"project_id": "boss-dogfood", "goals": [{"title": "Ship HookWatch"}]}),
            (200, {"status": "proposed", "applied": [], "note": "governed"}),
            (200, {"status": "proposed", "applied": [], "auto": True}),
        ]
    )
    client = tui.MetatronClient(_config(), urlopen=opener)
    watch = client.handle(tui.parse_line("/watch"))
    goals = client.handle(tui.parse_line("/goals"))
    drive = client.handle(tui.parse_line("/drive"))
    auto = client.handle(tui.parse_line("/drive auto"))
    assert "steady" in watch
    assert "HookWatch" in goals
    assert "proposed" in drive.lower() or "governed" in drive.lower()
    assert opener.calls[0]["method"] == "POST"
    assert opener.calls[0]["url"].endswith("/v1/metatron/watch")
    assert opener.calls[1]["method"] == "GET"
    assert "/v1/metatron/goals" in opener.calls[1]["url"]
    assert "project_id=boss-dogfood" in opener.calls[1]["url"]
    assert opener.calls[2]["url"].endswith("/v1/metatron/drive")
    assert opener.calls[2]["body"]["auto"] is False
    assert opener.calls[3]["body"]["auto"] is True
    assert auto  # non-empty formatted payload


def test_api_command_reaches_existing_v1_routes_with_server_auth():
    opener = FakeOpener(
        [
            (200, {"items": [{"id": "task-1"}]}),
            (200, {"id": "watch-1", "status": "done", "receipt_id": "receipt-1"}),
            (200, {"id": "watch-1", "status": "done", "receipt_id": "receipt-1"}),
        ]
    )
    client = tui.MetatronClient(_config(), urlopen=opener)
    read = client.dispatch(tui.parse_line("/api GET /v1/tasks?project_id=boss-dogfood"))
    write = client.dispatch(
        tui.parse_line('/api POST /v1/metatron/watch {"project_id":"boss-dogfood","actor":"ceo"}')
    )
    assert read == {"items": [{"id": "task-1"}]}
    assert write["status"] == "done"
    assert [call["method"] for call in opener.calls] == ["GET", "POST"]
    assert opener.calls[0]["url"].endswith("/v1/tasks?project_id=boss-dogfood")
    assert opener.calls[1]["url"].endswith("/v1/metatron/watch")
    assert opener.calls[1]["body"] == {"project_id": "boss-dogfood", "actor": "ceo"}
    assert all(call["headers"]["x-boss-api-key"] == SECRET for call in opener.calls)

    rendered = client.handle(
        tui.parse_line('/api POST /v1/metatron/watch {"project_id":"boss-dogfood","actor":"ceo"}')
    )
    assert '"receipt_id": "receipt-1"' in rendered


def test_overwatch_hits_overwatch_endpoint():
    opener = FakeOpener(
        [(200, {"status": "verified", "applied": [], "note": "Overwatch validated then held"})]
    )
    client = tui.MetatronClient(_config(), urlopen=opener)
    rendered = client.handle(tui.parse_line("/overwatch"))
    assert opener.calls[0]["url"].endswith("/v1/metatron/overwatch")
    assert opener.calls[0]["body"]["project_id"] == "boss-dogfood"
    assert "validated" in rendered.lower() or "verified" in rendered.lower()


def test_go_posts_drive_with_auto_true():
    opener = FakeOpener([(200, {"status": "proposed", "applied": []})])
    client = tui.MetatronClient(_config(), urlopen=opener)
    client.handle(tui.parse_line("/go"))
    assert opener.calls[0]["url"].endswith("/v1/metatron/drive")
    assert opener.calls[0]["body"]["auto"] is True


def test_http_errors_are_visible_and_redact_the_key():
    opener = FakeOpener(
        [(401, {"detail": f"unauthorized key={SECRET}"})]
    )
    client = tui.MetatronClient(_config(), urlopen=opener)
    rendered = client.handle(tui.parse_line("/watch"))
    assert "401" in rendered
    assert SECRET not in rendered
    assert "***" in rendered or "redacted" in rendered.lower()


def test_credential_validation_echo_is_redacted_in_http_error():
    credential = "FAKE-CREDENTIAL-VALUE-123"
    provider_key = "FAKE-PROVIDER-KEY-456"
    nested_secret = "FAKE-NESTED-SECRET-789"
    underscore_key = "FAKE-UNDERSCORE-API-KEY-321"
    opener = FakeOpener([(422, {
        "detail": [{"loc": ["body", "project_id"], "input": {
            "value": credential,
            "config": {"provider": {
                "apiKey": provider_key,
                "secret": nested_secret,
                "nested": {"api_key": underscore_key},
            }},
            "note": "missing project_id",
        }}]
    })])
    client = tui.MetatronClient(_config(), urlopen=opener)
    line = (
        '/api POST /v1/entitlements/sources/openai_api/credential '
        f'{{"value":"{credential}","config":{{"provider":'
        f'{{"apiKey":"{provider_key}","secret":"{nested_secret}",'
        f'"nested":{{"api_key":"{underscore_key}"}}}}}}}}'
    )
    rendered = client.handle(tui.parse_line(line))
    assert "HTTP 422" in rendered
    assert "missing project_id" in rendered
    assert credential not in rendered
    assert provider_key not in rendered
    assert nested_secret not in rendered
    assert underscore_key not in rendered
    assert SECRET not in rendered


def test_kms_key_value_validation_echo_is_redacted():
    credential = "FAKE-KMS-MATERIAL-123"
    opener = FakeOpener([(422, {
        "detail": [{"loc": ["body", "project_id"], "input": {"value": credential}}]
    })])
    client = tui.MetatronClient(_config(), urlopen=opener)
    rendered = client.handle(tui.parse_line(
        f'/api POST /v1/kms/keys {{"value":"{credential}"}}'
    ))
    assert "HTTP 422" in rendered
    assert credential not in rendered
    assert "***" in rendered


def test_success_response_and_plain_text_error_redact_nested_submitted_secrets():
    token = "FAKE-AUTH-TOKEN-123"
    password = "FAKE-PASSWORD-456"
    opener = FakeOpener([
        (200, {"echo": {"auth": {"accessToken": token}, "password": password}, "status": "accepted"}),
        (400, f"rejected token={token}, password={password}".encode()),
    ])
    client = tui.MetatronClient(_config(), urlopen=opener)
    line = (
        '/api POST /v1/tasks '
        f'{{"auth":{{"accessToken":"{token}"}},"password":"{password}"}}'
    )
    successful = client.handle(tui.parse_line(line))
    failed = client.handle(tui.parse_line(line))
    assert '"status": "accepted"' in successful
    assert "HTTP 400" in failed
    assert all(secret not in output for secret in (token, password) for output in (successful, failed))


def test_transport_failure_does_not_echo_request_or_exception_body():
    credential = "FAKE-CREDENTIAL-IN-URLERROR"
    def failed_transport(_request, timeout=None):
        raise URLError(f"request body included {credential}")
    client = tui.MetatronClient(_config(), urlopen=failed_transport)
    rendered = client.handle(tui.parse_line(
        f'/api POST /v1/entitlements/sources/openai_api/credential {{"value":"{credential}"}}'
    ))
    assert "unreachable" in rendered
    assert credential not in rendered

    def failed_unexpectedly(_request, timeout=None):
        raise RuntimeError(f"request body included {credential}")
    client = tui.MetatronClient(_config(), urlopen=failed_unexpectedly)
    rendered = client.handle(tui.parse_line(
        f'/api POST /v1/entitlements/sources/openai_api/credential {{"value":"{credential}"}}'
    ))
    assert "HTTP 0" in rendered
    assert "RuntimeError" in rendered
    assert credential not in rendered

    class UnreadableErrorBody:
        def read(self, *_args):
            raise RuntimeError(f"request body included {credential}")

        def close(self):
            return None

    def failed_error_read(request, timeout=None):
        raise HTTPError(request.full_url, 502, "error", {}, UnreadableErrorBody())
    client = tui.MetatronClient(_config(), urlopen=failed_error_read)
    rendered = client.handle(tui.parse_line(
        f'/api POST /v1/entitlements/sources/openai_api/credential {{"value":"{credential}"}}'
    ))
    assert "HTTP 502" in rendered
    assert "response body unavailable" in rendered
    assert credential not in rendered


def test_secret_query_value_is_redacted_from_error_echo():
    query_key = "FAKE-QUERY-KEY-123"
    client = tui.MetatronClient(
        _config(), urlopen=FakeOpener([(400, {"detail": f"bad api_key={query_key}"})])
    )
    rendered = client.handle(tui.parse_line(f"/api GET /v1/tasks?api_key={query_key}"))
    assert "HTTP 400" in rendered
    assert query_key not in rendered


def test_operation_id_invocation_redacts_credential_validation_input():
    credential = "FAKE-MODEL-KEY-FOR-OP-123"
    document = {"paths": {"/v1/entitlements/sources/{source_id}/credential": {
        "post": {
            "operationId": "upload_runtime_id",
            "parameters": [{"name": "source_id", "in": "path", "required": True}],
            "requestBody": {"required": True, "content": {"application/json": {"schema": {"type": "object"}}}},
        }
    }}}
    alias = tui.operation_records(document)[0]["operation_id"]
    opener = FakeOpener([
        (200, document),
        (422, {"detail": [{"loc": ["body", "project_id"], "input": {"value": credential}}]}),
    ])
    client = tui.MetatronClient(_config(), urlopen=opener)
    rendered = client.handle(tui.parse_line(
        f'/op {alias} {{"path":{{"source_id":"openai_api"}},"body":{{"value":"{credential}"}}}}'
    ))
    assert "HTTP 422" in rendered
    assert credential not in rendered


def test_one_shot_json_redacts_submitted_credential(monkeypatch, capsys):
    credential = "FAKE-CREDENTIAL-FOR-JSON-123"
    opener = FakeOpener([(422, {"detail": [{"input": {"value": credential}}]})])
    monkeypatch.setenv("BOSS_API_URL", "https://boss-api.test")
    monkeypatch.setenv("BOSS_API_KEY", SECRET)
    monkeypatch.setenv("BOSS_PROJECT_ID", "boss-dogfood")
    monkeypatch.setattr(tui, "urlopen", opener)
    command = f'/api POST /v1/entitlements/sources/openai_api/credential {{"value":"{credential}"}}'
    assert tui.main(["--exec", command, "--json"]) == 1
    output = capsys.readouterr()
    assert json.loads(output.out)["status"] == 422
    assert credential not in output.out + output.err


def test_one_shot_json_has_machine_readable_result_and_failure_exit(monkeypatch, capsys):
    opener = FakeOpener(
        [(200, {"items": [{"id": "task-1"}]}), (403, {"detail": f"denied key={SECRET}"})]
    )
    monkeypatch.setenv("BOSS_API_URL", "https://boss-api.test")
    monkeypatch.setenv("BOSS_API_KEY", SECRET)
    monkeypatch.setenv("BOSS_PROJECT_ID", "boss-dogfood")
    monkeypatch.setattr(tui, "urlopen", opener)

    assert tui.main(["--exec", "/api GET /v1/tasks?project_id=boss-dogfood", "--json"]) == 0
    success = capsys.readouterr()
    assert json.loads(success.out) == {"items": [{"id": "task-1"}]}
    assert success.err == ""

    assert tui.main(["--exec", "/api GET /v1/decisions?project_id=boss-dogfood", "--json"]) == 1
    failure = capsys.readouterr()
    payload = json.loads(failure.out)
    assert payload["status"] == 403
    assert payload["__http_error__"] is True
    assert SECRET not in failure.out + failure.err
    assert "***" in payload["body"]

    assert tui.main(["--exec", "/api GET https://evil.example/v1/tasks", "--json"]) == 2
    invalid = capsys.readouterr()
    assert json.loads(invalid.out)["__command_error__"] is True
    assert len(opener.calls) == 2


def test_repl_session_slash_quit_never_prints_key():
    opener = FakeOpener(
        [(200, {"reply": "captain ready", "thread_id": "athr_9", "intent": "status"})]
    )
    client = tui.MetatronClient(_config(), urlopen=opener)
    stdin = io.StringIO("hello captain\n/quit\n")
    stdout = io.StringIO()
    code = tui.run_repl(client, stdin=stdin, stdout=stdout)
    out = stdout.getvalue()
    assert code == 0
    assert "captain ready" in out
    assert SECRET not in out
    assert opener.calls[0]["url"].endswith("/v1/metatron/session")
    assert opener.calls[0]["body"]["message"] == "hello captain"


def test_makefile_and_engine_entry_exist():
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert "\nmetatron:" in makefile
    assert "scripts/metatron_tui.py" in makefile
    engine_entry = ROOT / "engine" / "boss_engine" / "cli" / "metatron.py"
    assert engine_entry.is_file()
    assert "metatron_tui" in engine_entry.read_text(encoding="utf-8")


def test_main_uses_env_and_does_not_need_live_api(monkeypatch):
    opener = FakeOpener([])
    monkeypatch.setenv("BOSS_API_URL", "https://boss-api.test")
    monkeypatch.setenv("BOSS_API_KEY", SECRET)
    monkeypatch.setenv("BOSS_PROJECT_ID", "boss-dogfood")
    monkeypatch.setattr(tui, "urlopen", opener)
    stdin = io.StringIO("/quit\n")
    stdout = io.StringIO()
    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.setattr(sys, "stdout", stdout)
    assert tui.main([]) == 0
    assert SECRET not in stdout.getvalue()
    assert "metatron" in stdout.getvalue().lower()


def test_catalog_search_and_describe_use_live_capabilities():
    opener = FakeOpener([(200, _catalog()), (200, _openapi())])
    client = tui.MetatronClient(_config(), urlopen=opener)
    domains = client.dispatch(tui.parse_line("/ops"))
    assert domains["domains"][0]["id"] == "core"
    found = client.dispatch(tui.parse_line("/ops tasks"))
    assert found["count"] == 3
    assert all(item["terminal_invokable"] for item in found["operations"])
    get_id = next(item["operation_id"] for item in found["operations"] if item["method"] == "GET" and item["path"] == "/v1/tasks")
    detail = client.dispatch(tui.parse_line(f"/describe {get_id}"))
    assert detail["method"] == "GET"
    assert detail["parameters"][0]["name"] == "project_id"
    assert detail["access_note"].startswith("The API applies edition")
    create_id = next(item["operation_id"] for item in found["operations"] if item["method"] == "POST")
    create_detail = client.dispatch(tui.parse_line(f"/describe {create_id}"))
    assert {field["name"] for field in create_detail["body_fields"] if field["required"]} == {"project_id", "title"}
    assert '"project_id":"boss-dogfood"' in create_detail["example"]
    assert [call["url"].split(".test")[-1] for call in opener.calls] == [
        "/v1/meta/api-catalog", "/openapi.json"
    ]


def test_project_list_shows_available_ids_before_switching():
    opener = FakeOpener([(200, {"items": [
        {"id": "boss-dogfood", "name": "BOSS", "org_name": "reference host", "workspace_name": "Ops"},
        {"id": "partner-one", "name": "Partner One", "org_name": "reference host", "workspace_name": "Ops"},
    ]})])
    client = tui.MetatronClient(_config(), urlopen=opener)
    command = tui.parse_line("/projects")
    assert command.kind == "projects"
    result = client.dispatch(command)
    assert result["active_project_id"] == "boss-dogfood"
    assert [item["id"] for item in result["items"]] == ["boss-dogfood", "partner-one"]
    rendered = client.render(command, result)
    assert "* boss-dogfood: BOSS" in rendered
    assert "/project ID" in rendered
    assert SECRET not in rendered
    assert opener.calls[0]["url"].endswith("/v1/projects")


def test_operation_search_pages_every_live_route_without_truncating_json():
    paths = {f"/v1/records/{index}": {"get": {"summary": f"Read record {index}"}} for index in range(60)}
    routes = [{"method": "GET", "path": path} for path in paths]
    catalog = {"domains": [{"id": "core", "title": "Core", "route_count": 60, "routes": routes}]}
    opener = FakeOpener([(200, catalog), (200, {"paths": paths})])
    client = tui.MetatronClient(_config(), urlopen=opener)
    first = client.dispatch(tui.parse_line("/ops *"))
    second = client.dispatch(tui.parse_line("/ops core --page 2"))
    third = client.dispatch(tui.parse_line("/ops * --page=3"))
    assert [page["shown_count"] for page in (first, second, third)] == [25, 25, 10]
    assert all(page["count"] == 60 and page["pages"] == 3 for page in (first, second, third))
    assert len({item["operation_id"] for page in (first, second, third) for item in page["operations"]}) == 60
    assert "Next: /ops * --page 2" in client.render(tui.parse_line("/ops *"), first)
    assert client.dispatch(tui.parse_line("/ops core --page 4"))["__command_error__"] is True
    assert client.dispatch(tui.parse_line("/ops core --page 0"))["__command_error__"] is True
    assert len(opener.calls) == 2  # Search pages reuse the live catalog and schema.


def test_stable_operation_aliases_disambiguate_duplicate_runtime_ids():
    records = tui.operation_records(_openapi())
    tasks = [record for record in records if record["path"] == "/v1/tasks"]
    assert len(tasks) == 2
    assert tasks[0]["runtime_operation_id"] == tasks[1]["runtime_operation_id"]
    assert tasks[0]["operation_id"] != tasks[1]["operation_id"]
    assert all(record["addressable"] for record in tasks)


def test_operation_invocation_uses_api_key_and_project_query_without_bypass():
    opener = FakeOpener([
        (200, _openapi()),
        (200, {"items": [{"id": "task-1"}]}),
        (200, {"id": "task-2", "event_id": "evt-2"}),
    ])
    client = tui.MetatronClient(_config(), urlopen=opener)
    records = tui.operation_records(_openapi())
    list_id = next(r["operation_id"] for r in records if r["method"] == "GET" and r["path"] == "/v1/tasks")
    create_id = next(r["operation_id"] for r in records if r["method"] == "POST" and r["path"] == "/v1/tasks")
    assert client.dispatch(tui.parse_line(f"/op {list_id}"))["items"][0]["id"] == "task-1"
    created = client.dispatch(tui.parse_line(f'/op {create_id} {{"body":{{"project_id":"boss-dogfood","title":"Plan"}}}}'))
    assert created["event_id"] == "evt-2"
    assert opener.calls[1]["url"].endswith("/v1/tasks?project_id=boss-dogfood")
    assert opener.calls[2]["method"] == "POST"
    assert opener.calls[2]["body"] == {"project_id": "boss-dogfood", "title": "Plan"}
    assert all(call["headers"]["x-boss-api-key"] == SECRET for call in opener.calls)


def test_operation_autofills_selected_project_only_for_declared_json_field():
    opener = FakeOpener([
        (200, _openapi()),
        (200, {"id": "task-2"}),
        (200, {"id": "task-3"}),
    ])
    client = tui.MetatronClient(_config(), urlopen=opener)
    create_id = next(
        record["operation_id"] for record in tui.operation_records(_openapi())
        if record["method"] == "POST" and record["path"] == "/v1/tasks"
    )
    client.dispatch(tui.parse_line("/project selected-project"))
    created = client.dispatch(tui.parse_line(f'/op {create_id} {{"body":{{"title":"Plan"}}}}'))
    explicit = client.dispatch(tui.parse_line(
        f'/op {create_id} {{"body":{{"project_id":"another-project","title":"Plan"}}}}'
    ))
    assert created["id"] == "task-2"
    assert explicit["id"] == "task-3"
    assert opener.calls[1]["body"] == {"project_id": "selected-project", "title": "Plan"}
    assert opener.calls[2]["body"] == {"project_id": "another-project", "title": "Plan"}
    assert "selected project_id" in client.handle(tui.parse_line(f"/describe {create_id}"))


def test_operation_does_not_invent_project_for_ambiguous_body_union():
    document = {"paths": {"/v1/sample": {"post": {
        "requestBody": {"required": True, "content": {"application/json": {"schema": {
            "oneOf": [
                {"type": "object", "properties": {"project_id": {"type": "string"}}},
                {"type": "object", "properties": {"name": {"type": "string"}}},
            ]
        }}}},
    }}}}
    alias = tui.operation_records(document)[0]["operation_id"]
    opener = FakeOpener([(200, document), (200, {"status": "accepted"})])
    client = tui.MetatronClient(_config(), urlopen=opener)
    result = client.dispatch(tui.parse_line(f'/op {alias} {{"body":{{"name":"sample"}}}}'))
    assert result["status"] == "accepted"
    assert opener.calls[1]["body"] == {"name": "sample"}


def test_description_resolves_nullable_body_refs_and_nested_field_hints():
    document = {"paths": {"/v1/sample": {"post": {
        "requestBody": {"required": True, "content": {"application/json": {"schema": {
            "anyOf": [{"$ref": "#/components/schemas/Sample"}, {"type": "null"}],
        }}}},
    }}}, "components": {"schemas": {
        "Sample": {"type": "object", "required": ["project_id", "settings"], "properties": {
            "project_id": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "mode": {"type": "string", "enum": ["plan", "apply"]},
            "settings": {"$ref": "#/components/schemas/Settings"},
        }},
        "Settings": {"type": "object", "required": ["reason"], "properties": {
            "reason": {"type": "string"},
        }},
    }}}
    alias = tui.operation_records(document)[0]["operation_id"]
    opener = FakeOpener([(200, document)])
    client = tui.MetatronClient(_config(), urlopen=opener)
    detail = client.dispatch(tui.parse_line(f"/describe {alias}"))
    assert detail["auto_filled_body_fields"] == ["project_id"]
    assert {field["name"] for field in detail["body_fields"] if field["required"]} == {
        "project_id", "settings",
    }
    rendered = client.render(tui.parse_line(f"/describe {alias}"), detail)
    assert "project_id: string, required" in rendered
    assert "choices=['plan', 'apply']" in rendered
    assert "required keys=['reason']" in rendered


def test_operation_rejects_bad_arguments_before_an_effect():
    opener = FakeOpener([(200, _openapi())])
    client = tui.MetatronClient(_config(), urlopen=opener)
    records = tui.operation_records(_openapi())
    task_id = next(r["operation_id"] for r in records if r["path"] == "/v1/tasks/{task_id}")
    create_id = next(r["operation_id"] for r in records if r["method"] == "POST" and r["path"] == "/v1/tasks")
    for line in (
        f"/op {task_id}",
        f'/op {task_id} {{"path":{{"task_id":"../secret"}}}}',
        f'/op {task_id} {{"path":{{"task_id":"t1"}},"query":{{"unknown":"x"}}}}',
        f"/op {create_id}",
    ):
        result = client.dispatch(tui.parse_line(line))
        assert result["__command_error__"] is True, line
    assert len(opener.calls) == 1


def test_project_switch_changes_context_and_discards_previous_thread():
    opener = FakeOpener([(200, {"project_id": "project-two", "goals": []})])
    client = tui.MetatronClient(_config(), urlopen=opener)
    client.thread_id = "thread-in-old-project"
    changed = client.dispatch(tui.parse_line("/project project-two"))
    assert changed["active_project_id"] == "project-two"
    assert client.thread_id is None
    client.dispatch(tui.parse_line("/goals"))
    assert opener.calls[0]["url"].endswith("/v1/metatron/goals?project_id=project-two")
    assert client.dispatch(tui.parse_line("/project ../other"))["__command_error__"] is True


def test_coverage_separates_discovery_terminal_mapping_and_api_access():
    opener = FakeOpener([(200, _catalog()), (200, _openapi())])
    client = tui.MetatronClient(_config(), urlopen=opener)
    report = client.dispatch(tui.parse_line("/coverage"))
    assert report["discovered_operations"] == 5
    assert report["operation_id_invokable"] == 3
    assert report["raw_api_invokable"] == 4
    missing = next(r for r in report["operations"] if r["path"] == "/v1/undocumented")
    assert missing["source"] == "live_catalog_only"
    assert missing["terminal_invokable"] is False
    assert missing["raw_api_invokable"] is True
    assert all(r["edition_policy_access"] == "unverified_server_enforced" for r in report["operations"])
    assert "Access is checked" in client.handle(tui.parse_line("/coverage"))
    assert len(opener.calls) == 2  # Cached catalog and schema are reused.


def test_coverage_matches_starlette_catchall_to_openapi_template():
    document = {"paths": {"/v1/previews/{preview_id}/web/{path}": {
        "get": {"operationId": "runtime_id", "parameters": [
            {"name": "preview_id", "in": "path", "required": True},
            {"name": "path", "in": "path", "required": True},
        ]}
    }}}
    catalog = {"domains": [{"id": "delivery", "routes": [
        {"method": "GET", "path": "/v1/previews/{preview_id}/web/{path:path}"}
    ]}]}
    client = tui.MetatronClient(_config(), urlopen=FakeOpener([(200, catalog), (200, document)]))
    report = client.coverage()
    assert report["discovered_operations"] == 1
    assert report["operations"][0]["domain"] == "delivery"
    assert report["operations"][0]["source"] == "live_openapi"


def test_operation_catchall_path_allows_nested_files_only_for_declared_route():
    document = {"paths": {"/v1/previews/{preview_id}/web/{path}": {
        "get": {"operationId": "runtime_id", "parameters": [
            {"name": "preview_id", "in": "path", "required": True},
            {"name": "path", "in": "path", "required": True},
        ]}
    }}}
    catalog = {"domains": [{"id": "delivery", "routes": [
        {"method": "GET", "path": "/v1/previews/{preview_id}/web/{path:path}"}
    ]}]}
    alias = tui.operation_records(document)[0]["operation_id"]
    opener = FakeOpener([(200, document), (200, catalog), (200, {"status": "ok"})])
    client = tui.MetatronClient(_config(), urlopen=opener)
    result = client.dispatch(tui.parse_line(
        f'/op {alias} {{"path":{{"preview_id":"p-1","path":"docs/index.html"}}}}'
    ))
    assert result["status"] == "ok"
    assert opener.calls[-1]["url"].endswith("/v1/previews/p-1/web/docs/index.html")
    rejected = client.dispatch(tui.parse_line(
        f'/op {alias} {{"path":{{"preview_id":"p-1","path":"docs/../secret"}}}}'
    ))
    assert rejected["__command_error__"] is True
    assert len(opener.calls) == 3


def test_community_snapshot_maps_every_operator_operation():
    yaml = pytest.importorskip("yaml")
    # The public export installs the Community contract as api/openapi.yaml.
    community_path = ROOT / "api" / "openapi.community.yaml"
    contract = community_path if community_path.exists() else ROOT / "api" / "openapi.yaml"
    document = yaml.safe_load(contract.read_text(encoding="utf-8"))
    records = tui.operation_records(document)
    assert len(records) == 983
    operator_records = [record for record in records if record["path"].startswith("/v1/")]
    assert len(operator_records) == 970
    assert all(record["addressable"] for record in operator_records)
    assert len({record["operation_id"] for record in records}) == len(records)
    assert all(record["operation_id"] == record["runtime_operation_id"] for record in records)
    features = (ROOT / "docs/community/FEATURES.md").read_text(encoding="utf-8")
    terminal = (ROOT / "docs/community/TERMINAL_CAPABILITY_MATRIX.md").read_text(encoding="utf-8")
    assert "983" in features and "986" not in features
    assert "983" in terminal and "970" in terminal and "986" not in terminal


def test_private_snapshot_maps_every_operator_operation_except_inbound_callbacks():
    if not (ROOT / "api" / "openapi.community.yaml").exists():
        pytest.skip("private contract is absent from the Community export")
    yaml = pytest.importorskip("yaml")
    document = yaml.safe_load((ROOT / "api" / "openapi.yaml").read_text(encoding="utf-8"))
    records = tui.operation_records(document)
    assert len(records) == 1035
    operator_records = [record for record in records if record["path"].startswith("/v1/")]
    assert len(operator_records) == 1022
    unaddressable = {(record["method"], record["path"]) for record in operator_records if not record["addressable"]}
    assert unaddressable == {
        ("POST", "/v1/webhooks/github"),
        ("POST", "/v1/webhooks/inbound/{source}"),
    }
    assert len({record["operation_id"] for record in records}) == len(records)
    assert all(record["runtime_operation_id"] for record in records)
    assert len({record["runtime_operation_id"] for record in records}) == len(records)


def test_live_community_app_schema_and_catalog_map_every_mounted_operator_route():
    if importlib.util.find_spec("fastapi") is None:
        pytest.skip("API dependencies are not installed")
    code = """
import json
from apps.api.app.main import app, get_meta_api_catalog
from scripts.metatron_tui import operation_records, _route_key
records = operation_records(app.openapi())
catalog = get_meta_api_catalog()
documented = {_route_key(r['method'], r['path']) for r in records}
mounted = {_route_key(r['method'], r['path']) for d in catalog['domains'] for r in d['routes']}
operator = {key for key in mounted if key[1].startswith('/v1/')}
assert all(r['addressable'] for r in records if r['path'].startswith('/v1/'))
assert operator <= documented, sorted(operator - documented)[:10]
assert len({r['operation_id'] for r in records}) == len(records)
print(json.dumps({'openapi': len(records), 'operator': len(operator)}))
"""
    env = os.environ.copy()
    env["BOSS_EDITION"] = "community"
    env.pop("DATABASE_URL", None)
    env["PYTHONPATH"] = os.pathsep.join((str(ROOT), str(ROOT / "engine")))
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, env=env,
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stderr
    counts = json.loads(result.stdout)
    assert counts == {"openapi": 983, "operator": 970}


def test_schedule_edit_uses_typed_op_input_and_returns_the_api_result():
    """The terminal addresses the existing schedule operation; it does not invent a second one."""
    document = {"paths": {"/v1/delivery-workflows/schedules": {"post": {
        "summary": "Create Workflow Schedule",
        "requestBody": {"required": True, "content": {"application/json": {"schema": {
            "type": "object",
            "required": ["project_id", "workflow_key", "name", "cadence"],
            "properties": {
                "project_id": {"type": "string"},
                "workflow_key": {"type": "string"},
                "name": {"type": "string"},
                "cadence": {"type": "string"},
                "timezone": {"type": "string"},
                "schedule_id": {"type": "string"},
                "input": {"type": "object", "properties": {
                    "sectors": {"type": "string"},
                    "to": {"type": "string"},
                }, "required": ["sectors", "to"]},
            },
        }}}},
    }}}}
    alias = tui.operation_records(document)[0]["operation_id"]
    opener = FakeOpener([
        (200, document),
        (200, {"id": "wsc_1", "input": {"sectors": "energy", "to": "reader@example.com"}}),
    ])
    client = tui.MetatronClient(_config(), urlopen=opener)
    described = client.handle(tui.parse_line(f"/describe {alias}"))
    assert "input" in described
    assert "schedule_id" in described
    assert "sectors" in described
    result = client.dispatch(tui.parse_line(
        f'/op {alias} {{"body":{{"workflow_key":"weekly_newsletter",'
        f'"name":"Weekly newsletter — Monday","cadence":"weekly",'
        f'"timezone":"Europe/London","schedule_id":"wsc_1",'
        f'"input":{{"sectors":"energy","to":"reader@example.com"}}}}}}'
    ))
    assert result["id"] == "wsc_1"
    assert opener.calls[1]["method"] == "POST"
    assert opener.calls[1]["url"].endswith("/v1/delivery-workflows/schedules")
    assert opener.calls[1]["headers"]["x-boss-api-key"] == SECRET
    assert opener.calls[1]["body"] == {
        "project_id": "boss-dogfood",
        "workflow_key": "weekly_newsletter",
        "name": "Weekly newsletter — Monday",
        "cadence": "weekly",
        "timezone": "Europe/London",
        "schedule_id": "wsc_1",
        "input": {"sectors": "energy", "to": "reader@example.com"},
    }


AGENT_TURN = {
    "thread_id": "athr_7",
    "reply": "I can't create projects from here: there is no route for it. Create them in the "
             "console under Projects. I can set up the workflow once they exist.",
    "actions": [],
    "pending_confirmation": None,
    "proposed_actions": [
        {"id": "pa_1", "tool": "start_workflow", "args": {"template": "build_app"},
         "label": "Start the build", "why": "you asked for it"},
    ],
    "next_steps": [{"id": "ns_1", "label": "Investigate further", "depth": "investigate",
                    "message": "Investigate further"}],
    "tool_calls": [],
    "messages": [],
    "cited_acts": [],
}


def test_plain_text_renders_the_agent_reply_with_numbered_choices():
    opener = FakeOpener([(200, AGENT_TURN)])
    client = tui.MetatronClient(_config(), urlopen=opener)
    out = client.handle(tui.parse_line("set up three projects"))
    assert "can't create projects" in out
    assert "1. Start the build [start_workflow]" in out
    assert "2. Investigate further" in out
    assert opener.calls[0]["url"].endswith("/v1/metatron/session")
    assert opener.calls[0]["body"]["surface"] == "chat"


def test_picking_an_offer_goes_through_the_consoles_button_path():
    """A numbered choice is the console's button: /v1/engine/agent/action with exactly the tool
    and args the person was shown — no model in between, nothing re-decided."""
    opener = FakeOpener([(200, AGENT_TURN),
                         (200, {"ok": True, "tool": "start_workflow", "result": {"ok": True}})])
    client = tui.MetatronClient(_config(), urlopen=opener)
    client.handle(tui.parse_line("set up three projects"))
    out = client.handle(tui.parse_line("1"))
    call = opener.calls[1]
    assert call["url"].endswith("/v1/engine/agent/action")
    assert call["body"] == {"project_id": "boss-dogfood", "actor": "ceo", "tool": "start_workflow",
                            "args": {"template": "build_app"}, "thread_id": "athr_7",
                            "action_id": "pa_1", "label": "Start the build"}
    assert "start_workflow: done" in out
    # Spent: the same offer cannot be pressed twice by retyping its number.
    assert [c["kind"] for c in client.choices()] == ["next_step"]


def test_a_fresh_process_picks_the_offer_the_last_command_was_shown():
    """`--print '/pick 1'` is a new process with nothing in memory: the choices come from the
    server's record of the conversation (its standing offers), exactly as the console reloads them."""
    offer = AGENT_TURN["proposed_actions"][0]
    opener = FakeOpener([(200, {"thread_id": "athr_7"}),
                         (200, {"thread_id": "athr_7", "messages": [], "proposed_actions": [offer],
                                "pending_confirmation": None}),
                         (200, {"ok": True, "tool": offer["tool"], "result": {"ok": True}})])
    client = tui.MetatronClient(_config(), urlopen=opener)
    client.handle(tui.parse_line("/pick 1"))
    assert opener.calls[0]["url"].endswith("/v1/agent/threads/ensure")
    assert "/v1/agent/threads/athr_7/messages" in opener.calls[1]["url"]
    call = opener.calls[2]
    assert call["url"].endswith("/v1/engine/agent/action")
    assert call["body"]["tool"] == offer["tool"] and call["body"]["action_id"] == offer["id"]
    assert client.choices() == []  # spent, as in the console


def test_a_fresh_process_picks_the_next_step_the_last_reply_offered():
    """The other standing offer. A turn that stopped short ends with ONE resume step ("Keep
    waiting", "Continue") instead of a button -- stored on the message as `next_steps`, exactly
    like `proposed_actions`. Observed live: a `--print '/pick 1'` process said "nothing to pick"
    right after the console had shown "Choices ... 1. Keep waiting" for the same thread, because
    the restore path only ever rebuilt `proposed_actions` and dropped `next_steps` on the floor."""
    next_step = {"id": "keep_waiting", "label": "Keep waiting", "depth": "investigate",
                "message": "Keep waiting on the northpeak-bank-b index job."}
    opener = FakeOpener([(200, {"thread_id": "athr_9"}),
                         (200, {"thread_id": "athr_9", "messages": [], "proposed_actions": [],
                                "next_steps": [next_step], "pending_confirmation": None}),
                         (200, {**AGENT_TURN, "thread_id": "athr_9", "proposed_actions": [],
                                "next_steps": [], "reply": "Still indexing."})])
    client = tui.MetatronClient(_config(), urlopen=opener)
    out = client.handle(tui.parse_line("/pick 1"))
    assert opener.calls[0]["url"].endswith("/v1/agent/threads/ensure")
    assert "/v1/agent/threads/athr_9/messages" in opener.calls[1]["url"]
    call = opener.calls[2]
    assert call["url"].endswith("/v1/metatron/session")
    assert call["body"]["message"] == next_step["message"]
    assert call["body"]["depth"] == "investigate"
    assert "Still indexing" in out


def test_picking_a_next_step_sends_the_consoles_next_step_turn():
    opener = FakeOpener([(200, AGENT_TURN), (200, {**AGENT_TURN, "proposed_actions": [],
                                                  "next_steps": [], "reply": "Looked deeper."})])
    client = tui.MetatronClient(_config(), urlopen=opener)
    client.handle(tui.parse_line("why is the build stuck?"))
    out = client.handle(tui.parse_line("/pick 2"))
    body = opener.calls[1]["body"]
    assert opener.calls[1]["url"].endswith("/v1/metatron/session")
    assert body["message"] == "Investigate further" and body["depth"] == "investigate"
    assert "Looked deeper." in out


def test_confirm_dispatches_the_pending_plan_and_pick_without_choices_is_an_error():
    pending = {**AGENT_TURN, "proposed_actions": [], "next_steps": [],
               "pending_confirmation": {"plan_id": "plan_9", "summary": "Build HelpDesk"}}
    opener = FakeOpener([(200, pending), (200, {**pending, "pending_confirmation": None,
                                               "reply": "Dispatched."})])
    client = tui.MetatronClient(_config(), urlopen=opener)
    out = client.handle(tui.parse_line("build helpdesk"))
    assert "Waiting for your confirmation: Build HelpDesk" in out
    assert "/confirm" in out
    assert client.handle(tui.parse_line("/pick 1")).startswith("error:")
    client.handle(tui.parse_line("/confirm"))
    assert opener.calls[1]["body"]["confirm_plan_id"] == "plan_9"
    assert len(opener.calls) == 2


def test_a_step_up_refusal_says_what_to_do():
    opener = FakeOpener([(200, AGENT_TURN), (401, {"detail": "step_up_required"})])
    client = tui.MetatronClient(_config(), urlopen=opener)
    client.handle(tui.parse_line("deploy it"))
    out = client.handle(tui.parse_line("1"))
    assert "step-up" in out and "HTTP 401" in out
    assert SECRET not in out


def test_one_shot_json_returns_the_consoles_reply_structure(monkeypatch, capsys):
    opener = FakeOpener([(200, AGENT_TURN)])
    monkeypatch.setattr(tui, "urlopen", opener)
    monkeypatch.setenv("BOSS_API_URL", "https://boss-api.test")
    monkeypatch.setenv("BOSS_API_KEY", SECRET)
    monkeypatch.setenv("BOSS_PROJECT_ID", "boss-dogfood")
    assert tui.main(["--exec", "set up three projects", "--json"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed == AGENT_TURN


# ── A slow turn is not a failed turn (2026-10-23: "unreachable" for a turn that succeeded) ────

def _fake_boss(**kwargs):
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from metatron_fake_api import FakeBoss

    return FakeBoss(**kwargs)


def _env(monkeypatch, url: str) -> None:
    monkeypatch.setenv("BOSS_API_URL", url)
    monkeypatch.setenv("BOSS_API_KEY", SECRET)
    monkeypatch.setenv("BOSS_PROJECT_ID", "boss-dogfood")


def _real_http(monkeypatch) -> None:
    monkeypatch.setattr(tui, "urlopen", tui.build_opener(tui.NoRedirectHandler()).open)


def test_a_turn_waits_for_its_reply_and_other_requests_keep_the_short_timeout():
    opener = FakeOpener([(200, AGENT_TURN), (200, {"project_id": "boss-dogfood", "items": []})])
    client = tui.MetatronClient(_config(), urlopen=opener)
    client.handle(tui.parse_line("set up three projects"))
    client.handle(tui.parse_line("/tasks"))
    assert opener.calls[0]["timeout"] == tui.TURN_TIMEOUT_SECONDS >= 600
    assert opener.calls[1]["timeout"] == tui.DEFAULT_TIMEOUT_SECONDS


def test_a_turn_that_takes_forty_seconds_returns_its_reply(monkeypatch, capsys):
    """A real HTTP server that answers the turn after 40 s, past the 30 s every other request
    allows: the one-shot JSON is the reply, not an error."""
    _real_http(monkeypatch)
    with _fake_boss(delay=40.0) as boss:
        _env(monkeypatch, boss.url)
        assert tui.main(["--exec", "set up three projects", "--json"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert "three projects" in printed["reply"]
    assert "__http_error__" not in printed


def test_a_dropped_turn_is_read_back_from_its_thread(monkeypatch, capsys):
    """The server read the turn and the connection closed without an answer: the client finds
    the reply in the thread (/v1/agent/threads/{id}/messages) once the turn commits it."""
    _real_http(monkeypatch)
    monkeypatch.setattr(tui, "RECOVER_POLL_SECONDS", 0.05)
    with _fake_boss(drop=True, turn_lands_after=2) as boss:
        _env(monkeypatch, boss.url)
        assert tui.main(["--exec", "set up three projects", "--json"]) == 0
        paths = [c["path"] for c in boss.calls]
    printed = json.loads(capsys.readouterr().out)
    assert "three projects" in printed["reply"]
    assert printed["recovered_from_thread"] is True and printed["thread_id"] == "athr_demo"
    assert paths[:2] == ["/v1/metatron/session", "/v1/agent/threads/ensure"]
    assert paths.count("/v1/agent/threads/athr_demo/messages") >= 2


def test_a_dropped_turn_that_has_not_answered_says_it_is_still_running(monkeypatch, capsys):
    _real_http(monkeypatch)
    monkeypatch.setattr(tui, "RECOVER_POLL_SECONDS", 0.05)
    monkeypatch.setattr(tui, "RECOVER_SECONDS", 0.3)
    with _fake_boss(drop=True, turn_lands_after=10_000, working=True) as boss:
        _env(monkeypatch, boss.url)
        assert tui.main(["--exec", "set up three projects", "--json"]) == 1
        result = json.loads(capsys.readouterr().out)
        assert result["still_running"] is True and result["thread_id"] == "athr_demo"
        assert "unreachable" not in result["body"]
        assert "/thread athr_demo" in result["body"]
        # Plain one-shot says the same, without "HTTP 0".
        assert tui.main(["--exec", "set up three projects"]) == 1
        said = capsys.readouterr()
        assert said.err.startswith("Still working:") and "unreachable" not in said.err
        assert "HTTP 0" not in said.err and SECRET not in said.out + said.err


def test_a_refused_connection_is_still_unreachable():
    def refused(_request, timeout=None):
        raise URLError(ConnectionRefusedError(61, "Connection refused"))

    client = tui.MetatronClient(_config(), urlopen=refused)
    payload = client.dispatch(tui.parse_line("hello"))
    assert payload["status"] == 0 and "unreachable" in payload["body"]
    assert "still_running" not in payload


def test_the_thread_command_reads_the_conversation_from_the_server():
    opener = FakeOpener([(200, {"thread_id": "athr_7", "working": False, "messages": [
        {"id": "m1", "role": "user", "content": "set up three projects", "created_at": "x"},
        {"id": "m2", "role": "assistant", "content": "Done: three projects.", "created_at": "x"}]})])
    client = tui.MetatronClient(_config(), urlopen=opener)
    client.thread_id = "athr_7"
    out = client.handle(tui.parse_line("/thread"))
    assert "you: set up three projects" in out and "BOSS: Done: three projects." in out
    assert "/v1/agent/threads/athr_7/messages?project_id=boss-dogfood&actor=ceo" in opener.calls[0]["url"]


def test_new_command_asks_the_server_for_a_new_thread():
    """BOSS_ACTOR alone no longer forks a conversation (identity binds every request to the
    signed-in principal); /new is the explicit ask -- it must carry new_thread=True and adopt
    whatever thread id comes back."""
    opener = FakeOpener([(200, {"thread_id": "athr_new_1"})])
    client = tui.MetatronClient(_config(), urlopen=opener)
    client.thread_id = "athr_old"
    out = client.handle(tui.parse_line("/new"))
    call = opener.calls[0]
    assert call["method"] == "POST"
    assert call["url"].endswith("/v1/agent/threads/ensure")
    assert call["body"]["new_thread"] is True
    assert "thread_id" not in call["body"]  # the stale held id must not defeat /new
    assert client.thread_id == "athr_new_1"
    assert "athr_new_1" in out


def test_new_command_drops_the_last_turns_standing_offers():
    """/pick after /new must have nothing to pick -- the old turn's numbered choices belong to
    the conversation that was just left behind."""
    opener = FakeOpener([(200, AGENT_TURN), (200, {"thread_id": "athr_new_2"})])
    client = tui.MetatronClient(_config(), urlopen=opener)
    client.handle(tui.parse_line("why is the build stuck?"))
    assert client.choices()  # the first turn did offer something
    client.handle(tui.parse_line("/new"))
    assert client.choices() == []


def test_new_command_asks_for_the_held_roles_thread():
    opener = FakeOpener([(200, {"thread_id": "athr_qa_new"})])
    client = tui.MetatronClient(_config(), urlopen=opener)
    client.role, client.role_name = "qa_lead", "QA Lead"
    client.handle(tui.parse_line("/new"))
    assert opener.calls[0]["body"]["role"] == "qa_lead"


def test_new_flag_starts_a_new_conversation_before_the_one_shot_command(monkeypatch):
    opener = FakeOpener([(200, {"thread_id": "athr_new_3"}), (200, AGENT_TURN)])
    monkeypatch.setenv("BOSS_API_URL", "https://boss-api.test")
    monkeypatch.setenv("BOSS_API_KEY", SECRET)
    monkeypatch.setenv("BOSS_PROJECT_ID", "boss-dogfood")
    monkeypatch.setenv("BOSS_ACTOR", "ceo")
    monkeypatch.setattr(tui, "urlopen", opener)
    assert tui.main(["--new", "--exec", "why is the build stuck?", "--json"]) == 0
    assert opener.calls[0]["url"].endswith("/v1/agent/threads/ensure")
    assert opener.calls[0]["body"]["new_thread"] is True
    assert opener.calls[1]["url"].endswith("/v1/metatron/session")
    assert opener.calls[1]["body"]["thread_id"] == "athr_new_3"


def test_agent_model_runs_and_alerts_commands_use_their_routes():
    agents = {"project_id": "boss-dogfood", "items": [
        {"role_key": "qa_lead", "name": "QA Lead", "lifecycle": "active"},
        {"role_key": "old", "name": "Old", "lifecycle": "retired"}]}
    opener = FakeOpener([(200, agents), (200, AGENT_TURN),
                         (200, {"jobs": [{"job": "chat", "resolved": {"label": "Qwen"}, "options": []}]}),
                         (200, {"project_id": "boss-dogfood", "items": []}),
                         (200, {"project_id": "boss-dogfood", "alerts": [], "counts": {}})])
    client = tui.MetatronClient(_config(), urlopen=opener)
    assert "Talking to QA Lead" in client.handle(tui.parse_line("/agent qa_lead"))
    client.handle(tui.parse_line("is the inbox tested?"))
    assert opener.calls[1]["body"]["role"] == "qa_lead"
    client.handle(tui.parse_line("/model ollama:qwen2.5-coder:7b"))
    assert opener.calls[2]["method"] == "PUT" and opener.calls[2]["url"].endswith("/v1/setup/jobs/chat")
    assert opener.calls[2]["body"]["choice"] == {"engine": "ollama", "model": "qwen2.5-coder:7b"}
    client.handle(tui.parse_line("/runs"))
    assert "/v1/workflow-runs?project_id=boss-dogfood" in opener.calls[3]["url"]
    client.handle(tui.parse_line("/alerts all"))
    assert opener.calls[4]["url"].endswith("/v1/alerts?project_id=boss-dogfood&state=all")
    assert tui.parse_line("/alerts loud").kind == "error"
    assert tui.parse_line("/follow wr_1").auto is True
    assert "/agent" in {name for name, _a, _d in tui.SLASH_COMMANDS}


def test_an_agent_is_addressed_by_any_spelling_of_its_name():
    """Live (northpeak-bank, BOSSAPP-768): `/agent site_reliability_engineer` answered "no agent"
    for the Site Reliability Engineer, whose key is `sre`. Any spelling of the name is the agent."""
    agents = {"project_id": "northpeak-bank", "items": [
        {"role_key": "sre", "name": "Site Reliability Engineer", "lifecycle": "active"},
        {"role_key": "incident_commander", "name": "Incident Commander", "lifecycle": "active"}]}
    for typed, key in (("site_reliability_engineer", "sre"), ("Site Reliability Engineer", "sre"),
                       ("site-reliability-engineer", "sre"), ("SRE", "sre"),
                       ("incident commander", "incident_commander")):
        opener = FakeOpener([(200, agents)])
        client = tui.MetatronClient(_config(), urlopen=opener)
        out = client.handle(tui.parse_line(f"/agent {typed}"))
        assert client.role == key, (typed, out)
    opener = FakeOpener([(200, agents)])
    client = tui.MetatronClient(_config(), urlopen=opener)
    assert "no agent 'nobody'" in client.handle(tui.parse_line("/agent nobody"))


def test_the_rich_terminal_stays_a_thin_client_of_the_one_pipeline():
    """Metatron's drawing and keyboard files decide nothing: no model, no router, no agent
    decision, no request of their own. Every request is a MetatronClient method, and the only
    turn routes the client sends are the console's (session → agent_turn, action → the button)."""
    import ast

    signs = {"agent_decision", "route_turn", "IntentRouter", "create_chat_completion",
             "create_text_response", "run_agent_loop", "urlopen", "Request", "urllib", "httpx",
             "requests", "openai", "anthropic"}
    for name in ("metatron_render.py", "metatron_shell.py"):
        tree = ast.parse((ROOT / "scripts" / name).read_text(encoding="utf-8"))
        used = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                used.add(node.id)
            elif isinstance(node, ast.Attribute):
                used.add(node.attr)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                used.update(part for alias in node.names for part in alias.name.split("."))
                used.update((getattr(node, "module", "") or "").split("."))
        assert not used & signs, (name, sorted(used & signs))
    client = (ROOT / "scripts" / "metatron_tui.py").read_text(encoding="utf-8")
    assert "/v1/engine/agent/chat" not in client
    assert client.count('"/v1/metatron/session"') == 1
    assert client.count('"/v1/engine/agent/action"') == 1


# ── /api browses the API instead of demanding a route (BOSSAPP-761 follow-on) ────────────────

def test_api_browses_and_picks_operations_without_losing_the_raw_path_form():
    """Plain /api browses; METHOD-first still sends an operator-built path."""
    assert tui.parse_line("/api").kind == "api_menu"
    assert tui.parse_line("/api").message == ""
    search = tui.parse_line("/api tasks --page 2")
    assert search.kind == "api_menu"
    assert search.message == "tasks --page 2"
    assert tui.parse_line("/api list budget").message == "budget"
    pick = tui.parse_line("/api 7")
    assert pick.kind == "api_template"
    assert pick.message == "7"
    run = tui.parse_line('/api run 7 {"body":{"title":"Plan"}}')
    assert run.kind == "api_run"
    assert run.message == "7"
    assert run.body == {"body": {"title": "Plan"}}
    assert tui.parse_line("/api run 7").body == {}
    raw = tui.parse_line('/api POST /v1/metatron/watch {"project_id":"boss-dogfood"}')
    assert raw.kind == "api"
    assert (raw.method, raw.path) == ("POST", "/v1/metatron/watch")
    for line in (
        "/api 0",
        '/api 7 {"body":{}}',
        "/api run",
        "/api run 7 {bad-json}",
        '/api run 7 {"unknown":{}}',
        "/api POST",
        "/api /v1/tasks",
        "/api GET https://evil.example/v1/tasks",
    ):
        assert tui.parse_line(line).kind == "error", line


def test_api_menu_lists_numbered_operations_with_a_template_for_the_pick():
    opener = FakeOpener([(200, _catalog()), (200, _openapi())])
    client = tui.MetatronClient(_config(), urlopen=opener)
    domains_command = tui.parse_line("/api")
    domains = client.dispatch(domains_command)
    assert domains["view"] == "domains"
    assert [item["id"] for item in domains["domains"]] == ["core", "system"]
    domains_rendered = client.render(domains_command, domains)
    assert "core" in domains_rendered and "/api DOMAIN" in domains_rendered
    assert client.dispatch(tui.parse_line("/api --page 2"))["__command_error__"] is True

    menu_command = tui.parse_line("/api *")
    menu = client.dispatch(menu_command)
    assert menu["view"] == "operations"
    assert [item["index"] for item in menu["operations"]] == [1, 2, 3, 4]
    assert [(item["method"], item["path"]) for item in menu["operations"]] == [
        ("GET", "/v1/tasks"),
        ("POST", "/v1/tasks"),
        ("GET", "/v1/tasks/{task_id}"),
        ("GET", "/v1/undocumented"),
    ]
    assert menu["domains"] == [{"id": "core", "count": 4}]
    rendered = client.render(menu_command, menu)
    assert "   1  GET    /v1/tasks" in rendered
    assert "/api NUMBER" in rendered
    assert SECRET not in rendered

    pick_command = tui.parse_line("/api 3")
    pick = client.dispatch(pick_command)
    assert (pick["method"], pick["path"]) == ("GET", "/v1/tasks/{task_id}")
    assert pick["path_template"] == "/v1/tasks/<task_id>?project_id=boss-dogfood"
    assert pick["api_example"] == "/api GET /v1/tasks/<task_id>?project_id=boss-dogfood"
    assert pick["run_command"] == (
        '/api run 3 {"path":{"task_id":"<task_id>"},"query":{"project_id":"boss-dogfood"}}'
    )
    rendered_pick = client.render(pick_command, pick)
    assert "Path template:" in rendered_pick
    assert "GET /v1/tasks/<task_id>?project_id=boss-dogfood" in rendered_pick
    assert "  /api run 3 {" in rendered_pick

    create = client.dispatch(tui.parse_line("/api 2"))
    assert create["path_template"] == "/v1/tasks"
    assert create["api_example"] == (
        '/api POST /v1/tasks {"project_id":"boss-dogfood","title":"<title>"}'
    )
    assert len(opener.calls) == 2  # The chooser reuses one catalog and schema read.


def test_api_pick_rejects_a_number_outside_the_listing_and_explains_catalog_only_rows():
    opener = FakeOpener([(200, _catalog()), (200, _openapi())])
    client = tui.MetatronClient(_config(), urlopen=opener)
    narrowed = client.dispatch(tui.parse_line("/api tasks"))
    assert narrowed["count"] == 3
    out_of_range = client.dispatch(tui.parse_line("/api 4"))
    assert out_of_range["__command_error__"] is True
    assert "pick 1-3" in out_of_range["message"]
    assert client.dispatch(tui.parse_line("/api nothing-matches-this"))["__command_error__"] is True

    everything = client.dispatch(tui.parse_line("/api *"))
    catalog_only = next(
        item["index"] for item in everything["operations"] if item["path"] == "/v1/undocumented"
    )
    template = client.dispatch(tui.parse_line(f"/api {catalog_only}"))
    assert template["terminal_invokable"] is False
    assert template["api_example"] == "/api GET /v1/undocumented"
    assert template["run_command"] is None
    refused = client.dispatch(tui.parse_line(f"/api run {catalog_only}"))
    assert refused["__command_error__"] is True
    assert "/api GET /v1/undocumented" in refused["message"]


def test_api_run_invokes_the_picked_row_with_the_selected_project():
    opener = FakeOpener([
        (200, _catalog()),
        (200, _openapi()),
        (200, {"id": "task-9", "title": "Plan launch"}),
    ])
    client = tui.MetatronClient(_config(), urlopen=opener)
    menu = client.dispatch(tui.parse_line("/api tasks"))
    create = next(item["index"] for item in menu["operations"] if item["method"] == "POST")
    result = client.dispatch(tui.parse_line(f'/api run {create} {{"body":{{"title":"Plan launch"}}}}'))
    assert result["id"] == "task-9"
    assert opener.calls[2]["method"] == "POST"
    assert opener.calls[2]["url"].endswith("/v1/tasks")
    assert opener.calls[2]["headers"]["x-boss-api-key"] == SECRET
    assert opener.calls[2]["body"] == {"project_id": "boss-dogfood", "title": "Plan launch"}


def test_api_numbers_stay_stable_across_pages_and_work_without_a_prior_listing():
    paths = {f"/v1/records/{index}": {"get": {"summary": f"Read record {index}"}} for index in range(60)}
    routes = [{"method": "GET", "path": path} for path in paths]
    catalog = {"domains": [{"id": "core", "title": "Core", "route_count": 60, "routes": routes}]}
    opener = FakeOpener([(200, catalog), (200, {"paths": paths})])
    client = tui.MetatronClient(_config(), urlopen=opener)
    first = client.dispatch(tui.parse_line("/api *"))
    assert (first["count"], first["pages"]) == (60, 3)
    assert [item["index"] for item in first["operations"]] == list(range(1, 26))
    assert first["next_page_command"] == "/api * --page 2"
    second = client.dispatch(tui.parse_line("/api * --page 2"))
    assert [item["index"] for item in second["operations"]] == list(range(26, 51))
    expected = {item["index"]: item["path"] for item in second["operations"]}
    assert client.dispatch(tui.parse_line("/api 30"))["path"] == expected[30]
    assert client.dispatch(tui.parse_line("/api 61"))["__command_error__"] is True

    # A one-shot pick with no prior listing numbers the whole API the same way.
    fresh = tui.MetatronClient(_config(), urlopen=FakeOpener([(200, catalog), (200, {"paths": paths})]))
    assert fresh.dispatch(tui.parse_line("/api 30"))["path"] == expected[30]


def test_ops_rows_carry_the_same_pick_numbers_as_the_api_chooser():
    opener = FakeOpener([(200, _catalog()), (200, _openapi())])
    client = tui.MetatronClient(_config(), urlopen=opener)
    command = tui.parse_line("/ops tasks")
    found = client.dispatch(command)
    assert [item["index"] for item in found["operations"]] == [1, 2, 3]
    rendered = client.render(command, found)
    assert "   1  get_v1_tasks_339c8c60  GET /v1/tasks" in rendered
    assert "/api NUMBER" in rendered
    picked = client.dispatch(tui.parse_line("/api 1"))
    assert (picked["method"], picked["path"]) == ("GET", "/v1/tasks")


def test_describe_shows_the_path_template_and_a_raw_api_example():
    opener = FakeOpener([(200, _openapi())])
    client = tui.MetatronClient(_config(), urlopen=opener)
    alias = next(
        record["operation_id"] for record in tui.operation_records(_openapi())
        if record["path"] == "/v1/tasks" and record["method"] == "GET"
    )
    command = tui.parse_line(f"/describe {alias}")
    payload = client.dispatch(command)
    assert payload["path_template"] == "/v1/tasks?project_id=boss-dogfood"
    assert payload["api_example"] == "/api GET /v1/tasks?project_id=boss-dogfood"
    assert payload["parameter_fields"] == [
        {"in": "query", "name": "project_id", "required": True, "type": "composite"}]
    rendered = client.render(command, payload)
    assert "Path template:" in rendered
    assert "Copy one of these and replace every <value>:" in rendered
    assert SECRET not in rendered


def test_every_api_form_is_in_the_command_list_the_help_and_completion_read():
    row = next(entry for entry in tui.SLASH_COMMANDS if entry[0] == "/api")
    assert "N" in row[1] and "run N" in row[1] and "METHOD /v1/path" in row[1]
    assert "/api NUMBER" in tui.HELP_TEXT and "/api run NUMBER" in tui.HELP_TEXT


# ---- consent asked in a turn: Metatron prints the console link (operator, 2026-10-24) -------------

CONSENT_LINK = "https://boss.test/account/agents?consent=pcn_abc"
CONSENT_TURN = {
    **AGENT_TURN,
    "reply": "The Software Engineer needs your consent to build in northpeak-bank.",
    "proposed_actions": [
        {"id": "pa_c", "tool": "create_code_task", "args": {"title": "Loan ledger"},
         "label": "Give consent · Build: Loan ledger", "why": "Needs your consent",
         "consent": {"consent_id": "pcn_abc", "grant_href": "/account/agents?consent=pcn_abc",
                     "grant_url": CONSENT_LINK}},
    ],
    "next_steps": [],
}
CONSENT_409 = {"error": "consent_required", "task": "Loan ledger", "task_run_id": None,
               "agents": [{"id": "agt_se", "name": "Software Engineer", "role_key": "builder"}],
               "permissions": ["code_task:read"], "projects": ["northpeak-bank"],
               "durations": [{"kind": "standing", "label": "For 30 days", "days": 30}],
               "consent_id": "pcn_abc", "grant_href": "/account/agents?consent=pcn_abc",
               "grant_url": CONSENT_LINK}


def test_a_give_consent_offer_prints_the_console_link_that_opens_the_sheet():
    opener = FakeOpener([(200, CONSENT_TURN)])
    client = tui.MetatronClient(_config(), urlopen=opener)
    out = client.handle(tui.parse_line("start the build in northpeak-bank"))
    assert "1. Give consent · Build: Loan ledger" in out
    assert f"Give consent in the console: {CONSENT_LINK}, then /pick 1 again." in out
    assert "Create one in the console" not in out


def test_picking_it_before_consenting_says_where_to_consent_not_http_409():
    opener = FakeOpener([(200, CONSENT_TURN), (409, CONSENT_409)])
    client = tui.MetatronClient(_config(), urlopen=opener)
    client.handle(tui.parse_line("start the build in northpeak-bank"))
    out = client.handle(tui.parse_line("1"))
    assert opener.calls[1]["url"].endswith("/v1/engine/agent/action")
    assert "Software Engineer need your consent" in out and CONSENT_LINK in out
    assert "HTTP 409" not in out and "consent_required" not in out
    # Nothing ran, so the offer still stands to be picked after the grant.
    assert [c["tool"] for c in client.choices()] == ["create_code_task"]


def test_without_a_public_address_the_link_is_the_console_path():
    assert tui.consent_where({"grant_href": "/account/agents?consent=pcn_x"}) == \
        "Give consent in the BOSS console at /account/agents?consent=pcn_x"
    assert tui.consent_refusal("not json") is None
    assert tui.consent_refusal(json.dumps({"detail": CONSENT_409})).startswith("Nothing ran")
