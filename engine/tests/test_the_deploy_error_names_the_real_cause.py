"""A deploy that refuses must say WHICH refusal this is.

An app failed to deploy with:

    cannot tell which app this task built: the repository contains 8 runnable apps
    (py:serve.py, py:serve_nqueens.py, static:web, ...) and the run recorded no
    implementation files identifying one.

Every clause of that was wrong about this failure. The run HAD recorded its files. The eight
apps were unrelated samples. The actual cause was that the deploy checked out `main` while the
app existed only on its open PR branch — `commit_ref: main`, one field, visible the whole time.

Four investigations went into host binding, ssh credentials, surface ownership and app_entry
before anybody read it, because the message confidently described a different problem. The
tree is right there when the error is raised, so "are this app's files even here?" is a
question the code can answer instead of guessing at the operator's expense.
"""
from __future__ import annotations

import inspect

from boss_engine.services import deploy_service


class TestTheMessageDistinguishesTheTwoFailures:
    """One symptom, two causes, and they need different actions from the operator.

    Ambiguity is fixed by declaring the app. Absence is fixed by deploying a different ref or
    merging — declaring the app cannot help, and telling somebody to declare it wastes their
    afternoon, which is exactly what happened.
    """

    def _source(self) -> str:
        src = inspect.getsource(deploy_service)
        return src[src.index("except AmbiguousAppError"):][:2600]

    def test_absence_is_reported_as_absence(self):
        body = self._source()
        assert "is not in" in body, "the message must name the missing-from-this-ref case"

    def test_it_checks_the_tree_rather_than_assuming(self):
        """The claim has to be verified against the checkout, not inferred from the error."""
        body = self._source()
        assert "(work / c).exists()" in body

    def test_it_says_which_ref_it_looked_in(self):
        """`commit_ref: main` was the answer and appeared in no message anywhere."""
        body = self._source()
        assert "where = ref or branch" in body
        assert "{where}" in body

    def test_it_offers_the_actions_that_can_actually_work(self):
        body = self._source()
        assert "merge" in body.lower()
        assert "branch the app was built on" in body

    def test_the_ambiguous_case_still_says_declare_the_app(self):
        """The original failure mode is real too; it must not lose its own advice."""
        body = self._source()
        assert "boss.preview.command" in body

    def test_both_paths_still_refuse(self):
        """Fail-closed is the whole point: deploying the wrong app to a public host is worse
        than deploying nothing."""
        body = self._source()
        assert body.count("raise RuntimeError(") == 2


class TestTheProbeCannotPassTrivially:
    """A check that always passes is worse than no check: it prints the wrong message with
    added confidence.

    The first version tested every hinted path for existence, including "." — the repository
    root, which exists in every checkout. So for an app declared at the root (the case that
    prompted all of this) the probe passed, the code concluded the app WAS present, and the
    misleading "cannot tell which app this task built" printed exactly as before. Caught only
    by running the real deploy again and reading the message.
    """

    def test_the_repository_root_is_not_treated_as_evidence(self):
        src = inspect.getsource(deploy_service)
        body = src[src.index("except AmbiguousAppError"):][:2600]
        assert 'c not in (".", "./")' in body
