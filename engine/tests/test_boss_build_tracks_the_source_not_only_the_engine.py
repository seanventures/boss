"""BOSS's build id follows its stamped source and its local stack, not only the engine's files
(BOSSAPP-571 F5): a fix in apps/, scripts/ or packages/ is an update, and a parked step resumes
on the build that holds its fix, never on an unrelated engine change."""
from __future__ import annotations

from boss_engine.services import step_recovery as sr

SOURCE = "a" * 40
STACK = "b" * 40


def _fresh(monkeypatch, **env):
    for key in ("BOSS_SOURCE_SHA", "BOSS_STACK_HEAD"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(sr, "_BUILD_ID", None)
    return sr.boss_build()


def test_a_stamped_source_names_the_source_and_the_stack(monkeypatch):
    from boss_engine import __version__

    assert _fresh(monkeypatch, BOSS_SOURCE_SHA=SOURCE) == f"{__version__}+{SOURCE[:12]}.{SOURCE[:12]}"
    assert _fresh(monkeypatch, BOSS_SOURCE_SHA=SOURCE, BOSS_STACK_HEAD=STACK) == \
        f"{__version__}+{SOURCE[:12]}.{STACK[:12]}"


def test_a_new_stack_head_is_an_update_for_a_parked_step(monkeypatch):
    before = _fresh(monkeypatch, BOSS_SOURCE_SHA=SOURCE)
    handle = {"then": sr.THEN_BOSS_UPDATE, "boss_build": before}
    assert not sr.due(handle)
    _fresh(monkeypatch, BOSS_SOURCE_SHA=SOURCE, BOSS_STACK_HEAD=STACK)
    assert sr.due(handle)


def test_without_a_stamp_the_engine_fingerprint_still_answers(monkeypatch):
    built = _fresh(monkeypatch, BOSS_SOURCE_SHA="unknown")
    assert "+" in built and "." not in built.split("+")[1]
