"""Pipeline profile gates must work without the paid edition service."""

from __future__ import annotations

import pytest

from boss_engine.config import get_settings
from boss_engine.services.pipeline_profile_service import (
    create_custom_profile,
    default_profile_name,
)


class _NoDefaultProfile:
    def execute(self, *_args, **_kwargs):
        return self

    def mappings(self):
        return self

    def first(self):
        return None


def test_community_pipeline_defaults_and_custom_gate_without_paid_service() -> None:
    settings = get_settings().model_copy(update={"boss_edition": "community"})
    assert default_profile_name(_NoDefaultProfile(), "project", settings) == "lean"

    with pytest.raises(PermissionError, match="Custom pipeline profiles require"):
        create_custom_profile(
            _NoDefaultProfile(),
            project_id="project",
            name="custom",
            label="Custom",
            tier_floor=0,
            tier_cap=1,
            model_routing={},
            caching=False,
            parallel_checks=False,
            actor="ceo",
            settings=settings,
        )
