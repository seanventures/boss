"""Pack, module and shipped-cast seeding must converge without another integrity packet.

The production service and shipped YAML are used directly. This focused connection records
their selected state transitions; it does not stand in for full bootstrap or Postgres proof.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from boss_engine.config import Settings
from boss_engine.services import agent_identity, seed_exclusions, specpack
from boss_engine.services.shipped_cast import fill_missing

ROOT = Path(__file__).resolve().parents[2]
PROJECT = "reseed-regression"


class _Result:
    def __init__(self, row=None):
        self.row = row

    def mappings(self):
        return self

    def first(self):
        return copy.deepcopy(self.row)


class _SeedConnection:
    """Selected pack/module/role rows, scoped by project like the production queries."""
    def __init__(self):
        self.roles = {}
        self.modules = {}
        self.packs = {}
        self.artifacts = []
        self.events = []
        self.exclusions = set()
        self.role_updates = []

    def execute(self, statement, params=None):
        sql = " ".join(str(statement).split())
        p = dict(params or {})
        if sql.startswith("SELECT id, display_name, autonomy_level, spec, source_module_id"):
            return _Result(self.roles.get((p["project_id"], p["role_key"])))
        if sql.startswith("INSERT INTO roles"):
            self.roles[(p["project_id"], p["role_key"])] = {**p, "spec": json.loads(p["spec"])}
        elif sql.startswith("UPDATE roles SET display_name"):
            row = next(r for r in self.roles.values() if r["id"] == p["id"])
            row.update({**p, "spec": json.loads(p["spec"])})
            self.role_updates.append((row["project_id"], row["role_key"]))
        elif sql.startswith("SELECT id, module_version, config, spec, status FROM agent_module_installs"):
            return _Result(self.modules.get((p["project_id"], p["module_id"])))
        elif sql.startswith("INSERT INTO agent_module_installs"):
            self.modules[(p["project_id"], p["module_id"])] = {
                **p, "config": json.loads(p["config"]), "spec": json.loads(p["spec"])}
        elif sql.startswith("SELECT id, pack_version, spec FROM pack_installs"):
            return _Result(self.packs.get((p["project_id"], p["pack_id"])))
        elif sql.startswith("INSERT INTO pack_installs"):
            self.packs[(p["project_id"], p["pack_id"])] = {**p, "spec": json.loads(p["spec"])}
        else:
            raise AssertionError(f"Unexpected seed query: {sql}")
        return _Result()


@pytest.fixture()
def seed_service(monkeypatch):
    svc = specpack.SpecPackService(Settings(specpack_root=ROOT / "packages/specpack"))
    monkeypatch.setattr(seed_exclusions, "is_excluded",
                        lambda c, p, kind, key: (p, kind, key) in c.exclusions)
    monkeypatch.setattr(agent_identity, "quietly", lambda *a, **k: None)
    monkeypatch.setattr(specpack, "emit_event", lambda c, *a: c.events.append(a))

    def artifact(c, settings, project, job, kind, title, content, **kwargs):
        item = {"id": f"art_test_{len(c.artifacts)}", "project_id": project,
                "kind": kind, "module_id": content["module_id"], "action": content["action"]}
        c.artifacts.append(item)
        return item["id"]

    monkeypatch.setattr(specpack, "create_artifact", artifact)
    monkeypatch.setattr(svc, "_project_capabilities", lambda *a: set())
    monkeypatch.setattr(svc, "_entitlement_result", lambda *a: {
        "status": "allowed", "required_entitlements": [], "missing_entitlements": []})
    monkeypatch.setattr(svc, "_record_agent_module_install", lambda *a, **k: None)
    monkeypatch.setattr(svc, "get_agent_module_for_project",
                        lambda c, p, m: copy.deepcopy(c.modules[(p, m)]))
    monkeypatch.setattr(svc, "get_pack_for_project",
                        lambda c, p, pack: copy.deepcopy(c.packs[(p, pack)]))
    return svc


def _install_defaults(c, svc):
    # bootstrap._seed_project_defaults uses this order. The cast then fills missing fields.
    svc.install_pack(c, PROJECT, "core_v1", "bootstrap")
    svc.install_default_agent_modules(c, PROJECT, "bootstrap")
    for (project, key), row in c.roles.items():
        if project == PROJECT:
            row["spec"] = fill_missing(row["spec"], svc.load_role_spec(key),
                                       operator_edited=(project, "role_spec", key) in c.exclusions)


def test_a_fresh_seed_has_the_cast_and_an_unchanged_reseed_adds_no_packets(seed_service):
    c = _SeedConnection()
    _install_defaults(c, seed_service)
    assert len(c.artifacts) == len(seed_service.DEFAULT_AGENT_MODULE_IDS) == 17
    assert {r["kind"] for r in c.artifacts} == {"agent_module_integrity_packet"}
    pack_roles = set(seed_service.load_pack_spec("core_v1")["installs"]["roles"])
    assert {key for project, key in c.roles} == pack_roles | {"software_engineer"}
    before = copy.deepcopy((c.roles, c.modules, c.artifacts, c.events))
    c.role_updates.clear()
    _install_defaults(c, seed_service)
    assert (c.roles, c.modules, c.artifacts, c.events) == before
    assert c.role_updates == []


@pytest.mark.parametrize("team", [None, "", "   ", "Fixture chosen team"])
def test_module_teams_and_cast_agree_so_reseeding_is_inert(seed_service, monkeypatch, team):
    load = seed_service.load_agent_module_spec
    def configured(module_id):
        module = load(module_id)
        if module_id == "codex_software_engineer_v1":
            module["role"]["team"] = team
        return module
    monkeypatch.setattr(seed_service, "load_agent_module_spec", configured)
    c = _SeedConnection()
    _install_defaults(c, seed_service)
    before = copy.deepcopy((c.roles, c.artifacts))
    _install_defaults(c, seed_service)
    assert (c.roles, c.artifacts) == before
    expected = team if team and team.strip() else seed_service.load_role_spec("software_engineer")["team"]
    assert c.roles[(PROJECT, "software_engineer")]["spec"]["team"] == expected


def test_a_changed_module_records_one_new_packet_and_its_new_role(seed_service, monkeypatch):
    c = _SeedConnection()
    _install_defaults(c, seed_service)
    count = len(c.artifacts)
    load = seed_service.load_agent_module_spec
    def upgraded(module_id):
        module = load(module_id)
        if module_id == "pm_product_planner_v1":
            module["version"] = "1.1.0"
            module["role"]["mission"] = "Plan the new bounded product work."
        return module
    monkeypatch.setattr(seed_service, "load_agent_module_spec", upgraded)
    _install_defaults(c, seed_service)
    assert len(c.artifacts) == count + 1
    assert c.artifacts[-1]["module_id"] == "pm_product_planner_v1"
    assert c.roles[(PROJECT, "pm")]["source_module_version"] == "1.1.0"
    assert c.roles[(PROJECT, "pm")]["spec"]["mission"] == "Plan the new bounded product work."
    _install_defaults(c, seed_service)
    assert len(c.artifacts) == count + 1


def test_a_new_module_records_one_packet_and_keeps_its_explicit_team(seed_service, monkeypatch):
    c = _SeedConnection()
    load = seed_service.load_agent_module_spec
    def added(module_id):
        if module_id != "new_regression_module":
            return load(module_id)
        module = load("codex_software_engineer_v1")
        module["id"] = module_id
        module["role"].update(role_key="new_regression_role", team="Fixture chosen team")
        return module
    monkeypatch.setattr(seed_service, "load_agent_module_spec", added)
    first = seed_service.install_agent_module(c, PROJECT, "new_regression_module", "operator")
    second = seed_service.install_agent_module(c, PROJECT, "new_regression_module", "operator")
    assert first["install_status"] == "installed"
    assert second["install_status"] == "already_installed"
    assert len(c.artifacts) == 1
    assert c.roles[(PROJECT, "new_regression_role")]["spec"]["team"] == "Fixture chosen team"


def test_missing_and_changed_core_only_roles_still_install_and_update(seed_service, monkeypatch):
    c = _SeedConnection()
    _install_defaults(c, seed_service)
    count = len(c.artifacts)
    del c.roles[(PROJECT, "docs_writer")]
    _install_defaults(c, seed_service)
    assert c.roles[(PROJECT, "docs_writer")]["source_module_id"] is None
    load = seed_service.load_role_spec
    def upgraded(role_key):
        role = load(role_key)
        if role_key == "docs_writer":
            role["mission"] = "Explain the updated bounded release."
        return role
    monkeypatch.setattr(seed_service, "load_role_spec", upgraded)
    _install_defaults(c, seed_service)
    assert c.roles[(PROJECT, "docs_writer")]["spec"]["mission"] == "Explain the updated bounded release."
    assert len(c.artifacts) == count


def test_operator_deleted_role_and_chosen_mission_and_team_stay_as_left(seed_service):
    c = _SeedConnection()
    _install_defaults(c, seed_service)
    count = len(c.artifacts)
    del c.roles[(PROJECT, "qa")]
    c.exclusions.add((PROJECT, "role", "qa"))
    c.roles[(PROJECT, "software_engineer")]["spec"].update(
        mission="The operator's bounded remit.", team="Fixture operator team")
    c.exclusions.add((PROJECT, "role_spec", "software_engineer"))
    _install_defaults(c, seed_service)
    assert (PROJECT, "qa") not in c.roles
    assert c.roles[(PROJECT, "software_engineer")]["spec"]["mission"] == "The operator's bounded remit."
    assert c.roles[(PROJECT, "software_engineer")]["spec"]["team"] == "Fixture operator team"
    assert len(c.artifacts) == count
