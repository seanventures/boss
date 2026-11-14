"""THE CONTEXT POLICY AS KERNEL MIDDLEWARE (AP-P6, BOSSAPP-1057; AGENT_PLATFORM.md §4.7).

The one place a run's context is shaped. It implements deepagents' measured defaults
(docs.langchain.com/oss/python/deepagents/context-engineering) on BOSS's kernel, over the run
filesystem (kernel/runfs.py), and keeps BOSS's cache discipline:

  wrap_tool_call  OFFLOAD, DON'T CLIP. A tool result over the threshold — `offload_tokens`
                  (default 20,000 tokens) or the preset's character clip (16,000 for agent steps
                  and chat), whichever is smaller — is written whole to
                  /large_tool_results/<call id> and the model reads the path, its size and a
                  10-line preview instead. Nothing is cut: the model greps or pages the file.
                  This replaces the lossy CUT marker on kernel runs; the non-kernel paths keep
                  `tool_result_text.clip_for_model`.

  before_model    CLEAR, THEN SUMMARISE. Past `clear_at` of the window (default 50%), old tool
                  results become stubs (the lightest compaction; the whole result is kept at
                  /large_tool_results/<call id>, so a stub names where). Past `summarise_at`
                  (default 85%) the older conversation is summarised, keeping the most recent
                  `keep` (default 10%) of the window verbatim; the originals are archived to
                  /scratch/conversation_history/<n>.md before they leave the conversation. The
                  summary is a governed model call (budgeted, receipted: an llm_calls row), with
                  a deterministic extractive digest when the model cannot answer.

  before_agent    SKILLS ON DEMAND. Held SKILL.md skills are listed by name and description in
                  one system block; each body is at /skills/<name>/SKILL.md and costs tokens
                  only when the agent reads it.

CACHE STABILITY. Every change here is made to the run's state ONCE and then stays: an offloaded
stub, a cleared result and a summary are written into `state.messages` (the archive keeps the
originals), so the request prefix is byte-stable between compactions and the prompt cache keeps
hitting. Clearing waits until it is worth a cache write (the window is half full) and always
clears in one sweep. The skills block is appended LAST to the system (changing state last), so
the cached primer before it is untouched.

Configured from the agent spec's `context` field (boss.agent/v1: offload_tokens, summarise_at,
keep, clear_at, clear_keep, window_tokens) — `ContextConfig.from_spec`.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional

from . import runfs
from .engine import Block, ModelRequest
from .middleware import Middleware, ModelCall, ToolRequest
from .types import ToolResult

#: deepagents' defaults (context-engineering docs): offload above 20k tokens with a 10-line
#: preview; summarise at 85% of the window keeping 10%.
OFFLOAD_TOKENS = 20_000
SUMMARISE_AT = 0.85
KEEP = 0.10
PREVIEW_LINES = 10
#: Tool-result clearing: past half the window, all but the newest `CLEAR_KEEP` results become
#: stubs (Anthropic's context-editing default is to clear at a threshold, not every call).
CLEAR_AT = 0.50
CLEAR_KEEP = 4
#: The chars/4 heuristic every BOSS estimate uses (engine.estimate_tokens).
CHARS_PER_TOKEN = 4

HISTORY_DIR = f"{runfs.SCRATCH}/conversation_history"

SUMMARY_SYSTEM = (
    "You compress an AI agent's earlier conversation so the agent can keep working without it. "
    "Write a factual summary in plain prose and short bullet lists: the task and its goal; every "
    "DECISION made and who made it; facts established (ids, numbers, names, paths, errors) "
    "exactly as written; what was tried and did not work; open questions and the next step. "
    "Never invent anything. Prefer specific values to descriptions of them. The originals are "
    "archived, so say nothing about what you left out.")

SUMMARY_HEAD = (
    "[EARLIER CONVERSATION, SUMMARISED to keep this conversation inside the model's window. "
    "Nothing was deleted: the {count} original messages are archived word for word at {path}. "
    "When you need an exact detail from them — a decision, an id, a number — grep that file "
    "(grep pattern=..., path={path}) instead of guessing.]\n\n")

#: The same fold where no archive is kept (the legacy chat loop, which has no run filesystem).
DIGEST_HEAD = ("[EARLIER CONVERSATION, CONDENSED to keep this conversation inside the model's "
               "window: the {count} older messages, one line each. If an exact detail from "
               "them matters and is not here, say so rather than guess.]\n\n")

CLEARED_NOTE = ("cleared from the conversation to save room; nothing is lost — the whole result "
                "is at {path} (read_file or grep it if you need it again)")

SKILLS_HEAD = ("SKILLS YOU HOLD. Each is a playbook; only its description is shown here. When one "
               "fits the task, read its body first (read_file path=/skills/<name>/SKILL.md).")


@dataclass(frozen=True)
class ContextConfig:
    """How one run's context is shaped. Every field has the deepagents default; an agent spec's
    `context` overrides them (`from_spec`)."""

    offload_tokens: int = OFFLOAD_TOKENS
    summarise_at: float = SUMMARISE_AT
    keep: float = KEEP
    clear_at: float = CLEAR_AT
    clear_keep: int = CLEAR_KEEP
    preview_lines: int = PREVIEW_LINES
    #: A cap on the engine's context window (tokens): chat keeps its history to a working size
    #: rather than filling a 200k window on every turn. None: the engine's own window.
    window_tokens: Optional[int] = None
    #: "model" (a governed fast call, falling back to the digest) or "extractive" (no call).
    summariser: str = "model"
    #: A conversation that outlives one run (a chat thread): its last compaction is reused by
    #: the next turn instead of being summarised again (`_THREAD_MEMO`).
    thread_key: Optional[str] = None
    #: Held SKILL.md skills: ({name, description, body}, ...), disclosed progressively.
    skills: tuple = ()
    #: The run's MemoryPort factory for /memories ((state) -> port or None).
    memory_for: Optional[Callable[[Any], Any]] = field(default=None, compare=False)
    hooks: tuple = ()

    @classmethod
    def from_spec(cls, context: Optional[Mapping[str, Any]] = None, **overrides: Any
                  ) -> "ContextConfig":
        """The defaults, then the agent spec's `context` field, then the caller's overrides."""
        values: dict = {}
        spec = dict(context or {})
        for key in ("offload_tokens", "clear_keep", "window_tokens"):
            if spec.get(key) is not None:
                values[key] = int(spec[key])
        for key in ("summarise_at", "keep", "clear_at"):
            if spec.get(key) is not None:
                values[key] = float(spec[key])
        if spec.get("hooks"):
            values["hooks"] = tuple(str(h) for h in spec["hooks"])
        values.update({k: v for k, v in overrides.items() if v is not None})
        if "skills" in values:
            values["skills"] = tuple(dict(s) for s in values["skills"])
        return cls(**values)

    @property
    def version(self) -> str:
        return (f"ctx.policy@1(offload={self.offload_tokens},summarise={self.summarise_at},"
                f"keep={self.keep},clear={self.clear_at}/{self.clear_keep})")


# ── Estimating ────────────────────────────────────────────────────────────────────────────────

def tokens_of(value: Any) -> int:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return max(1, len(text) // CHARS_PER_TOKEN)


def message_tokens(messages: list) -> int:
    return sum(tokens_of(m) for m in messages)


def state_tokens(state: Any, tools: list = ()) -> int:
    system = "\n\n".join(b.text for b in state.system if isinstance(b, Block))
    total = tokens_of(system) + message_tokens(state.messages)
    if tools:
        total += tokens_of([getattr(t, "parameters", {}) for t in tools])
    return total


def window_of(engine: Any, config: ContextConfig) -> int:
    traits = getattr(engine, "traits", None)
    window = int(getattr(traits, "context_window", 0) or 128_000)
    if config.window_tokens:
        window = min(window, int(config.window_tokens))
    return max(1_000, window)


# ── Offloading ────────────────────────────────────────────────────────────────────────────────

def full_render(policy: Any, result: ToolResult) -> str:
    """The result as the preset's ContextPolicy renders it (redaction, the JSON default), but
    WITHOUT its clip: the whole text the model would otherwise have had cut."""
    if dataclasses.is_dataclass(policy) and hasattr(policy, "clip"):
        return dataclasses.replace(policy, clip=None).render(result)
    return policy.render(result)


def offload_threshold_chars(policy: Any, config: ContextConfig) -> int:
    """Offload above `offload_tokens`, or above the preset's own clip (the 16,000 characters the
    CUT marker used to fire at), whichever is smaller — so nothing that used to be cut is cut."""
    limit = int(config.offload_tokens) * CHARS_PER_TOKEN
    clip = getattr(policy, "clip", None)
    if clip:
        limit = min(limit, int(clip))
    return max(256, limit)


def render_for(run: Any, result: ToolResult) -> str:
    """The tool message body: a stub the context policy wrote, else the preset's rendering."""
    rendered = getattr(result, "rendered", None)
    if rendered is not None:
        return rendered
    return run.policies.context.render(result)


# ── Compaction ────────────────────────────────────────────────────────────────────────────────

_THREAD_MEMO: "OrderedDict[str, dict]" = OrderedDict()
_THREAD_MEMO_LOCK = threading.Lock()
_THREAD_MEMO_MAX = 512


def _digest(messages: list) -> str:
    return hashlib.sha256(json.dumps(messages, sort_keys=True, default=str).encode()).hexdigest()


def _content_text(message: Mapping[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, list):
        return " ".join(str(b.get("text") or "") for b in content if isinstance(b, Mapping))
    return "" if content is None else str(content)


def archive_markdown(messages: list, *, first_number: int = 1) -> str:
    """The originals, word for word, one numbered block each — what grep reads."""
    out: list = []
    for offset, message in enumerate(messages):
        number = first_number + offset
        role = str(message.get("role") or "user")
        head = f"### [{number}] {role}"
        if role == "tool":
            head += f" (result of call {message.get('tool_call_id')})"
        out.append(head)
        text = _content_text(message)
        if text:
            out.append(text)
        for call in message.get("tool_calls") or []:
            fn = call.get("function") if isinstance(call.get("function"), Mapping) else call
            out.append(f"(called {fn.get('name')} with {fn.get('arguments')})")
        out.append("")
    return "\n".join(out)


def extractive_summary(messages: list, *, budget_tokens: int) -> str:
    """A deterministic digest: each message's role and first line, within the budget. Used when
    the summarising model cannot answer, and by ClipContext.compact (no model at all)."""
    lines: list = []
    used = 0
    for number, message in enumerate(messages, start=1):
        role = str(message.get("role") or "user")
        text = " ".join(_content_text(message).split())
        if role == "tool":
            line = f"- [{number}] tool result ({len(text)} chars)"
        else:
            calls = [str((c.get("function") or c).get("name") or "")
                     for c in message.get("tool_calls") or []]
            said = text[:240] + ("…" if len(text) > 240 else "")
            line = f"- [{number}] {role}: {said}" + (f" (called {', '.join(calls)})" if calls
                                                    else "")
        cost = tokens_of(line)
        if used + cost > budget_tokens:
            lines.append(f"- … {len(messages) - number + 1} more messages, in the archive")
            break
        lines.append(line)
        used += cost
    return "\n".join(lines)


def choose_cut(messages: list, keep_tokens: int, *, start: int = 0) -> int:
    """The first message index to KEEP: everything before it is summarised. The kept tail is
    about `keep_tokens`, and the cut never separates a tool result from the call that made it
    (it lands on an assistant message when it can, so the summary — a user message — is
    followed by the assistant: the alternation every provider accepts)."""
    tail = 0
    cut = len(messages)
    for index in range(len(messages) - 1, start - 1, -1):
        tail += tokens_of(messages[index])
        if tail > keep_tokens and index < len(messages) - 1:
            break
        cut = index
    # never keep nothing but the last message unless that is all there is
    cut = max(start + 1, min(cut, len(messages) - 1)) if len(messages) - start > 1 else start
    for index in range(cut, len(messages)):
        if messages[index].get("role") == "assistant":
            return index
    for index in range(cut, len(messages)):
        if messages[index].get("role") == "user":
            return index
    return len(messages)


def _pin_first(messages: list, keep_tokens: int) -> int:
    """How many leading messages stay verbatim: the run's opening user message (the task) when
    it is small enough to keep — the instruction is the first thing a summary must not lose."""
    if messages and messages[0].get("role") == "user" and tokens_of(messages[0]) <= max(
            256, keep_tokens // 2):
        return 1
    return 0


def compact_messages(messages: list, *, keep_tokens: int, archive_path: str,
                     summarise: Callable[[list], str], first_number: int = 1,
                     pinned: Optional[int] = None, head: str = "") -> Optional[dict]:
    """(new messages, archived originals, summary) — or None when there is nothing to fold.
    Pure: the caller writes the archive and swaps the messages."""
    pin = _pin_first(messages, keep_tokens) if pinned is None else pinned
    cut = choose_cut(messages, keep_tokens, start=pin)
    folded = messages[pin:cut]
    if not folded:
        return None
    summary = summarise(folded)
    note = (head or SUMMARY_HEAD).format(count=len(folded), path=archive_path) + summary
    head = list(messages[:pin])
    if head and head[-1].get("role") == "user":
        # one user turn, never two in a row: the pinned task and the summary are one message
        merged = dict(head[-1])
        merged["content"] = f"{_content_text(head[-1])}\n\n{note}"
        head[-1] = merged
        new = head + list(messages[cut:])
    else:
        new = head + [{"role": "user", "content": note}] + list(messages[cut:])
    if len(new) > 1 and new[1].get("role") == "user" and new[0].get("role") == "user":
        merged = dict(new[0])
        merged["content"] = f"{_content_text(new[0])}\n\n{_content_text(new[1])}"
        new = [merged] + new[2:]
    return {"messages": new, "archived": folded, "summary": summary,
            "archive": archive_markdown(folded, first_number=first_number + pin),
            "cut": cut, "pin": pin}


# ── The middleware ────────────────────────────────────────────────────────────────────────────

class ContextPolicyMiddleware(Middleware):
    """See the module docstring. Persistent state lives in `state.extra["_ctx"]` (the checkpoint
    carries it): the compactions made, the results cleared, whether the skills were disclosed."""

    name = "context_policy"
    version = "ctx.policy@1"
    can_jump_to: tuple = ()

    def __init__(self, config: Optional[ContextConfig] = None):
        self.config = config or ContextConfig()
        self.version = self.config.version

    # ── helpers ────────────────────────────────────────────────────────────────────────────
    def fs(self, state: Any) -> runfs.RunFS:
        memory = self.config.memory_for(state) if self.config.memory_for is not None else None
        return runfs.RunFS(state, memory=memory)

    @staticmethod
    def memo(state: Any) -> dict:
        memo = state.extra.get("_ctx")
        if not isinstance(memo, dict):
            memo = {"compactions": 0, "cleared": []}
            state.extra["_ctx"] = memo
        return memo

    def toolset(self) -> runfs.RunFSToolSet:
        return runfs.RunFSToolSet(memory_for=self.config.memory_for)

    # ── before_agent: the files and the skills ─────────────────────────────────────────────
    def before_agent(self, run: Any) -> None:
        state = run.state
        self.fs(state).files  # the FS exists from the first checkpoint on
        memo = self.memo(state)
        if self.config.skills and not memo.get("skills_disclosed"):
            skills = {}
            for skill in self.config.skills:
                name = str(skill.get("name") or "").strip()
                if name:
                    skills[name] = {"description": str(skill.get("description") or ""),
                                    "body": str(skill.get("body") or "")}
            if skills:
                state.extra[runfs.SKILLS_KEY] = skills
                listing = "\n".join(f"- {n}: {s['description']}" for n, s in sorted(skills.items()))
                # Changing state LAST: an uncached block after the cached primer.
                state.system.append(Block(text=f"{SKILLS_HEAD}\n{listing}"))
                memo["skills_disclosed"] = sorted(skills)
        return None

    # ── wrap_tool_call: offload ────────────────────────────────────────────────────────────
    def wrap_tool_call(self, run: Any, request: ToolRequest, handler: Callable) -> ToolResult:
        result = handler(request)
        if not isinstance(result, ToolResult):
            return result
        if request.call.name in runfs.RUN_FS_TOOLS:
            # Bounded by construction (a page, 40 matches): shown whole, never offloaded again
            # and never cut — a read of an offloaded result must not meet the CUT it replaced.
            if result.status == "ok" and result.rendered is None:
                return dataclasses.replace(result, rendered=full_render(run.policies.context,
                                                                        result))
            return result
        if result.status == "deferred" or result.content is None:
            return result
        policy = run.policies.context
        body = full_render(policy, result)
        if len(body) <= offload_threshold_chars(policy, self.config):
            return result
        stub = runfs.offload(self.fs(run.state), call_id=result.call_id or request.call.id,
                             tool=result.name, body=body, preview_lines=self.config.preview_lines)
        run.rec.emit("context.compacted", {"how": "offload", "tool": result.name,
                                           "call_id": result.call_id, "path": stub["path"],
                                           "chars": stub["chars"],
                                           "tokens": stub["approx_tokens"]})
        return dataclasses.replace(result, rendered=runfs.stub_text(stub))

    # ── before_model: clear, then summarise ────────────────────────────────────────────────
    def before_model(self, run: Any, call: ModelCall) -> None:
        state = run.state
        window = window_of(run.engine, self.config)
        tools = run.toolset.schemas(step=state.step, state=state)
        used = state_tokens(state, tools)
        if used > self.config.clear_at * window:
            self.clear(run, used=used, window=window)
            used = state_tokens(state, tools)
        memo = self.memo(state)
        grown = len(state.messages) - int(memo.get("messages_after") or 0)
        # Never twice on the same messages: a conversation that is still over the bar right
        # after a compaction (one enormous message) is not summarised again until it has grown.
        if used > self.config.summarise_at * window and grown >= 2:
            if self.summarise(run, used=used, window=window, tools=tools):
                memo["messages_after"] = len(state.messages)
        return None

    def clear(self, run: Any, *, used: int, window: int) -> int:
        """All but the newest `clear_keep` tool results become stubs, in one sweep; each
        original is kept at /large_tool_results/<call id>."""
        state = run.state
        memo = self.memo(state)
        cleared = set(memo.get("cleared") or [])
        indices = [i for i, m in enumerate(state.messages) if m.get("role") == "tool"]
        old = indices[:-self.config.clear_keep] if self.config.clear_keep > 0 else indices
        fs = self.fs(state)
        count = 0
        for index in old:
            message = state.messages[index]
            call_id = str(message.get("tool_call_id") or "")
            body = str(message.get("content") or "")
            if call_id in cleared or len(body) < 400:
                continue
            path = runfs.offload_path(call_id)
            try:
                already = json.loads(body)
            except ValueError:
                already = None
            if not (isinstance(already, dict) and already.get("status") == "offloaded"):
                fs.put(path, body, meta={"tool": "cleared"})
            state.messages[index] = {**message, "content": json.dumps(
                {"status": "cleared", "path": path, "note": CLEARED_NOTE.format(path=path)})}
            cleared.add(call_id)
            count += 1
        if count:
            memo["cleared"] = sorted(cleared)
            run.rec.emit("context.compacted", {"how": "clear", "results": count,
                                               "before_tokens": used, "window": window})
        return count

    def summarise(self, run: Any, *, used: int, window: int, tools: list) -> bool:
        state = run.state
        memo = self.memo(state)
        keep_tokens = max(256, int(self.config.keep * window))
        reused = self._reuse_thread_memo(state, window=window, tools=tools)
        if reused:
            run.rec.emit("context.compacted", {"how": "summary", "reused": True,
                                               "before_tokens": used,
                                               "after_tokens": state_tokens(state, tools),
                                               "window": window})
            return True
        generation = int(memo.get("compactions") or 0) + 1
        path = f"{HISTORY_DIR}/{generation:03d}.md"
        first_number = int(memo.get("archived_messages") or 0) + 1
        original = list(state.messages)

        def summarise(folded: list) -> str:
            return self._summary(run, folded, budget_tokens=keep_tokens)

        out = compact_messages(original, keep_tokens=keep_tokens, archive_path=path,
                               summarise=summarise, first_number=first_number)
        if out is None:
            return False
        fs = self.fs(state)
        fs.put(path, out["archive"], meta={"tool": "conversation_history"})
        state.messages[:] = out["messages"]
        memo["compactions"] = generation
        memo["archived_messages"] = first_number - 1 + len(out["archived"])
        self._remember_thread_memo(original, out, path=path)
        run.rec.emit("context.compacted", {"how": "summary", "archived": len(out["archived"]),
                                           "path": path, "before_tokens": used,
                                           "after_tokens": state_tokens(state, tools),
                                           "window": window,
                                           "summariser": memo.get("summariser_used", "")})
        return True

    def _summary(self, run: Any, folded: list, *, budget_tokens: int) -> str:
        """The governed model summary, else the extractive digest."""
        memo = self.memo(run.state)
        if self.config.summariser == "model":
            text = self._model_summary(run, folded, budget_tokens=budget_tokens)
            if text:
                memo["summariser_used"] = "model"
                return text
        memo["summariser_used"] = "extractive"
        return extractive_summary(folded, budget_tokens=budget_tokens)

    def _model_summary(self, run: Any, folded: list, *, budget_tokens: int) -> str:
        transcript = archive_markdown(folded)
        # The summariser's own input must fit: past half the window it reads the digest.
        if tokens_of(transcript) > window_of(run.engine, self.config) // 2:
            transcript = extractive_summary(folded, budget_tokens=window_of(
                run.engine, self.config) // 3)
        request = ModelRequest(system=[Block(text=SUMMARY_SYSTEM)],
                               messages=[{"role": "user", "content": transcript}], tools=[],
                               max_output_tokens=max(256, min(4_096, budget_tokens)))
        call = ModelCall(request=request)
        call.notes["purpose"] = "context.summarise"
        try:
            called = run.chain.model_call(run, call, run.complete)
        except Exception:  # noqa: BLE001 — a summary that cannot be had is the digest
            return ""
        turn = getattr(called, "turn", None)
        if turn is None:
            return ""
        run.state.add_usage(turn.usage)
        # THE SUMMARY IS A RECEIPTED CALL: model.response is what the ledger sink turns into
        # the run's llm_calls row (the loop emits it for the turns it drives; this call is
        # the context policy's own).
        run.rec.emit("model.response", {"text": (turn.text or "")[:2000], "tool_calls": [],
                                        "stop_reason": turn.stop_reason,
                                        "purpose": "context.summarise"},
                     usage=turn.usage, latency_ms=int(getattr(called, "latency_ms", 0) or 0))
        return (turn.text or "").strip()

    # ── a chat thread's compaction, reused across its turns ─────────────────────────────────
    def _remember_thread_memo(self, original: list, out: dict, *, path: str) -> None:
        key = self.config.thread_key
        if not key:
            return
        upto = out["cut"]
        with _THREAD_MEMO_LOCK:
            _THREAD_MEMO[key] = {"upto": upto, "pin": out["pin"],
                                 "digest": _digest(original[:upto]),
                                 "summary": out["summary"], "archived": out["archived"],
                                 "path": path}
            _THREAD_MEMO.move_to_end(key)
            while len(_THREAD_MEMO) > _THREAD_MEMO_MAX:
                _THREAD_MEMO.popitem(last=False)

    def _reuse_thread_memo(self, state: Any, *, window: int, tools: list) -> bool:
        """A chat thread whose last turn already summarised the same opening messages: the
        same summary stands in for them (no model call), and the cut stays where it was — the
        prefix the next turns send is the same bytes."""
        key = self.config.thread_key
        if not key:
            return False
        with _THREAD_MEMO_LOCK:
            memo = _THREAD_MEMO.get(key)
        if not memo:
            return False
        upto = int(memo["upto"])
        if upto > len(state.messages) or _digest(state.messages[:upto]) != memo["digest"]:
            return False
        pin = int(memo["pin"])
        head = SUMMARY_HEAD.format(count=len(memo["archived"]), path=memo["path"])
        note = head + memo["summary"]
        lead = list(state.messages[:pin])
        if lead and lead[-1].get("role") == "user":
            lead[-1] = {**lead[-1], "content": f"{_content_text(lead[-1])}\n\n{note}"}
            new = lead + list(state.messages[upto:])
        else:
            new = lead + [{"role": "user", "content": note}] + list(state.messages[upto:])
        if state_tokens(_with_messages(state, new), tools) > self.config.summarise_at * window:
            return False  # the tail itself has grown past the bar: a fresh compaction
        self.fs(state).put(memo["path"], archive_markdown(memo["archived"], first_number=pin + 1),
                           meta={"tool": "conversation_history"})
        state.messages[:] = new
        ctx = self.memo(state)
        ctx["compactions"] = int(ctx.get("compactions") or 0) + 1
        ctx["archived_messages"] = len(memo["archived"])
        ctx["summariser_used"] = "reused"
        return True


class _StateView:
    def __init__(self, state: Any, messages: list):
        self.system = state.system
        self.messages = messages


def _with_messages(state: Any, messages: list) -> Any:
    return _StateView(state, messages)


def thread_memo_clear() -> None:
    """Tests: forget every chat thread's compaction."""
    with _THREAD_MEMO_LOCK:
        _THREAD_MEMO.clear()


def compact_state(state: Any, *, window: int, config: Optional[ContextConfig] = None) -> bool:
    """ClipContext.compact's body: the extractive compaction, no model call (the protocol's
    `compact(state, engine)` holds no governed path to a model)."""
    config = config or ContextConfig(summariser="extractive")
    if state_tokens(state) <= config.summarise_at * window:
        return False
    keep_tokens = max(256, int(config.keep * window))
    memo = state.extra.setdefault("_ctx", {"compactions": 0, "cleared": []})
    generation = int(memo.get("compactions") or 0) + 1
    path = f"{HISTORY_DIR}/{generation:03d}.md"
    out = compact_messages(list(state.messages), keep_tokens=keep_tokens, archive_path=path,
                           summarise=lambda folded: extractive_summary(
                               folded, budget_tokens=keep_tokens))
    if out is None:
        return False
    runfs.RunFS(state).put(path, out["archive"], meta={"tool": "conversation_history"})
    state.messages[:] = out["messages"]
    memo["compactions"] = generation
    return True


__all__ = ["CLEAR_AT", "CLEAR_KEEP", "DIGEST_HEAD", "ContextConfig", "ContextPolicyMiddleware", "HISTORY_DIR",
           "KEEP", "OFFLOAD_TOKENS", "PREVIEW_LINES", "SUMMARISE_AT", "archive_markdown",
           "choose_cut", "compact_messages", "compact_state", "extractive_summary",
           "full_render", "offload_threshold_chars", "render_for", "state_tokens",
           "thread_memo_clear", "tokens_of", "window_of"]
