"""Uploading a finished video to the connected channel.

RESUMABLE, BECAUSE A LARGE UPLOAD OVER A HOME CONNECTION WILL BE INTERRUPTED.
A single POST of 135 MB that restarts from zero on a dropped connection is a step that never
completes on a bad day. Google's resumable protocol lets ONE attempt survive a broken pipe,
which is a different thing from retrying: the skill still gets exactly one attempt, because a
second attempt could publish the same video twice.

PRIVATE ON ARRIVAL, ALWAYS.
`publish_youtube` refuses a public visibility outright (see packages/specpack/skills/
youtube.yaml). Making a video public is a separate act, taken by a person who has watched the
uploaded file — not a parameter on the job that produced it.
"""
from __future__ import annotations

import json
import os
from typing import Any

UPLOAD_ENDPOINT = "https://www.googleapis.com/upload/youtube/v3/videos"

#: What YouTube accepts. `public` is deliberately absent: this module cannot publish publicly
#: even if something upstream asked it to.
ALLOWED_VISIBILITY = ("private", "unlisted")


class YouTubePublishError(RuntimeError):
    """Something the operator can fix, phrased for them."""


def upload_video(
    connection,
    settings,
    *,
    project_id: str,
    local_path: str,
    title: str,
    description: str = "",
    tags: Any = (),
    made_for_kids: Any = None,
    visibility: str = "private",
    connection_key: str = "youtube_default",
    category_id: str = "22",
) -> dict[str, Any]:
    """Upload one file. Returns the video id and its watch URL."""
    import httpx

    from .channel_oauth import ChannelOAuthError, access_token_for

    if not os.path.exists(local_path):
        raise YouTubePublishError(f"Nothing to upload at {local_path}.")
    if not str(title or "").strip():
        raise YouTubePublishError("A title is required.")
    if visibility not in ALLOWED_VISIBILITY:
        raise YouTubePublishError(
            f"visibility must be one of {', '.join(ALLOWED_VISIBILITY)}. Making a video public "
            "is a separate decision, taken after somebody has watched it.")
    if made_for_kids is None:
        # LEGALLY REQUIRED (COPPA), so BOSS must not guess it. A default here would be BOSS
        # making a compliance declaration on the operator's behalf.
        raise YouTubePublishError(
            "made_for_kids must be stated explicitly — it is a legal declaration YouTube "
            "requires, not metadata BOSS can infer.")

    try:
        token = access_token_for(
            connection, settings, project_id=project_id, connection_key=connection_key)
    except ChannelOAuthError as error:
        raise YouTubePublishError(str(error)) from error

    size = os.path.getsize(local_path)
    metadata = {
        "snippet": {
            "title": str(title)[:100],          # YouTube's own limit; truncating beats a 400
            "description": str(description or "")[:5000],
            "tags": [str(t) for t in (tags or [])][:60],
            "categoryId": str(category_id),
        },
        "status": {
            "privacyStatus": visibility,
            "selfDeclaredMadeForKids": bool(made_for_kids),
        },
    }

    start = httpx.post(
        UPLOAD_ENDPOINT,
        params={"uploadType": "resumable", "part": "snippet,status"},
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Length": str(size),
            "X-Upload-Content-Type": "video/*",
        },
        content=json.dumps(metadata),
        timeout=60.0,
    )
    if start.status_code not in (200, 201):
        raise YouTubePublishError(
            f"YouTube refused the upload ({start.status_code}): {start.text[:300]}")
    session_url = start.headers.get("Location")
    if not session_url:
        raise YouTubePublishError("YouTube did not return an upload session.")

    with open(local_path, "rb") as handle:
        done = httpx.put(
            session_url,
            headers={"Content-Length": str(size),
                     "Content-Range": f"bytes 0-{max(size - 1, 0)}/{size}"},
            content=handle.read(),
            timeout=None,
        )
    if done.status_code not in (200, 201):
        raise YouTubePublishError(
            f"The upload did not complete ({done.status_code}): {done.text[:300]}")

    body = done.json() or {}
    video_id = body.get("id")
    if not video_id:
        raise YouTubePublishError("YouTube accepted the upload but returned no video id.")
    return {
        "video_id": video_id,
        "video_url": f"https://www.youtube.com/watch?v={video_id}",
        "studio_url": f"https://studio.youtube.com/video/{video_id}/edit",
        "visibility": (body.get("status") or {}).get("privacyStatus", visibility),
        "bytes": size,
    }
