"""'What is your biggest concern about this project right now?' — about what, exactly?

The operator opened Ask Adversarial Reviewer and was offered that, and asked the obvious
question back: "we dont have any project context right? what this project means?"

They were right twice over. The word is ambiguous — in BOSS a PROJECT is the workspace that
CONTAINS the apps, so a reviewer that had just read one app's code was being asked about a
container holding six of them. And this module's own rule, two paragraphs into its docstring,
already forbids it:

    IT MUST NAME SOMETHING THAT EXISTS. "Which of Hookwatch's ports are reachable from
    outside?" is answerable; "which of your services are exposed?" invites the model to invent
    a service.

"this project" is the second kind. The grounded starters were careful about this from the
start; the generic fallbacks were not, and they were what an unmatched role always got.

And Adversarial Reviewer was unmatched. So were Consistency Reviewer and any auditor — two of
the roles that run on EVERY build had no family, which is how a reviewer fresh from reading the
code came to be offered a dashboard question.
"""
from __future__ import annotations

from boss_engine.services.role_starters import (
    FAMILIES,
    GENERIC_ROLE_ASKS,
    NOTHING_BUILT_ASKS,
    _families_for,
    _fill,
    role_starters,
)


class _Rows:
    """A project with one real app and one real failure."""

    def __init__(self, app_title="Lead-Triage Inbox", role_row=None):
        self.app_title = app_title
        self.role_row = role_row

    def execute(self, statement, params=None):
        text = str(statement)
        outer = self

        class Result:
            def mappings(self_inner):
                return self_inner

            def scalars(self_inner):
                return self_inner

            def first(self_inner):
                return outer.role_row

            def all(self_inner):
                if "FROM meeting_roles" in text:
                    return [outer.role_row] if outer.role_row else []
                if "FROM code_task_runs" in text and "status = 'completed'" in text:
                    return [outer.app_title] if outer.app_title else []
                if "FROM code_task_runs" in text:
                    return [{"id": "crun_1", "title": outer.app_title}] if outer.app_title else []
                return []

        return Result()


class TestTheRolesThatHadNoFamily:
    def test_an_adversarial_reviewer_is_a_reviewer(self):
        assert [f["family"] for f in _families_for("Adversarial Reviewer", "")] == ["review"]

    def test_and_so_is_a_consistency_reviewer(self):
        assert [f["family"] for f in _families_for("Consistency Reviewer", "")] == ["review"]

    def test_and_an_auditor(self):
        assert "review" in [f["family"] for f in _families_for("Security Auditor", "")]

    def test_the_review_asks_could_only_be_asked_of_a_reviewer(self):
        """The module's other rule: if QA and Finance could both be asked it, it belongs on a
        dashboard."""
        asks = next(f for f in FAMILIES if f["family"] == "review")["asks"]
        assert any("flag" in a and "shipped anyway" in a for a in asks)
        assert any("attack first" in a for a in asks)

    def test_the_existing_families_did_not_move(self):
        assert [f["family"] for f in _families_for("Security", "")] == ["security"]
        assert [f["family"] for f in _families_for("QA Lead", "")] == ["qa"]
        assert [f["family"] for f in _families_for("Engineering Captain", "")] == ["engineering"]


class TestNoStarterSaysThisProject:
    def test_the_generic_asks_name_the_app(self):
        assert any("{app}" in a for a in GENERIC_ROLE_ASKS)

    def test_and_none_of_them_says_this_project_or_here(self):
        for ask in GENERIC_ROLE_ASKS + NOTHING_BUILT_ASKS:
            assert "this project" not in ask.lower(), ask
            assert " here " not in f" {ask.lower()} ", ask

    def test_an_unfillable_generic_is_dropped_rather_than_asked_vaguely(self):
        assert _fill(GENERIC_ROLE_ASKS[0], app=None, run=None) is None
        assert _fill(GENERIC_ROLE_ASKS[0], app="Lead-Triage Inbox", run=None) == (
            "What is your biggest concern about Lead-Triage Inbox right now?")

    def test_a_role_specific_ask_needs_no_app_and_survives(self):
        """"What would you refuse to sign off on today?" is about this role's judgement, not
        about a thing — it is grounded by whose judgement it asks for."""
        assert _fill(GENERIC_ROLE_ASKS[2], app=None, run=None) == GENERIC_ROLE_ASKS[2]

    def test_a_workspace_with_nothing_built_says_so(self):
        """Rather than gesturing at a project the operator has to interpret."""
        out = role_starters(_Rows(app_title=None), "p", "Some Unmatched Role")
        assert out
        assert "Nothing has been built in this workspace yet" in out[0]

    def test_every_starter_returned_names_the_real_app(self):
        out = role_starters(_Rows(), "p", "Adversarial Reviewer")
        assert out
        assert any("Lead-Triage Inbox" in ask for ask in out)
        assert not any("this project right now" in ask for ask in out)

    def test_the_module_still_says_why(self):
        import inspect

        from boss_engine.services import role_starters as module

        body = inspect.getsource(module)
        assert "IT MUST NAME SOMETHING THAT EXISTS" in body
        assert "the workspace that" in body and "CONTAINS the apps" in body
