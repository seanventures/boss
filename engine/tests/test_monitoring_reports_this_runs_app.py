"""The Meeting cost meter workflow said it was watching three apps.

    Set up monitoring · succeeded
    watching Incident-Timeline Builder · staging, Meeting cost meter · staging,
             Vendor-Status-Watcher · staging
    TARGETS 3 items

A workflow run is about ONE app. Two thirds of that was somebody else's work reported as this
step's result — and once the step grew links, it became three prominent links to the wrong apps,
which is how a cosmetic inaccuracy turns into a navigational one.

The capability read every active deployment target in the project because that is what a health
sweep covers. What the STEP is entitled to report is what this run shipped.

Scoping to nothing is the right answer when an app has no target: "nothing is watching this yet"
is true and useful, and listing the neighbours instead is not.
"""
from __future__ import annotations

import inspect

import pytest

from boss_engine.services import workflow_capability_runner as runner
from boss_engine.services.naming import identity_key


class TestFindingTheRunsApp:
    def test_the_deploy_step_is_the_strongest_claim(self):
        """`applied.app` is the thing this run actually shipped."""
        context = {"steps": {"deploy": {"detail": {"applied": {"app": "meeting-cost-meter"}}}}}
        assert runner._this_runs_app(None, context) == identity_key("Meeting cost meter")

    def test_the_slug_and_the_display_name_are_the_same_app(self):
        """The deploy records `meeting-cost-meter`; the target records `Meeting cost meter`.
        Comparing them literally would have matched nothing and scoped every run to zero."""
        assert identity_key("meeting-cost-meter") == identity_key("Meeting cost meter")
        assert identity_key("Vendor-Status-Watcher") == identity_key("vendor-status-watcher")

    def test_a_step_that_is_not_a_deploy_is_ignored(self):
        context = {"steps": {"build": {"detail": {"applied": {"other": "x"}}}}}
        assert runner._this_runs_app(None, context) is None

    def test_a_run_that_names_no_app_is_not_scoped(self):
        """Silently narrowing to nothing would be worse than over-reporting: the step would
        claim the app is unwatched when it simply could not tell which app it was."""
        assert runner._this_runs_app(None, {"steps": {}}) is None

    def test_it_falls_back_to_the_code_task(self):
        source = inspect.getsource(runner._this_runs_app)
        assert "_build_run_id(context)" in source
        assert "code_task_runs" in source

    def test_a_database_failure_leaves_it_unscoped_rather_than_failing_the_step(self):
        class Boom:
            def execute(self, *a, **kw):
                raise RuntimeError("gone")

        context = {"steps": {"build": {"code_task_run_id": "crun_1"}}}
        assert runner._this_runs_app(Boom(), context) is None


class TestTheStepReportsOnlyThatApp:
    def test_targets_are_filtered_to_it(self):
        source = inspect.getsource(runner._observed_monitoring)
        assert 'identity_key(str(t["app_name"] or "")) == wanted' in source

    def test_and_only_when_the_run_says_which_app(self):
        source = inspect.getsource(runner._observed_monitoring)
        assert "if wanted:" in source

    def test_an_app_with_no_target_says_so_about_itself(self):
        """Not "this project has no target" — the project may have several. The claim has to be
        about the app the step is for."""
        source = inspect.getsource(runner._observed_monitoring)
        assert "nothing is watching this app" in source
        assert "this one is not" in source

    def test_the_unscoped_message_still_exists_for_a_project_with_nothing(self):
        source = inspect.getsource(runner._observed_monitoring)
        assert "this project has no active deployment target" in source


class TestTheLinksFollow:
    """The step's links are built from `targets`, so scoping the capability scopes them — which
    is the point of fixing it here rather than filtering in the UI."""

    def test_the_links_come_from_the_same_list_the_step_reports(self):
        import pathlib

        ui = pathlib.Path("apps/web/app/delivery-workflows-ui.tsx").read_text()
        assert "if (Array.isArray(output.targets))" in ui
        assert 'label: `monitor ${app}`' in ui

    def test_and_the_watching_summary_too(self):
        import pathlib

        ui = pathlib.Path("apps/web/app/delivery-workflows-ui.tsx").read_text()
        assert "return names.length ? `watching ${names.join(\", \")}` : null;" in ui


@pytest.mark.parametrize("app,target,matches", [
    ("meeting-cost-meter", "Meeting cost meter", True),
    ("meeting-cost-meter", "Incident-Timeline Builder", False),
    ("Vendor-Status-Watcher", "vendor status watcher", True),
    ("meeting-cost-meter", "Meeting Cost Meter.", True),
])
def test_the_comparison_itself(app, target, matches):
    """As data, because this is the line that decides whether a run sees its own app."""
    assert (identity_key(app) == identity_key(target)) is matches


class TestTheDashboardCheckRuns:
    """The Grafana check read `dashboard_uid(w["app"])` — the key is `app_name` — and the KeyError
    was swallowed as "could not check dashboards", so the step never named a missing dashboard."""

    def test_missing_dashboards_reads_the_app_name(self):
        from boss_engine.services.observability_stack import dashboard_uid

        watched = [{"app_name": "HelpDesk"}, {"app_name": "Onboarding"}]
        assert runner.missing_dashboards(watched, {dashboard_uid("HelpDesk")}) == ["Onboarding"]
        assert runner.missing_dashboards(watched, set()) == ["HelpDesk", "Onboarding"]

    def test_the_step_names_the_apps_grafana_lacks(self, monkeypatch):
        from boss_engine.services import observability_stack

        class Rows:
            def __init__(self, rows):
                self.rows = rows

            def mappings(self):
                return self

            def all(self):
                return self.rows

            def first(self):
                return self.rows[0] if self.rows else None

        class Db:
            def execute(self, statement, params=None):
                sql = str(statement)
                if "FROM deployment_targets" in sql:
                    return Rows([{"id": "deptgt_1", "app_name": "HelpDesk", "environment": "staging",
                                  "domains": ["192.0.2.9:8000"], "health_checks": []}])
                return Rows([])

        monkeypatch.setattr(runner, "_monitoring_host", lambda c, p: "192.0.2.9")
        monkeypatch.setattr(observability_stack, "provisioned_dashboards", lambda base: set())
        out = runner._observed_monitoring(Db(), "p_x", {"steps": {}})
        assert out["missing_dashboards"] == ["HelpDesk"], out
        assert "no Grafana dashboard yet" in out["note"]
