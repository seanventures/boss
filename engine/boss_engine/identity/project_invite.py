"""An invite that grants one project role.

Accepting it binds that project only. It does not grant an org-wide role and it does not
include a second project. The row records who sent it. Accepting the same invite again does
not add a second binding.

The row is an identity_invitations row. This module does not keep a second invite store.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from sqlalchemy import text

from .invitations import Invitation, InvitationRefused
from .invitations import accept as _accept_invitation
from .invitations import create as _create_invitation


class ProjectInviteRefused(InvitationRefused):
    """The invite is not one project role, or it can no longer be accepted."""


def _one_project(bindings: list[dict[str, Any]]) -> dict[str, Any]:
    if len(bindings) != 1:
        raise ProjectInviteRefused("not_one_project", 422)
    binding = bindings[0]
    if (
        binding.get("scope_type") != "project"
        or not binding.get("scope_id")
        or not binding.get("role_key")
    ):
        raise ProjectInviteRefused("not_one_project", 422)
    return {
        "role_key": str(binding["role_key"]),
        "scope_type": "project",
        "scope_id": str(binding["scope_id"]),
    }


def _stored_bindings(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, list):
        return list(raw)
    return list(json.loads(raw or "[]"))


def create(
    db: Any, *, sender: Any, email: str, project_id: str, role_key: str
) -> tuple[Invitation, str]:
    """Invite one address to one project role. The sender is stored on the row."""
    if not str(project_id or "").strip() or not str(role_key or "").strip():
        raise ProjectInviteRefused("invalid_binding", 422)
    try:
        invite, token = _create_invitation(
            db,
            inviter=sender,
            email=email,
            bindings=[{"role_key": role_key, "scope_type": "project", "scope_id": project_id}],
        )
    except InvitationRefused as exc:
        raise ProjectInviteRefused(exc.code, exc.status, exc.detail) from None
    _one_project(list(invite.bindings))
    return invite, token


def accept(
    db: Any,
    invite: Invitation,
    *,
    display_name: str,
    password_hash: Optional[str] = None,
    email_verified: bool = False,
) -> str:
    """Bind the invited project role once. A second accept adds no binding."""
    row = db.execute(
        text(
            "SELECT bindings, accepted_at, revoked_at, group_ids FROM identity_invitations "
            "WHERE id = :id FOR UPDATE"
        ),
        {"id": invite.id},
    ).first()
    if row is None or row.accepted_at is not None or row.revoked_at is not None:
        raise ProjectInviteRefused("invitation_invalid", 400)
    binding = _one_project(_stored_bindings(row.bindings))
    # Groups on a project invite would carry whatever those groups hold, including another
    # project or an org role. A project invite has none.
    if list(row.group_ids or []):
        raise ProjectInviteRefused("not_one_project", 422)
    stored = Invitation(
        invite.id,
        invite.org_id,
        invite.email,
        [binding],
        [],
        invite.invited_by,
        invite.expires_at,
        None,
        None,
    )
    try:
        return _accept_invitation(
            db,
            stored,
            display_name=display_name,
            password_hash=password_hash,
            email_verified=email_verified,
        )
    except InvitationRefused as exc:
        raise ProjectInviteRefused(exc.code, exc.status, exc.detail) from None
