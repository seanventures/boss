"""A coding agent driven over the Agent Client Protocol (BOSSAPP-853).

`GenericCliCodeAgent` starts a CLI with the prompt and reads back whatever it printed. An ACP
agent offers much more, and this driver uses it: a SESSION BOSS opens and can continue, every
tool call as a STRUCTURED EVENT (kind, title, arguments, status) that lands in the transcript
under the engine contract's canonical names, and — the part that matters for governance — a
PERMISSION REQUEST before each sensitive call that BOSS, not the agent, answers.

Session lifecycle (acp_protocol has the wire):

    initialize      protocol version + capabilities both ways; a version BOSS does not speak
                    is refused before any work starts
    session/new     cwd = the sandbox tree, mcpServers = the agent's servers when the agent's
                    `mcpCapabilities.http` says it can take them (harness_delivery, ACP mode)
    session/load    instead of /new on a follow-up turn, when the agent offers `loadSession`
                    (the replayed history is not re-recorded)
    session/prompt  the stage's prompt; the answer's stopReason ends the turn
    session/cancel  on the stage's deadline, and when a call is held for approval

Permission requests take THE EXISTING MID-RUN APPROVAL PATH, in this order:

    1. a read-only session refuses every write (Write/Edit/MultiEdit/NotebookEdit);
    2. tool_gateway.verdict over the agent's `tool_policy` (the native loop's check):
       run -> allow once; deny -> reject once;
    3. wait -> with the MCP gateway on (BOSSAPP-814) and a run token, a decision is raised and
       the call PARKS until a person answers it in Approvals (mcp_gateway.raise_decision/park),
       approved -> allow once, anything else -> reject once;
       with it off (the pre-814 behaviour), the call is rejected, the session is cancelled and
       the turn ends `awaiting_approval` with the pending tool and its argument digest — exactly
       what the native loop returns for an Ask tool.

BOSS never answers `allow_always`: each call is decided on its own.

`fs/read_text_file` and `fs/write_text_file` are served inside the session's cwd only (a path
outside it is refused), and writes are not offered to a read-only session. No terminal is
offered: the agent runs its own commands inside BOSS's sandbox.
"""
from __future__ import annotations

import logging
import os
import queue
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from . import acp_protocol as acp
from . import owned_process
from .cli_harness import CliHarnessUnavailable, GenericCliCodeAgent, _proxy_scoped
from .engine_contract import WRITE_TOOLS, EngineCapabilities
from .harness_delivery import current as current_kit, delivers_kit
from .workspace_perms import tidies_workspace

_log = logging.getLogger(__name__)

#: Seconds to wait for the agent to answer `session/cancel` before it is killed.
CANCEL_GRACE_SECONDS = 10.0


class _Timeout(Exception):
    pass


class AcpConnection:
    """JSON-RPC over a child's stdio. `call` sends a request and serves every message the agent
    sends meanwhile (its notifications and its own requests) until the answer arrives."""

    def __init__(self, child: Any, handler: Callable[[dict], Optional[dict]], *,
                 deadline: float) -> None:
        self.child = child
        self.handler = handler
        self.deadline = deadline
        self._next = 0
        self._lines: "queue.Queue[Optional[str]]" = queue.Queue()
        self._write_lock = threading.Lock()
        self.stderr_tail: list[str] = []
        threading.Thread(target=self._pump, args=(child.stdout, self._lines.put), daemon=True,
                         name="acp-stdout").start()
        if getattr(child, "stderr", None) is not None:
            threading.Thread(target=self._pump_stderr, daemon=True, name="acp-stderr").start()

    @staticmethod
    def _pump(stream: Any, put: Callable[[Optional[str]], None]) -> None:
        try:
            for line in iter(stream.readline, ""):
                put(line)
        except (OSError, ValueError):
            pass
        finally:
            put(None)

    def _pump_stderr(self) -> None:
        try:
            for line in iter(self.child.stderr.readline, ""):
                self.stderr_tail.append(line)
                del self.stderr_tail[:-200]
        except (OSError, ValueError):
            pass

    def send(self, message: dict) -> None:
        with self._write_lock:
            try:
                self.child.stdin.write(acp.encode(message))
                self.child.stdin.flush()
            except (OSError, ValueError) as exc:
                raise acp.AcpProtocolError(f"the agent's stdin closed: {exc}") from None

    def notify(self, method: str, params: dict) -> None:
        self.send(acp.notification(method, params))

    def _next_message(self, deadline: float) -> dict:
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise _Timeout()
            try:
                line = self._lines.get(timeout=min(left, 1.0))
            except queue.Empty:
                continue
            if line is None:
                raise acp.AcpProtocolError("the agent closed its output before answering")
            message = acp.decode(line)
            if message is not None:
                return message

    def call(self, method: str, params: dict, *, deadline: Optional[float] = None) -> Any:
        rid = self._next
        self._next += 1
        self.send(acp.request(rid, method, params))
        until = self.deadline if deadline is None else deadline
        while True:
            message = self._next_message(until)
            if "method" not in message and message.get("id") == rid:
                if "error" in message:
                    err = message.get("error") or {}
                    raise acp.AcpProtocolError(f"{method}: {err.get('message') or err}",
                                               code=err.get("code"))
                return message.get("result")
            self.serve(message)

    def serve(self, message: dict) -> None:
        if "method" not in message:
            return  # a response to nothing we are waiting on
        try:
            answer = self.handler(message)
        except Exception as exc:  # noqa: BLE001 — a handler bug answers the agent, never hangs it
            _log.warning("acp: handling %s failed: %s", message.get("method"), exc)
            answer = acp.error(message.get("id"), acp.INTERNAL_ERROR, str(exc)[:300])
        if "id" in message and answer is not None:
            self.send(answer)


def decide_permission(agent: Any, name: str, args: dict, *, read_only: bool) -> dict:
    """{allow, reason, held, decision_id} for one permission request (see the module doc)."""
    if read_only and name in WRITE_TOOLS:
        return {"allow": False, "reason": "read_only", "held": False, "decision_id": None}
    from .tool_gateway import DENY, WAIT, verdict

    word = verdict(name, getattr(agent, "tool_policy", None), args)
    if word == DENY:
        return {"allow": False, "reason": "off", "held": False, "decision_id": None}
    if word != WAIT:
        return {"allow": True, "reason": "allow", "held": False, "decision_id": None}
    token = getattr(getattr(agent, "mcp_harness", None), "gateway_token", "") or ""
    from . import mcp_gateway

    if token and mcp_gateway.enabled():
        gateway = mcp_gateway.get()
        grant = gateway.grant_for(token)
        if grant is not None and grant.live():
            decision_id = gateway.raise_decision(
                grant, kind=mcp_gateway.ACTION_REQUEST_APPROVAL, tool=name, arguments=args)
            answer = gateway.park(grant, decision_id)
            approved = answer.get("status") == "approved"
            return {"allow": approved, "reason": "approved" if approved else
                    str(answer.get("status") or "rejected"), "held": False,
                    "decision_id": decision_id}
    # Pre-814: an unattended session cannot wait — the turn ends awaiting approval.
    return {"allow": False, "reason": "ask", "held": True, "decision_id": None}


def child_wait(child: Any, seconds: float) -> None:
    try:
        child.wait(timeout=seconds)
    except Exception:  # noqa: BLE001 — still running: the caller terminates it
        pass


def _inside(root: Path, path: str) -> Path:
    real_root = Path(os.path.realpath(root))
    target = Path(path)
    if not target.is_absolute():
        target = real_root / target
    resolved = Path(os.path.realpath(target))
    if resolved != real_root and real_root not in resolved.parents:
        raise PermissionError(f"{path} is outside the session's working tree")
    return resolved


class AcpCodeAgent(GenericCliCodeAgent):
    """Drive an ACP agent (`spec.protocol == "acp"`) as a BOSS engine."""

    @property
    def capabilities(self) -> EngineCapabilities:
        # No read-only mode BOSS can trust (an agent need not ask before every write), so the
        # contract runs review seats on a throwaway copy; sessions are offered per agent
        # (loadSession) and continued when they are, seeded by BOSS otherwise.
        return EngineCapabilities(streams_turns=True)

    def _acp_argv(self, model: Optional[str]) -> list[str]:
        argv = [self._resolve_binary(), *self.spec.args]
        if model and self.spec.model_flag:
            from .model_router.compat import fits

            if fits(self.spec.name, model):
                argv += [self.spec.model_flag, model]
            else:
                _log.warning("%s cannot run model %s; using its own default", self.spec.name,
                             model)
        return argv

    @tidies_workspace
    @_proxy_scoped
    @delivers_kit
    def run_role(self, prompt: str, cwd: Path, *, read_only: bool = False,
                 model: Optional[str] = None, resume_session_id: Optional[str] = None,
                 on_turn: Optional[Callable[[dict], None]] = None, **_ignored: Any) -> dict:
        from .code_agent import _cap_to_deadline, register_seat, release_seat
        from .llm_telemetry import record_agent_turns, record_llm_call

        self._preflight(read_only=read_only,
                        read_only_by_copy=bool(_ignored.get("_contract_read_only_copy")))
        timeout = _cap_to_deadline(
            int(self.spec.review_timeout if read_only else self.spec.timeout), read_only=read_only)
        cwd = Path(cwd)
        argv = self._acp_argv(model)
        env = self._env()
        started = time.monotonic()
        state: dict[str, Any] = {"summary": [], "tool_calls": 0, "turns": [], "held": None,
                                 "replaying": False, "session_id": None, "permissions": []}
        calls: dict[str, acp.ToolCallState] = {}

        def _turn(row: dict) -> None:
            if state["replaying"]:
                return
            state["turns"].append(row)
            if on_turn:
                try:
                    on_turn(row)
                except Exception:  # noqa: BLE001
                    pass

        def _update(update: dict) -> None:
            kind = update.get("sessionUpdate")
            if kind == "agent_message_chunk":
                if not state["replaying"]:
                    state["summary"].append(acp.text_of(update.get("content")))
            elif kind == "tool_call":
                call_id = str(update.get("toolCallId") or len(calls))
                tc = acp.ToolCallState(call_id, acp.canonical_tool(update),
                                       str(update.get("title") or ""), acp.tool_input(update),
                                       str(update.get("status") or "pending"))
                calls[call_id] = tc
                if not state["replaying"]:
                    state["tool_calls"] += 1
                _turn({"direction": "tool_call", "tool_name": tc.name, "content": tc.args,
                       "tool_call_id": call_id})
                if tc.status in ("completed", "failed"):
                    _turn({"direction": "tool_result", "tool_name": tc.name,
                           "content": acp.text_of(update.get("content"))[:4000],
                           "ok": tc.status == "completed", "tool_call_id": call_id})
            elif kind == "tool_call_update":
                call_id = str(update.get("toolCallId") or "")
                tc = calls.get(call_id)
                if tc is None:
                    tc = calls[call_id] = acp.ToolCallState(call_id, acp.canonical_tool(update))
                if update.get("kind") or update.get("title"):
                    tc.name = acp.canonical_tool({**{"title": tc.title}, **update})
                if isinstance(update.get("rawInput"), dict):
                    tc.args = acp.tool_input({"kind": update.get("kind"), **update})
                status = str(update.get("status") or "")
                if status in ("completed", "failed") and tc.status not in ("completed",
                                                                           "failed"):
                    _turn({"direction": "tool_result", "tool_name": tc.name,
                           "content": acp.text_of(update.get("content"))[:4000],
                           "ok": status == "completed", "tool_call_id": call_id})
                if status:
                    tc.status = status

        def _handle(message: dict) -> Optional[dict]:
            method = message.get("method")
            params = message.get("params") or {}
            rid = message.get("id")
            if method == "session/update":
                _update(params.get("update") or {})
                return None
            if method == "session/request_permission":
                raw = params.get("toolCall") or {}
                known = calls.get(str(raw.get("toolCallId") or ""))
                # The request may carry the whole call, or only its id and what changed.
                name = (acp.canonical_tool(raw) if (raw.get("kind") or raw.get("title"))
                        else (known.name if known else acp.canonical_tool(raw)))
                args = (acp.tool_input(raw) if (raw.get("rawInput") or raw.get("locations"))
                        else (known.args if known else {}))
                decided = decide_permission(self, name, args, read_only=read_only)
                state["permissions"].append({"tool": name, **decided})
                option = acp.pick_option(params.get("options") or [], decided["allow"])
                if decided["held"] and state["held"] is None:
                    state["held"] = {"tool": name, "args": args}
                    # Nothing in this turn runs after an Ask: the session is cancelled.
                    threading.Thread(target=lambda: _cancel(), daemon=True).start()
                return acp.result(rid, acp.permission_outcome(option))
            if method == "fs/read_text_file":
                try:
                    path = _inside(cwd, str(params.get("path") or ""))
                    text = path.read_text(encoding="utf-8", errors="replace")
                except (OSError, PermissionError) as exc:
                    return acp.error(rid, acp.INVALID_PARAMS, str(exc)[:300])
                line = params.get("line")
                limit = params.get("limit")
                if line or limit:
                    lines = text.splitlines(keepends=True)
                    start = max(int(line or 1) - 1, 0)
                    text = "".join(lines[start:start + int(limit)] if limit else lines[start:])
                return acp.result(rid, {"content": text})
            if method == "fs/write_text_file":
                if read_only:
                    return acp.error(rid, acp.INVALID_REQUEST,
                                     "this is a read-only session; writes are refused")
                try:
                    path = _inside(cwd, str(params.get("path") or ""))
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(str(params.get("content") or ""), encoding="utf-8")
                except (OSError, PermissionError) as exc:
                    return acp.error(rid, acp.INVALID_PARAMS, str(exc)[:300])
                return acp.result(rid, None)
            if rid is not None:
                return acp.error(rid, acp.METHOD_NOT_FOUND, f"BOSS does not serve {method}")
            return None

        from .scheduling import low_priority

        child = owned_process.popen(low_priority(argv), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True, bufsize=1, cwd=str(cwd),
                                    env=env)
        register_seat(child.pid, "review" if read_only else "agent")
        conn = AcpConnection(child, _handle, deadline=started + timeout)

        def _cancel() -> None:
            if state["session_id"]:
                try:
                    conn.notify("session/cancel", {"sessionId": state["session_id"]})
                except acp.AcpProtocolError:
                    pass

        stop = "error"
        error: Optional[str] = None
        timed_out = False
        kit = current_kit()
        try:
            info = acp.AgentInfo.from_result(
                conn.call("initialize", acp.initialize_params(read_only=read_only)) or {})
            if info.protocol_version != acp.PROTOCOL_VERSION:
                raise CliHarnessUnavailable(
                    f"{self.spec.name} speaks ACP version {info.protocol_version}; BOSS speaks "
                    f"{acp.PROTOCOL_VERSION}. Upgrade the CLI or BOSS.")
            servers = kit.acp_servers_for(http=info.mcp_http) if kit is not None else []
            session_id = None
            if resume_session_id and info.load_session:
                state["replaying"] = True
                try:
                    conn.call("session/load", acp.load_session_params(
                        resume_session_id, str(cwd), servers))
                    session_id = resume_session_id
                except acp.AcpProtocolError:
                    _log.info("%s could not load session %s; starting a new one",
                              self.spec.name, resume_session_id)
                finally:
                    state["replaying"] = False
            if session_id is None:
                if resume_session_id:
                    from .engine_contract import LEDGER, seeded_prompt

                    prompt = seeded_prompt(prompt, LEDGER.get(resume_session_id))
                opened = conn.call("session/new", acp.new_session_params(str(cwd), servers)) or {}
                session_id = str(opened.get("sessionId") or "")
                if not session_id:
                    raise acp.AcpProtocolError("session/new answered without a sessionId")
            state["session_id"] = session_id
            answered = conn.call("session/prompt", acp.prompt_params(session_id, prompt)) or {}
            stop = acp.STOP_REASON_MAP.get(str(answered.get("stopReason") or ""), "error")
        except _Timeout:
            timed_out = True
            _cancel()
            # A short grace for the agent to wind down on its own before it is terminated.
            child_wait(child, CANCEL_GRACE_SECONDS)
        except acp.AcpProtocolError as exc:
            if exc.code == acp.AUTH_REQUIRED:
                raise CliHarnessUnavailable(
                    f"{self.spec.name} is not signed in (ACP auth_required). "
                    f"{self.spec.signin_hint}") from None
            error = str(exc)
        finally:
            release_seat(child.pid)
            try:
                child.stdin.close()
            except (OSError, ValueError):
                pass
            try:
                child.terminate()
                child.wait(timeout=5)
            except Exception:  # noqa: BLE001
                try:
                    child.kill()
                except Exception:  # noqa: BLE001
                    pass
        ms = int((time.monotonic() - started) * 1000)
        if state["held"] is not None:
            stop = "awaiting_approval"
        summary = "".join(state["summary"]).strip()
        ok = stop in ("done", "turn_cap", "awaiting_approval") and not timed_out
        record_llm_call(engine_name=self.engine, usage={"model": model or self.spec.name},
                        cost_usd_micros=0, latency_ms=ms,
                        turns=max(1, state["tool_calls"]), ok=ok,
                        error=None if ok else (error or ("timed out" if timed_out else stop)))
        try:
            record_agent_turns(state["turns"] + [{"direction": "completion",
                                                  "content": summary[:8000], "ok": ok}],
                               engine_name=self.engine, model=model or "", prompt=prompt)
        except Exception:  # noqa: BLE001 — a transcript never fails a build
            pass
        if timed_out:
            raise CliHarnessUnavailable(f"{self.spec.name} did not finish within {timeout}s")
        stderr = "".join(conn.stderr_tail)
        self._raise_harness_verdict(stdout=summary, stderr=stderr,
                                    summary=summary or (error or ""), ok=ok)
        if error and not summary:
            raise CliHarnessUnavailable(f"{self.spec.name}: {error[:300]}")
        out: dict[str, Any] = {
            "summary": summary[:8000] or f"({self.spec.name} produced no output)",
            "usage": {"model": model or self.spec.name},
            "cost_usd_micros": 0,
            "cost_unreported": True,
            "turns": max(1, state["tool_calls"]),
            "tool_calls": state["tool_calls"],
            "model": model or "",
            "session_id": state["session_id"],
            "stop_reason": stop,
            "permissions": state["permissions"],
        }
        if kit is not None:
            out["kit"] = kit.as_dict()
        if stop == "awaiting_approval":
            from .argument_binding import args_sha256, redacted_preview

            held = state["held"]
            out.update({"awaiting_approval": True, "pending_tool": held["tool"],
                        "pending_args_sha256": args_sha256(held["tool"], held["args"]),
                        "pending_args_preview": redacted_preview(held["args"])})
        return out
