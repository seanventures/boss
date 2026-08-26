"""Whether a screen is actually WORKING — not just which requests it made.

The console test agent could tell you what a control fetched. It could not tell you whether the
page rendered, whether it was styled, whether it was showing an error, or whether it was
silently empty. The operator found an unstyled screen by looking at it, which is exactly the
job this was supposed to take off them.

So this is the other half: a set of properties every screen must satisfy, checkable without a
baseline and without a human. They are deliberately about being BROKEN rather than about being
pretty — a machine can prove a page is broken; it cannot prove a page is good.

THE PROPERTIES
--------------
1. It rendered its own root. A screen that does not identify itself did not render.
2. It is not showing an unhandled error. Framework crash text, a stack trace, a bare 500.
3. It is not SILENTLY empty. Empty is allowed; empty with no explanation is not, because that
   is indistinguishable from an outage — the confusion this console has shipped most often.
4. Every control has a name. A button with no accessible text cannot be used or described.
5. Every internal link goes somewhere real. Six nav entries once pointed at routes that 404ed.
6. The design tokens are actually in force. This is the check that catches "no style applied",
   which is otherwise invisible to a request recorder.
7. No console errors.
8. It answers inside its budget.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

#: A screen must answer in this long. Not a performance target — a brokenness threshold. v1's
#: 20-31s renders were not slow, they were failing.
TTFB_BUDGET_MS = 3_000

#: Text that means the framework fell over rather than the screen deciding to say something.
CRASH_MARKERS = (
    "application error",
    "a client-side exception",
    "internal server error",
    "unhandled runtime error",
    "traceback (most recent call last)",
    "cannot read properties of",
    "hydration failed",
    "text content does not match",
    "__next_error__",
)

#: The empty states this console considers legitimate — each explains itself. A screen may be
#: empty; it may not be empty and silent.
EXPLAINED_EMPTY = re.compile(
    r"(no |nothing |none )|not moved|could not reach|never |yet\b|has not|do not|does not",
    re.I,
)

Severity = str
BROKEN, SUSPECT, OK = "BROKEN", "SUSPECT", "OK"


@dataclass
class ScreenObservation:
    """What the driver saw. Deliberately plain data, so the rules below are pure."""

    path: str
    status: int = 200
    ttfb_ms: int = 0
    html: str = ""
    #: Elements carrying data-testid, as seen in the DOM.
    testids: list[str] = field(default_factory=list)
    #: (label, href) for every link; href is normalised to a path.
    links: list[tuple[str, str]] = field(default_factory=list)
    #: Accessible names of every button. An empty string means the button has no name.
    buttons: list[str] = field(default_factory=list)
    #: Text content of the main region, whitespace-collapsed.
    text: str = ""
    #: Browser console errors.
    console_errors: list[str] = field(default_factory=list)
    #: Computed styles sampled from the page: {property: value}.
    computed: dict[str, str] = field(default_factory=dict)
    #: Paths that were probed and their status, for the link check.
    link_status: dict[str, int] = field(default_factory=dict)


@dataclass
class Finding:
    severity: Severity
    rule: str
    detail: str

    def is_failure(self) -> bool:
        return self.severity == BROKEN


def check(obs: ScreenObservation, *, expect_testid: Optional[str] = None,
          budget_ms: int = TTFB_BUDGET_MS) -> list[Finding]:
    """Every rule, applied to one screen.

    Returns findings rather than a boolean, because "this screen is broken" is not actionable
    and "this screen has three buttons with no accessible name" is.
    """
    out: list[Finding] = []

    # 1. Did it answer at all?
    if obs.status >= 500:
        return [Finding(BROKEN, "http", f"{obs.path} returned {obs.status}")]
    if obs.status >= 400:
        return [Finding(BROKEN, "http", f"{obs.path} returned {obs.status} — no page to check")]

    # 2. Framework crash. Checked before anything else about content, because a crashed page
    #    can still contain plausible-looking markup from a partial render.
    lowered = obs.html.lower()
    for marker in CRASH_MARKERS:
        if marker in lowered:
            out.append(Finding(BROKEN, "crash", f"page contains {marker!r}"))
            break

    # 3. Did the screen render its own root?
    if expect_testid and expect_testid not in obs.testids:
        out.append(Finding(
            BROKEN, "not-rendered",
            f"expected {expect_testid!r} in the DOM; a screen that does not identify itself "
            "did not render",
        ))

    # 4. Silently empty. Empty is fine; empty with nothing to say is not, because it is
    #    indistinguishable from an outage.
    body = " ".join(obs.text.split())
    if len(body) < 40:
        out.append(Finding(
            BROKEN, "blank",
            f"only {len(body)} characters of text — a blank screen and a broken one look alike",
        ))
    elif not obs.links and not obs.buttons and not EXPLAINED_EMPTY.search(body):
        out.append(Finding(
            SUSPECT, "unexplained-empty",
            "no controls and no wording that explains why the screen is empty",
        ))

    # 5. Nameless controls.
    nameless = [i for i, name in enumerate(obs.buttons) if not name.strip()]
    if nameless:
        out.append(Finding(
            BROKEN, "nameless-control",
            f"{len(nameless)} button(s) with no accessible name — unusable and undescribable",
        ))

    # 6. Links that go nowhere. This is the "fake buttons" class, checked mechanically.
    dead = sorted(p for p, code in obs.link_status.items() if code >= 400)
    if dead:
        out.append(Finding(BROKEN, "dead-link", f"link(s) to {dead}"))

    # 7. Styling actually applied. The check that catches "no style", which a request recorder
    #    is blind to: if the canvas is still the browser default, no stylesheet is in force.
    background = (obs.computed.get("background-color") or "").replace(" ", "")
    if background in ("rgba(0,0,0,0)", "transparent", "rgb(255,255,255)", ""):
        out.append(Finding(
            BROKEN, "unstyled",
            f"page background is {background or 'unset'} — the stylesheet is not in force",
        ))
    # Both the prose font AND a heading, because a global h1..h6 rule from the console this
    # replaces wins inside the subtree by being a plain element selector on a later stylesheet.
    # Checking one element could never have caught that -- and did not.
    scope = obs.computed.get("scoped-to") or "root"
    for key, what in (("font-family", scope), ("heading-font-family", f"{scope} heading")):
        font = (obs.computed.get(key) or "").lower()
        if font and "grotesk" not in font:
            out.append(Finding(
                SUSPECT, "wrong-font",
                f"{what} font is {font!r}, not the console's Space Grotesk",
            ))

    # 8. Console errors.
    if obs.console_errors:
        out.append(Finding(
            SUSPECT, "console-error",
            f"{len(obs.console_errors)}: {obs.console_errors[0][:120]}",
        ))

    # 9. Budget. Not a performance target — v1's 20-31s renders were not slow, they were
    #    failing, and an operator cannot tell those apart from the outside.
    if obs.ttfb_ms > budget_ms:
        out.append(Finding(
            BROKEN if obs.ttfb_ms > budget_ms * 3 else SUSPECT,
            "slow",
            f"{obs.ttfb_ms}ms to first byte, budget {budget_ms}ms",
        ))

    return out or [Finding(OK, "ok", "no problems found")]


def summarise(results: dict[str, list[Finding]]) -> dict[str, Any]:
    """One verdict across every screen checked."""
    broken = {path: [f for f in fs if f.severity == BROKEN]
              for path, fs in results.items()}
    broken = {p: fs for p, fs in broken.items() if fs}
    suspect = {path: [f for f in fs if f.severity == SUSPECT]
               for path, fs in results.items()}
    suspect = {p: fs for p, fs in suspect.items() if fs}
    return {
        "screens": len(results),
        "broken": {p: [f.detail for f in fs] for p, fs in broken.items()},
        "suspect": {p: [f.detail for f in fs] for p, fs in suspect.items()},
        # A screen that merely LOOKS fine is not passing; it is un-refuted. The wording matters
        # because this runs without a human, and "passed" invites more confidence than it earns.
        "unrefuted": sorted(set(results) - set(broken) - set(suspect)),
        "passed": not broken,
    }


# --------------------------------------------------------------------------- #
# Does it match the reference?
#
# The checks above ask whether a screen WORKS. These ask whether it looks like the thing it is
# replacing — which the operator kept having to check by eye, screen by screen, and which is
# entirely mechanical once the reference's own numbers are written down.
#
# The type scale in style-spec.json was read out of the reference's rendered DOM. An earlier
# pass used rounded rem values that came out 15-60% large throughout: the screen title was
# 28.5px where the reference is 18px. That is the difference between "in the style of" and
# "the style", and it is invisible to every other check here.
# --------------------------------------------------------------------------- #

def check_style(measured: dict[str, Any], spec: dict[str, Any], *,
                path: str = "") -> list[Finding]:
    """Compare a screen's computed styles against the reference's own numbers.

    `measured` is {role: {"font_size": "...", "font_weight": "...", "font_family": "..."}} as
    read from the live DOM; roles missing from the page are skipped rather than failed, because
    not every screen has every element.
    """
    out: list[Finding] = []
    scale = spec.get("type_scale") or {}
    fonts = spec.get("fonts") or {}

    for role, want in scale.items():
        got = measured.get(role)
        if not got:
            continue
        want_size = str(want.get("font_size") or "")
        got_size = str(got.get("font_size") or "")
        if want_size and got_size and got_size != want_size:
            out.append(Finding(
                SUSPECT, "type-scale",
                f"{path}{role}: {got_size} where the reference is {want_size}",
            ))
        want_weight = str(want.get("font_weight") or "")
        got_weight = str(got.get("font_weight") or "")
        if want_weight and got_weight and got_weight != want_weight:
            out.append(Finding(
                SUSPECT, "type-weight",
                f"{path}{role}: weight {got_weight} where the reference is {want_weight}",
            ))

        family = str(got.get("font_family") or "").lower()
        display = str(fonts.get("display") or "").lower()
        mono = str(fonts.get("mono") or "").lower()
        if family and display and mono and display not in family and mono not in family:
            out.append(Finding(
                SUSPECT, "type-family",
                f"{path}{role}: {family[:40]!r} is neither the display nor the mono face",
            ))

    return out


def check_structure(html: str, testids: list[str], spec: dict[str, Any],
                    path: str) -> list[Finding]:
    """Structural expectations for a specific screen.

    Used where a screen has a SHAPE the reference cares about — the Hive is rings with halos
    and dashed curves, and an implementation of filled discs on one circle passes every other
    check in this file while looking like a different diagram.
    """
    rules = (spec.get("structure") or {}).get(path)
    if not rules:
        return []

    out: list[Finding] = []
    for prefix in rules.get("requires") or []:
        if not any(t.startswith(prefix) or t == prefix for t in testids):
            out.append(Finding(
                BROKEN, "missing-structure",
                f"{path}: nothing matching {prefix!r} — {rules.get('note', '')}",
            ))
    for marker in rules.get("svg_must_have") or []:
        if marker not in html:
            out.append(Finding(
                SUSPECT, "shape",
                f"{path}: no {marker!r} in the markup — {rules.get('note', '')}",
            ))
    return out
