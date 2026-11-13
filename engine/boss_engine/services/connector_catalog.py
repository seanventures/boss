"""THE CONNECTOR CATALOG (BOSSAPP-849): every MCP connector BOSS knows how to set up, declared once.

The catalog is a versioned YAML file (packages/specpack/kits/connector_catalog.yaml). Each entry
is a *connector definition*: name, category, transport (http | stdio), auth kind (none | bearer |
header | oauth, with OAuth hints for the 846 flow), default tool tiers and policies, URL
patterns, setup guidance and risk notes. :data:`CONNECTOR_SCHEMA` is that shape as JSON Schema
(published for tooling); :func:`validate_definition` is the check BOSS actually runs, with a
sentence per problem.

Three readers, one source:

* :func:`sync_project` / :func:`sync_all` — at boot, every definition is upserted into each
  project's ``integration_definitions`` as ``connector:<key>`` through
  ``IntegrationRegistryService.create_definition``: an unchanged definition is a duplicate by
  content hash (nothing written), a changed one publishes the next version and supersedes the
  previous. Idempotent, so every boot after the first writes nothing.
* :func:`catalog_view` — the console's Add MCP sheet (search + category chips).
* :func:`prefill` — what picking a connector puts into the add-server form: name, URL (or the
  ``stdio://<project>/<key>`` address of its stdio template), auth, tier, and the tool tier /
  policy globs the form applies once the probe has named the tools (:func:`tool_overrides`).
"""
from __future__ import annotations

import fnmatch
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Optional

_log = logging.getLogger("boss.connector_catalog")

SCHEMA_VERSION = 1
FILE_NAME = "connector_catalog.yaml"
INTEGRATION_PREFIX = "connector:"

CATEGORIES = ("code", "project", "knowledge", "docs", "search", "browser", "observability",
              "communication", "productivity", "data", "system", "cloud", "payments", "design",
              "crm", "automation")
TRANSPORTS = ("http", "stdio")
AUTH_KINDS = ("none", "bearer", "header", "oauth")
POLICIES = ("allow", "ask", "off")
BOUNDARIES = ("public", "internal", "confidential", "restricted")
REUSE_KEYS = ("credential", "template")

_KEY = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_ENV = re.compile(r"^[A-Z_][A-Z0-9_]{0,63}$")
_NETWORK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")

#: The definition's shape as JSON Schema (draft 2020-12). Published for editors and tooling;
#: :func:`validate_definition` enforces the same rules without a schema library.
CONNECTOR_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://boss.dev/schemas/connector-definition/v1",
    "type": "object",
    "required": ["key", "name", "category", "transport", "auth", "risk", "setup", "risk_notes"],
    "properties": {
        "key": {"type": "string", "pattern": _KEY.pattern},
        "name": {"type": "string", "minLength": 1},
        "category": {"enum": list(CATEGORIES)},
        "transport": {"enum": list(TRANSPORTS)},
        "description": {"type": "string"},
        "http": {"type": "object", "required": ["url"], "properties": {
            "url": {"type": "string", "pattern": "^https?://"},
            "url_patterns": {"type": "array", "items": {"type": "string"}}}},
        "stdio": {"type": "object", "required": ["image", "argv"], "properties": {
            "image": {"type": "string", "minLength": 1},
            "argv": {"type": "array", "minItems": 1, "items": {"type": "string"}},
            "network": {"type": "string"},
            "env_allowlist": {"type": "array", "items": {"type": "string", "pattern": _ENV.pattern}},
            "env_defaults": {"type": "object", "additionalProperties": {"type": "string"}},
            "env_from_auth": {"type": ["string", "null"]},
            "idle_ttl_seconds": {"type": "integer", "minimum": 30, "maximum": 86400},
            "reuse_key": {"enum": list(REUSE_KEYS)}}},
        "auth": {"type": "object", "required": ["kind"], "properties": {
            "kind": {"enum": list(AUTH_KINDS)},
            "header_name": {"type": "string"},
            "token_url": {"type": "string"},
            "scopes_tip": {"type": "string"},
            "oauth": {"type": "object", "properties": {
                "discovery_url": {"type": ["string", "null"]},
                "scopes": {"type": "array", "items": {"type": "string"}},
                "token_fallback": {"enum": ["bearer", "header", None]}}}}},
        "risk": {"type": "object", "required": ["default_tier"], "properties": {
            "default_tier": {"type": "integer", "minimum": 0, "maximum": 3},
            "data_boundary": {"enum": list(BOUNDARIES)},
            "tool_tiers": {"type": "object",
                           "additionalProperties": {"type": "integer", "minimum": 0, "maximum": 3}},
            "tool_policies": {"type": "object", "additionalProperties": {"enum": list(POLICIES)}}}},
        "setup": {"type": "string", "minLength": 1},
        "risk_notes": {"type": "string", "minLength": 1},
        "docs_url": {"type": "string"},
        # BOSSAPP-870: an MCP AGGREGATOR (Composio, Arcade, Zapier, Pipedream) fronts many apps
        # behind one server. `toolkit_pattern` is how its tool names carry the app ("toolkit"),
        # with one placeholder — {TOOLKIT} (upper case), {toolkit} (lower) or {Toolkit} (as
        # typed) — so a rule can target one app (toolkit_selector); `execute_tools` names the
        # meta-tools that run another tool by name (their arguments, not their name, say which).
        "aggregator": {"type": "object", "required": ["toolkit_pattern"], "properties": {
            "toolkit_pattern": {"type": "string"},
            "execute_tools": {"type": "array", "items": {"type": "string"}},
            "example_toolkits": {"type": "array", "items": {"type": "string"}}}},
        # BOSSAPP-868: default tool-policy rules offered (never applied) once the server is added.
        "suggested_rules": {"type": "array", "items": {"type": "object", "required": ["tool"]}},
    },
}

#: The placeholders an aggregator's toolkit_pattern may carry (exactly one).
TOOLKIT_PLACEHOLDERS = ("{TOOLKIT}", "{toolkit}", "{Toolkit}")


class CatalogError(ValueError):
    """The catalog file does not hold valid definitions; the message lists every problem."""


# ------------------------------------------------------------------------------------------ #
# validation
# ------------------------------------------------------------------------------------------ #

def validate_definition(d: Any) -> list[str]:
    """Every problem with one definition, as sentences naming the field. Empty = valid."""
    if not isinstance(d, dict):
        return ["a connector definition must be a mapping"]
    key = d.get("key")
    where = f"connector {key!r}" if isinstance(key, str) else "a connector"
    out: list[str] = []

    def bad(field: str, why: str) -> None:
        out.append(f"{where}: {field} {why}")

    for field in ("key", "name", "category", "transport", "auth", "risk", "setup", "risk_notes"):
        if d.get(field) in (None, "", {}):
            bad(field, "is required")
    if isinstance(key, str) and not _KEY.match(key):
        bad("key", "must be a lowercase slug (a-z, 0-9, '-')")
    if d.get("category") is not None and d.get("category") not in CATEGORIES:
        bad("category", f"must be one of {', '.join(CATEGORIES)}")
    transport = d.get("transport")
    if transport is not None and transport not in TRANSPORTS:
        bad("transport", "must be http or stdio")
    if transport == "http":
        http = d.get("http")
        if not isinstance(http, dict) or not re.match(r"^https?://\S+$", str(http.get("url") or "")):
            bad("http.url", "must be an absolute http(s) URL")
        elif not all(isinstance(p, str) and p for p in http.get("url_patterns") or []):
            bad("http.url_patterns", "must be a list of strings")
        if d.get("stdio") is not None:
            bad("stdio", "is only for transport stdio")
    if transport == "stdio":
        out.extend(f"{where}: stdio.{m}" for m in validate_stdio(d.get("stdio")))
        if d.get("http") is not None:
            bad("http", "is only for transport http")
    auth = d.get("auth")
    if isinstance(auth, dict):
        kind = auth.get("kind")
        if kind not in AUTH_KINDS:
            bad("auth.kind", f"must be one of {', '.join(AUTH_KINDS)}")
        if kind == "header" and not str(auth.get("header_name") or "").strip():
            bad("auth.header_name", "is required for header auth")
        oauth = auth.get("oauth")
        if kind == "oauth" and not isinstance(oauth, dict):
            bad("auth.oauth", "is required for oauth auth (discovery_url, scopes, token_fallback)")
        if isinstance(oauth, dict):
            if kind != "oauth":
                bad("auth.oauth", "is only for oauth auth")
            disc = oauth.get("discovery_url")
            if disc is not None and not re.match(r"^https://\S+$", str(disc)):
                bad("auth.oauth.discovery_url", "must be an https URL or null")
            if not isinstance(oauth.get("scopes", []), list):
                bad("auth.oauth.scopes", "must be a list")
            if oauth.get("token_fallback") not in ("bearer", "header", None):
                bad("auth.oauth.token_fallback", "must be bearer, header or null")
        if transport == "stdio" and kind not in ("none", None):
            stdio = d.get("stdio") if isinstance(d.get("stdio"), dict) else {}
            if not stdio.get("env_from_auth"):
                bad("stdio.env_from_auth", "names where the token goes; required when auth is set")
    elif auth is not None:
        bad("auth", "must be a mapping")
    risk = d.get("risk")
    if isinstance(risk, dict):
        tier = risk.get("default_tier")
        if isinstance(tier, bool) or not isinstance(tier, int) or not 0 <= tier <= 3:
            bad("risk.default_tier", "must be an integer 0-3")
        if risk.get("data_boundary") is not None and risk["data_boundary"] not in BOUNDARIES:
            bad("risk.data_boundary", f"must be one of {', '.join(BOUNDARIES)}")
        for glob, value in (risk.get("tool_tiers") or {}).items():
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 3:
                bad(f"risk.tool_tiers[{glob}]", "must be an integer 0-3")
        for glob, value in (risk.get("tool_policies") or {}).items():
            if value not in POLICIES:
                bad(f"risk.tool_policies[{glob}]", "must be allow, ask or off")
    elif risk is not None:
        bad("risk", "must be a mapping")
    agg = d.get("aggregator")
    if agg is not None:
        if not isinstance(agg, dict):
            bad("aggregator", "must be a mapping")
        else:
            pattern = str(agg.get("toolkit_pattern") or "")
            if sum(pattern.count(p) for p in TOOLKIT_PLACEHOLDERS) != 1 or "*" not in pattern:
                bad("aggregator.toolkit_pattern",
                    "must carry exactly one {TOOLKIT}/{toolkit}/{Toolkit} and a '*'")
            for field in ("execute_tools", "example_toolkits"):
                value = agg.get(field, [])
                if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
                    bad(f"aggregator.{field}", "must be a list of names")
    rules = d.get("suggested_rules")
    if rules is not None:
        if not isinstance(rules, list):
            bad("suggested_rules", "must be a list")
        else:
            from .tool_policy_engine import ToolPolicyError, validate as validate_rule

            for i, rule in enumerate(rules):
                if not isinstance(rule, dict) or not str(rule.get("tool") or "").strip():
                    bad(f"suggested_rules[{i}]", "must name a tool glob")
                    continue
                try:
                    validate_rule({"effect": rule.get("effect") or "rate_limit",
                                   "rate_limit": rule.get("rate_limit"),
                                   "scope": rule.get("scope") or "project"})
                except ToolPolicyError as error:
                    bad(f"suggested_rules[{i}]", f"is not a rule: {error}")
    return out


def validate_stdio(s: Any) -> list[str]:
    """Problems with a stdio launch block (shared with the template service)."""
    if not isinstance(s, dict):
        return ["is required for transport stdio"]
    out: list[str] = []
    if not str(s.get("image") or "").strip():
        out.append("image is required")
    argv = s.get("argv")
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) and a for a in argv):
        out.append("argv must be a non-empty list of strings")
    network = str(s.get("network") or "none")
    if network == "host" or not _NETWORK.match(network):
        out.append("network must be 'none', 'bridge' or a named network (never 'host')")
    allow = s.get("env_allowlist") or []
    if not isinstance(allow, list) or not all(isinstance(e, str) and _ENV.match(e) for e in allow):
        out.append("env_allowlist must be a list of UPPER_CASE names")
        allow = []
    defaults = s.get("env_defaults") or {}
    if not isinstance(defaults, dict) or not all(isinstance(v, str) for v in defaults.values()):
        out.append("env_defaults must map names to strings")
    else:
        stray = sorted(set(defaults) - set(allow))
        if stray:
            out.append(f"env_defaults names {', '.join(stray)} outside env_allowlist")
    from_auth = s.get("env_from_auth")
    if from_auth is not None and from_auth not in allow:
        out.append("env_from_auth must be one of env_allowlist")
    ttl = s.get("idle_ttl_seconds", 600)
    if isinstance(ttl, bool) or not isinstance(ttl, int) or not 30 <= ttl <= 86400:
        out.append("idle_ttl_seconds must be an integer 30-86400")
    reuse = s.get("reuse_key", "credential")
    if reuse not in REUSE_KEYS:
        out.append("reuse_key must be credential or template")
    elif reuse == "template" and from_auth:
        out.append("reuse_key template shares one instance across credentials; "
                   "use credential when env_from_auth is set")
    return out


def validate_catalog(data: Any) -> list[str]:
    if not isinstance(data, dict):
        return ["the catalog must be a mapping"]
    out: list[str] = []
    if data.get("schema_version") != SCHEMA_VERSION:
        out.append(f"schema_version must be {SCHEMA_VERSION}")
    if not str(data.get("catalog_version") or "").strip():
        out.append("catalog_version is required")
    items = data.get("connectors")
    if not isinstance(items, list) or not items:
        return out + ["connectors must be a non-empty list"]
    seen: set[str] = set()
    for item in items:
        out.extend(validate_definition(item))
        key = item.get("key") if isinstance(item, dict) else None
        if isinstance(key, str):
            if key in seen:
                out.append(f"connector {key!r}: key is used twice")
            seen.add(key)
    return out


# ------------------------------------------------------------------------------------------ #
# loading
# ------------------------------------------------------------------------------------------ #

_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


def catalog_path(settings: Any = None) -> Path:
    if settings is None:
        from ..config import get_settings

        settings = get_settings()
    return Path(settings.specpack_root) / "kits" / FILE_NAME


def load_catalog(settings: Any = None, *, path: Optional[Path] = None) -> dict[str, Any]:
    """The validated catalog, read once per file change. Raises :class:`CatalogError`."""
    import yaml

    where = Path(path) if path is not None else catalog_path(settings)
    mtime = os.path.getmtime(where)
    cached = _CACHE.get(str(where))
    if cached and cached[0] == mtime:
        return cached[1]
    data = yaml.safe_load(where.read_text(encoding="utf-8")) or {}
    problems = validate_catalog(data)
    if problems:
        raise CatalogError("connector catalog is invalid: " + "; ".join(problems))
    _CACHE[str(where)] = (mtime, data)
    return data


def definitions(settings: Any = None, *, path: Optional[Path] = None) -> list[dict[str, Any]]:
    return list(load_catalog(settings, path=path)["connectors"])


def definition(settings: Any, key: str, *, path: Optional[Path] = None) -> Optional[dict]:
    return next((d for d in definitions(settings, path=path) if d["key"] == key), None)


# ------------------------------------------------------------------------------------------ #
# integration_definitions
# ------------------------------------------------------------------------------------------ #

def integration_payload(d: dict[str, Any]) -> dict[str, Any]:
    """The `create_definition` arguments for one connector. The content hash covers the whole
    definition, so a changed definition (and only that) publishes a new version."""
    risk = d.get("risk") or {}
    tier = int(risk.get("default_tier", 1))
    effect = "read" if tier == 0 else "write"
    boundary = risk.get("data_boundary") or "internal"
    contract = {
        "input_schema": {"type": "object"},
        "output_schema": {"type": "object"},
        "permissions": ["mcp_server:read"],
        "risk_tier": tier,
        "cost": {"kind": "external", "usd_micros": 0},
        "data_boundaries": [boundary],
        "retry": {"max_attempts": 1},
        "artifact_requirements": [],
        "evidence_requirements": ["receipt"],
        "side_effect_class": effect,
        "receipt_required": True,
        "dedupe_required": True,
    }
    binding: dict[str, Any] = {"kind": "mcp", "transport": d["transport"],
                               "auth_kind": (d.get("auth") or {}).get("kind", "none")}
    if d["transport"] == "http":
        binding["url"] = d["http"]["url"]
        binding["url_patterns"] = list(d["http"].get("url_patterns") or [])
    else:
        binding["stdio_template"] = d["key"]
    return {
        "integration_key": INTEGRATION_PREFIX + d["key"],
        "name": d["name"],
        "category": d["category"],
        "skill_contracts": {f"connector.{d['key']}.mcp": contract},
        "executor_bindings": [binding],
        "provenance": {"trusted": True, "source": "connector_catalog",
                       "schema_version": SCHEMA_VERSION, "definition": d},
    }


def sync_project(connection: Any, settings: Any, project_id: str, *, actor: str = "boss",
                 path: Optional[Path] = None) -> dict[str, Any]:
    """Upsert every catalog connector into the project's integration_definitions.

    Each write is in its own savepoint, so one refused definition never loses the others."""
    from .governed_collaboration import IntegrationRegistryService

    catalog = load_catalog(settings, path=path)
    service = IntegrationRegistryService(settings)
    published, unchanged, failed = [], 0, []
    for d in catalog["connectors"]:
        try:
            with connection.begin_nested():
                row = service.create_definition(connection, project_id=project_id,
                                                actor=actor, **integration_payload(d))
        except Exception as exc:  # noqa: BLE001 — one bad definition never blocks the rest
            failed.append({"key": d["key"], "error": f"{type(exc).__name__}: {exc}"[:300]})
            continue
        if row.get("duplicate"):
            unchanged += 1
        else:
            published.append({"key": d["key"], "version": int(row["version"])})
    return {"project_id": project_id, "catalog_version": str(catalog["catalog_version"]),
            "published": published, "unchanged": unchanged, "failed": failed}


def sync_all(engine: Any, settings: Any, *, actor: str = "boss") -> dict[str, Any]:
    """The boot step: every project, one short transaction each."""
    from sqlalchemy import text

    with engine.begin() as c:
        projects = [r[0] for r in c.execute(text("SELECT id FROM projects ORDER BY id"))]
    results = []
    for project_id in projects:
        try:
            with engine.begin() as c:
                results.append(sync_project(c, settings, project_id, actor=actor))
        except Exception as exc:  # noqa: BLE001
            _log.warning("connector catalog not synced for %s: %s", project_id, exc)
    return {"projects": len(results),
            "published": sum(len(r["published"]) for r in results),
            "unchanged": sum(r["unchanged"] for r in results)}


def published_versions(connection: Any, project_id: str) -> dict[str, int]:
    """{connector key: active integration_definitions version} for one project."""
    from sqlalchemy import text

    rows = connection.execute(text(
        "SELECT integration_key, version FROM integration_definitions WHERE project_id = :p "
        "AND status = 'active' AND integration_key LIKE 'connector:%'"), {"p": project_id})
    return {str(k)[len(INTEGRATION_PREFIX):]: int(v) for k, v in rows}


# ------------------------------------------------------------------------------------------ #
# the console: list and pre-fill
# ------------------------------------------------------------------------------------------ #

def stdio_url(project_id: str, key: str) -> str:
    return f"stdio://{project_id}/{key}"


def prefill(d: dict[str, Any], project_id: str) -> dict[str, Any]:
    """What picking this connector puts into the add-server form (the console's one shape).

    `auth.type` is what the form asks for now: an OAuth connector with a token fallback asks for
    that token until the OAuth flow (BOSSAPP-846) is there; one without stays `oauth`, and the
    form says it needs that sign-in. A stdio connector also carries the template the operator
    creates first (`stdio_template`), and its URL is that template's address."""
    auth = d.get("auth") or {}
    kind = auth.get("kind", "none")
    oauth = auth.get("oauth") if kind == "oauth" else None
    form_type = kind
    if kind == "oauth":
        form_type = (oauth or {}).get("token_fallback") or "oauth"
    risk = d.get("risk") or {}
    out: dict[str, Any] = {
        "name": d["key"],
        "transport": d["transport"],
        "url": d["http"]["url"] if d["transport"] == "http" else stdio_url(project_id, d["key"]),
        "auth": {"type": form_type,
                 "header_name": auth.get("header_name") if form_type == "header" else None},
        "risk_tier": int(risk.get("default_tier", 1)),
        "data_boundary": risk.get("data_boundary") or "internal",
        "description": d.get("description") or "",
        "tool_tiers": dict(risk.get("tool_tiers") or {}),
        "tool_policies": dict(risk.get("tool_policies") or {}),
        "oauth": ({"discovery_url": oauth.get("discovery_url"),
                   "scopes": list(oauth.get("scopes") or []),
                   "token_fallback": oauth.get("token_fallback")} if oauth else None),
        "provenance": {"source": "connector_catalog", "connector_key": d["key"]},
        "stdio_template": None,
    }
    if d["transport"] == "stdio":
        s = d["stdio"]
        out["stdio_template"] = {
            "name": d["key"], "image": s["image"], "argv": list(s["argv"]),
            "network": s.get("network") or "none",
            "env_allowlist": list(s.get("env_allowlist") or []),
            "env_defaults": dict(s.get("env_defaults") or {}),
            "env_from_auth": s.get("env_from_auth"),
            "idle_ttl_seconds": int(s.get("idle_ttl_seconds", 600)),
            "reuse_key": s.get("reuse_key", "credential"),
            "connector_key": d["key"], "description": d.get("description") or "",
        }
    return out


def _searchable(d: dict[str, Any]) -> str:
    return " ".join(str(x) for x in (d["key"], d["name"], d["category"], d.get("description"),
                                     d["transport"])).lower()


def catalog_view(settings: Any, *, project_id: Optional[str] = None, query: str = "",
                 category: str = "", added: Optional[set[str]] = None,
                 templates: Optional[set[str]] = None, versions: Optional[dict[str, int]] = None,
                 path: Optional[Path] = None) -> dict[str, Any]:
    """The Add MCP sheet's Connectors tab: items (filtered by `query` words and `category`),
    the category chips with counts, and each item's pre-fill."""
    catalog = load_catalog(settings, path=path)
    words = [w for w in str(query or "").lower().split() if w]
    added = {a.lower() for a in (added or set())}
    templates = templates or set()
    versions = versions or {}
    every = catalog["connectors"]
    counts = {c: sum(1 for d in every if d["category"] == c) for c in CATEGORIES}
    items = []
    for d in every:
        if category and d["category"] != category:
            continue
        if words and not all(w in _searchable(d) for w in words):
            continue
        auth = d.get("auth") or {}
        items.append({
            "key": d["key"], "name": d["name"], "category": d["category"],
            "transport": d["transport"], "description": d.get("description") or "",
            "auth_kind": auth.get("kind", "none"),
            "token_url": auth.get("token_url"), "scopes_tip": auth.get("scopes_tip"),
            "setup": d["setup"], "risk_notes": d["risk_notes"], "docs_url": d.get("docs_url"),
            "default_tier": int((d.get("risk") or {}).get("default_tier", 1)),
            "added": d["key"].lower() in added,
            "template_ready": d["transport"] != "stdio" or d["key"] in templates,
            "definition_version": versions.get(d["key"]),
            "prefill": prefill(d, project_id or "{project_id}"),
            # BOSSAPP-870: how an aggregator names one app's tools, for a rule's selector.
            "aggregator": dict(d["aggregator"]) if isinstance(d.get("aggregator"), dict) else None,
        })
    return {"schema_version": SCHEMA_VERSION, "catalog_version": str(catalog["catalog_version"]),
            "total": len(every),
            "categories": [{"key": c, "count": counts[c]} for c in CATEGORIES if counts[c]],
            "items": items}


def _rank(pattern: str) -> tuple[int, int]:
    """More specific first: fewer wildcards, then longer."""
    return (pattern.count("*") + pattern.count("?"), -len(pattern))


def tool_overrides(d: dict[str, Any], tool_names: list[str]) -> dict[str, dict[str, Any]]:
    """{"tiers": {tool: tier}, "policies": {tool: policy}} for the tools a probe named, from
    the definition's globs (most specific glob wins)."""
    risk = d.get("risk") or {}
    out: dict[str, dict[str, Any]] = {"tiers": {}, "policies": {}}
    for source, target in (("tool_tiers", "tiers"), ("tool_policies", "policies")):
        patterns = sorted((risk.get(source) or {}).items(), key=lambda kv: _rank(kv[0]))
        for name in tool_names:
            for glob, value in patterns:
                if fnmatch.fnmatchcase(name, glob):
                    out[target][name] = value
                    break
    return out


def toolkit_glob(d: dict[str, Any], toolkit: str) -> Optional[str]:
    """The tool glob that selects one app ("toolkit") on an aggregator, e.g. Composio + gmail →
    ``GMAIL_*``; None for a connector that is not an aggregator or an empty toolkit name."""
    agg = d.get("aggregator") if isinstance(d.get("aggregator"), dict) else None
    name = re.sub(r"[^A-Za-z0-9_-]", "", str(toolkit or ""))
    if not agg or not name:
        return None
    pattern = str(agg.get("toolkit_pattern") or "")
    return (pattern.replace("{TOOLKIT}", name.upper()).replace("{toolkit}", name.lower())
            .replace("{Toolkit}", name))


def toolkit_selector(d: dict[str, Any], server_name: str, toolkit: str) -> Optional[dict[str, str]]:
    """A tool-policy selector ({server, tool}) for one app behind an aggregator server — what
    POST /v1/tool-policies takes, so "deny every Gmail tool on Composio" is one rule. A meta-tool
    that executes another tool by name (``execute_tools``) is NOT covered by it: target that one
    by name, with a condition on the argument that names the tool."""
    glob = toolkit_glob(d, toolkit)
    return {"server": str(server_name), "tool": glob} if glob else None


def schema_json() -> str:
    return json.dumps(CONNECTOR_SCHEMA, indent=2, sort_keys=True)
