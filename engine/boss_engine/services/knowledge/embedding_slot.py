"""Setup › Repo and models › Embedding model: ONE embedding model for the whole org.

Operator decision 2026-10-22 made the embedding model its own slot; 2026-10-23 ("why is it not
in Repo and models, so all projects can use it?") made that slot the ORG's: it is set once,
in the Models step, and every project of the org uses it. The slot is an `org_settings` row
(`knowledge.embedding_model`, the same shape `project_settings` has, keyed by the org); its
key is ONE org-scoped secret (secret_refs scope_type='org'), which every project of the org
resolves (secret_backends._shared_with) and which is held to the provider's hosts
(secret_backends._hold_to_provider). Changing it needs `model_provider:manage` for the whole
org, as the org's GitHub credentials need `github:connect` at the org (identity/github_setup).

An install from before that has a per-project `project_settings` row. It is read as a fallback
while the org has no slot, and it is PROMOTED to the org (its key re-stored at org scope, the
project row removed) the first time anyone saves or checks it; saving or removing the org slot
removes every project row of the org, so one org never runs two models.

The slot is CHECKED by one tiny embedding through the model gateway
(model_routing.project_embedder: reserved in the budget category `knowledge`, receipted,
settled). The check records the model's real reply, or its error, and Setup shows it. The
DIMENSION IS LEARNED from that reply: nobody has to know that text-embedding-3-small answers
1536 numbers. An operator who wants a smaller vector sets it under "More", and BOSS then asks
the model for that size (the `dimensions` parameter text-embedding-3 models take).

Pasting one URL is enough: for Azure the endpoint, the deployment and the API version come out
of the "Target URI" Azure shows (`parse_url`), and an API version from before 2024 is replaced
by one that takes `dimensions`.

Meaning-based search runs only on a slot whose last check passed (`active_embedder`). With no
slot, a slot never checked, or a check that failed, retrieval runs on full text and the graph
and says why in plain words; nothing errors.

The vector space stays keyed PER PROJECT by `model_id_of(slot)` = provider, model and
dimension. A new org slot is a new space for every project: each project's background tick
(embeddings.embed_due) drops its vectors of every other space and re-embeds under the new one,
so two spaces never mix in one search.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import parse_qs, urlsplit

from sqlalchemy import text
from sqlalchemy.engine import Connection

#: The org_settings row (and the legacy project_settings row of an older install).
SETTING_KEY = "knowledge.embedding_model"
SECRET_NAME = "KNOWLEDGE_EMBEDDING_API_KEY"
PROBE_TEXT = "BOSS embedding check"
MIN_DIMENSION, MAX_DIMENSION = 8, 4096
#: The API version BOSS uses on Azure; one from before 2024 cannot take `dimensions`.
DEFAULT_AZURE_API_VERSION = "2024-10-21"
#: Where Setup shows it, named the same way everywhere (Metatron, the knowledge console).
PLACE = "Setup › Repo and models › Embedding model"

#: What Setup offers, in order. `fields` is the form; `more` sits under the row's "More" toggle.
PROVIDERS: tuple[dict[str, Any], ...] = (
    {"provider": "azure", "label": "Azure OpenAI", "needs_key": True,
     "fields": ["url", "api_key"], "more": ["deployment", "api_version", "dimension"],
     "example_model": "text-embedding-3-small"},
    {"provider": "openai", "label": "OpenAI or compatible", "needs_key": True,
     "fields": ["url", "model", "api_key"], "more": ["dimension"],
     "example_model": "text-embedding-3-small", "example_base_url": "https://api.openai.com/v1"},
    {"provider": "ollama", "label": "Ollama (local)", "needs_key": False,
     "fields": ["url", "model"], "more": ["dimension"],
     "example_model": "nomic-embed-text", "example_base_url": "http://localhost:11434"},
)
_BY_PROVIDER = {p["provider"]: p for p in PROVIDERS}

NO_MODEL = f"meaning-based search is off: no embedding model is set ({PLACE})"
UNCHECKED = "meaning-based search is off: the embedding model has not been checked yet"


class EmbeddingSlotError(ValueError):
    """A slot BOSS will not store, with the reason in words."""


class EmbeddingSlotForbidden(EmbeddingSlotError):
    """The caller may not change the org's embedding model (it is every project's)."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def model_id_of(slot: dict[str, Any]) -> str:
    """The vector space's key: provider, model and dimension."""
    model = str(slot.get("model") or slot.get("deployment") or "")
    return f"{slot.get('provider')}:{model}:{int(slot.get('dimension') or 0)}"


# ── one URL in, the provider's fields out ───────────────────────────────────────────────────

def _clean_url(value: Any, label: str) -> str:
    url = str(value or "").strip().rstrip("/")
    if url and not re.match(r"^https?://[^\s/@]+(:\d+)?(/[^\s]*)?$", url):
        raise EmbeddingSlotError(f"{label} must be an http(s) URL with no user name or password in it.")
    return url


def _api_version(value: str) -> str:
    """The version to call with: a version from before 2024 (or none) becomes the default."""
    value = str(value or "").strip()
    match = re.match(r"^(\d{4})-\d{2}-\d{2}", value)
    return value if match and int(match.group(1)) >= 2024 else DEFAULT_AZURE_API_VERSION


def parse_url(provider: str, url: str) -> dict[str, str]:
    """What one pasted URL says. Azure: the base (`https://X.openai.azure.com/`,
    `https://X.cognitiveservices.azure.com/`) or the whole Target URI Azure shows
    (`…/openai/deployments/<name>/embeddings?api-version=…`) → endpoint, deployment, api_version.
    OpenAI-compatible and Ollama: the base URL, with a pasted `/embeddings` path taken off."""
    provider = str(provider or "").strip().lower()
    raw = str(url or "").strip()
    if not raw:
        return {}
    cleaned = _clean_url(raw.split("?", 1)[0].split("#", 1)[0], "The URL")
    parts = urlsplit(raw)
    origin = f"{parts.scheme}://{parts.hostname}" + (f":{parts.port}" if parts.port else "")
    path = (parts.path or "").rstrip("/")
    if provider == "azure":
        found = re.search(r"/openai/deployments/([^/]+)", path)
        version = (parse_qs(parts.query).get("api-version") or [""])[0]
        out = {"endpoint": origin, "api_version": _api_version(version)}
        if found:
            out["deployment"] = found.group(1)
        return out
    base = cleaned
    for tail in ("/embeddings", "/api/embeddings", "/api/embed"):
        if base.endswith(tail):
            base = base[: -len(tail)]
            break
    if provider == "ollama":
        base = base[:-3] if base.endswith("/v1") else base
    return {"base_url": base}


def _validate(provider: str, *, model: str, dimension: Any, base_url: str, endpoint: str,
              deployment: str, api_version: str, url: str = "") -> dict[str, Any]:
    from ..scripted_engine import enabled as scripted_enabled

    provider = str(provider or "").strip().lower()
    if provider not in _BY_PROVIDER and not (provider == "scripted" and scripted_enabled()):
        raise EmbeddingSlotError(
            "choose Azure OpenAI, OpenAI or compatible, or Ollama (local) for the embedding model.")
    parsed = parse_url(provider, url)
    dims, dimension_set = 0, False
    if dimension not in (None, "", 0, "0"):
        try:
            dims = int(dimension)
        except (TypeError, ValueError):
            raise EmbeddingSlotError("the dimension is the length of the model's vectors, a whole "
                                     "number; leave it empty and BOSS learns it from the model.") from None
        if not (MIN_DIMENSION <= dims <= MAX_DIMENSION):
            raise EmbeddingSlotError(f"the dimension has to be between {MIN_DIMENSION} and {MAX_DIMENSION}.")
        dimension_set = True
    model = str(model or "").strip()
    # A field typed into the form wins over what the URL said (the URL only prefills it).
    deployment = str(deployment or "").strip() or parsed.get("deployment", "")
    if provider == "azure":
        endpoint = _clean_url(endpoint, "The Azure endpoint") or parsed.get("endpoint", "")
        if endpoint and "/openai/" in endpoint + "/":
            got = parse_url("azure", endpoint)          # a Target URI pasted as the endpoint
            endpoint, deployment = got["endpoint"], deployment or got.get("deployment", "")
        if not endpoint:
            raise EmbeddingSlotError("paste the Azure endpoint or the Target URI of the embedding "
                                     "deployment (https://<resource>.openai.azure.com).")
        if not deployment:
            raise EmbeddingSlotError("Azure OpenAI needs the deployment name of the embedding model "
                                     "(it is in the Target URI, after /deployments/).")
        api_version = _api_version(str(api_version or "").strip() or parsed.get("api_version", ""))
        model = model or deployment
    elif not model:
        raise EmbeddingSlotError("name the embedding model (for example text-embedding-3-small or "
                                 "nomic-embed-text).")
    if not re.fullmatch(r"[A-Za-z0-9._:/\-]{1,200}", model) or (
            deployment and not re.fullmatch(r"[A-Za-z0-9._\-]{1,200}", deployment)):
        raise EmbeddingSlotError("that model name has characters a model name cannot have.")
    base = (_clean_url(base_url, "The base URL") or parsed.get("base_url", "")) if provider != "azure" else ""
    if base and provider != "scripted":
        base = parse_url(provider, base).get("base_url", base)
    return {"provider": provider, "model": model, "dimension": dims, "dimension_set": dimension_set,
            "base_url": base,
            "endpoint": endpoint if provider == "azure" else "",
            "deployment": deployment if provider == "azure" else "",
            "api_version": api_version if provider == "azure" else ""}


# ── where the slot lives ─────────────────────────────────────────────────────────────────────

def org_of(connection: Connection, project_id: str) -> Optional[str]:
    try:
        with connection.begin_nested():
            value = connection.execute(text(
                "SELECT w.org_id FROM projects p JOIN workspaces w ON w.id = p.workspace_id "
                "WHERE p.id = :p"), {"p": project_id}).scalar()
    except Exception:  # noqa: BLE001 — no such tables: no org
        return None
    return str(value) if value else None


def _value(connection: Connection, sql: str, params: dict[str, Any]) -> Optional[dict[str, Any]]:
    try:
        with connection.begin_nested():
            value = connection.execute(text(sql), params).scalar()
    except Exception:  # noqa: BLE001 — no settings table: no slot
        return None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    return value if isinstance(value, dict) and value.get("provider") else None


def _read_org(connection: Connection, org_id: str) -> Optional[dict[str, Any]]:
    return _value(connection, "SELECT value FROM org_settings WHERE org_id = :o AND key = :k",
                  {"o": org_id, "k": SETTING_KEY})


def _read_project(connection: Connection, project_id: str) -> Optional[dict[str, Any]]:
    return _value(connection, "SELECT value FROM project_settings WHERE project_id = :p AND key = :k",
                  {"p": project_id, "k": SETTING_KEY})


def read(connection: Connection, project_id: str) -> Optional[dict[str, Any]]:
    """The slot this project uses: its org's, else (an older install) its own project row."""
    org_id = org_of(connection, project_id)
    slot = _read_org(connection, org_id) if org_id else None
    if slot is not None:
        return {**slot, "scope": "org", "org_id": org_id}
    legacy = _read_project(connection, project_id)
    return {**legacy, "scope": "project"} if legacy is not None else None


def _stored(slot: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in slot.items() if k not in ("scope", "org_id")}


def _write(connection: Connection, org_id: str, slot: dict[str, Any], actor: str) -> None:
    connection.execute(
        text("INSERT INTO org_settings (org_id, key, value, updated_by, updated_at) "
             "VALUES (:o, :k, CAST(:v AS JSONB), :by, now()) "
             "ON CONFLICT (org_id, key) DO UPDATE SET value = EXCLUDED.value, "
             "updated_by = EXCLUDED.updated_by, updated_at = now()"),
        {"o": org_id, "k": SETTING_KEY, "v": json.dumps(_stored(slot)), "by": actor})


def _org_projects(connection: Connection, org_id: str) -> list[str]:
    return [str(p) for p in connection.execute(text(
        "SELECT p.id FROM projects p JOIN workspaces w ON w.id = p.workspace_id WHERE w.org_id = :o "
        "ORDER BY p.id"), {"o": org_id}).scalars().all()]


def _drop_project_rows(connection: Connection, org_id: str, actor: str, *, keep_ref: Optional[str]) -> None:
    """One org, one model: the per-project rows of an older install go, and their keys with them."""
    for pid in _org_projects(connection, org_id):
        legacy = _read_project(connection, pid)
        if legacy is None:
            continue
        if legacy.get("secret_ref_id") and legacy["secret_ref_id"] != keep_ref:
            _revoke(connection, str(legacy["secret_ref_id"]), actor)
        connection.execute(text("DELETE FROM project_settings WHERE project_id = :p AND key = :k"),
                           {"p": pid, "k": SETTING_KEY})


def _require_org_authority(connection: Connection, org_id: str) -> None:
    """The slot is every project's: inside an enforced request the caller needs
    `model_provider:manage` for the whole org (as identity/github_setup.may_change_org_credentials
    asks `github:connect` at the org). Outside a request (the host shell, a test) it is trusted."""
    from ...identity import authz, lineage
    from ...identity.permissions_gen import P
    from ...identity.scope import ResolvedScope

    if not lineage.request_enforced():
        return
    ctx = lineage.current_context()
    if ctx is not None and ctx.principal.org_id == org_id and authz.check(
            ctx, P.MODEL_PROVIDER_MANAGE, ResolvedScope.org_level(org_id), db=connection).allowed:
        return
    raise EmbeddingSlotForbidden(
        "the embedding model is used by every project, so changing it needs model_provider:manage "
        "for the whole organisation (an Owner or an org Admin).")


def _org_or_error(connection: Connection, project_id: str) -> str:
    org_id = org_of(connection, project_id)
    if not org_id:
        raise EmbeddingSlotError(f"project {project_id} was not found.")
    return org_id


# ── the check ────────────────────────────────────────────────────────────────────────────────

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
    """One tiny embedding through the gateway. The model's real reply, or its error, in words;
    `dimension` is the length of the vector the model answered with."""
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
    base = {"reply": reply, "checked_at": _now(), "latency_ms": latency, "dimension": got}
    if not (MIN_DIMENSION <= got <= MAX_DIMENSION):
        return {**base, "state": "failed", "error": f"the model answered with {got} numbers, which is "
                                                    "not an embedding BOSS can store."}
    if slot.get("dimension_set") and got != int(slot.get("dimension") or 0):
        return {**base, "state": "failed",
                "error": f"the model answered with {got} numbers, not the {slot['dimension']} asked "
                         f"for. Leave the dimension empty to use the model's own ({got})."}
    return {**base, "state": "ok", "tokens": int(usage.get("prompt_tokens") or 0)}


def _checked(connection: Connection, settings: Any, project_id: str, slot: dict[str, Any], actor: str) -> dict[str, Any]:
    """Check the slot and, unless the operator set a size, learn the dimension from the reply."""
    # A learned size is not asked for: the model answers with its own. A failed check keeps the
    # size it had, so a provider's bad minute does not move the project's vector space.
    asked = slot if slot.get("dimension_set") else {**slot, "dimension": 0}
    slot["check"] = probe(connection, settings, project_id, asked, actor=actor)
    if not slot.get("dimension_set") and slot["check"].get("state") == "ok":
        slot["dimension"] = int(slot["check"]["dimension"])
    return slot


def save(connection: Connection, settings: Any, project_id: str, *, provider: str, model: str = "",
         dimension: Any = None, url: str = "", base_url: str = "", endpoint: str = "",
         deployment: str = "", api_version: str = "", api_key: Optional[str] = None,
         accept_database_fallback: bool = False, actor: str = "setup") -> dict[str, Any]:
    """Store the org's slot (the key into the vault, at org scope), check it, and answer with
    `project_id`'s status. `project_id` is where it was set from; every project of its org uses it."""
    from ..core import emit_event

    org_id = _org_or_error(connection, project_id)
    _require_org_authority(connection, org_id)
    clean = _validate(provider, model=model, dimension=dimension, base_url=base_url, url=url,
                      endpoint=endpoint, deployment=deployment, api_version=api_version)
    before = read(connection, project_id) or {}
    same_provider = before.get("provider") == clean["provider"]
    secret_ref_id = before.get("secret_ref_id") if same_provider else None
    api_key = (api_key or "").strip()
    if api_key:
        secret_ref_id = _store_key(connection, settings, project_id, org_id, api_key, clean, actor=actor,
                                   accept_database_fallback=accept_database_fallback)
    elif secret_ref_id and before.get("scope") == "project":
        # An older install's project key: re-stored once at org scope, so every project reaches it.
        secret_ref_id = _promote_key(connection, settings, project_id, org_id, str(secret_ref_id), clean, actor)
    elif _BY_PROVIDER.get(clean["provider"], {}).get("needs_key") and not secret_ref_id \
            and not (clean["provider"] == "openai" and clean["base_url"]):
        raise EmbeddingSlotError(f"{_BY_PROVIDER[clean['provider']]['label']} needs its API key.")
    if before.get("secret_ref_id") and before.get("secret_ref_id") != secret_ref_id:
        _revoke(connection, str(before["secret_ref_id"]), actor)
    slot = {**clean, "secret_ref_id": secret_ref_id, "saved_at": _now(), "saved_by": actor}
    _checked(connection, settings, project_id, slot, actor)
    _write(connection, org_id, slot, actor)
    _drop_project_rows(connection, org_id, actor, keep_ref=secret_ref_id)
    emit_event(connection, project_id, "setup.embedding_model_saved", actor, "org_setting", SETTING_KEY,
               {"org_id": org_id, "provider": slot["provider"], "model": slot["model"],
                "dimension": slot["dimension"], "model_id": model_id_of(slot),
                "check": slot["check"]["state"],
                "previous_model_id": model_id_of(before) if before else None})
    return status(connection, settings, project_id)


def check(connection: Connection, settings: Any, project_id: str, *, actor: str = "setup") -> dict[str, Any]:
    """Check the slot this project uses again. An older install's project slot is promoted to
    the org on the way (its key re-stored at org scope)."""
    slot = read(connection, project_id)
    if slot is None:
        raise EmbeddingSlotError("no embedding model is set to check.")
    org_id = _org_or_error(connection, project_id)
    _require_org_authority(connection, org_id)
    if slot.get("scope") == "project" and slot.get("secret_ref_id"):
        slot["secret_ref_id"] = _promote_key(connection, settings, project_id, org_id,
                                             str(slot["secret_ref_id"]), slot, actor)
    _checked(connection, settings, project_id, slot, actor)
    _write(connection, org_id, slot, actor)
    _drop_project_rows(connection, org_id, actor, keep_ref=slot.get("secret_ref_id"))
    return status(connection, settings, project_id)


def clear(connection: Connection, settings: Any, project_id: str, *, actor: str = "setup") -> dict[str, Any]:
    """Remove the org's slot (and any older project rows of the org): search is off everywhere."""
    from ..core import emit_event

    org_id = _org_or_error(connection, project_id)
    _require_org_authority(connection, org_id)
    slot = read(connection, project_id)
    if slot is not None:
        if slot.get("secret_ref_id"):
            _revoke(connection, str(slot["secret_ref_id"]), actor)
        connection.execute(text("DELETE FROM org_settings WHERE org_id = :o AND key = :k"),
                           {"o": org_id, "k": SETTING_KEY})
        _drop_project_rows(connection, org_id, actor, keep_ref=None)
        emit_event(connection, project_id, "setup.embedding_model_cleared", actor, "org_setting",
                   SETTING_KEY, {"org_id": org_id, "model_id": model_id_of(slot)})
    return status(connection, settings, project_id)


# ── the key: one org-scoped secret ───────────────────────────────────────────────────────────

def _key_binding(slot: dict[str, Any]) -> tuple[str, list[str]]:
    """The provider an org key is held to, and the host it may also go to (the operator's own
    endpoint): secret_backends._hold_to_provider refuses it anywhere else."""
    provider = "azure" if slot.get("provider") == "azure" else "openai"
    url = slot.get("endpoint") if provider == "azure" else slot.get("base_url")
    host = (urlsplit(str(url or "")).hostname or "").lower()
    return provider, [host] if host else []


def _store_key(connection: Connection, settings: Any, project_id: str, org_id: str, api_key: str,
               slot: dict[str, Any], *, actor: str, accept_database_fallback: bool,
               backend: Optional[str] = None) -> str:
    from ..secret_backends import default_secret_backend, operator_chosen_backend
    from ..secrets_service import put_secret
    from ..setup_service import SecretFallbackNotAccepted

    if backend is None:
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
                        secret_kind="api_token", backend=backend, scope_type="org", scope_id=org_id,
                        actor=actor)
    provider, hosts = _key_binding(slot)
    connection.execute(text(
        "UPDATE secret_refs SET provider = :prov, metadata = metadata || CAST(:m AS JSONB) WHERE id = :id"),
        {"prov": provider, "m": json.dumps({"allowed_hosts": hosts}), "id": stored["secret_ref_id"]})
    return str(stored["secret_ref_id"])


def _promote_key(connection: Connection, settings: Any, project_id: str, org_id: str, secret_ref_id: str,
                 slot: dict[str, Any], actor: str) -> str:
    """A project-scoped key of an older install, re-stored at org scope in the same backend
    (the operator chose that backend when they stored it). Already org-scoped: kept."""
    from ..secret_backends import resolve_secret_ref

    row = connection.execute(text("SELECT scope_type, storage_backend FROM secret_refs WHERE id = :id"),
                             {"id": secret_ref_id}).mappings().first()
    if row is None or row["scope_type"] == "org":
        return secret_ref_id
    value = resolve_secret_ref(connection, secret_ref_id, settings, project_id=project_id)
    promoted = _store_key(connection, settings, project_id, org_id, value, slot, actor=actor,
                          accept_database_fallback=True, backend=str(row["storage_backend"]))
    _revoke(connection, secret_ref_id, actor)
    return promoted


def _revoke(connection: Connection, secret_ref_id: str, actor: str) -> None:
    connection.execute(text("UPDATE secret_refs SET status = 'revoked', revoked_at = now(), revoked_by = :a "
                            "WHERE id = :id AND status <> 'revoked'"), {"a": actor, "id": secret_ref_id})


# ── what search and Setup read ───────────────────────────────────────────────────────────────

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


def projects_with_a_slot(connection: Connection) -> list[str]:
    """Every project that has a slot to embed with: each project of an org with one, and the
    projects of an older install that still have their own row."""
    found: set[str] = set()
    for sql in ("SELECT p.id FROM projects p JOIN workspaces w ON w.id = p.workspace_id "
                "JOIN org_settings s ON s.org_id = w.org_id AND s.key = :k",
                "SELECT project_id FROM project_settings WHERE key = :k"):
        try:
            with connection.begin_nested():
                found.update(str(p) for p in connection.execute(text(sql), {"k": SETTING_KEY}).scalars().all())
        except Exception:  # noqa: BLE001 — a table an older install lacks
            continue
    return sorted(found)


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
                           "permission": "model_provider:manage", "budget_category": "knowledge",
                           "scope": "org", "place": PLACE,
                           "default_api_version": DEFAULT_AZURE_API_VERSION}
    if slot is None:
        return out
    model_id = model_id_of(slot)
    org_id = slot.get("org_id") or org_of(connection, project_id)
    out.update({
        "scope": slot.get("scope", "org"),
        "projects": len(_org_projects(connection, org_id)) if org_id and slot.get("scope") == "org" else 1,
        "provider": slot["provider"],
        "label": _BY_PROVIDER.get(slot["provider"], {}).get("label", slot["provider"]),
        "model": slot.get("model") or "", "dimension": int(slot.get("dimension") or 0),
        "dimension_set": bool(slot.get("dimension_set")),
        "base_url": _without_userinfo(str(slot.get("base_url") or "")),
        "endpoint": _without_userinfo(str(slot.get("endpoint") or "")),
        "deployment": slot.get("deployment") or "", "api_version": slot.get("api_version") or "",
        "has_key": bool(slot.get("secret_ref_id")),
        "model_id": model_id,
        "check": dict(slot.get("check") or {"state": "unchecked"}),
        "reembed_pending": pending(connection, project_id, model_id) if on else 0,
    })
    return out
