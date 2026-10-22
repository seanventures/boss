"""Alpine to Debian upgrade plan.

musl and glibc collations differ. A populated data directory is dump/restored
under a protected backup. Copying the data directory in place is refused.
"""
from __future__ import annotations


def plan_upgrade(*, source_image: str, target_image: str, populated: bool) -> dict:
    source = source_image.lower()
    target = target_image.lower()
    alpine = "alpine" in source
    debian = "bookworm" in target or "debian" in target or target.endswith("postgres:16")
    if not alpine or not debian:
        raise ValueError("upgrade plan is for Alpine postgres to Debian postgres")
    if not populated:
        return {
            "method": "fresh_volume",
            "in_place_datadir_copy": False,
            "steps": ["stop", "remove_volume", "start_debian"],
        }
    return {
        "method": "dump_restore",
        "in_place_datadir_copy": False,
        "collation": "musl_glibc",
        "steps": [
            "protected_backup",
            "pg_dump",
            "start_debian",
            "pg_restore",
            "reindex_text",
        ],
    }


def assert_no_in_place_copy(plan: dict) -> None:
    if plan.get("in_place_datadir_copy"):
        raise RuntimeError("in-place data directory copy is refused")
    if "pg_dump" not in plan.get("steps", []) and plan.get("method") == "dump_restore":
        raise RuntimeError("a populated upgrade must dump")
