"""BOSSAPP-1264: show region, log retention, and whether the provider may train
before a prompt runs. A missing fact is the word unknown. Nothing here invents
a region or a training flag, and a repeat of the same run does not start twice.
"""
from __future__ import annotations

import inspect

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from boss_engine.services.provider_terms import (
    DisclosureMissing,
    ProviderTerms,
    TermsNotShown,
    reset_provider_terms,
)


@pytest.fixture()
def terms() -> ProviderTerms:
    return ProviderTerms()


def test_an_unrecorded_provider_is_unknown_on_every_fact(terms: ProviderTerms) -> None:
    facts = terms.facts("proj_1264", "openai")
    assert facts == {
        "region": "unknown",
        "log_retention": "unknown",
        "may_train": "unknown",
    }
    again = terms.facts("proj_1264", "Anthropic")
    assert again["region"] == "unknown"
    assert again["may_train"] == "unknown"
    assert "us-" not in again["region"]


def test_a_recorded_region_does_not_invent_the_other_facts(terms: ProviderTerms) -> None:
    terms.record("proj_1264", "acme", region="eu-central-1")
    facts = terms.facts("proj_1264", "ACME")
    assert facts["region"] == "eu-central-1"
    assert facts["log_retention"] == "unknown"
    assert facts["may_train"] == "unknown"
    terms.record("proj_1264", "acme", log_retention="0")
    assert terms.facts("proj_1264", "acme")["log_retention"] == "0"
    assert terms.facts("proj_1264", "acme")["may_train"] == "unknown"


def test_a_training_flag_is_only_an_explicit_yes_or_no(terms: ProviderTerms) -> None:
    terms.record("proj_1264", "acme", may_train=True)
    assert terms.facts("proj_1264", "acme")["may_train"] == "yes"
    assert terms.facts("proj_1264", "acme")["region"] == "unknown"
    terms.record("proj_1264", "acme", may_train="NO")
    assert terms.facts("proj_1264", "acme")["may_train"] == "no"
    terms.record("proj_1264", "acme", may_train=False)
    assert terms.facts("proj_1264", "acme")["may_train"] == "no"
    with pytest.raises(ValueError, match="yes or no"):
        terms.record("proj_1264", "acme", may_train="sometimes")
    with pytest.raises(ValueError, match="yes or no"):
        terms.record("proj_1264", "acme", may_train=1)
    assert terms.facts("proj_1264", "acme")["may_train"] == "no"
    terms.record("proj_1264", "acme", may_train="unknown")
    assert terms.facts("proj_1264", "acme")["may_train"] == "unknown"
    terms.record("proj_1264", "acme", region="   ")
    assert terms.facts("proj_1264", "acme")["region"] == "unknown"


def test_the_run_does_not_start_until_the_three_facts_are_shown(terms: ProviderTerms) -> None:
    terms.record("proj_1264", "acme", region="eu-central-1", may_train="yes")
    with pytest.raises(ValueError, match="prompt"):
        terms.disclose("proj_1264", "acme", "   ")
    first = terms.disclose("proj_1264", "acme", "say hello")
    assert first["shown"] is False
    assert first["region"] == "eu-central-1"
    assert first["log_retention"] == "unknown"
    assert first["may_train"] == "yes"
    with pytest.raises(TermsNotShown, match="not shown"):
        terms.start_run("proj_1264", first["disclosure_id"])
    shown = terms.mark_shown("proj_1264", first["disclosure_id"])
    assert shown["shown"] is True
    assert shown["region"] == "eu-central-1"
    assert shown["log_retention"] == "unknown"
    assert shown["may_train"] == "yes"
    started = terms.start_run("proj_1264", first["disclosure_id"])
    assert started["started"] is True
    assert started["deduped"] is False
    assert started["vendor_called"] is False
    assert started["prompt"] == "say hello"
    assert started["region"] == "eu-central-1"
    assert started["may_train"] == "yes"


def test_a_repeat_of_the_same_run_does_not_start_twice(terms: ProviderTerms) -> None:
    disc = terms.disclose("proj_1264", "acme", "say hello")
    same = terms.disclose("proj_1264", "acme", "  say   hello ")
    assert same["disclosure_id"] == disc["disclosure_id"]
    terms.mark_shown("proj_1264", disc["disclosure_id"])
    first = terms.start_run("proj_1264", disc["disclosure_id"])
    second = terms.start_run("proj_1264", disc["disclosure_id"])
    assert second["deduped"] is True
    assert second["run_id"] == first["run_id"]
    assert second["vendor_called"] is False


def test_what_was_shown_stays_what_the_run_uses(terms: ProviderTerms) -> None:
    terms.record("proj_1264", "acme", region="eu-central-1", log_retention="30 days")
    shown_disc = terms.disclose("proj_1264", "acme", "say hello")
    other = terms.disclose("proj_1264", "acme", "say goodbye")
    assert other["disclosure_id"] != shown_disc["disclosure_id"]
    terms.mark_shown("proj_1264", shown_disc["disclosure_id"])
    terms.record("proj_1264", "acme", region="ap-southeast-2", may_train="no")
    started = terms.start_run("proj_1264", shown_disc["disclosure_id"])
    assert started["region"] == "eu-central-1"
    assert started["log_retention"] == "30 days"
    assert started["may_train"] == "unknown"
    fresh = terms.disclose("proj_1264", "acme", "say hello")
    assert fresh["disclosure_id"] != shown_disc["disclosure_id"]
    assert fresh["region"] == "ap-southeast-2"
    assert fresh["shown"] is False
    with pytest.raises(TermsNotShown):
        terms.start_run("proj_1264", fresh["disclosure_id"])
    with pytest.raises(TermsNotShown):
        terms.start_run("proj_1264", other["disclosure_id"])


def test_a_missing_disclosure_is_a_visible_error(terms: ProviderTerms) -> None:
    with pytest.raises(DisclosureMissing):
        terms.start_run("proj_1264", "ptd_missing")
    disc = terms.disclose("proj_1264", "acme", "say hello")
    with pytest.raises(DisclosureMissing):
        terms.mark_shown("other_proj", disc["disclosure_id"])
    with pytest.raises(ValueError, match="provider"):
        terms.facts("proj_1264", "  ")
    with pytest.raises(ValueError, match="project"):
        terms.disclose("  ", "acme", "say hello")


def test_the_service_ships_no_region_and_no_training_catalog() -> None:
    import boss_engine.services.provider_terms as pt

    src = inspect.getsource(pt).lower()
    for needle in ("us-east", "us-west", "eu-west", "eu-central", "openai", "anthropic", "azure"):
        assert needle not in src
    compact = src.replace(" ", "")
    assert "may_train=true" not in compact
    assert "may_train=false" not in compact
    run_src = inspect.getsource(pt.ProviderTerms.start_run).lower()
    for needle in ("httpx", "requests", "urllib", "socket", "http.client"):
        assert needle not in run_src


@pytest.fixture()
def client() -> TestClient:
    reset_provider_terms()
    from apps.api.app.routers.gap_1264 import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_http_refuses_the_run_until_the_three_facts_are_shown(client: TestClient) -> None:
    bare = client.get("/v1/provider-terms", params={"project_id": "proj_1264", "provider": "openai"})
    assert bare.status_code == 200
    assert bare.json()["region"] == "unknown"
    assert bare.json()["log_retention"] == "unknown"
    assert bare.json()["may_train"] == "unknown"

    recorded = client.post("/v1/provider-terms/record", json={
        "project_id": "proj_1264",
        "provider": "acme",
        "log_retention": "30 days",
    })
    assert recorded.status_code == 200
    body = recorded.json()
    assert body["log_retention"] == "30 days"
    assert body["region"] == "unknown"
    assert body["may_train"] == "unknown"

    bad_flag = client.post("/v1/provider-terms/record", json={
        "project_id": "proj_1264",
        "provider": "acme",
        "may_train": "sometimes",
    })
    assert bad_flag.status_code == 422
    assert "yes or no" in bad_flag.json()["detail"]

    smuggled = client.post("/v1/provider-terms/disclose", json={
        "project_id": "proj_1264",
        "provider": "acme",
        "prompt": "say hello",
        "region": "eu-central-1",
    })
    assert smuggled.status_code == 422

    disclosed = client.post("/v1/provider-terms/disclose", json={
        "project_id": "proj_1264",
        "provider": "acme",
        "prompt": "say hello",
    })
    assert disclosed.status_code == 200
    snap = disclosed.json()
    assert snap["shown"] is False
    assert snap["region"] == "unknown"
    assert snap["log_retention"] == "30 days"
    assert snap["may_train"] == "unknown"
    disclosure_id = snap["disclosure_id"]

    early = client.post("/v1/provider-terms/run", json={
        "project_id": "proj_1264",
        "disclosure_id": disclosure_id,
    })
    assert early.status_code == 409
    assert "not shown" in early.json()["detail"]

    shown = client.post("/v1/provider-terms/shown", json={
        "project_id": "proj_1264",
        "disclosure_id": disclosure_id,
    })
    assert shown.status_code == 200
    assert shown.json()["shown"] is True
    assert shown.json()["region"] == "unknown"
    assert shown.json()["log_retention"] == "30 days"
    assert shown.json()["may_train"] == "unknown"

    started = client.post("/v1/provider-terms/run", json={
        "project_id": "proj_1264",
        "disclosure_id": disclosure_id,
    })
    assert started.status_code == 200
    run = started.json()
    assert run["started"] is True
    assert run["deduped"] is False
    assert run["vendor_called"] is False
    assert run["log_retention"] == "30 days"
    assert run["may_train"] == "unknown"

    again = client.post("/v1/provider-terms/run", json={
        "project_id": "proj_1264",
        "disclosure_id": disclosure_id,
    })
    assert again.status_code == 200
    assert again.json()["deduped"] is True
    assert again.json()["run_id"] == run["run_id"]

    missing = client.post("/v1/provider-terms/run", json={
        "project_id": "proj_1264",
        "disclosure_id": "ptd_missing",
    })
    assert missing.status_code == 404
