"""CodexCodeAgent — the Codex CLI as a BUILD engine, held to the engine contract.

`GenericCliCodeAgent` drove `codex exec` as a black box: one prompt in, the final message out,
`turns=1`, no session, no transcript, no tokens. Every BOSS behaviour that reads more than that
(the warm fix round, the implement continuation, the test-budget trim, the install loop, the
build profile, the live transcript, the ledger) was either skipped or empty on Codex. This adapter
gives BOSS the same things it reads from Claude Code, from Codex's own interfaces:

  * SESSIONS. `codex exec --json` announces the session (`thread.started` -> `thread_id`) and
    `codex exec resume <id>` continues it, so a warm fix round on Codex is warm, as on Claude.
  * THE TRANSCRIPT. Every `item.completed` event becomes a turn in BOSS's common schema with
    BOSS's tool names (engine_contract): `command_execution` -> Bash call + result,
    `file_change` -> Write (add) / Edit (update, delete), `agent_message` -> completion,
    `web_search` -> WebSearch, `mcp_tool_call` -> the tool. Written AS IT HAPPENS, like Claude's.
  * USAGE. `turn.completed` carries input, cached-input, output and reasoning tokens; the ledger
    gets them in BOSS's split, stamped `billing: subscription` on a ChatGPT login (an API-key
    login is metered: usage_is_included reads the stamp, never the engine's name).
  * EFFORT. BOSS's per-stage effort maps onto Codex's `model_reasoning_effort`.
  * READ-ONLY. A reviewer runs with `--sandbox read-only`; a writer with `danger-full-access`
    inside BOSS's own sandbox (see the spec in cli_harness for why).
  * THE DEADLINE. The stage's wall-time deadline bounds the process, as it bounds Claude's.

The prompt goes on stdin (`-`), which survives a prompt larger than one argv entry may be.
The event shapes follow `codex exec --json` (codex-cli 0.154): unknown events are ignored, never
fatal, and a stream that cannot be read still yields the final message from
`--output-last-message`, so a CLI version that changes its events loses the transcript, not the
build.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Optional

from . import owned_process
from .cli_harness import CliHarnessUnavailable, GenericCliCodeAgent
from .engine_contract import EngineCapabilities
from .workspace_perms import tidies_workspace

_log = logging.getLogger(__name__)

#: BOSS effort level -> Codex `model_reasoning_effort` (low | medium | high | xhigh).
EFFORT_TO_CODEX = {"low": "low", "medium": "medium", "high": "high", "xhigh": "xhigh",
                   "max": "xhigh"}


class CodexStreamParser:
    """`codex exec --json` events, one line at a time, into BOSS's common turns."""

    def __init__(self) -> None:
        self.thread_id: Optional[str] = None
        self.usage: dict[str, Any] = {}
        self.final_message = ""
        self.error = ""
        self.turns: list[dict[str, Any]] = []
        self.tool_calls = 0
        self.model_turns = 0
        self._index = 0

    def _add(self, row: dict[str, Any]) -> dict[str, Any]:
        row["turn_index"] = self._index
        self._index += 1
        self.turns.append(row)
        return row

    def feed(self, line: str) -> list[dict[str, Any]]:
        produced: list[dict[str, Any]] = []
        line = (line or "").strip()
        if not line.startswith("{"):
            return produced
        try:
            event = json.loads(line)
        except ValueError:
            return produced
        if not isinstance(event, dict):
            return produced
        kind = str(event.get("type") or "")
        if kind in ("thread.started", "session.created", "session_configured"):
            self.thread_id = str(event.get("thread_id") or event.get("session_id") or "") or None
        elif kind == "turn.started":
            self.model_turns += 1
        elif kind == "turn.completed":
            if isinstance(event.get("usage"), dict):
                # One turn per `codex exec`; a resumed session reports its own turn. Summed, in
                # case a CLI version reports more than one.
                for k, v in event["usage"].items():
                    if isinstance(v, (int, float)):
                        self.usage[k] = int(self.usage.get(k) or 0) + int(v)
        elif kind in ("turn.failed", "error"):
            err = event.get("error")
            self.error = str((err or {}).get("message") if isinstance(err, dict)
                             else (err or event.get("message") or ""))[:500]
        elif kind == "item.completed":
            produced.extend(self._item(event.get("item") or {}))
        return produced

    def _item(self, item: dict[str, Any]) -> list[dict[str, Any]]:
        if not isinstance(item, dict):
            return []
        itype = str(item.get("type") or item.get("item_type") or "")
        out: list[dict[str, Any]] = []
        if itype in ("agent_message", "assistant_message"):
            text = str(item.get("text") or "")
            if text.strip():
                self.final_message = text
                out.append(self._add({"direction": "completion", "content": text}))
        elif itype == "command_execution":
            self.tool_calls += 1
            out.append(self._add({"direction": "tool_call", "tool_name": "Bash",
                                  "content": {"command": str(item.get("command") or "")}}))
            code = item.get("exit_code")
            out.append(self._add({
                "direction": "tool_result", "tool_name": "Bash",
                "content": str(item.get("aggregated_output") or item.get("output") or ""),
                "ok": (code in (0, None)) and str(item.get("status") or "") != "failed"}))
        elif itype == "file_change":
            for change in item.get("changes") or []:
                if not isinstance(change, dict):
                    continue
                self.tool_calls += 1
                tool = "Write" if str(change.get("kind") or "") == "add" else "Edit"
                out.append(self._add({"direction": "tool_call", "tool_name": tool,
                                      "content": {"file_path": str(change.get("path") or ""),
                                                  "kind": str(change.get("kind") or "")}}))
                out.append(self._add({"direction": "tool_result", "tool_name": tool,
                                      "content": str(change.get("kind") or "changed"),
                                      "ok": str(item.get("status") or "") != "failed"}))
        elif itype == "web_search":
            self.tool_calls += 1
            out.append(self._add({"direction": "tool_call", "tool_name": "WebSearch",
                                  "content": {"query": str(item.get("query") or "")}}))
        elif itype == "mcp_tool_call":
            self.tool_calls += 1
            name = f"mcp__{item.get('server') or '?'}__{item.get('tool') or '?'}"
            out.append(self._add({"direction": "tool_call", "tool_name": name,
                                  "content": item.get("arguments") or {}}))
            out.append(self._add({"direction": "tool_result", "tool_name": name,
                                  "content": str(item.get("result") or item.get("error") or ""),
                                  "ok": str(item.get("status") or "") != "failed"}))
        return out


class CodexCodeAgent(GenericCliCodeAgent):
    """`codex exec` with sessions, a transcript, usage and effort (see the module docstring)."""

    capabilities = EngineCapabilities(resume=True, read_only=True, effort=True,
                                      reports_cost=True, streams_turns=True)

    def _usage(self, parser: CodexStreamParser, model: str) -> tuple[dict[str, Any], int]:
        """Codex's numbers in BOSS's split, as the Codex chat adapter maps them."""
        from .included_spend import BILLING_METERED, BILLING_SUBSCRIPTION
        from .openai_adapter import estimate_cost_usd_micros

        u = parser.usage
        cache_read = int(u.get("cached_input_tokens") or 0)
        cache_write = int(u.get("cache_write_input_tokens") or 0)
        prompt = max(int(u.get("input_tokens") or 0) - cache_read, 0)
        completion = int(u.get("output_tokens") or 0) + int(u.get("reasoning_output_tokens") or 0)
        usage = {
            "model": model or (getattr(self.settings, "codex_cli_model", "") or "") or "codex-cli",
            "prompt_tokens": prompt, "completion_tokens": completion,
            "cache_read_tokens": cache_read, "cache_write_tokens": cache_write,
            "total_tokens": prompt + cache_read + cache_write + completion,
            # A ChatGPT login is a subscription; an API key in the child's environment is money.
            "billing": (BILLING_METERED if self._env().get("OPENAI_API_KEY")
                        else BILLING_SUBSCRIPTION),
        }
        # The list-price value of those tokens, as Claude's CLI reports its own; whether it is
        # money is the `billing` stamp's call (included_spend), never this number's.
        cost = int(estimate_cost_usd_micros({k: v for k, v in usage.items() if k != "billing"}))
        usage["cost_usd_micros"] = cost
        return usage, cost

    def _codex_argv(self, *, read_only: bool, model: Optional[str], resume: Optional[str],
                    final_path: str) -> list[str]:
        from .code_agent import _current_effort

        binary = self._resolve_binary()
        common = ["--skip-git-repo-check", "--json", "--output-last-message", final_path]
        if model:
            common += ["--model", model]
        effort = EFFORT_TO_CODEX.get(str(_current_effort() or "").lower())
        if effort:
            common += ["-c", f'model_reasoning_effort="{effort}"']
        mode = "read-only" if read_only else "danger-full-access"
        if resume:
            # `exec resume` takes no --sandbox flag; the same policy goes in as config.
            return [binary, "exec", "resume", *common, "-c", f'sandbox_mode="{mode}"',
                    resume, "-"]
        return [binary, "exec", "--color", "never", "--sandbox", mode, *common, "-"]

    @tidies_workspace
    def run_role(self, prompt: str, cwd: Path, *, read_only: bool = False,
                 model: Optional[str] = None, resume_session_id: Optional[str] = None,
                 max_turns: Optional[int] = None, parallel_agents: int = 0,
                 on_turn: Optional[Callable[[dict], None]] = None, **_ignored: Any) -> dict:
        from .code_agent import _cap_to_deadline, _drain, register_seat, release_seat
        from .llm_telemetry import record_agent_turns, record_llm_call
        from .scheduling import low_priority

        if (self.spec.token_env or self.spec.auth_file_secret) and not getattr(
                self, "_auth_checked", False):
            self._auth_checked = True
            self._auth_state = self.ensure_authorised()
        timeout = _cap_to_deadline(
            int(self.spec.review_timeout if read_only else self.spec.timeout), read_only=read_only)
        fd, final_path = tempfile.mkstemp(prefix="boss-codex-final-", suffix=".txt")
        os.close(fd)
        resume = None if read_only else resume_session_id
        started = time.monotonic()
        parser = CodexStreamParser()
        written = {"rows": 0}
        try:
            record_agent_turns([], engine_name=self.engine, model=model or "", prompt=prompt)
        except Exception:  # noqa: BLE001 — a transcript never fails a build
            pass

        def _on_line(line: str) -> None:
            produced = parser.feed(line)
            if not produced:
                return
            try:
                written["rows"] += int(record_agent_turns(produced, engine_name=self.engine,
                                                          model=model or "") or 0)
            except Exception:  # noqa: BLE001
                pass
            for turn in produced:
                try:
                    if on_turn:
                        on_turn(turn)
                except Exception:  # noqa: BLE001
                    pass

        def _attempt(resume_id: Optional[str]) -> subprocess.CompletedProcess:
            argv = self._codex_argv(read_only=read_only, model=model, resume=resume_id,
                                    final_path=final_path)
            child = owned_process.popen(
                low_priority(argv), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, cwd=str(cwd), env=self._env())
            register_seat(child.pid, "review" if read_only else "agent")
            try:
                if child.stdin is not None:
                    try:
                        child.stdin.write(prompt)
                    finally:
                        child.stdin.close()
                out, err = _drain(child, timeout=timeout, on_line=_on_line)
            finally:
                release_seat(child.pid)
            return subprocess.CompletedProcess(argv, child.returncode, out, err)

        try:
            proc = _attempt(resume)
            if proc.returncode != 0 and resume and not parser.final_message:
                # A session Codex no longer has (another machine, a cleaned home) is continued
                # as a new one: BOSS never fails a fix round on a lost session.
                _log.info("codex could not resume %s; starting a new session", resume)
                resume = None
                proc = _attempt(None)
            if proc.returncode != 0 and not parser.final_message:
                from .cli_harness import _looks_like_revoked_credential

                if _looks_like_revoked_credential(proc.stdout, proc.stderr):
                    restored = self._authorise_from_file(cli_reports_signed_in=False)
                    if restored.get("authorised"):
                        proc = _attempt(resume)
        except subprocess.TimeoutExpired:
            record_llm_call(engine_name=self.engine, usage={"model": model or ""},
                            latency_ms=int((time.monotonic() - started) * 1000),
                            ok=False, error=f"timed out after {timeout}s")
            _unlink(final_path)
            raise CliHarnessUnavailable(f"codex did not finish within {timeout}s") from None
        except FileNotFoundError as exc:
            _unlink(final_path)
            raise CliHarnessUnavailable(f"codex is not installed: {exc}") from None
        summary = ""
        try:
            summary = Path(final_path).read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            pass
        _unlink(final_path)
        summary = summary or parser.final_message.strip()
        ok = proc.returncode == 0
        if ok:
            self.remember_rotated_credential()
        if written["rows"] == 0 and parser.turns:
            try:
                record_agent_turns(parser.turns, engine_name=self.engine, model=model or "")
            except Exception:  # noqa: BLE001
                pass
        usage, cost = self._usage(parser, model or "")
        ms = int((time.monotonic() - started) * 1000)
        record_llm_call(engine_name=self.engine, usage=usage, cost_usd_micros=cost,
                        latency_ms=ms, turns=max(1, parser.model_turns), ok=ok,
                        error=None if ok else (parser.error or (proc.stderr or ""))[:400])
        if not ok:
            # The CLI's stderr can quote a credential back on an auth failure: only the status
            # and the event stream's own error travel.
            detail = parser.error or (proc.stdout or "")[-300:]
            raise CliHarnessUnavailable(f"codex exited {proc.returncode}: {detail[:300]}")
        return {
            "summary": summary[:8000] or "(codex produced no summary)",
            "usage": usage,
            "cost_usd_micros": cost,
            "cost_unreported": False,
            "turns": max(1, parser.model_turns),
            "tool_calls": parser.tool_calls,
            "model": model or "",
            "session_id": parser.thread_id or resume,
            "stop_reason": "done",
            "exit_code": proc.returncode,
        }


def _unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass
