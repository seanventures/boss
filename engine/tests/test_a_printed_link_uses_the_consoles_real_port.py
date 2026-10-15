"""A link printed on the host before any public origin is bound (the claim link of a laptop install)
uses the console's REAL port. It said http://localhost:3000 on an install whose console was on
another port, so the first thing a stranger opened went nowhere."""
from __future__ import annotations

import re
from pathlib import Path

from boss_engine.identity.authn_support import local_console_origin

ROOT = Path(__file__).resolve().parents[2]


def test_the_local_origin_follows_boss_web_port(monkeypatch):
    monkeypatch.setenv("BOSS_WEB_PORT", "53300")
    assert local_console_origin() == "http://localhost:53300"
    monkeypatch.delenv("BOSS_WEB_PORT")
    assert local_console_origin() == "http://localhost:3000"
    monkeypatch.setenv("BOSS_WEB_PORT", "x; rm")
    assert local_console_origin() == "http://localhost:3000"


def test_compose_tells_the_api_and_worker_the_consoles_port():
    compose = (ROOT / "scripts/install/community-compose.yml").read_text()
    assert compose.count("BOSS_WEB_PORT: ${BOSS_WEB_PORT:-3000}") >= 2


def test_no_engine_code_assumes_port_3000():
    hits = []
    for path in (ROOT / "engine/boss_engine").rglob("*.py"):
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("#", 1)[0]
            if re.search(r"localhost:3000", code) and "authn_support.py" not in path.name:
                hits.append(f"{path.relative_to(ROOT)}:{n}")
    assert not hits, hits


def test_an_apps_local_placeholder_is_the_templates_own_default():
    """Sign in with BOSS registers the login template's local APP_BASE_URL while an app has no
    address. That is the APP's port, read from the template, not an assumption about the console."""
    import yaml

    from boss_engine.oidc_provider import app_sign_in

    manifest = yaml.safe_load((ROOT / "templates/apps/login-oauth/boss-template.yaml").read_text())
    declared = next(e["default"] for e in manifest["env"] if e["name"] == "APP_BASE_URL")
    assert declared and app_sign_in.local_base() == declared.rstrip("/")
