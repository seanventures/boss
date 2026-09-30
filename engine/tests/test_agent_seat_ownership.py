"""A process is safe to kill because of who started it and why — never because of its name.

The URL-shortener build died with

    claude code agent exited -9: [substrate=local_subprocess]

BOSS killed its own agent. `_kill_stray_review_processes` reaps review seats that outlive their
deadline by scanning `ps` for a claude binary under a sandbox root — but a review seat and an
IMPLEMENT agent are the same binary under the same root. It cannot tell them apart, so it
killed a build that was working.

Its own docstring records the same bug twice before, each time patched by tightening the
pattern: first it matched "any command line containing claude", which killed the checking
process itself; then it required a real path component. The pattern was never the problem.
"""

import inspect

from boss_engine.services import code_agent, governed_code_task_service
from boss_engine.services.code_agent import (
    is_protected, register_seat, release_seat, seat_of,
)


def test_an_implement_agent_is_never_reaped():
    register_seat(90001, "agent")
    try:
        assert is_protected(90001) is True
    finally:
        release_seat(90001)


def test_a_review_seat_is_reapable_because_that_is_the_whole_point():
    register_seat(90002, "review")
    try:
        assert is_protected(90002) is False
    finally:
        release_seat(90002)


def test_research_is_protected_too():
    """Research writes nothing, but killing it mid-sweep loses the dossier and the spend."""
    register_seat(90003, "research")
    try:
        assert is_protected(90003) is True
    finally:
        release_seat(90003)


def test_a_process_we_did_not_start_is_not_claimed():
    assert seat_of(90404) is None
    assert is_protected(90404) is False


def test_a_finished_seat_stops_being_protected():
    register_seat(90005, "agent")
    release_seat(90005)
    assert is_protected(90005) is False


def test_the_reaper_consults_ownership_before_signalling():
    src = inspect.getsource(governed_code_task_service._kill_stray_review_processes)
    assert "_is_protected_seat" in src
    guard = src.index("_is_protected_seat")
    kill = src.index("os.kill")
    assert guard < kill, "the ownership check must come BEFORE the signal"


def test_the_seat_is_decided_by_whether_the_role_writes():
    """read_only=True is the review seat; anything that can write is an agent."""
    src = inspect.getsource(code_agent.ClaudeCodeAgent.run_role)
    assert 'seat=("review" if read_only else "agent")' in src


def test_the_child_pid_is_registered_and_always_released():
    src = inspect.getsource(code_agent.ClaudeCodeAgent._invoke)
    assert "register_seat(child.pid, seat)" in src
    assert "finally:" in src and "release_seat(child.pid)" in src, (
        "a seat that leaks stays protected forever and the reaper stops working")
