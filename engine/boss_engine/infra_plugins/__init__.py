"""The official P0 infrastructure plugins (boss.plugin/v1).

Manifests: `plugins/official/<name>/boss-plugin.yaml` at the repository root (found like the
built-in app templates). Hooks: the modules here, in-process (`runtime.type: python`), behind the
same contract a container-runtime community plugin implements.
"""
from __future__ import annotations

from importlib import import_module

from ..platform.contract import PluginManifest, load_manifest
from ._runtime import official_dir

#: directory under plugins/official -> module in this package
OFFICIAL = {
    "postgres": "postgres",
    "pgvector": "pgvector",
    "sqlite": "sqlite",
    "valkey": "valkey",
    "minio-s3": "minio_s3",
    "docker-compose": "docker_compose",
    "caddy": "caddy",
    "opentelemetry": "opentelemetry",
}


def official_manifests() -> dict[str, PluginManifest]:
    """id -> manifest for every official plugin shipped in this tree."""
    out = {}
    for directory in OFFICIAL:
        m = load_manifest(official_dir() / directory)
        out[m.id] = m
    return out


def load_plugin(directory: str):
    """An instance of the official plugin in `plugins/official/<directory>`."""
    m = load_manifest(official_dir() / directory)
    module, _, cls = m.spec.runtime.entrypoint.partition(":")
    return getattr(import_module(module), cls)()


__all__ = ["OFFICIAL", "official_dir", "official_manifests", "load_plugin"]
