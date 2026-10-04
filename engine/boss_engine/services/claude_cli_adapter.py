"""ClaudeCliAdapter — use the Claude Code CLI (`claude -p`) as a raw completion
backend so BOSS can drive its OWN governed tool-calling loop while the actual
inference runs on a Claude Max subscription instead of a paid API.

`claude -p` does not expose native function-calling for *our* tool schemas (it
only tool-uses Claude Code's own tools), so we emulate it: render the
conversation + our tool schemas into a prompt, instruct Claude to reply with a
single JSON object describing the next tool call (or a final answer), shell out
to `claude -p --output-format json`, and parse the JSON back into the exact
`(message, usage)` shape the OpenAI adapter returns. The rest of BOSS's loop is
unchanged.

Note: a consumer Max subscription is licensed for interactive use; running it as
an automated backend is a prototype/dogfooding convenience, not a production
inference path.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import threading
from pathlib import Path
from typing import Any, Iterator, Optional, Tuple

from ..config import Settings
from .scheduling import low_priority

_log = logging.getLogger(__name__)


def _strip_fences(text: str) -> str:
    t = (text or "").strip()
    # remove ```json ... ``` or ``` ... ``` wrappers
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z0-9]*\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
    return t.strip()


def _first_json_object(text: str) -> Optional[dict]:
    """Best-effort extraction of the first balanced {...} JSON object."""
    s = _strip_fences(text)
    try:
        return json.loads(s)
    except Exception:
        pass
    start = s.find("{")
    while start != -1:
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(s)):
            c = s[i]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
            else:
                if c == '"':
                    in_str = True
                elif c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        candidate = s[start : i + 1]
                        try:
                            return json.loads(candidate)
                        except Exception:
                            break
        start = s.find("{", start + 1)
    return None


class CliChatBrain:
    """The half of a CLI chat backend that has nothing to do with WHICH CLI it is.

    Rendering a conversation and our tool schemas into a prompt, insisting on one JSON object
    back, and reshaping that into the `(message, usage)` pair the OpenAI adapter returns is
    identical for every coding CLI. Three things differ: the binary, its argv, and where it
    reports token usage.

    Splitting it here is what makes a second subscription a subclass instead of a fork. The
    prompt especially is load-bearing and hard-won — it carries the fix for a model that
    reached for its own toolbelt instead of emitting JSON, a bug that only appeared once the
    registry crossed ~53 tools — and a copied prompt would drift from this one silently, in
    the direction of that bug.

    A subclass implements `_invoke` (prompt in, a dict carrying "result" out) and `_usage`.
    """

    #: What `create_text_response` calls the session when the CLI does not name one.
    _session_default = "cli"

    #: The harness name this backend spends against — the key connected_engines and the role
    #: knobs use. Declared so capacity observations land on the right entitlement source
    #: without a second map from class to engine somewhere else.
    _engine_name = ""

    def _invoke(self, prompt: str) -> dict:  # pragma: no cover - subclass responsibility
        raise NotImplementedError

    def _child_env(self) -> dict:
        """The CLI's environment, with the credential this install actually holds.

        Nothing was passed here, so the child inherited the API container's environment — and
        on an install whose token lives in a secret store that is empty. `claude -p` answered
        "Not logged in", the falling-back adapter read that as "this backend is not available
        here" and handed the operator the SECOND backend's error:

            IntegrationConfigurationError: OPENAI_API_KEY is not configured

        about a subscription that was connected, proved with a real turn, and stored. The
        coding harness had always resolved the stored credential; the chat never did, so the
        one interface a customer talks to was the one that could not reach the model.
        """
        from .agent_env import agent_environment
        from .cli_harness import stored_credential

        token = str(getattr(self.settings, "claude_code_oauth_token", "") or "").strip()
        if not token and not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
            token = stored_credential(self.settings, "CLAUDE_CODE_OAUTH_TOKEN") or ""
        extra = {"CLAUDE_CODE_OAUTH_TOKEN": token} if token else {}
        env = agent_environment(os.environ, extra)
        if token:
            env.pop("ANTHROPIC_API_KEY", None)
        return env

    def _usage(self, data: dict) -> dict:  # pragma: no cover - subclass responsibility
        raise NotImplementedError

    def __init__(self, settings: Settings, *, model: Optional[str] = None):
        self.settings = settings
        #: Per-consumer model override. The chat agent runs on Opus while the coding pipeline
        #: keeps its own choice — one setting for both would make raising the chat's brain a
        #: decision about every build's cost.
        self.model_override = model or None

    # ---------------------------------------------------------------- prompt
    def _render_tools(self, tools: list[dict]) -> str:
        lines = ["AVAILABLE TOOLS:"]
        for t in tools:
            fn = t.get("function") or {}
            name = fn.get("name")
            desc = fn.get("description", "")
            params = fn.get("parameters") or {"type": "object", "properties": {}}
            lines.append(f"- {name}: {desc}")
            lines.append(f"  schema: {json.dumps(params)}")
        return "\n".join(lines)

    def _render_conversation(self, messages: list[dict]) -> str:
        id_to_name: dict[str, str] = {}
        out: list[str] = []
        for m in messages:
            role = m.get("role")
            if role == "system":
                out.append(f"[SYSTEM INSTRUCTIONS]\n{m.get('content') or ''}")
            elif role == "user":
                out.append(f"[USER]\n{m.get('content') or ''}")
            elif role == "assistant":
                txt = m.get("content") or ""
                calls = m.get("tool_calls") or []
                rendered_calls = []
                for tc in calls:
                    fn = tc.get("function") or {}
                    nm = fn.get("name")
                    if tc.get("id"):
                        id_to_name[tc["id"]] = nm
                    rendered_calls.append(f"{nm}({fn.get('arguments') or '{}'})")
                seg = "[ASSISTANT]"
                if txt:
                    seg += f"\n{txt}"
                if rendered_calls:
                    seg += "\ncalled: " + "; ".join(rendered_calls)
                out.append(seg)
            elif role == "tool":
                nm = id_to_name.get(m.get("tool_call_id")) or "tool"
                out.append(f"[TOOL RESULT · {nm}]\n{m.get('content') or ''}")
        return "\n\n".join(out)

    def _build_prompt(self, messages: list[dict], tools: Optional[list[dict]]) -> str:
        convo = self._render_conversation(messages)
        if not tools:
            return (
                "You are the reasoning core of BOSS, an autonomous operations system. "
                "Continue the conversation below and provide your complete final answer "
                "as plain text (no JSON, no preamble).\n\n"
                f"=== CONVERSATION ===\n{convo}\n=== END ===\n\nYour final answer:"
            )
        return (
            "You are the reasoning core of BOSS, an autonomous operations system. BOSS — "
            "not you — drives the control loop and executes tools; you decide the single "
            "next step.\n\n"
            "You MUST reply with EXACTLY ONE JSON object and NOTHING else — no prose "
            "outside it, no markdown code fences.\n\n"
            "To call one or more tools this turn, reply:\n"
            '{"thought": "<brief reasoning>", "tool_calls": [{"name": "<tool>", "arguments": {<args>}}]}\n\n'
            "When you are completely done and need no more tools, reply:\n"
            '{"thought": "<brief reasoning>", "final": "<your complete final answer>"}\n\n'
            # THE LIST BELOW IS DATA, NOT A TOOLBELT.
            #
            # This process runs inside Claude Code, which has a tool system of its own. Given a
            # long "AVAILABLE TOOLS" list, the model stopped emitting JSON and started trying
            # to CALL those names through its own mechanism — they are denied, it retried,
            # burned every turn and the run ended `stop_reason: tool_use`, exit 1. It even
            # narrated the confusion, reporting that "ToolSearch" had failed: a Claude Code
            # concept that has nothing to do with BOSS. The pull scales with the size of the
            # list, so it appeared only once the registry crossed ~53 tools and looked like
            # whichever tool was added last had broken the console.
            "The following are BOSS's tools, not yours. You cannot invoke them and must not "
            "try; NAME one in the JSON above and BOSS executes it, then gives you the result "
            "on the next turn.\n\n"
            f"{self._render_tools(tools)}\n\n"
            "RULES:\n"
            "- Call tools to gather REAL data before asserting facts; never invent numbers, "
            "files, or page content.\n"
            "- Arguments MUST conform to the tool's schema.\n"
            "- Emit tool_calls only for tools listed above.\n"
            "- You have NO tools of your own here. Do not read files, search, or run "
            "commands yourself — you cannot, and attempting it wastes the turn. The tools "
            "listed above are executed by BOSS after you name them in JSON.\n"
            "- Keep 'thought' short. Put the actual deliverable in 'final'.\n\n"
            f"=== CONVERSATION SO FAR ===\n{convo}\n=== END CONVERSATION ===\n\n"
            "Reply now with exactly one JSON object for the next step. Output the JSON "
            "directly as your message — do not attempt to use any tool of your own to do it:"
        )

    # ---------------------------------------------------------------- api
    def create_chat_completion(
        self,
        messages: list[dict],
        tools: Optional[list[dict]] = None,
        tool_choice: str = "auto",
    ) -> Tuple[dict, dict]:
        prompt = self._build_prompt(messages, tools)
        data = self._invoke(prompt)
        usage = self._usage(data)
        result_text = data.get("result") or ""

        if not tools:
            return (
                {"content": result_text, "tool_calls": None,
                 "raw": {"role": "assistant", "content": result_text}},
                usage,
            )

        obj = _first_json_object(result_text)
        if not isinstance(obj, dict):
            # Could not parse an action — treat the text as a final answer so the
            # loop terminates gracefully rather than spinning.
            return (
                {"content": result_text, "tool_calls": None,
                 "raw": {"role": "assistant", "content": result_text}},
                usage,
            )

        thought = obj.get("thought") or ""
        raw_calls = obj.get("tool_calls")
        if not raw_calls and isinstance(obj.get("tool_call"), dict):
            raw_calls = [obj["tool_call"]]

        if raw_calls:
            tool_calls = []
            raw_tool_calls = []
            for i, c in enumerate(raw_calls):
                if not isinstance(c, dict) or not c.get("name"):
                    continue
                args = c.get("arguments")
                args_str = args if isinstance(args, str) else json.dumps(args or {})
                cid = f"call_{i + 1}"
                tool_calls.append({"id": cid, "name": c["name"], "arguments": args_str})
                raw_tool_calls.append({
                    "id": cid, "type": "function",
                    "function": {"name": c["name"], "arguments": args_str},
                })
            if tool_calls:
                return (
                    {
                        "content": thought or None,
                        "tool_calls": tool_calls,
                        "raw": {"role": "assistant", "content": thought or None, "tool_calls": raw_tool_calls},
                    },
                    usage,
                )

        # final answer
        final = obj.get("final")
        if final is None:
            final = thought or result_text
        if not isinstance(final, str):
            final = json.dumps(final)
        return (
            {"content": final, "tool_calls": None,
             "raw": {"role": "assistant", "content": final}},
            usage,
        )

    def create_text_response(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        model: Optional[str] = None,
    ) -> Tuple[str, str, dict]:
        full = (f"{system_prompt}\n\n{prompt}" if system_prompt else prompt)
        # EVERY CLI CHAT ENGINE PASSES THROUGH HERE, which is why the capacity observation
        # sits on the base rather than in each adapter's own raise. The chat and panel path
        # reports no llm_calls row at all, so the hook in record_llm_call — which covers the
        # BUILD path — never saw a panel failure: a subscription hit its weekly cap, the
        # meeting stalled for fifteen minutes, and nothing anywhere recorded why.
        try:
            data = self._invoke(full)
        except Exception as exc:  # noqa: BLE001 — observed, then re-raised unchanged
            self._observe_capacity(ok=False, error=str(exc))
            raise
        self._observe_capacity(ok=True, error=None)
        return (data.get("session_id", self._session_default),
                (data.get("result") or ""), self._usage(data))

    def create_plain_answer(self, system_prompt: str, prompt: str) -> Tuple[str, dict]:
        """One completion with no tools at all, for subject_answer's first answer.

        The base form is the text response on the adapter's own terms; the Claude CLI overrides
        `_invoke_plain` to drop Claude Code's own ~34k-token system prompt as well.
        """
        try:
            data = self._invoke_plain(system_prompt, prompt)
        except Exception as exc:  # noqa: BLE001 — observed, then re-raised unchanged
            self._observe_capacity(ok=False, error=str(exc))
            raise
        self._observe_capacity(ok=True, error=None)
        return (data.get("result") or ""), self._usage(data)

    def _invoke_plain(self, system_prompt: str, prompt: str) -> dict:
        return self._invoke(f"{system_prompt}\n\n{prompt}")

    def _observe_capacity(self, *, ok: bool, error: Optional[str]) -> None:
        """Fold this call's outcome into what BOSS knows about the provider's capacity.

        Never raises: an adapter must not fail a call because a bookkeeping write did.
        """
        try:
            from .llm_telemetry import get_llm_context
            from .provider_limits import record_call_outcome
            engine_name = getattr(self, "_engine_name", "")
            ctx = get_llm_context()
            db_engine, project_id = ctx.get("db_engine"), ctx.get("project_id")
            if not engine_name or not db_engine or not project_id:
                return
            with db_engine.begin() as connection:
                record_call_outcome(connection, getattr(self, "settings", None),
                                    str(project_id), engine_name,
                                    ok=ok, error=error)
        except Exception:  # noqa: BLE001
            return

class ClaudeCliAdapter(CliChatBrain):
    """Claude Code (`claude -p`) as a raw completion backend."""

    _session_default = "claude-cli"
    _engine_name = "claude_code"

    # ---------------------------------------------------------------- cli
    def _claude_bin(self) -> str:
        configured = getattr(self.settings, "claude_cli_path", None) or "claude"
        if configured == "claude":
            local = Path.home() / ".local" / "bin" / "claude"
            if local.exists():
                return str(local)
        return configured

    def _scratch_dir(self) -> str:
        # run in an empty dir so the CLI doesn't load a project's CLAUDE.md
        d = (self.settings.repo_root / ".local-logs" / "claude-scratch").resolve()
        d.mkdir(parents=True, exist_ok=True)
        return str(d)

    # NO BUILT-IN TOOLS, so Claude is a pure reasoning brain: BOSS, not Claude, controls all
    # data access and actions. `--tools ""` is the CLI's own switch for that (its --help: 'Use
    # "" to disable all tools'), so nothing here names a tool. A written deny list did, and went
    # stale both ways: Claude Code 2.1 dropped MultiEdit and a deny rule naming an unknown tool
    # makes `claude -p` exit 1 — every meeting step failed with 'Permission deny rule
    # "MultiEdit" matches no known tool' — while it had added tools the list never named
    # (Cron*, EnterWorktree, DesignSync, …: 26 built-ins, measured on boss-1, 0 with this flag).
    # (An empty --allowedTools does NOT disable server tools like WebSearch; --tools "" does.)
    _NO_BUILTIN_TOOLS = ("--tools", "")

    #: THE FIRST ANSWER'S PER-CALL OVERHEAD, MEASURED (this Mac, Claude Code 2.1.284, a
    #: one-word reply): Claude Code's own system prompt is ~34,000 input tokens on every `-p`
    #: call; with `--system-prompt` it is ~450. An answer-from-the-record call has no use for an
    #: agentic coding persona, and no tools, so it runs on its own short prompt, one turn, and
    #: with no MCP servers loaded (subject_answer.py).
    _PLAIN_FLAGS = ("--max-turns", "2", "--strict-mcp-config", "--disable-slash-commands")

    def _invoke_plain(self, system_prompt: str, prompt: str) -> dict:
        return self._invoke(prompt, system_prompt=system_prompt)

    def _command(self, prompt: str, system_prompt: Optional[str], *,
                 output: tuple[str, ...] = ("--output-format", "json")) -> tuple[list[str], bool]:
        """The `claude -p` command for one call, and whether the prompt goes on stdin."""
        # A LONG CONVERSATION IS NOT AN ARGUMENT. Linux caps a single argv entry at
        # MAX_ARG_STRLEN (128 KiB), and this prompt is the whole conversation plus ~48 tool
        # schemas — so a thread that had been going a while died with
        # `OSError: [Errno 7] Argument list too long: 'nice'`, which reached the operator as "I
        # could not answer that". The prompt goes on stdin once it is large; small ones keep the
        # argv form so nothing changes for the common case.
        via_stdin = len(prompt.encode("utf-8", "ignore")) > 96_000
        cmd = [
            self._claude_bin(),
            *(("-p",) if via_stdin else ("-p", prompt)),
            *output,
            # NOT 2. A TURN SPENT REACHING FOR A BLOCKED TOOL WAS FATAL.
            #
            # BOSS drives the loop and this process is meant to be pure reasoning, so Claude
            # Code's own tools are denied. But denying them does not stop the model from
            # TRYING: it burns a turn, gets refused, and with a 2-turn ceiling the run ends at
            # `stop_reason: tool_use` and exits 1 — which surfaced as "I could not answer
            # that" for every tool-using question in the console.
            #
            # It was latent and count-sensitive: 52 registered tools answered fine and the
            # 53rd tipped it over, so it would have looked like whichever tool was added last
            # broke the console. Leave room to recover from one stray attempt.
            *(("--max-turns", "6") if system_prompt is None else self._PLAIN_FLAGS),
            # VARIADIC: `--tools` takes every argument up to the next flag, so it never sits
            # last before a positional prompt — the prompt is passed with -p above.
            *self._NO_BUILTIN_TOOLS,  # pure completion, no tool use
        ]
        if system_prompt is not None:
            cmd += ["--system-prompt", system_prompt]
        model = self.model_override or getattr(self.settings, "claude_cli_model", None)
        if model:
            cmd += ["--model", model]
        return cmd, via_stdin

    def _timeouts(self) -> tuple[float, int]:
        own_timeout = int(getattr(self.settings, "claude_cli_timeout", 240) or 240)
        # NO CALL OUTLIVES ITS TURN (call_deadline.py): the CLI's own 240 s is a ceiling, and
        # inside a chat turn the turn's remaining time is the real one.
        from .call_deadline import bounded_timeout

        return bounded_timeout(own_timeout), own_timeout

    def _timed_out(self, timeout: float, own_timeout: int) -> Exception:
        from .call_deadline import TurnDeadlineExceeded, deadline_cut

        if deadline_cut(timeout, own_timeout):
            return TurnDeadlineExceeded(
                f"the turn's time ran out while the model was still working "
                f"({int(timeout)}s for this call)")
        return RuntimeError(f"claude CLI timed out after {int(timeout)}s")

    def _invoke(self, prompt: str, *, system_prompt: Optional[str] = None) -> dict:
        timeout, own_timeout = self._timeouts()
        cmd, via_stdin = self._command(prompt, system_prompt)
        try:
            # Mostly network wait, but it is still a node process in the API's container and
            # nothing here is latency-critical. See services/scheduling.py.
            proc = subprocess.run(
                low_priority(cmd),
                input=(prompt if via_stdin else None),
                stdin=(None if via_stdin else subprocess.DEVNULL),
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=self._scratch_dir(),
                env=self._child_env(),
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                "claude CLI not found. Install Claude Code and ensure `claude` is on PATH."
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise self._timed_out(timeout, own_timeout) from exc
        if proc.returncode != 0:
            raise RuntimeError(
                f"claude CLI exited {proc.returncode}: {(proc.stderr or proc.stdout or '')[:300]}"
            )
        try:
            data = json.loads(proc.stdout)
        except Exception as exc:
            raise RuntimeError(f"claude CLI returned non-JSON: {proc.stdout[:300]}") from exc
        if data.get("is_error"):
            raise RuntimeError(f"claude CLI error: {data.get('result') or data.get('subtype')}")
        return data

    # ---------------------------------------------------------------- streaming
    #: `stream-json` with partial messages: one JSON event per line, the reply's text arriving
    #: as `content_block_delta`/`text_delta` events while the model writes it, then a `result`
    #: event shaped exactly like `--output-format json`'s single object (so `_usage` reads it).
    _STREAM_OUTPUT = ("--output-format", "stream-json", "--verbose", "--include-partial-messages")

    def stream_text_response(self, prompt: str, system_prompt: Optional[str] = None,
                             model: Optional[str] = None) -> Iterator[Any]:
        """`create_text_response`, as the model writes it: yields each piece of text as it
        arrives, then the usage dict. A talk shows the words as they come instead of thinking
        dots for the whole of a 20-60 s reply (agent_converse._call).

        Same prompt, flags, credential, timeout and capacity observation as the one-shot form;
        closing the generator (the person pressed Stop, or left) ends the CLI process.
        """
        full = (f"{system_prompt}\n\n{prompt}" if system_prompt else prompt)
        try:
            yield from self._stream(full)
        except Exception as exc:  # noqa: BLE001 — observed, then re-raised unchanged
            self._observe_capacity(ok=False, error=str(exc))
            raise
        self._observe_capacity(ok=True, error=None)

    def _stream(self, prompt: str) -> Iterator[Any]:
        timeout, own_timeout = self._timeouts()
        cmd, via_stdin = self._command(prompt, None, output=self._STREAM_OUTPUT)
        try:
            proc = subprocess.Popen(
                low_priority(cmd),
                stdin=(subprocess.PIPE if via_stdin else subprocess.DEVNULL),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1,
                cwd=self._scratch_dir(), env=self._child_env(),
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                "claude CLI not found. Install Claude Code and ensure `claude` is on PATH."
            ) from exc
        expired = threading.Event()

        def expire() -> None:
            expired.set()
            proc.kill()

        timer = threading.Timer(timeout, expire)
        timer.daemon = True
        # stderr is drained on the side: a chatty CLI must not fill the pipe and stall stdout.
        errors: list[str] = []
        drain = threading.Thread(target=lambda: errors.append(proc.stderr.read() or ""),
                                 daemon=True)
        timer.start()
        drain.start()
        result: Optional[dict] = None
        raw: list[str] = []
        try:
            if via_stdin:
                proc.stdin.write(prompt)
                proc.stdin.close()
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    raw.append(line)
                    continue
                kind = event.get("type")
                if kind == "stream_event":
                    delta = (event.get("event") or {}).get("delta") or {}
                    if delta.get("type") == "text_delta" and delta.get("text"):
                        yield str(delta["text"])
                elif kind == "result":
                    result = event
            proc.wait()
        finally:
            timer.cancel()
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        drain.join(timeout=1)
        if expired.is_set():
            raise self._timed_out(timeout, own_timeout)
        if result is None:
            said = ("".join(errors) or "\n".join(raw))[:300]
            raise RuntimeError(f"claude CLI exited {proc.returncode}: {said}")
        if result.get("is_error"):
            raise RuntimeError(
                f"claude CLI error: {result.get('result') or result.get('subtype')}")
        yield self._usage(result)

    def _child_env(self) -> dict:
        """The CLI's environment, with the credential this install actually holds.

        Nothing was passed here, so the child inherited the API container's environment — and
        on an install whose token lives in a secret store that is empty. `claude -p` answered
        "Not logged in", the falling-back adapter read that as "this backend is not available
        here" and handed the operator the SECOND backend's error:

            IntegrationConfigurationError: OPENAI_API_KEY is not configured

        about a subscription that was connected, proved with a real turn, and stored. The
        coding harness had always resolved the stored credential; the chat never did, so the
        one interface a customer talks to was the one that could not reach the model.
        """
        from .agent_env import agent_environment
        from .cli_harness import stored_credential

        token = str(getattr(self.settings, "claude_code_oauth_token", "") or "").strip()
        if not token and not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
            token = stored_credential(self.settings, "CLAUDE_CODE_OAUTH_TOKEN") or ""
        extra = {"CLAUDE_CODE_OAUTH_TOKEN": token} if token else {}
        env = agent_environment(os.environ, extra)
        if token:
            env.pop("ANTHROPIC_API_KEY", None)
        return env

    def _usage(self, data: dict) -> dict:
        u = data.get("usage") or {}
        # Cache tokens tracked SEPARATELY (BOSS-280 P1): folding them into prompt_tokens
        # priced cache reads as full-price input on the estimate path, making cache savings
        # invisible-to-negative. prompt_tokens = genuinely new input only.
        prompt_tokens = int(u.get("input_tokens") or 0)
        cache_read = int(u.get("cache_read_input_tokens") or 0)
        cache_write = int(u.get("cache_creation_input_tokens") or 0)
        completion = int(u.get("output_tokens") or 0)
        model_usage = data.get("modelUsage") or {}
        # primary (non-haiku helper) model name, else configured/default
        primary = None
        for name in model_usage:
            if "haiku" not in name:
                primary = name
                break
        primary = (primary or (next(iter(model_usage), None)) or self.model_override
                   or getattr(self.settings, "claude_cli_model", None) or "claude-cli")
        cost = data.get("total_cost_usd")
        usage = {
            "model": primary,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion,
            "cache_read_tokens": cache_read,
            "cache_write_tokens": cache_write,
            "total_tokens": prompt_tokens + cache_read + cache_write + completion,
        }
        if cost is not None:
            usage["cost_usd_micros"] = int(round(float(cost) * 1_000_000))
        return usage



class FallingBackAdapter:
    """Claude first; the previous backend if Claude cannot be reached at all.

    Raising the chat agent's brain must not be a way to take chat down. The Claude path shells
    out to a binary that has to exist, be authenticated, and answer inside a timeout — three
    things that are true in the API container and need not be true everywhere, forever.

    The distinction that matters: an INFRASTRUCTURE failure (binary missing, not logged in,
    process timed out) is not an answer, and falling back gets the operator a reply. A MODEL
    failure — bad JSON, a refusal, an empty completion — is an answer of sorts and belongs to
    the caller's own retry logic, which already knows how to press it. Falling back on those
    would silently downgrade the brain the moment the good one had a bad turn, and nobody
    would ever see it happen.
    """

    #: Substrings that mean "this backend is not available here", not "the model did badly".
    _UNAVAILABLE = ("cli not found", "not found. install", "timed out after",
                    "exited 1", "exited 127", "not logged in", "authentication",
                    "credit balance", "usage limit", "rate limit")

    def __init__(self, primary, fallback, *, on_fallback=None):
        self.primary = primary
        self.fallback = fallback
        self._on_fallback = on_fallback
        #: Which one actually answered last, so it can be reported rather than guessed at.
        self.last_backend = ""

    def _unavailable(self, exc: BaseException) -> bool:
        from .call_deadline import TurnDeadlineExceeded

        # THE TURN RAN OUT, NOT THE BACKEND. Asking the fallback brain the same question from
        # the start would only spend more of a person's wait (call_deadline.py).
        if isinstance(exc, TurnDeadlineExceeded):
            return False
        blob = f"{type(exc).__name__}: {exc}".lower()
        return any(marker in blob for marker in self._UNAVAILABLE)

    def create_chat_completion(self, messages, tools=None, **kwargs):
        primary_error = None
        try:
            out = self.primary.create_chat_completion(messages, tools=tools, **kwargs)
            self.last_backend = type(self.primary).__name__
            return out
        except Exception as exc:  # noqa: BLE001
            if not self._unavailable(exc):
                raise
            primary_error = exc
            _log.warning("chat brain unavailable (%s); falling back to %s",
                         exc, type(self.fallback).__name__)
            if self._on_fallback:
                try:
                    self._on_fallback(exc)
                except Exception:  # noqa: BLE001 — reporting must not break answering
                    pass
        try:
            out = self.fallback.create_chat_completion(messages, tools=tools, **kwargs)
        except Exception as fallback_exc:  # noqa: BLE001
            # THE FIRST FAILURE IS THE REAL ONE. Reporting the second hides a CLI that said
            # exactly what was wrong behind a backend the operator never chose.
            raise RuntimeError(
                f"{primary_error} (and the fallback failed too: {fallback_exc})") from primary_error
        self.last_backend = type(self.fallback).__name__
        return out

    def create_plain_answer(self, system_prompt, prompt):
        """subject_answer's one call, on the same fallback terms as the chat one. A fallback
        without the lean path gets an ordinary tool-less chat completion."""
        primary_error = None
        try:
            plain = getattr(self.primary, "create_plain_answer", None)
            if callable(plain):
                out = plain(system_prompt, prompt)
            else:
                msg, usage = self.primary.create_chat_completion(
                    [{"role": "system", "content": system_prompt},
                     {"role": "user", "content": prompt}], tools=None)
                out = (str(msg.get("content") or ""), usage)
            self.last_backend = type(self.primary).__name__
            return out
        except Exception as exc:  # noqa: BLE001
            if not self._unavailable(exc):
                raise
            primary_error = exc
            _log.warning("chat brain unavailable (%s); falling back to %s",
                         exc, type(self.fallback).__name__)
            if self._on_fallback:
                try:
                    self._on_fallback(exc)
                except Exception:  # noqa: BLE001 — reporting must not break answering
                    pass
        try:
            msg, usage = self.fallback.create_chat_completion(
                [{"role": "system", "content": system_prompt},
                 {"role": "user", "content": prompt}], tools=None)
        except Exception as fallback_exc:  # noqa: BLE001
            raise RuntimeError(
                f"{primary_error} (and the fallback failed too: {fallback_exc})") from primary_error
        self.last_backend = type(self.fallback).__name__
        return str(msg.get("content") or ""), usage

    @property
    def stream_text_response(self):
        """The primary's stream, on the same fallback terms: a backend that is not available
        before its first word falls back (streaming if the fallback can, else its whole
        answer). Once words have reached the person, a failure is the answer's, never a cue
        to start again on another backend. None when the primary cannot stream."""
        primary = getattr(self.primary, "stream_text_response", None)
        if not callable(primary):
            return None

        def stream(prompt, system_prompt=None, model=None):
            spoke = False
            try:
                for piece in primary(prompt, system_prompt=system_prompt, model=model):
                    spoke = spoke or not isinstance(piece, dict)
                    yield piece
                self.last_backend = type(self.primary).__name__
                return
            except Exception as exc:  # noqa: BLE001
                if spoke or not self._unavailable(exc):
                    raise
                primary_error = exc
                _log.warning("chat brain unavailable (%s); falling back to %s",
                             exc, type(self.fallback).__name__)
                if self._on_fallback:
                    try:
                        self._on_fallback(exc)
                    except Exception:  # noqa: BLE001 — reporting must not break answering
                        pass
            try:
                other = getattr(self.fallback, "stream_text_response", None)
                if callable(other):
                    yield from other(prompt, system_prompt=system_prompt, model=model)
                else:
                    reply = self.fallback.create_text_response(
                        prompt, system_prompt=system_prompt, model=model)
                    yield str(reply[1] if isinstance(reply, tuple) and len(reply) > 1 else reply)
                    if isinstance(reply, tuple) and len(reply) > 2 and isinstance(reply[2], dict):
                        yield reply[2]
            except Exception as fallback_exc:  # noqa: BLE001
                raise RuntimeError(f"{primary_error} (and the fallback failed too: "
                                   f"{fallback_exc})") from primary_error
            self.last_backend = type(self.fallback).__name__

        return stream

    def create_text_response(self, prompt, system_prompt=None, model=None):
        """The one-shot completion, on the same terms as the chat one.

        MEETINGS USE THIS AND THIS ONLY. The panel calls `create_text_response`; both CLI
        adapters inherit it from CliChatBrain and this wrapper did not have it at all — so
        routing meetings at a subscription, which is what the setting labelled "Console chat,
        agents and meetings" promises, would have raised AttributeError instead of the
        OPENAI_API_KEY error it was meant to replace. A wrapper that implements half of the
        interface it wraps is a fallback that only works for the callers somebody tested.
        """
        primary_error = None
        try:
            out = self.primary.create_text_response(
                prompt, system_prompt=system_prompt, model=model)
            self.last_backend = type(self.primary).__name__
            return out
        except Exception as exc:  # noqa: BLE001
            if not self._unavailable(exc):
                raise
            primary_error = exc
            _log.warning("chat brain unavailable (%s); falling back to %s",
                         exc, type(self.fallback).__name__)
            if self._on_fallback:
                try:
                    self._on_fallback(exc)
                except Exception:  # noqa: BLE001 — reporting must not break answering
                    pass
        try:
            out = self.fallback.create_text_response(
                prompt, system_prompt=system_prompt, model=model)
        except Exception as fallback_exc:  # noqa: BLE001
            raise RuntimeError(
                f"{primary_error} (and the fallback failed too: {fallback_exc})") from primary_error
        self.last_backend = type(self.fallback).__name__
        return out


#: What the console calls these engines, mapped onto what this factory calls them.
#:
#: The console shows one vocabulary everywhere — `claude_code` is the name in the role pickers,
#: in detection and on the Connect button — and this factory has always spelled it
#: `claude_cli`. An operator who chose `claude_code` for the chat would otherwise land in the
#: `engine != "claude_cli"` branch and get the OpenAI adapter, which is the exact failure this
#: is meant to remove.
_CHAT_ENGINE_ALIASES = {
    "claude_code": "claude_cli",
    "claude": "claude_cli",
    "anthropic": "claude_cli",
    "codex_cli": "codex",
    "openai": "native",
}


def openai_fallback_usable(settings: Settings) -> bool:
    """Can the OpenAI adapter actually answer on this install?

    A FALLBACK THAT CANNOT WORK IS NOT A FALLBACK — it is a way to replace an actionable error
    with an unactionable one. `_UNAVAILABLE` includes "exited 1", so any hiccup from the CLI
    (codex printed `failed to refresh available models` and exited 1) sent the call to the
    OpenAI adapter, which on an install with no key raises

        IntegrationConfigurationError: OPENAI_API_KEY is not configured.

    and that is what the operator saw — about a subscription that was connected and working,
    while the real reason sat in a log nobody reads. Twice.
    """
    for attr in ("openai_api_key", "azure_openai_key"):
        if str(getattr(settings, attr, "") or "").strip():
            return True
    return False


def _chat_engine_name(value: str) -> str:
    key = (value or "").strip().lower()
    return _CHAT_ENGINE_ALIASES.get(key, key)


def make_chat_adapter(settings: Settings):
    """Pick the completion backend: a subscription CLI, else the OpenAI/Azure adapter."""
    from .openai_adapter import OpenAIAdapter

    engine = _chat_engine_name(getattr(settings, "llm_engine", "auto") or "auto")
    if engine == "claude_cli":
        return ClaudeCliAdapter(settings)
    if engine == "codex":
        from .codex_cli_adapter import CodexCliAdapter

        return CodexCliAdapter(settings)
    return OpenAIAdapter(settings)


def make_agent_chat_adapter(settings: Settings):
    """The backend for the CHAT agent specifically — the one an operator talks to.

    It was on `gpt-5.2-codex` and nobody had decided that: `llm_engine` defaults to "auto",
    `make_chat_adapter` selects Claude only on the exact string "claude_cli", and so the one
    interface customers use ran on the fallback while the Claude subscription went to the
    coding pipeline. This makes the choice explicit and separately settable.
    """
    from .openai_adapter import OpenAIAdapter

    engine = _chat_engine_name(getattr(settings, "chat_llm_engine", "") or "")
    if not engine:
        return make_chat_adapter(settings)          # inherit — local dev, CI
    model = (getattr(settings, "chat_model", "") or "").strip() or None
    if engine == "codex":
        # Codex reached the coding pipeline long before it could answer a question, so a
        # `codex` lock moved builds and left chat on whatever OPENAI_* was configured. An
        # operator who sets one lock believes they have moved everything.
        from .codex_cli_adapter import make_codex_chat_adapter

        return make_codex_chat_adapter(settings, model=model)
    if engine != "claude_cli":
        return OpenAIAdapter(settings)
    claude = ClaudeCliAdapter(settings, model=model)
    if not openai_fallback_usable(settings):
        # Alone, deliberately: with nothing behind it, the CLI's own error reaches the caller
        # — "not logged in", "usage limit", "exited 1 …" — each of which names its own fix.
        return claude
    return FallingBackAdapter(claude, OpenAIAdapter(settings))
