"""One product is one project, whatever the punctuation.

The console showed "Incident Timeline Builder." and "Incident-Timeline Builder" as two
projects — two PRs, two bills, 17 attempts on one and 2 on the other — for a single idea. Both
the app identity and the duplicate guard compared names with `casefold()` alone, so a trailing
full stop and a hyphen were treated as meaning.

That cost twice: the display was wrong, and the guard that exists to refuse a second build of
an app already building did not fire, so BOSS paid to build the same thing again.
"""

from __future__ import annotations

import pytest

from boss_engine.services.naming import identity_key, project_name, stable_app_id

SAME = [
    "Incident Timeline Builder",
    "Incident Timeline Builder.",
    "Incident-Timeline Builder",
    "incident timeline builder",
    "Incident  Timeline   Builder",
    "Incident–Timeline Builder",      # en dash
    "Incident_Timeline_Builder",
]


class TestOneProductOneIdentity:
    @pytest.mark.parametrize("name", SAME)
    def test_punctuation_and_case_do_not_change_the_key(self, name):
        assert identity_key(name) == "incident timeline builder"

    @pytest.mark.parametrize("name", SAME)
    def test_they_all_hash_to_one_app(self, name):
        assert stable_app_id("p", "r", name) == stable_app_id("p", "r", SAME[0])

    def test_different_products_stay_different(self):
        """Collapsing punctuation must not collapse meaning."""
        assert stable_app_id("p", "r", "Bug Triage Board") != stable_app_id("p", "r", "Bug Tracker")
        assert identity_key("Shift-Swap Board") != identity_key("Shift Board")

    def test_a_different_project_or_repo_is_a_different_app(self):
        a = stable_app_id("proj-a", "repo", "Same Name")
        assert a != stable_app_id("proj-b", "repo", "Same Name")
        assert a != stable_app_id("proj-a", "other", "Same Name")

    def test_an_unusable_name_stamps_nothing(self):
        """Better lineage-only than joining a bogus group."""
        assert stable_app_id("p", "r", "") == ""
        assert identity_key("...") == ""
        assert identity_key("   ") == ""


class TestTheDuplicateGuardUsesTheSameRule:
    def test_it_calls_identity_key(self):
        import inspect

        from boss_engine.services import code_task_service

        source = inspect.getsource(code_task_service)
        marker = source.index("for row in rows:")
        assert "identity_key" in source[marker - 800:marker + 400], (
            "the guard that refuses a duplicate build must define 'same app' the same way the "
            "console groups them, or BOSS pays to build one product twice"
        )


def test_the_two_names_from_the_incident_are_one_app():
    """The exact pair the operator was shown, end to end through project_name."""
    a, _b, _l = project_name(
        "Build an incident timeline builder.\nWho it is for: an on-call engineer")
    b, _b2, _l2 = project_name("Incident-Timeline Builder")
    assert stable_app_id("boss-dogfood", "paramg/boss-sandbox", a) == \
           stable_app_id("boss-dogfood", "paramg/boss-sandbox", b)
