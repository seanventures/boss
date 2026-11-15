"""E7 people links (P9a-7): the BOSS person behind each chat link (name, email) is directory
data. GET /v1/intake/connections/{id}/people is integration:read, but only a caller holding
user:read (Owner, Admin, Auditor — what GET /v1/identity/users needs) gets the mapping;
Operator and Viewer see the link, its method and when, never who it is."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from apps.api.app.routers import intake
from boss_engine.identity import authz
from boss_engine.identity.permissions_gen import ROLE_PERMISSIONS

SEES = {"owner": True, "admin": True, "auditor": True, "operator": False, "viewer": False}


@pytest.mark.parametrize("role,sees", sorted(SEES.items()))
def test_each_built_in_role_sees_the_people_only_with_user_read(monkeypatch, role, sees):
    from apps.api.app import identity_guard

    ctx = SimpleNamespace(principal=SimpleNamespace(org_id="org_x", id=f"usr_{role}"))
    monkeypatch.setattr(identity_guard, "request_auth", lambda request: ctx)
    monkeypatch.setattr(authz, "effective_permissions",
                        lambda c, scope, db=None: SimpleNamespace(permissions=frozenset(ROLE_PERMISSIONS[role])))
    assert intake.people_visible(object()) is sees
    # The route itself stays readable by every role that reads integrations.
    assert "integration:read" in ROLE_PERMISSIONS[role]


def test_no_identity_or_a_failing_check_withholds(monkeypatch):
    from apps.api.app import identity_guard

    monkeypatch.setattr(identity_guard, "request_auth", lambda request: None)
    assert intake.people_visible(object()) is False

    def boom(request):
        raise RuntimeError("no guard")

    monkeypatch.setattr(identity_guard, "request_auth", boom)
    assert intake.people_visible(object()) is False
