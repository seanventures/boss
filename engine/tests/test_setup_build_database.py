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
    assert on == {"key": "build_services", "mode": "always", "enabled": True, "source": "project",
                  "configured": True, "permission": "settings:update"}
    monkeypatch.delenv(build_services.SERVER_URL_ENV)
    monkeypatch.setattr(project_settings, "get_all", lambda c, p: {})
    auto = setup_view.build_database(_Conn(), SimpleNamespace(), "boss-dogfood")
    assert auto["mode"] == "auto" and auto["source"] == "default" and auto["configured"] is False


def test_the_setting_is_a_known_knob_only_owners_and_admins_change():
    assert project_settings.KNOBS[build_services.SETTING_KEY]["choices"] == ("auto", "always", "never")
    assert set(PERMISSION_META["settings:update"].roles) == {"owner", "admin"}


# ---- AUTOMATIC: a build gets its database when the app needs one (build_services.decide) --------

def test_the_brief_or_prd_naming_postgres_gives_the_build_a_database():
    d = build_services.decide((), None, spec={"directive": "A booking app on PostgreSQL"}, title="Bookings")
    assert d["services"] == ("postgres",) and d["mode"] == "auto" and "brief" in d["why"][0]
    assert build_services.decide((), None, spec={"directive": "A static landing page"})["services"] == ()


def test_the_apps_own_code_declares_it(tmp_path):
    cases = {
        "requirements.txt": "flask\npsycopg[binary]==3.2\n",
        "package.json": '{"dependencies": {"pg": "^8"}}',
        "prisma/schema.prisma": 'datasource db {\n  provider = "postgresql"\n}',
        "docker-compose.yml": "services:\n  db:\n    image: postgres:16\n",
        "app/db.py": 'URL = "postgresql+psycopg://u@localhost/app"\n',
    }
    for rel, text in cases.items():
        root = tmp_path / rel.replace("/", "_")
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
        d = build_services.decide((), "auto", spec={}, root=root)
        assert d["services"] == ("postgres",), rel
        assert rel in d["why"][0]
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "requirements.txt").write_text("flask\n")
    assert build_services.decide((), None, root=plain)["services"] == ()


def test_a_template_that_needs_it_and_a_plan_that_records_it():
    t = build_services.decide((), None, spec={"template": {"key": "crud-app", "services": ["postgres"]}})
    assert t["services"] == ("postgres",) and "template" in t["why"][0]
    p = build_services.decide((), None, spec={}, design={"plan": "Files: app.py\n\ndatabase: postgres\n"})
    assert p["services"] == ("postgres",) and p["why"] == ["the plan says database: postgres"]
    none = build_services.decide((), None, spec={}, design={"plan": "database: none"})
    assert none["services"] == ()


def test_never_is_respected_over_every_signal_and_always_needs_no_reason(tmp_path):
    (tmp_path / "requirements.txt").write_text("psycopg2-binary\n")
    never = build_services.decide(("postgres",), "never", spec={"directive": "PostgreSQL app"}, root=tmp_path)
    assert never["services"] == () and never["mode"] == "never"
    assert build_services.decide((), "always", spec={})["services"] == ("postgres",)
    assert build_services.mode_of("postgres") == "always" and build_services.mode_of("off") == "auto"
    assert build_services.requested(("postgres",), "never") == ()
