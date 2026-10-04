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
from boss_engine.services import github_connections as github_connections_mod
from boss_engine.tools import build_discuss_read_registry
from boss_engine.tools.builtin import _DISCUSS_OPERATIONS_TOOLS, build_default_registry


def _tool(name):
    return {spec.name: spec for spec in build_default_registry().specs()}[name]


def test_a_second_attempt_reuses_the_connection():
    source = inspect.getsource(GitHubConnectionService.easy_connect)
    assert "_connection_by_label(connection, project_id, connection_label)" in source
    assert "self.repoint_connection(" in source
    assert "self._existing_repository_binding(" in source


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
    and click Test access". It runs Repo and models' own three probes instead."""
    assert _tool("test_github_access").kind == "write"
    assert "test_github_access" in build_discuss_read_registry().names()
    source = inspect.getsource(_tool("test_github_access").handler)
    assert "setup_actions.check_repository(" in source


def test_and_the_retest_is_deliberately_repeatable():
    """Every check writes its own receipt — a fixed dedupe key would return the first failure
    forever."""
    from boss_engine.services import setup_actions

    source = inspect.getsource(setup_actions._receipt)
    assert "uuid.uuid4()" in source


def test_it_finds_the_connection_and_repository_without_being_told():
    """An agent that needs three ids before it can act will ask the operator for them: the
    default repository is found, and an install configured from its settings file too."""
    source = inspect.getsource(_tool("test_github_access").handler)
    assert "(role = 'primary') DESC" in source
    assert 'target = "install"' in source


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


# -- connecting a different repository ---------------------------------------------------------

def test_connecting_a_new_primary_replaces_the_old_one():
    """The wizard says "Connecting something below replaces what is here for this project".
    What it did was raise "Project already has an active primary repository" — the screen's
    promise and the service's rule disagreeing, with the operator finding out. Same shape as
    the connection-label collision."""
    source = inspect.getsource(GitHubConnectionService.easy_connect)
    assert "self._retire_primary_binding(" in source


def test_and_the_old_one_is_disabled_not_deleted():
    """Which repository this project used to build in is worth keeping, and re-enabling is a
    row update rather than a re-connect."""
    source = inspect.getsource(GitHubConnectionService._retire_primary_binding)
    assert "status = 'disabled'" in source
    # The STATEMENT, not the word: the docstring says "disabled rather than deleted", and an
    # uppercased substring search finds that sentence and calls it a DELETE.
    assert "DELETE FROM" not in source.upper()
    assert "github.primary_repository_replaced" in source


def test_reconnecting_the_same_repository_retires_nothing():
    """That is a retry — the binding is reused, and disabling it would break the run that is
    already pointed at it."""
    source = inspect.getsource(GitHubConnectionService._retire_primary_binding)
    assert 'if (row["full_name"] or "").lower() == full_name.lower():' in source
    assert "continue" in source


def test_and_only_a_primary_stands_down_a_primary():
    """Binding a `source` or `mirror` must not disturb where the project builds."""
    source = inspect.getsource(GitHubConnectionService._retire_primary_binding)
    assert 'if (role or "primary").strip().lower() != "primary":' in source


def test_the_guard_still_protects_the_general_api():
    """A multi-repo customer binding a second primary by mistake should still be stopped —
    only the wizard, where the intent is explicit, replaces."""
    source = inspect.getsource(GitHubConnectionService.create_repository_binding)
    assert "Project already has an active primary repository." in source


def test_a_second_connect_keeps_the_binding_it_already_made(monkeypatch):
    """The first Connect already bound the repository. Creating it again is how retry dies.

    Trigger: paste a token that cannot see the repo → Connect creates the connection AND the
    primary binding, then the access test fails (201, committed). Fix the token, press
    Connect again. easy_connect repoints the connection, then calls create_repository_binding
    for the same owner/name. That raises "Project already has an active primary repository."
    The API wraps the whole call in one transaction, so the new credential rolls back too.
    The operator is stuck: every subsequent Connect 422s, and the connection still holds
    the token that cannot see the repo.

    The comment on the retry promised it would keep the binding. The create after repoint
    is what broke that promise.
    """
    from boss_engine.config import Settings

    svc = GitHubConnectionService(Settings())
    existing_connection = {
        "id": "ghconn_retry",
        "auth_method": "personal_access_token",
        "status": "configured",
    }
    existing_binding = {
        "id": "ghrepo_retry",
        "full_name": "acme/app",
        "connection_id": "ghconn_retry",
        "status": "active",
        "role": "primary",
    }
    created_bindings: list[str] = []

    svc.store_token_credential = lambda *args, **kwargs: "infisical:github_retry"  # type: ignore[method-assign]
    svc._connection_by_label = lambda *args, **kwargs: existing_connection  # type: ignore[method-assign]
    svc.repoint_connection = lambda *args, **kwargs: existing_connection  # type: ignore[method-assign]
    svc._retire_primary_binding = lambda *args, **kwargs: None  # type: ignore[method-assign]
    svc._existing_repository_binding = lambda *args, **kwargs: existing_binding  # type: ignore[method-assign]
    svc.create_connection = (  # type: ignore[method-assign]
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("retry must not create a connection"))
    )

    def _create_binding(*args, **kwargs):
        created_bindings.append(kwargs.get("repository") or "")
        raise ValueError("Project already has an active primary repository.")

    svc.create_repository_binding = _create_binding  # type: ignore[method-assign]
    svc.test_connection = (  # type: ignore[method-assign]
        lambda *args, **kwargs: {
            "status": "passed",
            "connection": existing_connection,
            "repository_binding": existing_binding,
        }
    )
    monkeypatch.setattr(github_connections_mod, "emit_event", lambda *args, **kwargs: None)

    result = svc.easy_connect(
        object(),
        project_id="proj_1",
        connection_label="GitHub",
        auth_method="personal_access_token",
        env_credential_key="",
        app_id=None,
        installation_id=None,
        api_base_url="https://api.github.com",
        required_permissions=["pull", "push"],
        repository="acme/app",
        role="primary",
        test_after_link=True,
        metadata={},
        actor="ceo",
        token="github_pat_new_token",
    )

    assert created_bindings == [], created_bindings
    assert result["repository_binding"]["id"] == "ghrepo_retry"
    assert result["connection"]["id"] == "ghconn_retry"
    assert result["test"]["status"] == "passed"


def test_a_first_connect_still_creates_the_binding(monkeypatch):
    """Reuse is for a binding that exists. The first Connect still has to make one."""
    from boss_engine.config import Settings

    svc = GitHubConnectionService(Settings())
    created_connection = {
        "id": "ghconn_first",
        "auth_method": "personal_access_token",
        "status": "configured",
    }
    created_binding = {
        "id": "ghrepo_first",
        "full_name": "acme/app",
        "connection_id": "ghconn_first",
        "status": "active",
        "role": "primary",
    }
    created: list[str] = []

    svc.store_token_credential = lambda *args, **kwargs: "infisical:github_first"  # type: ignore[method-assign]
    svc._connection_by_label = lambda *args, **kwargs: None  # type: ignore[method-assign]
    svc._retire_primary_binding = lambda *args, **kwargs: None  # type: ignore[method-assign]
    svc._existing_repository_binding = lambda *args, **kwargs: None  # type: ignore[method-assign]
    svc.create_connection = lambda *args, **kwargs: created_connection  # type: ignore[method-assign]

    def _create_binding(*args, **kwargs):
        created.append(kwargs.get("repository") or "")
        return created_binding

    svc.create_repository_binding = _create_binding  # type: ignore[method-assign]
    svc.test_connection = (  # type: ignore[method-assign]
        lambda *args, **kwargs: {
            "status": "passed",
            "connection": created_connection,
            "repository_binding": created_binding,
        }
    )
    monkeypatch.setattr(github_connections_mod, "emit_event", lambda *args, **kwargs: None)

    result = svc.easy_connect(
        object(),
        project_id="proj_1",
        connection_label="GitHub",
        auth_method="personal_access_token",
        env_credential_key="",
        app_id=None,
        installation_id=None,
        api_base_url="https://api.github.com",
        required_permissions=["pull", "push"],
        repository="acme/app",
        role="primary",
        test_after_link=True,
        metadata={},
        actor="ceo",
        token="github_pat_first_token",
    )

    assert created == ["acme/app"]
    assert result["repository_binding"]["id"] == "ghrepo_first"
    assert result["test"]["status"] == "passed"
