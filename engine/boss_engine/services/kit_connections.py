"""The Kit's connections and their live tests (skills-mcp spec §5.3 Connection, §6.4).

An ability may depend on at most one CONNECTION — an account or a key: SMTP for Email, a
tracker for File tickets, Slack or Mattermost for Team chat, a search key for Web search, a
catalogue MCP server for Library docs. This module is the one place that knows, per ability:

* what is stored and how a screen may see it (`view`: text fields as typed, a secret only as
  `…a1f3`, never its value);
* how to TEST the values an operator typed BEFORE storing them (`connect`): Save & test is
  atomic — a failing test writes nothing, and says which field the provider named;
* what the ability's `Test` does against what is stored (`run_test`);
* the dependencies that live in other areas (GitHub in Setup, deploy targets in Infra).

NETWORK OUTSIDE TRANSACTIONS. Every function that talks to a provider takes `begin` (a context
manager factory, as mcp_registry.servers_with_tools does) and holds no database connection
while it waits on someone else's server.

Secrets go through the secret store (secrets_service.put_secret) and only their reference is
kept. Nothing here returns, logs or emits a secret.
"""
from __future__ import annotations

import json
import re
import time
from contextlib import AbstractContextManager
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .core import emit_event, make_id

Begin = Callable[[], AbstractContextManager]

#: Every provider test gives up after this long (spec §6.4).
TEST_TIMEOUT_SECONDS = 15.0

#: project_configurations keys for the connections that have no table of their own.
TEAM_CHAT_KEY = "kit_team_chat"
WEB_SEARCH_KEY = "kit_web_search"


class KitFieldError(ValueError):
    """A test of entered values failed. `field` names the input the provider objected to."""

    def __init__(self, message: str, field: Optional[str] = None, *, latency_ms: int = 0):
        super().__init__(message)
        self.field = field
        self.latency_ms = latency_ms


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def result(ok: bool, *, fact: Optional[str] = None, error: Optional[str] = None,
           field: Optional[str] = None, started: Optional[float] = None,
           latency_ms: Optional[int] = None) -> dict[str, Any]:
    """A TestResult (kit-types.ts)."""
    if latency_ms is None:
        latency_ms = int((time.monotonic() - started) * 1000) if started is not None else 0
    return {"ok": bool(ok), "at": now_iso(), "latency_ms": int(latency_ms),
            "fact": fact, "error": None if ok else (error or "Test failed"),
            "field": None if ok else field}


_TOKEN = re.compile(r"\b40[13]\b|bad credentials|unauthori[sz]ed|forbidden|invalid api key|"
                    r"invalid_auth|token rejected|authentication|\b535\b", re.I)
_REACH = re.compile(r"connecterror|connection refused|unreachable|timed out|timeout|can't reach|"
                    r"no answer|name or service|nodename|gaierror|urlopen error|\b5\d\d\b|"
                    r"bad gateway|service unavailable", re.I)
_TLS = re.compile(r"starttls|smtpnotsupported|ssl|wrong version number|tls", re.I)
_NOT_FOUND = re.compile(r"\b404\b|not found", re.I)
_MISSING = re.compile(r"\bneeds\b|\brequired\b|\bfirst\b", re.I)
_RAW = re.compile(r"[{}\":]|error|exception|http", re.I)


def short_error(message: Optional[str]) -> str:
    """At most six plain words for a red line; the whole message stays in the tooltip.

    NEVER THE RAW TEXT. A provider's own words ("SMTPNotSupportedError: STARTTLS extension
    not supported", `HTTP 502: {"message":`) are classified — a rejected token, a server that
    cannot be reached, a TLS mismatch, not found — and a message BOSS wrote itself, short and
    plain, is kept. apps/web/app/skills/kit-copy.ts `shortError` applies the same rules."""
    said = str(message or "").strip()
    if _TOKEN.search(said):
        return "Token rejected"
    if _REACH.search(said):
        return "Can't reach server"
    if _TLS.search(said):
        return "TLS setting mismatch"
    if _NOT_FOUND.search(said):
        return "Not found"
    words = said.split()
    if words and len(words) <= 6 and not _RAW.search(said):
        return said
    if _MISSING.search(said):
        return "Missing a setting"
    words = said.split()
    if words and len(words) <= 6 and not _RAW.search(said):
        return said
    return "Test failed"


# --------------------------------------------------------------------------- #
# project_configurations rows (team chat, web search)                         #
# --------------------------------------------------------------------------- #

def _config(connection: Connection, project_id: str, key: str) -> Optional[dict[str, Any]]:
    try:
        found = connection.execute(
            text("SELECT body FROM project_configurations WHERE project_id = :p "
                 "AND config_key = :k"), {"p": project_id, "k": key}).scalar()
    except Exception:  # noqa: BLE001 — an install predating the table has nothing stored
        return None
    if isinstance(found, str):
        try:
            found = json.loads(found)
        except ValueError:
            return None
    return found if isinstance(found, dict) else None


def _put_config(connection: Connection, project_id: str, key: str, body: dict[str, Any],
                actor: str) -> None:
    connection.execute(
        text("""INSERT INTO project_configurations (id, project_id, config_key, body, updated_by)
                VALUES (:id, :p, :k, CAST(:body AS JSONB), :actor)
                ON CONFLICT (project_id, config_key) DO UPDATE
                  SET body = EXCLUDED.body, updated_by = EXCLUDED.updated_by,
                      updated_at = now()"""),
        {"id": make_id("cfg"), "p": project_id, "k": key, "body": json.dumps(body),
         "actor": actor})


def _drop_config(connection: Connection, project_id: str, key: str) -> None:
    connection.execute(
        text("DELETE FROM project_configurations WHERE project_id = :p AND config_key = :k"),
        {"p": project_id, "k": key})


def _store_secret(connection: Connection, settings: Any, project_id: str, name: str,
                  value: str, actor: str) -> dict[str, Any]:
    from .secret_backends import default_secret_backend
    from .secrets_service import put_secret

    return put_secret(connection, settings, project_id=project_id, name=name, value=value,
                      secret_kind="api_token", backend=default_secret_backend(settings),
                      actor=actor)


def _resolve(connection: Connection, settings: Any, ref: Optional[str], project_id: str) -> str:
    """A stored reference's value, for THIS project only (id-sec-final2: a configuration a person
    sent could name another project's reference id)."""
    if not ref:
        return ""
    from .secret_backends import resolve_secret_ref

    return resolve_secret_ref(connection, str(ref), settings, project_id=project_id) or ""


def chat_settings(connection: Connection, settings: Any, project_id: str
                  ) -> Optional[dict[str, Any]]:
    """The project's team-chat connection with its token resolved, for the executors. None
    when the console has not connected one (the executors then fall back to the environment)."""
    body = _config(connection, project_id, TEAM_CHAT_KEY)
    if not body or not body.get("provider"):
        return None
    out = {k: v for k, v in body.items() if k not in ("secret_ref_id", "last4")}
    out["token"] = _resolve(connection, settings, body.get("secret_ref_id"), project_id)
    return out


def search_settings(connection: Connection, settings: Any, project_id: str) -> dict[str, Any]:
    """{provider, key}: the project's web search provider, Exa without a key by default."""
    body = _config(connection, project_id, WEB_SEARCH_KEY) or {}
    return {"provider": body.get("provider") or "exa",
            "key": _resolve(connection, settings, body.get("secret_ref_id"), project_id)}


# --------------------------------------------------------------------------- #
# Dependencies that live in other areas                                       #
# --------------------------------------------------------------------------- #

def github_state(connection: Connection, settings: Any, project_id: str) -> dict[str, Any]:
    """{connected, repo, api_base, failing}: the project's GitHub connection and primary
    repository, as Setup connected them. The token is resolved separately (`github_token`).

    `failing` is the reason Setup's last check of the connection failed (None when it passed or
    never ran): a connection that exists but fails its check is FAILING, not "needs repo" —
    the operator has connected one, and what they need is the reason it does not work."""
    failing: Optional[str] = None
    try:
        conn_row = connection.execute(
            text("""SELECT c.id, c.api_base_url, c.last_test_status, c.last_error
                      FROM github_connections c
                     WHERE c.project_id = :p AND c.status IN ('configured', 'active')
                     ORDER BY c.updated_at DESC LIMIT 1"""),
            {"p": project_id}).mappings().first()
        row = connection.execute(
            text("""SELECT b.full_name, c.api_base_url, c.last_test_status, c.last_error
                      FROM github_repository_bindings b
                      JOIN github_connections c ON c.id = b.connection_id
                     WHERE b.project_id = :p AND b.status = 'active'
                       AND c.status IN ('configured', 'active')
                     ORDER BY (b.role = 'primary') DESC, b.updated_at DESC LIMIT 1"""),
            {"p": project_id}).mappings().first()
    except Exception:  # noqa: BLE001 — an older schema has no registry: not connected
        conn_row = row = None
    checked = row or conn_row
    if checked is not None and str(checked["last_test_status"] or "") == "failed":
        error = checked["last_error"]
        if isinstance(error, str):
            try:
                error = json.loads(error)
            except ValueError:
                error = {"message": error}
        failing = str((error or {}).get("message") or "Setup's GitHub check failed") \
            if isinstance(error, dict) else str(error or "Setup's GitHub check failed")
    if row is None:
        # BOSSAPP-918: the project's code is in a repository BOSS keeps on this machine. It is
        # connected (builds, reads and CI run against it) — on host "local", with no GitHub API.
        try:
            local = connection.execute(text(
                "SELECT full_name FROM github_repository_bindings WHERE project_id = :p "
                "AND host = 'local' AND status = 'active' AND role <> 'mirror' "
                "ORDER BY (role = 'primary') DESC, created_at LIMIT 1"),
                {"p": project_id}).scalar()
        except Exception:  # noqa: BLE001 — an older schema has no host column
            local = None
        if local:
            return {"connected": True, "repo": str(local), "host": "local", "api_base": None,
                    "failing": None, "has_connection": False}
        env_repo = "/".join(x for x in (getattr(settings, "github_owner", "") or "",
                                        getattr(settings, "github_repo", "") or "") if x)
        if conn_row is None and getattr(settings, "github_token", None) \
                and env_repo.count("/") == 1:
            return {"connected": True, "repo": env_repo, "api_base": "https://api.github.com",
                    "failing": None}
        base = str((conn_row or {}).get("api_base_url") or "https://api.github.com").rstrip("/")
        return {"connected": False, "repo": None, "api_base": base, "failing": failing,
                "has_connection": conn_row is not None}
    base = str(row["api_base_url"] or "https://api.github.com").rstrip("/")
    return {"connected": True, "repo": str(row["full_name"]), "api_base": base,
            "failing": failing, "has_connection": True}


def github_token(connection: Connection, settings: Any, project_id: str,
                 repo: str = "") -> str:
    from .github_connections import github_token_for

    owner, _, name = (repo or "").partition("/")
    return github_token_for(connection, settings, project_id, owner, name)


def deploy_targets(connection: Connection, project_id: str) -> list[dict[str, Any]]:
    try:
        rows = connection.execute(
            text("SELECT * FROM deployment_targets WHERE project_id = :p "
                 "ORDER BY updated_at DESC NULLS LAST LIMIT 5"), {"p": project_id}
        ).mappings().all()
    except Exception:  # noqa: BLE001
        return []
    return [dict(r) for r in rows]


def deps(connection: Connection, settings: Any, project_id: str) -> dict[str, Any]:
    """What the abilities' `depends_on` names, read once per Kit."""
    # SOMEWHERE TO DEPLOY is a target OR a verified host: an app's first deploy creates its own
    # target on the host (delivery_pipeline.deploy_build), so a host is all that is needed up front.
    return {"github": github_state(connection, settings, project_id),
            "deploy_target": bool(deploy_targets(connection, project_id))
            or verified_hosts(connection, project_id) > 0}


def verified_hosts(connection: Connection, project_id: str) -> int:
    try:
        with connection.begin_nested():
            return int(connection.execute(
                text(f"SELECT count(*) FROM deployment_hosts WHERE {_usable_hosts()} "
                     "AND status IN ('verified', 'ready', 'active')"), {"p": project_id}).scalar() or 0)
    except Exception:  # noqa: BLE001 — no hosts table: none
        return 0


# --------------------------------------------------------------------------- #
# What is stored, as a screen may see it                                       #
# --------------------------------------------------------------------------- #

def _mask(last4: Optional[str]) -> str:
    return f"…{last4}" if last4 else "…set"


def _tracker_rows(connection: Connection, project_id: str) -> list[dict[str, Any]]:
    try:
        rows = connection.execute(
            text("SELECT provider_key, is_default, configuration FROM work_tracker_providers "
                 "WHERE project_id = :p ORDER BY is_default DESC, updated_at DESC"),
            {"p": project_id}).mappings().all()
    except Exception:  # noqa: BLE001
        return []
    out = []
    for r in rows:
        config = r["configuration"]
        if isinstance(config, str):
            try:
                config = json.loads(config)
            except ValueError:
                config = {}
        out.append({"provider_key": str(r["provider_key"]), "is_default": bool(r["is_default"]),
                    "configuration": dict(config or {})})
    return out


def kit_tracker(connection: Connection, project_id: str) -> Optional[dict[str, Any]]:
    """The tracker the Kit connected: the default provider whose configuration the Kit wrote
    (`kit: true`) — a seeded row with empty settings is not a connection."""
    for row in _tracker_rows(connection, project_id):
        if row["is_default"] and row["configuration"].get("kit"):
            return row
    return None


def view(connection: Connection, settings: Any, project_id: str, ability: dict[str, Any],
         dep: dict[str, Any]) -> Optional[dict[str, Any]]:
    """{providers, provider, set_fields, acts_as, flow, connected} for an ability with a
    connection; None for one that needs none."""
    spec = ability.get("connection")
    if not spec:
        return None
    providers = [{k: v for k, v in p.items() if k not in ("catalogue",)}
                 for p in spec.get("providers") or []]
    for p in providers:
        p.setdefault("fields", [])
    key = ability["key"]
    provider: Optional[str] = None
    fields: dict[str, str] = {}
    acts_as: Optional[str] = None
    connected = False
    if key == "email":
        # EMAIL (SMTP), THE CONNECTOR (email_config). The local test inbox is offered only when
        # the install turned Mailpit on; a saved row of any age reads as this connector.
        from .email_config import describe
        from .mail_transport import dev_inbox_enabled

        if not dev_inbox_enabled(settings):
            providers = [p for p in providers if p["key"] != "dev_inbox"]
        stored = describe(connection, project_id)
        provider = "smtp"
        if stored.get("mode") == "dev_inbox":
            provider, connected = "dev_inbox", True
            acts_as = "Local test inbox"
        elif stored.get("configured"):
            provider, connected = "smtp", True
            fields = {"url": str(stored.get("url") or ""),
                      "host": str(stored.get("host") or ""), "port": str(stored.get("port") or ""),
                      "from_address": str(stored.get("from_address") or ""),
                      "from_name": str(stored.get("from_name") or ""),
                      "username": str(stored.get("username") or "")}
            if stored.get("has_password"):
                # "set", never any part of the key.
                fields["password"] = "set"
            fields["security"] = ("ssl" if stored.get("use_ssl") else "starttls"
                                  if stored.get("use_tls") else "none")
            acts_as = stored.get("from_address")
        else:
            from .mail_transport import route

            chosen = route(connection, settings, project_id)
            if chosen.source == "env":
                # Configured the old way (environment). It works; say so, and show where.
                provider, connected = "smtp", True
                fields = {"host": str(chosen.settings.email_smtp_host),
                          "from_address": str(chosen.settings.email_from_address or "")}
                acts_as = chosen.settings.email_from_address
    elif key == "file_tickets":
        row = kit_tracker(connection, project_id)
        if row is not None:
            provider, connected = row["provider_key"], True
            config = row["configuration"]
            for field in next((p["fields"] for p in providers if p["key"] == provider), []):
                if field.get("secret"):
                    if config.get("secret_ref_id"):
                        fields[field["name"]] = _mask(config.get("last4"))
                elif config.get(field["name"]) is not None:
                    fields[field["name"]] = str(config.get(field["name"]))
            acts_as = config.get("acts_as") or None
        elif dep["github"]["connected"]:
            provider = "github_issues"
    elif key in ("team_chat", "web_search"):
        body = _config(connection, project_id,
                       TEAM_CHAT_KEY if key == "team_chat" else WEB_SEARCH_KEY) or {}
        provider = body.get("provider") or spec.get("default_provider")
        connected = bool(body.get("provider"))
        if key == "web_search" and not connected:
            # Exa answers without a key (D-3): the default is a working connection.
            connected = bool(next((p for p in providers if p["key"] == provider
                                   and p.get("keyless")), None))
        for field in next((p["fields"] for p in providers if p["key"] == provider), []):
            if field.get("secret"):
                if body.get("secret_ref_id"):
                    fields[field["name"]] = _mask(body.get("last4"))
            elif body.get(field["name"]) is not None:
                fields[field["name"]] = str(body[field["name"]])
        acts_as = body.get("acts_as")
    elif key == "library_docs" or any(p.get("catalogue") for p in spec.get("providers") or []):
        catalogue_key = next((p.get("catalogue") for p in spec.get("providers") or []
                              if p.get("catalogue")), None)
        server = _catalogue_server(connection, project_id, catalogue_key)
        provider = (spec.get("providers") or [{}])[0].get("key")
        connected = server is not None
        acts_as = None
    elif spec.get("flow") == "channels":
        provider = "youtube"
        connected = _youtube_connected(connection, project_id)
    return {"providers": providers, "provider": provider, "set_fields": fields,
            "acts_as": acts_as, "flow": spec.get("flow"), "connected": connected}


def _youtube_connected(connection: Connection, project_id: str) -> bool:
    try:
        return bool(connection.execute(
            text("""SELECT count(*) FROM integration_connections c
                      JOIN integration_definitions d ON d.id = c.integration_definition_id
                     WHERE c.project_id = :p AND d.category = 'media_channel'
                       AND c.status = 'active'"""), {"p": project_id}).scalar())
    except Exception:  # noqa: BLE001
        return False


def _catalogue_server(connection: Connection, project_id: str,
                      catalogue_key: Optional[str]) -> Optional[dict[str, Any]]:
    if not catalogue_key:
        return None
    from .mcp_registry import find_mcp_server

    entry = catalogue_entry(catalogue_key)
    return find_mcp_server(connection, project_id, entry["name"]) if entry else None


# --------------------------------------------------------------------------- #
# The curated MCP catalogue                                                    #
# --------------------------------------------------------------------------- #

_CATALOGUE: Optional[list[dict[str, Any]]] = None


def catalogue(settings: Any = None) -> list[dict[str, Any]]:
    """packages/specpack/kits/mcp_catalogue.yaml, read once."""
    global _CATALOGUE
    if _CATALOGUE is None:
        import yaml

        from ..config import get_settings

        root = (settings or get_settings()).specpack_root / "kits" / "mcp_catalogue.yaml"
        data = yaml.safe_load(root.read_text()) or {}
        _CATALOGUE = [dict(e) for e in data.get("catalogue") or []]
    return [dict(e) for e in _CATALOGUE]


def catalogue_entry(key: str) -> Optional[dict[str, Any]]:
    return next((e for e in catalogue() if e["key"] == key), None)


def add_catalogue_server(connection: Connection, settings: Any, project_id: str, key: str, *,
                         token: Optional[str], actor: str) -> dict[str, Any]:
    """Register a catalogue server (unreviewed, like any server a person adds). Idempotent."""
    from .mcp_registry import find_mcp_server, register_mcp_server, update_mcp_server

    entry = catalogue_entry(key)
    if entry is None:
        raise KitFieldError(f"no catalogue server {key!r}.")
    auth: dict[str, Any] = {"type": entry.get("auth") or "none"}
    if entry.get("header_name"):
        auth["header_name"] = entry["header_name"]
    if token:
        auth["secret"] = token
    from . import kit
    from .mcp_registry import review_mcp_server

    existing = find_mcp_server(connection, project_id, entry["name"])
    if existing is not None:
        server = existing
        if token:
            server = update_mcp_server(connection, project_id, existing["id"], actor=actor,
                                       settings=settings, auth=auth) or existing
    else:
        server = register_mcp_server(
            connection, project_id, entry["name"], entry["url"],
            risk_tier=int(entry.get("risk_default") or 0), data_boundary="public",
            description=entry.get("description"),
            publisher={"id": key, "name": entry.get("label") or key, "kind": "vendor"},
            provenance={"source": "catalogue", "catalogue": key, "trusted": False},
            auth=auth, actor=actor, settings=settings)
    # A FIRST-PARTY CATALOGUE SERVER IS REVIEWED BY BEING CHOSEN: the person picked a curated,
    # vendor-published server at its vendor's own URL, so it does not land as "Review" with
    # every call waiting. (A tier >= 2 tool still asks a person each time — that is the tier.)
    if entry.get("provenance") == "official" and not server.get("trusted"):
        server = review_mcp_server(connection, project_id, server["id"], actor=actor) or server
    # THE AGENTS COME WITH IT: who held it before a disconnect, else the recommended roles.
    kit.restore_server_holders(connection, settings, project_id, server["name"], actor=actor)
    return server


# --------------------------------------------------------------------------- #
# Tests                                                                        #
# --------------------------------------------------------------------------- #

def _http_get(url: str, token: str, timeout: float = TEST_TIMEOUT_SECONDS) -> Any:
    """A GitHub-style read. Raises httpx.HTTPStatusError on a refusal (the status is the fact)."""
    import httpx

    resp = httpx.get(url, headers={"Authorization": f"Bearer {token}",
                                   "Accept": "application/vnd.github+json",
                                   "X-GitHub-Api-Version": "2022-11-28",
                                   "User-Agent": "boss-kit-test"}, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def _said(error: BaseException) -> str:
    from .mcp_registry import _said as said

    return said(error)


def _github_test(begin: Begin, settings: Any, project_id: str, *, push: bool,
                 checks: bool = False) -> dict[str, Any]:
    started = time.monotonic()
    with begin() as connection:
        state = github_state(connection, settings, project_id)
        token = github_token(connection, settings, project_id, state.get("repo") or "") \
            if state["connected"] else ""
    if not state["connected"] or not state["repo"]:
        return result(False, error="Connect your GitHub repo in Setup first", started=started)
    if state.get("host") == "local":
        from .local_repo import exists

        owner, _, name = str(state["repo"]).partition("/")
        if exists(owner, name, settings):
            return result(True, fact=f"{state['repo']} is on this machine; BOSS commits to it "
                                       "and its sandbox runs the checks (no GitHub).", started=started)
        return result(False, error=f"{state['repo']} is not on this machine any more",
                      started=started)
    if not token:
        return result(False, error="No GitHub token for this repo", started=started)
    repo = state["repo"]
    try:
        data = _http_get(f"{state['api_base']}/repos/{repo}", token)
        if checks:
            branch = (data or {}).get("default_branch") or "main"
            runs = _http_get(f"{state['api_base']}/repos/{repo}/commits/{branch}/check-runs"
                             "?per_page=1", token)
            count = int((runs or {}).get("total_count") or 0)
            return result(True, fact=f"CI · {count} checks on {branch}", started=started)
    except Exception as error:  # noqa: BLE001 — the reason IS the result
        return result(False, error=_said(error), started=started)
    permissions = (data or {}).get("permissions") or {}
    if push and not permissions.get("push"):
        return result(False, error=f"{repo} · no push access", started=started)
    return result(True, fact=f"{repo} · {'push' if push else 'read'} ✓", started=started)


def _servers_of(connection: Connection, project_id: str, ability: dict[str, Any]
                ) -> list[dict[str, Any]]:
    from .mcp_registry import find_mcp_server

    out = []
    for entry in ability.get("mcp") or []:
        server = find_mcp_server(connection, project_id, str(entry.get("server") or ""))
        if server is not None:
            out.append(server)
    return out


def _mcp_test(begin: Begin, settings: Any, project_id: str, ability: dict[str, Any]
              ) -> dict[str, Any]:
    from .mcp_registry import auth_headers, probe, record_reachability

    started = time.monotonic()
    with begin() as connection:
        servers = _servers_of(connection, project_id, ability)
        signed = []
        for server in servers:
            try:
                signed.append((server, auth_headers(connection, settings, project_id, server)))
            except Exception as error:  # noqa: BLE001
                signed.append((server, error))
    if not servers:
        return result(False, error="No MCP server for this ability", started=started)
    tools = 0
    failed: Optional[str] = None
    found = []
    for server, headers in signed:
        if isinstance(headers, Exception):
            outcome = {"reachable": False, "tools": [], "error": f"Token unavailable: "
                                                                 f"{_said(headers)}"}
        else:
            outcome = probe(settings, server["url"], timeout=TEST_TIMEOUT_SECONDS,
                            headers=headers)
        found.append((server, outcome))
        if outcome["reachable"]:
            tools += len(outcome["tools"])
        elif failed is None:
            failed = f"{server['name']}: {outcome['error']}"
    with begin() as connection:
        for server, outcome in found:
            record_reachability(connection, project_id, server["id"],
                                reachable=outcome["reachable"], tools=outcome["tools"],
                                error=outcome["error"])
    if failed:
        return result(False, error=failed, started=started)
    elapsed = int((time.monotonic() - started) * 1000)
    return result(True, fact=f"{tools} tool{'s' if tools != 1 else ''}", latency_ms=elapsed)


def _search_call(settings: Any, server: dict[str, Any], headers: dict[str, str], query: str,
                 n: int = 1) -> Any:
    from .mcp_registry import call_tool_unattended

    return call_tool_unattended(settings, server, "search_web", {"query": query, "n": n},
                                timeout=TEST_TIMEOUT_SECONDS, headers=headers)


def search_headers(provider: str, key: str) -> dict[str, str]:
    """What web-intel's search_web reads: the provider in a header, the key as a bearer."""
    headers = {"X-Search-Provider": provider}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return headers


def _search_test(begin: Begin, settings: Any, project_id: str, *, provider: Optional[str] = None,
                 key: Optional[str] = None, field: Optional[str] = None) -> dict[str, Any]:
    from .mcp_registry import find_mcp_server

    started = time.monotonic()
    with begin() as connection:
        server = find_mcp_server(connection, project_id, "web-intel")
        stored = search_settings(connection, settings, project_id)
    if server is None:
        return result(False, error="web-intel is not registered", started=started)
    chosen = provider or stored["provider"]
    secret = key if key is not None else stored["key"]
    try:
        found = _search_call(settings, server, search_headers(chosen, secret), "BOSS test")
    except Exception as error:  # noqa: BLE001
        return result(False, error=_said(error), field=field, started=started)
    items = found.get("results") if isinstance(found, dict) else found
    count = len(items) if isinstance(items, list) else 0
    if isinstance(found, dict) and found.get("error"):
        return result(False, error=str(found["error"])[:300], field=field, started=started)
    return result(True, fact=f"{count} result{'s' if count != 1 else ''} · "
                             f"{chosen.capitalize()}", started=started)


def _tracker_config_with_token(connection: Connection, settings: Any, project_id: str,
                               provider: str, config: dict[str, Any]) -> dict[str, Any]:
    out = dict(config)
    if out.get("credential") == "github_connection":
        out["api_token"] = github_token(connection, settings, project_id)
    elif out.get("secret_ref_id") and not out.get("api_token"):
        out["api_token"] = _resolve(connection, settings, out.get("secret_ref_id"), project_id)
    return out


def _tracker_test(settings: Any, provider: str, config: dict[str, Any]) -> dict[str, Any]:
    from .work_tracker_adapters import get_adapter

    started = time.monotonic()
    try:
        outcome = get_adapter(provider).test_connection(config)
    except Exception as error:  # noqa: BLE001
        return result(False, error=_said(error), field="api_token", started=started)
    if not outcome.get("ok"):
        message = str(outcome.get("message") or "Test failed")
        field = "api_token" if (outcome.get("status") in (401, 403)
                                or "token" in message.lower() or "key" in message.lower()) \
            else None
        return result(False, error=message[:300], field=field, started=started)
    label = {"github_issues": "GitHub Issues", "linear": "Linear", "jira": "Jira",
             "plane": "Plane", "azure_devops": "Azure DevOps"}.get(provider, provider)
    where = (outcome.get("repository") or config.get("team_key") or config.get("project_key")
             or config.get("project") or outcome.get("account") or "")
    passed = result(True, fact=f"{label} · {where}".strip(" ·"), started=started)
    # ACTS AS is an account the provider named (a login, a user), never the repo or project.
    passed["account"] = outcome.get("account") or None
    return passed


SMTP_SECURITY = ("none", "starttls", "ssl")


def smtp_security(values: dict[str, Any]) -> str:
    """none | starttls | ssl: what was chosen, else what the port usually means."""
    chosen = str(values.get("security") or "").strip().lower()
    if chosen in SMTP_SECURITY:
        return chosen
    try:
        port = int(values.get("port") or 587)
    except (TypeError, ValueError):
        port = 587
    return "ssl" if port == 465 else "starttls"


def _smtp_send(settings: Any, values: dict[str, Any], to: str) -> dict[str, Any]:
    """Send test: one message through exactly these values. The fact is the SERVER'S OWN REPLY
    (`250 2.0.0 Ok: queued as …`); a failure is the server's reason in words
    (mail_transport.failure_reason). Neither carries any part of the key."""
    from .mail_transport import failure_reason, send_test

    started = time.monotonic()
    try:
        port = int(values.get("port") or 587)
    except (TypeError, ValueError):
        return result(False, error="Port must be a number", field="port", started=started)
    security = smtp_security(values)
    scoped = settings.model_copy(update={
        "email_smtp_host": values.get("host"), "email_smtp_port": port,
        "email_smtp_username": values.get("username") or None,
        "email_smtp_password": values.get("password") or None,
        "email_from_address": values.get("from_address"),
        "email_from_name": values.get("from_name") or None,
        "email_smtp_use_tls": security == "starttls", "email_smtp_use_ssl": security == "ssl",
        "email_real_send_allowed": True,
    })
    try:
        reply = send_test(scoped, to)
    except Exception as error:  # noqa: BLE001
        said = failure_reason(error, scoped)
        low = (said + " " + type(error).__name__).lower()
        field = ("password" if "auth" in low or "535" in low or "login" in low
                 or "password" in low
                 else "security" if any(k in low for k in ("starttls", "ssl", "tls",
                                                          "wrong version number"))
                 else "from_address" if "from address" in low
                 else "host" if any(k in low for k in ("connect", "refused", "resolve", "nodename", "gaierror",
                                                        "timed out", "name or service"))
                 else None)
        return result(False, error=said[:300], field=field, started=started)
    return result(True, fact=f"{reply} · to {_masked_address(to)}", started=started)


def _masked_address(address: str) -> str:
    local, _, domain = str(address or "").partition("@")
    return f"{local[:1]}…@{domain}" if domain else address


def _chat_post(settings: Any, provider: str, values: dict[str, Any]) -> dict[str, Any]:
    import httpx

    started = time.monotonic()
    token = str(values.get("token") or values.get("bot_token") or "")
    channel = str(values.get("channel") or "").strip()
    try:
        if provider == "slack":
            resp = httpx.post("https://slack.com/api/chat.postMessage",
                              headers={"Authorization": f"Bearer {token}"},
                              json={"channel": channel, "text": "BOSS connected ✓"},
                              timeout=TEST_TIMEOUT_SECONDS)
            resp.raise_for_status()
            data = resp.json()
            if not data.get("ok"):
                error = str(data.get("error") or "Slack refused")
                field = ("bot_token" if error in ("invalid_auth", "not_authed",
                                                  "account_inactive", "token_revoked")
                         else "channel")
                return result(False, error=error, field=field, started=started)
            return result(True, fact=channel or "Slack", started=started)
        base = str(values.get("base_url") or "").rstrip("/")
        resp = httpx.post(f"{base}/api/v4/posts", headers={"Authorization": f"Bearer {token}"},
                          json={"channel_id": channel, "message": "BOSS connected ✓"},
                          timeout=TEST_TIMEOUT_SECONDS)
        resp.raise_for_status()
        return result(True, fact=f"Channel {channel}", started=started)
    except httpx.HTTPStatusError as error:
        code = error.response.status_code
        return result(False, error=_said(error),
                      field="token" if code in (401, 403) else "channel", started=started)
    except Exception as error:  # noqa: BLE001
        return result(False, error=_said(error),
                      field="base_url" if provider == "mattermost" else None, started=started)


def _deploy_test(begin: Begin, settings: Any, project_id: str) -> dict[str, Any]:
    started = time.monotonic()
    with begin() as connection:
        targets = deploy_targets(connection, project_id)
    if not targets:
        return result(False, error="No deploy target yet", started=started)
    from .deployment_connectors import preflight

    target = targets[0]
    try:
        with begin() as connection:
            outcome = preflight(target, settings, connection=connection)
    except Exception as error:  # noqa: BLE001
        return result(False, error=_said(error), started=started)
    name = target.get("app_name") or target.get("name") or "target"
    ok = bool(outcome.get("ok", outcome.get("ready", True)))
    if not ok:
        return result(False, error=str(outcome.get("message") or outcome.get("error")
                                       or "Preflight failed")[:300], started=started)
    return result(True, fact=f"{name} · ready", started=started)


def run_test(begin: Begin, settings: Any, project_id: str, ability: dict[str, Any], *,
             actor_email: Optional[str] = None) -> dict[str, Any]:
    """The ability's `Test`, against what is STORED (spec §6.4). Never raises."""
    test = ability.get("test")
    started = time.monotonic()
    try:
        if test == "github_push_access":
            return _github_test(begin, settings, project_id, push=True)
        if test == "github_read":
            return _github_test(begin, settings, project_id, push=False)
        if test == "ci_checks":
            return _github_test(begin, settings, project_id, push=False, checks=True)
        if test == "mcp_list_tools":
            return _mcp_test(begin, settings, project_id, ability)
        if test == "search_query":
            return _search_test(begin, settings, project_id)
        if test == "deploy_target_preflight":
            return _deploy_test(begin, settings, project_id)
        if test == "tracker_auth_and_project":
            with begin() as connection:
                row = kit_tracker(connection, project_id)
                if row is None:
                    return result(False, error="No tracker connected", started=started)
                config = _tracker_config_with_token(connection, settings, project_id,
                                                    row["provider_key"], row["configuration"])
            return _tracker_test(settings, row["provider_key"], config)
        if test == "smtp_send":
            from .mail_transport import route

            with begin() as connection:
                chosen = route(connection, settings, project_id)
            if not chosen.ready:
                return result(False, error=chosen.detail, started=started)
            scoped = chosen.settings
            values = {"host": scoped.email_smtp_host, "port": scoped.email_smtp_port,
                      "username": scoped.email_smtp_username,
                      "password": scoped.email_smtp_password,
                      "from_address": scoped.email_from_address,
                      "from_name": scoped.email_from_name,
                      "security": ("ssl" if scoped.email_smtp_use_ssl else "starttls"
                                   if scoped.email_smtp_use_tls else "none")}
            return _smtp_send(settings, values, actor_email or str(scoped.email_from_address))
        if test == "chat_post":
            with begin() as connection:
                stored = chat_settings(connection, settings, project_id)
            if not stored:
                return result(False, error="No chat connected", started=started)
            return _chat_post(settings, str(stored["provider"]), stored)
        if test == "channel_connected":
            with begin() as connection:
                ok = _youtube_connected(connection, project_id)
            return result(ok, fact="Channel connected" if ok else None,
                          error=None if ok else "No channel connected", started=started)
    except Exception as error:  # noqa: BLE001 — a test that crashed failed; say how
        return result(False, error=_said(error), started=started)
    return result(False, error="This ability has no test", started=started)


# --------------------------------------------------------------------------- #
# Save & test: test what was TYPED, store only on a pass                       #
# --------------------------------------------------------------------------- #

def _required(provider: dict[str, Any], fields: dict[str, Any], stored: dict[str, str]
              ) -> None:
    for field in provider.get("fields") or []:
        if field.get("optional"):
            continue
        value = str(fields.get(field["name"]) or "").strip()
        if not value and not (field.get("secret") and stored.get(field["name"])):
            raise KitFieldError(f"{field['label']} is required", field["name"])


def connect(begin: Begin, settings: Any, project_id: str, ability: dict[str, Any], *,
            provider_key: str, fields: dict[str, Any], actor: str,
            actor_email: Optional[str] = None) -> dict[str, Any]:
    """Save & test (spec §6.4). Tests the ENTERED values; stores them only if the test passes.

    Raises KitFieldError (the API answers 422 {field, message}) and writes NOTHING when the
    test fails. Returns the passing TestResult."""
    spec = ability.get("connection") or {}
    provider = next((p for p in spec.get("providers") or [] if p["key"] == provider_key), None)
    if provider is None:
        raise KitFieldError(f"{ability['label']} has no provider {provider_key!r}.", None)
    fields = {k: (v.strip() if isinstance(v, str) else v) for k, v in (fields or {}).items()}
    key = ability["key"]
    with begin() as connection:
        current = view(connection, settings, project_id, ability,
                       deps(connection, settings, project_id)) or {}
    stored = current.get("set_fields") or {} if current.get("provider") == provider_key else {}
    if not provider.get("reuses") and not provider.get("catalogue"):
        _required(provider, fields, stored)

    if key == "email":
        return _connect_email(begin, settings, project_id, provider_key, fields, actor,
                              actor_email)
    if key == "file_tickets":
        return _connect_tracker(begin, settings, project_id, provider, fields, actor)
    if key == "team_chat":
        return _connect_chat(begin, settings, project_id, provider_key, fields, actor)
    if key == "web_search":
        return _connect_search(begin, settings, project_id, provider, fields, actor)
    if provider.get("catalogue"):
        return _connect_catalogue(begin, settings, project_id, ability, provider, fields, actor)
    raise KitFieldError(f"{ability['label']} is connected elsewhere.", None)


def _fail(outcome: dict[str, Any]) -> None:
    if not outcome["ok"]:
        raise KitFieldError(str(outcome.get("error") or "Test failed"), outcome.get("field"),
                            latency_ms=int(outcome.get("latency_ms") or 0))


def _connect_email(begin: Begin, settings: Any, project_id: str, provider_key: str,
                   fields: dict[str, Any], actor: str,
                   actor_email: Optional[str]) -> dict[str, Any]:
    """Email (SMTP): one connection URL OR the separate fields (email_config.connector_values
    makes them the same values), tested by sending, stored only when the server took it. Or the
    local test inbox, when the install turned it on."""
    from . import email_config
    from .mail_transport import _dev_inbox

    if provider_key == "dev_inbox":
        dev = _dev_inbox(settings)
        if dev is None:
            raise KitFieldError("The local test inbox is off on this install. Re-run the "
                                "installer with --with-mailpit to turn it on.", None)
        values = {"host": dev.email_smtp_host, "port": dev.email_smtp_port,
                  "from_address": dev.email_from_address, "security": "none"}
        outcome = _smtp_send(settings, values, actor_email or str(dev.email_from_address))
        _fail(outcome)
        with begin() as connection:
            email_config.save_dev_inbox(connection, settings, project_id=project_id, actor=actor)
        return outcome

    try:
        values = email_config.connector_values(
            url=fields.get("url"), host=fields.get("host"), port=fields.get("port"),
            security=fields.get("security"), username=fields.get("username"),
            password=fields.get("password"), from_address=fields.get("from_address"),
            from_name=fields.get("from_name"))
    except email_config.EmailConfigError as error:
        raise KitFieldError(str(error), error.field) from error
    typed_password = values.get("password") or None
    if not typed_password:
        # NOT RETYPED: the stored key is kept — for the same login only — and the test uses it.
        with begin() as connection:
            row = email_config._row(connection, project_id) or {}
            if row.get("mode") == "smtp" and row.get("username") == values["username"]:
                values["password"] = email_config._password(connection, settings, row) or ""
    outcome = _smtp_send(settings, values, actor_email or str(values.get("from_address")))
    _fail(outcome)
    with begin() as connection:
        try:
            email_config.save(connection, settings, project_id=project_id,
                              host=values["host"], from_address=values["from_address"],
                              port=values["port"], username=values["username"],
                              password=typed_password, use_tls=values["use_tls"],
                              use_ssl=values["use_ssl"], from_name=values["from_name"],
                              actor=actor)
        except email_config.EmailConfigError as error:
            raise KitFieldError(str(error), error.field) from error
    return outcome


def _connect_tracker(begin: Begin, settings: Any, project_id: str, provider: dict[str, Any],
                     fields: dict[str, Any], actor: str) -> dict[str, Any]:
    from .work_orchestration import WorkOrchestrationService

    key = provider["key"]
    with begin() as connection:
        config: dict[str, Any] = {}
        if provider.get("reuses") == "github":
            state = github_state(connection, settings, project_id)
            if not state["connected"] or not state["repo"]:
                raise KitFieldError("Connect your GitHub repo in Setup first", None)
            if state.get("host") == "local":
                raise KitFieldError("This project's code is on this machine, not on GitHub: "
                                    "publish it to GitHub first, or pick another tracker.", None)
            owner, _, repo = state["repo"].partition("/")
            config = {"owner": owner, "repo": repo, "api_base_url": state["api_base"],
                      "credential": "github_connection"}
        else:
            config = {k: v for k, v in fields.items()
                      if k in {f["name"] for f in provider.get("fields") or []}
                      and not next((f for f in provider["fields"] if f["name"] == k
                                    and f.get("secret")), None)}
            current = kit_tracker(connection, project_id)
            if current and current["provider_key"] == key and not fields.get("api_token"):
                # Not retyped: keep the stored token.
                config["secret_ref_id"] = current["configuration"].get("secret_ref_id")
                config["last4"] = current["configuration"].get("last4")
        tested = _tracker_config_with_token(connection, settings, project_id, key, config)
    if fields.get("api_token"):
        tested["api_token"] = fields["api_token"]
    if key == "jira":
        tested.setdefault("issue_type", "Task")
    outcome = _tracker_test(settings, key, tested)
    _fail(outcome)
    with begin() as connection:
        if fields.get("api_token"):
            written = _store_secret(connection, settings, project_id,
                                    f"tracker_{key}_token", str(fields["api_token"]), actor)
            config["secret_ref_id"] = written["secret_ref_id"]
            config["last4"] = written.get("last4")
        config["kit"] = True
        config["acts_as"] = outcome.get("account") or None
        service = WorkOrchestrationService(settings)
        service.connect_tracker_provider(connection, project_id=project_id, provider_key=key,
                                         configuration=config, actor=actor,
                                         set_as_default=True, test=False)
    return outcome


def _connect_chat(begin: Begin, settings: Any, project_id: str, provider_key: str,
                  fields: dict[str, Any], actor: str) -> dict[str, Any]:
    values = dict(fields)
    secret_field = "bot_token" if provider_key == "slack" else "token"
    with begin() as connection:
        stored = chat_settings(connection, settings, project_id) or {}
        if not values.get(secret_field) and stored.get("provider") == provider_key:
            values[secret_field] = stored.get("token")
    values["token"] = values.get(secret_field)
    outcome = _chat_post(settings, provider_key, values)
    _fail(outcome)
    body = {"provider": provider_key, "channel": values.get("channel") or "",
            "acts_as": outcome.get("fact")}
    if provider_key == "mattermost":
        body["base_url"] = values.get("base_url") or ""
    with begin() as connection:
        current = _config(connection, project_id, TEAM_CHAT_KEY) or {}
        if fields.get(secret_field):
            written = _store_secret(connection, settings, project_id,
                                    f"chat_{provider_key}_token", str(fields[secret_field]),
                                    actor)
            body["secret_ref_id"] = written["secret_ref_id"]
            body["last4"] = written.get("last4")
        elif current.get("provider") == provider_key:
            body["secret_ref_id"] = current.get("secret_ref_id")
            body["last4"] = current.get("last4")
        _put_config(connection, project_id, TEAM_CHAT_KEY, body, actor)
        emit_event(connection, project_id, "kit.connected", actor, "kit_ability", "team_chat",
                   {"provider": provider_key})
    return outcome


def _connect_search(begin: Begin, settings: Any, project_id: str, provider: dict[str, Any],
                    fields: dict[str, Any], actor: str) -> dict[str, Any]:
    key = provider["key"]
    typed = str(fields.get("api_key") or "")
    with begin() as connection:
        current = _config(connection, project_id, WEB_SEARCH_KEY) or {}
        kept = (current.get("provider") == key and current.get("secret_ref_id"))
        secret = typed or (_resolve(connection, settings, current.get("secret_ref_id"), project_id)
                           if kept else "")
    outcome = _search_test(begin, settings, project_id, provider=key, key=secret,
                           field="api_key")
    _fail(outcome)
    body: dict[str, Any] = {"provider": key}
    with begin() as connection:
        if typed:
            written = _store_secret(connection, settings, project_id, f"search_{key}_key",
                                    typed, actor)
            body["secret_ref_id"] = written["secret_ref_id"]
            body["last4"] = written.get("last4")
        elif kept:
            body["secret_ref_id"] = current.get("secret_ref_id")
            body["last4"] = current.get("last4")
        _put_config(connection, project_id, WEB_SEARCH_KEY, body, actor)
        _point_web_intel(connection, settings, project_id, key, body.get("secret_ref_id"),
                         actor)
        emit_event(connection, project_id, "kit.connected", actor, "kit_ability",
                   "web_search", {"provider": key})
    return outcome


def _point_web_intel(connection: Connection, settings: Any, project_id: str, provider: str,
                     secret_ref_id: Optional[str], actor: str) -> None:
    """web-intel's search_web reads its provider and key from the request: give the registered
    server that sign-in, by reference, so every call carries it and no call's inputs do."""
    from .mcp_registry import find_mcp_server

    server = find_mcp_server(connection, project_id, "web-intel")
    if server is None:
        return
    auth = {"type": "bearer" if secret_ref_id else "none",
            "headers": {"X-Search-Provider": provider}}
    if secret_ref_id:
        auth["secret_ref_id"] = secret_ref_id
        row = connection.execute(
            text("SELECT metadata FROM secret_refs WHERE id = :i"), {"i": secret_ref_id}
        ).scalar()
        meta = row if isinstance(row, dict) else (json.loads(row) if row else {})
        auth["last4"] = (meta or {}).get("last4")
    from .mcp_registry import set_seeded_auth

    set_seeded_auth(connection, project_id, server["id"], auth, actor=actor)


def _connect_catalogue(begin: Begin, settings: Any, project_id: str, ability: dict[str, Any],
                       provider: dict[str, Any], fields: dict[str, Any], actor: str
                       ) -> dict[str, Any]:
    from .mcp_registry import test_entered

    entry = catalogue_entry(str(provider.get("catalogue")))
    if entry is None:
        raise KitFieldError("This catalogue entry is gone.", None)
    token = str(fields.get("token") or "") or None
    auth = {"type": entry.get("auth") or "none"}
    if token:
        auth["secret"] = token
    probe = test_entered(settings, None, project_id, entry["url"], auth,
                         timeout=TEST_TIMEOUT_SECONDS)
    if not probe["ok"]:
        raise KitFieldError(str(probe.get("error") or "Can't reach server"),
                            "token" if token else None,
                            latency_ms=int(probe.get("latency_ms") or 0))
    with begin() as connection:
        server = add_catalogue_server(connection, settings, project_id, entry["key"],
                                      token=token, actor=actor)
        from .mcp_registry import record_reachability

        record_reachability(connection, project_id, server["id"], reachable=True,
                            tools=probe["tools"])
    count = len(probe["tools"])
    return result(True, fact=f"{count} tool{'s' if count != 1 else ''}",
                  latency_ms=int(probe.get("latency_ms") or 0))


def disconnect(connection: Connection, settings: Any, project_id: str, ability: dict[str, Any],
               actor: str) -> None:
    """Forget the connection. Agents keep their grants; the ability shows Set up."""
    key = ability["key"]
    if key == "email":
        connection.execute(
            text("DELETE FROM project_configurations WHERE project_id = :p "
                 "AND config_key = 'email_smtp'"), {"p": project_id})
    elif key == "file_tickets":
        connection.execute(
            text("""UPDATE work_tracker_providers
                       SET is_default = false,
                           configuration = configuration - 'kit' - 'secret_ref_id'
                                           - 'credential' - 'last4' - 'acts_as',
                           updated_at = now()
                     WHERE project_id = :p AND configuration ? 'kit'"""), {"p": project_id})
    elif key == "team_chat":
        _drop_config(connection, project_id, TEAM_CHAT_KEY)
    elif key == "web_search":
        _drop_config(connection, project_id, WEB_SEARCH_KEY)
        _point_web_intel(connection, settings, project_id, "exa", None, actor)
    elif (ability.get("connection") or {}).get("providers") and any(
            p.get("catalogue") for p in ability["connection"]["providers"]):
        from .mcp_registry import delete_mcp_server

        server = _catalogue_server(connection, project_id, next(
            p["catalogue"] for p in ability["connection"]["providers"] if p.get("catalogue")))
        if server is not None:
            # Remember who held it, so reconnecting gives it back to them.
            from . import kit

            kit.remember_server_holders(connection, settings, project_id, server["name"],
                                        actor=actor)
            delete_mcp_server(connection, project_id, server["id"], actor=actor,
                              release_grants=True)
    else:
        raise KitFieldError(f"{ability['label']} is connected elsewhere.", None)
    emit_event(connection, project_id, "kit.disconnected", actor, "kit_ability", key, {})


def _usable_hosts(alias: str = "") -> str:
    """deployment_fleet.usable_hosts_sql: a project's hosts include the ones its organisation shares."""
    from .deployment_fleet import usable_hosts_sql

    return usable_hosts_sql(alias)
