"""Setup shows "Give builds a database (Postgres)": the project setting build_services, which had
no console control. The view says whether it is on, where that came from, and whether this install
has a build-services server at all; the write is PUT /v1/settings/build_services (settings:update)."""
from __future__ import annotations

import contextlib
from types import SimpleNamespace

from boss_engine.identity.permissions_gen import PERMISSION_META
from boss_engine.services import build_services, project_settings, setup_view


class _Conn:
    def begin_nested(self):
        return contextlib.nullcontext()


def test_the_view_reports_the_setting_its_source_and_the_server(monkeypatch):
    monkeypatch.setenv(build_services.SERVER_URL_ENV, "postgresql://postgres@build-services:5433/postgres")
    monkeypatch.setattr(project_settings, "get_all", lambda c, p: {"build_services": "postgres"})
    on = setup_view.build_database(_Conn(), SimpleNamespace(), "boss-dogfood")
    assert on == {"key": "build_services", "enabled": True, "source": "project", "configured": True,
                  "permission": "settings:update"}
    monkeypatch.delenv(build_services.SERVER_URL_ENV)
    monkeypatch.setattr(project_settings, "get_all", lambda c, p: {})
    off = setup_view.build_database(_Conn(), SimpleNamespace(), "boss-dogfood")
    assert off["enabled"] is False and off["source"] == "default" and off["configured"] is False


def test_the_setting_is_a_known_knob_only_owners_and_admins_change():
    assert project_settings.KNOBS[build_services.SETTING_KEY]["choices"] == ("off", "postgres")
    assert set(PERMISSION_META["settings:update"].roles) == {"owner", "admin"}
