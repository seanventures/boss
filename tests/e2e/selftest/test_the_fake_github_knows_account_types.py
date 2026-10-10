"""The compose fake GitHub (tests/e2e/compose/fakegh.py) holds BOSS to GitHub's real rules about
account types: a personal account's settings live under /settings, an organisation's under
/organizations/<org>/settings, and the other tree is a 404. And the App's permissions are not the
installation's: a permission added on the App waits until the installation accepts it."""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

COMPOSE = Path(__file__).resolve().parents[1] / "compose"


@pytest.fixture()
def fake(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKEGH_STATE", str(tmp_path))
    monkeypatch.syspath_prepend(str(COMPOSE))
    sys.modules.pop("fakegh", None)
    mod = importlib.import_module("fakegh")
    yield mod
    sys.modules.pop("fakegh", None)


def _create_app(fake):
    fake.APPS.data["manifests"]["c1"] = {"name": "BOSS x", "default_permissions": {"contents": "write"},
                                         "setup_url": "http://localhost:3000/v1/identity/github/app/installed"}
    status, app = fake.app_api("POST", "/app-manifests/c1/conversions", "", None)
    assert status == 201
    fake.APPS.data["installed"] = True
    return app


@pytest.mark.parametrize("kind,login,good,bad", [
    ("User", "paramg", "/settings", "/organizations/paramg/settings"),
    ("Organization", "acme", "/organizations/acme/settings", "/settings"),
])
def test_each_account_type_has_its_own_settings_tree(fake, kind, login, good, bad):
    fake.configure(login, kind, [f"{login}/boss-sandbox"])
    assert fake.app_api("GET", f"/users/{login}", "", None) == (200, {"login": login, "type": kind})
    assert fake.app_api("GET", "/users/nobody", "", None)[0] == 404
    assert fake.browser_page("POST", f"{bad}/apps/new", "state=s", b"manifest={}")[0] == 404
    assert fake.browser_page("POST", f"{good}/apps/new", "state=s", b"manifest={}")[0] == 200
    app = _create_app(fake)
    assert app["owner"] == {"login": login, "type": kind}
    assert fake.browser_page("GET", f"{good}/apps/{app['slug']}/permissions", "", b"")[0] == 200
    assert fake.browser_page("GET", f"{bad}/apps/{app['slug']}/permissions", "", b"")[0] == 404
    assert fake.browser_page("GET", f"{good}/installations/{fake.INSTALLATION_ID}", "", b"")[0] == 200
    assert fake.browser_page("GET", f"{bad}/installations/{fake.INSTALLATION_ID}", "", b"")[0] == 404


def test_a_new_app_permission_waits_until_the_installation_accepts_it(fake):
    fake.configure("paramg", "User", ["paramg/boss-sandbox"])
    app = _create_app(fake)
    fake.browser_page("POST", f"/settings/apps/{app['slug']}/permissions", "", b"name=workflows&level=write")
    assert fake.APPS.data["app"]["permissions"]["workflows"] == "write"
    assert "workflows" not in fake._installation()["permissions"]            # waiting
    page = fake.browser_page("GET", f"/settings/installations/{fake.INSTALLATION_ID}", "", b"")
    assert "Accept new permissions" in page[1]
    fake.browser_page("POST", f"/settings/installations/{fake.INSTALLATION_ID}", "", b"")
    assert fake._installation()["permissions"]["workflows"] == "write"       # accepted
