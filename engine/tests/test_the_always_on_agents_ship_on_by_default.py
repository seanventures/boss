"""The always-on agents ship ON (operator, 2026-11-09; rc10.11–rc10.12 shipped them off).

* BOSS_AMBIENT_COS unset and no mode chosen in the console → route: the Chief of Staff triages
  every signal and routes it to its squad, which acts within its autonomy level. Decisions on
  the Where people decide list still wait for a person.
* Off stays one choice away: BOSS_AMBIENT_COS=off, or Off in Govern, always wins.
* The signal sweep (BOSS_AMBIENT_SIGNALS) and the learning tick (BOSS_AMBIENT_LEARNING) follow
  the mode when unset, and an explicit 1/0 wins either way.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from boss_engine.config import Settings
from boss_engine.services.ambient import chief_of_staff, policy


def _settings(**env: str) -> Settings:
    return Settings(_env_file=None, **{k: v for k, v in env.items()})


def test_an_install_with_nothing_set_runs_the_always_on_agents(monkeypatch):
    for name in ("BOSS_AMBIENT_COS", "BOSS_AMBIENT_SIGNALS", "BOSS_AMBIENT_LEARNING"):
        monkeypatch.delenv(name, raising=False)
    s = _settings()
    assert policy.DEFAULT_COS_MODE == "route"
    assert chief_of_staff.mode_of(s) == "route"
    assert policy.signals_on(s)
    assert policy.learning_on(s)


def test_off_is_still_one_choice_away(monkeypatch):
    monkeypatch.setenv("BOSS_AMBIENT_COS", "off")
    monkeypatch.delenv("BOSS_AMBIENT_SIGNALS", raising=False)
    monkeypatch.delenv("BOSS_AMBIENT_LEARNING", raising=False)
    s = _settings()
    assert chief_of_staff.mode_of(s) == "off"
    assert not policy.signals_on(s) and not policy.learning_on(s)


@pytest.mark.parametrize("mode", ["propose", "route"])
def test_turning_the_chief_of_staff_on_turns_the_sweep_and_learning_on(monkeypatch, mode):
    monkeypatch.setenv("BOSS_AMBIENT_COS", mode)
    monkeypatch.delenv("BOSS_AMBIENT_SIGNALS", raising=False)
    monkeypatch.delenv("BOSS_AMBIENT_LEARNING", raising=False)
    s = _settings()
    assert chief_of_staff.mode_of(s) == mode
    assert policy.signals_on(s) and policy.learning_on(s)


@pytest.mark.parametrize("value,expected", [("1", True), ("true", True), ("0", False),
                                            ("", None), (None, None)])
def test_an_explicit_switch_wins_over_the_mode(value, expected):
    for mode in ("off", "propose"):
        s = SimpleNamespace(ambient_cos_mode=mode, ambient_signals=value, ambient_learning=value)
        follows = mode != "off"
        want = follows if expected is None else expected
        assert policy.signals_on(s) is want
        assert policy.learning_on(s) is want


def test_an_unknown_mode_is_the_safe_acting_one():
    assert chief_of_staff.mode_of(SimpleNamespace(ambient_cos_mode="maybe")) == "propose"
