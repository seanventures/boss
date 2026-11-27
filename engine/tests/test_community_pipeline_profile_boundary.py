"""Pipeline profiles behave identically regardless of boss_edition (BOSSAPP-1460)."""

from __future__ import annotations

from boss_engine.config import get_settings
from boss_engine.services.pipeline_profile_service import (
    DEFAULT_PROFILE,
    create_custom_profile,
    default_profile_name,
)


class _FakeConn:
    def __init__(self):
        self.executed = []

    def execute(self, stmt, params=None):
        self.executed.append((str(stmt), params or {}))
        return self

    def mappings(self):
        return self

    def first(self):
        return None


def test_pipeline_defaults_and_custom_creation_are_edition_agnostic() -> None:
    for edition in ("community", "enterprise_self_hosted"):
        settings = get_settings().model_copy(update={"boss_edition": edition})
        conn = _FakeConn()
        assert default_profile_name(conn, "project", settings) == (settings.pipeline_profile or DEFAULT_PROFILE)
        created = create_custom_profile(
            conn,
            project_id="project",
            name=f"custom_{edition}",
            label="Custom",
            tier_floor=0,
            tier_cap=1,
            model_routing={},
            caching=False,
            parallel_checks=False,
            actor="ceo",
            settings=settings,
        )
        assert created["name"] == f"custom_{edition}"
