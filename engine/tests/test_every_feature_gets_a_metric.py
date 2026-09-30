"""Metrics were mandated for the builder and verified by nobody.

The observability standard has been in every pipeline stage for a while: expose `/metrics`,
these four series, templated route labels, and "rendering must be testable". Two things were
still missing, and the evidence was on the host — three apps deployed, none of them scraped,
and a monitoring page linking to Grafana dashboards for metrics no stage had checked existed.

  1. DOMAIN METRICS WERE OPTIONAL. "Add domain counters where they answer a real operational
     question" is a suggestion. An app can satisfy it with nothing, and then request counts say
     the server is alive while the thing the app exists to do fails silently.

  2. QA WAS NEVER TOLD TO TEST ANY OF IT. The stage whose entire job is writing tests was not
     asked for a single metrics case. A standard no stage verifies is advice.

Both are prompt-level, and prompt rules are weak — but a rule that is DELETED is weaker, and
these encode a gap that shipped.
"""
from __future__ import annotations

import pytest

from boss_engine.services.governed_code_task_service import _qa_prompt
from boss_engine.services.platform_standards import (
    METRICS_TEST_REQUIREMENT,
    applies_to,
    platform_standards,
)


class TestFeaturesMustBeInstrumented:
    def test_domain_metrics_are_required_not_suggested(self):
        standard = platform_standards("an http api")
        assert "FUNCTIONAL METRICS ARE REQUIRED, NOT OPTIONAL" in standard

    def test_it_ties_the_metric_to_the_acceptance_criteria(self):
        """Otherwise "add some counters" is satisfied by counting anything."""
        standard = platform_standards("an http api")
        assert "Every feature in the acceptance criteria" in standard

    def test_it_gives_the_question_that_decides_whether_a_feature_is_covered(self):
        standard = platform_standards("an http api")
        assert "would tell me this broke at 3am" in standard.lower()

    def test_it_still_warns_against_exhaustive_coverage(self):
        """The point is signal somebody would page on, not a metric per function."""
        assert "somebody would page on" in platform_standards("an http api")


class TestQaTestsTheInstrumentation:
    WEB = dict(title="Build a status watcher API", instructions="expose http endpoints",
               criteria=["a user can add a vendor"], test_cmd="npm run verify",
               web=True, app_root="app")

    def test_the_web_qa_stage_is_asked_for_metrics_cases(self):
        prompt = _qa_prompt(**self.WEB)
        assert "METRICS TESTS ARE PART OF THE SUITE" in prompt

    def test_the_python_qa_stage_is_too(self):
        prompt = _qa_prompt("An HTTP service", "serves requests", ["it responds"], "pytest")
        assert "METRICS TESTS ARE PART OF THE SUITE" in prompt

    def test_it_demands_a_delta_not_a_name(self):
        """A metric that never increments is the same as no metric and harder to notice."""
        assert "Assert the delta" in METRICS_TEST_REQUIREMENT

    def test_it_demands_the_cardinality_test(self):
        """The failure that takes the metrics backend down rather than the app."""
        assert "ONE series, not two" in METRICS_TEST_REQUIREMENT

    def test_it_names_the_four_platform_series(self):
        for series in ("http_requests_total", "http_request_duration_seconds",
                       "app_info", "app_uptime_seconds"):
            assert series in METRICS_TEST_REQUIREMENT, series

    def test_it_tells_qa_to_say_so_rather_than_test_around_a_gap(self):
        assert "rather than writing" in METRICS_TEST_REQUIREMENT


class TestItOnlyAppliesWhereItMakesSense:
    @pytest.mark.parametrize("task", [
        "Build a REST api for vendors",
        "an express web app",
        "a flask service on a port",
    ])
    def test_services_are_in_scope(self, task):
        assert applies_to(task)
        assert "METRICS TESTS" in _qa_prompt(task, task, [], "pytest")

    def test_a_library_is_not_asked_for_an_endpoint(self):
        """Demanding one would produce cargo-culted dead code."""
        task = "A CSV sorting library that orders rows in place"
        assert not applies_to(task)
        assert "METRICS TESTS" not in _qa_prompt(task, task, [], "pytest")


class TestTheStandardReachesRealBriefs:
    """The gap that made all of the above decorative.

    `applies_to` keyword-matches the prose for "http", "api", "service", "port". Real briefs are
    written in product language, and checked against the directives BOSS has ACTUALLY built
    from it fired on none of them:

        "Build an incident timeline builder. Who it is for: an on-call engineer…"   -> no
        "Vendor-Status-Watcher"                                                     -> no
        "HookWatch"                                                                 -> no
        "Docs freshness board — whoever owns docs nobody trusts any more."          -> no

    So the observability standard was silently absent from every app the platform ever shipped.
    Three of them are on a host right now with a monitoring page linking to dashboards for data
    nobody was collecting. Strengthening the standard would have changed nothing.

    The pipeline knows what it is building — `web_app_build` is set before any prompt is
    composed — so the rule applies because of what is being built, not because of a word
    somebody happened to use.
    """

    REAL_BRIEF = ("Build an incident timeline builder.\n"
                  "Who it is for: an on-call engineer writing the post-mortem the morning after.")
    CRITERIA = ["an engineer can paste an alert log and get a timeline"]

    def test_the_prose_check_alone_would_still_miss_it(self):
        """Kept as a fallback, and this is why it cannot be the only signal."""
        assert not applies_to(self.REAL_BRIEF)

    def test_a_web_app_build_gets_the_standard_anyway(self):
        prompt = _qa_prompt(self.REAL_BRIEF, self.REAL_BRIEF, self.CRITERIA,
                            "npm run verify", web=True, app_root="app")
        assert "BOSS PLATFORM STANDARDS" in prompt
        assert "METRICS TESTS ARE PART OF THE SUITE" in prompt

    def test_an_app_root_is_enough_on_the_python_path_too(self):
        """A deployed app is a thing BOSS scrapes, whatever it is written in."""
        prompt = _qa_prompt(self.REAL_BRIEF, self.REAL_BRIEF, self.CRITERIA,
                            "pytest", app_root="app")
        assert "BOSS PLATFORM STANDARDS" in prompt
        assert "METRICS TESTS ARE PART OF THE SUITE" in prompt

    def test_the_implementation_stage_sees_it_as_well(self):
        """A standard read only by QA is a standard the builder never applies."""
        from boss_engine.services.governed_code_task_service import _impl_prompt

        prompt = _impl_prompt(self.REAL_BRIEF, self.REAL_BRIEF, ["test_x.py"],
                              "npm run verify", criteria=self.CRITERIA, app_root="app")
        assert "BOSS PLATFORM STANDARDS" in prompt

    def test_a_library_is_still_left_alone(self):
        """The widening must not turn into "every task gets every rule"."""
        prompt = _qa_prompt("A CSV sorting library", "sort rows in place",
                            ["sorts numerically"], "pytest")
        assert "BOSS PLATFORM STANDARDS" not in prompt
        assert "METRICS TESTS" not in prompt
