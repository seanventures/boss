"""A PEOPLE-ONLY ROUTE SAYS SO IN ITS DECLARATION (round 3, R3-STACK S5).

`GET /v1/identity/me/profile` was declared `authenticated:self` while its handler answers anyone
but a person's own session 403 people_only. The catalogue (and so the matrix and the e2e API
sweep, which predicts every caller's answer from it) said a service account may read it; the stack
said no. Neither was wrong about the intent — the declaration just did not carry it.

Now the declaration does (`@authenticated(..., people_only=True)` → `authenticated:self:people`),
and this test holds the declaration and the handler's gate to each other, both ways, for every
route the app serves:

  * a route whose handler gates on a person's session (`need_person_session`, or answers
    "people_only") and is declared `Authenticated` declares `people_only`;
  * a route declared `people_only` has that gate in its handler;
  * a GET/HEAD `Permit` route never refuses "people_only" in its handler: the sweep calls allowed
    GETs, and only an `Authenticated` declaration can say people-only today.
"""
from __future__ import annotations

import inspect
import re

from apps.api.app.main import app
from boss_engine.identity.declarations import Authenticated, Permit, access_of

PEOPLE_GATE = re.compile(r"need_person_session\(|[\"']people_only[\"']")


def _routes():
    for route in app.routes:
        endpoint = getattr(route, "endpoint", None)
        if endpoint is None:
            continue
        access = access_of(endpoint)
        if access is None:
            continue
        try:
            source = inspect.getsource(inspect.unwrap(endpoint))
        except (OSError, TypeError):
            continue
        methods = tuple(sorted(getattr(route, "methods", None) or ()))
        yield f"{','.join(methods)} {getattr(route, 'path', '?')}", access, bool(PEOPLE_GATE.search(source))


def test_a_people_gated_authenticated_route_declares_people_only():
    missing = [label for label, access, gated in _routes()
               if isinstance(access, Authenticated) and gated and not access.people_only]
    assert not missing, ("these handlers refuse anyone but a person's session, but their declaration "
                         f"does not say people_only=True: {missing}")


def test_a_people_only_declaration_is_enforced_by_its_handler():
    loose = [label for label, access, gated in _routes()
             if isinstance(access, Authenticated) and access.people_only and not gated]
    assert not loose, f"declared people_only, but the handler never checks for a person's session: {loose}"


def test_no_permit_get_refuses_people_only():
    hidden = [label for label, access, gated in _routes()
              if isinstance(access, Permit) and gated and re.match(r"^(GET|HEAD)(,HEAD)? ", label)]
    assert not hidden, ("a GET declared by permission refuses non-people in its handler; the catalogue "
                        f"cannot say so — declare it @authenticated(people_only=True) or drop the gate: {hidden}")


def test_the_profile_route_is_declared_people_only():
    found = {label: access for label, access, _ in _routes()}
    profile = next(a for label, a in found.items() if label.endswith(" /v1/identity/me/profile"))
    assert isinstance(profile, Authenticated) and profile.people_only
    assert profile.describe() == "authenticated:self:people"
