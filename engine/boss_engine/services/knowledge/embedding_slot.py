"""Setup › Embedding model: the embedding model as its own slot (operator decision 2026-10-22).

Mirrors the decision model's slot (setup_service.decision_model_status / store_decision_key):
one project setting names the provider, the model and its dimension; the key goes into the vault
and is never returned; and the slot is CHECKED by one tiny embedding through the model gateway
(model_routing.project_embedder: reserved in the budget category `knowledge`, receipted,
settled). The check records the model's real reply, or its error, and Setup shows it.

Meaning-based search runs only on a slot whose last check passed (`active_embedder`). With no
slot, a slot never checked, or a check that failed (a provider or model with no embeddings),
retrieval runs on full text and the graph and says why in plain words; nothing errors.

The vector space is keyed by `model_id_of(slot)` = provider, model and dimension. Switching any
of them is a new space: the background tick (embeddings.embed_due, on the worker's knowledge
tick) drops the project's vectors of every other space and re-embeds under the new one, so two
spaces never mix in one search.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

#: The project_settings row (a plain row, like setup.secret_db_consent, not a console knob).
SETTING_KEY = "knowledge.embedding_model"
SECRET_NAME = "KNOWLEDGE_EMBEDDING_API_KEY"
PROBE_TEXT = "BOSS embedding check"
MIN_DIMENSION, MAX_DIMENSION = 8, 4096

#: What Setup offers, in order. `fields` is the form.
PROVIDERS: tuple[dict[str, Any], ...] = (
    {"provider": "azure", "label": "Azure OpenAI", "needs_key": True,
     "fields": ["endpoint", "deployment", "api_version", "api_key", "dimension"],
     "example_model": "text-embedding-3-small", "example_dimension": 1536},
    {"provider": "openai", "label": "OpenAI or compatible", "needs_key": True,
     "fields": ["base_url", "model", "api_key", "dimension"],
     "example_model": "text-embedding-3-small", "example_dimension": 1536},
    {"provider": "ollama", "label": "Ollama (local)", "needs_key": False,
     "fields": ["base_url", "model", "dimension"],
     "example_model": "nomic-embed-text", "example_dimension": 768,
     "example_base_url": "http://localhost:11434"},
)
_BY_PROVIDER = {p["provider"]: p for p in PROVIDERS}

NO_MODEL = "meaning-based search is off: no embedding model is set (Setup › Embedding model)"
UNCHECKED = "meaning-based search is off: the embedding model has not been checked yet"


class EmbeddingSlotError(ValueError):
    """A slot BOSS will not store, with the reason in words."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def model_id_of(slot: dict[str, Any]) -> str:
    """The vector space's key: provider, model and dimension."""
    model = str(slot.get("model") or slot.get("deployment") or "")
    return f"{slot.get('provider')}:{model}:{int(slot.get('dimension') or 0)}"


def read(connection: Connection, project_id: str) -> Optional[dict[str, Any]]:
    try:
        with connection.begin_nested():
            value = connection.execute(
                text("SELECT value FROM project_settings WHERE project_id = :p AND key = :k"),
                {"p": project_id, "k": SETTING_KEY}).scalar()
    except Exception:  # noqa: BLE001 — no settings table: no slot
        return None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    return value if isinstance(value, dict) and value.get("provider") else None


def _write(connection: Connection, project_id: str, slot: dict[str, Any], actor: str) -> None:
    connection.execute(
        text("INSERT INTO project_settings (project_id, key, value, updated_by, updated_at) "
             "VALUES (:p, :k, CAST(:v AS JSONB), :by, now()) "
             "ON CONFLICT (project_id, key) DO UPDATE SET value = EXCLUDED.value, "
             "updated_by = EXCLUDED.updated_by, updated_at = now()"),
        {"p": project_id, "k": SETTING_KEY, "v": json.dumps(slot), "by": actor})


def _clean_url(value: Any, label: str) -> str:
    url = str(value or "").strip().rstrip("/")
    if url and not re.match(r"^https?://[^\s/@]+(:\d+)?(/[^\s]*)?$", url):
        raise EmbeddingSlotError(f"{label} must be an http(s) URL with no user name or password in it.")
    return url


def _validate(provider: str, *, model: str, dimension: Any, base_url: str, endpoint: str,
              deployment: str, api_version: str) -> dict[str, Any]:
    from ..scripted_engine import enabled as scripted_enabled

    provider = str(provider or "").strip().lower()
    if provider not in _BY_PROVIDER and not (provider == "scripted" and scripted_enabled()):
        raise EmbeddingSlotError(
            "choose Azure OpenAI, OpenAI or compatible, or Ollama (local) for the embedding model.")
    try:
        dims = int(dimension)
    except (TypeError, ValueError):
        raise EmbeddingSlotError("the dimension is the length of the model's vectors, a whole number "
                                 "(1536 for text-embedding-3-small, 768 for nomic-embed-text).") from None
    if not (MIN_DIMENSION <= dims <= MAX_DIMENSION):
        raise EmbeddingSlotError(f"the dimension has to be between {MIN_DIMENSION} and {MAX_DIMENSION}.")
    model = str(model or "").strip()
    deployment = str(deployment or "").strip()
    if provider == "azure":
        if not deployment:
            raise EmbeddingSlotError("Azure OpenAI needs the deployment name of the embedding model.")
        endpoint = _clean_url(endpoint, "The Azure endpoint")
        if not endpoint:
            raise EmbeddingSlotError("Azure OpenAI needs its endpoint (https://<resource>.openai.azure.com).")
        model = model or deployment
    elif not model:
        raise EmbeddingSlotError("name the embedding model (for example text-embedding-3-small or "
                                 "nomic-embed-text).")
    if not re.fullmatch(r"[A-Za-z0-9._:/\-]{1,200}", model):
        raise EmbeddingSlotError("that model name has characters a model name cannot have.")
    return {"provider": provider, "model": model, "dimension": dims,
            "base_url": _clean_url(base_url, "The base URL") if provider != "azure" else "",
            "endpoint": endpoint if provider == "azure" else "",
            "deployment": deployment if provider == "azure" else "",
            "api_version": str(api_version or "").strip() if provider == "azure" else ""}


def _scrub_error(message: str, key: Optional[str]) -> str:
    from .scrub import scrub

    out = str(message or "")
    if key:
        out = out.replace(key, "[key]")
    out = re.sub(r"(?i)(api key provided:?\s*)\S+", r"\1[key]", out)
    out, _ = scrub(out)
    return out[:300]


def probe(connection: Connection, settings: Any, project_id: str, slot: dict[str, Any], *,
          actor: str = "setup") -> dict[str, Any]:
    """One tiny embedding through the gateway. The model's real reply, or its error, in words."""
    from ..model_routing import project_embedder

    started = time.monotonic()
    key = None
    try:
        if slot.get("secret_ref_id"):
            from ..secret_backends import resolve_secret_ref

            key = resolve_secret_ref(connection, str(slot["secret_ref_id"]), settings, project_id=project_id)
        embedder = project_embedder(connection, settings, project_id, slot, ref="check",
                                    skill_id="knowledge.embedding_check")
        # No savepoint around the call: a failed call's receipt is marked failed and kept.
        vectors = embedder.embed([PROBE_TEXT])
    except Exception as error:  # noqa: BLE001 — the check's answer is the error itself, in words
        return {"state": "failed", "error": _scrub_error(f"{type(error).__name__}: {error}", key),
                "checked_at": _now(), "latency_ms": int((time.monotonic() - started) * 1000)}
    latency = int((time.monotonic() - started) * 1000)
    got = len(vectors[0]) if vectors else 0
    usage = embedder.last_usage
    reply = f"{got} numbers from {usage.get('model') or slot.get('model')}"
    if got != int(slot["dimension"]):
        return {"state": "failed", "reply": reply, "checked_at": _now(), "latency_ms": latency,
                "error": f"the model answered with {got} dimensions, and the slot says "
                         f"{slot['dimension']}. Set the dimension to {got}."}
    return {"state": "ok", "reply": reply, "checked_at": _now(), "latency_ms": latency,
            "tokens": int(usage.get("prompt_tokens") or 0)}


def save(connection: Connection, settings: Any, project_id: str, *, provider: str, model: str = "",
         dimension: Any = None, base_url: str = "", endpoint: str = "", deployment: str = "",
         api_version: str = "", api_key: Optional[str] = None, accept_database_fallback: bool = False,
         actor: str = "setup") -> dict[str, Any]:
    """Store the slot (the key into the vault), check it, and answer with its status."""
    from ..core import emit_event

    clean = _validate(provider, model=model, dimension=dimension, base_url=base_url,
                      endpoint=endpoint, deployment=deployment, api_version=api_version)
    before = read(connection, project_id) or {}
    secret_ref_id = before.get("secret_ref_id") if before.get("provider") == clean["provider"] else None
    api_key = (api_key or "").strip()
    if api_key:
        secret_ref_id = _store_key(connection, settings, project_id, api_key, actor=actor,
                                   accept_database_fallback=accept_database_fallback)
    elif _BY_PROVIDER.get(clean["provider"], {}).get("needs_key") and not secret_ref_id \
            and not (clean["provider"] == "openai" and clean["base_url"]):
        raise EmbeddingSlotError(f"{_BY_PROVIDER[clean['provider']]['label']} needs its API key.")
    if before.get("secret_ref_id") and before.get("secret_ref_id") != secret_ref_id:
        _revoke(connection, str(before["secret_ref_id"]), actor)
    slot = {**clean, "secret_ref_id": secret_ref_id, "saved_at": _now(), "saved_by": actor}
    slot["check"] = probe(connection, settings, project_id, slot, actor=actor)
    _write(connection, project_id, slot, actor)
    emit_event(connection, project_id, "setup.embedding_model_saved", actor, "project_setting", SETTING_KEY,
               {"provider": slot["provider"], "model": slot["model"], "dimension": slot["dimension"],
                "model_id": model_id_of(slot), "check": slot["check"]["state"],
                "previous_model_id": model_id_of(before) if before else None})
    return status(connection, settings, project_id)


def check(connection: Connection, settings: Any, project_id: str, *, actor: str = "setup") -> dict[str, Any]:
    slot = read(connection, project_id)
    if slot is None:
        raise EmbeddingSlotError("no embedding model is set to check.")
    slot["check"] = probe(connection, settings, project_id, slot, actor=actor)
    _write(connection, project_id, slot, actor)
    return status(connection, settings, project_id)


def clear(connection: Connection, settings: Any, project_id: str, *, actor: str = "setup") -> dict[str, Any]:
    from ..core import emit_event

    slot = read(connection, project_id)
    if slot is not None:
        if slot.get("secret_ref_id"):
            _revoke(connection, str(slot["secret_ref_id"]), actor)
        connection.execute(text("DELETE FROM project_settings WHERE project_id = :p AND key = :k"),
                           {"p": project_id, "k": SETTING_KEY})
        emit_event(connection, project_id, "setup.embedding_model_cleared", actor, "project_setting",
                   SETTING_KEY, {"model_id": model_id_of(slot)})
    return status(connection, settings, project_id)


def _store_key(connection: Connection, settings: Any, project_id: str, api_key: str, *, actor: str,
               accept_database_fallback: bool) -> str:
    from ..secret_backends import default_secret_backend, operator_chosen_backend
    from ..secrets_service import put_secret
    from ..setup_service import SecretFallbackNotAccepted

    backend = default_secret_backend(settings)
    if (backend == "database" and not accept_database_fallback
            and operator_chosen_backend() != "database"):
        raise SecretFallbackNotAccepted(
            backend,
            "this installation has no secret store configured, so the key would go into BOSS's "
            "Postgres database. That works and survives restarts, but anyone with the database "
            "has the key. Set up a secret store, or re-send with accept_database_fallback=true "
            "to store it there deliberately.")
    stored = put_secret(connection, settings, project_id=project_id, name=SECRET_NAME, value=api_key,
                        secret_kind="api_token", backend=backend, actor=actor)
    return str(stored["secret_ref_id"])


def _revoke(connection: Connection, secret_ref_id: str, actor: str) -> None:
    connection.execute(text("UPDATE secret_refs SET status = 'revoked', revoked_at = now(), revoked_by = :a "
                            "WHERE id = :id AND status <> 'revoked'"), {"a": actor, "id": secret_ref_id})


def search_state(slot: Optional[dict[str, Any]]) -> tuple[bool, str]:
    """(meaning-based search on, the reason it is off in words)."""
    if slot is None:
        return False, NO_MODEL
    checked = slot.get("check") or {}
    if checked.get("state") == "ok":
        return True, ""
    if checked.get("state") == "failed":
        return False, ("meaning-based search is off: the embedding model check failed: "
                       f"{checked.get('error') or 'no reason given'}")
    return False, UNCHECKED


def active_embedder(connection: Connection, settings: Any, project_id: str):
    """The governed embedder of a slot whose check passed, else (None, the reason in words)."""
    slot = read(connection, project_id)
    on, reason = search_state(slot)
    if not on:
        return None, reason
    from ..model_routing import project_embedder

    try:
        return project_embedder(connection, settings, project_id, slot), ""
    except Exception as error:  # noqa: BLE001 — a key that no longer resolves: off, and why
        return None, f"meaning-based search is off: the embedding model cannot be reached ({type(error).__name__})"


def pending(connection: Connection, project_id: str, model_id: str) -> int:
    from .embeddings import EMBEDDED_KINDS

    try:
        with connection.begin_nested():
            return int(connection.execute(text(
                """
                SELECT count(*) FROM kg_nodes n
                WHERE n.project_id = :p AND n.kind = ANY(:kinds) AND n.content_hash IS NOT NULL
                  AND (n.valid_to IS NULL OR n.valid_to > now())
                  AND NOT EXISTS (SELECT 1 FROM kg_embeddings e WHERE e.node_id = n.id
                                    AND e.model_id = :m AND e.content_hash = n.content_hash)
                """), {"p": project_id, "kinds": list(EMBEDDED_KINDS), "m": model_id}).scalar_one())
    except Exception:  # noqa: BLE001 — no knowledge tables yet
        return 0


def status(connection: Connection, settings: Any, project_id: str) -> dict[str, Any]:
    """What Setup shows. Only THAT a key is held, never the key or any part of it."""
    from ..setup_view import _without_userinfo

    slot = read(connection, project_id)
    on, reason = search_state(slot)
    out: dict[str, Any] = {"key": SETTING_KEY, "configured": slot is not None, "search": on,
                           "reason": reason, "providers": [dict(p) for p in PROVIDERS],
                           "permission": "model_provider:manage", "budget_category": "knowledge"}
    if slot is None:
        return out
    model_id = model_id_of(slot)
    out.update({
        "provider": slot["provider"],
        "label": _BY_PROVIDER.get(slot["provider"], {}).get("label", slot["provider"]),
        "model": slot.get("model") or "", "dimension": int(slot.get("dimension") or 0),
        "base_url": _without_userinfo(str(slot.get("base_url") or "")),
        "endpoint": _without_userinfo(str(slot.get("endpoint") or "")),
        "deployment": slot.get("deployment") or "", "api_version": slot.get("api_version") or "",
        "has_key": bool(slot.get("secret_ref_id")),
        "model_id": model_id,
        "check": dict(slot.get("check") or {"state": "unchecked"}),
        "reembed_pending": pending(connection, project_id, model_id) if on else 0,
    })
    return out
