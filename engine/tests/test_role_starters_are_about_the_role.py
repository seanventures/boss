"""Every chat surface offered the same three prompts, under whichever name it had just claimed.

    ENGINEERING CAPTAIN — answering from this role's own work here
    [ What changed today? ] [ Which runs failed and why? ] [ What is this run waiting on? ]

Those are status questions. They are what you ask a dashboard, and the whole reason to open a
conversation with a named role rather than read a page is that the role has a lens, a remit and
a record of what it has already argued here. Three identical chips under eleven different names
say the opposite: that the name is decoration.

Two properties make a starter worth showing.

IT MUST ONLY MAKE SENSE FOR THIS ROLE — if QA and Finance could both be asked it, it belongs on
a dashboard. IT MUST NAME SOMETHING THAT EXISTS — "which of Hookwatch's ports are open" is
answerable, "which of your services are exposed" invites an answer about a service that was
never built.
"""
from __future__ import annotations

import pytest

from boss_engine.services.role_starters import (
    FAMILIES,
    GENERIC_ROLE_ASKS,
    _families_for,
    role_starters,
)

BANNED = ("what changed today", "which runs failed", "what is this run waiting on")


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None

    def scalars(self):
        return self

    def __iter__(self):
        return iter(self._rows)


class FakeConn:
    """Answers the four reads this service makes; anything else comes back empty."""

    def __init__(self, *, role=None, apps=(), failure=None, meetings=()):
        self.role = role
        self.apps = list(apps)
        self.failure = failure
        self.meetings = list(meetings)

    def execute(self, statement, params=None):
        sql = " ".join(str(statement).split())
        if "FROM meeting_roles" in sql:
            return _Rows([self.role] if self.role else [])
        if "status = 'completed'" in sql:
            return _Rows([{"title": a} for a in self.apps])
        if "'failed', 'blocked', 'error'" in sql:
            return _Rows([self.failure] if self.failure else [])
        if "FROM meeting_runs" in sql:
            return _Rows([{"state": m} for m in self.meetings])
        return _Rows([])


def _scalars(rows):
    return rows


# `.scalars().all()` and `.mappings().first()` are both used; make the fake answer either shape.
class _ScalarRows(_Rows):
    def all(self):
        out = []
        for row in self._rows:
            out.append(list(row.values())[0] if isinstance(row, dict) else row)
        return out


class Conn(FakeConn):
    def execute(self, statement, params=None):
        rows = super().execute(statement, params)
        sql = " ".join(str(statement).split())
        if "status = 'completed'" in sql:
            return _ScalarRows([{"title": a} for a in self.apps])
        if "FROM meeting_runs" in sql:
            return _ScalarRows([{"state": m} for m in self.meetings])
        return rows


SECURITY = {"title": "Security", "lens": "threat modeling and abuse. Authentication, secrets."}
QA = {"title": "QA / Test", "lens": "quality, edge cases and acceptance criteria."}
FINANCE = {"title": "Finance / FinOps", "lens": "cost, budget and ROI."}
CAPTAIN = {"title": "Engineering Captain",
           "lens": "technical feasibility and architecture for the team that will own it."}


class TestTheStartersAreAboutTheRole:
    def test_security_and_finance_are_asked_different_things(self):
        """The failure, stated as a test: two roles, one set of questions."""
        conn = Conn(role=SECURITY, apps=["Build HookWatch, a webhook monitor"])
        sec = role_starters(conn, "p", "Security")
        fin = role_starters(Conn(role=FINANCE, apps=["Build HookWatch, a webhook monitor"]),
                            "p", "Finance / FinOps")
        assert sec and fin
        assert not (set(sec) & set(fin)), f"shared prompts: {set(sec) & set(fin)}"

    def test_the_generic_status_prompts_are_gone(self):
        for role in (SECURITY, QA, FINANCE, CAPTAIN):
            starters = role_starters(Conn(role=role, apps=["Build HookWatch"]), "p",
                                     role["title"])
            for s in starters:
                assert not any(b in s.lower() for b in BANNED), f"{role['title']}: {s}"

    def test_security_asks_about_exposure(self):
        out = " ".join(role_starters(Conn(role=SECURITY, apps=["Build HookWatch"]), "p",
                                     "Security")).lower()
        assert "port" in out or "secret" in out or "abuse" in out

    def test_qa_asks_about_tests_and_criteria(self):
        out = " ".join(role_starters(Conn(role=QA, apps=["Build HookWatch"]), "p",
                                     "QA / Test")).lower()
        assert "test" in out or "criteria" in out


class TestARoleThePlatformNeverHeardOf:
    """`Engineering Captain` is not in ROLE_CATALOG. A starter set that only covers the built-in
    roles is blank exactly where somebody has bothered to build a workforce of their own."""

    def test_a_custom_role_is_matched_by_its_lens(self):
        families = _families_for("Engineering Captain", CAPTAIN["lens"])
        assert families and families[0]["family"] == "engineering"

    def test_and_so_gets_engineering_questions_not_generic_ones(self):
        out = role_starters(Conn(role=CAPTAIN, apps=["Build HookWatch"]), "p",
                            "Engineering Captain")
        assert out and out[0] not in GENERIC_ROLE_ASKS

    def test_a_role_matching_nothing_still_gets_role_questions(self):
        """Not status questions: still about this member's own judgement."""
        odd = {"title": "Chief Vibes Officer", "lens": "morale"}
        out = role_starters(Conn(role=odd), "p", "Chief Vibes Officer")
        assert out and set(out) <= set(GENERIC_ROLE_ASKS)
        for s in out:
            assert not any(b in s.lower() for b in BANNED)


class TestItNamesThingsThatExist:
    def test_a_project_with_no_apps_gets_no_app_questions(self):
        """An unfilled `{app}` reads as a bug; a hypothetical one invites the model to answer
        about software that was never built."""
        out = role_starters(Conn(role=SECURITY), "p", "Security")
        assert out
        assert not any("{app}" in s or "{run}" in s for s in out)

    def test_the_app_name_is_the_real_one(self):
        conn = Conn(role=SECURITY, apps=["Build HookWatch, a webhook monitor.\nWho it is for:"])
        assert any("HookWatch" in s for s in role_starters(conn, "p", "Security"))

    def test_a_run_placeholder_needs_a_real_failure(self):
        conn = Conn(role=QA, apps=["Build HookWatch"])            # no failed run
        assert not any("{run}" in s for s in role_starters(conn, "p", "QA / Test"))

    def test_a_failure_with_no_usable_title_is_described_not_pasted(self):
        """Live, this rendered as "What actually caused crun_734b070b10ce48d6b71f to fail?".
        Grounded and unreadable — a starter is an invitation, and nobody is invited by a
        24-character token. The phrase resolves to exactly one run and the role can find it."""
        conn = Conn(role=QA, apps=["Build HookWatch"],
                    failure={"id": "crun_734b070b10ce48d6b71f", "title": "New work"})
        out = role_starters(conn, "p", "QA / Test")
        assert not any("crun_" in s for s in out)
        assert any("the last failed run" in s for s in out)

    def test_no_placeholder_ever_reaches_the_operator(self):
        for role in (SECURITY, QA, FINANCE, CAPTAIN):
            for s in role_starters(Conn(role=role), "p", role["title"]):
                assert "{" not in s and "}" not in s


class TestWhatOnlyThisRoleCouldBeAsked:
    """The strongest starter is the one no page can show: this member's own line through the
    minutes. The console renders a meeting as a whole, never one participant's argument."""

    def test_a_recorded_position_becomes_the_first_question(self):
        conn = Conn(role=SECURITY, apps=["Build HookWatch"], meetings=[{
            "round1": {"Security": "The webhook signing secret is read from the environment "
                                   "with no rotation path, which we will regret."}}])
        out = role_starters(conn, "p", "Security")
        assert "signing secret" in out[0]
        assert "did the build honour that" in out[0].lower()

    def test_another_role_s_position_is_not_borrowed(self):
        conn = Conn(role=SECURITY, apps=["Build HookWatch"], meetings=[{
            "round1": {"Finance / FinOps": "This costs more than it returns."}}])
        assert not any("costs more than it returns" in s for s in role_starters(conn, "p",
                                                                               "Security"))

    def test_a_one_word_position_is_not_worth_quoting(self):
        conn = Conn(role=SECURITY, meetings=[{"round1": {"Security": "Agreed."}}])
        assert not any("Agreed" in s for s in role_starters(conn, "p", "Security"))


class TestAPlaceholderIsNotAName:
    """Observed live. The Security starter came back as "Which ports does New work have open?"
    with HookWatch and Vendor-Status-Watcher sitting four rows behind it in the same list.

    A run whose brief was never written keeps the catalogue's default title, and it passes every
    "is this a real app?" check because it IS a real row. This is the worst kind of ungrounded:
    it sounds specific, so the operator believes there is an app called New work."""

    def test_the_placeholder_title_is_skipped_for_the_real_app_behind_it(self):
        conn = Conn(role=SECURITY, apps=["New work", "Build HookWatch, a webhook monitor"])
        assert any("HookWatch" in s for s in role_starters(conn, "p", "Security"))
        assert not any("New work" in s for s in role_starters(conn, "p", "Security"))

    def test_a_follow_up_run_does_not_supply_the_name(self):
        """"Follow-up: deferred review findings — HookWatch" names a piece of REWORK. Used as an
        app name it produces a question addressed to a sentence."""
        conn = Conn(role=SECURITY,
                    apps=["Follow-up: deferred review findings — HookWatch",
                          "Build Vendor Status Watcher"])
        out = role_starters(conn, "p", "Security")
        assert any("Vendor" in s for s in out)
        assert not any("deferred review" in s for s in out)

    def test_a_project_of_nothing_but_placeholders_asks_nothing_about_apps(self):
        conn = Conn(role=SECURITY, apps=["New work", "Untitled", "sample"])
        out = role_starters(conn, "p", "Security")
        assert out
        assert not any("New work" in s or "Untitled" in s for s in out)


class TestItCannotBreakThePanel:
    def test_a_database_that_will_not_answer_still_returns_something(self):
        class Broken(Conn):
            def execute(self, statement, params=None):
                raise RuntimeError("connection reset")

        assert role_starters(Broken(), "p", "Security")

    def test_an_empty_role_asks_nothing(self):
        assert role_starters(Conn(), "p", "") == []

    def test_the_count_is_bounded(self):
        conn = Conn(role=SECURITY, apps=["Build HookWatch"],
                    failure={"id": "crun_1", "title": "Build Vendor Status Watcher"},
                    meetings=[{"round1": {"Security": "A long enough position to be quoted here."}}])
        assert len(role_starters(conn, "p", "Security", limit=4)) <= 4

    @pytest.mark.parametrize("family", [f["family"] for f in FAMILIES])
    def test_every_family_offers_something(self, family):
        spec = next(f for f in FAMILIES if f["family"] == family)
        assert spec["asks"], family
