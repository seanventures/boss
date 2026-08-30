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
import re
import subprocess
from pathlib import Path
from typing import Optional, Tuple

from ..config import Settings
from .scheduling import low_priority


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


class ClaudeCliAdapter:
    def __init__(self, settings: Settings):
        self.settings = settings

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

    # Claude Code's built-in tools — explicitly DENIED so Claude is a pure
    # reasoning brain: BOSS, not Claude, controls all data access and actions.
    # (An empty --allowedTools does NOT disable server tools like WebSearch.)
    _BLOCKED_TOOLS = (
        "WebSearch,WebFetch,Bash,BashOutput,KillShell,Edit,MultiEdit,Write,Read,"
        "Glob,Grep,Task,NotebookEdit,TodoWrite,SlashCommand,ExitPlanMode"
    )

    def _invoke(self, prompt: str) -> dict:
        timeout = int(getattr(self.settings, "claude_cli_timeout", 240) or 240)
        cmd = [
            self._claude_bin(),
            "-p", prompt,
            "--output-format", "json",
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
            "--max-turns", "6",
            "--disallowedTools", self._BLOCKED_TOOLS,  # pure completion, no tool use
        ]
        model = getattr(self.settings, "claude_cli_model", None)
        if model:
            cmd += ["--model", model]
        try:
            # Mostly network wait, but it is still a node process in the API's container and
            # nothing here is latency-critical. See services/scheduling.py.
            proc = subprocess.run(
                low_priority(cmd),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=self._scratch_dir(),
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                "claude CLI not found. Install Claude Code and ensure `claude` is on PATH."
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"claude CLI timed out after {timeout}s") from exc
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
        primary = primary or (next(iter(model_usage), None)) or getattr(self.settings, "claude_cli_model", None) or "claude-cli"
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
        data = self._invoke(full)
        return data.get("session_id", "claude-cli"), (data.get("result") or ""), self._usage(data)


def make_chat_adapter(settings: Settings):
    """Pick the completion backend. claude_cli → Claude Code CLI on the Max plan;
    otherwise the OpenAI/Azure adapter."""
    from .openai_adapter import OpenAIAdapter

    if (getattr(settings, "llm_engine", "auto") or "auto").lower() == "claude_cli":
        return ClaudeCliAdapter(settings)
    return OpenAIAdapter(settings)
