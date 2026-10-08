"""Every backend BOSS can write to has to be one it will record a reference for.

Measured in the console, on the step a customer does first:

    credential_ref kind must be one of: env, local_runner, local_vault,
    platform_secret_store, vault.

The token was written to Infisical successfully. What failed was recording the POINTER to it,
because `ALLOWED_REF_KINDS` kept its own private list of stores and Infisical was not on it —
nor was `database`. Those are precisely the two configurations a customer has: the installer
provisions Infisical, and an install with no store at all falls back to Postgres. So "paste a
GitHub token" could not complete on either, while the `platform_secret_store` and `vault`
paths that only a platform operator uses worked fine.

Reading was never affected, which is why this survived: `resolve_secret_ref` asks
`backend_for`, and that has always known every backend. Only the validation disagreed.
"""
from __future__ import annotations

import pytest

from boss_engine.config import get_settings
from boss_engine.services.secret_backends import backend_for, default_secret_backend
from boss_engine.services.secret_refs import SecretRefService


#: Every backend `backend_for` can hand back, by the name a ref kind uses for it.
WRITABLE_KINDS = ("env", "vault", "local_vault", "local_runner", "platform_secret_store",
                  "infisical", "database")


@pytest.mark.parametrize("kind", WRITABLE_KINDS)
def test_a_reference_can_be_recorded_for_every_backend(kind):
    parsed = SecretRefService.parse_credential_ref(f"{kind}:BOSS_TEST_SOME_KEY")
    assert parsed == (kind, "BOSS_TEST_SOME_KEY")


@pytest.mark.parametrize("kind", WRITABLE_KINDS)
def test_and_each_one_maps_to_a_storage_backend(kind):
    """A kind the allow-list accepts and this map does not is a KeyError, not a message."""
    backend = SecretRefService._storage_backend_for_ref_kind(kind)
    assert backend in SecretRefService.ALLOWED_STORAGE_BACKENDS


@pytest.mark.parametrize("kind", WRITABLE_KINDS)
def test_and_every_one_is_a_backend_the_resolver_recognises(kind, monkeypatch):
    """The pair has to agree: a reference that validates and then cannot be read is worse than
    a refusal, because it fails later and somewhere else.

    "Recognised" is the claim, not "usable here". `backend_for` rightly refuses a store this
    machine has not configured — vault with no address, a local vault with no master key — and
    that is a different answer from "no such backend", which is the one that would mean the
    two lists had drifted apart again.
    """
    from boss_engine.services.secret_backends import SecretUnavailable

    # Backend registration does not require this host's default Postgres to be
    # running. The real database persistence path has its own tests.
    monkeypatch.setenv("BOSS_KMS_ALLOW_EPHEMERAL_MASTER", "true")

    try:
        assert backend_for(kind, get_settings()) is not None
    except SecretUnavailable as unavailable:
        assert "no secret backend for" not in str(unavailable), (
            f"{kind!r} can be recorded as a credential_ref but nothing can read it")


def test_the_backend_this_install_would_choose_is_one_it_accepts(monkeypatch):
    """THE BUG, stated as the invariant it broke. Whatever `default_secret_backend` picks is
    what the wizard writes through, so it must be a kind a reference can be recorded for."""
    monkeypatch.setenv("BOSS_KMS_ALLOW_EPHEMERAL_MASTER", "true")
    chosen = default_secret_backend(get_settings())

    assert chosen in SecretRefService.ALLOWED_REF_KINDS, (
        f"this install stores secrets in {chosen!r}, which cannot be recorded as a "
        f"credential_ref — so pasting a token succeeds and then is refused")


def test_a_kind_nobody_implements_is_still_refused():
    """The fix is a longer list, not an open one: a ref kind is the name of a store, and an
    unknown one would be recorded and then fail at read time."""
    with pytest.raises(ValueError, match="credential_ref kind must be one of"):
        SecretRefService.parse_credential_ref("dropbox:BOSS_TOKEN")


def test_an_env_reference_still_cannot_hold_a_value():
    """The one rule that matters more than the list: `env:` names a variable, never a secret."""
    with pytest.raises(ValueError, match="never raw values"):
        SecretRefService.parse_credential_ref("env:ghp_averyrealtokenvalue")


# -- and the field that takes the repository ---------------------------------------------------

def test_a_pasted_github_url_is_a_repository():
    """A person told "the repository" reaches for the thing in their address bar, because that
    is where the repository is. Refusing it with "repository must use a valid 'owner/name'
    format" makes them retype what they had already given us correctly."""
    from boss_engine.services.github_connections import GitHubConnectionService as G

    for given in ("example-user/boss-sandbox",
                  "https://github.com/example-user/boss-sandbox",
                  "http://github.com/example-user/boss-sandbox/",
                  "https://github.com/example-user/boss-sandbox.git",
                  "https://github.com/example-user/boss-sandbox/tree/main/docs",
                  "git@github.com:example-user/boss-sandbox.git",
                  "  example-user/boss-sandbox  "):
        assert G._normalize_repository(given)[2] == "example-user/boss-sandbox", given


def test_but_something_that_names_no_repository_is_still_refused():
    """Guessing would be worse than refusing: a binding to the wrong repository is a pull
    request opened somewhere nobody is looking."""
    import pytest as _pytest

    from boss_engine.services.github_connections import GitHubConnectionService as G

    for given in ("", "notarepo", "https://github.com/example-user", "/boss-sandbox",
                  "example-user/", "a b/c"):
        with _pytest.raises(ValueError, match="owner/name"):
            G._normalize_repository(given)
