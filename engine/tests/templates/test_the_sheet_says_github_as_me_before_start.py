"""The template sheet says, before Start, when a build needs the person's own GitHub link.

templates.apply.github_as_me answers it for GET /v1/templates/workflows: required when the
install's consent checks the person's own GitHub access (grants.repo_guard_active) and the repo
host is GitHub; linked when they hold an active GitHub connection. Keys and agents never see it.
"""
from __future__ import annotations

from boss_engine.identity import grants
from boss_engine.templates import apply


class _Conn:
    def __init__(self, linked: bool):
        self.linked = linked

    def execute(self, *_a, **_k):
        linked = self.linked

        class _R:
            def first(self):
                return (1,) if linked else None
        return _R()


def test_required_and_not_linked_is_said(monkeypatch):
    monkeypatch.setattr(grants, "repo_guard_active", lambda org_id, db: True)
    out = apply.github_as_me(_Conn(False), org_id="o", person_id="usr_1", is_person=True)
    assert out == {"required": True, "linked": False, "href": "/account/agents"}
    assert apply.github_as_me(_Conn(True), org_id="o", person_id="usr_1", is_person=True)["linked"] is True


def test_not_required_on_a_single_person_install_or_for_a_key(monkeypatch):
    monkeypatch.setattr(grants, "repo_guard_active", lambda org_id, db: False)
    assert apply.github_as_me(_Conn(False), org_id="o", person_id="usr_1", is_person=True)["required"] is False
    monkeypatch.setattr(grants, "repo_guard_active", lambda org_id, db: True)
    assert apply.github_as_me(_Conn(False), org_id="o", person_id="sva_1", is_person=False)["required"] is False
