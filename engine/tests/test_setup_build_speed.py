"""Setup's Build speed row: the choice, what a build gets right now, and what the machine allows.

Balanced asks for 3 parallel sub-agents, Fastest for 6; the machine caps either. Same model and
same checks at every speed; only Owners and Admins change it.
"""
from __future__ import annotations

import contextlib
from types import SimpleNamespace

from boss_engine.identity.permissions_gen import PERMISSION_META
from boss_engine.services import project_settings, setup_view


class _Conn:
    def begin_nested(self):
        return contextlib.nullcontext()


def _view(monkeypatch, chosen: dict, machine: int):
    monkeypatch.setattr(project_settings, "get_all", lambda c, p: chosen)
    monkeypatch.setattr(project_settings, "effective",
                        lambda c, p, s, key: chosen.get(key, getattr(s, key, None)))
    monkeypatch.setattr(project_settings, "machine_parallelism", lambda: machine)
    return setup_view.build_speed(_Conn(), SimpleNamespace(build_speed="balanced",
                                                            build_parallelism=0), "boss-dogfood")


def test_the_default_is_balanced_with_three_at_once(monkeypatch):
    view = _view(monkeypatch, {}, machine=6)
    assert view == {"key": "build_speed", "mode": "balanced", "parallelism": 3, "machine": 6,
                    "asks": {"balanced": 3, "fastest": 6}, "source": "default",
                    "permission": "settings:update"}


def test_fastest_is_capped_by_the_machine_and_says_so(monkeypatch):
    view = _view(monkeypatch, {"build_speed": "fastest"}, machine=4)
    assert view["mode"] == "fastest" and view["source"] == "project"
    assert view["parallelism"] == 4 and view["machine"] == 4


def test_it_is_a_known_knob_only_owners_and_admins_change():
    assert project_settings.KNOBS["build_speed"]["choices"] == ("balanced", "fastest")
    assert set(PERMISSION_META["settings:update"].roles) == {"owner", "admin"}
