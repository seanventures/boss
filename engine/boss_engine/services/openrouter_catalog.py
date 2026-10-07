"""OpenRouter's model catalogue, with prices — so OpenRouter is a first-class routing source.

`GET {base}/models` lists every model with `pricing.prompt` / `pricing.completion` in USD per
TOKEN, `context_length` and `supported_parameters`. BOSS fetches it off the hot path (Setup,
or a refresh), keeps it in-process for six hours and in `provider_model_catalog`, and:

  * `price(model)` feeds `openai_adapter.price_for_model`, so an OpenRouter call's cost is a
    known price rather than the DEFAULT_PRICE estimate;
  * `suggest()` picks a Fast / Balanced / Frontier model for the router's tiers from what is
    actually listed, rather than a written list that goes stale.

Never raises to a caller that is doing work: an unreachable catalogue reads as empty.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import asdict, dataclass
from typing import Any, Iterable, Optional

log = logging.getLogger("boss.openrouter_catalog")

TTL_S = 6 * 3600.0
PROVIDER = "openrouter"

_LOCK = threading.Lock()
_CACHE: dict[str, Any] = {"at": 0.0, "models": {}}

#: Families worth routing cheap work to, in order of preference, and the frontier family.
_CHEAP_FAMILIES = ("deepseek/", "qwen/", "moonshotai/", "z-ai/", "google/gemini", "mistralai/")
_FRONTIER_PREFIXES = ("anthropic/claude-opus", "anthropic/claude-sonnet", "openai/gpt-5")


@dataclass(frozen=True)
class CatalogModel:
    id: str
    name: str = ""
    prompt_usd_per_mtok: Optional[float] = None
    completion_usd_per_mtok: Optional[float] = None
    context_length: Optional[int] = None
    tools: bool = False
    created: int = 0

    @property
    def blended(self) -> float:
        return (self.prompt_usd_per_mtok or 0.0) * 0.75 + (self.completion_usd_per_mtok or 0.0) * 0.25


def _per_mtok(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number < 0 else round(number * 1_000_000, 6)


def parse(data: Any) -> list[CatalogModel]:
    """The `/models` body → catalogue entries. Pure; unknown fields are ignored."""
    items = data.get("data") if isinstance(data, dict) else data
    out: list[CatalogModel] = []
    for item in items or []:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        pricing = item.get("pricing") if isinstance(item.get("pricing"), dict) else {}
        params = item.get("supported_parameters") or []
        out.append(CatalogModel(
            id=str(item["id"]), name=str(item.get("name") or "")[:120],
            prompt_usd_per_mtok=_per_mtok(pricing.get("prompt")),
            completion_usd_per_mtok=_per_mtok(pricing.get("completion")),
            context_length=int(item["context_length"]) if str(item.get("context_length") or "")
            .isdigit() else None,
            tools="tools" in params if isinstance(params, list) else False,
            created=int(item.get("created") or 0)))
    return out


def remember(models: Iterable[CatalogModel]) -> None:
    with _LOCK:
        _CACHE["models"] = {m.id: m for m in models}
        _CACHE["at"] = time.monotonic()


def cached() -> dict[str, CatalogModel]:
    with _LOCK:
        return dict(_CACHE["models"])


def fresh() -> bool:
    with _LOCK:
        return bool(_CACHE["models"]) and time.monotonic() - float(_CACHE["at"]) < TTL_S


def price(model: str) -> Optional[tuple[float, float]]:
    """(input, output) USD per million tokens for an OpenRouter model id, from the cache."""
    entry = cached().get(str(model or ""))
    if entry is None or entry.prompt_usd_per_mtok is None:
        return None
    return entry.prompt_usd_per_mtok, entry.completion_usd_per_mtok or 0.0


def fetch(base_url: str, api_key: Optional[str] = None, *, client: Any = None,
          timeout: float = 10.0) -> list[CatalogModel]:
    """GET {base}/models. The list is public; the key is sent when there is one."""
    import httpx

    url = (base_url or "https://openrouter.ai/api/v1").rstrip("/") + "/models"
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    own = client is None
    client = client or httpx.Client(timeout=timeout)
    try:
        response = client.get(url, headers=headers)
        response.raise_for_status()
        return parse(response.json())
    finally:
        if own:
            client.close()


def store(connection: Any, models: Iterable[CatalogModel]) -> int:
    """Persist the catalogue (so a fresh process has prices before its first fetch)."""
    from sqlalchemy import text

    count = 0
    for m in models:
        connection.execute(text("""
            INSERT INTO provider_model_catalog (provider, model_id, name, prompt_usd_per_mtok,
                completion_usd_per_mtok, context_length, tools, fetched_at)
            VALUES (:p, :id, :n, :pi, :po, :ctx, :tools, now())
            ON CONFLICT (provider, model_id) DO UPDATE SET name = EXCLUDED.name,
                prompt_usd_per_mtok = EXCLUDED.prompt_usd_per_mtok,
                completion_usd_per_mtok = EXCLUDED.completion_usd_per_mtok,
                context_length = EXCLUDED.context_length, tools = EXCLUDED.tools,
                fetched_at = now()"""),
            {"p": PROVIDER, "id": m.id, "n": m.name, "pi": m.prompt_usd_per_mtok,
             "po": m.completion_usd_per_mtok, "ctx": m.context_length, "tools": m.tools})
        count += 1
    return count


def load(connection: Any) -> list[CatalogModel]:
    """The persisted catalogue, into the in-process cache. Empty when there is none."""
    from sqlalchemy import text

    try:
        with connection.begin_nested():
            rows = connection.execute(text(
                "SELECT model_id, name, prompt_usd_per_mtok, completion_usd_per_mtok, "
                "context_length, tools FROM provider_model_catalog WHERE provider = :p"),
                {"p": PROVIDER}).mappings().all()
    except Exception:  # noqa: BLE001
        return []
    models = [CatalogModel(id=r["model_id"], name=r["name"] or "",
                           prompt_usd_per_mtok=float(r["prompt_usd_per_mtok"])
                           if r["prompt_usd_per_mtok"] is not None else None,
                           completion_usd_per_mtok=float(r["completion_usd_per_mtok"])
                           if r["completion_usd_per_mtok"] is not None else None,
                           context_length=r["context_length"], tools=bool(r["tools"]))
              for r in rows]
    if models and not cached():
        remember(models)
    return models


def refresh(connection: Any, settings: Any, project_id: Optional[str]) -> list[CatalogModel]:
    """Fetch with this project's stored OpenRouter key (or the environment's), remember and
    persist. Never raises: returns what is cached when the fetch fails."""
    key, base_url = None, ""
    try:
        # THE SOURCE'S OWN STORED KEY AND URL (a custom OpenRouter endpoint, a proxy), the same
        # binding a call on this lane uses; the environment only when nothing is stored.
        from .code_agent import _project_api_model_binding

        bound = _project_api_model_binding(connection, settings, project_id or "", "openrouter")
        if bound:
            key, _model, base_url = bound
    except Exception:  # noqa: BLE001
        key = None
    if not key:
        try:
            from .credential_resolve import resolve_project_credential

            key = resolve_project_credential(connection, settings, project_id=project_id,
                                             provider=PROVIDER,
                                             env_fallback_attr="openrouter_api_key")
        except Exception:  # noqa: BLE001
            key = str(getattr(settings, "openrouter_api_key", "") or "") or None
    try:
        models = fetch(base_url or str(getattr(settings, "openrouter_base_url", "") or ""), key)
    except Exception:  # noqa: BLE001
        log.warning("OpenRouter catalogue fetch failed", exc_info=True)
        return list(cached().values())
    remember(models)
    try:
        with connection.begin_nested():
            store(connection, models)
    except Exception:  # noqa: BLE001
        log.warning("OpenRouter catalogue not persisted", exc_info=True)
    return models


#: A curated quality floor per tier: families known to do the tier's work well, best first.
#: The cheapest listed model is NOT the default — a 3B model is cheap and wrong for code.
CURATED = {
    "fast": ("qwen/qwen3-coder-flash", "qwen/qwen3-coder-30b", "deepseek/deepseek-chat",
             "google/gemini-2.5-flash", "openai/gpt-5-mini", "z-ai/glm-4.5-air",
             "mistralai/devstral"),
    "balanced": ("qwen/qwen3-coder", "moonshotai/kimi-k2", "z-ai/glm-4.6",
                 "deepseek/deepseek-chat", "deepseek/deepseek-v3", "anthropic/claude-sonnet"),
    "frontier": ("anthropic/claude-opus", "openai/gpt-5", "google/gemini-2.5-pro"),
}
MIN_CONTEXT = {"fast": 32_000, "balanced": 64_000, "frontier": 128_000}


def suggest(models: Optional[Iterable[CatalogModel]] = None) -> dict[str, str]:
    """A model per tier from the listed catalogue, held to CURATED and MIN_CONTEXT; {} for a
    tier nothing qualifies for (the router's static defaults stand in)."""
    from .model_router.policy import model_tier

    pool = [m for m in (models if models is not None else cached().values())
            if m.tools and not m.id.endswith(":free")]
    picks: dict[str, str] = {}
    for tier, families in CURATED.items():
        for family in families:
            found = [m for m in pool if m.id.startswith(family)
                     and (m.context_length or 0) >= MIN_CONTEXT[tier]
                     and model_tier("openrouter", m.id) == (
                         tier if tier == "frontier" else model_tier("openrouter", m.id))
                     and not (tier != "fast" and model_tier("openrouter", m.id) == "fast")
                     and not (tier == "fast" and re.search(r"(^|[-/])[1-8]b\b", m.id))]
            if found:
                picks[tier] = sorted(found, key=lambda m: (-m.created, m.blended))[0].id
                break
    return picks


def view() -> list[dict[str, Any]]:
    """For the console: id, name, prices — never a key."""
    return [asdict(m) for m in sorted(cached().values(), key=lambda m: m.id)]


__all__ = ["CatalogModel", "fetch", "load", "parse", "price", "refresh", "remember", "store",
           "suggest", "view"]
