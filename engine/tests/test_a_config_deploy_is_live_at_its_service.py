"""A CONFIG DEPLOY IS LIVE AT ITS SERVICE, NOT ITS HEALTH PATH (laptop 2026-11-21).

create_config_target records the target's health URL as its domain, so the Deploy config step's
`deployed_url` was "http://2.29.2.202:8080/readyz" and the contract test bound to it would have
requested /readyz/v1/payments. Northgate's API onboarding had to hard-code its base URL.
"""
from __future__ import annotations

import inspect

from boss_engine.services import workflow_capability_runner as w


def test_a_config_target_is_live_at_its_health_url_s_origin():
    assert w.deployed_url_of(["http://2.29.2.202:8080/readyz"], "config_dir") == \
        "http://2.29.2.202:8080"
    assert w.deployed_url_of('["https://gw.example/healthz"]', "config_dir") == \
        "https://gw.example"


def test_an_app_target_keeps_its_domain_as_it_was():
    assert w.deployed_url_of(["192.0.2.7:3014"], "stack") == "http://192.0.2.7:3014"
    assert w.deployed_url_of(["https://app.example/base"], "ssh") == "https://app.example/base"
    assert w.deployed_url_of([], "config_dir") == ""


def test_the_deploy_step_s_evidence_uses_it():
    source = inspect.getsource(w._terminal_evidence)
    assert "deployed_url_of(" in source and "target_kind" in source
