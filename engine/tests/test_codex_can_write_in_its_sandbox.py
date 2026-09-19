""""Test author produced no test files even after escalating to a frontier model."

The QA stage's first pass was codex: 1 turn, ZERO tokens, 15 seconds, exit 0, nothing written.
Run by hand with the same flags, it said exactly what was wrong and still exited 0:

    I couldn't write `test_calc.py` because the environment blocked file operations.

`--sandbox workspace-write` makes codex enforce its own boundary with kernel facilities
(landlock/seccomp) that a container does not grant it, so the guard fails closed and every
edit is refused. Measured, same prompt, same container:

    workspace-write      -> no file, exit 0, "the environment blocked file operations"
    danger-full-access   -> file written, pytest run, 1 passed

So codex was the only harness held to a second boundary, and the only one that could not do
its job. The isolation is BOSS's to provide — an ephemeral clone, in a container, with a
denied-by-default environment — which is what that flag's own help means by "environments that
are externally sandboxed".
"""
from __future__ import annotations

import inspect

from boss_engine.services.cli_harness import BUILTIN_CLI_HARNESSES

CODEX = BUILTIN_CLI_HARNESSES["codex"]


def test_codex_can_write_when_it_is_asked_to_write():
    assert CODEX.write_args == ("--sandbox", "danger-full-access")


def test_but_a_reviewer_still_cannot():
    """"The reviewer cannot edit" is an invariant of the governed pipeline, and read-only does
    not need the kernel facilities the write path was missing."""
    assert CODEX.read_only_args == ("--sandbox", "read-only")
    assert CODEX.supports_read_only() is True


def test_the_two_modes_are_never_combined():
    """They are the same flag with different values. Appending one to the other would leave
    two copies and let argument order decide whether a reviewer can edit."""
    assert CODEX.write_args[0] == CODEX.read_only_args[0] == "--sandbox"
    assert CODEX.write_args[1] != CODEX.read_only_args[1]


def test_the_reason_is_written_down_where_the_flag_is():
    """A flag named `danger-full-access` will be "corrected" by the next reader unless the
    measurement that forced it is beside it."""
    source = inspect.getsource(BUILTIN_CLI_HARNESSES["codex"].__class__)
    module = inspect.getsource(__import__("boss_engine.services.cli_harness",
                                          fromlist=["x"]))
    at = module.index('write_args=("--sandbox", "danger-full-access")')
    context = module[at - 1400:at]
    assert "externally sandboxed" in context
    assert "blocked file operations" in context


def test_an_operator_can_still_put_it_back():
    """Every spec field is overridable through BOSS_CLI_HARNESSES_JSON, so an install that
    gives its container the kernel facilities codex wants can have the tighter boundary."""
    from boss_engine.services.cli_harness import CliHarnessSpec

    assert "write_args" in CliHarnessSpec.__dataclass_fields__
