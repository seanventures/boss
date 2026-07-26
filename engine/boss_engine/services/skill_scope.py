"""Shared SkillSpec reference matching for RoleSpec load and hot paths."""
from __future__ import annotations


def skill_reference_matches(reference: str, skill_id: str) -> bool:
    if reference == skill_id:
        return True
    if reference.endswith(".*") and skill_id.startswith(reference[:-1]):
        return True
    reference_parts = [part for part in reference.split(".") if part]
    skill_parts = [part for part in skill_id.split(".") if part]
    position = 0
    for part in skill_parts:
        if position < len(reference_parts) and part == reference_parts[position]:
            position += 1
    return bool(reference_parts) and position == len(reference_parts)


def matching_skill_ids(reference: str, catalog_ids: set[str]) -> set[str]:
    if reference in catalog_ids:
        return {reference}
    return {
        skill_id
        for skill_id in catalog_ids
        if skill_reference_matches(reference, skill_id)
    }
