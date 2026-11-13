"""THE CONNECTOR CATALOG (BOSSAPP-849): validated definitions, an idempotent versioned loader into
integration_definitions, and the console's list / pre-fill shapes."""
from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml
from sqlalchemy import text

from boss_engine.config import get_settings
from boss_engine.services import connector_catalog as cc

from ._p29b_support import drop, fresh_engine, make_project

CATALOG = Path(__file__).resolve().parents[2] / "packages" / "specpack" / "kits" / cc.FILE_NAME


def _catalog() -> dict:
    return cc.load_catalog(path=CATALOG)


# ---------------------------------------------------------------- the shipped file and schema

def test_the_shipped_catalog_is_valid_and_covers_the_obvious_set():
    data = _catalog()
    keys = {d["key"] for d in data["connectors"]}
    assert len(keys) >= 40
    for wanted in ("github", "linear", "atlassian", "notion", "sentry", "slack", "google-drive",
                   "google-calendar", "gmail", "postgres", "filesystem", "git", "playwright"):
        assert wanted in keys, wanted
    by = {d["key"]: d for d in data["connectors"]}
    assert {by[k]["transport"] for k in ("filesystem", "git", "playwright")} == {"stdio"}
    assert by["github"]["auth"]["kind"] == "oauth"
    assert by["github"]["auth"]["oauth"]["token_fallback"] == "bearer"
    assert by["postgres"]["auth"]["kind"] == "bearer"
    assert by["context7"]["auth"]["kind"] == "none"
    # Every stdio connector that takes a credential says where it goes, and never shares one
    # instance across credentials.
    for d in data["connectors"]:
        if d["transport"] == "stdio" and d["auth"]["kind"] != "none":
            assert d["stdio"]["env_from_auth"] in d["stdio"]["env_allowlist"], d["key"]
            assert d["stdio"]["reuse_key"] == "credential", d["key"]


def _good() -> dict:
    return copy.deepcopy(next(d for d in _catalog()["connectors"] if d["key"] == "github"))


@pytest.mark.parametrize("mutate, field", [
    (lambda d: d.update(category="toys"), "category"),
    (lambda d: d.update(transport="carrier-pigeon"), "transport"),
    (lambda d: d["http"].update(url="ftp://x"), "http.url"),
    (lambda d: d["auth"].update(kind="password"), "auth.kind"),
    (lambda d: d["auth"].pop("oauth"), "auth.oauth"),
    (lambda d: d["auth"]["oauth"].update(discovery_url="http://insecure"), "discovery_url"),
    (lambda d: d["risk"].update(default_tier=7), "risk.default_tier"),
    (lambda d: d["risk"]["tool_policies"].update({"x*": "maybe"}), "tool_policies"),
    (lambda d: d.pop("risk_notes"), "risk_notes"),
    (lambda d: d.update(key="Not A Slug"), "key"),
])
def test_a_bad_definition_is_refused_naming_the_field(mutate, field):
    d = _good()
    mutate(d)
    problems = cc.validate_definition(d)
    assert problems and any(field in p for p in problems), problems


def test_a_stdio_block_refuses_host_network_and_secrets_outside_the_allowlist():
    stdio = {"image": "node:22", "argv": ["npx", "x"], "network": "host",
             "env_allowlist": ["A"], "env_defaults": {"B": "1"}, "env_from_auth": "C",
             "reuse_key": "template"}
    problems = " | ".join(cc.validate_stdio(stdio))
    assert "never 'host'" in problems
    assert "outside env_allowlist" in problems
    assert "env_from_auth must be one of env_allowlist" in problems


def test_a_catalog_with_a_duplicate_key_or_wrong_schema_version_is_refused(tmp_path):
    data = copy.deepcopy(_catalog())
    data["connectors"].append(copy.deepcopy(data["connectors"][0]))
    data["schema_version"] = 99
    path = tmp_path / "c.yaml"
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(cc.CatalogError) as raised:
        cc.load_catalog(path=path)
    assert "used twice" in str(raised.value) and "schema_version" in str(raised.value)


# ----------------------------------------------------------------- the console's shapes

def test_prefill_of_an_http_oauth_connector_asks_for_its_fallback_token():
    github = next(d for d in _catalog()["connectors"] if d["key"] == "github")
    shape = cc.prefill(github, "proj_x")
    assert set(shape) == {"name", "transport", "url", "auth", "risk_tier", "data_boundary",
                          "description", "tool_tiers", "tool_policies", "oauth", "provenance",
                          "stdio_template"}
    assert shape["url"] == "https://api.githubcopilot.com/mcp/"
    assert shape["auth"] == {"type": "bearer", "header_name": None}
    assert shape["oauth"]["scopes"] == ["repo", "read:org"]
    assert shape["provenance"] == {"source": "connector_catalog", "connector_key": "github"}
    assert shape["stdio_template"] is None


def test_prefill_of_a_stdio_connector_points_at_its_template():
    pg = next(d for d in _catalog()["connectors"] if d["key"] == "postgres")
    shape = cc.prefill(pg, "proj_x")
    assert shape["transport"] == "stdio"
    assert shape["url"] == "stdio://proj_x/postgres"
    tpl = shape["stdio_template"]
    assert tpl["name"] == "postgres" and tpl["env_from_auth"] == "DATABASE_URI"
    assert tpl["argv"][0] == "uvx" and tpl["network"] == "bridge"


def test_catalog_view_searches_and_filters_by_category():
    view = cc.catalog_view(None, project_id="p", query="google", path=CATALOG)
    assert {i["key"] for i in view["items"]} == {"google-drive", "google-calendar", "gmail"}
    data = cc.catalog_view(None, project_id="p", category="data", path=CATALOG,
                           added={"postgres"}, templates={"postgres"})
    assert data["items"] and all(i["category"] == "data" for i in data["items"])
    pg = next(i for i in data["items"] if i["key"] == "postgres")
    assert pg["added"] and pg["template_ready"]
    assert sum(c["count"] for c in view["categories"]) == view["total"] >= 40


def test_tool_overrides_pick_the_most_specific_glob():
    d = {"risk": {"tool_tiers": {"*": 1, "get_*": 0, "get_secret": 3},
                  "tool_policies": {"delete_*": "off"}}}
    out = cc.tool_overrides(d, ["get_issue", "get_secret", "delete_repo", "other"])
    assert out["tiers"] == {"get_issue": 0, "get_secret": 3, "delete_repo": 1, "other": 1}
    assert out["policies"] == {"delete_repo": "off"}


# --------------------------------------------------------- the loader, against a real database

@pytest.fixture(scope="module")
def db():
    engine, admin = fresh_engine()
    make_project(engine, "proj_cat_a")
    make_project(engine, "proj_cat_b")
    yield engine
    drop(engine, admin)


def _rows(engine, project: str) -> list[dict]:
    with engine.begin() as c:
        return [dict(r) for r in c.execute(text(
            "SELECT integration_key, version, status FROM integration_definitions "
            "WHERE project_id = :p AND integration_key LIKE 'connector:%' "
            "ORDER BY integration_key, version"), {"p": project}).mappings()]


def test_the_loader_is_idempotent_and_versions_a_changed_definition(db, tmp_path):
    settings = get_settings()
    with db.begin() as c:
        first = cc.sync_project(c, settings, "proj_cat_a", path=CATALOG)
    total = len(_catalog()["connectors"])
    assert len(first["published"]) == total and not first["failed"]
    with db.begin() as c:
        again = cc.sync_project(c, settings, "proj_cat_a", path=CATALOG)
    assert again["published"] == [] and again["unchanged"] == total
    assert len(_rows(db, "proj_cat_a")) == total

    changed = copy.deepcopy(_catalog())
    linear = next(d for d in changed["connectors"] if d["key"] == "linear")
    linear["risk_notes"] = "Changed: now also archives issues."
    path = tmp_path / cc.FILE_NAME
    path.write_text(yaml.safe_dump(changed))
    with db.begin() as c:
        third = cc.sync_project(c, settings, "proj_cat_a", path=path)
    assert third["published"] == [{"key": "linear", "version": 2}]
    rows = [r for r in _rows(db, "proj_cat_a") if r["integration_key"] == "connector:linear"]
    assert [(r["version"], r["status"]) for r in rows] == [(1, "superseded"), (2, "active")]
    with db.begin() as c:
        assert cc.published_versions(c, "proj_cat_a")["linear"] == 2
    # The stored definition carries the connector, so a reader needs nothing but the row.
    with db.begin() as c:
        prov = c.execute(text(
            "SELECT provenance FROM integration_definitions WHERE project_id = 'proj_cat_a' "
            "AND integration_key = 'connector:linear' AND status = 'active'")).scalar_one()
    assert prov["definition"]["risk_notes"].startswith("Changed")


def test_sync_all_covers_every_project(db, monkeypatch):
    monkeypatch.setattr(cc, "catalog_path", lambda settings=None: CATALOG)
    result = cc.sync_all(db, get_settings())
    assert result["projects"] == 2
    assert len(_rows(db, "proj_cat_b")) == len(_catalog()["connectors"])
    assert cc.sync_all(db, get_settings())["published"] == 0
