"""Unit tests for the Metatron captain TUI (stdlib, mocked HTTP, no live API)."""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError

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
