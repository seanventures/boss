"""Goldeneye kit — modular bare-metal PaaS (catalog + module contract).

This package is intentionally small and dependency-light so it can ship as a
standalone OSS project. BOSS imports it as an optional integration consumer.
"""

from __future__ import annotations

from .catalog import (
    KIT_API_VERSION,
    build_catalog,
    load_catalog,
    load_module_manifest,
    discover_modules,
)

__all__ = [
    "KIT_API_VERSION",
    "build_catalog",
    "load_catalog",
    "load_module_manifest",
    "discover_modules",
]

__version__ = "0.2.0"
