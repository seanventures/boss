"""Connecting a YouTube channel the operator owns.

The shortcut this deliberately does NOT take is reusing a logged-in browser session. A Google
session cookie is the whole account — Gmail, Drive, Photos — while a token minted for
`youtube.upload` reaches one channel and does one thing. The "simpler" path stores the far more
dangerous credential, and it cannot produce a receipt: the Data API returns a video id, browser
automation returns "I believe I clicked Publish", and on a retry there is no way to tell whether
the first attempt landed.
"""

import pytest

from apps.api.app import main as api_main


PROJECT = "boss-dogfood"


def test_the_scopes_are_the_narrowest_that_can_do_the_job():
    """Scope creep here is permanent: it is what the operator consents to, once."""
    from apps.api.app.services.channel_oauth import YOUTUBE_SCOPES

    assert "https://www.googleapis.com/auth/youtube.upload" in YOUTUBE_SCOPES
    # NOT force-ssl, which also reads and writes comments, playlists and subscriptions, and
    # NOT the blanket `youtube` scope. readonly is present only so a connection can be
    # verified by reading the channel's own name back.
    assert not any(s.endswith("/youtube") for s in YOUTUBE_SCOPES)
    assert not any("force-ssl" in s for s in YOUTUBE_SCOPES)
    assert len(YOUTUBE_SCOPES) == 2


def test_an_unconfigured_install_says_which_variables_to_set(client):
    """The failure otherwise is Google's `invalid_client`, which names none of them."""
    r = client.post("/v1/channels/youtube/connect",
                    json={"project_id": PROJECT, "actor": "ceo"})
    assert r.status_code == 422
    detail = r.json()["detail"]
    # BOTH routes, because two different people hit this: a customer who should enter their own
    # client in the console, and a self-host operator who should set the variables.
    assert "Add one in the console" in detail
    for var in ("BOSS_GOOGLE_OAUTH_CLIENT_ID", "BOSS_GOOGLE_OAUTH_CLIENT_SECRET",
                "BOSS_GOOGLE_OAUTH_REDIRECT_URI"):
        assert var in detail


def test_connecting_needs_a_permission_somebody_actually_holds(client):
    """`integration_manage` granted to nobody would make this endpoint unreachable.

    That is not hypothetical — an earlier feature shipped gated on a permission no member had,
    and every call 403'd with a message about a permission that did not exist.
    """
    r = client.post("/v1/channels/youtube/connect",
                    json={"project_id": PROJECT, "actor": "ceo"})
    # 422 (unconfigured) proves the permission check PASSED. A 403 would mean it did not.
    assert r.status_code != 403

    denied = client.post("/v1/channels/youtube/connect",
                         json={"project_id": PROJECT, "actor": "not-a-member"})
    assert denied.status_code == 403


def test_an_unknown_provider_is_a_404_not_a_half_built_flow(client):
    r = client.post("/v1/channels/instagram/connect",
                    json={"project_id": PROJECT, "actor": "ceo"})
    assert r.status_code == 404


def test_the_callback_is_exempt_from_transport_auth_but_nothing_else_is():
    """A browser following Google's redirect carries no bearer token.

    So the callback authenticates on its single-use `state` nonce instead, exactly as the
    signed webhooks do. Asserted on the middleware's own path logic, because getting this
    wrong in either direction is serious: too strict makes the flow impossible, too loose
    exempts the endpoints that bind and revoke credentials.
    """
    import pathlib

    source = pathlib.Path(api_main.__file__).read_text(encoding="utf-8")
    block = source[source.index("is_channel_callback = ("):]
    block = block[:block.index(")\n")]
    assert '_parts[2] == "channels"' in block
    assert '_parts[4] == "callback"' in block
    # The exemption must be on the CALLBACK segment specifically — not on /v1/channels.
    assert "startswith" not in block


def test_a_callback_without_a_known_state_is_refused(client):
    """Replay and forgery both land here, and both must fail the same closed way."""
    r = client.get("/v1/channels/youtube/callback",
                   params={"code": "anything", "state": "never-minted-by-boss"})
    assert r.status_code == 400
    assert "expired or was already used" in r.text


def test_the_operator_pressing_cancel_is_not_reported_as_a_fault(client):
    r = client.get("/v1/channels/youtube/callback", params={"error": "access_denied"})
    assert r.status_code == 400
    assert "Not connected" in r.text
    assert "access_denied" in r.text


def test_listing_channels_never_returns_a_credential(client):
    """Nor the provenance blob, which carries the state nonce and who started the flow."""
    r = client.get("/v1/channels", params={"project_id": PROJECT})
    assert r.status_code == 200
    body = r.text.lower()
    for leak in ("refresh_token", "client_secret", "\"state\"", "secret_ref_ids"):
        assert leak not in body, f"{leak} appears in the channel list response"


def test_the_publish_capability_tops_out_where_the_step_does():
    """A connection must not be able to authorise something the step would have gated."""
    from apps.api.app.services.channel_oauth import YOUTUBE_CAPABILITIES
    from apps.api.app.services.workflow_capabilities import CAPABILITIES

    assert "video.publish" in YOUTUBE_CAPABILITIES
    assert CAPABILITIES["social_publish"].risk_tier == 3


# ---------------------------------------------------------------------------
# Publishing guardrails
# ---------------------------------------------------------------------------
#
# "We need some basic rules or skills for the youtube publishing — critical."
#
# The rules split in two, and the split is the design. A rule that only exists as TEXT is a
# suggestion to a model, and the failures these prevent — a duplicate upload, a retry storm, a
# leaked token — are exactly the ones a model under pressure talks itself out of. So the ones
# that must hold are STRUCTURAL and not editable, and the ones that should differ per team are
# editable and carry no safety weight.


def _publish_spec():
    import pathlib
    import yaml

    # parents[3] is the repo root: apps/api/app/main.py -> app -> api -> apps -> root.
    root = pathlib.Path(api_main.__file__).resolve().parents[3] / "packages" / "specpack"
    doc = yaml.safe_load((root / "skills" / "youtube.yaml").read_text(encoding="utf-8"))
    return next(s for s in doc["skills"] if s["id"] == "publish_youtube")


def test_a_failed_upload_is_never_retried():
    """iii) 'if the video upload fails, fail the workflow, dont retry multiple times'

    Not merely a low retry count — exactly one attempt. The failure may have happened AFTER
    YouTube accepted the bytes, so a second attempt can publish the same video twice.
    """
    spec = _publish_spec()
    assert spec["retry"]["max_attempts"] == 1
    assert spec["retry"]["backoff"] == "none"


def test_the_same_video_cannot_be_uploaded_twice():
    """ii) 'Dont perform duplicate upload of same video'

    Keyed on the MEDIA, not the title or the moment: retitling and re-running must collide
    with the first upload rather than produce a second video.
    """
    spec = _publish_spec()
    key = spec["policy"]["dedupe_key_template"]
    assert "{media_artifact_id}" in key
    assert "{channel_id}" in key
    assert "{title}" not in key


def test_published_videos_cannot_be_edited_or_deleted():
    """i) 'should not edit/delete the already published videos'

    Refused twice: the OAuth scope BOSS requests cannot perform them, and the skill forbids
    them by name. Two independent mechanisms, because this is the irreversible one.
    """
    from apps.api.app.services.channel_oauth import YOUTUBE_SCOPES

    spec = _publish_spec()
    forbidden = spec["permissions"]["forbidden_operations"]
    assert "videos.delete" in forbidden
    assert "videos.update" in forbidden
    # And the scope itself cannot: `youtube.upload` manages only videos this app created, and
    # force-ssl — which could — is deliberately not requested.
    assert not any("force-ssl" in s for s in YOUTUBE_SCOPES)


def test_nothing_becomes_public_without_a_person():
    """The irreversible half is separated from the job that produced the video."""
    spec = _publish_spec()
    assert spec["policy"]["default_values"]["visibility"] == "private"
    assert "public" in spec["policy"]["forbidden_values"]["visibility"]
    assert "ceo" in spec["policy"]["requires_approval"]
    assert spec["risk_tier"] == 3


def test_made_for_kids_is_required_and_has_no_default():
    """A legal declaration (COPPA), so BOSS must not guess it."""
    spec = _publish_spec()
    assert spec["input_schema"]["made_for_kids"] == "boolean"   # no '?' — required
    assert "made_for_kids" not in (spec["policy"].get("default_values") or {})


def test_quota_and_concurrency_are_bounded():
    """An upload costs 1600 of a 10,000-unit daily quota.

    Exhausting it takes the channel's whole API surface down for the day — including the read
    that verifies the connection is healthy.
    """
    spec = _publish_spec()
    assert spec["policy"]["max_concurrent_per_connection"] == 1
    assert spec["policy"]["quota_units"] == 1600


def test_publishing_requires_a_healthy_connection_to_a_real_channel():
    spec = _publish_spec()
    assert spec["policy"]["requires_connection"] == "youtube"
    assert spec["policy"]["requires_connection_health"] == "healthy"
    assert spec["policy"]["requires_receipt"] is True


def test_the_standards_are_editable_and_the_safety_rules_are_not():
    """v) and iv) as words, plus the honest boundary between the two kinds of rule.

    The standards exist to be rewritten per team. What they must never do is carry a guarantee
    on their own — so the text says so, and the structural rules above hold regardless.
    """
    import re

    from apps.api.app.services.channel_oauth import YOUTUBE_PUBLISHING_STANDARDS as raw

    # Whitespace-normalised: this is prose meant to be rewritten and rewrapped, and a test that
    # breaks when a sentence moves across a line break teaches people to stop editing it.
    std = re.sub(r"\s+", " ", raw)

    # It tells the reader which rules it is NOT.
    assert "NOT the safety rules" in std
    assert "cannot turn those off by editing this" in std
    # v) Terms of Service, named, with the concrete case that matters.
    assert "Terms of Service" in std
    assert "automating the site rather than using the official API" in std
    # iv) credentials, including the leak route people forget.
    assert "including into a message explaining an error" in std
    # i) restated where a person will read it.
    assert "Delete or edit an already published video" in std


def test_the_standards_are_seeded_as_an_ordinary_editable_skill(client, engine):
    """Seeded, not hardcoded — and only if absent, so a rewrite is never overwritten."""
    from apps.api.app.services.channel_oauth import seed_publishing_standards
    from apps.api.app.services.skill_definitions import get_skill, save_skill

    with engine.begin() as connection:
        first = seed_publishing_standards(connection, PROJECT, "ceo")
        assert first["seeded"] is True
        skill = get_skill(connection, PROJECT, "youtube_publishing_standards")
        assert skill is not None
        # Tier 0: it performs nothing, so editing it needs no approval.
        assert skill["risk_tier"] == 0
        assert "HOW THIS CHANNEL PUBLISHES" in skill["knowledge"]

        # An operator rewrites it...
        save_skill(connection, project_id=PROJECT, actor="ceo",
                   skill_key="youtube_publishing_standards",
                   label="YouTube publishing standards",
                   behaviour_kind="builtin", knowledge="Our own rules.",
                   config={"executor_id": "knowledge.only"}, risk_tier=0)
        # ...and connecting again must not replace their words.
        again = seed_publishing_standards(connection, PROJECT, "ceo")
        assert again["seeded"] is False
        assert get_skill(connection, PROJECT,
                         "youtube_publishing_standards")["knowledge"] == "Our own rules."


# ---------------------------------------------------------------------------
# A customer brings their own OAuth client
# ---------------------------------------------------------------------------
#
# "I want customers/users to upload this to BOSS and then BOSS should manage the secrets
# properly."
#
# Per PROJECT, not per installation. A single shared client would make the BOSS vendor the
# applicant for every customer's YouTube access: one Google verification covering everybody,
# one quota pool they all compete for, and one credential whose compromise reaches every
# connected channel.


def _save_client(client, **over):
    body = {
        "project_id": PROJECT, "actor": "ceo",
        "client_id": "1234567890-abcdefghijklmnop.apps.googleusercontent.com",
        "client_secret": "GOCSPX-super-secret-value",
        "redirect_uri": "https://boss-api.local.sean.network/v1/channels/youtube/callback",
    }
    body.update(over)
    return client.put("/v1/channels/youtube/client", json=body)


def test_a_project_can_bring_its_own_oauth_client(client):
    r = _save_client(client)
    assert r.status_code == 200, r.text
    assert r.json()["configured"] is True

    status = client.get("/v1/channels/youtube/client",
                        params={"project_id": PROJECT}).json()
    assert status["configured"] is True
    assert status["source"] == "project"


def test_neither_half_of_the_client_can_be_read_back(client):
    """The status endpoint exists to say IF one is configured, not WHICH one."""
    _save_client(client)
    body = client.get("/v1/channels/youtube/client",
                      params={"project_id": PROJECT}).text
    assert "GOCSPX-super-secret-value" not in body
    assert "1234567890-abcdefghijklmnop" not in body
    # A hint short enough to identify and useless to impersonate with.
    hint = client.get("/v1/channels/youtube/client",
                      params={"project_id": PROJECT}).json()["client_id_hint"]
    assert hint and len(hint.strip("…")) <= 12


def test_no_agent_can_query_the_client_out_of_the_database(client, engine):
    """It lives in the secret backend, not in a table an agent can SELECT from.

    This is the difference between a design that is secure and one that only feels secure: an
    agent that can read a credential can also print it into a message explaining an error.
    """
    from sqlalchemy import text

    _save_client(client)
    with engine.begin() as connection:
        rows = connection.execute(
            text("SELECT metadata::text FROM secret_refs WHERE project_id = :p "
                 "AND secret_kind = 'oauth_client'"),
            {"p": PROJECT},
        ).scalars().all()
    assert rows, "the reference row should exist"
    for row in rows:
        assert "GOCSPX-super-secret-value" not in row


def test_a_project_client_takes_precedence_over_the_installation_default(client, engine):
    """A customer who has entered their own must not silently keep using the vendor's."""
    from apps.api.app.services.channel_oauth import _require_client

    _save_client(client, client_id="project-owned-id", client_secret="project-owned-secret")
    with engine.begin() as connection:
        cid, csec, redirect = _require_client(api_main.settings, connection, PROJECT)
    assert cid == "project-owned-id"
    assert csec == "project-owned-secret"
    assert redirect.endswith("/v1/channels/youtube/callback")


def test_a_project_without_its_own_client_falls_back_to_the_installation(engine):
    """A self-host that set the environment variables needs no UI at all.

    Exercised on the service rather than the endpoint: `require_project_actor` crashes on a
    project that does not exist (a pre-existing rough edge, see test_audit_summary), which
    would be testing that bug rather than this fallback.
    """
    from apps.api.app.services.channel_oauth import ChannelOAuthError, _require_client

    with engine.begin() as connection:
        try:
            cid, _csec, _redirect = _require_client(
                api_main.settings, connection, "project-with-no-client-row")
        except ChannelOAuthError as error:
            # No project client AND no installation default: the message must serve both
            # audiences rather than assuming which one is reading.
            assert "Add one in the console" in str(error)
            assert "BOSS_GOOGLE_OAUTH_CLIENT_ID" in str(error)
        else:
            # An installation default IS set, so it was used. That is the fallback working.
            assert cid == (api_main.settings.google_oauth_client_id or "").strip()


def test_a_plain_http_redirect_is_refused_before_google_refuses_it(client):
    r = _save_client(client, redirect_uri="http://boss-api.local.sean.network/cb")
    assert r.status_code == 422
    assert "https" in r.json()["detail"]


def test_configuring_a_client_needs_the_integration_permission(client):
    assert _save_client(client, actor="not-a-member").status_code == 403


def test_the_status_endpoint_tells_the_operator_what_to_paste_into_google(client):
    """The commonest failure in this flow is redirect_uri_mismatch."""
    status = client.get("/v1/channels/youtube/client",
                        params={"project_id": PROJECT}).json()
    assert status["callback_path"] == "/v1/channels/youtube/callback"
