"""Scrubbing keeps every credential shape out of the knowledge store (BOSSAPP-622).

Breaks covered: a Stripe-style key with a mode segment (sk_live_..., rk_test_...) passed the
secret pattern, because the segment broke the run of 16 characters; GitHub fine-grained and
user tokens, Google keys, Slack webhooks, JWTs, URL passwords and secret-named assignments were
not secret shapes at all; and every 13-19 digit number was scrubbed as a card, order numbers
included. The values below are fakes built for this test.
"""
from __future__ import annotations

import pytest

from boss_engine.services.knowledge.scrub import contains_secret_marker, scrub

_FILL = "FAKEfakeFAKEfake00000000"

SECRETS = [
    "sk_live_" + _FILL,
    "sk_test_" + _FILL,
    "rk_live_" + _FILL,
    "pk_test_" + _FILL,
    "sk-proj-" + _FILL,
    "whsec_" + _FILL,
    "github_pat_11ABCDEFG0" + "a" * 30,
    "ghu_" + "b" * 30,
    "AIza" + "S" * 35,
    "https://hooks.slack.com/services/T000/B000/XXXXXXXX",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.c2lnbmF0dXJlLWZha2U",
]


@pytest.mark.parametrize("secret", SECRETS)
def test_a_credential_shape_never_survives_the_scrub(secret):
    cleaned, counts = scrub(f"the deploy used {secret} yesterday")
    assert secret not in cleaned
    assert counts
    assert not contains_secret_marker(cleaned)


def test_a_url_password_and_a_secret_assignment_are_scrubbed():
    text = "db at postgres://app:hunter22secret@db.example.com/x and password=correct-horse-battery"
    cleaned, _ = scrub(text)
    assert "hunter22secret" not in cleaned
    assert "correct-horse-battery" not in cleaned
    assert "db.example.com" in cleaned


def test_a_card_is_scrubbed_only_when_it_passes_luhn():
    cleaned, counts = scrub("card 4111 1111 1111 1111, order 1234567890123456")
    assert "4111 1111 1111 1111" not in cleaned
    assert counts.get("CARD") == 1
    assert "1234567890123456" in cleaned
