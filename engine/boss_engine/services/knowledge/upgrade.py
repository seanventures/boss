"""Database image upgrade plan (BOSSAPP-622; operator decision 2026-10-22).

The database image is PostgreSQL 16 on Alpine with pgvector and AGE compiled in. Installs ran
postgres:16 on Alpine before, the same major version and the same musl libc, so an existing data
directory opens unchanged: the upgrade is an image swap under the installer's protected backup,
then the schema step creates the extensions. A change of PostgreSQL major version or of libc
(musl to glibc sorts text differently) is not an image swap and is refused here.
"""
from __future__ import annotations


def _libc(image: str) -> str:
    image = image.lower()
    if "alpine" in image or image.startswith("boss-community-db"):
        return "musl"
    return "glibc"


def _major(image: str) -> str:
    tag = image.lower().rsplit(":", 1)[-1]
    for part in tag.replace("-", ".").split("."):
        digits = part.removeprefix("pg")
        if digits.isdigit():
            return digits
    return ""


def plan_upgrade(*, source_image: str, target_image: str, populated: bool) -> dict:
    if not populated:
        return {"method": "fresh_volume", "in_place_datadir_copy": False,
                "steps": ["start", "schema_extensions"]}
    if _libc(source_image) != _libc(target_image):
        raise ValueError("a libc change (musl and glibc sort text differently) is not an image swap")
    if _major(source_image) != _major(target_image):
        raise ValueError("a PostgreSQL major version change is not an image swap")
    return {"method": "image_swap", "in_place_datadir_copy": False, "collation": "unchanged",
            "steps": ["protected_backup", "stop", "swap_image", "start", "schema_extensions"]}


def assert_no_in_place_copy(plan: dict) -> None:
    if plan.get("in_place_datadir_copy"):
        raise RuntimeError("in-place data directory copy is refused")
    if plan.get("method") == "image_swap" and "protected_backup" not in plan.get("steps", []):
        raise RuntimeError("an image swap needs the protected backup first")
