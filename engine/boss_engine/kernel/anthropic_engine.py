"""THE ANTHROPIC MESSAGES ENGINE (K7): native Claude tool calling for API-key installs.

Today a Claude chat turn on an API key is a `claude -p` subprocess with its tools switched off
and a JSON object parsed back out of prose (claude_cli_adapter.py). This engine is the kernel's
native seam instead (design §2.3, §4.2): one `/v1/messages` call per step, with the provider's
own tool calling, strict schemas, explicit prompt-cache breakpoints, an exact token counter and
true streaming. Nothing selects it until K6 moves chat onto the kernel; until then it is reached
only behind `BOSS_CHAT_ANTHROPIC_NATIVE` (model_routing.kernel_chat_engine).

THE WIRE FORMAT CONVERTS AT THE EDGE. The kernel's checkpoint shape stays the OpenAI chat shape
(engine.py's module docstring; K4 depends on it), so this engine converts on the way in and out:

  in   system Blocks -> Anthropic `system` text blocks, `cache_control` where Block.cache;
       ToolSchemas -> `input_schema` tools, `strict` where the schema qualifies, `defer_loading`
       on deferred tools plus the provider's BM25 tool-search tool;
       messages -> `user` turns, `assistant` turns, tool-role messages folded into `tool_result`
       user turns (every run of tool messages becomes ONE user message, as the API requires).
  out  ModelTurn whose `raw` is the OpenAI-shaped assistant message the kernel appends, carrying
       the provider-native content blocks beside it (`anthropic_content`) so thinking signatures
       and server-tool blocks survive the round trip verbatim.

THE KEY IS NEVER THIS ENGINE'S TO HOLD. `engine_for` resolves it the way every Claude engine
does (claude_credential.resolve_anthropic_api_key: console vault first, the install's
environment last) and immediately trades it for a model-proxy run token (model_proxy.shield's
path), so what this engine carries is a revocable loopback credential, exactly like a coding
session. There is no environment read of its own.

RETRY IS THE KERNEL'S JOB. The engine classifies and raises; it never loops. A 429/408/5xx/
connection failure is raised as ProviderTransient and an invalid request as ProviderPermanent —
`errors.classify` reads the class off `boss_error_kind` — except a provider's refusal of a
malformed tool call, which is re-raised bare so the kernel's refused-call feedback
(errors.provider_refused_the_call) stays one behaviour for every engine.

Anthropic is imported lazily: the kernel package stays importable where the SDK is not
installed, and ScriptedEngine-only tests never load it.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Iterable, Mapping, Optional

from .engine import Block, CancelToken, EngineTraits, ModelRequest, estimate_tokens, _usage_of
from .errors import Cancelled, provider_refused_the_call
from .types import ModelTurn, ToolCall

#: What the Messages API can do natively (design §2.3). `context_window` is the floor every
#: served Claude model clears; the ContextPolicy may know a larger per-model window (K9).
TRAITS = EngineTraits(native_tools=True, strict_tools=True, tool_search=True,
                      prompt_cache="explicit", context_window=200_000, count_tokens=True,
                      streams=True, family="anthropic")

#: The chat default when neither the router nor a knob names a model: the same model the Agent
#: SDK engine defaults to (agent_sdk_engine.DEFAULT_MODEL), so "Claude on an API key" means one
#: thing across BOSS.
DEFAULT_MODEL = "claude-sonnet-5"

#: `max_tokens` is required by the API. Chat answers are short; a caller that wants more passes
#: ModelRequest.max_output_tokens.
DEFAULT_MAX_OUTPUT_TOKENS = 8_192

#: The API allows at most 4 cache breakpoints per request.
MAX_CACHE_BREAKPOINTS = 4

#: The provider-side tool search pair (design §5.2): eligible non-core tools are sent with
#: `defer_loading: true` and this server tool reveals them, so the tool list — and with it the
#: cache prefix — never changes mid-run.
TOOL_SEARCH_TOOL = {"type": "tool_search_tool_bm25_20251119", "name": "tool_search_tool_bm25"}

#: The provider-native content blocks ride beside the OpenAI-shaped assistant message under this
#: key, replayed verbatim on the next request (thinking signatures, server-tool blocks).
NATIVE_CONTENT_KEY = "anthropic_content"


class ProviderTransient(RuntimeError):
    """Overloaded (529), rate-limited (429), 5xx, a timeout or a connection failure: the same
    call again shortly. errors.classify reads `boss_error_kind`."""

    boss_error_kind = "transient"


class ProviderPermanent(RuntimeError):
    """The provider refused the request itself (an invalid request, bad auth): retrying spends
    the wait twice. The run fails carrying the provider's words."""

    boss_error_kind = "fatal"


class NoAnthropicKey(LookupError):
    """This install holds no Anthropic API key, so there is nothing to build the engine on."""


# ── Request conversion ────────────────────────────────────────────────────────────────────────

def _system_param(blocks: Iterable[Any]) -> list:
    """Block values as Anthropic system text blocks, `cache_control` where Block.cache.

    THE STABLE PREFIX COMES FIRST, BY CONTRACT. A cached block after an uncached one would put a
    breakpoint behind changing state — every later request misses it and pays the write again —
    so that order is refused loudly here rather than discovered in a cache-hit-rate graph
    (design §4.3: stable prefix first, changing state last)."""
    out: list = []
    changing_seen = False
    for block in blocks:
        text = block.text if isinstance(block, Block) else str(block)
        if not text:
            continue
        cache = bool(getattr(block, "cache", False))
        if cache and changing_seen:
            raise ValueError("a cached system block follows an uncached one: put the stable "
                             "prefix first and the changing state last (design §4.3)")
        changing_seen = changing_seen or not cache
        entry: dict = {"type": "text", "text": text}
        if cache:
            entry["cache_control"] = {"type": "ephemeral"}
        out.append(entry)
    return _capped_breakpoints(out)


def _capped_breakpoints(blocks: list) -> list:
    """At most 3 system breakpoints (the 4th is the tool list's), keeping the LAST marked blocks:
    the longest prefixes are the ones worth a breakpoint."""
    marked = [i for i, b in enumerate(blocks) if "cache_control" in b]
    for i in marked[: max(0, len(marked) - (MAX_CACHE_BREAKPOINTS - 1))]:
        del blocks[i]["cache_control"]
    return blocks


def _strict_ok(parameters: Mapping[str, Any]) -> bool:
    """Whether a schema qualifies for `strict: true` (additionalProperties false and every
    property required). K5's strict_schema derivation will make every kernel tool qualify;
    until then a non-conforming schema is sent unstrict rather than refused by the API."""
    if parameters.get("additionalProperties") is not False:
        return False
    properties = parameters.get("properties")
    required = parameters.get("required") or []
    if not isinstance(properties, Mapping):
        return False
    return set(properties) == set(required)


def _tools_param(req: ModelRequest, *, strict: bool, cache: bool) -> list:
    """ToolSchemas as Anthropic tools: core tools first (a breakpoint on the last, so the tool
    prefix caches), then the search tool and the deferred tools, which carry no cache_control
    (the API refuses that combination) and never change the prefix before them."""
    core = [t for t in req.tools if not getattr(t, "deferred", False)]
    deferred = [t for t in req.tools if getattr(t, "deferred", False)] if core else []

    def tool(schema: Any, *, defer: bool = False) -> dict:
        entry: dict = {"name": schema.name, "description": schema.description,
                       "input_schema": dict(schema.parameters)}
        if strict and _strict_ok(entry["input_schema"]):
            entry["strict"] = True
        if defer:
            entry["defer_loading"] = True
        return entry

    out = [tool(t) for t in core] or [tool(t) for t in req.tools]
    if cache and out:
        out[-1]["cache_control"] = {"type": "ephemeral"}
    if deferred:
        out.append(dict(TOOL_SEARCH_TOOL))
        out.extend(tool(t, defer=True) for t in deferred)
    return out


def _tool_choice(req: ModelRequest) -> Optional[dict]:
    if not req.tools:
        return None
    choice = req.tool_choice
    if choice == "auto":
        return None
    if choice == "required":
        return {"type": "any"}
    if choice == "none":
        return {"type": "none"}
    return {"type": "tool", "name": str(choice)}


def _assistant_content(message: Mapping[str, Any]) -> list:
    """An OpenAI-shaped assistant message as Anthropic content blocks: the native blocks it
    carried if this engine produced it, rebuilt from text + tool_calls otherwise (a checkpoint
    another engine wrote)."""
    native = message.get(NATIVE_CONTENT_KEY)
    if isinstance(native, list) and native:
        return [dict(b) for b in native]
    blocks: list = []
    text = message.get("content")
    if isinstance(text, str) and text:
        blocks.append({"type": "text", "text": text})
    for call in message.get("tool_calls") or []:
        fn = call.get("function") if isinstance(call.get("function"), Mapping) else call
        raw = fn.get("arguments")
        if isinstance(raw, Mapping):
            arguments: dict = dict(raw)
        else:
            try:
                value = json.loads(str(raw or "") or "{}")
                arguments = value if isinstance(value, dict) else {}
            except ValueError:
                arguments = {}
        blocks.append({"type": "tool_use", "id": str(call.get("id") or ""),
                       "name": str(fn.get("name") or ""), "input": arguments})
    return blocks or [{"type": "text", "text": ""}]


def _messages_param(req: ModelRequest) -> tuple[list, list]:
    """(system blocks, messages): the OpenAI conversation as Messages-API turns. Each RUN of
    tool-role messages becomes one `user` message of tool_result blocks — the API wants every
    tool_use answered in the single next user turn, and the kernel answers every call
    (step_tools), so the fold is total. A stray system message joins the system blocks."""
    system = _system_param(req.system)
    out: list = []
    results: list = []

    def flush() -> None:
        if results:
            out.append({"role": "user", "content": list(results)})
            results.clear()

    for message in req.messages:
        role = str(message.get("role") or "")
        if role == "tool":
            results.append({"type": "tool_result",
                            "tool_use_id": str(message.get("tool_call_id") or ""),
                            "content": str(message.get("content") or "")})
            continue
        flush()
        if role == "system":
            text = str(message.get("content") or "")
            if text:
                system.append({"type": "text", "text": text})
        elif role == "assistant":
            out.append({"role": "assistant", "content": _assistant_content(message)})
        else:
            out.append({"role": "user", "content": str(message.get("content") or "")})
    flush()
    return system, out


# ── Response conversion ───────────────────────────────────────────────────────────────────────

_STOP_REASONS = {"tool_use": "tool_use", "max_tokens": "max_tokens", "refusal": "refusal"}


def _block_dict(block: Any) -> dict:
    """A response content block as a plain, JSON-able dict that the API accepts back verbatim."""
    if isinstance(block, Mapping):
        return dict(block)
    dump = getattr(block, "model_dump", None)
    if callable(dump):
        return dump(exclude_none=True)
    return {"type": "text", "text": str(block)}


def _turn_from(blocks: list, stop_reason: Optional[str], usage: dict, model: str) -> ModelTurn:
    text = "".join(b.get("text") or "" for b in blocks if b.get("type") == "text")
    calls = [ToolCall(id=str(b.get("id") or ""), name=str(b.get("name") or ""),
                      arguments=dict(b.get("input") or {}),
                      raw_arguments=json.dumps(b.get("input") or {}))
             for b in blocks if b.get("type") == "tool_use"]
    raw: dict = {"role": "assistant", "content": text or None, NATIVE_CONTENT_KEY: blocks}
    if calls:
        raw["tool_calls"] = [c.as_openai() for c in calls]
    adapter_usage = {"model": model, "provider": "anthropic",
                     "prompt_tokens": int(usage.get("input_tokens") or 0),
                     "completion_tokens": int(usage.get("output_tokens") or 0),
                     "cache_read_tokens": int(usage.get("cache_read_input_tokens") or 0),
                     "cache_write_tokens": int(usage.get("cache_creation_input_tokens") or 0)}
    adapter_usage["total_tokens"] = (adapter_usage["prompt_tokens"]
                                     + adapter_usage["completion_tokens"])
    return ModelTurn(text=text, tool_calls=calls,
                     stop_reason=(_STOP_REASONS.get(str(stop_reason or ""))
                                  or ("tool_use" if calls else "end_turn")),
                     usage=_usage_of(adapter_usage), raw=raw)


def _usage_dict(usage: Any) -> dict:
    if usage is None:
        return {}
    if isinstance(usage, Mapping):
        return dict(usage)
    dump = getattr(usage, "model_dump", None)
    return dump(exclude_none=True) if callable(dump) else {}


def _raise_typed(error: BaseException) -> None:
    """One provider failure, typed for errors.classify. A refusal of a malformed tool call is
    re-raised bare so every engine shares the kernel's refused-call feedback."""
    import anthropic

    detail = f"{type(error).__name__}: {error}"[:600]
    if provider_refused_the_call(error) is not None:
        raise error
    if isinstance(error, (anthropic.APIConnectionError, anthropic.APITimeoutError)):
        raise ProviderTransient(detail) from error
    if isinstance(error, anthropic.APIStatusError):
        status = int(getattr(error, "status_code", 0) or 0)
        if status in (408, 429) or status >= 500:
            raise ProviderTransient(detail) from error
        raise ProviderPermanent(detail) from error
    raise error


# ── The engine ────────────────────────────────────────────────────────────────────────────────

class AnthropicMessagesEngine:
    """The kernel Engine protocol over `/v1/messages`.

    `client` is an `anthropic.Anthropic` already pointed at its credential — in BOSS that is a
    model-proxy run token on a loopback base URL (`engine_for`), in tests a client on a stubbed
    httpx transport. The client's own retries stay OFF (engine_for builds it with
    `max_retries=0`): the kernel's RecoveryPolicy owns every retry, so each attempt is a visible
    `model.retry` event and its own budget receipt.

    Streaming: with `on_delta` or a CancelToken the request streams, text deltas reach
    `on_delta` as they arrive, and the token is honoured BETWEEN EVENTS — a cancelled run stops
    mid-answer, not after it. Without either, one plain request."""

    def __init__(self, client: Any, *, model: str = DEFAULT_MODEL, name: str = "anthropic",
                 traits: Optional[EngineTraits] = None,
                 max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
                 on_close: Optional[Callable[[], None]] = None):
        self.client = client
        self.model = model or DEFAULT_MODEL
        self.name = name
        self.traits = traits or TRAITS
        self.max_output_tokens = int(max_output_tokens)
        self._on_close = on_close

    def close(self) -> None:
        """Revoke the engine's model-proxy token (engine_for wires this); safe to call twice."""
        on_close, self._on_close = self._on_close, None
        if on_close is not None:
            on_close()

    # ── the protocol ──────────────────────────────────────────────────────────────────────
    def complete(self, req: ModelRequest, *, on_delta: Optional[Callable[[str], None]] = None,
                 cancel: Optional[CancelToken] = None) -> ModelTurn:
        kwargs = self._request_kwargs(req)
        try:
            if on_delta is not None or cancel is not None:
                return self._stream(kwargs, on_delta=on_delta, cancel=cancel)
            response = self.client.messages.create(**kwargs)
        except Cancelled:
            raise
        except Exception as error:  # noqa: BLE001 — typed for the RecoveryPolicy
            _raise_typed(error)
        blocks = [_block_dict(b) for b in response.content]
        return _turn_from(blocks, getattr(response, "stop_reason", None),
                          _usage_dict(getattr(response, "usage", None)),
                          str(getattr(response, "model", "") or self.model))

    def count(self, req: ModelRequest) -> int:
        """Exact, through `/v1/messages/count_tokens` (the proxy allows it); the chars/4
        estimate only when the counter itself fails — a count must never fail a run."""
        kwargs = self._request_kwargs(req)
        kwargs.pop("max_tokens", None)
        kwargs.pop("tool_choice", None)
        try:
            counted = self.client.messages.count_tokens(**kwargs)
            return int(counted.input_tokens)
        except Exception:  # noqa: BLE001 — an estimate beats a failed step
            return estimate_tokens(req)

    # ── the request ───────────────────────────────────────────────────────────────────────
    def _request_kwargs(self, req: ModelRequest) -> dict:
        system, messages = _messages_param(req)
        kwargs: dict = {"model": self.model, "messages": messages,
                        "max_tokens": int(req.max_output_tokens or self.max_output_tokens)}
        if system:
            kwargs["system"] = system
        tools = _tools_param(req, strict=self.traits.strict_tools,
                             cache=self.traits.prompt_cache == "explicit"
                             and any(getattr(b, "cache", False) for b in req.system))
        if tools:
            kwargs["tools"] = tools
            choice = _tool_choice(req)
            if choice is not None:
                kwargs["tool_choice"] = choice
        return kwargs

    # ── streaming ─────────────────────────────────────────────────────────────────────────
    def _stream(self, kwargs: dict, *, on_delta: Optional[Callable[[str], None]],
                cancel: Optional[CancelToken]) -> ModelTurn:
        blocks: list = []
        partial: dict = {}
        usage: dict = {}
        stop_reason: Optional[str] = None
        model = self.model
        try:
            stream = self.client.messages.create(stream=True, **kwargs)
        except Exception as error:  # noqa: BLE001
            _raise_typed(error)
        try:
            for event in stream:
                if cancel is not None and cancel.is_set():
                    raise Cancelled(cancel.reason or "cancelled")
                kind = str(getattr(event, "type", "") or "")
                if kind == "message_start":
                    message = getattr(event, "message", None)
                    usage.update(_usage_dict(getattr(message, "usage", None)))
                    model = str(getattr(message, "model", "") or model)
                elif kind == "content_block_start":
                    block = _block_dict(getattr(event, "content_block", None) or {})
                    blocks.append(block)
                    if block.get("type") == "tool_use":
                        partial[len(blocks) - 1] = ""
                elif kind == "content_block_delta":
                    delta = getattr(event, "delta", None)
                    dkind = str(getattr(delta, "type", "") or "")
                    index = int(getattr(event, "index", len(blocks) - 1))
                    if dkind == "text_delta" and blocks:
                        text = str(getattr(delta, "text", "") or "")
                        blocks[index]["text"] = (blocks[index].get("text") or "") + text
                        if on_delta is not None and text:
                            on_delta(text)
                    elif dkind == "input_json_delta" and index in partial:
                        partial[index] += str(getattr(delta, "partial_json", "") or "")
                    elif dkind == "thinking_delta" and blocks:
                        blocks[index]["thinking"] = ((blocks[index].get("thinking") or "")
                                                     + str(getattr(delta, "thinking", "") or ""))
                    elif dkind == "signature_delta" and blocks:
                        blocks[index]["signature"] = str(getattr(delta, "signature", "") or "")
                elif kind == "message_delta":
                    stop_reason = str(getattr(getattr(event, "delta", None), "stop_reason", "")
                                      or "") or stop_reason
                    usage.update(_usage_dict(getattr(event, "usage", None)))
        except Cancelled:
            raise
        except Exception as error:  # noqa: BLE001
            _raise_typed(error)
        finally:
            close = getattr(stream, "close", None)
            if callable(close):
                close()
        for index, text in partial.items():
            try:
                value = json.loads(text or "{}")
            except ValueError:
                value = {}
            blocks[index]["input"] = value if isinstance(value, dict) else {}
        return _turn_from(blocks, stop_reason, usage, model)


# ── Building one inside BOSS ──────────────────────────────────────────────────────────────────

def engine_for(settings: Any, *, connection: Any = None, project_id: Optional[str] = None,
               model: Optional[str] = None, proxy: Any = None) -> AnthropicMessagesEngine:
    """The engine on this install's own Anthropic API key, through the model proxy.

    The key is resolved exactly as the Agent SDK engine resolves it (console vault, else the
    install's environment; never the subscription login) and immediately exchanged for a
    model-proxy run token, so the engine holds a revocable loopback credential and the real key
    stays in BOSS (model_proxy's contract). Raises NoAnthropicKey when the install holds none —
    the routing hook reads that as "keep today's adapter"."""
    from ..services import model_proxy
    from ..services.claude_credential import resolve_anthropic_api_key

    try:
        cred = resolve_anthropic_api_key(settings, connection=connection, project_id=project_id)
    except Exception as error:  # noqa: BLE001 — a store that did not answer holds no key now
        raise NoAnthropicKey(f"the credential store did not answer: {type(error).__name__}") \
            from error
    if not getattr(cred, "is_key", False):
        raise NoAnthropicKey("this install holds no Anthropic API key")
    proxy = proxy or model_proxy.get()
    issued = proxy.issue("anthropic", cred.value, ttl=model_proxy.session_ttl(settings))
    import anthropic
    import httpx

    client = anthropic.Anthropic(
        api_key=issued.token, base_url=issued.base_url, max_retries=0,
        timeout=httpx.Timeout(connect=30.0, read=600.0, write=120.0, pool=30.0))
    return AnthropicMessagesEngine(client, model=model or DEFAULT_MODEL,
                                   on_close=lambda: proxy.revoke(issued.token))


__all__ = ["AnthropicMessagesEngine", "DEFAULT_MODEL", "NATIVE_CONTENT_KEY", "NoAnthropicKey",
           "ProviderPermanent", "ProviderTransient", "TOOL_SEARCH_TOOL", "TRAITS", "engine_for"]
