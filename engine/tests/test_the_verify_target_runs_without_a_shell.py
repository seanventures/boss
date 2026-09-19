"""The verification target has to be runnable by the thing that runs it.

A real build reached the sandbox and was refused:

    '[ -d node_modules ] || npm install … && npm run build && npm run typecheck && …' uses
    shell syntax, and sandbox commands run WITHOUT a shell. Operators like && || | ; > $ ( )
    are not interpreted — the command would run only up to the first one.

Two parts of the system, each right on its own. The sandbox refuses shell syntax deliberately:
an argv[0]-only check would have run `npm install` and silently skipped every test, reporting
a green verify. And the composer joined its steps with `&&` "so it fits the pipeline's
single-command model".

That model was never the contract. `test_commands` is a LIST, and the runner already does
`[sandbox.run(cmd) for cmd in spec.test_commands]` — which short-circuits the same way, since
the first failure is the one the repair stage sees.
"""
from __future__ import annotations

import pytest

from boss_engine.services.sandbox import _looks_like_shell
from boss_engine.services.verification_target import (
    PYTHON_TEST_CMD,
    WEB_TEST_COMMANDS,
    default_test_commands,
)

WEB_SPEC = {"kind": "web", "title": "A web app with a page"}


@pytest.mark.parametrize("command", WEB_TEST_COMMANDS + [PYTHON_TEST_CMD])
def test_no_default_command_needs_a_shell(command):
    assert not _looks_like_shell(command), command


def test_the_web_target_is_several_commands_not_one():
    commands = default_test_commands(WEB_SPEC, "A web app")

    assert len(commands) > 1
    assert all(not _looks_like_shell(c) for c in commands)


def test_install_comes_first_so_a_fresh_clone_can_build():
    """Without it `npm run build` exits 127 on a clean tree and the failure reads as the
    task's fault when it is the environment's."""
    commands = default_test_commands(WEB_SPEC, "A web app")

    assert commands[0].startswith("npm install")
    assert commands[1] == "npm run build"


def test_the_order_still_puts_the_cheapest_failure_first():
    """Compile before lint before e2e: the repair evidence should point at the earliest broken
    stage, which is what the `&&` chain was for."""
    commands = default_test_commands(WEB_SPEC, "A web app")

    assert commands.index("npm run build") < commands.index("npm run lint")
    assert commands.index("npm run lint") < commands.index("npm run test:e2e")


def test_a_python_target_is_unchanged():
    assert default_test_commands({"kind": "python"}, "A library") == [PYTHON_TEST_CMD]


def test_the_joined_string_is_display_only():
    """It still exists for the console, which shows the sequence as one line — but nothing may
    execute it, because the sandbox would refuse it and be right to."""
    import inspect

    from boss_engine.services import verification_target

    module_source = inspect.getsource(verification_target)
    at = module_source.index("WEB_TEST_CMD = ")
    assert "Display only" in module_source[at - 120:at]
    # And it is NOT what the default hands to the runner.
    assert verification_target.WEB_TEST_CMD not in default_test_commands(WEB_SPEC, "A web app")
