"""Every app built from a PRD was stamped with the SAME app_id.

`stable_app_id` hashes the app's NAME, and `project_name` read the document type as the name:
`BOILERPLATE` is anchored at ^, and the pipeline titles its PRD runs
"📄 Product Requirements Document", so the leading emoji made the check miss.

The console unions runs on `app:${app_id}` — any shared token means the same app — so every
PRD-built app collapsed into one card. Reported as: click "[latency-probe] add a formatBytes
helper", press Ask BOSS, and the header becomes "Database Schema Drift Detector", with four
unrelated runs listed beneath it.

The renderer already had `unadorn()` for exactly this, with a comment explaining that emoji are
decoration and must not change how a title is read. This file never got it — and here the
consequence is not a wrong label but a wrong identity, written to the database at creation.
"""

from __future__ import annotations

import pytest

from boss_engine.services.naming import project_name, stable_app_id

PRD = "📄 Product Requirements Document"
PRD_FOLLOW_UP = "Follow-up: deferred review findings — 📄 Product Requirements Document"


def _app_id(title: str) -> str:
    return stable_app_id("boss-dogfood", "example-user/boss-sandbox", title)


def test_two_different_apps_built_from_a_prd_do_not_share_an_id():
    """THE BUG. Both titles are document types, so neither may claim an identity — and above
    all they must not claim the SAME one."""
    assert _app_id(PRD) == ""
    assert _app_id(PRD_FOLLOW_UP) == ""


def test_a_document_type_is_never_read_as_a_product_name():
    assert project_name(PRD)[0] == "Untitled app"
    assert project_name(PRD_FOLLOW_UP)[0] == "Untitled app"


@pytest.mark.parametrize("title", [
    "📄 Product Requirements Document",
    "📝 PRD",
    "# Design",
    "  •  Specification ",
    "Overview",
])
def test_decoration_does_not_smuggle_boilerplate_past_the_gate(title):
    """Anchored patterns and decorated titles are how this class of bug happens. A bullet, a
    hash, an emoji, a variation selector — none of them change what the words say."""
    assert project_name(title)[0] == "Untitled app", title
    assert _app_id(title) == ""


def test_a_real_name_still_gets_a_stable_identity():
    """The whole point of app_id: two runs of the same app must group."""
    assert _app_id("Database Schema Drift Detector") != ""
    assert _app_id("Database Schema Drift Detector") == _app_id("Database Schema Drift Detector")


def test_different_apps_still_get_different_identities():
    a = _app_id("Access-Review Helper")
    b = _app_id("Database Schema Drift Detector")
    assert a and b and a != b


def test_a_name_behind_the_boilerplate_is_still_found():
    """The gate must reject document TYPES, not titles that merely mention one. "PRD: BugBox"
    names a product; refusing it would trade one grouping bug for another."""
    assert project_name("PRD: BugBox — a bug tracker")[0] == "BugBox"
    assert _app_id("PRD: BugBox — a bug tracker") != ""


def test_the_label_is_still_lifted_off_a_follow_up():
    """Stripping the name must not cost the run its badge."""
    _name, _blurb, label = project_name(PRD_FOLLOW_UP)
    assert label == "follow-up"


def test_the_renderer_and_the_stamper_agree():
    """Two implementations of one rule, in two languages. They disagreed for months: the web
    guard was added when a CARD read wrong, and nobody carried it back to the function that
    writes the identity down."""
    import pathlib

    web = pathlib.Path("apps/web/app/app-identity.ts").read_text(encoding="utf-8")
    engine = pathlib.Path("engine/boss_engine/services/naming.py").read_text(encoding="utf-8")
    assert "function unadorn(" in web
    assert "def _unadorn(" in engine, "the stamper still cannot see past a leading emoji"
    # Both must apply it before the boilerplate test, not merely define it.
    assert "BOILERPLATE.match(_unadorn(" in engine
