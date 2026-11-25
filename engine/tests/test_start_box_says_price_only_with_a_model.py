"""The start box's price words need a connected model (stranger test r2, 2026-11-17).

The Command Center project card said "included in subscription or pay per use" on a fresh
install with no model connected. `start_project.model_connected` is Setup's own answer
(`connected_basics`), sent on the start plan so the card can leave the price words out.
Breaks if it stops reading connected_basics, or an unreadable answer fails the page.
"""
from __future__ import annotations

from boss_engine.services import setup_service, start_project


def test_model_connected_is_setups_answer(monkeypatch):
    monkeypatch.setattr(setup_service, "connected_basics", lambda c, s, p: {"model": False})
    assert start_project.model_connected(None, "p") is False
    monkeypatch.setattr(setup_service, "connected_basics", lambda c, s, p: {"model": True})
    assert start_project.model_connected(None, "p") is True


def test_an_unreadable_answer_is_unknown_not_an_error(monkeypatch):
    def boom(*_a):
        raise RuntimeError("db down")
    monkeypatch.setattr(setup_service, "connected_basics", boom)
    assert start_project.model_connected(None, "p") is None
