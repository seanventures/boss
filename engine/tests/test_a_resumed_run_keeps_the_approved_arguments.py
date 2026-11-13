"""A resumed run may execute only the arguments a person approved.

The decision stores a digest of the held call. Resume compares the pending
arguments to that digest and refuses when they differ. An older decision
with no digest still resumes.
"""
from __future__ import annotations

from boss_engine.services.agent_loop import resume_agent_run
from boss_engine.services.argument_binding import held_call_matches

from ._source import code_only

# sha256 of {"arguments":{"password":"hunter2","path":"a.py"},"tool":"write_file"}
_WRITE_FILE = "520fdccd4b173d629f76cc8d2c29a597b897e4edfb2fdf09f3e7b4151094a589"
_APPROVED = {"path": "a.py", "password": "hunter2"}


def test_a_different_path_is_not_the_approved_call():
    changed = {"path": "b.py", "password": "hunter2"}
    assert held_call_matches("write_file", changed, _WRITE_FILE) is False


def test_the_same_arguments_are_the_approved_call():
    assert held_call_matches("write_file", dict(_APPROVED), _WRITE_FILE) is True


def test_a_missing_or_empty_digest_matches():
    other = {"path": "b.py", "password": "hunter2"}
    assert held_call_matches("write_file", other, None) is True
    assert held_call_matches("write_file", other, "") is True
    assert held_call_matches("write_file", "not-a-dict", None) is True


def test_a_non_dict_does_not_match_when_a_digest_is_present():
    assert held_call_matches("write_file", "not-a-dict", _WRITE_FILE) is False
    assert held_call_matches("write_file", ["a.py"], _WRITE_FILE) is False
    assert held_call_matches("write_file", None, _WRITE_FILE) is False


def test_resume_selects_the_recommendation_and_checks_the_held_call_once():
    source = code_only(resume_agent_run)
    columns = source[source.index("SELECT"):source.index("FROM decisions")]
    assert "recommendation" in columns
    assert source.count("binding_holds(") == 1
    assert source.index("binding_holds(") < source.index('pending.get("kind")')
