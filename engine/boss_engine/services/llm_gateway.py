"""An operator-named LLM gateway for the coding CLIs (BOSSAPP-887).

WHY. Enterprises route every LLM call through one gateway — LiteLLM, Portkey, an internal proxy —
for audit, spend and data-residency. Chat lanes could take a base URL; the coding harnesses only
passed ANTHROPIC_BASE_URL through from the install's environment, so a build either bypassed the
gateway or the whole install was pointed at it by hand.

WHAT. A gateway row names a PROTOCOL (`openai` — any OpenAI-compatible endpoint — or `anthropic`),
a BASE URL and a secret, for one project or for the whole org (a project's own wins). Each
harness speaks one protocol (HARNESS_PROTOCOL): `claude_code` → anthropic, `codex` → openai. When
a run of that harness starts in a project with an active gateway for its protocol, the launcher
routes it there:

  * THROUGH THE MODEL PROXY (model_proxy), never with the key. The CLI gets a run token and the
    loopback base URL; the proxy forwards to the gateway with the real key, only on the
    protocol's model-API paths, and the token dies with the session. The sandbox never holds the
    gateway's key.
  * IN PLACE OF the harness's own login: the Claude subscription token / the Codex ChatGPT login
    is taken out of that run's environment, so a gateway-routed run cannot fall back to going
    direct. A gateway whose key cannot be read refuses the run (GatewayUnavailable) rather than
    quietly going direct.

With no gateway row nothing changes: `for_harness` answers None and every launcher does exactly
what it did. The key is written to the secret store once (`secret_key`) and never read back to a
caller, logged or returned — only whether one is set.

DEFERRED, SAID PLAINLY: Bedrock (SigV4 signing) is not a protocol here; other harnesses are not
routed (they keep their own credentials); a gateway's model names are the operator's to match.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Optional
from urllib.parse import urlsplit

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .core import emit_event, make_id

_log = logging.getLogger("boss.llm_gateway")

PROTOCOLS = ("openai", "anthropic")
#: Which protocol each routed harness speaks.
HARNESS_PROTOCOL: dict[str, str] = {"claude_code": "anthropic", "codex": "openai"}
SECRET_SCOPE = "llm_gateway"
CACHE_TTL_S = 30.0
TEST_TIMEOUT_S = 15.0

_LOCK = threading.Lock()
_CACHE: dict[tuple[str, str], tuple[float, Optional[dict[str, Any]]]] = {}


class GatewayError(ValueError):
    """A gateway that cannot be saved as given."""


class GatewayUnavailable(RuntimeError):
    """A project routes this harness through a gateway, and the gateway cannot be used now."""


def reset_cache() -> None:
    with _LOCK:
        _CACHE.clear()


def normalise_url(value: Any) -> str:
    """https://gw.example.com[/prefix] with no credentials, query or fragment; a trailing /v1 is
    dropped because the CLIs and the proxy add the API path themselves."""
    url = str(value or "").strip().rstrip("/")
    parts = urlsplit(url)
    if parts.scheme not in ("https", "http") or not parts.hostname:
        raise GatewayError("base_url must be an http(s) URL, e.g. https://llm-gateway.example.com")
    if parts.username or parts.password:
        raise GatewayError("put the key in the secret field, never in the URL")
    if parts.query or parts.fragment:
        raise GatewayError("base_url takes no query string or fragment")
    if url.endswith("/v1"):
        url = url[:-3]
    return url.rstrip("/")


def secret_key_for(gateway_id: str) -> str:
    from .secrets_service import ref_key_for

    return ref_key_for(SECRET_SCOPE, gateway_id)


def _store(settings: Any):
    from .secret_backends import backend_for, default_secret_backend

    return backend_for(default_secret_backend(settings), settings)


def _public(row: Any) -> dict[str, Any]:
    harnesses = row["harnesses"]
    if isinstance(harnesses, str):
        harnesses = json.loads(harnesses)
    last = row["last_test"]
    if isinstance(last, str):
        last = json.loads(last)
    iso = lambda v: v.isoformat() if v is not None and hasattr(v, "isoformat") else v  # noqa: E731
    return {"id": row["id"], "scope": "project" if row["project_id"] else "org",
            "project_id": row["project_id"], "org_id": row["org_id"], "name": row["name"],
            "protocol": row["protocol"], "base_url": row["base_url"],
            "harnesses": list(harnesses or []), "status": row["status"],
            "secret_set": bool(row["secret_key"]), "last_test": last,
            "routes": sorted(h for h, p in HARNESS_PROTOCOL.items() if p == row["protocol"]
                             and (not harnesses or h in harnesses)),
            "created_by": row["created_by"], "updated_by": row["updated_by"],
            "created_at": iso(row["created_at"]), "updated_at": iso(row["updated_at"])}


def _org_of(connection: Connection, project_id: str) -> Optional[str]:
    return connection.execute(text(
        "SELECT w.org_id FROM projects p JOIN workspaces w ON w.id = p.workspace_id "
        "WHERE p.id = :p"), {"p": project_id}).scalar()


def list_gateways(connection: Connection, project_id: str) -> list[dict[str, Any]]:
    org = _org_of(connection, project_id)
    rows = connection.execute(text(
        "SELECT * FROM llm_gateways WHERE project_id = :p OR (project_id IS NULL AND org_id = :o) "
        "ORDER BY (project_id IS NULL), protocol, created_at"),
        {"p": project_id, "o": org}).mappings().all()
    return [_public(r) for r in rows]


def _clean_harnesses(value: Any, protocol: str) -> list[str]:
    names = sorted({str(h).strip().lower() for h in (value or []) if str(h).strip()})
    wrong = [h for h in names if HARNESS_PROTOCOL.get(h) != protocol]
    if wrong:
        speaks = ", ".join(h for h, p in HARNESS_PROTOCOL.items() if p == protocol) or "none"
        raise GatewayError(f"{', '.join(wrong)} cannot be routed through a {protocol} gateway "
                           f"(it speaks: {speaks})")
    return names


def create_gateway(connection: Connection, settings: Any, project_id: str, *, actor: str,
                   name: str, protocol: str, base_url: str, secret: str,
                   scope: str = "project", harnesses: Optional[list[str]] = None) -> dict:
    protocol = str(protocol or "").strip().lower()
    if protocol not in PROTOCOLS:
        raise GatewayError("protocol must be openai (any OpenAI-compatible gateway) or anthropic")
    label = " ".join(str(name or "").split())[:80]
    if not label:
        raise GatewayError("name the gateway, e.g. LiteLLM")
    url = normalise_url(base_url)
    if not str(secret or "").strip():
        raise GatewayError("the gateway's key is required; it goes to the secret store only")
    if scope not in ("project", "org"):
        raise GatewayError("scope must be project or org")
    org = _org_of(connection, project_id)
    if scope == "org" and not org:
        raise GatewayError("this project belongs to no organization; add a project gateway")
    routes = _clean_harnesses(harnesses, protocol)
    gid = make_id("llmgw")
    key = secret_key_for(gid)
    _store(settings).put(key, str(secret).strip())       # the value leaves here, never returns
    try:
        row = connection.execute(text(
            "INSERT INTO llm_gateways (id, org_id, project_id, name, protocol, base_url, "
            "secret_key, harnesses, created_by) VALUES (:i, :o, :p, :n, :pr, :u, :k, "
            "CAST(:h AS JSONB), :a) RETURNING *"),
            {"i": gid, "o": org, "p": project_id if scope == "project" else None, "n": label,
             "pr": protocol, "u": url, "k": key, "h": json.dumps(routes), "a": actor}
        ).mappings().one()
    except Exception as exc:
        try:
            _store(settings).delete(key)
        except Exception:  # noqa: BLE001 — a stray store key is better than a hidden failure
            pass
        if "uq_llm_gateways" in str(exc):
            raise GatewayError(f"this {scope} already has an active {protocol} gateway; disable "
                               "it first") from exc
        raise
    reset_cache()
    gw = _public(row)
    emit_event(connection, project_id, "llm_gateway.created", actor, "llm_gateway", gid,
               {"name": label, "protocol": protocol, "base_url": url, "scope": scope,
                "harnesses": routes})
    return gw


def _owned(connection: Connection, project_id: str, gateway_id: str) -> dict[str, Any]:
    gw = next((g for g in list_gateways(connection, project_id) if g["id"] == gateway_id), None)
    if gw is None:
        raise LookupError(f"no gateway {gateway_id!r} for this project or its org")
    return gw


def update_gateway(connection: Connection, settings: Any, project_id: str, gateway_id: str, *,
                   actor: str, name: Optional[str] = None, base_url: Optional[str] = None,
                   harnesses: Optional[list[str]] = None, status: Optional[str] = None,
                   secret: Optional[str] = None) -> dict[str, Any]:
    gw = _owned(connection, project_id, gateway_id)
    sets: dict[str, Any] = {}
    if name is not None:
        label = " ".join(str(name).split())[:80]
        if not label:
            raise GatewayError("name the gateway")
        sets["name"] = label
    if base_url is not None:
        sets["base_url"] = normalise_url(base_url)
    if harnesses is not None:
        sets["harnesses"] = json.dumps(_clean_harnesses(harnesses, gw["protocol"]))
    if status is not None:
        if status not in ("active", "disabled"):
            raise GatewayError("status must be active or disabled")
        sets["status"] = status
    if secret is not None:
        if not str(secret).strip():
            raise GatewayError("a rotated key cannot be empty")
        _store(settings).put(secret_key_for(gateway_id), str(secret).strip())
        sets["last_test"] = None
    if not sets:
        return gw
    clause = ", ".join(f"{k} = CAST(:{k} AS JSONB)" if k in ("harnesses", "last_test")
                       else f"{k} = :{k}" for k in sets)
    try:
        row = connection.execute(text(
            f"UPDATE llm_gateways SET {clause}, updated_by = :a, updated_at = now() "
            "WHERE id = :i RETURNING *"), {**sets, "a": actor, "i": gateway_id}).mappings().one()
    except Exception as exc:
        if "uq_llm_gateways" in str(exc):
            raise GatewayError("another active gateway already serves this protocol here; "
                               "disable it first") from exc
        raise
    reset_cache()
    emit_event(connection, project_id, "llm_gateway.changed", actor, "llm_gateway", gateway_id,
               {"changed": sorted(k for k in sets if k != "last_test"),
                "secret_rotated": secret is not None})
    return _public(row)


def delete_gateway(connection: Connection, settings: Any, project_id: str, gateway_id: str, *,
                   actor: str) -> dict[str, Any]:
    gw = _owned(connection, project_id, gateway_id)
    connection.execute(text("DELETE FROM llm_gateways WHERE id = :i"), {"i": gateway_id})
    try:
        _store(settings).delete(secret_key_for(gateway_id))
    except Exception:  # noqa: BLE001 — the row is gone; a store that kept the key is logged
        _log.warning("could not delete the stored key of gateway %s", gateway_id)
    reset_cache()
    emit_event(connection, project_id, "llm_gateway.deleted", actor, "llm_gateway", gateway_id,
               {"name": gw["name"], "protocol": gw["protocol"]})
    return {"deleted": gateway_id}


# ── the live test (Setup's "Test") ───────────────────────────────────────────────────────────

def _probe(method: str, url: str, headers: dict[str, str]) -> tuple[int, Any]:
    import httpx

    with httpx.Client(timeout=TEST_TIMEOUT_S, follow_redirects=False) as client:
        resp = client.request(method, url, headers=headers)
        try:
            body = resp.json()
        except ValueError:
            body = None
        return resp.status_code, body


def live_test(connection: Connection, settings: Any, project_id: str, gateway_id: str, *,
              actor: str, probe=None) -> dict[str, Any]:
    """One live call: GET <base>/v1/models with the stored key. Says ok/status and how many
    models the gateway offers — never the key, never the gateway's raw answer."""
    gw = _owned(connection, project_id, gateway_id)
    gw_key = secret_key_for(gateway_id)
    try:
        secret = (_store(settings).get(gw_key) or "").strip()
    except Exception:  # noqa: BLE001 — not stored, or the store did not answer
        secret = ""
    if not secret:
        result = {"ok": False, "status": None, "detail": f"no key is stored as {gw_key}"}
    else:
        headers = ({"authorization": f"Bearer {secret}"} if gw["protocol"] == "openai" else
                   {"x-api-key": secret, "anthropic-version": "2023-06-01"})
        try:
            status, body = (probe or _probe)("GET", gw["base_url"] + "/v1/models", headers)
            models = len(body.get("data") or []) if isinstance(body, dict) else None
            ok = 200 <= int(status) < 300
            result = {"ok": ok, "status": int(status),
                      "detail": (f"the gateway answered and offers {models} models"
                                 if ok and models is not None else
                                 "the gateway answered" if ok else
                                 "the gateway refused the key" if status in (401, 403) else
                                 f"the gateway answered {status}")}
        except Exception as exc:  # noqa: BLE001 — a network failure is an answer here
            result = {"ok": False, "status": None,
                      "detail": f"could not reach the gateway ({type(exc).__name__})"}
    from datetime import datetime, timezone

    result["at"] = datetime.now(timezone.utc).isoformat()
    connection.execute(text(
        "UPDATE llm_gateways SET last_test = CAST(:t AS JSONB) WHERE id = :i"),
        {"t": json.dumps(result), "i": gateway_id})
    emit_event(connection, project_id, "llm_gateway.tested", actor, "llm_gateway", gateway_id,
               {"ok": result["ok"], "status": result["status"]})
    return {"gateway_id": gateway_id, **result}


# ── the launchers' seam ──────────────────────────────────────────────────────────────────────

def _active_for(connection: Connection, project_id: str, harness: str) -> Optional[dict]:
    protocol = HARNESS_PROTOCOL.get(harness)
    if not protocol:
        return None
    for gw in list_gateways(connection, project_id):
        if gw["status"] == "active" and gw["protocol"] == protocol and (
                not gw["harnesses"] or harness in gw["harnesses"]):
            return gw                     # project rows come first (list order)
    return None


def for_harness(harness: str, settings: Any = None, *, project_id: Optional[str] = None,
                connection: Optional[Connection] = None) -> Optional[dict[str, Any]]:
    """The active gateway this harness's run is routed through, or None (the default: no change).

    The project comes from the run's LLM context when not given. Cached briefly; a database that
    cannot be read means no gateway is known — the launch proceeds as it always did."""
    if harness not in HARNESS_PROTOCOL:
        return None
    if project_id is None:
        try:
            from .llm_telemetry import get_llm_context

            project_id = get_llm_context().get("project_id")
        except Exception:  # noqa: BLE001
            project_id = None
    if not project_id:
        return None
    cache_key = (str(project_id), harness)
    with _LOCK:
        hit = _CACHE.get(cache_key)
        if hit and time.monotonic() < hit[0]:
            return hit[1]
    found: Optional[dict[str, Any]] = None
    try:
        if connection is not None:
            with connection.begin_nested():
                found = _active_for(connection, str(project_id), harness)
        else:
            from ..db import get_engine
            from .llm_telemetry import get_llm_context

            engine = get_llm_context().get("db_engine") or get_engine(settings)
            with engine.connect() as c:
                found = _active_for(c, str(project_id), harness)
    except Exception:  # noqa: BLE001 — no table, no database: no gateway is known
        found = None
    with _LOCK:
        _CACHE[cache_key] = (time.monotonic() + CACHE_TTL_S, found)
    return found


def isolate_codex_home(env: dict, *, home: str) -> dict:
    """A gateway-routed codex run gets a CODEX_HOME of its own beside the usual one, with the
    config copied and NO login file, so the ChatGPT login cannot be used on it — the same home
    the "API key" pick uses (cli_harness.apply_codex_pick). Called before the agent's kit is
    laid over the environment, exactly where the pick is applied."""
    import os
    import shutil

    key_home = home.rstrip("/") + "-apikey"
    os.makedirs(key_home, mode=0o700, exist_ok=True)
    config = os.path.join(home, "config.toml")
    if os.path.isfile(config) and not os.path.exists(os.path.join(key_home, "config.toml")):
        shutil.copyfile(config, os.path.join(key_home, "config.toml"))
    env["CODEX_HOME"] = key_home
    return env


def apply(env: dict, gateway: dict[str, Any], *, harness: str, settings: Any = None,
          proxy: Any = None) -> dict:
    """Route this run's `env` through `gateway`: the harness's own login out, a model-proxy run
    token and the loopback base URL in, the proxy's upstream the gateway. Raises
    GatewayUnavailable rather than go direct when the key cannot be read."""
    from . import model_proxy

    try:
        secret = (_store(settings).get(gateway.get("secret_key") or secret_key_for(gateway["id"]))
                  or "").strip()
    except Exception:  # noqa: BLE001
        secret = ""
    if not secret:
        raise GatewayUnavailable(
            f"this project routes {harness} through the gateway {gateway['name']!r}, and its key "
            "could not be read from the secret store. Save the key again in Setup › Gateways, or "
            "disable the gateway to go direct.")
    if gateway["protocol"] == "anthropic":
        for name in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY"):
            env.pop(name, None)
        model_proxy.shield(env, "anthropic", secret, settings=settings, proxy=proxy,
                           upstream=gateway["base_url"])
    else:
        for name in ("OPENAI_API_KEY", "CODEX_API_KEY"):
            env.pop(name, None)
        model_proxy.shield(env, "openai", secret, settings=settings, proxy=proxy,
                           upstream=gateway["base_url"])
    # Whatever else the environment still carries is shielded as before.
    return model_proxy.shield_env(env, settings=settings, proxy=proxy)
