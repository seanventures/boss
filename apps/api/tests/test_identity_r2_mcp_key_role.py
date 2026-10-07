"""R2: the MCP key's access role (bound at ONE project) carried four org-only permissions —
directory:read, org:read, access_role:read, api_key:manage_own — which do nothing at a project
(A3) and which the role editor would have refused. It holds project permissions only."""
from __future__ import annotations

import pytest

from boss_engine.identity.host_tools import MCP_DEFAULT_CAPABILITIES, mcp_permissions
from boss_engine.identity.permissions_gen import PERMISSION_META


def test_the_mcp_role_holds_no_org_only_permission():
    perms = mcp_permissions(MCP_DEFAULT_CAPABILITIES)
    assert perms and not [k for k in perms if PERMISSION_META[k].org_only]
    assert "project:read" in perms and "workflow_run:start" in perms


def test_asking_for_an_org_only_permission_by_name_is_refused():
    with pytest.raises(ValueError):
        mcp_permissions(["user:invite"])
