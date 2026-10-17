"""Removing a deployed app is governed like a deploy, and touches only that app.

Community refused every host teardown, so an operator had to ssh in to remove HelpDesk. A
removal is a deployment run of kind `remove` (services/app_removal.py).
"""
from __future__ import annotations

import pytest

from boss_engine.services import app_removal as ar


@pytest.mark.parametrize("bad", [
    "/opt/boss-apps", "/opt/boss-apps/", "/opt/boss-apps/../etc", "/opt/boss-apps/a/b",
    "/etc/passwd", "/opt/boss-apps/..", "opt/boss-apps/x", "/opt/boss-apps/x y", "",
    "/opt/boss-apps/HelpDesk", "/opt/boss-apps/./x",
])
def test_the_path_guard_refuses_anything_but_one_app_folder(bad):
    with pytest.raises(ar.RemovalRefused):
        ar.guard_folder(bad)


def test_the_path_guard_accepts_an_app_folder():
    assert ar.guard_folder("/opt/boss-apps/helpdesk-ai") == "/opt/boss-apps/helpdesk-ai"
    with pytest.raises(ar.RemovalRefused):
        ar.guard_project("helpdesk; rm -rf /")


def test_data_is_kept_unless_asked_and_the_script_names_only_the_scope():
    scope = {"projects": ["helpdesk-ai"], "containers": ["helpdesk"],
             "folders": ["/opt/boss-apps/helpdesk-ai", "/opt/boss-apps/helpdesk"]}
    kept = ar.removal_script(scope, delete_data=False)
    assert "down --remove-orphans 2>&1" in kept and " -v" not in kept
    assert "! -name data" in kept and "rm -rf -- /opt/boss-apps/helpdesk-ai;" not in kept
    gone = ar.removal_script(scope, delete_data=True)
    assert "down --remove-orphans -v" in gone and "rm -rf -- /opt/boss-apps/helpdesk-ai" in gone
    for line in gone.splitlines()[1:]:
        assert "helpdesk" in line          # every command names this app's scope, nothing else


def test_a_script_cannot_be_given_a_path_outside_the_app():
    with pytest.raises(ar.RemovalRefused):
        ar.removal_script({"folders": ["/opt/boss-apps/../../root"]}, delete_data=True)


def test_delete_data_defaults_off_and_the_plan_is_a_teardown_changeset(monkeypatch):
    import inspect
    assert inspect.signature(ar.plan_removal).parameters["delete_data"].default is False
    src = inspect.getsource(ar.plan_removal)
    assert '"effect_kind": "deployment_target.teardown"' in src and '"destructive": True' in src
    # The environment's policy decides whether a person is asked (staging may not ask).
    assert "pre_approval(connection, project_id, environment)" in src
    assert "create_blocking_decision" in src


def test_a_removal_runs_through_the_governed_host_apply():
    import inspect
    from boss_engine.services import governed_host_deploy as g
    assert "remove_on_host" in inspect.getsource(g.apply_on_host)
    assert "after_removal" in inspect.getsource(g._finalize)


def test_a_folder_another_active_app_uses_is_never_removed():
    """A rebuilt app (HelpDesk AI) can reuse its predecessor's folder and compose project;
    removing the OLD target must leave the new one alone."""
    import inspect
    src = inspect.getsource(ar.removal_scope)
    assert "t.status = 'active'" in src and "s.target_id <> :t" in src
    assert "if f not in taken_dirs" in src and "if p not in taken_projects" in src


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None


class _Conn:
    """Answers removal_scope's three queries: this target's deploys, other active ones, single."""

    def __init__(self, own, others):
        self.own, self.others = own, others

    def execute(self, stmt, params=None):
        sql = str(stmt)
        if "s.target_id <> :t" in sql:
            return _Rows(self.others)
        if "FROM stack_deployments" in sql:
            return _Rows(self.own)
        return _Rows([])


def test_a_compose_app_with_resources_is_removed_from_the_shared_project():
    """deprun_bdb914b2: Remove stopped `helpdesk-ai` while the app and its database ran in the
    resources' project `bossapp-helpdesk-ai-deploy`, so nothing stopped."""
    own = [{"deploy_dir": "/opt/boss-apps/helpdesk-ai",
            "manifest": {"kind": "compose", "name": "helpdesk-ai",
                         "shared_compose_project": "bossapp-helpdesk-ai-deploy"}}]
    scope = ar.removal_scope(_Conn(own, []), {"id": "t1", "repo": "o/r", "app_name": "helpdesk-ai"})
    assert "bossapp-helpdesk-ai-deploy" in scope["projects"]
    # ...but never when another active app now runs in that shared project.
    other = [{"deploy_dir": "/opt/boss-apps/helpdesk-ai-2",
              "manifest": {"kind": "compose", "name": "helpdesk-ai-2",
                           "shared_compose_project": "bossapp-helpdesk-ai-deploy"}}]
    scope = ar.removal_scope(_Conn(own, other), {"id": "t1", "repo": "o/r", "app_name": "helpdesk-ai"})
    assert "bossapp-helpdesk-ai-deploy" not in scope["projects"]
