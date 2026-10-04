"""One status per ability, by precedence (skills-mcp §5.2, test 9).

An MCP row used to show off/reachable/unreachable/not checked + risk + reviewed + boundary at
once, and nothing reduced it to one state with one next action. compute_status is the one
reduction; every state is reachable and exactly one wins.
"""
from __future__ import annotations

import itertools

import pytest

from boss_engine.services.kit import PRECEDENCE, compute_status

FLAGS = ("failing", "review", "needs_setup", "needs_repo", "disabled", "held", "tested")


def _status(**on):
    facts = {k: False for k in FLAGS}
    facts["held"] = True
    facts["tested"] = True
    facts.update(on)
    return compute_status(**facts)


@pytest.mark.parametrize("facts,expected", [
    ({}, "ready"),
    # READY MEANS IT PASSED A TEST: set up and held but never passed is not Ready.
    ({"tested": False}, "untested"),
    ({"held": False, "tested": False}, "unheld"),
    ({"held": False}, "unheld"),
    ({"disabled": True, "held": False}, "off"),
    ({"needs_repo": True, "disabled": True}, "needs_repo"),
    ({"needs_setup": True, "needs_repo": True}, "setup"),
    ({"review": True, "needs_setup": True}, "review"),
    ({"failing": True, "review": True, "needs_setup": True}, "failing"),
])
def test_precedence(facts, expected):
    assert _status(**facts) == expected


def test_every_combination_has_exactly_one_known_status():
    seen = set()
    for values in itertools.product((False, True), repeat=len(FLAGS)):
        status = compute_status(**dict(zip(FLAGS, values)))
        assert status in PRECEDENCE
        seen.add(status)
    # `testing` is the console's own (a test in flight); every other status is reachable.
    assert seen == set(PRECEDENCE) - {"testing"}
