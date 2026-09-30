"""Screen health checks — the half of the test agent that was missing.

The agent could tell you which requests a control made. It could not tell you whether the page
rendered, whether it was styled, whether it was showing an error, or whether it was silently
empty. The operator found an unstyled screen by looking at it, which is the job this was
supposed to take off them.

Every rule below is about being BROKEN rather than about being pretty. A machine can prove a
page is broken; it cannot prove a page is good, and pretending otherwise is how an automated
check becomes a rubber stamp.
"""

import pytest

from boss_engine.services.console_screen_check import (
    BROKEN,
    OK,
    SUSPECT,
    ScreenObservation,
    check,
    summarise,
)

STYLED = {"background-color": "rgb(9, 13, 23)", "font-family": '"Space Grotesk", sans-serif'}


def obs(**over) -> ScreenObservation:
    base = dict(
        path="/v2/factory",
        status=200,
        ttfb_ms=300,
        html="<section data-testid='v2-workforce'>...</section>",
        testids=["v2-workforce"],
        links=[("Projects", "/v2")],
        buttons=["ALL", "WORKING"],
        text="Workforce 21 agents last 24h Adversarial Reviewer no work in this window",
        console_errors=[],
        computed=dict(STYLED),
        link_status={"/v2": 200},
    )
    base.update(over)
    return ScreenObservation(**base)


def rules(findings):
    return {f.rule for f in findings}


# --------------------------------------------------------------------------- #
# The check that would have caught what the operator caught
# --------------------------------------------------------------------------- #
def test_an_unstyled_page_is_broken():
    """The operator spotted an unstyled screen by eye. A request recorder is blind to this: the
    page fetches exactly the same things whether or not a stylesheet loaded."""
    out = check(obs(computed={"background-color": "rgba(0, 0, 0, 0)"}), expect_testid="v2-workforce")
    assert "unstyled" in rules(out)
    assert any(f.severity == BROKEN for f in out)


def test_a_default_white_background_is_unstyled():
    out = check(obs(computed={"background-color": "rgb(255, 255, 255)"}))
    assert "unstyled" in rules(out)


def test_the_wrong_font_is_suspect_not_broken():
    """A fallback font means the webfont did not load. Worth saying; not worth failing a
    deploy over on its own."""
    out = check(obs(computed={**STYLED, "font-family": "Times New Roman"}))
    assert "wrong-font" in rules(out)
    assert not any(f.severity == BROKEN for f in out)


def test_a_correctly_styled_page_raises_nothing():
    assert check(obs(), expect_testid="v2-workforce") == [
        f for f in check(obs(), expect_testid="v2-workforce") if f.severity == OK
    ]


# --------------------------------------------------------------------------- #
# Rendered at all
# --------------------------------------------------------------------------- #
def test_a_screen_that_does_not_identify_itself_did_not_render():
    out = check(obs(testids=["v2-shell"]), expect_testid="v2-workforce")
    assert "not-rendered" in rules(out)


def test_a_500_stops_the_check_rather_than_reporting_style_problems():
    """Reporting six findings about a page that never loaded buries the one that matters."""
    out = check(obs(status=500, html="", text="", computed={}))
    assert len(out) == 1
    assert out[0].rule == "http"


@pytest.mark.parametrize("marker", [
    "Application error: a client-side exception has occurred",
    "Internal Server Error",
    "Traceback (most recent call last):",
    "Cannot read properties of undefined",
    "Hydration failed because the initial UI does not match",
])
def test_a_framework_crash_is_detected_in_the_markup(marker):
    """A crashed page can still contain plausible markup from a partial render, so this is
    checked before anything about content."""
    out = check(obs(html=f"<div>{marker}</div>"))
    assert "crash" in rules(out)


# --------------------------------------------------------------------------- #
# Empty vs broken — the confusion this console ships most often
# --------------------------------------------------------------------------- #
def test_a_blank_screen_is_broken():
    out = check(obs(text="", links=[], buttons=[]))
    assert "blank" in rules(out)


def test_an_empty_screen_that_explains_itself_is_fine():
    """Empty is allowed. Empty and silent is not."""
    out = check(obs(
        text="Workforce No agent roles are defined for this company yet.",
        links=[], buttons=[],
    ))
    assert "unexplained-empty" not in rules(out)


def test_an_empty_screen_with_nothing_to_say_is_flagged():
    out = check(obs(
        text="Workforce " + "x" * 60,
        links=[], buttons=[],
    ))
    assert "unexplained-empty" in rules(out)


# --------------------------------------------------------------------------- #
# Controls
# --------------------------------------------------------------------------- #
def test_a_button_with_no_accessible_name_is_broken():
    """It cannot be used by a screen reader, described in a bug report, or found by a test."""
    out = check(obs(buttons=["Deploy", "  ", ""]))
    assert "nameless-control" in rules(out)


def test_a_link_to_a_route_that_404s_is_broken():
    """The 'fake buttons' class, checked mechanically. Six nav entries once pointed at routes
    that did not exist."""
    out = check(obs(link_status={"/v2": 200, "/v2/ghost": 404}))
    assert "dead-link" in rules(out)
    assert "/v2/ghost" in [f.detail for f in out if f.rule == "dead-link"][0]


# --------------------------------------------------------------------------- #
# Time
# --------------------------------------------------------------------------- #
def test_a_slow_screen_is_suspect_and_a_very_slow_one_is_broken():
    """v1's 20-31s renders were not slow, they were failing, and an operator cannot tell those
    apart from the outside."""
    mild = check(obs(ttfb_ms=4_000))
    severe = check(obs(ttfb_ms=25_000))
    assert [f.severity for f in mild if f.rule == "slow"] == [SUSPECT]
    assert [f.severity for f in severe if f.rule == "slow"] == [BROKEN]


def test_console_errors_are_reported():
    out = check(obs(console_errors=["TypeError: x is not a function"]))
    assert "console-error" in rules(out)


# --------------------------------------------------------------------------- #
# The verdict
# --------------------------------------------------------------------------- #
def test_a_clean_run_says_unrefuted_rather_than_passed():
    """A screen that merely looks fine is not passing; it is un-refuted. This runs without a
    human, and "passed" invites more confidence than the check earns."""
    result = summarise({"/v2": check(obs())})
    assert result["passed"] is True
    assert result["unrefuted"] == ["/v2"]


def test_one_broken_screen_fails_the_run():
    result = summarise({
        "/v2": check(obs()),
        "/v2/factory": check(obs(status=500, html="", text="", computed={})),
    })
    assert result["passed"] is False
    assert "/v2/factory" in result["broken"]


def test_suspect_findings_do_not_fail_the_run_but_are_reported():
    """A run that fails on every wobble gets switched off."""
    result = summarise({"/v2": check(obs(ttfb_ms=4_000))})
    assert result["passed"] is True
    assert "/v2" in result["suspect"]


# --------------------------------------------------------------------------- #
# The probe has to look at the right element
#
# The first version read document.body and reported the wrong font on every screen —
# correctly, but about the wrong element. v2 scopes its styling to .v2-shell; body still
# belongs to the console being replaced. The check was measuring v1.
# --------------------------------------------------------------------------- #
def test_a_heading_in_the_wrong_font_is_caught():
    """A global h1..h6 rule from the other console wins inside the subtree by being a plain
    element selector on a later stylesheet. The prose was right and every heading was Sora, and
    checking a single element could never have shown it."""
    out = check(obs(computed={
        "background-color": "rgb(9, 13, 23)",
        "font-family": '"Space Grotesk", sans-serif',
        "heading-font-family": "Sora, Manrope, sans-serif",
        "scoped-to": ".v2-shell",
    }))
    fonts = [f for f in out if f.rule == "wrong-font"]
    assert len(fonts) == 1
    assert "heading" in fonts[0].detail


def test_both_fonts_correct_raises_nothing():
    out = check(obs(computed={
        "background-color": "rgb(9, 13, 23)",
        "font-family": '"Space Grotesk", sans-serif',
        "heading-font-family": '"Space Grotesk", sans-serif',
        "scoped-to": ".v2-shell",
    }))
    assert "wrong-font" not in rules(out)


def test_the_finding_says_which_element_it_measured():
    """"the font is wrong" is not actionable when two consoles share a page."""
    out = check(obs(computed={
        "background-color": "rgb(9, 13, 23)",
        "font-family": "Manrope",
        "heading-font-family": "Manrope",
        "scoped-to": ".v2-shell",
    }))
    details = " ".join(f.detail for f in out if f.rule == "wrong-font")
    assert ".v2-shell" in details


def test_the_driver_samples_the_app_root_not_the_document_body():
    from engine.tests._source import code_only
    from boss_engine.services import console_driver as cd

    src = code_only(cd.observe_screen)
    assert "querySelector('.v2-shell')" in src
    assert "h1, h2" in src


# --------------------------------------------------------------------------- #
# A structural rule set by a stylesheet must be READ from the computed style
#
# An earlier version grepped the HTML for "stroke-dasharray" and reported the Hive's edges as
# undashed while they were dashed — the rule lives in the CSS. A check that reports a problem
# which is not there trains the reader to ignore the output, which costs more than the check is
# worth.
# --------------------------------------------------------------------------- #
from boss_engine.services.console_screen_check import check_structure  # noqa: E402

HIVE_SPEC = {
    "structure": {
        "/v2/hive": {
            "note": "rings with halos and dashed curves",
            "requires": ["v2-hive-edge-"],
            "computed": [
                {"selector": "[data-testid^='v2-hive-edge-']", "property": "strokeDasharray",
                 "must_not_be": ["none", ""], "why": "edges are dashed"},
            ],
            "markup": [{"pattern": "Q", "in_attribute": "d", "why": "edges are curves"}],
        }
    }
}
EDGE_KEY = "[data-testid^='v2-hive-edge-']::strokeDasharray"


def test_a_dash_applied_by_css_is_not_reported_as_missing():
    findings = check_structure(
        '<path d="M0 0 Q10 10 20 0" data-testid="v2-hive-edge-a-b"></path>',
        ["v2-hive-edge-a-b"], HIVE_SPEC, "/v2/hive",
        computed={EDGE_KEY: "5px, 5px"},
    )
    assert findings == []


def test_an_undashed_edge_is_still_caught():
    findings = check_structure(
        '<path d="M0 0 Q10 10 20 0" data-testid="v2-hive-edge-a-b"></path>',
        ["v2-hive-edge-a-b"], HIVE_SPEC, "/v2/hive",
        computed={EDGE_KEY: "none"},
    )
    assert [f.rule for f in findings] == ["shape"]
    assert "dashed" in findings[0].detail


def test_a_straight_edge_is_caught_by_the_markup_rule():
    """The `d` attribute IS in the markup, so it is checked there."""
    findings = check_structure(
        '<path d="M0 0 L20 0" data-testid="v2-hive-edge-a-b"></path>',
        ["v2-hive-edge-a-b"], HIVE_SPEC, "/v2/hive",
        computed={EDGE_KEY: "5px, 5px"},
    )
    assert any("curves" in f.detail for f in findings)


def test_a_missing_element_is_still_broken():
    findings = check_structure("<svg></svg>", [], HIVE_SPEC, "/v2/hive", computed={})
    assert any(f.rule == "missing-structure" for f in findings)


def test_an_unmeasurable_rule_is_not_reported_as_a_failure():
    """No computed value means the probe found no element — which the `requires` rule already
    covers. Reporting it twice would double-count one problem."""
    findings = check_structure(
        '<path d="M0 0 Q1 1 2 0" data-testid="v2-hive-edge-a-b"></path>',
        ["v2-hive-edge-a-b"], HIVE_SPEC, "/v2/hive", computed={},
    )
    assert findings == []


def test_a_screen_with_no_structural_rules_is_left_alone():
    assert check_structure("<div></div>", [], HIVE_SPEC, "/v2/timeline") == []
