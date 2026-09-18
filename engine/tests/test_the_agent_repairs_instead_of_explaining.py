""""Just explaining the problem would not help anyone."

Asked the same setup question twice, BOSS diagnosed it correctly both times — down to naming
the file and line of the duplicate-label bug — and then told the operator to go and press a
button itself. A diagnosis with no matching capability is a loop, and setup is where that loop
hurts most, because it is the first thing every customer touches.

Two halves.

RETRYING SETUP IS A RETRY. The first attempt stored the connection and then failed its access
test, correctly: the token could not see the repository. Fixing the token and pressing Connect
again was refused — "Project already has a GitHub connection with this label" — because the
label is fixed at "GitHub" and easy_connect only ever created. So the screen whose entire job
is getting this right could not be used twice.

AND THE AGENT CAN NOW ACT. `test_github_access` re-runs the check it used to describe, and
`set_project_setting` applies a recommendation instead of pointing at a page. Both are in the
proposable set, so they travel the existing confirm-before-run path rather than being a new
kind of power.
"""
from __future__ import annotations

import inspect

from boss_engine.services.github_connections import GitHubConnectionService
from boss_engine.tools import build_discuss_read_registry
from boss_engine.tools.builtin import _DISCUSS_OPERATIONS_TOOLS, build_default_registry


def _tool(name):
    return {spec.name: spec for spec in build_default_registry().specs()}[name]


def test_a_second_attempt_reuses_the_connection():
    source = inspect.getsource(GitHubConnectionService.easy_connect)
    assert "_connection_by_label(connection, project_id, connection_label)" in source
    assert "self.repoint_connection(" in source


def test_and_keeps_the_id_the_bindings_and_the_history():
    """Deleting and recreating would lose the repository binding and every receipt attached to
    the connection — a retry must not cost the operator their setup."""
    source = inspect.getsource(GitHubConnectionService.repoint_connection)
    assert "UPDATE github_connections" in source
    assert "DELETE" not in source.upper()


def test_and_clears_the_failure_it_is_retrying():
    """Leaving last_test_status = 'failed' next to a fresh credential reads as "still broken"
    on a connection nobody has tested yet."""
    source = inspect.getsource(GitHubConnectionService.repoint_connection)
    assert "last_test_status = NULL" in source
    assert "last_error = NULL" in source


def test_the_status_it_sets_is_one_the_column_allows():
    """'enabled' is not in the CHECK constraint — the first version of this raised
    CheckViolation against the live database. 'configured' is the state before a passing
    test; a pass sets 'active'."""
    assert "enabled" not in GitHubConnectionService.CONNECTION_STATUSES
    source = inspect.getsource(GitHubConnectionService.repoint_connection)
    assert "status = 'configured'" in source


def test_and_no_query_filters_on_a_status_that_cannot_exist():
    """Found while fixing the above: a repository lookup filtered `c.status = 'enabled'`, a
    value the constraint forbids — so the condition was never true, the lookup always found
    nothing, and it failed silently."""
    module = inspect.getsource(inspect.getmodule(GitHubConnectionService))
    # WITHOUT THE COMMENTS. The note explaining the old predicate contains the old predicate,
    # so a raw search finds the explanation and calls it the bug — the third time this shape
    # of test has lied in this session.
    code = "\n".join(line for line in module.splitlines()
                     if not line.strip().startswith(("--", "#")))
    assert "c.status = 'enabled'" not in code
    assert "c.status IN ('configured', 'active')" in code


def test_the_event_records_which_credential_was_replaced():
    """"Which credential was this using when it worked" is asked later, and the serialized
    connection does not expose the ref — so reading it from there recorded null."""
    source = inspect.getsource(GitHubConnectionService.repoint_connection)
    assert "previous_ref" in source
    assert "SELECT credential_ref FROM github_connections" in source


# -- and the agent can act -------------------------------------------------------------------

def test_the_agent_can_rerun_the_check_itself():
    """The exact instruction it used to give: "open BOSS's GitHub view, select the repository,
    and click Test access"."""
    assert _tool("test_github_access").kind == "write"
    assert "test_github_access" in build_discuss_read_registry().names()
    source = inspect.getsource(_tool("test_github_access").handler)
    assert "svc.test_connection(" in source


def test_and_the_retest_is_deliberately_repeatable():
    """Receipts dedupe by key, and the whole point is re-asking a question whose answer has
    changed — a fixed key would return the first failure forever."""
    source = inspect.getsource(_tool("test_github_access").handler)
    assert "_uuid.uuid4()" in source


def test_it_finds_the_connection_and_repository_without_being_told():
    """An agent that needs three ids before it can act will ask the operator for them."""
    source = inspect.getsource(_tool("test_github_access").handler)
    assert "ORDER BY created_at DESC LIMIT 1" in source
    assert "(role = 'primary') DESC" in source


def test_the_agent_can_apply_a_setting_it_recommends():
    assert _tool("set_project_setting").kind == "write"
    source = inspect.getsource(_tool("set_project_setting").handler)
    # The allowlist and the bounds are the service's, not the tool's — one place decides.
    assert "ps.KNOBS" in source
    assert "ps.set_one(" in source
    assert "ps.clear_one(" in source


def test_and_a_refused_value_says_what_is_allowed():
    source = inspect.getsource(_tool("set_project_setting").handler)
    assert "ps.offered_choices(" in source


def test_both_go_through_the_confirm_before_run_path():
    """Not a new kind of power: the same proposal flow every other operation uses, so nothing
    runs until the operator presses the button."""
    for name in ("test_github_access", "set_project_setting"):
        assert name in _DISCUSS_OPERATIONS_TOOLS, name


def test_and_neither_one_takes_a_credential():
    """The line the agent must not cross: it can retest and it can change a setting, and it
    can never be handed a token."""
    for name in ("test_github_access", "set_project_setting"):
        schema = (_tool(name).parameters or {}).get("properties") or {}
        for field in schema:
            assert not any(word in field for word in ("token", "secret", "credential", "key")) \
                or field == "key", (name, field)
