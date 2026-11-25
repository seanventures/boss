"""P0: the dev-autonomy policy (BOSSAPP-1441) — production always asks, a place BOSS cannot
classify is production, a stop a person set is honoured, and a project is only ever stricter
than its org."""
from __future__ import annotations

import pytest

from boss_engine.services import autonomy_policy as ap


def test_the_default_is_the_operators_word():
    policy = ap.DEFAULT
    assert (policy.dev, policy.prod, policy.unknown_remote, policy.explicit_stops) == \
        ("act", "ask", "prod", "honour")
    assert policy.dev_acts
    assert ap.describe(policy)["line"] == "Development: agents act · Production: you decide"


@pytest.mark.parametrize("raw", [{"prod": "act"}, {"unknown_remote": "dev"},
                                 {"explicit_stops": "ignore"}])
def test_fixed_fields_cannot_be_loosened(raw):
    with pytest.raises(ap.PolicyLooser):
        ap.coerce(raw)


def test_an_unknown_dev_mode_is_refused():
    with pytest.raises(ValueError):
        ap.coerce({"dev": "yolo"})


def test_a_project_may_only_be_stricter_than_its_org():
    org = ap.coerce({"dev": "act_capped", "dev_topup_usd": {"per_item": 2, "per_day": 5}})
    ap.validate_stricter(ap.coerce({"dev": "ask"}, base=org), org)
    ap.validate_stricter(ap.coerce({"dev_topup_usd": {"per_item": 1}}, base=org), org)
    with pytest.raises(ap.PolicyLooser):
        ap.validate_stricter(ap.coerce({"dev": "act"}, base=org), org)
    with pytest.raises(ap.PolicyLooser):
        ap.validate_stricter(ap.coerce({"dev_topup_usd": {"per_day": 6}}, base=org), org)


def test_a_top_up_is_bounded():
    with pytest.raises(ValueError):
        ap.coerce({"dev_topup_usd": {"per_item": 10_000}})


def test_the_tool_ceiling_rises_to_tier_two_only_under_dev_autonomy(monkeypatch):
    monkeypatch.setattr(ap, "load", lambda *a, **k: ap.DEFAULT)
    assert ap.tool_threshold(None, "p", 1) == 2
    monkeypatch.setattr(ap, "load", lambda *a, **k: ap.coerce({"dev": "ask"}))
    assert ap.tool_threshold(None, "p", 1) == 1
