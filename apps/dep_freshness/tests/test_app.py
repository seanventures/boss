"""End-to-end tests against the FastAPI app via Starlette's TestClient.

TestClient never binds a real socket, so this stays consistent with the
deployability rule that only real server startup reads HOST/PORT.
"""

from __future__ import annotations

import re
import unittest

from fastapi.testclient import TestClient

from app.main import app


class ReportPageTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_root_returns_html_200(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers["content-type"])

    def test_root_never_errors(self):
        response = self.client.get("/")
        self.assertLess(response.status_code, 500)

    def test_root_lists_a_recognizable_freshness_status(self):
        response = self.client.get("/")
        body = response.text.lower()
        self.assertTrue(
            any(status in body for status in ("current", "outdated", "unknown")),
            "expected at least one freshness status token in the rendered report",
        )


class HealthzTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_healthz_returns_200(self):
        response = self.client.get("/healthz")
        self.assertEqual(response.status_code, 200)


class MetricsEndpointTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_metrics_returns_200_with_prometheus_content_type(self):
        response = self.client.get("/metrics")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.headers["content-type"],
            "text/plain; version=0.0.4; charset=utf-8",
        )

    def test_metrics_body_contains_required_series(self):
        response = self.client.get("/metrics")
        for name in (
            "http_requests_total",
            "http_request_duration_seconds",
            "app_info",
            "app_uptime_seconds",
        ):
            self.assertIn(name, response.text)

    def test_metrics_requires_no_authentication(self):
        response = self.client.get("/metrics")
        self.assertNotIn(response.status_code, (401, 403))

    def test_metrics_scrapes_are_excluded_from_request_counters(self):
        self.client.get("/metrics")
        self.client.get("/metrics")
        response = self.client.get("/metrics")
        self.assertNotIn('route="/metrics"', response.text)

    def test_real_requests_are_counted_under_templated_route(self):
        self.client.get("/")
        self.client.get("/")
        response = self.client.get("/metrics")
        match = re.search(
            r'http_requests_total\{[^}]*route="/"[^}]*\}\s+([0-9.]+)', response.text
        )
        self.assertIsNotNone(match)
        self.assertGreaterEqual(float(match.group(1)), 2.0)

    def test_metrics_route_itself_not_templated_into_root_series(self):
        before = self.client.get("/metrics")
        previous = re.search(
            r'http_requests_total\{[^}]*route="/"[^}]*\}\s+([0-9.]+)', before.text
        )
        self.client.get("/metrics")
        response = self.client.get("/metrics")
        match = re.search(
            r'http_requests_total\{[^}]*route="/"[^}]*\}\s+([0-9.]+)', response.text
        )
        # Earlier tests share this app's in-process registry. Scraping metrics
        # must leave the root route's count at its previous value.
        self.assertEqual(
            float(match.group(1)) if match else 0.0,
            float(previous.group(1)) if previous else 0.0,
        )


if __name__ == "__main__":
    unittest.main()
