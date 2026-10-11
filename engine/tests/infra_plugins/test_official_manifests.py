"""Every official plugin: a valid boss.plugin/v1 manifest, an importable entrypoint, images pinned
by digest, and a support level that matches the certification evidence."""
from __future__ import annotations

import importlib
import re

import pytest

from boss_engine.infra_plugins import OFFICIAL, load_plugin, official_dir, official_manifests
from boss_engine.platform.contract import (
    REQUIRED_HOOKS, Hook, HookContext, InfraPlugin, ResourceRequest, SupportLevel, load_manifest,
)


def test_every_official_plugin_has_a_valid_manifest_and_entrypoint():
    ms = official_manifests()
    assert len(ms) == len(OFFICIAL) == 8
    for directory in OFFICIAL:
        m = load_manifest(official_dir() / directory)
        assert m.id == f"official.{directory}"
        assert REQUIRED_HOOKS <= set(m.spec.hooks)
        assert m.spec.supportLevel in (SupportLevel.CERTIFIED, SupportLevel.EXPERIMENTAL)
        plugin = load_plugin(directory)
        assert isinstance(plugin, InfraPlugin) and plugin.manifest.id == m.id
        for hook in m.spec.hooks:
            assert hasattr(plugin, f"do_{hook.value}"), f"{m.id} declares {hook.value} but does not implement it"


@pytest.mark.parametrize("module", sorted(set(OFFICIAL.values())))
def test_every_image_is_pinned_by_digest(module):
    mod = importlib.import_module(f"boss_engine.infra_plugins.{module}")
    for name, value in vars(mod).items():
        if name.endswith("IMAGE") or name in ("CURL",):
            assert re.fullmatch(r"[a-z0-9][a-z0-9._/-]*(:[A-Za-z0-9_.-]{1,128})?@sha256:[0-9a-f]{64}", value), f"{module}.{name} is not pinned"


def test_no_data_service_fragment_publishes_a_port():
    for directory in ("postgres", "valkey", "minio-s3", "opentelemetry"):
        plugin = load_plugin(directory)
        cap = plugin.manifest.spec.capabilities[0]
        ctx = HookContext(hook=Hook.PLAN, operation_id="o", idempotency_key="k",
                          resource=ResourceRequest(name="x", capability=cap), resource_id="deploy/a/x", app_key="a",
                          secrets=None, workdir=official_dir())  # type: ignore[arg-type]
        for svc in plugin.fragment(ctx)["services"].values():
            assert "ports" not in svc, f"{directory} publishes a port"
