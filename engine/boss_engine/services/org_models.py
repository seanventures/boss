"""Models are the ORG's: set once in Setup › Repo and models, every project of the org uses them.

Filming the banking case study (2026-10-24), the four northpeak-bank-* projects answered "BOSS
needs a connected model … none is answering": the providers, keys and the chat/code/review model
choices lived in boss-dogfood's own rows, so a project created later had none. The operator's
principle, already applied to the embedding model (knowledge/embedding_slot.py, `org_settings`):
set once, every project of the org uses it.

What resolves where — ONE order for every reader, project → org → install:

  * THE MODEL KNOBS (MODEL_KNOBS: chat engine and model, the code agent's engine/model and its
    hard/routine tiers, the reviewer, the CLI models, routing mode, decisions, the Anthropic
    credential order). The org's value is an `org_settings` row `models.<knob>`; a project row
    of the same knob is a PROJECT OVERRIDE (an operator pinned it), visible and removable.
    project_settings.get_all / effective / applied layer them, so every reader — model_routing,
    the code-task path, the router's job pins, chat — gets the same answer from one place.
    Setup writes the org's value (and drops the acting project's override of that knob) unless
    the caller passes scope='project'. Changing the org's needs model_provider:manage for the
    whole org (an Owner or org Admin); a caller with it only for this project (a project Owner)
    writes this project's own value instead — shown as this project's pin. A per-run `model_config` still wins over all of it: it is
    applied on top of the layered settings by the run itself.

  * THE PROVIDERS AND KEYS (entitlement_sources rows). The org's connections are the rows of the
    org's MODEL HOME project (`org_settings` `models.home`); every other project of the org holds
    a MIRROR of each (connection_metadata.org_source = the home), so the gateway's leases,
    budgets and receipts stay per project and nothing that reads entitlement_sources changes.
    A key a mirror points at is the home's secret, re-scoped to the org (secret_refs
    scope_type='org', as the embedding key is) and held to its provider's hosts
    (secret_backends._hold_to_provider; a custom base URL's host is listed in `allowed_hosts`).
    A project's OWN connected row for a source is its override and is never overwritten.
    `sync_sources` makes the mirrors match; it runs after every Setup model write, when a project
    is created, and at API start (the upgrade path).

UPGRADE (db/schema.sql ONE-TIME DATA STEP `org_models_adopted_from_project`): an org with no
model configuration adopts the configuration of its most-configured project (oldest on a tie) —
its model knobs become the org's and leave that project, and it becomes the org's model home.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Iterable, Optional
from urllib.parse import urlsplit

from sqlalchemy import text

logger = logging.getLogger(__name__)

#: The org_settings key prefix for a model knob, and the row naming the org's model home.
PREFIX = "models."
HOME_KEY = "models.home"
PLACE = "Setup › Repo and models › Models"
SCOPES = ("org", "project")

#: The knobs that are the org's (project_settings.KNOBS keys). Everything that says WHICH model or
#: provider answers; budget, concurrency and build-shape knobs stay per project.
MODEL_KNOBS: frozenset[str] = frozenset({
    "code_agent_engine", "code_agent_model",
    "code_review_engine", "code_review_model",
    "code_agent_strong_engine", "code_agent_strong_model",
    "code_agent_low_cost_engine", "code_agent_low_cost_model",
    "codex_cli_model", "claude_cli_model",
    "chat_llm_engine", "chat_model",
    "model_routing_mode", "decision_backend", "decision_model",
    "anthropic_credential_order",
})


class OrgModelsForbidden(PermissionError):
    """The caller may not change the org's models (they are every project's)."""


def is_org_knob(key: str) -> bool:
    return key in MODEL_KNOBS


def _nested(connection: Any):
    begin = getattr(connection, "begin_nested", None)
    return begin() if callable(begin) else _Null()


class _Null:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _decode(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def org_of(connection: Any, project_id: Optional[str]) -> Optional[str]:
    """The org a project belongs to; None when it cannot be said (no such project or tables)."""
    if not project_id or connection is None:
        return None
    try:
        with _nested(connection):
            value = connection.execute(text(
                "SELECT w.org_id FROM projects p JOIN workspaces w ON w.id = p.workspace_id "
                "WHERE p.id = :p"), {"p": project_id}).scalar()
    except Exception:  # noqa: BLE001 — no org is an answer: the project's own rows stand alone
        return None
    return str(value) if value else None


# ── the knobs ────────────────────────────────────────────────────────────────────────────────

def org_values(connection: Any, project_id: Optional[str], *, org_id: Optional[str] = None
               ) -> dict[str, Any]:
    """{knob: value} the project's org has set. Never raises: an install without the table, or a
    project without an org, has none."""
    org_id = org_id or org_of(connection, project_id)
    if not org_id:
        return {}
    try:
        with _nested(connection):
            rows = connection.execute(text(
                "SELECT key, value FROM org_settings WHERE org_id = :o AND key LIKE 'models.%'"),
                {"o": org_id}).mappings().all()
    except Exception:  # noqa: BLE001
        return {}
    out: dict[str, Any] = {}
    for row in rows:
        knob = str(row["key"])[len(PREFIX):]
        if knob in MODEL_KNOBS:
            out[knob] = _decode(row["value"])
    return out


def org_row(connection: Any, org_id: str, knob: str) -> Optional[dict[str, Any]]:
    try:
        with _nested(connection):
            row = connection.execute(text(
                "SELECT value, updated_at, updated_by FROM org_settings "
                "WHERE org_id = :o AND key = :k"), {"o": org_id, "k": PREFIX + knob}
            ).mappings().first()
    except Exception:  # noqa: BLE001
        return None
    return dict(row) if row else None


def write_org(connection: Any, org_id: str, knob: str, value: Any, actor: str) -> None:
    connection.execute(text(
        "INSERT INTO org_settings (org_id, key, value, updated_by, updated_at) "
        "VALUES (:o, :k, CAST(:v AS JSONB), :by, now()) "
        "ON CONFLICT (org_id, key) DO UPDATE SET value = EXCLUDED.value, "
        "updated_by = EXCLUDED.updated_by, updated_at = now()"),
        {"o": org_id, "k": PREFIX + knob, "v": json.dumps(value), "by": actor})


def delete_org(connection: Any, org_id: str, knob: str) -> Any:
    return connection.execute(text(
        "DELETE FROM org_settings WHERE org_id = :o AND key = :k RETURNING value"),
        {"o": org_id, "k": PREFIX + knob}).scalar()


def org_projects(connection: Any, org_id: str) -> list[str]:
    return [str(p) for p in connection.execute(text(
        "SELECT p.id FROM projects p JOIN workspaces w ON w.id = p.workspace_id "
        "WHERE w.org_id = :o ORDER BY p.id"), {"o": org_id}).scalars().all()]


def may_change_org(connection: Any, org_id: str) -> bool:
    """Whether this caller may change the org's models: inside an enforced request it needs
    `model_provider:manage` for the whole org (as the embedding model does). Outside one (the
    host shell, the worker, a test) it is trusted."""
    from ..identity import authz, lineage
    from ..identity.permissions_gen import P
    from ..identity.scope import ResolvedScope

    if not lineage.request_enforced():
        return True
    ctx = lineage.current_context()
    try:
        return bool(ctx is not None and ctx.principal.org_id == org_id and authz.check(
            ctx, P.MODEL_PROVIDER_MANAGE, ResolvedScope.org_level(org_id), db=connection).allowed)
    except Exception:  # noqa: BLE001 — a decision that cannot be made is not an allow
        return False


def resolve_scope(connection: Any, org_id: Optional[str], scope: Optional[str]) -> str:
    """The scope a model write takes. Asked for 'org': the org's, or refused for a caller who
    may not change it. Asked for nothing: the org's for a caller who may (an Owner or org
    Admin), else this project's own — a project Owner configures the project they own, and
    Setup shows it as this project's pin."""
    if scope == "project" or not org_id:
        return "project"
    if may_change_org(connection, org_id):
        return "org"
    if scope == "org":
        require_org_authority(connection, org_id)
    return "project"


def require_org_authority(connection: Any, org_id: str) -> None:
    """Refuse a caller who may not change the org's models (may_change_org)."""
    if may_change_org(connection, org_id):
        return
    raise OrgModelsForbidden(
        "the models are used by every project of the organisation, so changing them needs "
        "model_provider:manage for the whole organisation (an Owner or an org Admin); "
        "send scope='project' to pin this project only.")


def invalidate_org(connection: Any, org_id: Optional[str]) -> None:
    """A knob every project of the org reads changed: drop each project's router snapshot."""
    try:
        from .model_router.store import invalidate
    except Exception:  # noqa: BLE001
        return
    if not org_id:
        invalidate(None)
        return
    try:
        for pid in org_projects(connection, org_id):
            invalidate(pid)
    except Exception:  # noqa: BLE001
        invalidate(None)


# ── the providers: the org's model home and its mirrors ──────────────────────────────────────

def home_project(connection: Any, org_id: Optional[str]) -> Optional[str]:
    """The project whose connections are the org's, or None when the org has not set one."""
    if not org_id:
        return None
    row = org_row(connection, org_id, "home")
    value = _decode(row["value"]) if row else None
    pid = value.get("project_id") if isinstance(value, dict) else None
    return str(pid) if pid else None


def home_for(connection: Any, project_id: Optional[str]) -> Optional[str]:
    return home_project(connection, org_of(connection, project_id))


def set_home(connection: Any, org_id: str, project_id: str, actor: str) -> None:
    write_org(connection, org_id, "home", {"project_id": project_id}, actor)


#: A row that IS a connection (as connected_engines reads one), not a seeded catalogue entry or a
#: connection proof's candidate.
_CONNECTED = ("(connection_status = 'connected' OR (credential_secret_ref_id IS NOT NULL "
              "AND connection_status <> 'revoked')) AND source_id NOT LIKE 'proof-%'")

_COLUMNS = ("provider", "source_kind", "entitlement_kind", "owner_type", "owner_id",
            "workload_classes_allowed", "multi_tenant_allowed", "credential_ref",
            "credential_secret_ref_id", "cost_mode", "limit_shape", "supported_models",
            "task_strengths", "risk_tier_max", "status", "auth_method", "connection_status",
            "connection_ref", "oauth_scopes", "policy_notes", "observed_capacity",
            "runtime_supported")
_JSON = {"workload_classes_allowed", "limit_shape", "supported_models", "task_strengths",
         "oauth_scopes", "policy_notes", "observed_capacity"}


def _meta(value: Any) -> dict[str, Any]:
    value = _decode(value)
    return dict(value) if isinstance(value, dict) else {}


def is_mirror(metadata: Any) -> bool:
    return bool(_meta(metadata).get("org_source"))


def write_target(connection: Any, project_id: str, *, scope: Optional[str] = None,
                 source_id: str = "", actor: str = "setup") -> str:
    """The project a Setup model write lands in. scope='project' (or a source this project
    connected itself) is the project's own; otherwise the org's model home — the acting project
    becoming it when the org has none yet."""
    org_id = org_of(connection, project_id)
    if resolve_scope(connection, org_id, scope) == "project":
        return project_id
    home = home_project(connection, org_id)
    if source_id and home != project_id:
        row = connection.execute(text(
            "SELECT connection_metadata, connection_status, credential_secret_ref_id "
            "FROM entitlement_sources WHERE project_id = :p AND source_id = :s"),
            {"p": project_id, "s": source_id}).mappings().first()
        if row and not is_mirror(row["connection_metadata"]) and (
                row["connection_status"] == "connected" or row["credential_secret_ref_id"]):
            return project_id
    if home is None or org_of(connection, home) != org_id:
        set_home(connection, org_id, project_id, actor)
        return project_id
    return home


def _promote_secret(connection: Any, org_id: str, secret_ref_id: str, provider: str,
                    base_url: str) -> None:
    """The home's key, re-scoped in place to the org (same id, same backend, same value): every
    project's mirror resolves it, and it is held to its provider's hosts from now on."""
    from .env_refs import PROVIDER_HOSTS, provider_binding

    row = connection.execute(text(
        "SELECT scope_type, provider, metadata FROM secret_refs WHERE id = :i"),
        {"i": secret_ref_id}).mappings().first()
    if row is None:
        return
    meta = _meta(row["metadata"])
    # Held to the provider the CONNECTION is for (what Setup knows it as), not whatever the
    # reference recorded (a stored key's row may say only `api_token`).
    held = str(provider or "").strip().lower() or str(row["provider"] or "").strip()
    hosts = list(meta.get("allowed_hosts") or [])
    host = (urlsplit(base_url or "").hostname or "").lower()
    official = provider_binding(held).hosts or PROVIDER_HOSTS.get(held, ())
    if host and host not in official and host not in hosts:
        hosts.append(host)
    if row["scope_type"] == "org" and hosts == list(meta.get("allowed_hosts") or []) \
            and str(row["provider"] or "") == held:
        return
    connection.execute(text(
        "UPDATE secret_refs SET scope_type = 'org', scope_id = :o, provider = :prov, "
        "metadata = COALESCE(metadata, '{}'::jsonb) || CAST(:m AS JSONB) WHERE id = :i"),
        {"o": org_id, "prov": held, "m": json.dumps({"allowed_hosts": hosts}), "i": secret_ref_id})


def sync_sources(connection: Any, org_id: str, *, actor: str = "boss",
                 projects: Optional[Iterable[str]] = None) -> dict[str, int]:
    """Make every project of the org hold the org's connections: a mirror of each connected row
    of the model home, a key re-scoped to the org, a mirror of a connection the home no longer
    has reset to not connected. A project's own connected row is its override and is kept.
    Idempotent. Returns counts."""
    home = home_project(connection, org_id)
    counts = {"mirrored": 0, "reset": 0, "kept_own": 0}
    if not home or org_of(connection, home) != org_id:
        return counts
    sources = [dict(r) for r in connection.execute(text(
        f"SELECT * FROM entitlement_sources WHERE project_id = :p AND {_CONNECTED}"),
        {"p": home}).mappings().all()]
    for row in sources:
        if row.get("credential_secret_ref_id"):
            _promote_secret(connection, org_id, str(row["credential_secret_ref_id"]),
                            str(row["provider"] or ""),
                            str(_meta(row["connection_metadata"]).get("base_url") or ""))
    wanted = {str(r["source_id"]) for r in sources}
    targets = [p for p in (projects if projects is not None else org_projects(connection, org_id))
               if p != home]
    for pid in targets:
        mine = {str(r["source_id"]): dict(r) for r in connection.execute(text(
            "SELECT source_id, connection_status, credential_secret_ref_id, connection_metadata "
            "FROM entitlement_sources WHERE project_id = :p"), {"p": pid}).mappings().all()}
        for row in sources:
            sid = str(row["source_id"])
            have = mine.get(sid)
            if have and not is_mirror(have["connection_metadata"]) and (
                    have["connection_status"] == "connected" or have["credential_secret_ref_id"]):
                counts["kept_own"] += 1
                continue
            meta = {**_meta(row["connection_metadata"]), "org_source": home}
            values = {c: row.get(c) for c in _COLUMNS}
            for c in _JSON:
                values[c] = json.dumps(_decode(values[c]) if values[c] is not None else
                                       ({} if c in ("limit_shape", "observed_capacity") else []))
            values.update({"id": f"src_{pid}_{sid}", "project_id": pid, "source_id": sid,
                           "connection_metadata": json.dumps(meta)})
            cols = ["id", "project_id", "source_id", *_COLUMNS, "connection_metadata"]
            casts = {c: f"CAST(:{c} AS JSONB)" for c in (*_JSON, "connection_metadata")}
            placeholders = ", ".join(casts.get(c, f":{c}") for c in cols)
            updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in (*_COLUMNS, "connection_metadata"))
            connection.execute(text(
                f"INSERT INTO entitlement_sources ({', '.join(cols)}) VALUES ({placeholders}) "
                f"ON CONFLICT (project_id, source_id) DO UPDATE SET {updates}, updated_at = now()"),
                values)
            counts["mirrored"] += 1
        for sid, have in mine.items():
            if sid in wanted or not is_mirror(have["connection_metadata"]):
                continue
            connection.execute(text(
                "UPDATE entitlement_sources SET credential_secret_ref_id = NULL, "
                "connection_status = 'not_connected', status = 'declared', "
                "connection_metadata = (COALESCE(connection_metadata, '{}'::jsonb) - 'org_source') "
                "- 'last_check', updated_at = now() WHERE project_id = :p AND source_id = :s"),
                {"p": pid, "s": sid})
            counts["reset"] += 1
    return counts


def sync_for(connection: Any, project_id: str, *, actor: str = "boss") -> dict[str, int]:
    """sync_sources for the org `project_id` belongs to; never raises (a failed mirror leaves
    the projects as they were, and the next write or start tries again)."""
    org_id = org_of(connection, project_id)
    if not org_id:
        return {}
    try:
        with _nested(connection):
            if home_project(connection, org_id) is None and connection.execute(text(
                    f"SELECT 1 FROM entitlement_sources WHERE project_id = :p AND {_CONNECTED} "
                    "LIMIT 1"), {"p": project_id}).first() is not None:
                # The org's first connection, made somewhere other than Setup's model door
                # (a deprecated route, the host shell): that project is the org's model home.
                set_home(connection, org_id, project_id, actor)
            counts = sync_sources(connection, org_id, actor=actor)
    except Exception:  # noqa: BLE001
        logger.exception("could not mirror the org's model connections for %s", org_id)
        return {}
    invalidate_org(connection, org_id)
    try:
        from . import setup_view

        setup_view.forget_cached("")
    except Exception:  # noqa: BLE001
        pass
    return counts


def sync_all(connection: Any, *, actor: str = "boss") -> dict[str, dict[str, int]]:
    """Every org with a model home (API start: the upgrade path)."""
    try:
        with _nested(connection):
            orgs = [str(o) for o in connection.execute(text(
                "SELECT org_id FROM org_settings WHERE key = :k"), {"k": HOME_KEY}).scalars().all()]
    except Exception:  # noqa: BLE001
        return {}
    out = {}
    for org_id in orgs:
        try:
            with _nested(connection):
                out[org_id] = sync_sources(connection, org_id, actor=actor)
        except Exception:  # noqa: BLE001
            logger.exception("could not mirror the org's model connections for %s", org_id)
    return out


# ── what Setup shows ─────────────────────────────────────────────────────────────────────────

def view(connection: Any, project_id: str) -> dict[str, Any]:
    """The Models step's scope line: whose models these are, and this project's overrides."""
    org_id = org_of(connection, project_id)
    own: dict[str, Any] = {}
    try:
        with _nested(connection):
            rows = connection.execute(text(
                "SELECT key, value FROM project_settings WHERE project_id = :p"),
                {"p": project_id}).mappings().all()
        own = {str(r["key"]): _decode(r["value"]) for r in rows if str(r["key"]) in MODEL_KNOBS}
    except Exception:  # noqa: BLE001
        own = {}
    org = org_values(connection, project_id, org_id=org_id) if org_id else {}
    home = home_project(connection, org_id) if org_id else None
    own_sources: list[str] = []
    try:
        with _nested(connection):
            rows = connection.execute(text(
                f"SELECT source_id, connection_metadata FROM entitlement_sources "
                f"WHERE project_id = :p AND {_CONNECTED}"), {"p": project_id}).mappings().all()
        if home and home != project_id:
            own_sources = sorted(str(r["source_id"]) for r in rows
                                 if not is_mirror(r["connection_metadata"]))
    except Exception:  # noqa: BLE001
        own_sources = []
    from .project_settings import KNOBS

    return {"scope": "org" if org_id else "project", "org_id": org_id, "home_project": home,
            "is_home": bool(home and home == project_id), "place": PLACE,
            "permission": "model_provider:manage",
            "org_values": org,
            "overrides": [{"key": k, "label": (KNOBS.get(k) or {}).get("label", k), "value": v,
                           "org_value": org.get(k)} for k, v in sorted(own.items())],
            "own_sources": own_sources}
