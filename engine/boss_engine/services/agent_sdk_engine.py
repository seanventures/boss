"""THE CLAUDE AGENT SDK ENGINE (`claude_agent_sdk`).

The same agent loop as `claude_code` — Claude Code's own tools, permission modes, sessions —
driven through Anthropic's official Python package (`claude-agent-sdk`) on an ANTHROPIC API KEY.
The difference is who pays: a key's calls are metered Agent SDK/API usage on the operator's
Anthropic account (which is what API credits cover), never the Claude subscription.

It sits behind the same engine interface as every other harness (code_agent.CodeAgent): it is
`ClaudeCodeAgent` with one method replaced — `_invoke`, the part that starts a session — so the
prompts, budgets, effort, tool policy, read-only review seats, turn capture, usage/cost and
billing records, and the `run`/`run_role`/`run_research` contract are the claude_code engine's,
line for line. Nothing downstream (gates, build profile, activity) can tell the engines apart
except by the `engine` column.

What this module adds:

  * `OwnedTransport` — the SDK's subprocess transport, spawned through `owned_process.popen`
    instead of `anyio.open_process`, so the session's CLI runs exactly where a claude_code
    agent runs: as the run's sandbox uid, in its own process group, owned by the build (a stop
    or a timeout ends the whole tree), niced, and with the deny-by-default agent environment —
    never the worker's inherited environment, which the SDK's own transport would pass through.
  * `message_to_event` — the SDK's typed messages back into the CLI's stream-json event shape,
    fed to the one StreamParser every Claude transcript goes through (agent_turns), so
    agent_turns rows are identical to claude_code's.
  * the credential: `claude_credential.resolve_anthropic_api_key` — the console's stored key,
    else ANTHROPIC_API_KEY — and never the subscription login. It reaches only the CLI child's
    environment; CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1 makes the CLI strip it from every
    subprocess its tools start (Bash, hooks, MCP servers), and it is registered with the log
    redactor the moment it is read.
"""
from __future__ import annotations

import asyncio
import collections
import contextvars
import json
import logging
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Optional

log = logging.getLogger(__name__)

ENGINE = "claude_agent_sdk"
#: The pinned package (engine/requirements.txt, apps/worker/requirements.txt).
SDK_PACKAGE = "claude-agent-sdk"
SDK_PIN = "0.2.166"
DEFAULT_MODEL = "claude-sonnet-5"
#: Claude Code strips Anthropic and cloud credentials from the environment of every subprocess
#: it starts (its Bash tool, hooks, MCP servers) when this is 1: the key runs the session and
#: is not something the agent's own commands can read.
SCRUB_ENV = "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB"

#: The CLI's tool names ARE BOSS's common names (Read/Write/Edit/Bash/Grep/Glob…): the SDK
#: drives the same CLI. Only the server-side tool spellings and old aliases differ.
_TOOL_NAMES = {
    "web_search": "WebSearch", "web_fetch": "WebFetch", "bash_code_execution": "Bash",
    "code_execution": "Bash", "text_editor_code_execution": "Edit", "MultiEdit": "Edit",
    "Task": "Agent",
}


class NoAnthropicKey(RuntimeError):
    """The engine needs an Anthropic API key and none is stored."""


NO_KEY_MESSAGE = (
    "The Claude Agent SDK engine runs on an Anthropic API key, and none is stored. "
    "Store one in Repo and models -> Anthropic API, or pick another engine.")


def sdk_available() -> bool:
    """Whether the `claude_agent_sdk` package is importable here. Never raises."""
    try:
        import claude_agent_sdk  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


def normalise_tool_name(name: Any) -> str:
    name = str(name or "")
    return _TOOL_NAMES.get(name, name)


# ── Credential ────────────────────────────────────────────────────────────────────────────────

def key_env(settings: Any, env: dict) -> tuple[dict, Any]:
    """`env` with exactly the Anthropic API key in it (never the login), and the scrub on.
    Raises NoAnthropicKey when none is held."""
    from .claude_credential import resolve_anthropic_api_key

    cred = resolve_anthropic_api_key(settings)
    if not cred.is_key:
        raise NoAnthropicKey(NO_KEY_MESSAGE)
    cred.apply(env)
    env[SCRUB_ENV] = "1"
    return env, cred


def readiness(connection: Any, settings: Any, project_id: Optional[str]) -> dict[str, Any]:
    """Ready when an Anthropic API key is stored (or set for the install) and the package is
    importable. Reads no secret value. Never raises."""
    if not sdk_available():
        return {"engine": ENGINE, "ready": False,
                "why": f"the {SDK_PACKAGE} package is not installed on this server"}
    try:
        from .claude_credential import KIND_ENV_KEY, KIND_STORED_KEY, describe, _env_key

        has_key = bool(_env_key(settings))
        if not has_key and connection is not None and project_id:
            state = describe(connection, settings, project_id)
            # `describe` names the ACTIVE credential; a stored key behind the subscription
            # still counts here, so ask for it directly.
            has_key = state["active"] in (KIND_STORED_KEY, KIND_ENV_KEY) or _stored_key_present(
                connection, project_id)
    except Exception:  # noqa: BLE001
        has_key = False
    if not has_key:
        return {"engine": ENGINE, "ready": False, "why": "no Anthropic API key is stored"}
    return {"engine": ENGINE, "ready": True, "why": "Anthropic API key stored"}


def _stored_key_present(connection: Any, project_id: str) -> bool:
    from .claude_credential import _project_ref

    try:
        return bool(_project_ref(connection, project_id, token=False))
    except Exception:  # noqa: BLE001
        return False


# ── Messages -> the CLI's stream-json events ──────────────────────────────────────────────────

def _block(block: Any) -> dict[str, Any]:
    kind = type(block).__name__
    if kind == "TextBlock":
        return {"type": "text", "text": getattr(block, "text", "")}
    if kind == "ThinkingBlock":
        return {"type": "thinking", "thinking": getattr(block, "thinking", "")}
    if kind in ("ToolUseBlock", "ServerToolUseBlock"):
        return {"type": "tool_use", "id": getattr(block, "id", ""),
                "name": normalise_tool_name(getattr(block, "name", "")),
                "input": getattr(block, "input", None)}
    if kind in ("ToolResultBlock", "ServerToolResultBlock"):
        return {"type": "tool_result", "tool_use_id": getattr(block, "tool_use_id", ""),
                "content": getattr(block, "content", None),
                "is_error": bool(getattr(block, "is_error", False))}
    if isinstance(block, dict):
        return block
    return {"type": "unknown"}


def message_to_event(msg: Any) -> Optional[dict[str, Any]]:
    """One SDK message as the CLI's stream-json event (the shape agent_turns.StreamParser and
    ClaudeCodeAgent._usage read), or None for messages that carry no turn or result."""
    kind = type(msg).__name__
    if kind == "AssistantMessage":
        return {"type": "assistant", "session_id": getattr(msg, "session_id", None),
                "message": {"role": "assistant", "model": getattr(msg, "model", "") or "",
                            "usage": getattr(msg, "usage", None) or {},
                            "content": [_block(b) for b in (msg.content or [])]}}
    if kind == "UserMessage":
        content = msg.content
        blocks = ([{"type": "text", "text": content}] if isinstance(content, str)
                  else [_block(b) for b in (content or [])])
        return {"type": "user", "message": {"role": "user", "content": blocks}}
    if kind == "ResultMessage":
        return {
            "type": "result", "subtype": getattr(msg, "subtype", "") or "",
            "is_error": bool(getattr(msg, "is_error", False)),
            "num_turns": int(getattr(msg, "num_turns", 0) or 0),
            "session_id": getattr(msg, "session_id", None),
            "result": getattr(msg, "result", None),
            "usage": getattr(msg, "usage", None) or {},
            "modelUsage": dict(getattr(msg, "model_usage", None) or {}),
            "total_cost_usd": getattr(msg, "total_cost_usd", None),
            "duration_ms": getattr(msg, "duration_ms", None),
            "stop_reason": getattr(msg, "stop_reason", None),
            "api_error_status": getattr(msg, "api_error_status", None),
            "errors": list(getattr(msg, "errors", None) or []),
        }
    if kind == "RateLimitEvent":
        info = getattr(msg, "rate_limit_info", None)
        return {"type": "rate_limit_event",
                "rate_limit_info": getattr(info, "__dict__", None) or {}}
    return None


# ── The transport: the SDK's CLI, spawned the way every BOSS agent is ────────────────────────

def _to_thread(fn: Callable, *args: Any):
    import anyio

    try:
        return anyio.to_thread.run_sync(fn, *args, abandon_on_cancel=True)
    except TypeError:  # anyio < 4.1
        return anyio.to_thread.run_sync(fn, *args, cancellable=True)


def make_owned_transport(prompt: Any, options: Any, *, extra_argv: tuple[str, ...] = (),
                         seat: str = "agent"):
    """The SDK's subprocess transport with its process started by `owned_process.popen`.

    The command line is the SDK's own (`_build_command`, pinned version), so every option —
    model, tools, permission mode, max turns, resume, system prompt — means what the SDK says it
    means. Only the spawn differs: the SDK's would run the CLI as the worker's uid with the
    worker's whole environment."""
    from claude_agent_sdk import __version__ as sdk_version
    from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport

    from . import owned_process
    from .scheduling import low_priority

    class OwnedTransport(SubprocessCLITransport):
        def __init__(self) -> None:
            super().__init__(prompt, options)
            self._extra = list(extra_argv)
            self._proc: Any = None
            self._stderr_tail: collections.deque = collections.deque(maxlen=40)
            self._wlock = threading.Lock()

        def command(self) -> list[str]:
            if self._cli_path is None:
                self._cli_path = self._find_cli()
            return self._build_command() + self._extra

        async def connect(self) -> None:
            if self._proc is not None:
                return
            cmd = self.command()
            env = {**(self._options.env or {}), "CLAUDE_CODE_ENTRYPOINT": "sdk-py",
                   "CLAUDE_AGENT_SDK_VERSION": sdk_version}
            if self._cwd:
                env["PWD"] = self._cwd
            from .code_agent import register_seat

            self._proc = owned_process.popen(
                low_priority(cmd), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, bufsize=1, cwd=self._cwd, env=env)
            register_seat(self._proc.pid, seat)
            threading.Thread(target=self._drain_stderr, daemon=True).start()
            self._ready = True

        def _drain_stderr(self) -> None:
            try:
                for line in self._proc.stderr:
                    self._stderr_tail.append(line.rstrip())
            except Exception:  # noqa: BLE001
                pass

        def stderr_tail(self) -> str:
            from ..identity.redaction import redact_text

            return redact_text("\n".join(self._stderr_tail))[-600:]

        def _write(self, data: str) -> None:
            with self._wlock:
                if self._proc is None or self._proc.stdin is None or self._proc.stdin.closed:
                    raise RuntimeError("the agent session's input is closed")
                self._proc.stdin.write(data)
                self._proc.stdin.flush()

        async def write(self, data: str) -> None:
            await _to_thread(self._write, data)

        async def read_messages(self) -> AsyncIterator[dict[str, Any]]:
            while True:
                line = await _to_thread(self._proc.stdout.readline)
                if not line:
                    break
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    data = json.loads(line)
                except ValueError:
                    continue
                if isinstance(data, dict):
                    yield data
            code = await _to_thread(self._proc.wait)
            if code not in (0, None):
                raise RuntimeError(
                    f"claude agent sdk session exited {code}: {self.stderr_tail()}")

        async def end_input(self) -> None:
            def _close() -> None:
                with self._wlock:
                    if self._proc is not None and self._proc.stdin and not self._proc.stdin.closed:
                        self._proc.stdin.close()
            await _to_thread(_close)

        def close_now(self) -> None:
            proc, self._proc = self._proc, None
            self._ready = False
            if proc is None:
                return
            from .code_agent import release_seat

            release_seat(proc.pid)
            try:
                if hasattr(proc, "end"):
                    proc.end(reason="session_closed", grace=2.0)
                elif proc.poll() is None:
                    proc.kill()
            except Exception:  # noqa: BLE001
                pass

        async def close(self) -> None:
            self.close_now()

        def is_ready(self) -> bool:
            return bool(self._ready and self._proc is not None)

    return OwnedTransport()


# ── Running one session ───────────────────────────────────────────────────────────────────────

def _query(**kwargs: Any):
    """`claude_agent_sdk.query` — a seam the tests replace; nothing else calls the SDK."""
    from claude_agent_sdk import query

    return query(**kwargs)


def _run_coro(factory: Callable[[], Any]) -> Any:
    """Run an async session from synchronous code (the build pipeline's worker threads). On a
    thread that already runs an event loop, a helper thread runs it with this thread's context
    (the run/stage/owner context the recorders and the sandbox read)."""
    import anyio

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return anyio.run(factory)
    ctx = contextvars.copy_context()
    box: dict[str, Any] = {}

    def target() -> None:
        try:
            box["value"] = ctx.run(anyio.run, factory)
        except BaseException as exc:  # noqa: BLE001 — re-raised on the caller's thread
            box["error"] = exc

    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join()
    if "error" in box:
        raise box["error"]
    return box.get("value")


class SessionTimeout(RuntimeError):
    pass


def run_session(prompt: str, options: Any, *, timeout: float,
                on_event: Optional[Callable[[dict], None]] = None,
                transport_factory: Optional[Callable[[], Any]] = None) -> Optional[dict]:
    """Run one SDK session to its result. Every message is converted (`message_to_event`) and
    handed to `on_event`; returns the result event (None if the session ended without one).
    Raises SessionTimeout past `timeout` seconds, with the session's process tree ended."""
    import anyio

    holder: dict[str, Any] = {}

    async def main() -> Optional[dict]:
        result = None
        transport = transport_factory() if transport_factory else None
        holder["transport"] = transport
        kwargs: dict[str, Any] = {"prompt": prompt, "options": options}
        if transport is not None:
            kwargs["transport"] = transport
        with anyio.fail_after(timeout):
            stream = _query(**kwargs)
            try:
                async for msg in stream:
                    event = message_to_event(msg)
                    if event is None:
                        continue
                    if event["type"] == "result":
                        result = event
                    if on_event is not None:
                        try:
                            on_event(event)
                        except Exception:  # noqa: BLE001 — a recorder never fails a session
                            pass
            finally:
                aclose = getattr(stream, "aclose", None)
                if aclose is not None:
                    with anyio.CancelScope(shield=True):
                        try:
                            await aclose()
                        except Exception:  # noqa: BLE001
                            pass
        return result

    try:
        return _run_coro(main)
    except TimeoutError as exc:
        raise SessionTimeout(f"claude agent sdk session timed out after {int(timeout)}s") from exc
    finally:
        transport = holder.get("transport")
        if transport is not None and hasattr(transport, "close_now"):
            transport.close_now()


# ── The engine ────────────────────────────────────────────────────────────────────────────────

def _transient(detail: str) -> bool:
    lowered = detail.lower()
    return any(m in lowered for m in (
        "connection error", "overloaded", "internal server error", "network error",
        "econnreset", "socket hang up", "529", "503", "bad gateway"))


def build_agent_class():
    """`ClaudeAgentSdkAgent`, defined against code_agent at call time (no import cycle)."""
    from . import code_agent as ca
    from .agent_turns import StreamParser, capture_enabled

    class ClaudeAgentSdkAgent(ca.ClaudeCodeAgent):
        """claude_code's agent, its sessions started through the Claude Agent SDK on the
        Anthropic API key. Everything but `_invoke` and the credential is inherited."""

        engine = ENGINE

        def _resolve_credential(self):
            from .claude_credential import resolve_anthropic_api_key

            cred = resolve_anthropic_api_key(self.settings)
            if not cred.is_key:
                raise NoAnthropicKey(NO_KEY_MESSAGE)
            return cred

        def _child_env(self) -> dict:
            env = super()._child_env()
            env[SCRUB_ENV] = "1"
            return env

        def _model(self, model: Optional[str]) -> str:
            return (model or getattr(self.settings, "agent_sdk_model", "")
                    or DEFAULT_MODEL)

        def _options(self, *, cwd: Path, read_only: bool, allowed: str, permission: str,
                     max_turns: int, model: str, resume: Optional[str], shared_ctx: str,
                     fan_out: int, effort: Optional[str]):
            from claude_agent_sdk import ClaudeAgentOptions

            system_prompt: dict[str, Any] = {"type": "preset", "preset": "claude_code",
                                             "exclude_dynamic_sections": True}
            if shared_ctx:
                system_prompt["append"] = shared_ctx
            # The agent allow-list + the key (_child_env). OwnedTransport passes exactly this,
            # never the worker's environment underneath (the SDK's own transport would).
            env = self._child_env()
            env.pop("CLAUDECODE", None)
            thinking = env.pop("MAX_THINKING_TOKENS", None)
            cli = str(getattr(self.settings, "agent_sdk_cli_path", "") or "").strip() or None
            return ClaudeAgentOptions(
                cwd=str(cwd),
                allowed_tools=[t.strip() for t in allowed.split(",") if t.strip()],
                disallowed_tools=([t for t in ca.SUB_AGENT_DENIED_TOOLS.split(",")]
                                  if fan_out else []),
                permission_mode=permission,
                max_turns=max_turns,
                model=model,
                resume=resume or None,
                system_prompt=system_prompt,
                env=env,
                cli_path=cli,
                effort=effort or None,
                max_thinking_tokens=int(thinking) if thinking else None,
            )

        def _invoke(
            self, prompt: str, cwd: Path, *, read_only: bool = False,
            model: Optional[str] = None, allowed_tools: Optional[str] = None,
            resume_session_id: Optional[str] = None, max_turns: Optional[int] = None,
            seat: str = "agent", on_turn: Optional[Callable[[dict], None]] = None,
            parallel_agents: int = 0,
        ) -> dict:
            if not sdk_available():
                raise RuntimeError(
                    f"The {SDK_PACKAGE} package is not installed on this server "
                    f"(pip install {SDK_PACKAGE}=={SDK_PIN}).")
            s = self.settings
            # THE SAME BUDGETS AS claude_code (ClaudeCodeAgent._invoke), so the engines are
            # interchangeable on every stage.
            timeout = int(s.code_agent_timeout_seconds or 900)
            if not read_only:
                timeout = max(timeout, 2400)
            else:
                timeout = min(timeout, int(getattr(s, "code_agent_review_timeout_seconds", 420)
                                           or 420))
            if allowed_tools is not None:
                allowed = allowed_tools
            else:
                allowed = "Read,Grep,Glob,Bash" if read_only else s.code_agent_allowed_tools
            fan_out = 0 if read_only else max(0, int(parallel_agents or 0))
            if fan_out:
                allowed = ca.sub_agent_tools(allowed)
            # Read-only seats: no Edit/Write in the allow-list and no auto-accept, so a reviewer
            # cannot change what it judges.
            permission = "default" if read_only else "acceptEdits"
            turns_cap = int(s.code_agent_max_turns or 40)
            if read_only:
                turns_cap = min(turns_cap, int(getattr(s, "code_agent_review_max_turns", 16)
                                               or 16))
            if max_turns:
                turns_cap = int(max_turns)
                timeout = max(timeout, min(3600, int(max_turns) * 30))
            timeout = ca._cap_to_deadline(timeout, read_only=read_only)
            model_name = self._model(model)
            shared_ctx, own_prompt = ca._shared_for(ca._argv_safe(prompt), resume_session_id)
            body = (own_prompt + ca._PARALLEL_TOOLS_RULE
                    + ("" if read_only else ca._WRITING_RULE + ca.sandbox_facts())
                    + (ca.sub_agent_rule(fan_out) if fan_out else ""))
            extra = tuple(self.mcp_harness.argv()) if self.mcp_harness is not None else ()
            capturing = capture_enabled()
            last_error = ""
            resume = resume_session_id
            for attempt in range(3):
                options = self._options(
                    cwd=cwd, read_only=read_only, allowed=allowed, permission=permission,
                    max_turns=turns_cap, model=model_name, resume=resume,
                    shared_ctx=shared_ctx, fan_out=fan_out, effort=ca._current_effort())
                parser = StreamParser()
                written = {"rows": 0}
                if capturing:
                    try:
                        ca.record_agent_turns([], engine_name=self.engine, model=model_name,
                                           prompt=prompt)
                    except Exception:  # noqa: BLE001
                        pass

                def on_event(event: dict, parser=parser, written=written) -> None:
                    produced = parser.feed(json.dumps(event, default=str))
                    if not produced or not capturing:
                        return
                    try:
                        written["rows"] += int(ca.record_agent_turns(
                            produced, engine_name=self.engine, model=model_name) or 0)
                    except Exception:  # noqa: BLE001
                        pass
                    for turn in produced:
                        try:
                            if on_turn:
                                on_turn(turn)
                        except Exception:  # noqa: BLE001
                            pass

                try:
                    result = run_session(
                        body, options, timeout=timeout, on_event=on_event,
                        transport_factory=lambda options=options: make_owned_transport(
                            body, options, extra_argv=extra, seat=seat))
                except SessionTimeout as exc:
                    raise RuntimeError(str(exc)) from exc
                except NoAnthropicKey:
                    raise
                except Exception as exc:  # noqa: BLE001
                    from ..identity.redaction import redact_text

                    detail = redact_text(str(exc))[:300]
                    last_error = f"claude agent sdk session failed: {detail}"
                    if resume and "no conversation found" in detail.lower():
                        # The session to resume is gone (another host, a cleaned home): BOSS's
                        # fallback — a fresh session; the repair prompt carries the evidence.
                        resume = None
                        continue
                    if _transient(detail) and attempt < 2:
                        time.sleep(5 * (attempt + 1))
                        continue
                    raise RuntimeError(last_error) from None
                if result is None:
                    raise RuntimeError("claude agent sdk session ended without a result")
                data = dict(result)
                data["_turns"] = list(parser.turns)
                if parser.rate_limit is not None:
                    data.setdefault("_rate_limit", parser.rate_limit)
                ca._mark_live(data, written)
                data["_billing"] = getattr(self, "_billing", "")
                if data.get("subtype") == "error_max_turns":
                    # Ran out of turns: a partial attempt the repair loop resumes, as with
                    # claude_code — spend recorded, not lost.
                    data["result"] = data.get("result") or (
                        "(ran out of turns before finishing — partial attempt; resume to "
                        "continue)")
                    return data
                if data.get("is_error"):
                    reason = str(data.get("result") or "; ".join(data.get("errors") or [])
                                 or "").strip()
                    status = data.get("api_error_status")
                    detail = " — ".join(p for p in (str(data.get("subtype") or ""),
                                                    f"HTTP {status}" if status else "",
                                                    reason[:220]) if p)
                    last_error = f"claude agent sdk session failed: {detail}"
                    if _transient(detail) and attempt < 2:
                        time.sleep(5 * (attempt + 1))
                        continue
                    raise RuntimeError(last_error)
                return data
            raise RuntimeError(last_error or "claude agent sdk session failed")

        @staticmethod
        def _usage(data: dict, settings: Any) -> tuple[dict, int]:
            usage, cost = ca.ClaudeCodeAgent._usage(data, settings)
            if not (data.get("modelUsage") or {}):
                usage["model"] = getattr(settings, "agent_sdk_model", "") or DEFAULT_MODEL
            # Who pays: always the operator's Anthropic API account (metered), and the source
            # it is booked against.
            from .included_spend import BILLING_METERED
            from .claude_credential import API_KEY_SOURCE_ID

            usage["billing"] = BILLING_METERED
            usage["source_id"] = API_KEY_SOURCE_ID
            return usage, cost

    return ClaudeAgentSdkAgent


_AGENT_CLASS = None


def agent_class():
    global _AGENT_CLASS
    if _AGENT_CLASS is None:
        _AGENT_CLASS = build_agent_class()
    return _AGENT_CLASS


def build_agent(settings: Any):
    """The harness builder (code_agent._register_builtin_harnesses)."""
    return agent_class()(settings)


# ── Chat: the same SDK, no tools ──────────────────────────────────────────────────────────────

def make_chat_adapter(settings: Any, *, model: Optional[str] = None):
    """The chat/generation backend for a project whose Chat job is pinned to this engine
    (model_routing.adapter_for): ClaudeCliAdapter's prompt rendering and JSON contract, each
    call a tool-less SDK session on the Anthropic API key."""
    from .agent_env import agent_environment
    from .claude_cli_adapter import ClaudeCliAdapter

    class AgentSdkChatAdapter(ClaudeCliAdapter):
        _session_default = "agent-sdk"
        _engine_name = ENGINE

        def _child_env(self) -> dict:
            env, cred = key_env(self.settings, agent_environment(os.environ))
            self._billing = cred.billing
            return env

        def _invoke_once(self, prompt: str, *, system_prompt: Optional[str] = None) -> dict:
            from claude_agent_sdk import ClaudeAgentOptions

            timeout, _own = self._timeouts()
            options = ClaudeAgentOptions(
                cwd=self._scratch_dir(), tools=[], strict_mcp_config=True,
                max_turns=2 if system_prompt is not None else None,
                model=(self.model_override or getattr(self.settings, "agent_sdk_model", "")
                       or DEFAULT_MODEL),
                system_prompt=(system_prompt if system_prompt is not None
                               else {"type": "preset", "preset": "claude_code"}),
                env=self._child_env(),
                cli_path=str(getattr(self.settings, "agent_sdk_cli_path", "") or "").strip()
                or None)
            result = run_session(
                prompt, options, timeout=float(timeout),
                transport_factory=lambda: make_owned_transport(prompt, options, seat="chat"))
            if result is None:
                raise RuntimeError("claude agent sdk session ended without a result")
            if system_prompt is not None:
                self._judge_generation(result)
            if result.get("is_error"):
                raise RuntimeError(
                    f"claude agent sdk error: {result.get('result') or result.get('subtype')}")
            return result

        def stream_text_response(self, prompt: str, system_prompt: Optional[str] = None,
                                 model: Optional[str] = None):
            """One piece (the whole reply), then the usage dict: the shape a talk reads."""
            try:
                data = self._invoke(prompt, system_prompt=system_prompt or self.GENERATION_SYSTEM)
            except Exception as exc:  # noqa: BLE001 — observed, then re-raised unchanged
                self._observe_capacity(ok=False, error=str(exc))
                raise
            self._observe_capacity(ok=True, error=None)
            text = str(data.get("result") or "")
            if text:
                yield text
            yield self._usage(data)

    return AgentSdkChatAdapter(settings, model=model)
