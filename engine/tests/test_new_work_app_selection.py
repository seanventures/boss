import pytest

from boss_engine.services.project_experience import new_work


@pytest.fixture
def selection(monkeypatch):
    choices = []
    monkeypatch.setattr(new_work, "app_choices", lambda connection, project_id: choices)
    monkeypatch.setattr(new_work, "app_anchor", lambda connection, project_id, app: {"run_id": app})
    return choices


def test_empty_project_has_no_anchor(selection):
    assert new_work.work_anchor(None, "project", None) is None


def test_only_app_is_automatically_anchored(selection):
    selection.append({"run_id": "crun_existing", "app": "Existing"})
    assert new_work.work_anchor(None, "project", None) == {"run_id": "crun_existing"}


def test_multiple_apps_require_a_choice(selection):
    selection.extend([{"run_id": "crun_one"}, {"run_id": "crun_two"}])
    with pytest.raises(ValueError, match="2 apps .*Pick one under Application in New work"):
        new_work.work_anchor(None, "project", None)


def test_explicit_app_wins(selection):
    selection.extend([{"run_id": "crun_one"}, {"run_id": "crun_two"}])
    assert new_work.work_anchor(None, "project", "crun_two") == {"run_id": "crun_two"}


def test_new_app_is_explicit(selection):
    selection.append({"run_id": "crun_existing"})
    assert new_work.work_anchor(None, "project", None, new_app=True) is None
    with pytest.raises(ValueError, match="not both"):
        new_work.work_anchor(None, "project", "crun_existing", new_app=True)


class _Runs:
    """A connection that answers list_code_task_runs (its list_runs duck type)."""

    def __init__(self, runs):
        self.runs = runs

    def list_runs(self, project_id, limit):
        return self.runs


def _run(run_id, title, status="completed", **fctx):
    return {"id": run_id, "project_id": "p", "title": title, "status": status,
            "spec": {"repo": "acme/shop"}, "finalize_ctx": fctx}


def test_choices_group_runs_into_apps_as_every_app_view_does():
    """A follow-up build of an app is the same app (its lineage), a failed-only app is not a
    choice, and BOSS's own housekeeping builds are never offered."""
    runs = [_run("crun_b2", "Lead Inbox", root_run_id="crun_b1"),
            _run("crun_c1", "Ticket Desk", status="failed"),
            _run("crun_q1", "Captain: QUALITY IMPROVE PASS"),
            _run("crun_b1", "Lead Inbox"),
            _run("crun_a1", "Order Board")]
    choices = new_work.app_choices(_Runs(runs), "p")
    assert [c["run_id"] for c in choices] == ["crun_b2", "crun_a1"]
    assert [c["app"] for c in choices] == ["Lead Inbox", "Order Board"]


def test_nothing_named_without_infer_stays_a_fresh_build(selection):
    selection.extend([{"run_id": "crun_one"}, {"run_id": "crun_two"}])
    assert new_work.work_anchor(None, "project", None, infer=False) is None
    assert new_work.work_anchor(None, "project", "crun_two", infer=False) == {"run_id": "crun_two"}
