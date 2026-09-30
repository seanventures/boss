"""A confirmation guard must be passable by the person it is guarding.

An operator could not delete a retired target. The guard requires typing the app name back,
and the name was "BugBox v1.0 – Product" — an EN DASH (U+2013), which is not on a keyboard.
No matter what they typed, an exact match could never succeed. The placeholder was truncated
("type BugBox v1.0 – Pro"), so they could not even read the character they were failing to
reproduce.

The guard's own docstring already recorded this class of bug for a stray SPACE:

    "That guard is meant to stop an accidental removal, not to be unpassable: a target
     registered with a stray space in its name could never be confirmed, because the space is
     invisible in every place the name is shown."

The space was fixed. The dash — the same bug with a different invisible character — was not.
"""

import pytest

from apps.api.app.main import _same_name

EN_DASH_NAME = "BugBox v1.0 – Product"


@pytest.mark.parametrize("typed", [
    "BugBox v1.0 - Product",        # plain hyphen, what a keyboard produces
    "BugBox v1.0 – Product",   # the exact en dash
    "BugBox v1.0 — Product",   # em dash
    "bugbox v1.0 - product",        # case
    "  BugBox  v1.0 - Product  ",   # stray and repeated whitespace
    "BugBox v1.0 - Product",   # non-breaking space
])
def test_a_person_can_actually_confirm_the_name(typed):
    assert _same_name(typed, EN_DASH_NAME) is True


@pytest.mark.parametrize("typed", [
    "",
    "   ",
    "HookWatch",
    "BugBox",                       # a prefix is not the name
    "BugBox v2.0 - Product",        # one character of real difference
    None,
])
def test_it_still_refuses_anything_that_is_not_the_name(typed):
    assert _same_name(typed, EN_DASH_NAME) is False


def test_smart_quotes_are_normalised_too():
    assert _same_name("Living 'Colors'", "Living ‘Colors’") is True


def test_the_guard_is_used_by_both_destructive_paths():
    """Teardown and purge both destroy; a typeable guard on only one is half a fix."""
    import inspect

    from apps.api.app import main

    src = inspect.getsource(main)
    assert src.count("_same_name(payload.get(\"confirm\")") >= 2
