"""Unit tests for BOSS-324 branch-protection safety gate (no Postgres / no GitHub)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.api.app.routers.safety_gates import router
from boss_engine.services.branch_protection_gate import (
    BranchProtectionGateService,
    REQUIRED_CHECKS,
    all_required_true_checks,
    dry_run_checklist,
    evaluate_protection_payload,
    reset_simulated_for_tests,
    store_simulated_checklist,
)


@pytest.fixture(autouse=True)
def _clear_sim():
    reset_simulated_for_tests()
    yield
    reset_simulated_for_tests()


def _full_protection_payload() -> dict:
    return {
        "required_pull_request_reviews": {
            "required_approving_review_count": 1,
            "dismiss_stale_reviews": True,
        },
        "required_conversation_resolution": {"enabled": True},
        "enforce_admins": {"enabled": True},
        "allow_force_pushes": {"enabled": False},
        "allow_deletions": {"enabled": False},
    }


def test_evaluate_full_protection_passes():
    result = evaluate_protection_payload(
        _full_protection_payload(), protected_flag=True
    )
    assert result["passed"] is True
    assert result["failed_ids"] == []


def test_evaluate_empty_fails_closed():
    result = evaluate_protection_payload({}, protected_flag=False)
    assert result["passed"] is False
    assert "protected" in result["failed_ids"]
    assert "required_approving_review_count_ge_1" in result["failed_ids"]


def test_evaluate_missing_conversation_resolution():
    payload = _full_protection_payload()
    del payload["required_conversation_resolution"]
    result = evaluate_protection_payload(payload, protected_flag=True)
    assert result["passed"] is False
    assert "require_conversation_resolution" in result["failed_ids"]


def test_evaluate_force_push_allowed_fails():
    payload = _full_protection_payload()
    payload["allow_force_pushes"] = {"enabled": True}
    result = evaluate_protection_payload(payload, protected_flag=True)
    assert result["passed"] is False
    assert "allow_force_pushes_false" in result["failed_ids"]


def test_dry_run_checklist_covers_required():
    checklist = dry_run_checklist()
    ids = {c["id"] for c in checklist}
    assert ids == {c["id"] for c in REQUIRED_CHECKS}
    assert all(c["status"] == "unchecked" for c in checklist)


def test_verify_no_credentials_fail_closed():
    settings = SimpleNamespace(
        target_github_owner="seanheiney",
        target_github_repo="boss-sandbox",
        github_token=None,
    )
    svc = BranchProtectionGateService(settings)
    result = svc.verify()
    assert result["live_demo_ready"] is False
    assert result["live"]["error"] == "no_github_credentials"
    assert result["blocker"]
    assert result["checklist"] is not None


def test_verify_live_protection_via_injected_request():
    settings = SimpleNamespace(
        target_github_owner="seanheiney",
        target_github_repo="boss-sandbox",
        github_token="fake",
    )

    def request_fn(method, path):
        assert method == "GET"
        assert "boss-sandbox" in path
        return _full_protection_payload()

    svc = BranchProtectionGateService(settings, request_fn=request_fn)
    result = svc.verify()
    assert result["live_demo_ready"] is True
    assert result["live"]["passed"] is True
    assert result["blocker"] is None


def test_verify_entitlement_403_fail_closed():
    class FakeHTTPError(Exception):
        def __init__(self):
            self.response = SimpleNamespace(status_code=403)
            super().__init__(
                "Upgrade to GitHub Pro or make this repository public to enable this feature."
            )

    settings = SimpleNamespace(
        target_github_owner="seanheiney",
        target_github_repo="boss-sandbox",
        github_token="fake",
    )

    def request_fn(method, path):
        raise FakeHTTPError()

    svc = BranchProtectionGateService(settings, request_fn=request_fn)
    result = svc.verify()
    assert result["live_demo_ready"] is False
    assert result["live"]["entitlement_blocked"] is True
    assert "entitlement" in (result["blocker"] or "").lower() or "Pro" in (
        result["blocker"] or ""
    )


def test_simulated_all_required_enables_live_demo_ready_for_tests():
    settings = SimpleNamespace(
        target_github_owner="seanheiney",
        target_github_repo="boss-sandbox",
        github_token=None,
    )
    record = store_simulated_checklist(
        owner="seanheiney",
        repo="boss-sandbox",
        branch="main",
        checks=all_required_true_checks(),
        actor="test",
        note="unit test",
    )
    assert record["satisfies_requirements"] is True
    svc = BranchProtectionGateService(settings)
    result = svc.verify(accept_simulated=True)
    assert result["live_demo_ready"] is True
    assert result["simulated"] is not None

    # Without accepting simulated, still fail closed
    result2 = svc.verify(accept_simulated=False)
    assert result2["live_demo_ready"] is False


def test_partial_simulated_does_not_enable_ready():
    settings = SimpleNamespace(
        target_github_owner="seanheiney",
        target_github_repo="boss-sandbox",
        github_token=None,
    )
    store_simulated_checklist(
        owner="seanheiney",
        repo="boss-sandbox",
        branch="main",
        checks={"protected": True},  # incomplete
        actor="test",
    )
    svc = BranchProtectionGateService(settings)
    result = svc.verify()
    assert result["live_demo_ready"] is False


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as c:
        yield c


def test_api_checklist(client):
    r = client.get("/v1/safety/branch-protection/checklist")
    assert r.status_code == 200
    body = r.json()
    assert len(body["required_checks"]) == len(REQUIRED_CHECKS)


def test_api_verify_fail_closed(client, monkeypatch):
    # Ensure no accidental live token
    monkeypatch.delenv("BOSS_GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    r = client.get(
        "/v1/safety/branch-protection",
        params={"owner": "seanheiney", "repo": "boss-sandbox", "branch": "main"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["live_demo_ready"] is False


def test_api_simulate_all_required(client, monkeypatch):
    monkeypatch.delenv("BOSS_GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    r = client.post(
        "/v1/safety/branch-protection/simulate",
        json={
            "owner": "seanheiney",
            "repo": "boss-sandbox",
            "branch": "main",
            "all_required": True,
            "actor": "test",
            "note": "simulated for unit test",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["live_demo_ready"] is True
    assert body["simulated"]["satisfies_requirements"] is True

    verify = client.get(
        "/v1/safety/branch-protection",
        params={"owner": "seanheiney", "repo": "boss-sandbox"},
    )
    assert verify.status_code == 200
    assert verify.json()["live_demo_ready"] is True
