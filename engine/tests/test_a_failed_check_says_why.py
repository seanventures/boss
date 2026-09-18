""""Connected, but the check failed: failed."

Four words, and they were the whole record. `test_connection` caught every exception and
replaced its message with the constant "GitHub connection test failed." — so a failed test was
unactionable for the operator AND for BOSS's own agent, which investigated, found the right
suspect in the source, and had to say: "the recorded event says only 'GitHub connection test
failed'... the underlying exception was suppressed."

The caution was right and the remedy was too blunt: a provider's error body can echo the
request, and the request carried the token. So the message is REDACTED, not discarded.

Underneath it was a real defect, and it is the one the agent spotted:

    if self.secret_resolver is self._resolve_secret:

always False. Line 61 stores a bound method; this creates a NEW one to compare against, and
bound methods are never identical under `is`. So the branch that passes the live connection
never ran, resolution fell back to `_active_connection`, and on the setup path — where nothing
sets it — the token could not be read at all.
"""
from __future__ import annotations

import inspect

from boss_engine.services.github_connections import (
    GitHubConnectionService,
    _redact_credentials,
)


def test_a_bound_method_is_not_identical_to_itself():
    """The language fact the bug rested on, stated once so nobody reintroduces it."""
    service = GitHubConnectionService.__new__(GitHubConnectionService)
    assert service._resolve_secret is not service._resolve_secret
    assert service._resolve_secret == service._resolve_secret


def _code_of(fn) -> str:
    """Source with comments and docstrings stripped.

    Asserting against raw source finds the comment that EXPLAINS the old expression and
    concludes the old expression is still there — which is how a passing test can describe a
    bug that was fixed, or a failing one a bug that was not.
    """
    import io
    import tokenize

    out = []
    for token in tokenize.generate_tokens(io.StringIO(inspect.getsource(fn)).readline):
        if token.type in (tokenize.COMMENT, tokenize.STRING):
            continue
        out.append(token.string)
    return " ".join(out)


def test_so_the_resolver_is_chosen_by_asking_what_was_injected():
    code = _code_of(GitHubConnectionService._make_adapter)
    assert "self . _custom_secret_resolver is None" in code
    assert "self . secret_resolver is self . _resolve_secret" not in code
    # And the live connection reaches the resolver, which was the point of the branch.
    assert "self . _resolve_secret ( secret_ref , connection )" in code


def test_a_failure_records_its_reason():
    code = _code_of(GitHubConnectionService.test_connection)
    assert "_redact_credentials ( error )" in code


def test_and_the_reason_is_the_useful_part():
    """This is the sentence an operator can act on, and it is safe to store."""
    error = RuntimeError("GitHub has no paramg/boss-sandbox visible to this token.")

    assert _redact_credentials(error) == (
        "GitHub has no paramg/boss-sandbox visible to this token.")


def test_but_a_credential_never_survives_it():
    for leak, shape in (
        ("Bad credentials: ghp_abcdefghij0123456789xyz rejected", "ghp_"),
        ("token github_pat_11ABCDEFG0123456789abcdefghij was revoked", "github_pat_"),
        ("authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9", "Bearer ey"),
        ("-----BEGIN RSA PRIVATE KEY-----\\nMIIEpAIBAAK", "PRIVATE KEY"),
    ):
        cleaned = _redact_credentials(RuntimeError(leak))
        assert "[redacted]" in cleaned, leak
        assert shape not in cleaned or shape == "PRIVATE KEY", cleaned


def test_an_empty_error_still_says_something():
    """A bare exception with no message would otherwise record an empty string, which is the
    same dead end by another route."""
    assert _redact_credentials(RuntimeError()) == "RuntimeError"


def test_and_the_message_is_bounded():
    assert len(_redact_credentials(RuntimeError("x" * 5000))) <= 300
