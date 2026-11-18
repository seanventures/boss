"""A SAMPLE NEVER WAITS ON A SERVER (stranger test r3, 2026-11-18).

Try a sample builds and opens its pull request with no deployment host; its own checklist says
so. Yet "Pick a staging host" sat in the project's Needs you and the bell. The deploy host is
optional for a sample (projects.sample_key), so it is not a Needs-you card there; any other
project still gets it, and every other "only you" item still shows on a sample.

Breaks if a sample's Needs you lists the staging host again, or a normal project loses it.
"""
from __future__ import annotations

from boss_engine.services.project_experience import needs

ONBOARDING = {"only_you": [
    {"key": "deploy_target", "label": "Deploy target", "detail": "No verified server yet.",
     "action": {"label": "Add a server", "href": "/?area=infra"}},
    {"key": "secrets", "label": "Secrets", "detail": "", "action": {}},
]}


class _Row:
    def __init__(self, value):
        self.value = value

    def first(self):
        return None if self.value is None else (self.value,)


class _Conn:
    """Answers the one read setup_cards makes here: the project's sample_key."""

    def __init__(self, sample_key):
        self.sample_key = sample_key
        self.sql: list[str] = []

    def execute(self, statement, params=None):
        self.sql.append(str(statement))
        assert "sample_key" in str(statement), str(statement)
        return _Row(self.sample_key)


def _labels(sample_key):
    cards = needs.setup_cards(_Conn(sample_key), "p1", ONBOARDING, production_waits=False)
    return [c["label"] for c in cards]


def test_a_sample_does_not_list_the_staging_host_as_needed():
    assert _labels("hello-world") == ["Set the app's secrets"]


def test_any_other_project_still_lists_it():
    assert _labels(None) == ["Pick a staging host", "Set the app's secrets"]
