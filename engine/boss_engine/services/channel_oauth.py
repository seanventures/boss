"""Connecting a social channel the operator owns, without BOSS ever holding more than it needs.

WHY OAUTH AND NOT A BROWSER SESSION
-----------------------------------
The obvious shortcut — reuse the operator's logged-in browser session and drive YouTube Studio
— inverts the thing it appears to simplify. A Google session cookie is the WHOLE account:
Gmail, Drive, Photos. A token minted for ``youtube.upload`` can do exactly one thing, to one
channel. So the "avoid OAuth" path stores a far more dangerous credential than the one it
avoids, and `media_publisher`'s first rule is that it never holds one it can read out.

It also cannot produce a receipt. BOSS's governance is one effect, one receipt, one dedupe key;
the Data API returns a video id, while automation returns "I believe I clicked Publish" — and
on a retry there is no way to tell whether the first attempt landed. That is how the same video
gets posted twice.

WHAT IS STORED, AND WHERE
-------------------------
* The REFRESH TOKEN goes to the secret backend (`secret_backends`), which is AES-GCM ciphertext
  in Postgres by default. It is referenced by a `secret_refs` row and never returned by any
  read path in this module.
* The CONNECTION is an `integration_connections` row: which capabilities it grants, what risk
  tier it tops out at, whether it is healthy. Revoking is one row, and two agents can share a
  channel without a secret being copied.
* The ACCESS TOKEN is never stored at all. It lives ~1 hour and is cheaper to mint than to
  manage; `access_token_for` exchanges the refresh token each time it is needed.

THE PENDING CONNECTION IS THE OAUTH STATE
-----------------------------------------
There is no separate session table. `start` creates the connection row already, with
``status='pending'`` and the state nonce in its provenance; `complete` finds it by that nonce.
An operator who abandons the consent screen leaves a visible pending connection rather than an
invisible orphan in a side table, and `entitlement_connection_sessions` — which is about
model-provider capacity — keeps meaning only that.
"""
from __future__ import annotations

import json
import secrets
from typing import Any, Optional
from urllib.parse import urlencode

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import Settings
from .core import emit_event, make_id, now_utc
from .secret_backends import SecretUnavailable, backend_for, default_secret_backend

#: Google's endpoints. Constants rather than settings: these are not deployment choices, and a
#: configurable token endpoint is a credential-exfiltration setting wearing a helpful face.
AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
REVOKE_ENDPOINT = "https://oauth2.googleapis.com/revoke"

#: THE NARROWEST SCOPE THAT CAN DO THE JOB.
#:
#: `youtube.upload` uploads and manages videos this app created. Deliberately NOT
#: `youtube.force-ssl`, which also reads and writes comments, playlists and subscriptions, nor
#: `youtube` itself. `youtube.readonly` is here only so the connection can be VERIFIED — a
#: channel name proves the token works without publishing anything.
YOUTUBE_SCOPES = (
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
)

INTEGRATION_KEY = "youtube"

#: What a connection to this integration is allowed to authorise. Named capabilities rather
#: than raw scopes, because a connection grants BOSS capabilities and the scopes are how Google
#: happens to express them.
YOUTUBE_CAPABILITIES = ("video.publish", "channel.read")


#: THE EDITABLE HALF OF THE RULES.
#:
#: The structural guarantees live in `packages/specpack/skills/youtube.yaml` and are enforced by
#: the runtime: one attempt, one upload per media artifact, private on arrival, an approval
#: before public, a credential no agent can read. Those are deliberately not editable — "never
#: leak the credential" must not be a toggle.
#:
#: THIS is the half that should change per team: what belongs on the channel, how a title is
#: written, which language the metadata uses. Seeded as an ordinary authored skill so it appears
#: in the Skills page and can be rewritten there, or by asking BOSS, without a deploy.
YOUTUBE_PUBLISHING_STANDARDS = """\

HOW THIS CHANNEL PUBLISHES

These are the standards the publishing agent holds to. Edit them freely — they are yours, and
they are meant to change as the channel does.

They are NOT the safety rules. Those are enforced by the runtime whatever this text says: one
upload attempt and no retry, one upload per media artifact, private on arrival, an approval
before anything becomes public, and a credential that no agent can read out. You cannot turn
those off by editing this, which is the point of them being separate.

BEFORE PUBLISHING
- Watch the actual file that will be uploaded, not the one that was edited. A re-encode that
  dropped the audio looks identical in a file listing.
- Check the title reads as a sentence a person would click, and that it matches what the video
  actually says. A title the video does not deliver is the fastest way to lose a channel's
  standing with its audience.
- Check the description's first two lines. They are what shows above the fold and in search;
  everything after them is for people already committed.
- Declare made-for-kids honestly. It is a legal declaration, not a marketing choice, and
  getting it wrong is a compliance problem rather than a metadata one.
- If the video contains music, footage, or images you did not create, say so in the request for
  approval. Content ID claims arrive after publication and are the operator's problem then.

WRITING METADATA
- Title: under 70 characters so it is not truncated in search results.
- Description: what the video covers, then any links, then hashtags. Not the reverse.
- Tags: specific over broad. Ten accurate tags beat forty hopeful ones.
- Keep the channel's language consistent. A Tamil channel's metadata should be in Tamil, with
  transliteration only where it genuinely helps discovery.

AFTER UPLOADING
- The video arrives PRIVATE. Report the URL and ask a person to watch it before it goes public.
- Never change a live video's title, description, or visibility on your own initiative. If
  something is wrong with a published video, say what is wrong and let a person decide.
- If the upload fails, stop. Report what failed and what state the channel is in. Do not try
  again — a second attempt can publish the same video twice, because the failure may have
  happened after YouTube accepted the bytes.

WHAT THIS AGENT NEVER DOES
- Delete or edit an already published video.
- Publish to a channel this project has not connected.
- Read, log, echo, or copy the channel credential, including into a message explaining an error.
- Use the channel for anything outside YouTube's Terms of Service. If a request would require
  automating the site rather than using the official API, refuse and say why.
- Reply to comments, manage subscriptions, or touch playlists. The connection has no permission
  for any of it, and asking for that permission would be a decision for the operator, not a
  convenience for the agent.
"""


def seed_publishing_standards(connection: Connection, project_id: str, actor: str) -> dict:
    """Put the editable standards in front of the operator, once.

    Seeded rather than hardcoded so it can be edited; seeded ONLY IF ABSENT so an operator who
    has rewritten it does not get their words replaced on the next connect.
    """
    from .skill_definitions import get_skill, save_skill

    key = "youtube_publishing_standards"
    if get_skill(connection, project_id, key) is not None:
        return {"skill_key": key, "seeded": False}
    save_skill(
        connection,
        project_id=project_id,
        actor=actor,
        skill_key=key,
        label="YouTube publishing standards",
        behaviour_kind="builtin",
        summary="How this channel publishes — editable house rules for the publishing agent.",
        knowledge=YOUTUBE_PUBLISHING_STANDARDS,
        config={"executor_id": "knowledge.only"},
        # Tier 0: it performs nothing. It is knowledge a step carries, which is why editing it
        # is safe and why it does not need an approval to change.
        risk_tier=0,
    )
    return {"skill_key": key, "seeded": True}


class ChannelOAuthError(RuntimeError):
    """Something the operator can fix, phrased for them."""


def _http():
    import httpx

    return httpx


def _require_client(settings: Settings) -> tuple[str, str, str]:
    """The app's OAuth client, or a sentence saying exactly what to set.

    Refusing with the three variable names is the difference between a ten-minute setup and an
    afternoon: the failure an operator hits otherwise is Google's own `invalid_client`, which
    says nothing about which of three things is missing here.
    """
    client_id = (settings.google_oauth_client_id or "").strip()
    client_secret = (settings.google_oauth_client_secret or "").strip()
    redirect = (settings.google_oauth_redirect_uri or "").strip()
    missing = [
        name for name, value in (
            ("BOSS_GOOGLE_OAUTH_CLIENT_ID", client_id),
            ("BOSS_GOOGLE_OAUTH_CLIENT_SECRET", client_secret),
            ("BOSS_GOOGLE_OAUTH_REDIRECT_URI", redirect),
        ) if not value
    ]
    if missing:
        raise ChannelOAuthError(
            "This installation has no Google OAuth client configured. Set "
            + ", ".join(missing)
            + " — create the client in Google Cloud Console, enable YouTube Data API v3, and "
            "register the redirect URI on it exactly as set here."
        )
    return client_id, client_secret, redirect


def ensure_definition(connection: Connection, project_id: str, actor: str) -> str:
    """The YouTube integration, published for this project. Idempotent."""
    from .governed_collaboration import IntegrationRegistryService

    existing = connection.execute(
        text("SELECT id FROM integration_definitions WHERE project_id = :p "
             "AND integration_key = :k AND status = 'active'"),
        {"p": project_id, "k": INTEGRATION_KEY},
    ).first()
    if existing:
        return str(existing[0])

    published = IntegrationRegistryService().publish_definition(
        connection,
        project_id=project_id,
        integration_key=INTEGRATION_KEY,
        name="YouTube",
        category="media_channel",
        skill_contracts={
            "video.publish": {
                "description": "Upload a video to the connected channel.",
                # Tier 3 here for the same reason `social_publish` is tier 3: publishing is
                # irreversible and public. A connection cannot lower it.
                "risk_tier": 3,
                "scopes": ["https://www.googleapis.com/auth/youtube.upload"],
            },
            "channel.read": {
                "description": "Read the connected channel's own name and quota.",
                "risk_tier": 0,
                "scopes": ["https://www.googleapis.com/auth/youtube.readonly"],
            },
        },
        executor_bindings=[{"skill_id": "publish_youtube", "capability": "video.publish"}],
        provenance={"source": "builtin", "provider": "google", "api": "youtube_data_v3"},
        actor=actor,
    )
    return str(published["id"])


def start(
    connection: Connection,
    settings: Settings,
    *,
    project_id: str,
    actor: str,
    connection_key: str = "youtube_default",
) -> dict[str, Any]:
    """Begin a connection. Returns the URL to send the operator to."""
    client_id, _secret, redirect = _require_client(settings)
    definition_id = ensure_definition(connection, project_id, actor)

    # A fresh nonce every time, and the old pending row is replaced rather than accumulated:
    # an abandoned consent screen must not leave a state that still validates tomorrow.
    state = secrets.token_urlsafe(32)
    connection.execute(
        text("DELETE FROM integration_connections WHERE project_id = :p "
             "AND connection_key = :k AND status = 'pending'"),
        {"p": project_id, "k": connection_key},
    )
    row_id = make_id("iconn")
    connection.execute(
        text(
            """
            INSERT INTO integration_connections (
              id, project_id, integration_definition_id, connection_key, scope_type, scope_id,
              owner_ref, secret_ref_ids, granted_capabilities, data_boundaries, max_risk_tier,
              health_status, freshness_seconds, provenance, status, created_by
            ) VALUES (
              :id, :p, :def, :k, 'project', :p, :owner, '[]'::jsonb,
              CAST(:caps AS JSONB), '["public"]'::jsonb, 3,
              'pending', 3600, CAST(:prov AS JSONB), 'pending', :actor
            )
            """
        ),
        {
            "id": row_id, "p": project_id, "def": definition_id, "k": connection_key,
            "owner": "media_publisher",
            "caps": json.dumps(list(YOUTUBE_CAPABILITIES)),
            "prov": json.dumps({
                "source": "oauth",
                "provider": "google",
                "state": state,
                "scopes": list(YOUTUBE_SCOPES),
                "started_by": actor,
                "started_at": now_utc().isoformat(),
            }),
            "actor": actor,
        },
    )
    url = f"{AUTH_ENDPOINT}?" + urlencode({
        "client_id": client_id,
        "redirect_uri": redirect,
        "response_type": "code",
        "scope": " ".join(YOUTUBE_SCOPES),
        # offline + consent is what mints a REFRESH token. Without `prompt=consent` Google
        # returns only an access token on a re-authorisation, and the connection silently
        # works for an hour and then stops — the worst failure shape available here.
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
    })
    emit_event(connection, project_id, "channel.connection_started", actor,
               "integration_connection", row_id,
               {"provider": "google", "connection_key": connection_key})
    return {"authorization_url": url, "connection_id": row_id, "state": state}


def complete(
    connection: Connection,
    settings: Settings,
    *,
    code: str,
    state: str,
) -> dict[str, Any]:
    """Exchange the consent code, store the refresh token, activate the connection."""
    # THE NONCE IS CHECKED BEFORE ANYTHING ELSE, INCLUDING CONFIGURATION.
    #
    # This endpoint is unauthenticated by necessity — a browser following Google's redirect
    # carries no bearer token — so an arbitrary caller can reach it. Validating the client
    # config first meant a forged callback got told which environment variables this
    # installation is missing, which is a configuration disclosure to a stranger. The state is
    # the thing that says this request came from a flow BOSS started; nothing else should
    # happen until it holds.
    if not code or not state:
        raise ChannelOAuthError("Google did not return both a code and a state.")

    pending = connection.execute(
        text("SELECT id, project_id, connection_key, provenance FROM integration_connections "
             "WHERE status = 'pending' AND provenance->>'state' = :s"),
        {"s": state},
    ).mappings().first()
    if not pending:
        # Also covers replay: the row is deleted or activated once used, so a second callback
        # with the same state finds nothing.
        raise ChannelOAuthError(
            "That authorisation has expired or was already used. Start the connection again.")

    client_id, client_secret, redirect = _require_client(settings)
    httpx = _http()
    try:
        response = httpx.post(
            TOKEN_ENDPOINT,
            data={
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect,
                "grant_type": "authorization_code",
            },
            timeout=30.0,
        )
    except Exception as error:  # noqa: BLE001
        raise ChannelOAuthError(f"Could not reach Google to exchange the code: {error}") from error
    if response.status_code != 200:
        # Google's own error body is the useful thing here and says which half is wrong
        # (redirect_uri_mismatch, invalid_client). Truncated, never logged elsewhere.
        raise ChannelOAuthError(
            f"Google refused the exchange ({response.status_code}): {response.text[:200]}")

    payload = response.json()
    refresh_token = str(payload.get("refresh_token") or "").strip()
    if not refresh_token:
        raise ChannelOAuthError(
            "Google returned no refresh token. This happens when the account has already "
            "authorised this app — remove BOSS at myaccount.google.com/permissions and "
            "connect again.")

    project_id = str(pending["project_id"])
    ref_key = f"channel/{project_id}/{pending['connection_key']}/refresh_token"
    kind = default_secret_backend(settings)
    try:
        backend_for(kind, settings).put(ref_key, refresh_token)
    except SecretUnavailable as error:
        raise ChannelOAuthError(f"Could not store the token: {error}") from error

    secret_ref_id = make_id("secref")
    connection.execute(
        text(
            """
            INSERT INTO secret_refs (id, project_id, scope_type, scope_id, ref_kind, ref_key,
                                     provider, secret_kind, storage_backend, status, metadata,
                                     created_by)
            VALUES (:id, :p, 'project', :p, :kind, :ref_key, 'google', 'oauth_refresh_token',
                    :kind, 'active', CAST(:meta AS JSONB), :actor)
            """
        ),
        {"id": secret_ref_id, "p": project_id, "kind": kind, "ref_key": ref_key,
         "meta": json.dumps({"scopes": list(YOUTUBE_SCOPES), "provider": "google"}),
         "actor": str((pending["provenance"] or {}).get("started_by") or "system")},
    )

    channel = _channel_identity(settings, refresh_token, client_id, client_secret)
    connection.execute(
        text(
            """
            UPDATE integration_connections
               SET status = 'active', health_status = 'healthy', health_checked_at = now(),
                   secret_ref_ids = CAST(:refs AS JSONB),
                   provenance = provenance || CAST(:prov AS JSONB),
                   updated_at = now()
             WHERE id = :id
            """
        ),
        {"id": pending["id"], "refs": json.dumps([secret_ref_id]),
         "prov": json.dumps({
             # The state is REMOVED, not kept: it has been spent, and a spent nonce left in a
             # readable column is a replay waiting for a bug elsewhere.
             "state": None,
             "connected_at": now_utc().isoformat(),
             "channel": channel,
         })},
    )
    # The standards become relevant the moment a channel exists to publish to. Seeded here so
    # an operator meets them while publishing is on their mind, rather than discovering an
    # unexplained row in the Skills page months later.
    try:
        seed_publishing_standards(
            connection, project_id,
            str((pending["provenance"] or {}).get("started_by") or "system"))
    except Exception:  # noqa: BLE001
        # A failed seed must not fail a connection that is already established and stored.
        pass

    emit_event(connection, project_id, "channel.connected",
               str((pending["provenance"] or {}).get("started_by") or "system"),
               "integration_connection", str(pending["id"]),
               {"provider": "google", "channel": channel})
    return {
        "connection_id": pending["id"],
        "project_id": project_id,
        "channel": channel,
        "status": "active",
    }


def _channel_identity(
    settings: Settings, refresh_token: str, client_id: str, client_secret: str
) -> dict[str, Any]:
    """Which channel did we just connect to?

    Asked immediately, because "connected" with no channel name is a claim nobody can check,
    and an operator with several Google accounts will pick the wrong one eventually. Read-only:
    it proves the token works without publishing anything.
    """
    try:
        access = _exchange_refresh(settings, refresh_token, client_id, client_secret)
        httpx = _http()
        r = httpx.get(
            "https://www.googleapis.com/youtube/v3/channels",
            params={"part": "snippet", "mine": "true"},
            headers={"Authorization": f"Bearer {access}"},
            timeout=20.0,
        )
        if r.status_code != 200:
            return {"verified": False, "reason": f"channels.list returned {r.status_code}"}
        items = (r.json() or {}).get("items") or []
        if not items:
            return {"verified": False, "reason": "this Google account has no YouTube channel"}
        snippet = items[0].get("snippet") or {}
        return {
            "verified": True,
            "id": items[0].get("id"),
            "title": snippet.get("title"),
            "custom_url": snippet.get("customUrl"),
        }
    except Exception as error:  # noqa: BLE001
        # A failed identity check must not fail the connection — the token is already stored
        # and valid. It downgrades to unverified, which the console shows.
        return {"verified": False, "reason": str(error)[:200]}


def _exchange_refresh(
    settings: Settings, refresh_token: str, client_id: str, client_secret: str
) -> str:
    httpx = _http()
    r = httpx.post(
        TOKEN_ENDPOINT,
        data={
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "refresh_token",
        },
        timeout=30.0,
    )
    if r.status_code != 200:
        raise ChannelOAuthError(
            f"Google refused to refresh the token ({r.status_code}): {r.text[:200]}")
    token = str((r.json() or {}).get("access_token") or "")
    if not token:
        raise ChannelOAuthError("Google returned no access token.")
    return token


def access_token_for(
    connection: Connection, settings: Settings, *, project_id: str, connection_key: str
) -> str:
    """A short-lived access token for a connected channel.

    THE ONLY PATH from a stored connection to something that can call Google, and it returns an
    ACCESS token — an hour long — never the refresh token. A caller that wanted the refresh
    token would be a caller able to keep reaching the channel after the connection is revoked.
    """
    from .secret_backends import resolve_secret_ref

    row = connection.execute(
        text("SELECT secret_ref_ids, status FROM integration_connections "
             "WHERE project_id = :p AND connection_key = :k AND status = 'active'"),
        {"p": project_id, "k": connection_key},
    ).mappings().first()
    if not row:
        raise ChannelOAuthError(
            f"No active '{connection_key}' connection on this project. Connect the channel "
            "first.")
    refs = row["secret_ref_ids"] or []
    if not refs:
        raise ChannelOAuthError(
            f"The '{connection_key}' connection has no stored credential. Reconnect it.")
    client_id, client_secret, _redirect = _require_client(settings)
    refresh_token = resolve_secret_ref(connection, str(refs[0]), settings)
    return _exchange_refresh(settings, refresh_token, client_id, client_secret)


def disconnect(
    connection: Connection, settings: Settings, *,
    project_id: str, connection_key: str, actor: str,
) -> dict[str, Any]:
    """Revoke at Google AND locally. Either alone leaves a live credential somewhere."""
    from .secret_backends import resolve_secret_ref

    row = connection.execute(
        text("SELECT id, secret_ref_ids FROM integration_connections "
             "WHERE project_id = :p AND connection_key = :k"),
        {"p": project_id, "k": connection_key},
    ).mappings().first()
    if not row:
        raise ChannelOAuthError(f"No '{connection_key}' connection on this project.")

    revoked_upstream = False
    for ref in (row["secret_ref_ids"] or []):
        try:
            token = resolve_secret_ref(connection, str(ref), settings)
            _http().post(REVOKE_ENDPOINT, data={"token": token}, timeout=20.0)
            revoked_upstream = True
        except Exception:  # noqa: BLE001
            # Google being unreachable must not block the local revoke: a connection nobody can
            # revoke because a third party is down is worse than one revoked on one side.
            pass
        connection.execute(
            text("UPDATE secret_refs SET status='revoked', revoked_at=now(), revoked_by=:a "
                 "WHERE id = :i"),
            {"i": str(ref), "a": actor},
        )
    connection.execute(
        text("UPDATE integration_connections SET status='revoked', health_status='revoked', "
             "secret_ref_ids='[]'::jsonb, updated_at=now() WHERE id = :i"),
        {"i": row["id"]},
    )
    emit_event(connection, project_id, "channel.disconnected", actor,
               "integration_connection", str(row["id"]),
               {"connection_key": connection_key, "revoked_upstream": revoked_upstream})
    return {"connection_key": connection_key, "status": "revoked",
            "revoked_upstream": revoked_upstream}
