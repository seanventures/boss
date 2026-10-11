"""boss.plugin/v1 is stable: the field sets are pinned and the committed JSON Schemas match the models.

A failure here after a change to platform/contract.py means the contract moved under every
provider plugin. Additive optional fields: regenerate (`python -m boss_engine.platform schemas`)
and add the field to the pinned set. A rename or removal is boss.plugin/v2, not an edit.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from boss_engine.platform import contract as c

CONTRACTS = Path(c.__file__).resolve().parent.parent / "contracts"

MANIFEST = """
apiVersion: boss.plugin/v1
kind: Plugin
metadata:
  id: official.postgres
  name: PostgreSQL
  version: 0.1.0
  owner: BOSS maintainers
  license: Apache-2.0
spec:
  supportLevel: certified
  capabilities: [database.relational, database.migrations]
  features: [migrations, backup]
  engines: [postgres]
  runtime: {type: python, entrypoint: "boss_plugins.postgres:Plugin"}
  permissions: {secrets: [write-scoped], docker: true, network: [declared-services]}
  hooks: [validate, plan, provision, configure, verify, observe, backup, restore, destroy]
  bindings: [DATABASE_URL]
  ports: [5432]
"""


def test_committed_schemas_match_the_models():
    for filename, model in c.SCHEMAS.items():
        committed = json.loads((CONTRACTS / filename).read_text())
        assert committed == json.loads(json.dumps(c.json_schema(model), sort_keys=True)), (
            f"{filename} is stale: run python -m boss_engine.platform schemas")


def test_pinned_field_sets():
    assert set(c.PluginManifest.model_fields) == {"apiVersion", "kind", "metadata", "spec"}
    assert set(c.OperationResult.model_fields) >= {"operationId", "resourceId", "status", "bindings",
                                                   "evidence", "artifacts"}
    assert {"mode", "capability", "provider", "public", "attach"} <= set(c.ResourceRequest.model_fields)
    assert [h.value for h in c.HOOKS] == ["validate", "plan", "provision", "configure", "verify",
                                          "observe", "backup", "restore", "destroy"]
    assert {s.value for s in c.SupportLevel} == {"certified", "experimental", "community"}
    assert {m.value for m in c.ResourceMode} == {"owned", "attached"}


def test_a_valid_manifest_loads_and_has_a_stable_digest():
    m = c.validate_manifest(MANIFEST)
    assert m.id == "official.postgres" and m.spec.supportLevel is c.SupportLevel.CERTIFIED
    assert m.digest() == c.validate_manifest(MANIFEST).digest()
    assert m.digest().startswith("sha256:")


@pytest.mark.parametrize("patch, words", [
    ({"apiVersion": "boss.plugin/v2"}, "apiVersion"),
    ({"metadata": {"id": "Postgres"}}, "metadata.id"),
    ({"spec": {"hooks": ["validate", "plan"]}}, "every plugin implements"),
    ({"spec": {"runtime": {"type": "container", "image": "postgres:17"}}}, "runtime"),
    ({"spec": {"bindings": ["database_url"]}}, "UPPER_CASE"),
    ({"spec": {"surprise": True}}, "unknown field"),
])
def test_an_invalid_manifest_names_every_problem(patch, words):
    import yaml
    data = yaml.safe_load(MANIFEST)
    for k, v in patch.items():
        if isinstance(v, dict):
            data[k] = {**data[k], **v}
        else:
            data[k] = v
    with pytest.raises(c.ContractError) as exc:
        c.validate_manifest(data)
    assert words in "\n".join(exc.value.reasons)


def test_resources_parse_with_modes_and_dependencies():
    rs = c.validate_resources({
        "db": {"capability": "database.relational", "engine": "postgres", "backups": True},
        "cache": {"capability": "cache", "dependsOn": ["db"], "publicAccess": False},
        "legacy": {"capability": "database.relational", "mode": "attached",
                   "attach": {"bindings": {"LEGACY_DATABASE_URL": "LEGACY_DB_URL"}}},
    })
    by = {r.name: r for r in rs}
    assert by["db"].mode is c.ResourceMode.OWNED and by["db"].public is False
    assert by["legacy"].mode is c.ResourceMode.ATTACHED
    assert by["db"].resource_id("helpdesk") == "deploy/helpdesk/db"


@pytest.mark.parametrize("body, words", [
    ({"db": {"capability": "database.relational", "mode": "attached"}}, "attach.bindings"),
    ({"db": {"capability": "cache", "dependsOn": ["nope"]}}, "no resource named"),
    ({"db": {"capability": "cache", "dependsOn": ["db"]}}, "itself"),
    ({"Bad": {"capability": "cache"}}, "Bad.name"),
])
def test_bad_resources_are_refused(body, words):
    with pytest.raises(c.ContractError) as exc:
        c.validate_resources(body)
    assert words in "\n".join(exc.value.reasons)


def test_results_carry_refs_not_values_and_short_evidence():
    r = c.OperationResult(operationId="op-1", resourceId="deploy/app/db", hook="verify", status="verified",
                          bindings={"DATABASE_URL": {"secretRef": "app_secret://deploy/app/DATABASE_URL"}},
                          evidence=["connection:passed", {"check": "select 1", "passed": True, "kind": "connection"}])
    assert r.ok and r.connected
    with pytest.raises(Exception):
        c.OperationResult(operationId="op", resourceId="r", hook="verify", status="verified",
                          bindings={"DATABASE_URL": "postgresql://u:p@h/db"})
    assert not c.OperationResult(operationId="o", resourceId="r", hook="verify", status="verified",
                                 evidence=["healthcheck:passed"]).connected


def test_plugin_base_dispatch_turns_exceptions_into_failed_results(tmp_path):
    class Broken(c.InfraPlugin):
        def verify(self, ctx):
            raise RuntimeError("password=hunter2")

    req = c.ResourceRequest(name="db", capability="cache")
    ctx = c.HookContext(hook=c.Hook.VERIFY, operation_id="op", idempotency_key="k", resource=req,
                        resource_id="deploy/a/db", app_key="a", secrets=None, workdir=tmp_path)  # type: ignore[arg-type]
    out = Broken().run(ctx)
    assert out.status is c.OperationStatus.FAILED and "hunter2" not in out.model_dump_json()
    ctx.hook = c.Hook.BACKUP
    assert Broken().run(ctx).status is c.OperationStatus.UNSUPPORTED


def test_idempotency_key_is_stable_and_plan_bound():
    a = c.idempotency_key("official.postgres", "0.1.0", "deploy/a/db", "provision", "sha256:x")
    assert a == c.idempotency_key("official.postgres", "0.1.0", "deploy/a/db", c.Hook.PROVISION, "sha256:x")
    assert a != c.idempotency_key("official.postgres", "0.1.0", "deploy/a/db", "provision", "sha256:y")


def test_support_levels_share_one_vocabulary_with_the_stack_registry():
    from boss_engine import support_levels
    from boss_engine.services import stacks

    assert tuple(s.value for s in c.SupportLevel) == support_levels.PLUGIN_LEVELS
    assert stacks.SUPPORT_LEVELS == support_levels.STACK_LEVELS
    for words in (support_levels.PLUGIN_LEVELS, support_levels.STACK_LEVELS):
        assert set(words) <= set(support_levels.LEVELS)
        assert sorted(words, key=support_levels.rank) == list(words)
