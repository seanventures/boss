"""The permission catalogue is one file, and everything derived from it is current.

packages/identity/catalogue.yaml is the only place a permission or a built-in role's membership is
defined (IDENTITY.md §6.2, D4). The Python enum, the TypeScript union, the DB seed and the published
table are generated from it and committed, so a catalogue edit shows its whole effect in review.
This file fails when any of them is stale, and pins the reviewed shape of the built-in roles.
"""
from __future__ import annotations

import copy

import pytest
import yaml

from boss_engine.identity import codegen
from boss_engine.identity.catalogue import NEVER_DELEGABLE, CatalogueError, load, parse, source_path
from boss_engine.identity.permissions_gen import (
    PASSPORT_PRESETS,
    PERMISSION_META,
    ROLE_PERMISSIONS,
    Permission,
)


def test_every_generated_file_is_current():
    stale = codegen.stale()
    assert not stale, (
        f"regenerate with `PYTHONPATH=engine python -m boss_engine.identity.codegen`: {stale}")


def test_generation_is_deterministic():
    assert codegen.render_all() == codegen.render_all()


def test_the_built_in_roles_are_the_reviewed_sizes():
    # Changing one of these numbers is a reviewed decision (IDENTITY.md §6.9), not a side effect.
    # +12 for the app platform (APP_TEMPLATES_AND_SECURITY_LOOP.md §8.2): template:*, app_secret:*,
    # security_scan:run, security_finding:triage, oidc:admin, oidc_client:manage,
    # oidc_pool_user:manage, oidc_app:sign_in. +1 app_link:manage (S5: operators link apps, §5.1).
    # +1 oidc:read (Owner/Admin/Auditor, no step-up: the App sign-in console's reads; integration).
    assert len(Permission) == 195
    sizes = {role: len(perms) for role, perms in ROLE_PERMISSIONS.items()}
    assert sizes == {"owner": 190, "admin": 181, "operator": 93, "viewer": 48, "auditor": 62,
                     "system_worker": 3, "system_collector": 1, "system_scim": 1}


def test_operator_includes_publish_and_non_production_deploys():
    # Operator decision 3 (2026-10-06). Production keeps its own permission and approval gate.
    operator = ROLE_PERMISSIONS["operator"]
    assert {"workflow:publish", "deployment:create"} <= operator
    assert "deployment:production" not in operator


def test_what_agents_can_never_do_is_not_delegable():
    for key in NEVER_DELEGABLE:
        assert not PERMISSION_META[key].delegable, key
    for preset in PASSPORT_PRESETS.values():
        for key in preset.permissions:
            assert PERMISSION_META[key].delegable, (preset.key, key)


def test_owner_holds_everything_a_human_can_and_no_human_holds_system_permissions():
    human_held = {k for k, m in PERMISSION_META.items() if not m.system_only}
    assert ROLE_PERMISSIONS["owner"] == human_held
    for role in ("owner", "admin", "operator", "viewer", "auditor"):
        assert not {k for k in ROLE_PERMISSIONS[role] if PERMISSION_META[k].system_only}


def test_critical_permissions_need_step_up_and_never_reach_viewer_or_auditor():
    for key, m in PERMISSION_META.items():
        if m.critical:
            assert m.step_up and not m.delegable, key
            assert key not in ROLE_PERMISSIONS["viewer"] | ROLE_PERMISSIONS["auditor"], key


def _raw():
    return yaml.safe_load(source_path().read_text(encoding="utf-8"))


def _first_permission(raw):
    return raw["categories"][0]["permissions"][0]


@pytest.mark.parametrize("mutate, message", [
    (lambda r: _first_permission(r).update(key="directory:*"), "key must match"),
    (lambda r: _first_permission(r).update(key="Directory:read"), "key must match"),
    (lambda r: _first_permission(r).update(flags=["delegable", "critical", "step_up"]),
     "never delegable"),
    (lambda r: _first_permission(r).update(flags=["sudo"]), "unknown flags"),
    (lambda r: _first_permission(r).update(roles=["owner", "system_worker"]), "only system_only"),
    (lambda r: _first_permission(r).update(roles=["owner", "superuser"]), "unknown roles"),
    (lambda r: r["categories"][0]["permissions"].append(copy.deepcopy(_first_permission(r))),
     "duplicate key"),
    (lambda r: r["access_roles"].append({"key": "editor", "name": "Editor",
                                          "bindable_scopes": ["org"]}), "human access roles"),
    (lambda r: r["passport_presets"][0]["permissions"].append("decision:approve"),
     "not delegable"),
])
def test_a_catalogue_that_breaks_a_rule_is_refused(mutate, message):
    raw = _raw()
    mutate(raw)
    with pytest.raises(CatalogueError, match=message):
        parse(raw)


def test_the_source_loads():
    assert len(load().permissions) == 195
