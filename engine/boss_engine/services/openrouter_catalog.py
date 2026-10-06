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
    key = None
    try:
        from .credential_resolve import resolve_project_credential

        key = resolve_project_credential(connection, settings, project_id=project_id,
                                         provider=PROVIDER,
                                         env_fallback_attr="openrouter_api_key")
    except Exception:  # noqa: BLE001
        key = str(getattr(settings, "openrouter_api_key", "") or "") or None
    try:
        models = fetch(str(getattr(settings, "openrouter_base_url", "") or ""), key)
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


def suggest(models: Optional[Iterable[CatalogModel]] = None) -> dict[str, str]:
    """A model per tier from the listed catalogue; {} when nothing is cached. Cheap families
    for Fast and Balanced (tool-capable, enough context), the newest Opus-class for Frontier."""
    pool = [m for m in (models if models is not None else cached().values())
            if m.tools and not m.id.endswith(":free") and m.prompt_usd_per_mtok is not None]
    if not pool:
        return {}
    picks: dict[str, str] = {}

    def family_rank(m: CatalogModel) -> int:
        return next((i for i, f in enumerate(_CHEAP_FAMILIES) if m.id.startswith(f)),
                    len(_CHEAP_FAMILIES))

    cheap = [m for m in pool if family_rank(m) < len(_CHEAP_FAMILIES)
             and (m.context_length or 0) >= 32_000]
    fast = sorted((m for m in cheap if m.blended <= 0.6), key=lambda m: (m.blended, -m.created))
    if fast:
        picks["fast"] = fast[0].id
    balanced = sorted((m for m in cheap if m.blended <= 2.5 and (m.context_length or 0) >= 64_000
                       and ("coder" in m.id or family_rank(m) <= 2)),
                      key=lambda m: (-("coder" in m.id), family_rank(m), -m.created))
    if balanced:
        picks["balanced"] = balanced[0].id
    for prefix in _FRONTIER_PREFIXES:
        frontier = sorted((m for m in pool if m.id.startswith(prefix)),
                          key=lambda m: -m.created)
        if frontier:
            picks["frontier"] = frontier[0].id
            break
    return picks


def view() -> list[dict[str, Any]]:
    """For the console: id, name, prices — never a key."""
    return [asdict(m) for m in sorted(cached().values(), key=lambda m: m.id)]


__all__ = ["CatalogModel", "fetch", "load", "parse", "price", "refresh", "remember", "store",
           "suggest", "view"]
