"""The screen checker must test the console the operator actually opens.

`V2_SCREENS` listed only `/v2/*`. Run against a live deployment whose console at `/` was
unusable — 9.8s to first byte against a 3s budget, 42 buttons with no accessible name, and
every client feed refused by CORS — it reported **9 screens checked, 0 broken**. A checker
that is green while the product is broken is worse than no checker: it is evidence for the
wrong conclusion.

`/` is what a bookmark, a share link and every "View in …" button land on.
"""

from __future__ import annotations

from boss_engine.cli.console_test import V2_SCREENS


def test_the_root_console_is_in_the_default_sweep() -> None:
    assert "/" in V2_SCREENS, (
        "the area-based console at / is the page people open; a sweep without it can pass "
        "while the console is unusable"
    )


def test_the_root_screen_expects_no_v2_test_id() -> None:
    """`/` is the v1 shell. Demanding a v2 testid there would be a permanent false failure,
    and the usual way that ends is someone deleting the row rather than the assertion."""
    assert V2_SCREENS["/"] is None


def test_the_v2_screens_are_still_covered() -> None:
    """Adding `/` must not quietly narrow what was already checked."""
    for path in ("/v2", "/v2/hive", "/v2/factory", "/v2/org", "/v2/panels",
                 "/v2/timeline", "/v2/boss", "/v2/infra", "/v2/settings"):
        assert path in V2_SCREENS
    assert V2_SCREENS["/v2"] == "v2-projects"


def test_every_other_screen_still_asserts_its_identity() -> None:
    """The testid check is what proves the right page rendered rather than a redirect or an
    error shell. `/` is the only entry allowed to opt out."""
    missing = [p for p, tid in V2_SCREENS.items() if tid is None and p != "/"]
    assert not missing, f"screens with no identity assertion: {missing}"
