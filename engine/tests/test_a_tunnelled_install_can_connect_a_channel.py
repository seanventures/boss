"""A BOSS reached through an SSH tunnel can save a localhost redirect.

On a private VM the browser's only way to BOSS's API is http://localhost:<port>, and Google
accepts http://localhost redirects for exactly this. The save refused anything but https, so a
tunnelled install could not connect a YouTube channel at all ("redirect_uri must be https").
"""
import pytest

from boss_engine.config import Settings
from boss_engine.services import channel_oauth
from boss_engine.services.channel_oauth import ChannelOAuthError, _is_loopback_http


@pytest.mark.parametrize("uri", [
    "http://localhost:8000/v1/channels/youtube/callback",
    "http://127.0.0.1:8000/v1/channels/youtube/callback",
    "http://[::1]:8000/v1/channels/youtube/callback",
])
def test_loopback_http_is_allowed(uri):
    assert _is_loopback_http(uri)


@pytest.mark.parametrize("uri", [
    "http://boss-api.example.com/v1/channels/youtube/callback",
    "http://localhost.evil.com/v1/channels/youtube/callback",
    "http://10.0.2.15:8000/v1/channels/youtube/callback",
    "https://localhost:8000/cb",  # https is allowed by the other branch, not this one
    "ftp://localhost/cb",
])
def test_anything_else_is_not_loopback_http(uri):
    assert not _is_loopback_http(uri)


def test_plain_http_to_a_real_host_is_still_refused():
    # Refused before anything is stored, so no database is needed to prove it.
    with pytest.raises(ChannelOAuthError, match="must be https"):
        channel_oauth.save_oauth_client(
            None, Settings(), project_id="p", actor="ceo",
            client_id="x.apps.googleusercontent.com", client_secret="s",
            redirect_uri="http://boss-api.example.com/v1/channels/youtube/callback")
