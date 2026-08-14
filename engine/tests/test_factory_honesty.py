"""Read-only sealed-slice honesty: empty is unknown, simulated is not a live PR."""

from __future__ import annotations

from boss_engine.services.factory_honesty import (
    UNKNOWN_LABEL,
    live_pr_success_from_counts,
)


def test_empty_sample_is_unknown_not_a_percent():
    payload = live_pr_success_from_counts(successes=0, attempts=0)
    assert payload["rate"] is None
    assert payload["label"] == UNKNOWN_LABEL
    assert payload["proven"] is False
    assert payload["sample_size"] == 0
    assert payload["successes"] == 0
    assert "%" not in payload["label"]


def test_zero_attempts_with_spurious_successes_stays_unknown():
    payload = live_pr_success_from_counts(successes=3, attempts=0)
    assert payload["rate"] is None
    assert payload["label"] == UNKNOWN_LABEL


def test_existing_receipts_report_k_of_n_not_a_bare_percent():
    payload = live_pr_success_from_counts(successes=1, attempts=2)
    assert payload["rate"] == 0.5
    assert payload["sample_size"] == 2
    assert payload["successes"] == 1
    assert payload["proven"] is False
    assert payload["label"] == "1/2 from existing receipts (not a sealed weekly proof)"
    assert "50%" not in payload["label"]
    assert "87%" not in payload["label"]
