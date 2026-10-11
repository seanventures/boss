"""The plugin registry: sources and trust, checksums and pins, enable/disable, search, versions."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from boss_engine.platform import registry as r
from boss_engine.platform.contract import Hook, PluginSource, SupportLevel, load_manifest

EXAMPLE = Path(__file__).resolve().parents[2] / "plugins" / "community" / "example"

PLUGIN_PY = '''
from boss_engine.platform.contract import InfraPlugin

class P(InfraPlugin):
    def validate(self, ctx): return ctx.result()
    def plan(self, ctx): return ctx.result()
    def provision(self, ctx): return ctx.result()
    def verify(self, ctx): return ctx.result()
    def destroy(self, ctx): return ctx.result()
'''


def _plugin(root: Path, source: str, name: str, pid: str, *, version="1.0.0", level="certified",
            caps=("database.relational",), features=("migrations",), engines=("postgres",)) -> Path:
    d = root / source / name
    d.mkdir(parents=True)
    (d / "boss-plugin.yaml").write_text(yaml.safe_dump({
        "apiVersion": "boss.plugin/v1", "kind": "Plugin",
        "metadata": {"id": pid, "name": name, "version": version, "owner": "t", "license": "MIT"},
        "spec": {"supportLevel": level, "capabilities": list(caps), "features": list(features),
                 "engines": list(engines), "runtime": {"type": "python", "entrypoint": "plugin:P"},
                 "hooks": ["validate", "plan", "provision", "verify", "destroy"]}}))
    (d / "plugin.py").write_text(PLUGIN_PY)
    return d


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.delenv(r.EXTRA_PATHS_ENV, raising=False)
    _plugin(tmp_path, "official", "postgres", "official.postgres")
    _plugin(tmp_path, "official", "valkey", "official.valkey", level="experimental", caps=("cache",),
            features=("ttl",), engines=("valkey",))
    v = _plugin(tmp_path, "verified", "acme", "verified.acme-db", version="2.1.0")
    _plugin(tmp_path, "verified", "unpinned", "verified.unpinned")
    _plugin(tmp_path, "community", "fancy", "community.fancy-db", level="certified")
    (tmp_path / r.LOCK_FILE).write_text(yaml.safe_dump({"plugins": [
        {"id": "verified.acme-db", "version": "2.1.0", "checksum": r.tree_checksum(v)}]}))
    return tmp_path


def _by_id(root):
    return {p.id: p for p in r.discover(root)}


def test_sources_trust_and_defaults(root):
    ps = _by_id(root)
    assert ps["official.postgres"].source is PluginSource.BUILTIN and ps["official.postgres"].usable
    assert ps["verified.acme-db"].usable
    assert not ps["verified.unpinned"].trusted and "not pinned" in ps["verified.unpinned"].problems[0]
    fancy = ps["community.fancy-db"]
    assert fancy.trusted and not fancy.enabled            # community: disabled until enabled
    assert fancy.support_level is SupportLevel.COMMUNITY  # claims certified, clamped by source


def test_a_verified_plugin_whose_files_changed_is_refused(root):
    (root / "verified" / "acme" / "plugin.py").write_text(PLUGIN_PY + "\n# changed\n")
    acme = _by_id(root)["verified.acme-db"]
    assert not acme.usable and "pinned checksum" in " ".join(acme.problems)


def test_the_official_namespace_is_reserved(root):
    _plugin(root, "community", "imposter", "official.imposter")
    assert not _by_id(root)["official.imposter"].trusted


def test_enabling_a_community_plugin_approves_its_checksum(root):
    ps = r.discover(root)
    fancy = next(p for p in ps if p.id == "community.fancy-db")
    r.apply_state(ps, {fancy.id: {"enabled": True, "approved_checksum": fancy.checksum}})
    assert fancy.usable
    (fancy.directory / "plugin.py").write_text(PLUGIN_PY + "\n# tampered\n")
    ps = r.discover(root)
    r.apply_state(ps, {fancy.id: {"enabled": True, "approved_checksum": fancy.checksum}})
    again = next(p for p in ps if p.id == "community.fancy-db")
    assert not again.enabled and "changed after it was enabled" in again.problems[0]


def test_disable_takes_a_builtin_out_of_use(root):
    ps = r.discover(root)
    r.apply_state(ps, {"official.postgres": {"enabled": False}})
    found = [p.id for p in r.Registry(ps).search(capability="database.relational")]
    assert "official.postgres" not in found


def test_search_by_capability_features_and_level(root):
    reg = r.Registry(r.discover(root))
    found = [p.id for p in reg.search(capability="database")]
    assert found == ["official.postgres", "verified.acme-db"]   # certified first; community disabled
    assert [p.id for p in reg.search(capability="cache", features=["ttl"])] == ["official.valkey"]
    assert reg.search(capability="cache", features=["persistence"]) == []
    assert [p.id for p in reg.search(support_level=SupportLevel.EXPERIMENTAL)] == ["official.valkey"]
    assert "community.fancy-db" in [p.id for p in reg.search(capability="database", include_disabled=True)]


@pytest.mark.parametrize("version, pin, ok", [
    ("1.2.3", None, True), ("1.2.3", "1.2.3", True), ("1.2.4", "1.2.3", False),
    ("1.9.0", "^1.2", True), ("2.0.0", "^1.2", False), ("1.1.0", "^1.2", False),
    ("1.2.9", "~1.2", True), ("1.3.0", "~1.2", False), ("1.0.0-rc1", "1.0.0", False),
])
def test_version_pins(version, pin, ok):
    assert r.version_matches(version, pin) is ok


def test_resolve_version_honours_request_and_operator_pins(root):
    _plugin(root, "official", "postgres-next", "official.postgres", version="1.3.0")
    ps = r.discover(root)
    reg = r.Registry(ps)
    assert reg.resolve_version("official.postgres").version == "1.3.0"
    assert reg.resolve_version("official.postgres", "~1.0").version == "1.0.0"
    r.apply_state(ps, {"official.postgres": {"enabled": True, "pinned_version": "1.0.0"}})
    assert reg.resolve_version("official.postgres").version == "1.0.0"
    with pytest.raises(r.RegistryError):
        reg.resolve_version("official.postgres", "^1.3")
    with pytest.raises(r.RegistryError):
        reg.resolve_version("community.fancy-db")


def test_load_plugin_of_the_example():
    reg = r.RegisteredPlugin(load_manifest(EXAMPLE), PluginSource.COMMUNITY, EXAMPLE, r.tree_checksum(EXAMPLE))
    plugin = r.load_plugin(reg)
    assert plugin.manifest.id == "community.example-cache"
    assert all(callable(getattr(plugin, h.value)) for h in Hook)
