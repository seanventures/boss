"""Say what is actually wrong with the command.

A build died with:

    Executable is not allowlisted: [

`[` is the shell's `test` builtin. The command had been written as shell — something like
"[ -d node_modules ] && npm ci" — and sandbox.run does shlex.split + subprocess.run WITHOUT a
shell, so the FIRST WORD became the executable. Neither the allowlist nor `[` was the problem,
and the message sent whoever read it to look at both.

Worse than the message: an argv[0]-only check PASSES "npm run build && npm test", then runs
only "npm run build" with the rest as arguments — a verify that reports success having never
run the tests.
"""

import pytest

from boss_engine.services.sandbox import _looks_like_shell


@pytest.mark.parametrize("command", [
    "[ -d node_modules ] && npm ci",
    "npm run build && npm test",
    "npm run lint || true",
    "npm test | tee out.log",
    "npm run build; npm test",
    "echo $(date)",
    "npm test > results.txt",
])
def test_shell_syntax_is_recognised(command):
    assert _looks_like_shell(command) is True


@pytest.mark.parametrize("command", [
    "npm run verify",
    "pytest -q",
    "./gradlew test",
    "npm ci",
    "python -m pytest tests/",
])
def test_a_plain_command_is_not_flagged(command):
    assert _looks_like_shell(command) is False


def test_the_whole_command_is_checked_not_just_argv0():
    """'npm run build && npm test' has a fine argv[0] and must still be rejected."""
    import inspect

    from boss_engine.services import sandbox

    src = inspect.getsource(sandbox.EphemeralSandbox.run)
    assert "_looks_like_shell(command)" in src
    assert src.index("_looks_like_shell(command)") < src.index("_assert_allowed_executable"), (
        "checking argv[0] first would let a && command through")


def test_the_message_names_the_real_problem():
    from boss_engine.services import sandbox

    with pytest.raises(sandbox.SandboxError) as err:
        sandbox.EphemeralSandbox.run(
            type("S", (), {"_require_provisioned": lambda self: None})(),
            "[ -d node_modules ] && npm ci")
    assert "without a shell" in str(err.value).lower()


@pytest.mark.parametrize("command", [
    "python3 -c 'print(42)'",          # parens inside a QUOTED argument
    'python -c "import sys; sys.exit(0)"',
    "npm run test -- --grep 'a|b'",    # a pipe inside a quoted regex
    "sh -c 'echo hi'",
])
def test_a_metacharacter_inside_a_quoted_argument_is_just_text(command):
    """Scanning the raw string rejected `python3 -c 'print(42)'` and broke two suites.

    shlex knows what is quoted. An operator that survives tokenising as its own word is a real
    operator; one inside a quoted string is an argument.
    """
    assert _looks_like_shell(command) is False
