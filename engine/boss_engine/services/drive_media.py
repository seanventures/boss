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


#: What a Google-native document has to be EXPORTED as; it cannot be downloaded directly.
#: `text/plain` rather than a rich format on purpose — the caller wants the words, and a .docx
#: would arrive as a zip nothing downstream can read.
GOOGLE_NATIVE_EXPORT = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.presentation": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
}

#: Plain files that are already text. A PDF is deliberately absent: extracting text from one is
#: a different problem with its own failure modes, and pretending otherwise would return
#: something that looks like prose and is not.
READABLE_TEXT_PREFIXES = ("text/", "application/json", "application/xml")

#: A document is prose, not media. Anything larger than this is not a brief somebody wrote.
MAX_DOCUMENT_BYTES = 5_000_000


def read_document(
    connection: Connection,
    settings: Settings,
    *,
    project_id: str,
    file_id: str,
    connection_key: str = "youtube_default",
) -> dict[str, Any]:
    """Read a Drive document as text, so an agent can act on what it says.

    WHY THIS EXISTS. An operator wrote the title and description for their video in a Drive doc
    and linked it. The agent could fetch a VIDEO and an IMAGE and had no way to read a DOCUMENT,
    so they were asked to retype into a form the things they had already written down.

    TAMIL — AND ANY OTHER SCRIPT — IS THE REASON THE DETAILS BELOW MATTER.
    ---------------------------------------------------------------------
    A Google Doc cannot be downloaded; it must be EXPORTED, and the export is UTF-8. Two things
    would quietly ruin non-Latin text here and both are handled explicitly rather than left to a
    default:

      * `response.text` lets httpx guess the encoding from headers, and a missing or wrong
        charset turns "மக்கள் சிந்தனை" into mojibake that still looks like a successful read.
        The bytes are decoded as UTF-8 deliberately.
      * `errors="replace"` rather than `strict`: a single bad byte should not throw away a
        whole document, and U+FFFD is visible to a reader in a way a truncated file is not.

    The text is returned, never stored — a document is the operator's, and the agent needs the
    words rather than a copy in BOSS.
    """
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
    name = str(info.get("name") or file_id)

    if mime in GOOGLE_NATIVE_EXPORT:
        # A Google-native file has no bytes to download — export is the only way to read it.
        response = httpx.get(
            f"{DRIVE_FILES}/{file_id}/export",
            params={"mimeType": GOOGLE_NATIVE_EXPORT[mime]},
            headers=headers, timeout=60.0,
        )
        exported_as = GOOGLE_NATIVE_EXPORT[mime]
    elif any(mime.startswith(prefix) for prefix in READABLE_TEXT_PREFIXES):
        size = int(info.get("size") or 0)
        if size > MAX_DOCUMENT_BYTES:
            raise DriveMediaError(
                f"'{name}' is {size / 1e6:.1f} MB, which is not a document somebody wrote. "
                f"This reads files up to {MAX_DOCUMENT_BYTES / 1e6:.0f} MB.")
        response = httpx.get(
            f"{DRIVE_FILES}/{file_id}",
            params={"alt": "media"}, headers=headers, timeout=60.0,
        )
        exported_as = mime
    else:
        raise DriveMediaError(
            f"'{name}' is {mime or 'of unknown type'}, which this cannot read as text. It reads "
            "Google Docs, Slides and Sheets, and plain text files. A PDF or an image needs a "
            "different skill.")

    if response.status_code != 200:
        raise DriveMediaError(
            f"Could not read '{name}' ({response.status_code}): {response.text[:200]}")

    # DECODED AS UTF-8 DELIBERATELY. See the docstring: letting the client guess is how Tamil,
    # Devanagari or anything else non-Latin becomes mojibake that still reads as success.
    text_value = response.content.decode("utf-8", errors="replace")
    if len(text_value) > MAX_DOCUMENT_BYTES:
        text_value = text_value[:MAX_DOCUMENT_BYTES]
    return {
        "file_id": file_id,
        "name": name,
        "mime_type": mime,
        "exported_as": exported_as,
        "text": text_value,
        "characters": len(text_value),
    }


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
                                   data_boundary)
            VALUES (:id, :p, :kind, :title, :uri, CAST(:content AS JSONB), 'public')
            """
        ),
        {
            "id": artifact_id, "p": project_id, "kind": kind,
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
                # WHO MADE IT, in the blob rather than in a column.
                #
                # This INSERT named a `created_by` COLUMN that has never existed on `artifacts`
                # — the table has `created_by_job_id`, which is a job id and a different fact.
                # So every media artifact insert failed with UndefinedColumn, which means
                # `record_artifact` had never once worked and neither had anything downstream of
                # it: media.fetch_link, every video edit, every publish.
                #
                # Nobody saw it because the failure was swallowed five layers deep — the
                # executor's retry loop caught it, the aborted transaction then made the
                # invocation record, the step failure and the tick all report a poisoned
                # transaction naming some other query. The workflow simply stopped.
                #
                # The actor is worth keeping, so it goes where the rest of this artifact's facts
                # already live rather than into a column that would have to be invented.
                "created_by": actor,
                **(detail or {}),
            }),
        },
    )
    return artifact_id
