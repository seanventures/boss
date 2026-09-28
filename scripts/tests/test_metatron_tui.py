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
    go = tui.parse_line("go")
    assert go.kind == "drive"
    assert go.auto is True


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
    client.handle(tui.parse_line("go"))
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
    assert len(records) == 981
    operator_records = [record for record in records if record["path"].startswith("/v1/")]
    assert len(operator_records) == 968
    assert all(record["addressable"] for record in operator_records)
    assert len({record["operation_id"] for record in records}) == len(records)
    assert all(record["operation_id"] == record["runtime_operation_id"] for record in records)


def test_private_snapshot_maps_every_operator_operation_except_inbound_callbacks():
    if not (ROOT / "api" / "openapi.community.yaml").exists():
        pytest.skip("private contract is absent from the Community export")
    yaml = pytest.importorskip("yaml")
    document = yaml.safe_load((ROOT / "api" / "openapi.yaml").read_text(encoding="utf-8"))
    records = tui.operation_records(document)
    assert len(records) == 1022
    operator_records = [record for record in records if record["path"].startswith("/v1/")]
    assert len(operator_records) == 1009
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
    assert counts == {"openapi": 981, "operator": 968}
