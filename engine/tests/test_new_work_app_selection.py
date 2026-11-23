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
    with pytest.raises(ValueError, match="Choose which"):
        new_work.work_anchor(None, "project", None)


def test_explicit_app_wins(selection):
    selection.extend([{"run_id": "crun_one"}, {"run_id": "crun_two"}])
    assert new_work.work_anchor(None, "project", "crun_two") == {"run_id": "crun_two"}


def test_new_app_is_explicit(selection):
    selection.append({"run_id": "crun_existing"})
    assert new_work.work_anchor(None, "project", None, new_app=True) is None
    with pytest.raises(ValueError, match="not both"):
        new_work.work_anchor(None, "project", "crun_existing", new_app=True)
