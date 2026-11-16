"""A change replaces contract items only by a person's structured pick (BOSSAPP-1322, PR #161 C1).

A request only adds must-haves; `replaces: ["AC-n"]` is the one way to drop one, checked where
the person asks: contract item ids only, and only a person allowed to revise the project's
contracts. The Change it / New work picker sends it; the words of a request never do.
"""
from __future__ import annotations

from apps.api.tests.test_a_feature_builds_onto_its_app import APP_RUN, _start, _the_app


def test_a_workflow_start_refuses_replaces_from_a_non_person(client, engine):
    _the_app(engine)
    got = _start(client, {"title": "Archive tickets", "instructions": "Archive tickets.",
                          "extends_run_id": APP_RUN, "replaces": ["AC-2"]})
    assert got.status_code == 403, got.text
    assert "acceptance contract" in got.text


def test_replaces_must_name_contract_item_ids(client, engine):
    _the_app(engine)
    got = _start(client, {"title": "x", "instructions": "x", "extends_run_id": APP_RUN,
                          "replaces": ["item 2"]})
    assert got.status_code == 422, got.text


def test_replaces_needs_an_app_to_change(client, engine):
    got = _start(client, {"title": "x", "instructions": "x", "replaces": ["AC-2"]})
    assert got.status_code == 422, got.text


def test_the_follow_up_endpoint_refuses_replaces_from_a_non_person(client, engine):
    _the_app(engine)
    got = client.post(f"/v1/code-tasks/{APP_RUN}/follow-up",
                      json={"instructions": "Archive tickets.", "replaces": ["AC-2"]})
    assert got.status_code in (403, 422), got.text
    assert "acceptance contract" in got.text or "replaces" in got.text
