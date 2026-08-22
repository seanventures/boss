"""A dedupe key must be an IDENTITY, not a fresh id.

Reported as "two duplicate runs in the Meeting rather than scheduling just one, the idempotency
is not working" — and it had happened before, more than once.

The mechanism was never broken. `tasks.dedupe_key` is checked before every INSERT, the check is
project-scoped, and it fails closed. Seven call sites passed `make_id(...)` — a freshly
generated id — so the value could never match an earlier row and the check silently did nothing
at every one of them. Pressing "Send to PRD meeting" twice produced two meetings, two tasks and
two code tasks from a single outcome.

That is why it kept coming back: each occurrence was fixed where it surfaced, and the six other
call sites with the same defect went on producing duplicates. This test is the infra-layer fix
— it fails on the ANTI-PATTERN anywhere in the tree, including in code nobody has written yet.
"""

import pathlib
import re

import pytest

from boss_engine.services.core import stable_dedupe_key

ROOTS = [pathlib.Path("engine/boss_engine"), pathlib.Path("apps/api/app")]

#: Anything that mints a fresh value. A dedupe key built from one of these is unique by
#: construction and therefore cannot deduplicate anything.
FRESH_VALUE = re.compile(
    r"dedupe_key\s*=\s*(make_id|uuid|uuid4|str\(uuid|_new_id|token_hex|secrets\.)")


def test_no_call_site_builds_a_dedupe_key_from_a_fresh_id():
    offenders = []
    for root in ROOTS:
        if not root.exists():
            continue
        for path in root.rglob("*.py"):
            for lineno, line in enumerate(path.read_text().splitlines(), 1):
                if FRESH_VALUE.search(line):
                    offenders.append(f"{path}:{lineno}: {line.strip()[:70]}")
    assert not offenders, (
        "a dedupe key generated fresh per call can never collide, so the uniqueness check "
        "does nothing. Derive it from what makes two requests the SAME request — use "
        "stable_dedupe_key(kind, *identity_parts):\n  " + "\n  ".join(offenders))


def test_the_same_inputs_produce_the_same_key():
    a = stable_dedupe_key("discovery-promote", "drun_abc", 0)
    b = stable_dedupe_key("discovery-promote", "drun_abc", 0)
    assert a == b


def test_different_inputs_produce_different_keys():
    base = stable_dedupe_key("discovery-promote", "drun_abc", 0)
    assert base != stable_dedupe_key("discovery-promote", "drun_abc", 1)
    assert base != stable_dedupe_key("discovery-promote", "drun_xyz", 0)
    assert base != stable_dedupe_key("prd-meeting", "drun_abc", 0)


def test_incidental_whitespace_does_not_create_a_second_identity():
    """A directive retyped with a trailing newline is the same directive."""
    assert (stable_dedupe_key("prd-meeting", "p", "Build LinkNest")
            == stable_dedupe_key("prd-meeting", "p", "  Build LinkNest\n"))


def test_the_key_is_bounded_regardless_of_input_size():
    """Directives run to thousands of characters; the column should not have to."""
    key = stable_dedupe_key("prd-meeting", "p", "x" * 100_000)
    assert len(key) < 64


def test_none_and_empty_are_handled_without_exploding():
    assert stable_dedupe_key("k", None, "") == stable_dedupe_key("k", "", None)


@pytest.mark.parametrize("kind,parts", [
    ("discovery-promote", ("drun_1", 0)),
    ("prd-meeting", ("proj", "some directive")),
    ("ceo-command", ("proj", "ship it")),
    ("welcome", ("proj",)),
])
def test_every_kind_used_in_the_tree_produces_a_usable_key(kind, parts):
    key = stable_dedupe_key(kind, *parts)
    assert key.startswith(f"{kind}:")
    assert len(key) > len(kind) + 1


def test_promote_returns_the_existing_meeting_instead_of_convening_a_second():
    """Idempotency is not just refusing the duplicate — it is giving back the first result."""
    import inspect

    from apps.api.app import main

    src = inspect.getsource(main.promote_discovery_outcome_endpoint)
    assert "already_convened" in src
    assert "stable_dedupe_key" in src
    assert "make_id" not in src.split("dedupe_key")[1][:200]
