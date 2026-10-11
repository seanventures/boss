"""Shared strict evidence validator; paid unknown is never inferred as free."""

import math
import pytest

from boss_engine.services.architecture_spend import settled_text_cost


@pytest.mark.parametrize("bad", [True, "0", -1, 1.5, math.inf, math.nan, None])
def test_unpriced_invalid_paid_cost_is_unresolved(bad):
    with pytest.raises(RuntimeError, match="no usable paid cost"):
        settled_text_cost(
            {"model": "unpriced-owned-fixture", "cost_usd_micros": bad}, included=False
        )


@pytest.mark.parametrize("cost", [0, 3000])
def test_typed_paid_cost_is_exact(cost):
    assert settled_text_cost({"cost_usd_micros": cost}, included=False) == cost


def test_invalid_override_cannot_zero_genuinely_priced_tokens():
    priced = {"model": "gpt-4o", "prompt_tokens": 1000, "completion_tokens": 1000}
    exact = settled_text_cost(priced, included=False)
    assert exact > 0
    assert settled_text_cost({**priced, "cost_usd_micros": False}, included=False) == exact
    with pytest.raises(RuntimeError):
        settled_text_cost({**priced, "cache_read_tokens": False}, included=False)


def test_included_source_retains_equivalent_value_and_allows_missing_usage():
    assert settled_text_cost({"cost_usd_micros": 3000}, included=True) == 3000
    assert settled_text_cost({}, included=True) == 0
