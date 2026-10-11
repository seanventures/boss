"""Resolver, declarative plan, policy, the deploy approval's preview, and the out-of-process runner.

No database and no docker: plugins here are small fakes on disk, loaded exactly as real ones are.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from boss_engine.platform import planner
from boss_engine.platform import registry as reg
from boss_engine.platform.bindings import MemoryBroker, Redactor
from boss_engine.platform.contract import (
    ContractError, Hook, HookContext, OperationStatus, ResourceRequest, validate_resources,
)

PLUGIN_PY = '''
from boss_engine.platform.contract import InfraPlugin, ResourcePlan, PlannedChange, PortSpec, CostEstimate

class P(InfraPlugin):
    PUBLIC = False

    def validate(self, ctx):
        if ctx.resource.config.get("bad"):
            return ctx.result("invalid", errors=[{"code": "bad_config", "message": "config.bad is not allowed"}])
        return ctx.result()

    def plan(self, ctx):
        action = "noop" if ctx.state.get("container") else "create"
        public = ctx.resource.public or self.PUBLIC
        return ctx.result(plan=ResourcePlan(
            resourceId="ignored", pluginId=ctx.plugin_id, pluginVersion=ctx.plugin_version, mode="owned",
            changes=[PlannedChange(action=action, target="container")],
            ports=[PortSpec(name="db", containerPort=5432, public=public)],
            publicAccess=public, cost=CostEstimate(monthlyUsd=1.5, basis="local")))

    def provision(self, ctx):
        return ctx.result(bindings={"DATABASE_URL": ctx.secrets.put("DATABASE_URL", "postgresql://u:s3cretpw@h/db")})

    def verify(self, ctx): return ctx.result(evidence=["connection:passed"])
    def destroy(self, ctx): return ctx.result()
'''

LEAKY_PY = '''
from boss_engine.platform.contract import InfraPlugin

class P(InfraPlugin):
    def validate(self, ctx): return ctx.result()
    def plan(self, ctx): return ctx.result()
    def provision(self, ctx):
        out = ctx.host.run("echo hi")
        return ctx.result(state={"out": out.stdout})
    def verify(self, ctx): return ctx.result()
    def destroy(self, ctx): return ctx.result()
'''


def _plugin(root: Path, source: str, name: str, pid: str, *, caps=("database.relational",), requires=(),
            body=PLUGIN_PY, permissions=None, bindings=("DATABASE_URL",)) -> Path:
    d = root / source / name
    d.mkdir(parents=True)
    (d / "boss-plugin.yaml").write_text(yaml.safe_dump({
        "apiVersion": "boss.plugin/v1", "kind": "Plugin",
        "metadata": {"id": pid, "name": name, "version": "1.0.0", "owner": "t", "license": "MIT"},
        "spec": {"supportLevel": "certified", "capabilities": list(caps), "requires": list(requires),
                 "features": ["migrations"], "engines": ["postgres"],
                 "runtime": {"type": "python", "entrypoint": "plugin:P"},
                 "permissions": permissions or {"secrets": ["write-scoped"]},
                 "hooks": ["validate", "plan", "provision", "verify", "destroy"],
                 "bindings": list(bindings)}}))
    (d / "plugin.py").write_text(body)
    return d


@pytest.fixture
def registry(tmp_path, monkeypatch):
    monkeypatch.delenv(reg.EXTRA_PATHS_ENV, raising=False)
    _plugin(tmp_path, "official", "postgres", "official.postgres")
    _plugin(tmp_path, "official", "pgvector", "official.pgvector", caps=("vector",),
            requires=("database.relational",), bindings=("VECTOR_URL",))
    return reg.Registry(reg.discover(tmp_path))


def _requests(body: dict) -> list[ResourceRequest]:
    return validate_resources(body)


def test_resolve_picks_by_capability_and_orders_implied_dependencies(registry):
    resolved = planner.resolve(_requests({
        "vec": {"capability": "vector"},
        "db": {"capability": "database.relational", "features": ["migrations"]},
    }), registry)
    assert [r.request.name for r in resolved] == ["db", "vec"]
    assert resolved[1].depends_on == ["db"]


@pytest.mark.parametrize("body, words", [
    ({"vec": {"capability": "vector"}}, "needs a database.relational resource"),
    ({"q": {"capability": "queue"}}, "no enabled plugin provides queue"),
    ({"db": {"capability": "database.relational", "features": ["sharding"]}}, "no enabled plugin"),
    ({"db": {"capability": "database.relational", "provider": "official.nope"}}, "no plugin official.nope"),
    ({"a": {"capability": "database.relational", "dependsOn": ["b"]},
      "b": {"capability": "database.relational", "dependsOn": ["a"]}}, "dependency cycle"),
])
def test_unresolvable_requests_are_refused_in_words(registry, body, words):
    with pytest.raises(ContractError) as exc:
        planner.resolve(_requests(body), registry)
    assert words in "\n".join(exc.value.reasons)


def _plan(registry, body, **kw):
    return planner.build_plan(planner.resolve(_requests(body), registry), app_key="helpdesk",
                              environment=kw.pop("environment", "staging"), **kw)


def test_plan_document_has_everything_the_approval_shows(registry):
    doc = _plan(registry, {"db": {"capability": "database.relational", "backups": True}})
    assert doc["kind"] == planner.PLAN_KIND and doc["order"] == ["db"]
    r = doc["resources"][0]
    assert r["resourceId"] == "deploy/helpdesk/db" and r["pluginId"] == "official.postgres"
    assert doc["bindings"] == {"DATABASE_URL": "db"}
    assert doc["ports"][0]["public"] is False and doc["publicAccess"] is False
    assert doc["cost"]["monthlyUsd"] == 1.5 and doc["destructive"] == []
    assert doc["approvalsRequired"] == [] and doc["refusals"] == []
    assert doc["plugins"][0]["checksum"].startswith("sha256:")
    assert doc["digest"] == planner.plan_digest(doc) == _plan(registry, {"db": {"capability": "database.relational",
                                                                               "backups": True}})["digest"]


def test_public_needs_approval_and_production_always_does(registry):
    doc = _plan(registry, {"db": {"capability": "database.relational", "public": True}}, environment="production")
    codes = {f["code"] for f in doc["approvalsRequired"]}
    assert codes == {"public_access", "production"} and doc["publicAccess"] is True


def test_a_plugin_opening_a_port_nobody_asked_for_is_refused(registry, monkeypatch):
    p = registry.get("official.postgres")
    cls = reg._plugin_class(str(p.directory), "plugin:P", p.checksum)
    monkeypatch.setattr(cls, "PUBLIC", True)
    doc = _plan(registry, {"db": {"capability": "database.relational"}})
    assert [f["code"] for f in doc["refusals"]] == ["public_port_not_requested"]


def test_removed_owned_is_destructive_and_removed_attached_is_only_detached(registry):
    existing = [
        {"name": "old", "resource_id": "deploy/helpdesk/old", "plugin_id": "official.postgres",
         "plugin_version": "1.0.0", "mode": "owned", "state": {}},
        {"name": "legacy", "resource_id": "deploy/helpdesk/legacy", "plugin_id": "official.postgres",
         "plugin_version": "1.0.0", "mode": "attached", "state": {}},
    ]
    doc = _plan(registry, {"db": {"capability": "database.relational"}}, existing=existing)
    assert [d["target"] for d in doc["destructive"]] == ["old"]
    assert [f["code"] for f in doc["approvalsRequired"]] == ["destructive"]
    legacy = next(r for r in doc["resources"] if r["name"] == "legacy")
    assert legacy["changes"][0]["action"] == "noop" and not legacy["destructive"]


def test_validate_failure_and_binding_clash(registry):
    with pytest.raises(ContractError) as exc:
        _plan(registry, {"db": {"capability": "database.relational", "config": {"bad": True}}})
    assert "config.bad is not allowed" in str(exc.value)
    doc = _plan(registry, {"a": {"capability": "database.relational"},
                           "b": {"capability": "database.relational"}})
    assert [f["code"] for f in doc["refusals"]] == ["binding_clash"]
    doc = _plan(registry, {"a": {"capability": "database.relational"},
                           "b": {"capability": "database.relational", "bindings": {"DATABASE_URL": "B_URL"}}})
    assert doc["refusals"] == [] and doc["bindings"] == {"DATABASE_URL": "a", "B_URL": "b"}


def test_the_decision_preview_shows_the_plan_and_pins_its_hash(registry):
    from boss_engine.platform.deploy import preview, preview_inputs
    from boss_engine.services.action_preview import content_hash

    doc = _plan(registry, {"db": {"capability": "database.relational", "public": True}})
    p = preview(doc)
    labels = {f["label"]: f["value"] for f in p["fields"]}
    assert p["kind"] == "fields" and p["skill_id"] == "infra.plan"
    assert labels["Public access"] == "YES" and "DATABASE_URL" in labels["App gets"]
    assert "Needs approval" in labels and labels["Plan digest"] == doc["digest"]
    assert p["content_hash"] == content_hash(preview_inputs(doc))


def test_attached_resource_plans_an_attach_and_never_runs_plugin_hooks(registry):
    doc = _plan(registry, {"legacy": {"capability": "database.relational", "mode": "attached",
                                      "attach": {"bindings": {"LEGACY_URL": "OLD_DB_URL"}}}})
    r = doc["resources"][0]
    assert r["mode"] == "attached" and r["changes"][0]["action"] == "attach" and r["bindings"] == ["LEGACY_URL"]


def test_deploy_manifest_accepts_resources():
    from boss_engine.services.app_deploy_manifest import ManifestError, parse_manifest
    base = {"version": 1, "name": "x", "kind": "commands", "install": ["true"]}
    m = parse_manifest({**base, "resources": {"db": {"capability": "database.relational"}}})
    assert m.summary()["resources"][0]["mode"] == "owned"
    with pytest.raises(ManifestError, match="attach.bindings"):
        parse_manifest({**base, "resources": {"db": {"capability": "database.relational", "mode": "attached"}}})


def _ctx(tmp_path, hook, request, broker, **kw):
    return HookContext(hook=hook, operation_id="op", idempotency_key="k", resource=request,
                       resource_id=request.resource_id("app"), app_key="app", secrets=broker,
                       workdir=tmp_path, plugin_id="community.good", plugin_version="1.0.0", **kw)


def test_community_plugins_run_out_of_process_and_their_permissions_are_enforced(tmp_path, monkeypatch):
    from boss_engine.platform.runner import SubprocessPlugin, plugin_for
    monkeypatch.delenv(reg.EXTRA_PATHS_ENV, raising=False)
    _plugin(tmp_path, "community", "good", "community.good")
    _plugin(tmp_path, "community", "nohost", "community.nohost", body=LEAKY_PY, permissions={"secrets": []})
    plugins = {p.id: p for p in reg.discover(tmp_path)}
    good = plugin_for(plugins["community.good"])
    assert isinstance(good, SubprocessPlugin)
    req = ResourceRequest(name="db", capability="database.relational")
    broker = MemoryBroker(app_key="app", manifest=plugins["community.good"].manifest, request=req)
    out = good.run(_ctx(tmp_path, Hook.PROVISION, req, broker))
    assert out.status is OperationStatus.PROVISIONED
    assert out.bindings["DATABASE_URL"].secretRef == "app_secret://deploy/app/DATABASE_URL"
    assert "s3cretpw" not in out.model_dump_json() and broker.store  # the value went to the broker only
    denied = plugin_for(plugins["community.nohost"]).run(
        _ctx(tmp_path, Hook.PROVISION, req, MemoryBroker(app_key="app")))
    assert denied.status is OperationStatus.FAILED and denied.errors[0].code == "permission_denied"


def test_redactor_strips_values_and_url_passwords():
    r = Redactor()
    r.remember("postgresql://u:s3cretpw@h/db")
    assert "s3cretpw" not in r("connect failed for s3cretpw at postgresql://u:s3cretpw@h/db")
    assert r.leaks(json.dumps({"x": "s3cretpw"}))
