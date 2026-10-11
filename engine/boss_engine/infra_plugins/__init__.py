"""The official P0 infrastructure plugins (boss.plugin/v1).

Each lives in `plugins/official/<name>/`: `boss-plugin.yaml`, a `plugin.py` entrypoint the
registry loads, and `harness.yaml` for the contract harness. The hook code is here, in the
engine, reviewed and shipped with BOSS (`runtime.type: python`, in-process).
"""
from __future__ import annotations

from ..platform.contract import InfraPlugin, PluginManifest, PluginSource, load_manifest
from ._runtime import official_dir

#: the directories under plugins/official
OFFICIAL = ("postgres", "pgvector", "sqlite", "valkey", "minio-s3", "docker-compose", "caddy", "opentelemetry")


def official_manifests() -> dict[str, PluginManifest]:
    """id -> manifest for every official plugin shipped in this tree."""
    return {m.id: m for m in (load_manifest(official_dir() / d) for d in OFFICIAL)}


def load_plugin(directory: str) -> InfraPlugin:
    """The official plugin in `plugins/official/<directory>`, loaded the way BOSS loads it."""
    from ..platform.registry import RegisteredPlugin, load_plugin as registry_load, tree_checksum
    d = official_dir() / directory
    return registry_load(RegisteredPlugin(load_manifest(d), PluginSource.BUILTIN, d, tree_checksum(d)))


__all__ = ["OFFICIAL", "official_dir", "official_manifests", "load_plugin"]
