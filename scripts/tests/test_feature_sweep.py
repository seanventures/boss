"""Unit tests for scripts/qa/feature_sweep.py (no network)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.qa.feature_sweep import (  # noqa: E402
    EXPECT_2XX_OR_404,
    EXPECT_2XX_OR_405,
    EXPECT_2XX_OR_503,
    EXPECT_401_UNAUTH,
    EXPECT_FREEZE_HONEST,
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
    assert catalog["count"] >= 55
    assert catalog["required"] >= 20
    assert catalog["count"] == len(catalog["names"])
    names = " ".join(catalog["names"])
    assert "edition" in names
    assert "providers" in names
    assert "resource-receipts" in names
    assert "benchmark-apps" in names
    assert "identity" in names
    assert "deadletter-remediations" in names
    assert "sandbox-echo" in names
    assert "skills-export" in names
    assert "meetings-standup" in names
    assert "assurance-seed-demo" in names
    assert "packs-export" in names
    assert "preview-fixture" in names
    assert "comms-dry-run" in names
    assert "providers-easy-connect" in names
    assert "openai-compatible-source" in names


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


def test_only_safe_posts_are_classify_and_optional_sandbox_echo():
    posts = [probe for probe in build_probes("boss-dogfood") if probe.method.upper() == "POST"]
    paths = {probe.path for probe in posts}
    assert paths == {"/v1/deadletters/classify", "/v1/diag/sandbox-echo"}
    classify = next(probe for probe in posts if probe.path.endswith("/classify"))
    assert classify.required is True
    assert classify.body is not None
    assert classify.body.get("escalate_model") is False
    echo = next(probe for probe in posts if probe.path.endswith("/sandbox-echo"))
    assert echo.required is False
    assert echo.expect == EXPECT_FREEZE_HONEST
    for probe in posts:
        assert not any(probe.path.startswith(prefix) for prefix in FORBIDDEN_POST_PREFIXES)


def test_community_add_on_probes_are_optional():
    probes = build_probes("boss-dogfood")
    community = [probe for probe in probes if probe.name.startswith("community.")]
    assert len(community) >= 16
    assert all(probe.required is False for probe in community)
    by_name = {probe.name: probe for probe in community}
    assert by_name["community.deadletter-remediations"].path == "/v1/deadletters/remediations"
    assert by_name["community.deadletter-remediations"].expect == EXPECT_2XX_OR_404
    assert by_name["community.sandbox-echo"].method == "POST"
    assert by_name["community.skills-import"].expect == EXPECT_2XX_OR_405
    assert by_name["community.meetings-standup"].expect == EXPECT_2XX_OR_405
    assert by_name["community.meetings-simulated-run"].expect == EXPECT_2XX_OR_405
    assert by_name["community.assurance-seed-demo"].expect == EXPECT_2XX_OR_405
    assert by_name["community.preview-fixture"].expect == EXPECT_2XX_OR_405
    assert by_name["community.comms-dry-run"].expect == EXPECT_2XX_OR_405
    assert by_name["community.providers-easy-connect"].expect == EXPECT_2XX_OR_405
    assert by_name["community.openai-compatible-source"].expect == EXPECT_2XX_OR_405
    posts = [probe for probe in community if probe.method.upper() == "POST"]
    assert [probe.path for probe in posts] == ["/v1/diag/sandbox-echo"]


def test_edition_probe_sends_project_id():
    edition = next(probe for probe in build_probes("boss-dogfood") if probe.name == "core.edition")
    assert edition.path == "/v1/edition"
    assert edition.query == {"project_id": "boss-dogfood"}


def test_evaluate_405_on_post_only_presence_is_ok():
    probe = Probe(
        name="community.preview-fixture",
        path="/v1/previews/fixture",
        required=False,
        expect=EXPECT_2XX_OR_405,
    )
    ok, detail = evaluate(probe, 405)
    assert ok is True
    assert "405" in detail
    assert evaluate(probe, 404)[0] is True
    assert evaluate(probe, 200, {})[0] is True
    assert evaluate(probe, 500, {})[0] is False


def test_evaluate_422_on_post_only_presence_is_ok():
    """647c742: GET /v1/packs/import and /v1/providers/easy-connect 422.

    FastAPI matches GET /v1/packs/{id} / /v1/providers/{id} and then
    rejects the missing project_id. That is presence, not a required fail.
    """
    for name, path in (
        ("community.packs-import", "/v1/packs/import"),
        ("community.providers-easy-connect", "/v1/providers/easy-connect"),
    ):
        probe = Probe(name=name, path=path, required=False, expect=EXPECT_2XX_OR_405)
        ok, detail = evaluate(probe, 422, {"detail": [{"type": "missing", "loc": ["query", "project_id"]}]})
        assert ok is True, name
        assert "422" in detail
        assert "validation" in detail
        assert evaluate(probe, 500, {})[0] is False


def test_evaluate_sandbox_echo_freeze_honest():
    probe = Probe(
        name="community.sandbox-echo",
        path="/v1/diag/sandbox-echo",
        method="POST",
        required=False,
        expect=EXPECT_FREEZE_HONEST,
    )
    assert evaluate(probe, 200, {"reached": True})[0] is True
    ok, detail = evaluate(probe, 422, {"detail": "frozen"})
    assert ok is True
    assert "frozen" in detail
    ok, detail = evaluate(probe, 503, {"detail": "no host"})
    assert ok is True
    assert "no host" in detail
    assert evaluate(probe, 404)[0] is True
    assert evaluate(probe, 405)[0] is True
    assert evaluate(probe, 500, {})[0] is False


def test_github_bindings_use_canonical_path():
    paths = {probe.path for probe in build_probes("p")}
    assert "/v1/github/repository-bindings" in paths
    assert "/v1/github/bindings" not in paths
    assert "/v1/identity-providers" in paths
    assert "/v1/resource-receipts" in paths
    assert "/v1/deadletters/remediations" in paths
    assert "/v1/diag/sandbox-echo" in paths
    assert "/v1/skills" in paths
    assert "/v1/packs" in paths
    assert "/v1/previews/fixture" in paths
    assert "/v1/meetings/standup" in paths
    assert "/v1/assurance/seed-demo" in paths
    assert "/v1/providers/easy-connect" in paths
    assert "/v1/entitlements/sources/approved_local_model_host/openai-compatible" in paths


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
            "/v1/deadletters/remediations": 404,
            "/v1/diag/sandbox-echo": 422,
            "/v1/skills/import": 405,
            "/v1/meetings/standup": 405,
            "/v1/meetings/simulated-run": 405,
            "/v1/assurance/seed-demo": 405,
            "/v1/packs/import": 405,
            "/v1/previews/fixture": 405,
            "/v1/comms/outbox/_qa_absent/dry-run": 405,
            "/v1/providers/easy-connect": 405,
            "/v1/github/easy-connect": 405,
            "/v1/cloud/easy-connect": 405,
            "/v1/entitlements/sources/approved_local_model_host/openai-compatible": 405,
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
    posts = {(method, path) for method, path, _ in client.calls if method == "POST"}
    assert posts == {
        ("POST", "/v1/deadletters/classify"),
        ("POST", "/v1/diag/sandbox-echo"),
    }
    assert any(path == "/v1/deadletters/remediations" for _, path, _ in client.calls)
    assert any(path == "/v1/edition" for _, path, _ in client.calls)
