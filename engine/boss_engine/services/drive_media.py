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

import io
import json
import os
import re
import shutil
import tempfile
import unicodedata
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

#: Plain files that are already text.
READABLE_TEXT_PREFIXES = ("text/", "application/json", "application/xml")

#: A PDF, which is NOT text and needs its words lifted out of the page description.
#:
#: IT WAS REFUSED HERE UNTIL A REAL BRIEF TURNED OUT TO BE ONE. The comment that used to sit
#: on the line above said a PDF was "deliberately absent" because extraction "would return
#: something that looks like prose and is not" — and the first operator to link a brief linked
#: a PDF. Refusing the format people actually have is not caution, it is the feature missing.
#: The honest failure it was worried about is handled below instead: a PDF whose pages are
#: images has NO text to lift, and that is said plainly rather than returned as empty prose.
PDF_MIME = "application/pdf"

#: A document is prose, not media. Anything larger than this is not a brief somebody wrote.
MAX_DOCUMENT_BYTES = 5_000_000


#: Vowel signs that a PDF paints BEFORE the consonant they belong to, while Unicode stores them
#: AFTER it. This is why lifting glyphs out of a PDF is not the same as reading its text, and it
#: is not a rounding error: extraction turned "சிந்தனை" into "சிந்தைன" — a different word.
#:
#: The list is additive. Only signs that are genuinely pre-base in their script belong here;
#: Telugu and Kannada are absent because their signs are not.
PREBASE_VOWEL_SIGNS = frozenset(
    "\u093f"              # Devanagari ि
    "\u09c7\u09c8"        # Bengali ে ৈ
    "\u0a3f"              # Gurmukhi ਿ
    "\u0abf"              # Gujarati િ
    "\u0b47\u0b48"        # Oriya େ ୈ
    "\u0bc6\u0bc7\u0bc8"  # Tamil ெ ே ை
    "\u0d46\u0d47\u0d48"  # Malayalam െ േ ൈ
    "\u0dd9\u0dda"        # Sinhala ෙ ේ
)

#: The character that joins two consonants into a cluster. A pre-base sign belongs after the
#: WHOLE cluster — "க்ரெ" is one — so reordering has to step over these.
VIRAMAS = frozenset("\u094d\u09cd\u0a4d\u0acd\u0b4d\u0bcd\u0c4d\u0ccd\u0d4d\u0dca")


def in_the_order_a_reader_wrote_it(extracted: str) -> str:
    """Put glyphs lifted out of a PDF back into the order the words are actually stored in.

    A PDF says where to paint each glyph, and for Tamil — Devanagari, Bengali, Malayalam, the
    same family — some vowel signs are painted to the LEFT of the consonant they modify. Read
    the glyphs off the page in the order they were painted and you get the sign before the
    consonant, which is a different string from the one the author typed:

        typed      சிந்தனை       ச ி ந ் த ன ை
        extracted  சிந்தைன       ச ி ந ் த ை ன     ← the same glyphs, the wrong word

    Nothing downstream can recover from that. A title read this way looks like Tamil, passes
    every check a program could make on it, and is wrong in a way only a reader can see — so it
    is fixed here, at the only point where the visual order is known to be visual.

    Each pre-base sign is moved after the cluster it belongs to, then NFC recomposes the split
    vowels that fall out of it (ெ + ா is ொ). Text with none of these signs comes back untouched.
    """
    if not any(ch in PREBASE_VOWEL_SIGNS for ch in extracted):
        return extracted

    pieces: list[str] = []
    index, length = 0, len(extracted)
    while index < length:
        sign = extracted[index]
        if sign in PREBASE_VOWEL_SIGNS:
            after = index + 1
            # The cluster is a letter, plus any number of virama-joined letters after it.
            if after < length and unicodedata.category(extracted[after]) == "Lo":
                after += 1
                while (after + 1 < length and extracted[after] in VIRAMAS
                       and unicodedata.category(extracted[after + 1]) == "Lo"):
                    after += 2
                pieces.append(extracted[index + 1:after])
                pieces.append(sign)
                index = after
                continue
        pieces.append(sign)
        index += 1
    return unicodedata.normalize("NFC", "".join(pieces))


def _tidied(extracted: str) -> str:
    """The spacing artefacts of a page, removed.

    Glyphs are painted at coordinates, not typed, so extraction reports the gaps between them:
    a word break can come back as two or three spaces and a full stop can come back detached
    from the word before it. Left alone, "பகுதி ." is not the string anybody wrote and no
    comparison against the original succeeds.
    """
    lines = []
    for line in extracted.split("\n"):
        line = re.sub(r"[ \t]{2,}", " ", line)
        line = re.sub(r" +([.,;:!?)\]])", r"\1", line)
        lines.append(line.rstrip())
    return "\n".join(lines).strip()


#: Types that tell you nothing. A file uploaded without an extension — "prompt", say — is
#: application/octet-stream in Drive whatever is inside it, and an empty mimeType means the same.
UNTYPED = ("", "application/octet-stream", "binary/octet-stream")


def _what_the_bytes_say(payload: bytes, name: str) -> str:
    """The type of a file Drive would not name, worked out by looking at it.

    THIS IS THE SECOND TIME THE SAME MISTAKE COST A RUN. The skill refused PDFs and the first
    real brief was a PDF. PDFs were added, and the same brief STILL failed — because the file is
    called "prompt", has no extension, and so reaches us as application/octet-stream:

        'prompt' is application/octet-stream, which this cannot read as text.

    Declared types describe how a file was uploaded, not what it is. A person who writes their
    title and description in a file and drops it in Drive has told us everything we need; the
    bytes are right there. So when Drive will not say, this looks — at the magic number, and
    failing that at whether the whole thing decodes as text, which is the only honest test of
    whether something IS text.
    """
    if payload.startswith(b"%PDF-"):
        return PDF_MIME
    if payload[:4] == b"PK\x03\x04":
        raise DriveMediaError(
            f"'{name}' is a zip-based document — a .docx, .odt or similar. Open it and save it "
            "as a Google Doc or a PDF, and this can read it.")
    # A NUL byte settles it: no document contains one, and several binary formats decode as
    # text by accident without this check.
    if b"\x00" not in payload:
        try:
            payload.decode("utf-8-sig")
        except (UnicodeDecodeError, UnicodeError):
            pass
        else:
            return "text/plain; charset=utf-8-sig"
    # UTF-16 ONLY WITH A BYTE-ORDER MARK. Almost any even-length run of bytes decodes as UTF-16
    # without complaint, so accepting it on a successful decode alone would call a video a
    # document. The mark is the file saying what it is.
    if payload[:2] in (b"\xff\xfe", b"\xfe\xff"):
        try:
            payload.decode("utf-16")
        except (UnicodeDecodeError, UnicodeError):
            pass
        else:
            return "text/plain; charset=utf-16"
    raise DriveMediaError(
        f"Drive does not say what '{name}' is, and it is neither a PDF nor text in any encoding "
        "this understands. Save it as a Google Doc or a PDF and link that.")


def _text_out_of_a_pdf(payload: bytes, name: str) -> str:
    """The words in a PDF, or a straight answer about why there are none.

    A PDF is a description of how to PAINT a page, not a document with text in it. Two kinds
    arrive and they have to be told apart, because one of them cannot be read at all:

      * EXPORTED from a word processor — every glyph carries a ToUnicode mapping, so the
        characters come back, Tamil included. This is what a brief written in Google Docs and
        saved as PDF looks like, and it reads correctly.
      * SCANNED, or exported as images — the pages are pictures. There is no text to extract,
        and `extract_text()` returns "" for every page. Returning that empty string would hand
        the agent a document it believes it read and found blank, which is how an empty title
        gets published. It raises instead, and says what to do about it.

    Nothing is stored and nothing is guessed. The pages are joined in order, so a title on page
    one stays before a description on page two.
    """
    try:
        from pypdf import PdfReader
    except ModuleNotFoundError as exc:  # pragma: no cover - a deployment fault, not a user one
        raise DriveMediaError(
            f"'{name}' is a PDF and this install cannot read PDFs: pypdf is not present. "
            "Export the brief as a Google Doc, or type the details into the form.") from exc

    try:
        reader = PdfReader(io.BytesIO(payload))
        pages = [(page.extract_text() or "") for page in reader.pages]
    except Exception as exc:  # pypdf raises a family of its own errors for a damaged file
        raise DriveMediaError(
            f"Could not read '{name}' as a PDF: {exc}. If it is encrypted or damaged, open it "
            "and save it again, or put the text in a Google Doc.") from exc

    text_value = _tidied("\n\n".join(page for page in pages if page.strip()))
    text_value = in_the_order_a_reader_wrote_it(text_value)
    if not text_value:
        raise DriveMediaError(
            f"'{name}' is a PDF with {len(pages)} page(s) and no text in any of them — the "
            "pages are images, so the words are part of the picture and there is nothing to "
            "lift out. Put the text in a Google Doc and link that instead, or type the title "
            "and description into the start form.")
    return text_value


def _text_by_asking_drive_to_convert(
    httpx: Any, headers: dict[str, str], *, file_id: str, name: str, language: str = "",
) -> str:
    """Let Google read the PDF, because Google knows what the glyphs MEAN.

    WHY THIS IS TRIED FIRST. Lifting glyphs out of a PDF gives their painted order, and for
    Tamil and its relatives that is not the order the words are stored in — see
    `in_the_order_a_reader_wrote_it`, which repairs it. Repairing is second best. Drive can
    CONVERT the PDF to a Google Doc, and a Doc holds real text: the export comes back in
    logical order with no repair needed, and a PDF whose pages are images is read by OCR on the
    way through, so a scan works too.

    The copy is a workspace, not a result. It is created, exported, and deleted in the same
    call, whatever happens — leaving Google Docs named after other people's briefs scattered
    through the connected account's Drive would be its own bug.
    """
    body = {"name": f"BOSS is reading — {name}",
            "mimeType": "application/vnd.google-apps.document"}
    params = {"fields": "id"}
    if language:
        # ONLY MATTERS FOR A SCAN. A PDF with real text in it is converted from that text and
        # the hint is ignored; a PDF of photographs is OCR'd, and OCR in the wrong language
        # returns confident nonsense.
        params["ocrLanguage"] = language
    made = httpx.post(f"{DRIVE_FILES}/{file_id}/copy", params=params, json=body,
                      headers=headers, timeout=120.0)
    if made.status_code != 200:
        raise DriveMediaError(
            f"Drive would not convert '{name}' ({made.status_code}): {made.text[:200]}")
    copy_id = str((made.json() or {}).get("id") or "")
    if not copy_id:
        raise DriveMediaError(f"Drive converted '{name}' and did not say where to.")
    try:
        exported = httpx.get(f"{DRIVE_FILES}/{copy_id}/export",
                             params={"mimeType": "text/plain"}, headers=headers, timeout=120.0)
        if exported.status_code != 200:
            raise DriveMediaError(
                f"Could not read the converted '{name}' ({exported.status_code}): "
                f"{exported.text[:200]}")
        # UTF-8 DELIBERATELY, for the same reason as every other read here.
        return exported.content.decode("utf-8", errors="replace")
    finally:
        # Best effort by design: a copy left behind is untidy, and raising about it would turn a
        # successful read into a failure.
        try:
            httpx.delete(f"{DRIVE_FILES}/{copy_id}", headers=headers, timeout=30.0)
        except Exception:  # pragma: no cover - the read already succeeded
            pass


#: Past this, the numbered copy is left out: it doubles the payload, and a document this long is
#: not one an agent is picking a title out of.
MAX_NUMBERED_CHARACTERS = 40_000


def _with_line_numbers(body: str) -> str:
    """The document as something an agent can refer to by number rather than by quotation."""
    if len(body) > MAX_NUMBERED_CHARACTERS:
        return ""
    return "\n".join(f"{number}: {line}"
                      for number, line in enumerate(body.split("\n"), start=1))


def read_document(
    connection: Connection,
    settings: Settings,
    *,
    project_id: str,
    file_id: str,
    language: str = "",
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
    read_via = "export" if mime in GOOGLE_NATIVE_EXPORT else "download"

    if mime in GOOGLE_NATIVE_EXPORT:
        # A Google-native file has no bytes to download — export is the only way to read it.
        response = httpx.get(
            f"{DRIVE_FILES}/{file_id}/export",
            params={"mimeType": GOOGLE_NATIVE_EXPORT[mime]},
            headers=headers, timeout=60.0,
        )
        exported_as = GOOGLE_NATIVE_EXPORT[mime]
    elif (mime == PDF_MIME or mime in UNTYPED
          or any(mime.startswith(prefix) for prefix in READABLE_TEXT_PREFIXES)):
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
            f"'{name}' is {mime}, which this cannot read as text. It reads Google Docs, Slides "
            "and Sheets, PDFs, plain text, and any file Drive does not type. An image needs a "
            "different skill.")

    if response.status_code != 200:
        raise DriveMediaError(
            f"Could not read '{name}' ({response.status_code}): {response.text[:200]}")

    # WHAT IT IS, NOT WHAT IT WAS CALLED. An untyped file is identified from its bytes; a file
    # Drive did name is taken at its word, because Drive knows what it converted.
    effective = mime
    if mime in UNTYPED:
        effective = _what_the_bytes_say(response.content, name)
        read_via = f"sniffed:{effective}"

    if effective == PDF_MIME:
        # TWO ROUTES, BEST FIRST. Drive's conversion returns logical-order text and OCRs a
        # scan; local extraction returns painted order, which then has to be repaired. The
        # second exists so a read still works when the account cannot write to Drive, and
        # `read_via` says which one answered rather than leaving it to be guessed.
        try:
            text_value = _text_by_asking_drive_to_convert(
                httpx, headers, file_id=file_id, name=name, language=language)
            exported_as = "text/plain"
            read_via = "drive_conversion"
            if not text_value.strip():
                raise DriveMediaError("the conversion came back empty")
        except DriveMediaError:
            text_value = _text_out_of_a_pdf(response.content, name)
            read_via = "extracted_and_reordered"
    else:
        # DECODED AS UTF-8 DELIBERATELY. See the docstring: letting the client guess is how
        # Tamil, Devanagari or anything else non-Latin becomes mojibake that still reads as
        # success. `utf-8-sig` over plain utf-8 so a byte-order mark does not arrive as a
        # stray character at the front of a title.
        charset = "utf-8-sig"
        if "charset=" in effective:
            # The sniffer already decoded the whole file to establish this, so it cannot fail.
            charset = effective.split("charset=", 1)[1].strip()
        text_value = response.content.decode(charset, errors="replace")
    if len(text_value) > MAX_DOCUMENT_BYTES:
        text_value = text_value[:MAX_DOCUMENT_BYTES]
    return {
        # THE SAME TEXT, WITH SOMETHING AN AGENT CAN POINT AT.
        #
        # An agent that has to hand a value to the next step cannot retype it — Tamil does not
        # survive being written back out, so a title arrived as a different sentence. It points
        # at the value instead. But a pointer made of the document's own words is retyped too,
        # and arrives damaged: one run pointed at "மதுராந்தகத்தில்இட்டற்றலெ...?" and matched the
        # wrong paragraph. A LINE NUMBER IS ASCII AND CANNOT BE DAMAGED, so the numbered view
        # goes with the text and the agent quotes digits.
        "numbered_text": _with_line_numbers(text_value),
        "file_id": file_id,
        "name": name,
        "mime_type": mime,
        "read_as": effective,
        "exported_as": exported_as,
        "read_via": read_via,
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
