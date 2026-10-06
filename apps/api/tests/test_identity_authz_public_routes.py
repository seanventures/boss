"""Every public route is listed, with its reason and the mechanism that protects it (§6.3, R1).

tests/identity/public_routes.yaml is the reviewed list. A route that becomes public without being
added there, or whose reason or verifier changes, fails here; so does a listed route that is gone.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from apps.api.app import identity_guard
from apps.api.app.main import app
from boss_engine.identity.declarations import NO_VERIFIER_NEEDED, Public

LISTED = Path(__file__).resolve().parents[3] / "tests" / "identity" / "public_routes.yaml"


def _verifier_name(fn) -> str | None:
    if fn is None:
        return None
    return f"{fn.__module__}:{fn.__qualname__}"


def live() -> dict[str, dict]:
    out = {}
    for entry in identity_guard.route_inventory(app):
        if isinstance(entry.access, Public):
            for label in entry.labels():
                out[label] = {"reason": entry.access.reason.value,
                              "verifier": _verifier_name(entry.access.verifier)}
    return dict(sorted(out.items()))


def test_the_public_routes_are_exactly_the_reviewed_list():
    listed = yaml.safe_load(LISTED.read_text(encoding="utf-8"))
    assert live() == listed


def test_every_public_route_names_a_verifier_unless_there_is_nothing_to_verify():
    for label, decl in live().items():
        if decl["verifier"] is None:
            assert decl["reason"] in {r.value for r in NO_VERIFIER_NEEDED}, label


if __name__ == "__main__":
    header = ("# EVERY PUBLIC ROUTE (IDENTITY.md §6.3, §15.4 R1): reason + the function that protects it.\n"
              "# Reviewed by hand; test_identity_authz_public_routes.py holds it equal to the app.\n")
    LISTED.write_text(header + yaml.safe_dump(live(), sort_keys=True, width=120), encoding="utf-8")
