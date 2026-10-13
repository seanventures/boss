"""Setup's Build speed row: the choice, what a build gets right now, and what the machine allows.

A build writes in ONE session at either speed (build_streams: the split is off unless
build_streams_max opts in), so the row never promises parallel agents a build will not run. Same model and
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


def test_the_default_is_balanced_with_one_session(monkeypatch):
    view = _view(monkeypatch, {}, machine=6)
    assert view == {"key": "build_speed", "mode": "balanced", "parallelism": 1, "machine": 6,
                    "asks": {"balanced": 1, "fastest": 1}, "source": "default",
                    "permission": "settings:update"}


def test_fastest_alone_does_not_split_a_build(monkeypatch):
    view = _view(monkeypatch, {"build_speed": "fastest"}, machine=4)
    assert view["mode"] == "fastest" and view["source"] == "project"
    assert view["parallelism"] == 1


def test_an_explicit_stream_max_opts_in_and_the_machine_caps_it(monkeypatch):
    view = _view(monkeypatch, {"build_streams_max": 6}, machine=4)
    assert view["parallelism"] == 4


def test_it_is_a_known_knob_only_owners_and_admins_change():
    assert project_settings.KNOBS["build_speed"]["choices"] == ("balanced", "fastest")
    assert set(PERMISSION_META["settings:update"].roles) == {"owner", "admin"}
