"""Open-model (Ollama/native) lane conformance suite (BOSSAPP-1024).

Drives the REAL `NativeCodeAgent` -> `OpenAIAdapter` -> `OpenAI` SDK -> loopback HTTP server
-> `SandboxToolset` path against a deterministic recorded fake OpenAI-compatible server
(no model download, no external network).

Every regression class from the 2026-11-12 local-lane audit MUST fail this suite before merge:
  * BOSSAPP-1019: Groq 1024-token clamp firing on local Ollama `qwen3:8b`, starving thinking
    models (`reasoning_content` > 1024 tokens) before they reach content or `tool_calls`.
  * BOSSAPP-1022: Weak-model doubly-escaped JSON tool arguments (`\\n` one-line file writes
    and edits) breaking Python syntax / test runners.
  * BOSSAPP-1023: Colon model IDs (`qwen3:8b`, `llama3.1:8b`, `ollama:qwen3:8b`) misparsed as
    an unknown harness prefix in governed pipeline routing or mangled on the wire.

Also proves:
  * protocol `tool_calls` multi-turn exploration + edit + verification loop over HTTP;
  * prose-instead-of-tools graceful completion & malformed tool-argument resilience;
  * read-only reviewer schema isolation over the wire;
  * periodic real-Ollama smoke runner (`scripts/ollama_smoke.py`) preflight & command plan.
"""
from __future__ import annotations

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import sys
import threading
from typing import Any, Callable

import pytest

from boss_engine.config import Settings
from boss_engine.services.code_agent import NativeCodeAgent, make_code_agent
from boss_engine.services.harness_registry import UnknownHarness, get_harness
from boss_engine.services.included_spend import BILLING_LOCAL
from boss_engine.services.model_router.policy import PolicyError, _target
from boss_engine.services.model_router.router import _parse_pin
from boss_engine.services.openai_adapter import (
    GROQ_QWEN_MAX_OUTPUT_TOKENS,
    OpenAIAdapter,
    capped_max_output_tokens,
)
from boss_engine.services.pipeline_profiles import (
    PROFILES,
    _parse_route,
    materialize_route,
    route_for_step,
)


class _RecordedOpenAIServer:
    """Deterministic loopback OpenAI-compatible HTTP server replaying recorded responses."""

    def __init__(
        self,
        handler: Callable[[dict[str, Any], int], tuple[int, dict[str, Any]]],
        *,
        models: tuple[str, ...] = ("qwen3:8b", "llama3.1:8b"),
    ) -> None:
        self._handler = handler
        self._models = models
        self.requests: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        assert self._httpd is not None
        host, port = self._httpd.server_address
        return f"http://{host}:{port}/v1"

    def start(self) -> "_RecordedOpenAIServer":
        parent = self

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return

            def do_GET(self) -> None:  # noqa: N802
                if self.path.rstrip("/") in ("/v1/models", "/api/tags"):
                    payload = {
                        "object": "list",
                        "data": [{"id": m, "object": "model"} for m in parent._models],
                        "models": [{"name": m} for m in parent._models],
                    }
                    body = json.dumps(payload).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self.send_response(404)
                self.end_headers()

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                req = json.loads(raw.decode("utf-8"))
                with parent._lock:
                    turn_index = len(parent.requests)
                    parent.requests.append(req)
                status, payload = parent._handler(req, turn_index)
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)


@contextmanager
def recorded_server(
    handler: Callable[[dict[str, Any], int], tuple[int, dict[str, Any]]],
    *,
    models: tuple[str, ...] = ("qwen3:8b", "llama3.1:8b"),
):
    srv = _RecordedOpenAIServer(handler, models=models).start()
    try:
        yield srv
    finally:
        srv.stop()


def _chat_response(
    *,
    model: str,
    content: str | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
    reasoning_content: str | None = None,
    prompt_tokens: int = 120,
    completion_tokens: int = 80,
    finish_reason: str = "stop",
) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if reasoning_content is not None:
        message["reasoning_content"] = reasoning_content
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
        finish_reason = "tool_calls"
    return {
        "id": "chatcmpl-recorded",
        "object": "chat.completion",
        "created": 1731400000,
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


def _tc(call_id: str, name: str, arguments: str) -> dict[str, Any]:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


def _ollama_agent(base_url: str, *, model: str = "qwen3:8b", **overrides: Any) -> NativeCodeAgent:
    settings = Settings(
        BOSS_CODE_AGENT_CONTEXT_FILES=0,
        BOSS_CODE_AGENT_ENGINE="ollama",
        OLLAMA_BASE_URL=base_url,
        OLLAMA_API_KEY="ollama",
        OPENAI_API_KEY="ollama",
        OPENAI_BASE_URL=base_url,
        OPENAI_MODEL=model,
        BOSS_NATIVE_MAX_OUTPUT_TOKENS=8192,
        BOSS_SANDBOX_VALIDATION_ALLOWED_COMMANDS="python3",
        **overrides,
    )
    adapter = OpenAIAdapter(settings, base_url=base_url, api_key="ollama", provider="ollama")
    agent = NativeCodeAgent(settings)
    agent.engine = "ollama"
    agent._llm = adapter
    return agent


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("BOSS_CAPTURE_AGENT_TURNS", "0")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "math_ops.py").write_text(
        "def add(a, b):\n    return a - b  # BUG\n",
        encoding="utf-8",
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_math_ops.py").write_text(
        "import unittest\n"
        "from src.math_ops import add\n\n"
        "class TestMathOps(unittest.TestCase):\n"
        "    def test_add(self):\n"
        "        self.assertEqual(add(2, 3), 5)\n",
        encoding="utf-8",
    )
    return tmp_path


# ── 1. Protocol tool_calls multi-turn loop over real HTTP ───────────────────────────────────

def test_ollama_protocol_tool_calls_explore_edit_and_verify(repo: Path) -> None:
    def _replay(req: dict[str, Any], turn: int) -> tuple[int, dict[str, Any]]:
        model = req["model"]
        if turn == 0:
            return 200, _chat_response(
                model=model,
                tool_calls=[
                    _tc("c1", "grep", json.dumps({"pattern": "def add"})),
                    _tc("c2", "read_file", json.dumps({"path": "src/math_ops.py"})),
                ],
            )
        if turn == 1:
            return 200, _chat_response(
                model=model,
                tool_calls=[
                    _tc("c3", "edit_file", json.dumps({
                        "path": "src/math_ops.py",
                        "old_string": "return a - b  # BUG",
                        "new_string": "return a + b",
                    })),
                ],
            )
        if turn == 2:
            return 200, _chat_response(
                model=model,
                tool_calls=[
                    _tc("c4", "bash", json.dumps({"command": "python3 -m unittest discover -s tests"})),
                ],
            )
        return 200, _chat_response(model=model, content="Fixed add() and verified unit tests pass.")

    with recorded_server(_replay) as srv:
        out = _ollama_agent(srv.base_url, model="qwen3:8b").run_role("Fix add()", repo, model="qwen3:8b")

    assert out["stop_reason"] == "done"
    assert out["turns"] == 4
    assert out["tool_calls"] == 4
    assert out["files_touched"] == ["src/math_ops.py"]
    assert out["usage"].get("billing") == BILLING_LOCAL
    assert "return a + b" in (repo / "src" / "math_ops.py").read_text(encoding="utf-8")
    # Tool results flowed back on subsequent turns as role="tool".
    final_tool_msgs = [m for m in srv.requests[-1]["messages"] if m.get("role") == "tool"]
    assert len(final_tool_msgs) == 4
    assert any("OK" in str(m.get("content")) for m in final_tool_msgs)


# ── 2. Prose instead of tools (and malformed tool JSON resilience) ──────────────────────────

def test_prose_instead_of_tools_exits_cleanly_without_hanging(repo: Path) -> None:
    def _replay(req: dict[str, Any], _turn: int) -> tuple[int, dict[str, Any]]:
        return 200, _chat_response(
            model=req["model"],
            content="To fix this bug, edit src/math_ops.py and change a - b to a + b.",
            tool_calls=None,
        )

    with recorded_server(_replay) as srv:
        out = _ollama_agent(srv.base_url, model="llama3.1:8b").run_role(
            "Fix add()", repo, model="llama3.1:8b"
        )

    assert out["stop_reason"] == "done"
    assert out["turns"] == 1
    assert out["tool_calls"] == 0
    assert out["files_touched"] == []
    assert "change a - b to a + b" in out["summary"]
    assert len(srv.requests) == 1


def test_malformed_tool_arguments_feed_error_back_instead_of_crashing(repo: Path) -> None:
    def _replay(req: dict[str, Any], turn: int) -> tuple[int, dict[str, Any]]:
        if turn == 0:
            return 200, _chat_response(
                model=req["model"],
                tool_calls=[_tc("bad1", "read_file", "{not valid json")],
            )
        return 200, _chat_response(model=req["model"], content="Recovered from malformed JSON.")

    with recorded_server(_replay) as srv:
        out = _ollama_agent(srv.base_url, model="llama3.1:8b").run_role(
            "Inspect repo", repo, model="llama3.1:8b"
        )

    assert out["stop_reason"] == "done"
    assert out["turns"] == 2
    assert "Recovered" in out["summary"]
    tool_msgs = [m for m in srv.requests[-1]["messages"] if m.get("role") == "tool"]
    assert tool_msgs and "error:" in tool_msgs[0]["content"]


# ── 3. Thinking burn (1k+ reasoning tokens) + BOSSAPP-1019 regression guard ─────────────────

def test_thinking_burn_qwen_on_ollama_gets_full_output_budget_catching_1019(repo: Path) -> None:
    """If BOSSAPP-1019 regresses (clamping non-Groq qwen to 1024 tokens), the recorded server
    simulates qwen3:8b exhausting its 1024-token budget on reasoning_content with empty content
    and zero tool calls — failing this test immediately before merge."""
    thinking_trace = ("reasoning step " * 350).strip()  # > 1024 tokens worth of reasoning

    def _replay(req: dict[str, Any], turn: int) -> tuple[int, dict[str, Any]]:
        max_tokens = int(req.get("max_tokens") or 0)
        if max_tokens and max_tokens <= 1024:
            # Exact BOSSAPP-1019 failure signature: burned entire budget thinking, zero tools.
            return 200, _chat_response(
                model=req["model"],
                content="",
                reasoning_content=thinking_trace,
                tool_calls=None,
                completion_tokens=1024,
                finish_reason="length",
            )
        if turn == 0:
            return 200, _chat_response(
                model=req["model"],
                content=None,
                reasoning_content=thinking_trace,
                tool_calls=[_tc("c1", "edit_file", json.dumps({
                    "path": "src/math_ops.py",
                    "old_string": "return a - b  # BUG",
                    "new_string": "return a + b",
                }))],
                completion_tokens=1480,
            )
        return 200, _chat_response(
            model=req["model"],
            content="Fixed after deep reasoning.",
            reasoning_content="Verified edit landed.",
            completion_tokens=1150,
        )

    with recorded_server(_replay) as srv:
        out = _ollama_agent(srv.base_url, model="qwen3:8b").run_role(
            "Fix add()", repo, model="qwen3:8b"
        )

    assert srv.requests[0]["max_tokens"] == 8192, "local Ollama qwen must NOT hit Groq's 1024 clamp"
    assert out["tool_calls"] == 1
    assert out["files_touched"] == ["src/math_ops.py"]
    assert out["usage"]["completion_tokens"] > 1024
    assert "return a + b" in (repo / "src" / "math_ops.py").read_text(encoding="utf-8")
    # Positive counterpart: Groq's real endpoint STILL clamps to GROQ_QWEN_MAX_OUTPUT_TOKENS.
    assert capped_max_output_tokens(
        8192, "qwen/qwen3-32b", base_url="https://api.groq.com/openai/v1"
    ) == GROQ_QWEN_MAX_OUTPUT_TOKENS


# ── 4. Double-escaped JSON tool args + BOSSAPP-1022 regression guard ────────────────────────

def test_double_escaped_tool_args_land_as_compilable_runnable_tests_catching_1022(repo: Path) -> None:
    """Replays llama3.1:8b's exact double-escaped JSON tool args over HTTP. If BOSSAPP-1022
    regresses, the written test file is a single line of literal \\n and unittest fails."""
    doubly_escaped_test = (
        "import unittest\\n"
        "from src.math_ops import add\\n\\n"
        "class TestGenerated(unittest.TestCase):\\n"
        "    def test_add_positive(self):\\n"
        "        self.assertEqual(add(4, 6), 10)\\n"
    )

    def _replay(req: dict[str, Any], turn: int) -> tuple[int, dict[str, Any]]:
        if turn == 0:
            return 200, _chat_response(
                model=req["model"],
                tool_calls=[
                    _tc("w1", "edit_file", json.dumps({
                        "path": "src/math_ops.py",
                        "old_string": "def add(a, b):\\n    return a - b  # BUG",
                        "new_string": "def add(a, b):\\n    return a + b",
                    })),
                    _tc("w2", "write_file", json.dumps({
                        "path": "tests/test_generated.py",
                        "content": doubly_escaped_test,
                    })),
                ],
            )
        return 200, _chat_response(model=req["model"], content="Wrote fix and regression test.")

    with recorded_server(_replay) as srv:
        out = _ollama_agent(srv.base_url, model="llama3.1:8b").run_role(
            "Fix add() and add tests/test_generated.py", repo, model="llama3.1:8b"
        )

    assert out["stop_reason"] == "done"
    assert sorted(out["files_touched"]) == ["src/math_ops.py", "tests/test_generated.py"]
    written = (repo / "tests" / "test_generated.py").read_text(encoding="utf-8")
    assert "\\n" not in written and written.count("\n") >= 5
    compile(written, "test_generated.py", "exec")
    proc = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
        cwd=repo, capture_output=True, text=True, check=False, timeout=30,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    tool_msgs = [m for m in srv.requests[-1]["messages"] if m.get("role") == "tool"]
    assert any("normalized doubly-escaped newlines" in str(m.get("content")) for m in tool_msgs)


# ── 5. Colon model IDs + BOSSAPP-1023 regression guard ──────────────────────────────────────

@pytest.mark.parametrize("model_id", ["qwen3:8b", "llama3.1:8b", "qwen2.5-coder:7b"])
def test_colon_model_ids_route_cleanly_and_reach_server_intact_catching_1023(
    repo: Path, model_id: str
) -> None:
    """If BOSSAPP-1023 regresses, colon model IDs like `qwen3:8b` are misparsed as an unknown
    harness (`qwen3`) instead of an Ollama model tag."""
    # Governed pipeline routing surfaces
    assert _parse_route(model_id)["engine"] is None
    assert _parse_route(model_id)["model"] == model_id
    assert (_parse_route(f"ollama:{model_id}")["engine"], _parse_route(f"ollama:{model_id}")["model"]) == (
        "ollama",
        model_id,
    )
    route = route_for_step(PROFILES["balanced"], "implement", {"default": f"ollama:{model_id}"})
    assert materialize_route(route, build_engine="claude_code", review_engine="claude_code") == (
        "ollama",
        model_id,
    )
    get_harness("ollama")
    with pytest.raises(UnknownHarness):
        get_harness(model_id.split(":", 1)[0])
    pin = _parse_pin(model_id, "ollama")
    assert (pin.engine, pin.model) == ("ollama", model_id)
    routed = _target(f"ollama:{model_id}", "tiers.fast")
    assert (routed.engine, routed.model) == ("ollama", model_id)
    with pytest.raises(PolicyError):
        _target(model_id, "tiers.fast")

    # End-to-end HTTP wire check via make_code_agent(..., engine="ollama")
    def _replay(req: dict[str, Any], _turn: int) -> tuple[int, dict[str, Any]]:
        return 200, _chat_response(model=req["model"], content=f"ok from {req['model']}")

    with recorded_server(_replay, models=(model_id,)) as srv:
        settings = Settings(
            BOSS_CODE_AGENT_CONTEXT_FILES=0,
            BOSS_CODE_AGENT_ENGINE="ollama",
            OLLAMA_BASE_URL=srv.base_url,
            OLLAMA_API_KEY="ollama",
        )
        agent = make_code_agent(settings, engine="ollama")
        out = agent.run_role("Say hi", repo, model=model_id)

    assert out["stop_reason"] == "done"
    assert srv.requests[0]["model"] == model_id
    assert out["model"] == model_id


def test_read_only_review_on_open_model_lane_never_offers_write_tools_over_http(repo: Path) -> None:
    def _replay(req: dict[str, Any], _turn: int) -> tuple[int, dict[str, Any]]:
        return 200, _chat_response(model=req["model"], content="SURVIVED")

    with recorded_server(_replay) as srv:
        _ollama_agent(srv.base_url, model="qwen3:8b").run_role(
            "Review", repo, read_only=True, model="qwen3:8b"
        )

    offered = {t["function"]["name"] for t in srv.requests[0]["tools"]}
    assert offered == {"read_file", "list_dir", "glob", "grep"}


# ── 6. Periodic real-Ollama smoke runner (no model download in CI) ──────────────────────────

def test_ollama_smoke_runner_preflight_and_command_plan() -> None:
    import importlib.util

    script = Path(__file__).resolve().parents[2] / "scripts" / "ollama_smoke.py"
    spec = importlib.util.spec_from_file_location("ollama_smoke", script)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    def _noop(req: dict[str, Any], _turn: int) -> tuple[int, dict[str, Any]]:
        return 200, _chat_response(model=req["model"], content="ok")

    with recorded_server(_noop, models=("qwen2.5-coder:7b",)) as srv:
        ok, available, detail = mod.check_ollama_ready(srv.base_url, "qwen2.5-coder:7b")
        assert ok is True, detail
        assert "qwen2.5-coder:7b" in available

        missing_ok, _avail, missing_detail = mod.check_ollama_ready(srv.base_url, "not-pulled:1b")
        assert missing_ok is False
        assert "not-pulled:1b" in missing_detail

    plan = mod.build_smoke_commands(
        model="qwen2.5-coder:7b",
        tasks=("helpdesk-classify-case", "textkit-word-count"),
        levels=("bare", "delegate"),
        db_url="postgresql+psycopg://boss:pw@127.0.0.1:55700/boss_test_bench",
        budget_usd=1.0,
        allow_unisolated=True,
    )
    assert len(plan) == 2
    assert "--level" in plan[0] and "bare" in plan[0]
    assert "--level" in plan[1] and "delegate" in plan[1]
    assert any("native:qwen2.5-coder:7b" in cmd or "ollama:qwen2.5-coder:7b" in cmd for cmd in plan)
