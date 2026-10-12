"""The redactor knows every credential shape as one class, keeps names, spares look-alikes,
and stays linear on long text (D2)."""
import time

import pytest

from boss_engine.services.redaction import redact, redact_secrets

MASKED = [
    ("export ANTHROPIC_API_KEY=sk-ant-api03-AbCdEfGhIjKlMnOpQrSt-_uv", "export ANTHROPIC_API_KEY=[SECRET]"),
    ("rotated to sk-ant-admin01-AbCdEfGhIjKlMnOpQrSt", "rotated to [SECRET]"),
    ("sk-proj-Ab12Cd34Ef56Gh78Ij90", "[SECRET]"),
    ("key sk-Qw12Er34Ty56Ui78Op90As12", "key [SECRET]"),
    ("xoxb-2402-1180-Zq8xWv7uTs6r", "[SECRET]"),
    ("xoxp-2402-1180-Zq8xWv7uTs6r", "[SECRET]"),
    ("ghp_Zq8xWv7uTs6rQp5oNm4lKj3iHg2f", "[SECRET]"),
    ("ghs_Zq8xWv7uTs6rQp5oNm4lKj3iHg2f", "[SECRET]"),
    ("github_pat_11AAQ7ZPY0Zq8xWv7uTs6r_Qp5o", "[SECRET]"),
    ("AKIAZQ8XWV7UTS6RQP5O", "[AWS_KEY]"),
    ("glpat-" + ("fixture0" * 3), "[SECRET]"),
    ("AIzaSyZq8xWv7uTs6rQp5oNm4lKj3iHg2fEd1cBa0", "[SECRET]"),
    ("DB_PASSWORD=hunter2!x", "DB_PASSWORD=[SECRET]"),
    ('{"password": "hunter2xx"}', '{"password": "[SECRET]"}'),
    ("GITHUB_TOKEN: abcd1234", "GITHUB_TOKEN: [SECRET]"),
    ("client_secret=abcdefgh", "client_secret=[SECRET]"),
    ("STRIPE_KEY=whsec_abcdef", "STRIPE_KEY=[SECRET]"),
    ("-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaA\n-----END OPENSSH PRIVATE KEY-----", "[PRIVATE_KEY]"),
    ("-----BEGIN PRIVATE KEY-----\nMIIEv (cut off", "[PRIVATE_KEY]"),
    ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJib3NzIn0.Zq8xWv7uTs6rQp5o", "[JWT]"),
    ("Authorization: Bearer abcdefghijklmnopqrstuvwxyz", "Authorization: [BEARER]"),
]

SPARED = ["max_total_tokens_per_call=64000", "key=code_agent_max_turns", "tokens: 1430",
          "the token budget is advisory", "task-runner ok", "ask-for-help", "desk-abcdefghijklmnopqrstuvwxyz",
          "skill_or_model_id claude-sonnet", "monkey=banana", "wfr_0e3f7afe77", "crun_0f24dc9c7a39440a"]


@pytest.mark.parametrize("raw,want", MASKED)
def test_every_credential_shape_is_masked(raw, want):
    assert redact_secrets(raw) == want
    assert redact(raw)[0] == want


@pytest.mark.parametrize("raw", SPARED)
def test_look_alikes_are_spared(raw):
    assert redact_secrets(raw) == raw


def test_masking_is_idempotent():
    once = redact_secrets("export API_KEY=sk-ant-api03-AbCdEfGhIjKlMnOpQrSt")
    assert redact_secrets(once) == once


def test_long_text_stays_fast():
    for text in ("a" * 20000, "abc_" * 5000, ("foo_bar.baz-" * 2000) + "tokenx=", "npm test " * 5000):
        start = time.perf_counter()
        redact(text)
        assert time.perf_counter() - start < 0.5
