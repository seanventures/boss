"""Mutating delivery/fleet endpoints must require a project actor.

`/execute` and `/redeploy` already gate on providerops_manage. Manual deploy,
first deploy, host verify/attach/backfill/update, unbind, and job cancel defaulted
actor to `operator` and shipped — knowing a target_id, run_id, or job_id
was enough.

Read as text so this check does not boot the API process.
"""

from __future__ import annotations

from pathlib import Path

MAIN = Path(__file__).resolve().parents[1] / "apps" / "api" / "app" / "main.py"
SOURCE = MAIN.read_text()


def _fn(name: str) -> str:
    needle = f"def {name}("
    start = SOURCE.index(needle)
    rest = SOURCE[start + len(needle) :]
    nxt = len(rest)
    for token in ("\n@app.", "\ndef "):
        i = rest.find(token)
        if i != -1:
            nxt = min(nxt, i)
    return SOURCE[start : start + len(needle) + nxt]


def test_manual_deploy_requires_providerops_on_the_target_project() -> None:
    src = _fn("delivery_deploy_endpoint")
    assert "require_project_actor" in src
    assert "providerops_manage" in src
    assert 'or "operator")' not in src


def test_first_deploy_requires_providerops_on_the_project() -> None:
    """First-deploy shipped a completed build with no actor check.

    Execute and redeploy already required providerops_manage. The first-deploy
    path added later did not, so knowing a project_id and crun_ was enough to
    ship to staging or production.
    """
    src = _fn("create_deployment_endpoint")
    assert "require_project_actor" in src
    assert "providerops_manage" in src
    assert 'or "operator")' not in src


def test_first_deploy_does_not_shadow_the_target_registry() -> None:
    """POST /v1/deployments is the target registry (201 + app_name).

    The first-deploy handler was registered on the same path first, so every
    onboarded target returned 400 'run_id is required' instead of creating a
    destination. First-deploy must live under /v1/delivery/.
    """
    start = SOURCE.index("def create_deployment_endpoint")
    header = SOURCE[max(0, start - 280) : start]
    assert "/v1/deployments" not in header
    assert "/v1/delivery/first-deploy" in header
    target = _fn("create_deployment_target")
    assert "require_project_actor" in target
    assert "providerops_manage" in target


def test_mounted_deploy_routes_have_distinct_handlers() -> None:
    """FastAPI dispatches to the first registered path, so inspect the mounted table."""
    from apps.api.app.main import app

    def post_handler(path: str) -> str:
        routes = [route for route in app.routes
                  if route.path == path and "POST" in (route.methods or set())]
        assert len(routes) == 1
        return routes[0].endpoint.__name__

    assert post_handler("/v1/deployments") == "create_deployment_target"
    assert post_handler("/v1/delivery/first-deploy") == "create_deployment_endpoint"


def test_host_verify_requires_a_project_actor() -> None:
    src = _fn("verify_deployment_host_endpoint")
    assert "require_project_actor" in src
    assert "actor" in src


def test_host_attach_requires_a_project_actor() -> None:
    src = _fn("attach_target_to_host_endpoint")
    assert "require_project_actor" in src


def test_host_backfill_requires_a_project_actor() -> None:
    src = _fn("backfill_deployment_hosts_endpoint")
    assert "require_project_actor" in src


def test_host_update_requires_a_project_actor() -> None:
    src = _fn("update_deployment_host_endpoint")
    assert "require_project_actor" in src


def test_job_cancel_requires_a_project_actor() -> None:
    src = _fn("cancel_job_endpoint")
    assert "require_project_actor" in src
    assert "project_id" in src


def test_setup_deployment_refuses_a_host_from_another_project() -> None:
    src = _fn("setup_deployment_for_code_task_endpoint")
    assert "different project" in src


def test_unbind_requires_a_project_actor() -> None:
    """DELETE /v1/deployment-targets/{id} retired any target by primary key.

    The console Unbind button made this reachable; without actor + project
    fence anyone who could hit the API could unbind another tenant's app.
    """
    src = _fn("retire_deployment_target_endpoint")
    assert "require_project_actor" in src
    assert "providerops_manage" in src
    assert "actor is required" in src


def test_linked_target_lookup_is_project_scoped() -> None:
    """The card's Unbind id came from app_name alone.

    Official canaries reuse ApproveIt / BugBox / HookWatch. ORDER BY created_at
    DESC LIMIT 1 without project_id returned another tenant's target, and Unbind
    retired it.
    """
    src = _fn("deployments_for_code_task_endpoint")
    assert "t.project_id = :p" in src
    assert "require_code_task_reader(connection, run_id)" in src
