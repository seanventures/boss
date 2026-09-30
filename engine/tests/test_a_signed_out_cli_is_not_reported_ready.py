""""Not logged in" contains "logged in", and BOSS believed it.

Measured on the live deployment, not imagined. `GET /v1/setup/coding-agents` returned, for a
Codex CLI that was installed but signed out:

    "authenticated": true,
    "auth_detail": "Not logged in",
    "ready": true,
    "token_present": false

The spec's `auth_ok_pattern` was `(?i)logged\\s+in`, and the CLI's denial contains its own
affirmative as a substring. Every field needed to see the contradiction was right there in the
same response, and the one field anything downstream actually reads — `ready` — said yes.

What that costs: BOSS routes real work to a CLI that cannot do it, and the failure arrives
later, inside a build, looking like the model was bad rather than like nobody was logged in.
Detection exists precisely to turn that into a specific next command, so detection that lies is
worse than no detection.

The guard is a default on the spec rather than a fix to one pattern, because every CLI phrases
a denial this way and the next spec would otherwise inherit the same trap.
"""
from __future__ import annotations

import re

import pytest

from boss_engine.services.cli_harness import BUILTIN_CLI_HARNESSES, CliHarnessSpec


def _verdict(spec: CliHarnessSpec, output: str) -> bool:
    """The same two-step decision `detect()` makes: denial first, then affirmation."""
    denied = bool(spec.auth_fail_pattern) and bool(re.search(spec.auth_fail_pattern, output))
    return (not denied and bool(spec.auth_ok_pattern)
            and bool(re.search(spec.auth_ok_pattern, output)))


@pytest.mark.parametrize("denial", [
    "Not logged in",                      # captured from codex-cli 0.154.0 on the deployment
    "not logged in",
    "Not  logged  in",
    "You are not authenticated",
    "not signed in",
    "Logged out",
    "Please log in to continue",
    "Please sign in",
])
def test_a_denial_is_never_read_as_a_sign_in(denial: str) -> None:
    for name, spec in BUILTIN_CLI_HARNESSES.items():
        if not spec.auth_ok_pattern:
            continue
        assert not _verdict(spec, denial), f"{name} read {denial!r} as signed in"


@pytest.mark.parametrize("affirmation", [
    "Logged in using ChatGPT",
    "logged in as someone@example.com",
    "Authenticated",
])
def test_a_real_sign_in_still_registers(affirmation: str) -> None:
    """The guard must not make every CLI look signed out, which would be the opposite bug."""
    codex = BUILTIN_CLI_HARNESSES["codex"]
    cursor = BUILTIN_CLI_HARNESSES["cursor"]

    assert _verdict(codex, affirmation) or _verdict(cursor, affirmation)


def test_every_spec_that_checks_auth_also_guards_the_denial() -> None:
    """A spec with a positive test and no negative one is the bug waiting to happen again."""
    unguarded = sorted(
        name for name, spec in BUILTIN_CLI_HARNESSES.items()
        if spec.auth_ok_pattern and not spec.auth_fail_pattern
    )
    assert not unguarded, f"these can read a denial as a sign-in: {unguarded}"


def test_the_captured_deployment_output_now_reads_as_signed_out() -> None:
    """The exact string the deployment returned, pinned so this cannot silently come back."""
    codex = BUILTIN_CLI_HARNESSES["codex"]

    assert re.search(codex.auth_ok_pattern, "Not logged in"), (
        "precondition: the affirmative pattern still matches the denial — which is WHY the "
        "negative check has to exist and win")
    assert not _verdict(codex, "Not logged in")


# --- which line gets reported -----------------------------------------------


def test_auth_detail_reports_the_status_not_a_warning() -> None:
    """These CLIs print warnings before their answer, and the doc keys troubleshooting on this.

    Measured: a fresh CODEX_HOME makes codex emit "WARNING: proceeding, even though we could
    not create PATH aliases…" ahead of the login status. Reporting the first line handed the
    operator a sentence about symlinks while they were debugging a login.
    """
    from boss_engine.services.cli_harness import _auth_detail

    codex = BUILTIN_CLI_HARNESSES["codex"]
    warned = ("WARNING: proceeding, even though we could not create PATH aliases\n"
              "Logged in using ChatGPT")

    assert _auth_detail(codex, warned) == "Logged in using ChatGPT"


def test_a_denial_behind_a_warning_is_still_what_gets_reported() -> None:
    from boss_engine.services.cli_harness import _auth_detail

    codex = BUILTIN_CLI_HARNESSES["codex"]

    assert _auth_detail(codex, "WARNING: noise\nNot logged in") == "Not logged in"


def test_a_warning_alone_is_better_than_nothing() -> None:
    """When the CLI says only that, it is the whole answer available."""
    from boss_engine.services.cli_harness import _auth_detail

    codex = BUILTIN_CLI_HARNESSES["codex"]

    assert _auth_detail(codex, "WARNING: only a warning").startswith("WARNING")
    assert _auth_detail(codex, "") == ""
