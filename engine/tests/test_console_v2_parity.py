"""The v1 -> v2 parity map must stay true to both consoles.

A fold from 31 areas to 9 is indistinguishable from a deletion unless the destination of every
area is written down. This map is that record; these tests keep it from rotting into fiction.
"""

import pathlib
import re

import pytest

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

REPO = pathlib.Path(__file__).resolve().parents[2]
PARITY = REPO / "docs/product/console_v2_parity.yaml"
PAGE = REPO / "apps/web/app/page.tsx"
SHELL = REPO / "apps/web/components/v2/Shell.tsx"

pytestmark = pytest.mark.skipif(yaml is None, reason="pyyaml not installed")


def _map() -> dict:
    return yaml.safe_load(PARITY.read_text(encoding="utf-8"))


#: `github`, `knowledge-graph` and `observability-suite` render their own <section data-area>
#: from app/<name>-ui.tsx. Reading page.tsx alone missed all three; `github` surfaced only
#: because a COMMENT there quoted the attribute, which is evidence of a mention, not of an
#: area. Scan the whole app directory, and strip line comments first so a mention can never
#: stand in for a rendered area again.
V1_SOURCES = sorted((REPO / "apps/web/app").glob("*.tsx"))

_COMMENT = re.compile(r"^\s*(//|\*|/\*).*$", re.MULTILINE)
#: Only literal attribute values are areas. `data-area={`${area}`}` and the regex fragments
#: that live in url-intent helpers are not.
_AREA = re.compile(r'data-area="([a-z0-9][a-z0-9-]*)"')


def _v1_areas() -> set[str]:
    areas: set[str] = set()
    for path in V1_SOURCES:
        text = _COMMENT.sub("", path.read_text(encoding="utf-8"))
        areas.update(_AREA.findall(text))
    return areas


def _v2_screens() -> set[str]:
    return set(re.findall(r'id:\s*"([^"]+)"', SHELL.read_text(encoding="utf-8")))


def _entries(m: dict) -> dict:
    return {k: v for k, v in m.items()
            if isinstance(v, dict) and "to" in v}


def test_every_v1_area_has_a_recorded_destination():
    """An area with no entry is the failure mode this file exists to prevent: it disappears in
    the fold and nobody can tell whether that was a decision."""
    missing = _v1_areas() - set(_entries(_map()))
    assert not missing, f"v1 areas with no parity entry: {sorted(missing)}"


def test_every_destination_is_a_screen_that_exists():
    """A map pointing at a screen v2 does not have would pass review and fail an operator."""
    screens = _v2_screens()
    bad = {area: e["to"] for area, e in _entries(_map()).items()
           if e["to"] is not None and e["to"] not in screens}
    assert not bad, f"destinations that are not v2 screens: {bad}"


def test_dropping_an_area_requires_a_reason():
    """"We decided" and "we forgot" look identical in a diff six months later."""
    for area, e in _entries(_map()).items():
        if e["to"] is None:
            assert str(e.get("what", "")).strip(), f"{area} is dropped with no reason given"


def test_the_five_core_areas_are_carried_over():
    """Named explicitly as must-not-regress: discovery, meetings, coding tasks, ci/cd,
    monitoring. None of them may be dropped, whatever else the fold does."""
    entries = _entries(_map())
    for area in ("discovery", "meetings", "code_tasks", "delivery", "monitoring"):
        assert area in entries, f"{area} has no parity entry"
        assert entries[area]["to"] is not None, f"{area} must not be dropped"


def test_the_core_areas_say_what_would_be_lost():
    """A destination alone does not help the person checking the fold. The risk line is what
    tells them where to look."""
    entries = _entries(_map())
    for area in ("discovery", "meetings", "code_tasks", "delivery", "monitoring"):
        risk = str(entries[area].get("risk", "")).strip()
        assert len(risk) > 40, f"{area} needs a specific risk, got {risk!r}"


def test_no_v2_screen_is_left_with_nothing_to_show():
    """A screen in the nav that nothing maps to renders an empty page, which reads as broken.
    Settings and Hive may legitimately be new, so they are exempt."""
    targets = {e["to"] for e in _entries(_map()).values() if e["to"]}
    orphans = _v2_screens() - targets - {"settings", "hive"}
    assert not orphans, f"v2 screens nothing maps to: {sorted(orphans)}"


def test_cross_cutting_features_are_tracked_separately():
    """Ask BOSS and the console toggle belong to no single area, so an area-keyed map would
    lose them silently."""
    ids = {c["id"] for c in _map().get("cross_cutting", [])}
    assert {"ask-boss", "console-toggle", "org-scope"} <= ids
