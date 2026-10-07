"""A person edits their own name and avatar colour (Kaappu parity, FX-C): PATCH /v1/identity/me
changes only the caller, only display_name and avatar_seed (an allowlist: anything else is a 422),
is audited as user.updated, and GET /v1/identity/me/profile reads it back. /v1/auth/options says
the policy's password minimum, so every password field can."""
from __future__ import annotations

from apps.api.tests.identity_s1_world import (  # noqa: F401  (world is a fixture)
    HEADERS,
    fresh_client,
    make_user,
    mark_claimed,
    sign_in,
    world,
)


def _csrf(client) -> dict[str, str]:
    me = client.get("/v1/identity/me")
    assert me.status_code == 200, me.text
    return {**HEADERS, "X-Boss-CSRF": me.json()["csrf_token"]}


def _person(engine, email: str, role: str = "viewer"):
    pid = make_user(engine, email, role=role, name="Old Name")
    c = fresh_client()
    assert sign_in(c, email).status_code == 200
    return pid, c


def test_a_person_renames_themselves_and_picks_an_avatar_colour(world, engine):
    mark_claimed(engine)
    pid, c = _person(engine, "self@example.test")
    r = c.patch("/v1/identity/me", json={"display_name": "  New Name ", "avatar_seed": "5"}, headers=_csrf(c))
    assert r.status_code == 200, r.text
    assert r.json()["display_name"] == "New Name" and r.json()["avatar_seed"] == "5"
    assert c.get("/v1/identity/me").json()["display_name"] == "New Name"
    assert c.get("/v1/identity/me/profile").json()["avatar_seed"] == "5"
    # The chosen colour travels with the person: /me draws the account menu's face.
    assert c.get("/v1/identity/me").json()["avatar_seed"] == "5"
    events = world.events_of("user.updated")
    assert events and events[-1].target_id == pid and events[-1].details["fields"] == ["avatar_seed", "display_name"]


def test_only_name_and_avatar_may_change(world, engine):
    mark_claimed(engine)
    _, c = _person(engine, "sneaky@example.test")
    for body in ({"email": "other@example.test"}, {"status": "active"}, {"access_roles": ["owner"]}, {"avatar_seed": "9"}):
        r = c.patch("/v1/identity/me", json=body, headers=_csrf(c))
        assert r.status_code == 422, (body, r.text)
    blank = c.patch("/v1/identity/me", json={"display_name": "   "}, headers=_csrf(c))
    assert blank.status_code == 422 and blank.json()["error"] == "name_required"


def test_a_write_without_the_csrf_token_is_refused(world, engine):
    mark_claimed(engine)
    _, c = _person(engine, "nocsrf@example.test")
    r = c.patch("/v1/identity/me", json={"display_name": "X"}, headers=HEADERS)
    assert r.status_code == 403


def test_the_sign_in_options_say_the_password_minimum(world, engine):
    r = fresh_client().get("/v1/auth/options", headers=HEADERS)
    assert r.status_code == 200, r.text
    assert isinstance(r.json()["password_min_length"], int) and r.json()["password_min_length"] >= 8
