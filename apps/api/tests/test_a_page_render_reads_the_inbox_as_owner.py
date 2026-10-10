"""THE APPROVALS PAGE'S SERVER RENDER READS THE INBOX AS THE OWNER (first-run walk, 2026-10-10).

Approvals showed "Decision API unavailable · API returned 403" for the Owner while the browser's
own call answered 200. The console's server render reads the inbox through the writing GET's POST
twin, `POST /v1/decisions/inbox/load`, and a cookie POST must carry the console's Origin and the
session's X-Boss-CSRF. The render forwarded no Origin when the page was opened from a link, so the
API refused it `403 csrf_failed`. The console's half (it now sends the Origin for a page's read)
is apps/web/tests/a-page-opened-from-a-link-reads-its-data.test.ts; this is the API's half, with a
REAL claimed Owner session under enforce: the headers the console's server fetch sends get the
inbox, and the same call without the Origin is the 403 the page used to show.
"""
from __future__ import annotations

from apps.api.tests.identity_s1_world import (  # noqa: F401  (world is a fixture)
    HEADERS,
    ORIGIN,
    STRONG,
    csrf,
    fresh_client,
    totp_now,
    world,
)
from apps.api.tests.test_identity_claim_to_signed_in_owner import (  # noqa: F401  (a fixture)
    _setup_token,
    real_world,
)
from boss_engine.config import get_settings


def _signed_in_owner(capsys):
    token = _setup_token(capsys)
    client = fresh_client()
    res = client.post("/v1/auth/setup/claim", headers=HEADERS, json={
        "name": "First Owner", "email": "owner@example.test", "setup_token": token, "password": STRONG})
    assert res.status_code == 200, res.text
    secret = client.post("/v1/identity/me/mfa/totp", json={}, headers=csrf(client)).json()["secret"]
    done = client.post("/v1/identity/me/mfa/totp/confirm", json={"code": totp_now(secret)},
                       headers=csrf(client))
    assert done.status_code == 200, done.text
    return client


def test_the_server_render_reads_the_inbox_as_the_owner(real_world, engine, capsys, monkeypatch):
    monkeypatch.setattr(get_settings(), "authz_mode", "enforce")
    client = _signed_in_owner(capsys)
    org = client.get("/v1/orgs").json()["items"][0]
    project = org["project_id"]          # what page.tsx reads as selectedOrg.project_id
    token = client.get("/v1/identity/me").json()["csrf_token"]

    # What lib/identity/forward.ts withForwardedIdentity sends for a page's twin read.
    console = {"Origin": ORIGIN, "Sec-Fetch-Site": "same-origin", "X-Boss-CSRF": token}
    got = client.post("/v1/decisions/inbox/load", params={"project_id": project}, headers=console)
    assert got.status_code == 200, got.text
    assert got.json()["project_id"] == project and isinstance(got.json()["items"], list)

    # The render before the fix: the page was opened from a link, so no Origin was forwarded.
    bare = {"X-Boss-CSRF": token}
    refused = client.post("/v1/decisions/inbox/load", params={"project_id": project}, headers=bare)
    assert refused.status_code == 403 and refused.json() == {"error": "csrf_failed"}, refused.text
