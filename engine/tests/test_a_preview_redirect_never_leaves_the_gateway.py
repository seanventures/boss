"""A previewed app's redirects stay inside /v1/previews/<id>/web, and no URL BOSS hands a person
or an agent names the preview's loopback port (review of c64594db, 2026-10-03)."""
from boss_engine.services import preview_approval
from boss_engine.services.preview_service import _gateway_location

P = "/v1/previews/prev_1/web"


def test_root_relative_and_loopback_absolute_redirects_come_back_through_the_gateway():
    assert _gateway_location("prev_1", "/login") == f"{P}/login"
    assert _gateway_location("prev_1", "http://127.0.0.1:43001/login?next=/x#f") == f"{P}/login?next=/x#f"
    assert _gateway_location("prev_1", "http://localhost:5000") == f"{P}/"
    assert _gateway_location("prev_1", "http://[::1]:5000/docs/") == f"{P}/docs/"


def test_a_redirect_to_another_host_is_the_apps_business():
    for location in ("https://accounts.google.com/o/oauth2/auth", "//cdn.example.com/x"):
        assert _gateway_location("prev_1", location) == location


def test_the_approval_gate_offers_the_gateway_path_not_the_loopback_port():
    assert preview_approval._open_url("prev_1") == f"{P}/"
    assert preview_approval._open_url(None) is None


def test_a_redirect_is_read_the_way_the_browser_reads_it():
    """The preview gateway open redirect: `\\` is `/` to a browser, tabs/newlines vanish and
    leading controls are stripped, so these spellings name the loopback port (or another scheme)
    and must be judged in their browser form, not as written."""
    assert _gateway_location("prev_1", "\\\\127.0.0.1:43001/admin") == f"{P}/admin"
    assert _gateway_location("prev_1", "/\\localhost:5000/x") == f"{P}/x"
    assert _gateway_location("prev_1", "//127.0.0.1:5000/y?z=1") == f"{P}/y?z=1"
    assert _gateway_location("prev_1", " \x01http://localhost:5000/k") == f"{P}/k"
    assert _gateway_location("prev_1", "ht\ttp://127.0.0.1:1/t") == f"{P}/t"
    assert _gateway_location("prev_1", "http:\\\\127.0.0.1:1\\u") == f"{P}/u"
    # Never a script or data URL out of a preview.
    for bad in ("javascript:alert(1)", "JaVaScRiPt:alert(1)", "data:text/html,x", "\tjavascript:x"):
        assert _gateway_location("prev_1", bad) == f"{P}/"
    # A bare relative redirect resolves against the gateway URL it came from.
    assert _gateway_location("prev_1", "login") == "login"
    # Another host stays the app's business, in the browser's form.
    assert _gateway_location("prev_1", "\\\\cdn.example.com/x") == "//cdn.example.com/x"


def test_a_redirect_to_boss_own_sign_in_on_a_local_install_is_not_glued_under_the_preview(monkeypatch):
    """Sign in with BOSS inside a preview: on a local install BOSS's issuer is loopback too; its
    /oidc/ paths at BOSS's own public origin pass unchanged. Anything else on loopback (another
    port, BOSS's other paths) is still pointed back through the gateway."""
    from boss_engine.config import get_settings

    monkeypatch.setattr(get_settings(), "public_url", "http://localhost:53760")
    to_boss = "http://localhost:53760/oidc/org_1/authorize?client_id=c&state=s"
    assert _gateway_location("prev_1", to_boss) == to_boss
    assert _gateway_location("prev_1", "http://localhost:53760/v1/identity/me") == f"{P}/v1/identity/me"
    assert _gateway_location("prev_1", "http://localhost:5000/oidc/x") == f"{P}/oidc/x"
    assert _gateway_location("prev_1", "http://127.0.0.1:53760/oidc/x") == f"{P}/oidc/x"
