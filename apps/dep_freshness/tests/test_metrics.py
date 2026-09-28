"""Tests for the hand-rolled stdlib metrics registry (no HTTP involved).

Exercises the exposition-format contract required by the BOSS platform
observability standard: cumulative/monotonic histogram buckets, le="+Inf"
equal to _count, label escaping, and route-template collapsing.
"""

from __future__ import annotations

import re
import time
import unittest

from app.metrics import Histogram, MetricsRegistry, escape_label_value


class EscapeLabelValueTests(unittest.TestCase):
    def test_escapes_backslash(self):
        self.assertEqual(escape_label_value("a\\b"), "a\\\\b")

    def test_escapes_double_quote(self):
        self.assertEqual(escape_label_value('a"b'), 'a\\"b')

    def test_escapes_newline(self):
        self.assertEqual(escape_label_value("a\nb"), "a\\nb")

    def test_plain_value_is_unchanged(self):
        self.assertEqual(escape_label_value("plain-value_1.0"), "plain-value_1.0")

    def test_escapes_backslash_before_quote_to_avoid_double_escaping(self):
        self.assertEqual(escape_label_value('back\\slash"quote'), 'back\\\\slash\\"quote')

    def test_empty_string_is_unchanged(self):
        self.assertEqual(escape_label_value(""), "")


class CounterTests(unittest.TestCase):
    def test_inc_default_amount_renders_as_one(self):
        registry = MetricsRegistry(version="1.0.0")
        registry.http_requests_total.inc({"method": "GET", "route": "/", "status": "200"})
        text = registry.render()
        self.assertRegex(text, r'http_requests_total\{[^}]*method="GET"[^}]*\}\s+1(\.0)?$')

    def test_inc_accumulates_amount(self):
        registry = MetricsRegistry(version="1.0.0")
        labels = {"method": "GET", "route": "/", "status": "200"}
        registry.http_requests_total.inc(labels)
        registry.http_requests_total.inc(labels, amount=2)
        text = registry.render()
        match = re.search(r'http_requests_total\{[^}]*\}\s+([0-9.]+)', text)
        self.assertIsNotNone(match)
        self.assertEqual(float(match.group(1)), 3.0)

    def test_distinct_label_values_produce_distinct_series(self):
        registry = MetricsRegistry(version="1.0.0")
        registry.http_requests_total.inc({"method": "GET", "route": "/", "status": "200"})
        registry.http_requests_total.inc({"method": "POST", "route": "/", "status": "201"})
        text = registry.render()
        lines = [line for line in text.splitlines() if line.startswith("http_requests_total{")]
        self.assertEqual(len(lines), 2)

    def test_repeated_identical_labels_collapse_to_one_series(self):
        # A templated route hit repeatedly must stay a single time series.
        registry = MetricsRegistry(version="1.0.0")
        for _ in range(5):
            registry.http_requests_total.inc({"method": "GET", "route": "/", "status": "200"})
        text = registry.render()
        lines = [line for line in text.splitlines() if line.startswith("http_requests_total{")]
        self.assertEqual(len(lines), 1)
        self.assertRegex(lines[0], r'\s5(\.0)?$')

    def test_label_values_are_escaped_in_render(self):
        registry = MetricsRegistry(version="1.0.0")
        registry.http_requests_total.inc(
            {"method": "GET", "route": '/weird"route', "status": "200"}
        )
        text = registry.render()
        self.assertIn('route="/weird\\"route"', text)


class HistogramTests(unittest.TestCase):
    def test_default_buckets_span_5ms_to_10s(self):
        self.assertEqual(Histogram.DEFAULT_BUCKETS[0], 0.005)
        self.assertEqual(Histogram.DEFAULT_BUCKETS[-1], 10)

    def test_bucket_counts_are_cumulative_and_monotonic(self):
        registry = MetricsRegistry(version="1.0.0")
        labels = {"method": "GET", "route": "/"}
        for value in (0.001, 0.02, 0.2, 3.0, 20.0):
            registry.http_request_duration_seconds.observe(value, labels)
        text = registry.render()

        counts = []
        for line in text.splitlines():
            if line.startswith("http_request_duration_seconds_bucket{"):
                match = re.search(r'le="([^"]+)"\}\s+([0-9.]+)', line)
                self.assertIsNotNone(match, msg=f"malformed bucket line: {line!r}")
                counts.append(float(match.group(2)))

        self.assertTrue(counts, "expected at least one bucket line")
        self.assertEqual(counts, sorted(counts))

    def test_inf_bucket_equals_count(self):
        registry = MetricsRegistry(version="1.0.0")
        labels = {"method": "GET", "route": "/"}
        for value in (0.001, 0.02, 0.2, 3.0, 20.0):
            registry.http_request_duration_seconds.observe(value, labels)
        text = registry.render()

        inf_match = re.search(
            r'http_request_duration_seconds_bucket\{[^}]*le="\+Inf"[^}]*\}\s+([0-9.]+)', text
        )
        count_match = re.search(
            r'http_request_duration_seconds_count\{[^}]*\}\s+([0-9.]+)', text
        )
        self.assertIsNotNone(inf_match)
        self.assertIsNotNone(count_match)
        self.assertEqual(float(inf_match.group(1)), float(count_match.group(1)))
        self.assertEqual(float(count_match.group(1)), 5.0)

    def test_value_above_largest_bucket_still_counted_in_inf(self):
        registry = MetricsRegistry(version="1.0.0")
        registry.http_request_duration_seconds.observe(999.0, {"method": "GET", "route": "/"})
        text = registry.render()
        inf_match = re.search(
            r'http_request_duration_seconds_bucket\{[^}]*le="\+Inf"[^}]*\}\s+([0-9.]+)', text
        )
        self.assertIsNotNone(inf_match)
        self.assertEqual(float(inf_match.group(1)), 1.0)


class RegistryExpositionTests(unittest.TestCase):
    def test_required_series_names_present(self):
        registry = MetricsRegistry(version="9.9.9")
        text = registry.render()
        for name in (
            "http_requests_total",
            "http_request_duration_seconds",
            "app_info",
            "app_uptime_seconds",
        ):
            self.assertIn(name, text)

    def test_app_info_is_fixed_at_one_with_version_label(self):
        registry = MetricsRegistry(version="9.9.9")
        text = registry.render()
        self.assertRegex(text, r'app_info\{[^}]*version="9\.9\.9"[^}]*\}\s+1(\.0)?$')

    def test_uptime_is_non_negative_and_monotonic(self):
        registry = MetricsRegistry(version="1.0.0")
        first = registry.uptime_seconds()
        time.sleep(0.01)
        second = registry.uptime_seconds()
        self.assertGreaterEqual(first, 0)
        self.assertGreaterEqual(second, first)

    def test_help_and_type_comments_present_for_each_metric(self):
        registry = MetricsRegistry(version="1.0.0")
        text = registry.render()
        for name in (
            "http_requests_total",
            "http_request_duration_seconds",
            "app_info",
            "app_uptime_seconds",
        ):
            self.assertIn(f"# HELP {name}", text)
            self.assertIn(f"# TYPE {name}", text)

    def test_exposition_lines_are_well_formed(self):
        registry = MetricsRegistry(version="1.0.0")
        registry.http_requests_total.inc({"method": "GET", "route": "/", "status": "200"})
        registry.http_request_duration_seconds.observe(0.01, {"method": "GET", "route": "/"})
        text = registry.render()

        line_pattern = re.compile(r'^[a-zA-Z_:][a-zA-Z0-9_:]*(\{[^}]*\})?\s+\S+$')
        for line in text.splitlines():
            if not line or line.startswith("#"):
                continue
            self.assertRegex(line, line_pattern, msg=f"malformed exposition line: {line!r}")

    def test_render_does_not_raise_on_freshly_constructed_registry(self):
        registry = MetricsRegistry(version="0.0.0")
        text = registry.render()
        self.assertIsInstance(text, str)
        self.assertGreater(len(text), 0)

    def test_no_deployment_metadata_labels_present(self):
        # app/environment/project/repo/target_id are attached by the collector,
        # never self-reported by the app.
        registry = MetricsRegistry(version="1.0.0")
        registry.http_requests_total.inc({"method": "GET", "route": "/", "status": "200"})
        text = registry.render()
        for forbidden in ("environment=", "project=", "repo=", "target_id=", "app=\""):
            self.assertNotIn(forbidden, text)


if __name__ == "__main__":
    unittest.main()
