"""Unit tests for scripts/qa/feature_sweep.py (no network)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.qa.feature_sweep import (  # noqa: E402
    EXPECT_2XX_OR_404,
    EXPECT_2XX_OR_503,
    EXPECT_401_UNAUTH,
    FORBIDDEN_POST_PREFIXES,
    PAID_PREFIXES,
    PHASE,
    Probe,
    build_probes,
    dry_run_catalog,
    evaluate,
    run_feature_sweep,
)


class _FakeClient:
    """In-memory BossClient stand-in. Never opens a socket."""

    def __init__(self, status_for: dict[str, int] | None = None) -> None:
        self.project_id = "boss-dogfood"
        self.actor = "executive_console"
        self.api_key = "test-key-must-never-appear"
        self.calls: list[tuple[str, str, bool]] = []
        self.status_for = status_for or {}

    def request(
        self,
        method: str,
        path: str,
        *,
        query=None,
        body=None,
        timeout=None,
        authenticate: bool = True,
    ):
        self.calls.append((method.upper(), path, authenticate))
        for prefix, status in self.status_for.items():
            if path == prefix or path.startswith(prefix.rstrip("/") + "/") or path.startswith(prefix):
                return status, {"items": []}, 3
        return 200, {"items": [{"id": "x"}]}, 2


def test_dry_run_catalog_count_and_required_surfaces():
    catalog = dry_run_catalog()
    assert catalog["count"] >= 25
    assert catalog["required"] >= 20
    assert catalog["count"] == len(catalog["names"])
    names = " ".join(catalog["names"])
    assert "edition" in names
    assert "providers" in names
    assert "benchmark-apps" in names
    assert "identity" in names


def test_evaluate_200_ok_500_not_ok():
    probe = Probe(name="core.edition", path="/v1/edition")
    ok, detail = evaluate(probe, 200, {"edition_id": "community"})
    assert ok is True
    assert "200" in detail

    ok, detail = evaluate(probe, 500, {"detail": "boom"})
    assert ok is False
    assert "500" in detail


def test_evaluate_404_on_2xx_or_404_is_ok():
    probe = Probe(
        name="paid.marketplace",
        path="/v1/marketplace/listings",
        expect=EXPECT_2XX_OR_404,
    )
    ok, detail = evaluate(probe, 404, {"detail": "Not Found"})
    assert ok is True
    assert "404" in detail

    ok, _ = evaluate(probe, 200, {"items": []})
    assert ok is True

    ok, _ = evaluate(probe, 500, {})
    assert ok is False


def test_paid_probes_expect_2xx_or_404():
    probes = build_probes("boss-dogfood")
    paid = [
        probe
        for probe in probes
        if probe.name.startswith("paid.")
        or any(probe.path.startswith(prefix) for prefix in PAID_PREFIXES)
    ]
    assert len(paid) >= 4
    assert {probe.expect for probe in paid} == {EXPECT_2XX_OR_404}
    assert all(probe.required for probe in paid)
    covered = " ".join(probe.path for probe in paid)
    for prefix in PAID_PREFIXES:
        assert prefix in covered


def test_evaluate_optional_503_warn_not_fail():
    probe = Probe(
        name="optional.elastic-status",
        path="/v1/elastic/status",
        required=False,
        expect=EXPECT_2XX_OR_503,
    )
    ok, detail = evaluate(probe, 503, {"detail": "schema missing"})
    assert ok is True
    assert "WARN" in detail

    ok, _ = evaluate(probe, 500, {})
    assert ok is False


def test_evaluate_401_unauth():
    probe = Probe(
        name="unauth.projects",
        path="/v1/projects",
        expect=EXPECT_401_UNAUTH,
        authenticate=False,
    )
    assert evaluate(probe, 401)[0] is True
    assert evaluate(probe, 403)[0] is True
    assert evaluate(probe, 200, {"items": []})[0] is False


def test_only_safe_post_is_deadletter_classify():
    posts = [probe for probe in build_probes("boss-dogfood") if probe.method.upper() == "POST"]
    assert len(posts) == 1
    classify = posts[0]
    assert classify.path == "/v1/deadletters/classify"
    assert classify.body is not None
    assert classify.body.get("escalate_model") is False
    for probe in posts:
        assert not any(probe.path.startswith(prefix) for prefix in FORBIDDEN_POST_PREFIXES)


def test_github_bindings_use_canonical_path():
    paths = {probe.path for probe in build_probes("p")}
    assert "/v1/github/repository-bindings" in paths
    assert "/v1/github/bindings" not in paths
    assert "/v1/identity-providers" in paths


def test_run_feature_sweep_sets_phase_and_never_logs_key():
    client = _FakeClient(
        status_for={
            "/v1/account-connect": 404,
            "/v1/marketplace": 404,
            "/v1/self-improvement": 404,
            "/v1/enterprise/clusters": 404,
            "/v1/token-efficiency/scorecard": 503,
            "/v1/elastic/status": 503,
            "/v1/worker-autoscaling/status": 503,
            "/v1/cloud/status": 503,
        }
    )
    checks = run_feature_sweep(client)
    assert checks
    assert all(check.phase == PHASE == "features" for check in checks)
    required_failed = [check.name for check in checks if check.required and not check.ok]
    assert required_failed == []
    dumped = str([check.to_dict() for check in checks])
    assert client.api_key not in dumped
    health = next(call for call in client.calls if call[1] == "/health")
    assert health[2] is False
    assert any(method == "POST" and path.endswith("/classify") for method, path, _ in client.calls)
