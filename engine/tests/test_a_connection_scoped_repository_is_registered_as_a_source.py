"""`register_repository(connection_id=...)` (the console's per-connection bind) must pass a role
GitHubConnectionService actually allows. It defaulted to "secondary", which is not in
REPOSITORY_ROLES (primary|source|upstream|mirror) and so always failed — every connection-scoped
registration was broken (FR1.4)."""
import inspect

import pytest

from boss_engine.config import get_settings
from boss_engine.services.github_connections import GitHubConnectionService
from boss_engine.tools import builtin

from ._multirepo_support import make_engine, make_world


def test_p0_the_tool_no_longer_defaults_to_the_rejected_secondary_role():
    source = inspect.getsource(builtin)
    assert 'args.get("role") or "secondary"' not in source
    assert 'args.get("role") or "source"' in source


def test_p1_a_connection_scoped_binding_is_created_with_an_allowed_role():
    engine = make_engine()
    world = make_world(engine)
    pid = world["projects"][0]
    connection_id = world["connections"][pid]

    service = GitHubConnectionService(get_settings())
    with engine.begin() as c:
        row = service.create_repository_binding(
            c, project_id=pid, connection_id=connection_id, repository="acme/api",
            role="source", default_branch=None, metadata={}, actor="test",
        )
    assert row["role"] == "source"


def test_p1_an_explicit_bogus_role_is_rejected_with_a_clear_error_not_a_500():
    engine = make_engine()
    world = make_world(engine)
    pid = world["projects"][0]
    connection_id = world["connections"][pid]

    service = GitHubConnectionService(get_settings())
    with pytest.raises(ValueError, match="role must be one of"):
        with engine.begin() as c:
            service.create_repository_binding(
                c, project_id=pid, connection_id=connection_id, repository="acme/api",
                role="bogus", default_branch=None, metadata={}, actor="test",
            )
