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
