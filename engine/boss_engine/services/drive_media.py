"""Moving media between the operator's Drive and the machine that edits it.

WHY DRIVE AND NOT AN UPLOAD ENDPOINT
------------------------------------
The operator's video is on their laptop; BOSS runs on a server. Something has to carry it. An
upload endpoint would mean building a resumable multi-part receiver, a storage bucket policy,
and a browser uploader — and the operator already has a place they put large files and trust.
Drive is the handover, on the SAME Google consent that publishes to YouTube, so there is one
credential with one expiry rather than two.

WHAT THIS IS NOT
----------------
It is not a Drive browser, and it deliberately cannot delete or overwrite anything. Reads are
`drive.readonly`; writes are `drive.file`, which reaches only files BOSS itself created. An
edited video is a NEW file every time. The operator's source is never modified, and BOSS has no
permission that would let a bug modify it.

THE FILE LANDS ON DISK, AND THAT IS THE POINT
---------------------------------------------
ffmpeg cannot read a URL as a seekable input for the kind of editing this does, and a 135 MB
video is not something to hold in memory. So a download is a real file in a working directory
that the caller is responsible for clearing — `workspace()` is the context manager that does it
whether the step succeeds or throws.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from contextlib import contextmanager
from typing import Any, Iterator, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import Settings
from .core import emit_event, make_id

DRIVE_FILES = "https://www.googleapis.com/drive/v3/files"
DRIVE_UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"

#: A read that would exhaust the worker's disk is a failure of this module, not of the box.
#: 2 GiB is comfortably above a long 1080p video and far below anything that would fill a
#: container. Raised deliberately, never silently.
MAX_MEDIA_BYTES = 2 * 1024 * 1024 * 1024

#: What BOSS is willing to treat as a video. A Drive folder contains spreadsheets and PDFs, and
#: handing one to ffmpeg produces a confusing failure several steps later instead of here.
VIDEO_MIME_PREFIXES = ("video/",)


class DriveMediaError(RuntimeError):
    """Something the operator can fix, phrased for them."""


def _http():
    import httpx

    return httpx


def _token(connection: Connection, settings: Settings, project_id: str,
           connection_key: str) -> str:
    from .channel_oauth import ChannelOAuthError, access_token_for

    try:
        return access_token_for(
            connection, settings, project_id=project_id, connection_key=connection_key)
    except ChannelOAuthError as error:
        raise DriveMediaError(str(error)) from error


@contextmanager
def workspace(prefix: str = "boss-media-") -> Iterator[str]:
    """A scratch directory that is always removed.

    A step that throws mid-edit must not leave a gigabyte behind: the worker is long-lived, and
    three abandoned renders fill the disk that every other project shares.
    """
    path = tempfile.mkdtemp(prefix=prefix)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def list_videos(
    connection: Connection,
    settings: Settings,
    *,
    project_id: str,
    folder_id: str = "",
    connection_key: str = "youtube_default",
    limit: int = 50,
) -> list[dict[str, Any]]:
    """The videos an operator could pick, so a workflow can be given one by name.

    Filtered to video mime types on the server side rather than here: a Drive with ten thousand
    documents should not be paged through to find four videos.
    """
    token = _token(connection, settings, project_id, connection_key)
    query = "trashed = false and mimeType contains 'video/'"
    if folder_id.strip():
        query = f"'{folder_id.strip()}' in parents and {query}"
    r = _http().get(
        DRIVE_FILES,
        params={
            "q": query,
            "fields": "files(id,name,mimeType,size,modifiedTime,videoMediaMetadata)",
            "orderBy": "modifiedTime desc",
            "pageSize": max(1, min(int(limit), 200)),
        },
        headers={"Authorization": f"Bearer {token}"},
        timeout=30.0,
    )
    if r.status_code != 200:
        raise DriveMediaError(f"Drive refused the listing ({r.status_code}): {r.text[:200]}")
    out = []
    for f in (r.json() or {}).get("files", []):
        meta = f.get("videoMediaMetadata") or {}
        out.append({
            "file_id": f.get("id"),
            "name": f.get("name"),
            "mime_type": f.get("mimeType"),
            "bytes": int(f.get("size") or 0),
            "modified_at": f.get("modifiedTime"),
            "width": meta.get("width"),
            "height": meta.get("height"),
            "duration_ms": int(meta["durationMillis"]) if meta.get("durationMillis") else None,
        })
    return out


def download(
    connection: Connection,
    settings: Settings,
    *,
    project_id: str,
    file_id: str,
    dest_dir: str,
    connection_key: str = "youtube_default",
) -> dict[str, Any]:
    """Fetch one Drive file onto local disk. Returns where it landed and what it is."""
    token = _token(connection, settings, project_id, connection_key)
    httpx = _http()
    headers = {"Authorization": f"Bearer {token}"}

    meta = httpx.get(
        f"{DRIVE_FILES}/{file_id}",
        params={"fields": "id,name,mimeType,size"},
        headers=headers, timeout=30.0,
    )
    if meta.status_code != 200:
        raise DriveMediaError(
            f"Could not read that Drive file ({meta.status_code}): {meta.text[:200]}. "
            "Check the id, and that the connected Google account can see it.")
    info = meta.json() or {}
    mime = str(info.get("mimeType") or "")
    size = int(info.get("size") or 0)

    if not any(mime.startswith(p) for p in VIDEO_MIME_PREFIXES):
        # Said HERE rather than by ffmpeg four steps later, where the error is about a format
        # rather than about the operator having picked a spreadsheet.
        raise DriveMediaError(
            f"'{info.get('name')}' is {mime or 'of unknown type'}, not a video.")
    if size > MAX_MEDIA_BYTES:
        raise DriveMediaError(
            f"'{info.get('name')}' is {size / 1e9:.1f} GB, above the {MAX_MEDIA_BYTES / 1e9:.0f} "
            "GB this worker will download. Trim it in Drive first, or raise the limit "
            "deliberately.")

    os.makedirs(dest_dir, exist_ok=True)
    local = os.path.join(dest_dir, _safe_name(str(info.get("name") or file_id)))
    # STREAMED. A 135 MB response read into memory is 135 MB of worker RSS that the next
    # concurrent step does not have.
    with httpx.stream("GET", f"{DRIVE_FILES}/{file_id}", params={"alt": "media"},
                      headers=headers, timeout=None) as response:
        if response.status_code != 200:
            raise DriveMediaError(
                f"Drive refused the download ({response.status_code}).")
        written = 0
        with open(local, "wb") as handle:
            for chunk in response.iter_bytes(chunk_size=1024 * 1024):
                written += len(chunk)
                if written > MAX_MEDIA_BYTES:
                    raise DriveMediaError("The file grew past the download limit mid-transfer.")
                handle.write(chunk)
    return {"path": local, "name": info.get("name"), "mime_type": mime, "bytes": written,
            "file_id": file_id}


def upload(
    connection: Connection,
    settings: Settings,
    *,
    project_id: str,
    local_path: str,
    name: str,
    folder_id: str = "",
    connection_key: str = "youtube_default",
    actor: str = "system",
) -> dict[str, Any]:
    """Put an edited video back in Drive as a NEW file.

    Never an update to an existing one: `drive.file` reaches only what BOSS created, and a
    pipeline that could overwrite the operator's source is a pipeline that will eventually
    overwrite the only copy of something.
    """
    if not os.path.exists(local_path):
        raise DriveMediaError(f"Nothing to upload at {local_path}.")
    size = os.path.getsize(local_path)
    token = _token(connection, settings, project_id, connection_key)
    httpx = _http()

    metadata: dict[str, Any] = {"name": name}
    if folder_id.strip():
        metadata["parents"] = [folder_id.strip()]

    # Resumable, because a multi-hundred-megabyte upload over a home connection will be
    # interrupted, and restarting from zero each time is how a step never completes.
    start = httpx.post(
        DRIVE_UPLOAD,
        params={"uploadType": "resumable", "fields": "id,name,webViewLink"},
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Length": str(size),
        },
        content=json.dumps(metadata),
        timeout=30.0,
    )
    if start.status_code not in (200, 201):
        raise DriveMediaError(
            f"Drive refused the upload ({start.status_code}): {start.text[:200]}")
    session_url = start.headers.get("Location")
    if not session_url:
        raise DriveMediaError("Drive did not return an upload session.")

    with open(local_path, "rb") as handle:
        done = httpx.put(
            session_url,
            headers={"Content-Length": str(size),
                     "Content-Range": f"bytes 0-{max(size - 1, 0)}/{size}"},
            content=handle.read(),
            timeout=None,
        )
    if done.status_code not in (200, 201):
        raise DriveMediaError(
            f"The upload did not complete ({done.status_code}): {done.text[:200]}")
    body = done.json() or {}
    emit_event(connection, project_id, "media.written_to_drive", actor, "drive_file",
               str(body.get("id") or ""), {"name": name, "bytes": size})
    return {
        "file_id": body.get("id"),
        "name": body.get("name") or name,
        "view_url": body.get("webViewLink")
                    or f"https://drive.google.com/file/d/{body.get('id')}/view",
        "bytes": size,
    }


def _safe_name(name: str) -> str:
    """A Drive filename is operator input and becomes a path. Strip anything that traverses."""
    cleaned = os.path.basename(name).replace("\x00", "").strip()
    cleaned = "".join(c for c in cleaned if c.isalnum() or c in "._- ()[]")
    return cleaned[:120] or "media"


def record_artifact(
    connection: Connection,
    *,
    project_id: str,
    actor: str,
    kind: str,
    drive_file: dict[str, Any],
    source_artifact_id: Optional[str] = None,
    detail: Optional[dict[str, Any]] = None,
) -> str:
    """The handle one workflow step passes to the next.

    Steps cannot hand each other a local path: each runs in its own invocation and the working
    directory is gone by the time the next one starts. What survives is the Drive file, so THAT
    is what an artifact records — plus which artifact it came from, so a published video can be
    traced back to the source it was cut from.
    """
    artifact_id = make_id("art")
    connection.execute(
        text(
            """
            INSERT INTO artifacts (id, project_id, artifact_type, title, uri, content,
                                   data_boundary, created_by)
            VALUES (:id, :p, :kind, :title, :uri, CAST(:content AS JSONB), 'public', :actor)
            """
        ),
        {
            "id": artifact_id, "p": project_id, "kind": kind, "actor": actor,
            # The title is what a person reads in the artifact list, so it is the FILENAME
            # rather than the id — "MS_latest_FULL_1080p.mp4" tells you which video this is
            # and "art_9f3c…" does not.
            "title": str(drive_file.get("name") or "media"),
            # The Drive link, on the row, so a reviewer can watch it without a second lookup.
            "uri": drive_file.get("view_url"),
            "content": json.dumps({
                "drive_file_id": drive_file.get("file_id"),
                "name": drive_file.get("name"),
                "view_url": drive_file.get("view_url"),
                "bytes": drive_file.get("bytes"),
                "source_artifact_id": source_artifact_id,
                **(detail or {}),
            }),
        },
    )
    return artifact_id
