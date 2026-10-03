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
