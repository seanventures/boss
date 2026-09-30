"""The repository you connect in Setup is the one BOSS pushes to.

The wizard wrote a connection and a repository binding, and `create_code_task` already resolved
the repo NAME through that registry — but every path that actually pushes read
`settings.github_token`, an environment variable. So a wizard-connected install reported
"Connected — owner/name is the build target" and its first build died at push on a message
telling the operator to set the variable the wizard exists to replace. The registry recorded
intent the runtime ignored.
"""
import inspect

from boss_engine.services import code_task_service, github_app_adapter, governed_code_task_service
from boss_engine.services.github_connections import GitHubConnectionService


def test_the_shared_resolver_can_be_given_a_project():
    signature = inspect.signature(github_app_adapter.repo_token)
    assert "connection" in signature.parameters
    assert "project_id" in signature.parameters
    # Optional, so the callers that have no transaction in scope are untouched.
    assert signature.parameters["connection"].default is None
    assert signature.parameters["project_id"].default == ""


def test_the_registry_is_the_third_source_and_the_last_one():
    """Every install configured the old way must keep behaving exactly as it did — a stale
    connection row must not be able to take a working build's credential away."""
    source = inspect.getsource(github_app_adapter.repo_token)
    env_at = source.index('token = getattr(settings, "github_token", "")')
    registry_at = source.index("token_for_repository(")
    assert env_at < registry_at


def test_and_the_refusal_now_names_the_wizard_too():
    source = inspect.getsource(github_app_adapter.repo_token)
    assert "no repository connected in Setup" in source


def test_the_resolver_prefers_the_exact_repo_then_the_primary():
    """A project with one repo should not have to spell it out; a project with several must not
    hand over the wrong account's token."""
    source = inspect.getsource(GitHubConnectionService.token_for_repository)
    assert "LOWER(b.full_name) = :want" in source
    assert "b.role = 'primary'" in source
    # Only live rows: a revoked connection is not a credential.
    assert "b.status = 'active'" in source
    assert "c.status = 'enabled'" in source


def test_a_missing_registry_is_not_an_error():
    """The caller has an environment fallback, and most installs have no registry at all."""
    source = inspect.getsource(GitHubConnectionService.token_for_repository)
    assert "return None" in source


def test_both_build_paths_pass_the_project_through():
    for module in (code_task_service, governed_code_task_service):
        source = inspect.getsource(module)
        assert "connection=c, project_id=project_id" in source, module.__name__


def test_nothing_here_names_the_operator_platform():
    """This ships as open source. The engine must not say where anybody's boxes are."""
    assert "shared-server" not in inspect.getsource(github_app_adapter).lower()
