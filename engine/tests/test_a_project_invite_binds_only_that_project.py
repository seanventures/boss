"""An invite for one project role binds that project only.

Accepting it does not grant an org-wide role and does not include a second project. The invite
records who sent it. Accepting the same invite again does not add another binding.
"""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine, text

from boss_engine.identity.principal import (
    AuthContext,
    AuthMethod,
    Principal,
    PrincipalKind,
    PrincipalStatus,
)
from boss_engine.identity.project_invite import ProjectInviteRefused, accept, create

URL = os.environ.get(
    "BOSS_TEST_DATABASE_URL",
    "postgresql+psycopg://boss:boss@127.0.0.1:55927/boss_test_invite",
)


@pytest.fixture(scope="module")
def pg():
    from boss_engine.db import ensure_schema

    engine = create_engine(URL, future=True)
    ensure_schema(engine)
    yield engine
    engine.dispose()


def _bindings(db, principal_id: str) -> list[dict]:
    rows = (
        db.execute(
            text(
                "SELECT b.scope_type, b.scope_id, b.granted_by, r.key AS role_key "
                "FROM identity_role_bindings b JOIN identity_access_roles r ON r.id = b.role_id "
                "WHERE b.subject_type = 'principal' AND b.subject_id = :p "
                "ORDER BY b.scope_type, b.scope_id"
            ),
            {"p": principal_id},
        )
        .mappings()
        .all()
    )
    return [dict(row) for row in rows]


def test_accepting_a_project_invite_binds_only_that_project(pg):
    tag = uuid.uuid4().hex[:10]
    org, workspace = f"org_inv{tag}", f"ws_inv{tag}"
    project_a, project_b = f"pa-{tag}", f"pb-{tag}"
    sender_id = f"usr_inv{tag}"
    email = f"ada-{tag}@example.test"
    with pg.begin() as db:
        db.execute(text("INSERT INTO orgs (id, name) VALUES (:o, :o)"), {"o": org})
        db.execute(
            text("INSERT INTO workspaces (id, org_id, name) VALUES (:w, :o, :w)"),
            {"w": workspace, "o": org},
        )
        for project_id in (project_a, project_b):
            db.execute(
                text("INSERT INTO projects (id, workspace_id, name, slug) VALUES (:p, :w, :p, :p)"),
                {"p": project_id, "w": workspace},
            )
        db.execute(
            text(
                "INSERT INTO identity_principals (id, org_id, kind, status, display_name) "
                "VALUES (:id, :o, 'user', 'active', 'Sender')"
            ),
            {"id": sender_id, "o": org},
        )
        db.execute(
            text(
                "INSERT INTO identity_role_bindings "
                "(id, org_id, subject_type, subject_id, role_id, scope_type, scope_id, granted_by) "
                "VALUES (:id, :o, 'principal', :s, 'arl_owner', "
                "'org', :o, 'test')"
            ),
            {"id": f"rbd_inv{tag}", "o": org, "s": sender_id},
        )
        sender = AuthContext(
            Principal(sender_id, PrincipalKind.USER, org, PrincipalStatus.ACTIVE, "Sender"),
            AuthMethod.SYSTEM,
        )
        invite, _token = create(
            db, sender=sender, email=email, project_id=project_a, role_key="operator"
        )
        stored = db.execute(
            text("SELECT invited_by, bindings FROM identity_invitations WHERE id = :id"),
            {"id": invite.id},
        ).one()
        assert stored.invited_by == sender_id
        raw = stored.bindings if isinstance(stored.bindings, list) else list(stored.bindings)
        assert raw == [{"role_key": "operator", "scope_type": "project", "scope_id": project_a}]

        person_id = accept(db, invite, display_name="Ada")
        held = _bindings(db, person_id)
        assert held == [
            {
                "scope_type": "project",
                "scope_id": project_a,
                "granted_by": sender_id,
                "role_key": "operator",
            }
        ]
        assert not any(row["scope_type"] == "org" for row in held)
        assert project_b not in {row["scope_id"] for row in held}

        try:
            again = accept(db, invite, display_name="Ada")
        except ProjectInviteRefused:
            again = None
        assert again in (None, person_id)
        assert _bindings(db, person_id) == held
