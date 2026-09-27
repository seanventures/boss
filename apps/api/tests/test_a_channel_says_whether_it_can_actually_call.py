"""A connected channel that cannot make a call must not report itself healthy.

WHAT HAPPENED. `/v1/channels` reported a YouTube channel as

    status: active, health: healthy, capabilities: [video.publish, drive.read, drive.write]

so I told the operator the workflow could genuinely run. Its first step then failed with "No
Google OAuth client for this project", and the health field had said the opposite one minute
earlier.

WHY THE TWO DISAGREE. `health_status` records how the last token exchange went and is never
revisited. The OAuth CLIENT — Client ID and secret from the operator's own Google Cloud project
— is what refreshes that token, and it lives somewhere else: a secret ref, or the installation's
environment. Lose it and every stored token is unrefreshable while the row still reads active.

A signal that confidently contradicts the thing it describes is worse than no signal at all.
"""
from apps.api.app import main as api_main


PROJECT = "boss-dogfood"


def test_a_channel_with_no_oauth_client_is_not_reported_healthy(client, monkeypatch):
    from apps.api.app.services import channel_oauth

    monkeypatch.setattr(channel_oauth, "oauth_client_status",
                        lambda *a, **k: {"configured": False, "source": None})
    body = client.get("/v1/channels", params={"project_id": PROJECT}).json()
    for channel in body["channels"]:
        assert channel["health"] == "unusable", channel
        # And it says what to do, where the operator is already looking.
        assert "no Google OAuth client" in channel["unusable_reason"]
        assert "Channels" in channel["unusable_reason"]


def test_a_channel_with_a_client_reports_its_real_health(client, monkeypatch):
    """The check must not swallow the genuine signal — a revoked token still reads revoked."""
    from apps.api.app.services import channel_oauth

    monkeypatch.setattr(channel_oauth, "oauth_client_status",
                        lambda *a, **k: {"configured": True, "source": "secret_ref"})
    body = client.get("/v1/channels", params={"project_id": PROJECT}).json()
    for channel in body["channels"]:
        assert channel["health"] != "unusable"
        assert "unusable_reason" not in channel


def test_the_capabilities_are_still_reported_either_way(client, monkeypatch):
    """What the grant COVERS is a fact about the consent, and it is true whether or not the
    client that minted it is still around. Hiding it would lose the audit trail."""
    from apps.api.app.services import channel_oauth

    monkeypatch.setattr(channel_oauth, "oauth_client_status",
                        lambda *a, **k: {"configured": False, "source": None})
    body = client.get("/v1/channels", params={"project_id": PROJECT}).json()
    for channel in body["channels"]:
        assert "capabilities" in channel and "max_risk_tier" in channel
