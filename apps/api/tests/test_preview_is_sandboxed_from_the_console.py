"""A previewed app runs someone else's code on the console's origin (/v1/previews/<id>/web/...).

Every preview answer — the page, its assets, its errors, and the feedback overlay's POST — carries
a CSP sandbox WITHOUT allow-same-origin, so the browser gives the preview an opaque origin: its
script cannot ride the operator's session to /v1/identity/me and read the CSRF token.
"""
from __future__ import annotations

from apps.api.app.services.preview_service import PREVIEW_SANDBOX_CSP, preview_isolation_headers


def _assert_sandboxed(response) -> None:
    csp = response.headers.get("content-security-policy") or ""
    tokens = csp.replace(";", " ").split()
    assert "sandbox" in tokens, csp
    assert "allow-same-origin" not in tokens, csp
    assert "allow-top-navigation" not in tokens, csp
    # No credentialed CORS: the wildcard is only safe because nothing carries a credential.
    assert (response.headers.get("access-control-allow-credentials") or "").lower() != "true"


def test_the_isolation_policy_never_grants_same_origin():
    assert "allow-same-origin" not in PREVIEW_SANDBOX_CSP
    assert PREVIEW_SANDBOX_CSP.startswith("sandbox ")
    assert preview_isolation_headers()["Content-Security-Policy"] == PREVIEW_SANDBOX_CSP


def _fixture_preview(client) -> str:
    created = client.post(
        "/v1/previews/fixture",
        json={"project_id": "boss-dogfood", "actor": "ceo", "ttl_minutes": 15},
    )
    assert created.status_code == 201, created.text
    return created.json()["id"]


def test_every_preview_web_answer_is_sandboxed(client):
    preview_id = _fixture_preview(client)
    page = client.get(f"/v1/previews/{preview_id}/web/")
    assert page.status_code == 200, page.text
    _assert_sandboxed(page)
    # A refusal from the preview (the fixture has no other paths) is isolated too.
    missing = client.get(f"/v1/previews/{preview_id}/web/does-not-exist")
    assert missing.status_code == 404
    _assert_sandboxed(missing)
    # So is an unknown preview id.
    unknown = client.get("/v1/previews/prv_does_not_exist/web/")
    assert unknown.status_code in (404, 409), unknown.text
    _assert_sandboxed(unknown)


def test_the_feedback_overlay_answer_is_sandboxed(client):
    preview_id = _fixture_preview(client)
    answer = client.post(
        f"/v1/previews/{preview_id}/feedback",
        json={"message": "make the button blue", "anchor": "/"},
    )
    # Whatever the capture decides for a fixture preview, the answer is isolated.
    _assert_sandboxed(answer)
