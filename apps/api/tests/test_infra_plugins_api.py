"""Infra › Plugins API: list with support level, enable records the approved checksum, disable,
unknown id 404, and a community plugin's claimed `certified` is clamped to `community`."""
from __future__ import annotations

import textwrap

import pytest
from sqlalchemy import text

MANIFEST = textwrap.dedent("""
    apiVersion: boss.plugin/v1
    kind: Plugin
    metadata:
      id: {id}
      name: {name}
      version: 0.1.0
      owner: test
      license: Apache-2.0
    spec:
      supportLevel: {level}
      capabilities: [{cap}]
      features: [backup]
      engines: [{engine}]
      runtime: {{type: python, entrypoint: "plugin:Plugin"}}
      hooks: [validate, plan, provision, verify, destroy]
      bindings: [DATABASE_URL]
""")


@pytest.fixture
def plugins_tree(tmp_path, monkeypatch, engine):
    root = tmp_path / "plugins"
    for source, pid, name, level, cap, eng in [
        ("official", "official.pg", "Postgres", "certified", "database.relational", "postgres"),
        ("community", "community.kv", "Example KV", "certified", "cache", "valkey"),
    ]:
        d = root / source / pid.split(".", 1)[1]
        d.mkdir(parents=True)
        (d / "boss-plugin.yaml").write_text(
            MANIFEST.format(id=pid, name=name, level=level, cap=cap, engine=eng))
    monkeypatch.setenv("BOSS_PLUGINS_DIR", str(root))
    with engine.begin() as c:
        c.execute(text("DELETE FROM infra_plugin_state WHERE plugin_id IN ('official.pg', 'community.kv')"))
    yield root
    with engine.begin() as c:
        c.execute(text("DELETE FROM infra_plugin_state WHERE plugin_id IN ('official.pg', 'community.kv')"))


def _by_id(client):
    r = client.get("/v1/infra/plugins")
    assert r.status_code == 200, r.text
    return {p["id"]: p for p in r.json()["plugins"]}


def test_list_shows_sources_support_levels_and_defaults(client, plugins_tree):
    by = _by_id(client)
    assert by["official.pg"]["source"] == "builtin"
    assert by["official.pg"]["supportLevel"] == "certified" and by["official.pg"]["enabled"] is True
    assert by["community.kv"]["source"] == "community"
    # a community plugin cannot claim certified
    assert by["community.kv"]["supportLevel"] == "community"
    assert by["community.kv"]["claimedSupportLevel"] == "certified"
    assert by["community.kv"]["enabled"] is False
    found = client.get("/v1/infra/plugins", params={"capability": "database"}).json()["plugins"]
    assert [p["id"] for p in found] == ["official.pg"]
    rows = client.get("/v1/infra/plugins/matrix").json()["rows"]
    assert {r["id"] for r in rows} == {"official.pg", "community.kv"}


def test_enable_records_the_checksum_and_disable_turns_it_off(client, plugins_tree, engine):
    r = client.post("/v1/infra/plugins/community.kv/enable", json={})
    assert r.status_code == 200, r.text
    assert r.json()["plugin"]["enabled"] is True
    with engine.begin() as c:
        row = c.execute(text("SELECT enabled, approved_checksum FROM infra_plugin_state "
                             "WHERE plugin_id = 'community.kv'")).mappings().one()
    assert row["enabled"] is True and row["approved_checksum"] == _by_id(client)["community.kv"]["checksum"]
    assert _by_id(client)["community.kv"]["usable"] is True

    # its files change: disabled again until re-approved
    (plugins_tree / "community" / "kv" / "plugin.py").write_text("# changed\n")
    changed = _by_id(client)["community.kv"]
    assert changed["enabled"] is False and changed["problems"]

    r = client.post("/v1/infra/plugins/official.pg/disable")
    assert r.status_code == 200 and r.json()["plugin"]["enabled"] is False
    assert _by_id(client)["official.pg"]["enabled"] is False


def test_unknown_plugin_is_404_and_bad_pin_422(client, plugins_tree):
    r = client.post("/v1/infra/plugins/community.nope/enable", json={})
    assert r.status_code == 404 and r.json()["error"] == "plugin_unknown"
    r = client.post("/v1/infra/plugins/official.pg/enable", json={"pinnedVersion": "latest"})
    assert r.status_code == 422 and r.json()["error"] == "bad_pin"


def _resource(engine, project_id, *, mode, rid):
    with engine.begin() as c:
        c.execute(text(
            "INSERT INTO infra_platform_resources (id, project_id, resource_id, app_key, name, environment, "
            "capability, plugin_id, plugin_version, mode, status, bindings) VALUES (:i, :p, :r, 'shop', :n, "
            "'deploy', 'database.relational', 'official.pg', '0.1.0', :m, 'verified', "
            "CAST(:b AS JSONB))"),
            {"i": f"ires_{rid.replace('/', '_')}", "p": project_id, "r": rid, "n": rid.rsplit("/", 1)[-1],
             "m": mode, "b": '{"DATABASE_URL": {"secretRef": "app_secret://deploy/shop/DATABASE_URL"}}'})


def test_resources_list_names_only_and_operations_follow_policy(client, plugins_tree, engine):
    from boss_engine.config import get_settings
    project_id = get_settings().default_project_id
    _resource(engine, project_id, mode="attached", rid="deploy/shop/legacy")
    _resource(engine, project_id, mode="owned", rid="deploy/shop/db")
    r = client.get("/v1/infra/platform/resources", params={"project_id": project_id})
    assert r.status_code == 200, r.text
    rows = {x["resource_id"]: x for x in r.json()["resources"]}
    assert rows["deploy/shop/db"]["bindings"] == ["DATABASE_URL"]
    assert "app_secret://" not in r.text  # names only, never a reference path or value

    def op(rid, hook):
        return client.post("/v1/infra/platform/resources/operations",
                           json={"project_id": project_id, "resource_id": rid, "hook": hook})

    refused = op("deploy/shop/legacy", "destroy")
    assert refused.status_code == 409 and refused.json()["error"] == "attached_destroy"
    assert op("deploy/shop/nope", "verify").status_code == 404
    assert op("deploy/shop/db", "provision").status_code == 422
    held = op("deploy/shop/db", "destroy")
    assert held.status_code == 200, held.text
    assert held.json()["status"] == "awaiting_approval" and held.json()["decision_id"]
    queued = op("deploy/shop/db", "verify")
    assert queued.status_code == 200 and queued.json()["status"] == "queued"
    with engine.begin() as c:
        rec = c.execute(text("SELECT recommendation FROM decisions WHERE id = :d"),
                        {"d": held.json()["decision_id"]}).scalar_one()
        c.execute(text("DELETE FROM infra_platform_resources WHERE project_id = :p"), {"p": project_id})
    assert rec["action"] == "infra_operation" and rec["action_preview"]["content_hash"].startswith("sha256:")
