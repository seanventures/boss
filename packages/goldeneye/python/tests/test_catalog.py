"""Unit tests for goldeneye_kit catalog assembly (no DB)."""

from __future__ import annotations

import sys
from pathlib import Path

KIT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(KIT / "python"))

from goldeneye_kit.catalog import (  # noqa: E402
    build_catalog,
    discover_modules,
    kit_version,
    load_catalog,
    load_module_manifest,
    write_catalog,
)


def test_kit_version_reads_version_file():
    assert kit_version(KIT) == (KIT / "VERSION").read_text(encoding="utf-8").strip()


def test_discover_modules_finds_layer0():
    mods = discover_modules(KIT)
    ids = {m.name for m in mods}
    assert "layer0-host" in ids
    assert "layer1-runtime" in ids
    assert "deploy-cli" in ids
    assert "minio" in ids


def test_load_module_manifest_contract():
    manifest = load_module_manifest(KIT / "modules" / "layer0-host")
    assert manifest["api_version"] == 1
    assert manifest["id"] == "layer0-host"
    assert manifest["version"]
    assert manifest["layer"] == 0
    assert "host-baseline" in (manifest.get("provides") or [])


def test_build_catalog_has_bootstrap_and_paths():
    cat = build_catalog(KIT)
    assert cat["api_version"] == 1
    assert cat["version"] == 2
    assert cat["kit_version"] == kit_version(KIT)
    ids = [p["id"] for p in cat["packages"]]
    assert "layer0-host" in ids
    assert "ovh" not in ids  # not a goldeneye module
    assert all(p.get("path", "").startswith("modules/") for p in cat["packages"])
    assert "layer0-host" in cat["default_bootstrap"]


def test_write_and_load_catalog(tmp_path):
    out = tmp_path / "catalog.yaml"
    write_catalog(KIT, out)
    assert out.is_file()
    text = out.read_text(encoding="utf-8")
    assert "GENERATED" in text
    loaded = load_catalog(KIT)
    assert loaded["package_count"] >= 8
