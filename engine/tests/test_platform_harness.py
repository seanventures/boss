"""The contract harness catches what it promises to: non-idempotent provision, leaked secrets,
a verify that never connected — and passes a plugin that keeps the contract. The example
community plugin is run for real against a Valkey container when docker is available."""
from __future__ import annotations

from pathlib import Path

import pytest

from boss_engine.platform.contract import Evidence, HookContext, InfraPlugin, ResourcePlan
from boss_engine.platform.harness import run_contract
from boss_engine.platform.sdk import docker_available

EXAMPLE = Path(__file__).resolve().parents[2] / "plugins" / "community" / "example"

MANIFEST = """
apiVersion: boss.plugin/v1
kind: Plugin
metadata: {id: community.fake-mem, name: Fake, version: 0.1.0, owner: tests, license: MIT}
spec:
  supportLevel: community
  capabilities: [cache]
  runtime: {type: python, entrypoint: "plugin:Fake"}
  permissions: {secrets: [write-scoped]}
  hooks: [validate, plan, provision, verify, destroy]
  bindings: [MEM_URL]
"""
SECRET = "mem://app:s3cret-pass-123@localhost/0"


class Fake(InfraPlugin):
    store: dict = {}

    def validate(self, ctx: HookContext):
        return ctx.result()

    def plan(self, ctx: HookContext):
        return ctx.result(plan=ResourcePlan(resourceId=ctx.resource_id, pluginId=ctx.plugin_id,
                                            pluginVersion=ctx.plugin_version, mode=ctx.resource.mode,
                                            bindings=["MEM_URL"]))

    def provision(self, ctx: HookContext):
        self.store[ctx.resource_id] = True
        ref = ctx.secrets.put("MEM_URL", SECRET)
        return ctx.result(bindings={"MEM_URL": ref}, state={"ref": ref.secretRef})

    def verify(self, ctx: HookContext):
        ok = ctx.secrets.resolve(ctx.state["ref"]) == SECRET and self.store.get(ctx.resource_id)
        return ctx.result(evidence=[Evidence(check="round trip", passed=bool(ok), kind="connection")])

    def destroy(self, ctx: HookContext):
        self.store.pop(ctx.resource_id, None)
        return ctx.result()


class NotIdempotent(Fake):
    def provision(self, ctx: HookContext):
        if self.store.get(ctx.resource_id):
            return ctx.fail("exists", "already exists")
        return super().provision(ctx)


class Leaky(Fake):
    def provision(self, ctx: HookContext):
        ctx.log(f"connecting to {SECRET}")
        return super().provision(ctx)


class NoConnection(Fake):
    def verify(self, ctx: HookContext):
        return ctx.result(evidence=["healthcheck:passed"])


@pytest.fixture
def plugin_dir(tmp_path):
    (tmp_path / "boss-plugin.yaml").write_text(MANIFEST)
    return tmp_path


def _failed(report):
    return {c.name for c in report.checks if not c.passed}


def test_a_plugin_that_keeps_the_contract_passes(plugin_dir):
    report = run_contract(plugin_dir, plugin=Fake())
    assert report.ok, report.text()


@pytest.mark.parametrize("plugin, check", [
    (NotIdempotent(), "provision is idempotent"),
    (Leaky(), "secrets are redacted"),
    (NoConnection(), "verify proves a real connection"),
])
def test_the_harness_catches_a_broken_plugin(plugin_dir, plugin, check):
    plugin.store = {}
    report = run_contract(plugin_dir, plugin=plugin)
    assert not report.ok and check in _failed(report), report.text()


def test_an_invalid_manifest_stops_the_run(tmp_path):
    (tmp_path / "boss-plugin.yaml").write_text("apiVersion: boss.plugin/v1\nkind: Plugin\n")
    report = run_contract(tmp_path)
    assert not report.ok and _failed(report) == {"manifest"}


def test_init_scaffolds_a_plugin_that_validates(tmp_path):
    from boss_engine.platform.__main__ import cmd_init, cmd_validate
    d = tmp_path / "my-thing"
    assert cmd_init(str(d)) == 0
    assert cmd_validate(str(d)) == 0


@pytest.mark.skipif(not docker_available(), reason="docker is not available")
def test_the_example_plugin_passes_against_a_real_container():
    report = run_contract(EXAMPLE)
    assert report.ok, report.text()
    assert "verify proves a real connection" in {c.name for c in report.checks if c.passed}
