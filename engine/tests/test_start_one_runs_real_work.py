""""Start one" demonstrated the workflow by showing it produce nothing.

The catalogue card sent this, verbatim:

    {"directive": "Describe the work here", "title": "New work"}

So the panel convened and debated the phrase "Describe the work here", the spec review approved
it, and the builder tried to build it. Ten steps ran and none of them did anything — which is a
worse demonstration than no button at all, because it looks like the workflow is the thing that
does not work.

The sample has to be chosen where the PROJECT is known, and it has to fit the work class. A
`feature.minor` lane has no research step and no spec review; handing it "build a lead triage
inbox" produces a whole product through a lane designed for a small change, which demonstrates
the wrong thing convincingly.
"""
from __future__ import annotations

from boss_engine.services import workflow_samples as ws


class FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class FakeConn:
    """Answers the one query the sampler makes: which apps this project has shipped."""

    def __init__(self, app_titles):
        self._titles = app_titles

    def execute(self, statement, params=None):
        return FakeResult(self._titles)


class TestNewProductSamples:
    def test_a_fresh_project_gets_a_whole_product_brief(self):
        got = ws.sample_for(FakeConn([]), "p", "project.new", seed=1)
        assert got and got["directive"] and got["title"]
        assert got["acceptance_criteria"], "a panel cannot argue about scope without these"

    def test_the_brief_names_a_user_and_a_problem(self):
        """"Build a thing" gives the panel nothing to push back on."""
        got = ws.sample_for(FakeConn([]), "p", "project.new", seed=2)
        assert "Who it is for:" in got["directive"]

    def test_it_does_not_offer_something_this_project_already_built(self):
        """BOSS refuses duplicate work by app identity, so a repeat would end the demo on an
        error at the build step."""
        first = ws.sample_for(FakeConn([]), "p", "project.new", seed=3)
        built = [first["title"]]
        for _ in range(10):
            again = ws.sample_for(FakeConn(built), "p", "project.new", seed=None)
            assert again is None or again["title"] != first["title"]

    def test_it_gives_up_honestly_when_everything_has_been_built(self):
        built = [s["title"] for s in ws.NEW_PRODUCT_SAMPLES]
        assert ws.sample_for(FakeConn(built), "p", "project.new") is None

    def test_punctuation_does_not_defeat_the_duplicate_check(self):
        """The same identity rule the duplicate guard uses, so the two cannot disagree."""
        first = ws.NEW_PRODUCT_SAMPLES[0]["title"]
        built = [f"{first}."]
        for _ in range(10):
            got = ws.sample_for(FakeConn(built), "p", "project.new", seed=None)
            assert got is None or got["title"] != first


class TestTheSampleFitsTheLane:
    def test_a_minor_feature_changes_an_app_that_exists(self):
        got = ws.sample_for(FakeConn(["HookWatch"]), "p", "feature.minor", seed=1)
        assert got and "HookWatch" in got["directive"]
        assert "existing app" in got["directive"]

    def test_a_minor_feature_is_refused_when_the_project_has_no_apps(self):
        """Inventing one produces a broken project card, not a demonstration."""
        assert ws.sample_for(FakeConn([]), "p", "feature.minor") is None

    def test_the_refusal_says_what_to_do_instead(self):
        why = ws.why_no_sample("feature.minor")
        assert "has not shipped one yet" in why
        assert "Standard Product Delivery first" in why

    def test_a_bug_lane_also_needs_an_existing_app(self):
        assert ws.sample_for(FakeConn([]), "p", "bug.fix") is None
        assert ws.sample_for(FakeConn(["HookWatch"]), "p", "bug.fix", seed=1)

    def test_discovery_gets_a_question_not_a_build_instruction(self):
        got = ws.sample_for(FakeConn([]), "p", "research.discovery", seed=1)
        assert got and got["directive"].endswith("?")

    def test_an_unknown_class_still_gets_real_work(self):
        got = ws.sample_for(FakeConn([]), "p", "something.else", seed=1)
        assert got and "Who it is for:" in got["directive"]


class TestEverySampleIsRunnable:
    def test_every_product_brief_has_criteria_and_a_user(self):
        for s in ws.NEW_PRODUCT_SAMPLES:
            assert s["title"] and s["directive"] and s["acceptance_criteria"], s["title"]
            assert "Who it is for:" in s["directive"], s["title"]
            assert len(s["acceptance_criteria"]) >= 2, s["title"]

    def test_no_two_briefs_are_the_same_app(self):
        from boss_engine.services.naming import identity_key

        keys = [identity_key(s["title"]) for s in ws.NEW_PRODUCT_SAMPLES]
        assert len(keys) == len(set(keys))

    def test_follow_ups_read_as_small_changes(self):
        """Not products: this lane has no research step and no spec review."""
        for change in ws.FOLLOW_UP_SAMPLES:
            assert change.lower().startswith("add ")
