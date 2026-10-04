"""A red line never shows a provider's raw text (tester round 1, K6).

`SMTPNotSupportedError…`, `HTTP 502: {"message":` and `502 Bad Gateway` reached cards and
drawers verbatim. short_error classifies; the full text stays in the tooltip. The console's
kit-copy.ts shortError applies the same rules.
"""
from __future__ import annotations

import pytest

from boss_engine.services.kit_connections import short_error


@pytest.mark.parametrize("said,shown", [
    ("HTTP 401 Bad credentials", "Token rejected"),
    ("403 Forbidden", "Token rejected"),
    ("(535, b'5.7.8 Username and Password not accepted')", "Token rejected"),
    ('POST https://api.linear.app/graphql failed HTTP 502: {"message":', "Can't reach server"),
    ("502 Bad Gateway", "Can't reach server"),
    ("web-intel: ConnectError: [Errno 61] Connection refused", "Can't reach server"),
    ("SMTPNotSupportedError: STARTTLS extension not supported by server.",
     "TLS setting mismatch"),
    ("HTTP 404: repository not found", "Not found"),
    ("No mail server set", "No mail server set"),
    ("GitHub Issues needs owner, repo, and a token.", "Missing a setting"),
    ("ValueError: something odd happened deep inside the adapter", "Test failed"),
    ("", "Test failed"),
])
def test_a_red_line_is_classified_never_raw(said, shown):
    assert short_error(said) == shown
    assert len(short_error(said).split()) <= 6
