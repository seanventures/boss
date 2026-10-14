from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import random
import re
import time
from typing import Any, Iterator, Optional, Tuple

from openai import (
    APIConnectionError,
    APITimeoutError,
    AzureOpenAI,
    BadRequestError,
    OpenAI,
    RateLimitError,
)

from ..config import Settings
from .github_adapter import IntegrationConfigurationError
from .redaction import redact as _redact

_RETRYABLE = (RateLimitError, APITimeoutError, APIConnectionError)

#: Signatures of a provider rejecting the MODEL'S OWN OUTPUT, not our request.
#:
#: Observed live, and it killed an entire Ask BOSS turn:
#:
#:   GroqException - Tool call validation failed: attempted to call tool
#:   'functions/read_logs' which was not in request.tools
#:
#: The model emitted `functions/read_logs` instead of `read_logs` — a generation flake, and a
#: non-deterministic one. The provider reports it as HTTP 400, so it lands as BadRequestError
#: and is treated as "your request was malformed". Our request was fine: the tool list was
#: correct and `read_logs` was in it. The next sample would almost certainly name it properly.
#:
#: Deliberately NARROW. A real 400 — a bad schema, an unsupported parameter — must still fail
#: fast and loudly, because retrying it eight times just burns money to reach the same error.
#: Only these signatures, which can only be produced by the model's own tool call, are retried.
_MODEL_OUTPUT_REJECTED = (
    "tool call validation failed",
    "which was not in request.tools",
    "tool_use_failed",
    "failed_generation",
)


def is_model_output_rejection(exc: BaseException) -> bool:
    """True when a 400 is the provider refusing what the MODEL produced.

    Retrying is correct: nothing about the request needs to change, and the flake does not
    reproduce reliably.
    """
    if not isinstance(exc, BadRequestError):
        return False
    text = str(exc).lower()
    return any(marker in text for marker in _MODEL_OUTPUT_REJECTED)

# Groq free-tier TPM is 6k. Default native_max_output_tokens=8192 plus a packed
# repo context routinely *requests* ~12k and 429s before any token is generated.
# Cap request size (not just completion) when the model class is groq/qwen.
GROQ_QWEN_MAX_OUTPUT_TOKENS = 1024
GROQ_QWEN_CONTEXT_FILES = 4
GROQ_QWEN_CONTEXT_HEAD_BYTES = 3000
GROQ_QWEN_CONTEXT_MAX_BYTES = 12_000


def is_groq_qwen_model_class(
    model: Optional[str] = None,
    *,
    base_url: str = "",
    engine: str = "",
) -> bool:
    """True when the request will hit Groq or a Qwen model (incl. groq:qwen/…)."""
    blob = " ".join(str(part or "") for part in (model, base_url, engine)).lower()
    return "groq" in blob or "qwen" in blob


def capped_max_output_tokens(
    requested: int,
    model: Optional[str] = None,
    *,
    base_url: str = "",
    engine: str = "",
) -> int:
    """Shrink max_tokens so a Groq 6k TPM lane cannot ask for 8k+ output."""
    requested = int(requested or 0)
    if requested <= 0:
        return requested
    if is_groq_qwen_model_class(model, base_url=base_url, engine=engine):
        return min(requested, GROQ_QWEN_MAX_OUTPUT_TOKENS)
    return requested


def groq_qwen_context_limits(
    model: Optional[str] = None,
    *,
    base_url: str = "",
    engine: str = "",
    file_limit: int = 14,
) -> tuple[int, int, int]:
    """(file_limit, max_bytes, head_bytes) for the code-task context packer.

    ``file_limit <= 0`` means packing is disabled and must stay disabled.
    """
    requested = int(file_limit or 0)
    if is_groq_qwen_model_class(model, base_url=base_url, engine=engine):
        if requested <= 0:
            return (0, GROQ_QWEN_CONTEXT_MAX_BYTES, GROQ_QWEN_CONTEXT_HEAD_BYTES)
        return (
            min(requested, GROQ_QWEN_CONTEXT_FILES),
            GROQ_QWEN_CONTEXT_MAX_BYTES,
            GROQ_QWEN_CONTEXT_HEAD_BYTES,
        )
    return (requested, 400_000, 20_000)


_SINGLE_ATTEMPT: ContextVar[bool] = ContextVar("model_single_attempt", default=False)


@contextmanager
def single_attempt():
    """A governed invocation cannot resample or hide another SDK request on its receipt."""
    token = _SINGLE_ATTEMPT.set(True)
    try:
        yield
    finally:
        _SINGLE_ATTEMPT.reset(token)


def _with_retry(call, *, tries: int = 8, base: float = 2.0, stats: Optional[dict] = None):
    """Call an OpenAI request, retrying on 429 / transient errors with exponential backoff +
    jitter. Honours a Retry-After header when the provider supplies one. ``stats`` (when
    given) receives {"retries": n} — how many retries the call needed (0 = first try)."""
    if _SINGLE_ATTEMPT.get():
        tries = 1
    for attempt in range(tries):
        if stats is not None:
            stats["retries"] = attempt
        try:
            return call()
        except BadRequestError as exc:
            # A 400 that describes the MODEL's output, not ours. See _MODEL_OUTPUT_REJECTED.
            if not is_model_output_rejection(exc) or attempt == tries - 1:
                raise
            # Short, fixed backoff: this is a resample, not congestion. Waiting 45s to ask a
            # model to name its own tool correctly helps nobody.
            time.sleep(min(1.0 + attempt, 4.0))
            continue
        except _RETRYABLE as exc:
            # The SDK turns a refused send (a host credential bound to another provider, rc6 N-1)
            # into a retried "Connection error.": no retry changes the answer, so say it now.
            from .env_refs import misdirection_in

            misdirected = misdirection_in(exc)
            if misdirected is not None:
                raise misdirected from None
            if attempt == tries - 1:
                raise
            delay = base * (2 ** attempt) + random.uniform(0, 1)
            retry_after = getattr(getattr(exc, "response", None), "headers", {}) or {}
            try:
                delay = max(delay, float(retry_after.get("retry-after")))
            except (TypeError, ValueError):
                pass
            # A WAIT THE TURN CANNOT AFFORD IS NOT A RETRY (call_deadline.py): inside a chat
            # turn, sleeping past its deadline only to be refused is the slowest failure there is.
            from .call_deadline import MIN_CALL_SECONDS, TurnDeadlineExceeded, remaining

            left = remaining()
            if left is not None and left < min(delay, 45.0) + MIN_CALL_SECONDS:
                raise TurnDeadlineExceeded(
                    f"the provider asked BOSS to wait {min(delay, 45.0):.0f}s and this turn "
                    f"has {max(0, int(left))}s left") from exc
            time.sleep(min(delay, 45.0))

# USD per 1,000,000 tokens: (input, output). Turns real token usage into a
# dollar cost (micros) for budget settlement and reporting. Extend/correct per
# deployment via BOSS_MODEL_PRICES_JSON (a {"model": [in, out]} JSON object) —
# open-model prices vary by host (DeepSeek direct vs OpenRouter vs Fireworks).
PRICE_PER_MTOK: dict[str, tuple[float, float]] = {
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4.1": (2.00, 8.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-5.2-codex": (2.50, 10.00),
    "deepseek-chat": (0.27, 1.10),
    "deepseek-reasoner": (0.55, 2.19),
    "qwen2.5-coder-32b-instruct": (0.90, 0.90),
    "qwen-2.5-coder-32b-instruct": (0.90, 0.90),  # OpenRouter's spelling (qwen/qwen-2.5-…)
    "deepseek-chat:free": (0.0, 0.0),  # OpenRouter free variant — genuinely $0
    "qwen3-32b": (0.29, 0.59),  # Groq serving (groq:qwen/qwen3-32b)
    # Anthropic first-party API rates (the same on Foundry). A dated snapshot id
    # ("claude-sonnet-4-5-20250929") and the "anthropic:"/"anthropic/" spellings resolve here.
    "claude-fable-5-1": (10.00, 50.00),
    "claude-fable-5": (10.00, 50.00),
    "claude-opus-5-5": (4.00, 20.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-opus-4-7": (5.00, 25.00),
    "claude-opus-4-6": (5.00, 25.00),
    "claude-opus-4-5": (5.00, 25.00),
    "claude-opus-4-1": (15.00, 75.00),
    "claude-opus-4-0": (15.00, 75.00),
    "claude-opus-4": (15.00, 75.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-sonnet-4-5": (3.00, 15.00),
    "claude-sonnet-4-0": (3.00, 15.00),
    "claude-sonnet-4": (3.00, 15.00),
    "claude-3-7-sonnet": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-3-5-haiku": (0.80, 4.00),
    "claude-3-haiku": (0.25, 1.25),
}
DEFAULT_PRICE = (2.50, 10.00)
#: What a reply BOSS has no price for is charged at, labelled "unpriced": the dearest per-token
#: rate in the table above, so an estimate never under-counts a known-good but unpriced reply.
UNPRICED_ESTIMATE_PRICE = (15.00, 75.00)

_DATED_SNAPSHOT = re.compile(r"[-@]\d{8}$")


def _anthropic_name(model: str) -> str:
    """"claude-sonnet-4-5-20250929", "claude-sonnet-4-5@20250929", "anthropic/claude-sonnet-4.5"
    and "claude-sonnet-4-5-latest" all name the priced "claude-sonnet-4-5"."""
    name = model.strip().lower().rsplit("/", 1)[-1]
    if not name.startswith("claude-"):
        return ""
    name = _DATED_SNAPSHOT.sub("", name)
    name = name.removesuffix("-latest").replace(".", "-")
    return name


def _price_overrides() -> dict[str, tuple[float, float]]:
    from ..config import get_settings

    raw = (get_settings().model_prices_json or "").strip()
    if not raw:
        return {}
    if raw != _price_overrides_cache.get("raw"):
        try:
            import json as _json

            parsed = {
                str(k): (float(v[0]), float(v[1]))
                for k, v in _json.loads(raw).items()
            }
        except Exception:  # noqa: BLE001 — a bad override must not break cost accounting
            parsed = {}
        _price_overrides_cache.update({"raw": raw, "parsed": parsed})
    return _price_overrides_cache.get("parsed", {})


_price_overrides_cache: dict = {}


def endpoint_price_key(base_url: str, model: str) -> str:
    """The BOSS_MODEL_PRICES_JSON key that prices MODEL only at one endpoint.

    "http://host.docker.internal:11434/v1#qwen2.5-coder:7b" prices the local Ollama model
    and nothing else: the same model name served by a hosted provider never matches it.
    """
    return f"{(base_url or '').strip().rstrip('/')}#{model}"


def price_for_model(model: str, provider: str = "", endpoint: str = "") -> tuple[float, float, bool]:
    """(input_$/Mtok, output_$/Mtok, known). Lookup order (BOSS-280 P2):
    "<base url>#model" (one endpoint only, e.g. a local Ollama priced at zero) →
    "provider:model" (Azure/OpenRouter/self-hosted price the SAME model differently — set
    e.g. {"azure:gpt-4o": [3.0, 12.0]} in BOSS_MODEL_PRICES_JSON) → exact model → basename
    of provider-prefixed names ("deepseek/deepseek-chat"). Overrides always win.
    known=False means the DEFAULT_PRICE fallback was used — treat that cost as an estimate."""
    table = {**PRICE_PER_MTOK, **_price_overrides()}
    keys = []
    if endpoint:
        keys.append(endpoint_price_key(endpoint, model))
    if provider:
        keys.append(f"{provider.strip().lower()}:{model}")
    keys += [model, model.rsplit("/", 1)[-1]]
    anthropic = _anthropic_name(model)
    if anthropic:
        keys += ([f"{provider.strip().lower()}:{anthropic}"] if provider else []) + [anthropic]
    for key in keys:
        if key in table:
            return (*table[key], True)
    # OPENROUTER PUBLISHES ITS PRICES (openrouter_catalog): a listed model's cost is known,
    # not the DEFAULT_PRICE guess. In-process cache only — never a fetch on this path.
    if provider.strip().lower() == "openrouter" or "/" in model:
        try:
            from .openrouter_catalog import price as _openrouter_price

            listed = _openrouter_price(model)
        except Exception:  # noqa: BLE001
            listed = None
        if listed is not None:
            return (listed[0], listed[1], True)
    return (*DEFAULT_PRICE, False)


# Cache pricing multipliers vs the input rate (Anthropic: reads 0.1x, 5-min writes 1.25x;
# OpenAI cached input is 0.25-0.5x — 0.1/1.25 is the conservative Anthropic default since
# that's the engine most of our cache traffic rides). Override via env if a provider differs.
CACHE_READ_MULT = 0.1
CACHE_WRITE_MULT = 1.25


def estimate_cost_usd_micros(usage: dict) -> int:
    """micros = prompt*input + completion*output + cache_read*input*0.1 + cache_write*input*1.25.

    If the backend already reported an exact cost (e.g. the Claude CLI returns
    total_cost_usd per call), honor that instead of the token price table."""
    if usage.get("cost_usd_micros") is not None:
        return int(usage["cost_usd_micros"])
    inp, out, _ = price_for_model(str(usage.get("model") or ""),
                                  provider=str(usage.get("provider") or ""),
                                  endpoint=str(usage.get("base_url") or ""))
    prompt = int(usage.get("prompt_tokens") or 0)
    completion = int(usage.get("completion_tokens") or 0)
    cache_read = int(usage.get("cache_read_tokens") or 0)
    cache_write = int(usage.get("cache_write_tokens") or 0)
    return int(round(prompt * inp + completion * out
                     + cache_read * inp * CACHE_READ_MULT
                     + cache_write * inp * CACHE_WRITE_MULT))


def _usage_from_response(response, model: str) -> dict:
    u = getattr(response, "usage", None)
    prompt = getattr(u, "prompt_tokens", None) or getattr(u, "input_tokens", None) or 0
    completion = (
        getattr(u, "completion_tokens", None) or getattr(u, "output_tokens", None) or 0
    )
    total = getattr(u, "total_tokens", None) or (int(prompt) + int(completion))
    return {
        "model": model,
        "prompt_tokens": int(prompt),
        "completion_tokens": int(completion),
        "total_tokens": int(total),
    }


#: Providers this adapter reaches that run on the operator's own hardware: no per-call money.
_LOCAL_PROVIDERS = frozenset({"ollama", "vllm"})


class OpenAIAdapter:
    def __init__(
        self,
        settings: Settings,
        *,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        connection=None,
        project_id: Optional[str] = None,
        provider: str = "openai",
    ):
        self.settings = settings
        self._base_url_override = (base_url or "").strip() or None
        self._api_key_override = api_key
        self._connection = connection
        self._project_id = (project_id or "").strip() or None
        self._provider = (provider or "openai").strip() or "openai"

    def _stamp_billing(self, usage: dict) -> None:
        """Say whether the call was money (included_spend.py). A local model is not; every
        hosted endpoint this adapter reaches is metered."""
        from .included_spend import BILLING_LOCAL, BILLING_METERED

        usage["billing"] = (BILLING_LOCAL if self._provider.lower() in _LOCAL_PROVIDERS
                            else BILLING_METERED)

    def _resolved_base_url(self) -> str:
        if self._base_url_override:
            return self._base_url_override
        return (self.settings.openai_base_url or "").strip()

    def _resolved_api_key(self) -> Optional[str]:
        if self._api_key_override is not None:
            return self._api_key_override
        if self._connection is not None and self._project_id:
            from .credential_resolve import resolve_project_credential

            return resolve_project_credential(
                self._connection,
                self.settings,
                project_id=self._project_id,
                provider=self._provider,
                env_fallback_attr="openai_api_key",
            )
        return self.settings.openai_api_key

    def _azure_enabled(self) -> bool:
        return bool(
            self.settings.azure_openai_key
            and self.settings.azure_openai_endpoint
            and self.settings.azure_openai_deployment
        )

    def _openai_client(self) -> OpenAI:
        """Plain-OpenAI client honoring OPENAI_BASE_URL, so the HOST-side native engine can
        target any OpenAI-compatible provider (OpenRouter/Fireworks/DeepSeek/local) — the
        same knob the guest path already honors. Empty base_url => SDK default (api.openai.com).
        Azure keeps precedence via _azure_enabled(); base_url is ignored when Azure is configured."""
        kwargs: dict = {"api_key": self._resolved_api_key()}
        base = self._resolved_base_url()
        if base:
            kwargs["base_url"] = base
        # rc6 N-1: refuse a host credential bound to another provider here, by name and host,
        # before the SDK hides it behind retries and "Connection error." (env_refs.check_outbound).
        from .env_refs import check_outbound

        if kwargs["api_key"]:
            check_outbound(base or "https://api.openai.com/v1",
                           {"Authorization": f"Bearer {kwargs['api_key']}"})
        if _SINGLE_ATTEMPT.get():
            kwargs["max_retries"] = 0
        return OpenAI(**kwargs)

    def create_text_response(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        model: Optional[str] = None,
        redact: bool = False,
    ) -> Tuple[str, str, dict]:
        # Data-boundary safety: redact PII before the prompt crosses to the external model.
        if redact:
            prompt, _ = _redact(prompt)
        if self._azure_enabled():
            client = AzureOpenAI(
                api_key=self.settings.azure_openai_key,
                azure_endpoint=self.settings.azure_openai_endpoint,
                api_version=self.settings.azure_openai_api_version,
                **({"max_retries": 0} if _SINGLE_ATTEMPT.get() else {}),
            )
            messages: list[dict] = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            messages.append({"role": "user", "content": prompt})
            deployment = self.settings.azure_openai_deployment or "gpt-4o"
            stats: dict = {}
            response = _with_retry(
                lambda: client.chat.completions.create(model=deployment, messages=messages),
                stats=stats,
            )
            text = response.choices[0].message.content or ""
            usage = _usage_from_response(response, deployment)
            usage["retries"] = stats.get("retries", 0)
            return response.id, text, usage

        if not self._resolved_api_key():
            raise IntegrationConfigurationError("OPENAI_API_KEY is not configured.")

        client = self._openai_client()
        used_model = model or self.settings.openai_model
        stats = {}
        if self._resolved_base_url():
            messages: list[dict] = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            messages.append({"role": "user", "content": prompt})
            response = _with_retry(
                lambda: client.chat.completions.create(
                    model=used_model,
                    messages=messages,
                ),
                stats=stats,
            )
            text = response.choices[0].message.content or ""
            usage = _usage_from_response(response, used_model)
            usage["retries"] = stats.get("retries", 0)
            # Which endpoint answered, so an endpoint-scoped price applies to it alone.
            usage["base_url"] = self._resolved_base_url()
            self._stamp_billing(usage)
            return response.id, text, usage

        # The Responses API is OpenAI-specific; compatible base URLs use
        # chat.completions above while the default OpenAI endpoint uses Responses.
        response = _with_retry(lambda: client.responses.create(
            model=used_model,
            instructions=system_prompt,
            input=prompt,
        ), stats=stats)
        usage = _usage_from_response(response, used_model)
        usage["retries"] = stats.get("retries", 0)
        return response.id, response.output_text, usage

    def stream_text_response(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        model: Optional[str] = None,
    ) -> Iterator[Any]:
        """`create_text_response` with `stream=True`: yields each piece of text as the model
        writes it, then the usage dict (agent_converse._call reads exactly this). Same
        endpoint choice as the one-shot form: Azure, a compatible base URL, or OpenAI's
        Responses API. Only opening the stream is retried; a reply cut off mid-way raises."""
        if self._azure_enabled():
            client: Any = AzureOpenAI(
                api_key=self.settings.azure_openai_key,
                azure_endpoint=self.settings.azure_openai_endpoint,
                api_version=self.settings.azure_openai_api_version,
                **({"max_retries": 0} if _SINGLE_ATTEMPT.get() else {}),
            )
            used_model, base_url = self.settings.azure_openai_deployment or "gpt-4o", ""
        else:
            if not self._resolved_api_key():
                raise IntegrationConfigurationError("OPENAI_API_KEY is not configured.")
            client = self._openai_client()
            used_model = model or self.settings.openai_model
            base_url = self._resolved_base_url()
        stats: dict = {}
        if self._azure_enabled() or base_url:
            messages: list[dict] = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            messages.append({"role": "user", "content": prompt})

            def open_stream(with_usage: bool):
                extra = {"stream_options": {"include_usage": True}} if with_usage else {}
                return _with_retry(lambda: client.chat.completions.create(
                    model=used_model, messages=messages, stream=True, **extra), stats=stats)

            try:
                stream = open_stream(True)
            except BadRequestError as error:
                # An older compatible server that does not know stream_options: stream without
                # the usage chunk rather than not at all.
                if "stream_options" not in str(error):
                    raise
                stream = open_stream(False)
            usage: dict = {"model": used_model, "prompt_tokens": 0, "completion_tokens": 0,
                           "total_tokens": 0}
            for chunk in stream:
                if getattr(chunk, "usage", None):
                    usage = _usage_from_response(chunk, used_model)
                for choice in getattr(chunk, "choices", None) or []:
                    piece = getattr(getattr(choice, "delta", None), "content", None)
                    if piece:
                        yield piece
            usage["retries"] = stats.get("retries", 0)
            if base_url:
                usage["base_url"] = base_url
            yield usage
            return

        stream = _with_retry(lambda: client.responses.create(
            model=used_model, instructions=system_prompt, input=prompt, stream=True,
        ), stats=stats)
        usage = {"model": used_model, "prompt_tokens": 0, "completion_tokens": 0,
                 "total_tokens": 0}
        for event in stream:
            kind = getattr(event, "type", "")
            if kind == "response.output_text.delta" and getattr(event, "delta", None):
                yield event.delta
            elif kind == "response.completed":
                usage = _usage_from_response(getattr(event, "response", None), used_model)
            elif kind in ("response.failed", "error"):
                failure = getattr(getattr(event, "response", None), "error", None) or event
                raise RuntimeError(f"the model stopped mid-reply: {failure}")
        usage["retries"] = stats.get("retries", 0)
        yield usage

    def create_chat_completion(
        self,
        messages: list[dict],
        tools: Optional[list[dict]] = None,
        tool_choice: str = "auto",
        model_override: Optional[str] = None,
        redact: bool = False,
    ) -> Tuple[dict, dict]:
        """One turn of a tool-calling conversation.

        Returns (message, usage) where message = {content, tool_calls, raw}.
        tool_calls is a list of {id, name, arguments(JSON string)} or None.
        """
        if redact:  # data-boundary safety: mask PII in message content before the external call
            messages = [
                {**m, "content": _redact(m["content"])[0]} if isinstance(m.get("content"), str) else m
                for m in messages
            ]
        if self._azure_enabled():
            client = AzureOpenAI(
                api_key=self.settings.azure_openai_key,
                azure_endpoint=self.settings.azure_openai_endpoint,
                api_version=self.settings.azure_openai_api_version,
                **({"max_retries": 0} if _SINGLE_ATTEMPT.get() else {}),
            )
            model = self.settings.azure_openai_deployment or "gpt-4o"
        elif self._resolved_api_key():
            client = self._openai_client()
            model = self.settings.openai_model
        else:
            raise IntegrationConfigurationError("OPENAI_API_KEY is not configured.")

        if model_override:
            model = model_override
        kwargs: dict = {"model": model, "messages": messages}
        # Cap the completion size: metered gateways (OpenRouter) reserve credits for the
        # requested MAXIMUM, so an uncapped request can 402 on reservation alone.
        # Groq/Qwen is additionally capped well below the 6k TPM window.
        max_out = int(getattr(self.settings, "native_max_output_tokens", 0) or 0)
        max_out = capped_max_output_tokens(
            max_out,
            model,
            base_url=self._resolved_base_url()
            or str(getattr(self.settings, "openai_base_url", "") or ""),
            engine=str(getattr(self.settings, "code_agent_engine", "") or ""),
        )
        if max_out > 0:
            kwargs["max_tokens"] = max_out
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice
        stats: dict = {}
        # NO CALL OUTLIVES ITS TURN (call_deadline.py). Asked per attempt, so a retry after a
        # slow first try gets only what is left, and none at all once the turn is out of time.
        from .call_deadline import bounded_timeout, remaining

        def _create():
            if remaining() is None:
                return client.chat.completions.create(**kwargs)
            return client.chat.completions.create(**kwargs, timeout=bounded_timeout(600))

        response = _with_retry(_create, stats=stats)
        msg = response.choices[0].message
        tool_calls = None
        if getattr(msg, "tool_calls", None):
            tool_calls = [
                {
                    "id": tc.id,
                    "name": tc.function.name,
                    "arguments": tc.function.arguments,
                }
                for tc in msg.tool_calls
            ]
        normalized = {
            "content": msg.content,
            "tool_calls": tool_calls,
            "raw": msg.model_dump(exclude_none=True),
        }
        usage = _usage_from_response(response, model)
        usage["retries"] = stats.get("retries", 0)
        if not self._azure_enabled() and self._resolved_base_url():
            usage["base_url"] = self._resolved_base_url()
        self._stamp_billing(usage)
        return normalized, usage


def adapter_for_entitlement_source(
    settings: Settings,
    source: dict,
    *,
    api_key: Optional[str] = None,
) -> OpenAIAdapter:
    """Build an adapter routed at the entitlement source's OpenAI-compatible base_url."""
    from .entitlements import EntitlementBrokerService

    endpoint = EntitlementBrokerService.resolve_openai_compatible_endpoint(source)
    return OpenAIAdapter(
        settings,
        base_url=endpoint.get("base_url"),
        api_key=api_key if api_key is not None else "local",
    )
