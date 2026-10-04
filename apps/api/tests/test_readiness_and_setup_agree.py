"""Workflow readiness and Repo and models read the same rows, so they cannot disagree, and the
readiness fixes land on the right row."""
from apps.api.tests.setup_helpers import PROJECT, as_ceo, connect_key, connect_repository, fakes
from apps.api.app import main as api_main
from boss_engine.services import setup_view, workflow_readiness


def _readiness(engine, key):
    with engine.begin() as c:
        return workflow_readiness.check(c, api_main.settings, PROJECT, key)[0]


def _view(engine):
    with engine.begin() as c:
        return setup_view.build(c, api_main.settings, PROJECT)


def test_they_agree_through_every_state(client, engine, monkeypatch):
    as_ceo(monkeypatch)
    fakes(monkeypatch)
    states = []
    for step in ("empty", "read_only", "full", "key"):
        if step == "read_only":
            connect_repository(client, token="ghp_e2e_contents_read_only")
        elif step == "full":
            connect_repository(client, token="ghp_e2e_full")
        elif step == "key":
            assert connect_key(client).status_code == 200
        view = _view(engine)
        repo_ok = any(setup_view.repository_working(r) for r in view["repository"]["repos"])
        model_ok = any(setup_view.model_working(m) for m in view["models"])
        assert _readiness(engine, "repository") == repo_ok, step
        assert _readiness(engine, "model") == model_ok, step
        states.append((repo_ok, model_ok, view["ready"]))
    assert states == [(False, False, False), (False, False, False), (True, False, False),
                      (True, True, True)]


def test_the_fixes_carry_focus():
    assert workflow_readiness.NEEDS["repository"].href == "/?area=setup&focus=repository"
    assert workflow_readiness.NEEDS["model"].href == "/?area=setup&focus=models"
    # Outgoing email lives in Skills and MCP now.
    assert workflow_readiness.NEEDS["email"].href == "/?area=skills&ability=email"
