"""A secret key with slashes in it cannot be stored.

    Could not store the client: infisical returned 404 for PATCH
    /api/v3/secrets/raw/channel%2Fboss-dogfood%2Fgoogle%2Foauth_client

`channel/{project}/google/oauth_client` reads like a path and is not one. The backend
percent-encodes the key into a single URL segment; Infisical decodes %2F back into separators
before routing, so the request no longer matches the one-segment :secretName route and 404s.

Every key that has ever stored successfully here is flat — BOSS_SUBSCRIPTION_CODEX_AUTH_JSON,
BOSS_BOSS_DOGFOOD_GITHUB_GITHUB. This was the first of its shape ever attempted, and no test
covered a key containing a slash.

The message named PATCH because put() had already masked the POST: any failed create was
treated as "already there" and retried as an update, which failed identically. The operator
was shown the wrong verb, the wrong cause, and an implication that the secret existed.
"""

from __future__ import annotations

import inspect

from boss_engine.services.channel_oauth import _client_ref_key
from boss_engine.services.secret_backends import InfisicalBackend
from boss_engine.services.secrets_service import ref_key_for


def test_the_google_client_key_has_no_slashes():
    assert "/" not in _client_ref_key("boss-dogfood")


def test_it_uses_the_same_builder_as_every_working_key():
    """Not a second convention that happens to work — the one that already does."""
    assert _client_ref_key("boss-dogfood") == ref_key_for("channel_boss-dogfood",
                                                          "google_oauth_client")


def test_the_key_still_says_what_it_is_for():
    key = _client_ref_key("boss-dogfood")
    assert "CHANNEL" in key and "GOOGLE" in key and "BOSS_DOGFOOD" in key


def test_it_is_deterministic_so_a_rotation_lands_on_the_same_key():
    assert _client_ref_key("boss-dogfood") == _client_ref_key("boss-dogfood")


def test_two_projects_do_not_collide():
    assert _client_ref_key("boss-dogfood") != _client_ref_key("other-project")


def test_a_failed_create_is_no_longer_reported_as_a_failed_update():
    """The operator was shown PATCH for a problem that happened on POST."""
    src = inspect.getsource(InfisicalBackend.put)
    assert "NOT EVERY FAILED CREATE IS A DUPLICATE" in src
    assert "create failed with" in src and "update then failed with" in src


def test_the_genuine_upsert_path_is_untouched():
    """A rotated credential takes the create-then-update branch every time and must not raise."""
    src = inspect.getsource(InfisicalBackend.put)
    assert "a rotated credential takes this branch" in src
    # The PATCH still runs; only its FAILURE is now reported instead of swallowed.
    assert 'self._request("PATCH"' in src
