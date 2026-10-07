"""HOTFIX (boss-1, 2026-10-07): every link the engine mints lands on a real public console page
that reads the token from the fragment key the link carries — and a locked-out sole Owner's
`boss identity recover-owner` link leads, in one go, to a signed-in Owner.

boss-1 printed `/auth/recover#token=…` (no such page): the browser was sent to /login with the
token in the URL, while /reset — which consumes that very token — reads `#t=`. Every link was an
f-string in its own module. Now there is one table (engine/boss_engine/identity/links.py), the
console's generated copy (apps/web/lib/identity/auth-links.gen.ts), and this walk; the console
half (each page reads `linkToken(kind)` and is public) is
apps/web/tests/every-minted-link-lands-on-a-real-page.test.ts.
"""
from __future__ import annotations

import re
from pathlib import Path

from sqlalchemy import text

from apps.api.tests.identity_s1_world import HEADERS, csrf, fresh_client, totp_now, world  # noqa: F401
from apps.api.tests.test_identity_claim_to_signed_in_owner import SHARED, real_world  # noqa: F401
from boss_engine.identity import cli, links

REPO = Path(__file__).resolve().parents[3]
APP = REPO / "apps" / "web" / "app"


def _route_of(page: Path) -> str:
    parts = [p for p in page.relative_to(APP).parent.parts if not (p.startswith("(") and p.endswith(")"))]
    return "/" + "/".join(parts) if parts else "/"


ROUTES = {_route_of(p) for p in APP.rglob("page.tsx")}


def test_the_consoles_copy_of_the_table_is_current():
    assert links.TS_PATH.read_text(encoding="utf-8") == links.render_ts(), (
        "apps/web/lib/identity/auth-links.gen.ts is stale: python -m boss_engine.identity.links --write")


def test_every_link_kind_and_alias_is_a_real_console_page():
    for kind, link in links.AUTH_LINKS.items():
        assert link.path in ROUTES, f"{kind}: {link.path} is not a page under apps/web/app"
        assert re.fullmatch(r"[a-z_]+", link.fragment)
        minted = links.url(kind, "https://boss.example.test/", "tok_ABC-123_x")
        assert minted == f"https://boss.example.test{link.path}#{link.fragment}=tok_ABC-123_x"
    for alias in links.LEGACY_ALIASES:
        assert alias in ROUTES, f"legacy alias {alias} has no forwarding page"


def test_no_module_spells_a_link_by_hand():
    """A link with a token in its fragment is built by links.url, nowhere else."""
    hand = re.compile(r"""[/}]\w*(/\w+)*#(t|token)=\{""")
    offenders = []
    for root in (REPO / "engine" / "boss_engine", REPO / "apps" / "api" / "app"):
        for path in root.rglob("*.py"):
            if path.name == "links.py":
                continue
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if hand.search(line):
                    offenders.append(f"{path.relative_to(REPO)}:{n}: {line.strip()}")
    assert not offenders, "links spelled by hand (use boss_engine.identity.links.url):\n" + "\n".join(offenders)


def test_recover_owner_link_resets_the_password_and_enrols_straight_into_a_signed_in_owner(real_world, engine):
    """A sole Owner who lost their factor: the host's recover-owner -> the printed link's page
    (/reset, #t=) -> a new password -> an enrolment-only session, no separate sign-in -> TOTP ->
    a fully signed-in Owner."""
    client = fresh_client()
    assert client.post("/v1/auth/setup/claim", headers=HEADERS, json={
        "name": "Sole Owner", "email": "sole@example.test", "legacy_password": SHARED,
        "password": "violet-otter-harbour-1987"}).status_code == 200
    secret = client.post("/v1/identity/me/mfa/totp", json={}, headers=csrf(client)).json()["secret"]
    assert client.post("/v1/identity/me/mfa/totp/confirm", json={"code": totp_now(secret)},
                       headers=csrf(client)).status_code == 200
    # ... and later loses the phone and the password.
    with engine.begin() as db:
        done = cli.recover_owner("sole@example.test", db=db, origin="https://boss.example.test")
    page, _, fragment = done.link.partition("#")
    assert page == "https://boss.example.test" + links.AUTH_LINKS["owner_recovery"].path
    key, _, token = fragment.partition("=")
    assert key == links.AUTH_LINKS["owner_recovery"].fragment
    assert client.get("/v1/identity/me").status_code == 401         # recovery ended every session

    browser = fresh_client()
    reset = browser.post("/v1/auth/password/reset", headers=HEADERS,
                         json={"token": token, "new_password": "amber-falcon-meadow-2040"})
    assert reset.status_code == 200, reset.text
    assert reset.json()["next"] == "enrol" and reset.json()["enrolment_grant"] is True
    # enrolment-only until a factor exists
    assert browser.get("/v1/identity/users").status_code == 403
    secret = browser.post("/v1/identity/me/mfa/totp", json={}, headers=csrf(browser)).json()["secret"]
    done_enrol = browser.post("/v1/identity/me/mfa/totp/confirm", json={"code": totp_now(secret)},
                              headers=csrf(browser))
    assert done_enrol.status_code == 200, done_enrol.text
    assert browser.get("/v1/identity/users").status_code == 200
    me = browser.get("/v1/identity/me").json()
    assert "owner" in me["access_roles"] and me["sign_in"]["totp"] is True
    # the link is single use
    again = fresh_client().post("/v1/auth/password/reset", headers=HEADERS,
                                json={"token": token, "new_password": "amber-falcon-meadow-2041"})
    assert again.status_code == 400


def test_a_reset_for_someone_who_has_a_factor_still_signs_nobody_in(real_world, engine):
    client = fresh_client()
    assert client.post("/v1/auth/setup/claim", headers=HEADERS, json={
        "name": "Has Factor", "email": "hf@example.test", "legacy_password": SHARED,
        "password": "violet-otter-harbour-1987"}).status_code == 200
    secret = client.post("/v1/identity/me/mfa/totp", json={}, headers=csrf(client)).json()["secret"]
    assert client.post("/v1/identity/me/mfa/totp/confirm", json={"code": totp_now(secret)},
                       headers=csrf(client)).status_code == 200
    from boss_engine.identity import recovery
    with engine.begin() as db:
        pid = db.execute(text("SELECT principal_id FROM identity_users WHERE email = 'hf@example.test'")).scalar()
        token = recovery.issue_reset_token(db, pid)
    browser = fresh_client()
    res = browser.post("/v1/auth/password/reset", headers=HEADERS,
                       json={"token": token, "new_password": "amber-falcon-meadow-2040"})
    assert res.status_code == 200 and res.json() == {"next": "signin"}
    assert browser.get("/v1/identity/me").status_code == 401
