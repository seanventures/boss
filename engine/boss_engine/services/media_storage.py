"""One way to fetch and store media, whatever holds it.

THE SEAM, AND WHY IT IS HERE RATHER THAN IN drive_media
-------------------------------------------------------
`drive_media` is Google Drive: its REST URLs, its link shape, its share-id rules. That is the
right amount of Google in one place. What was wrong is that everything above it — the editing
steps, the skills, the workflow — talked to Drive directly, so "which storage holds the video"
was not a question the system could ask, let alone answer differently.

This module is where it is asked. A caller says "fetch what this link points at"; which provider
that is, and which of four APIs answers, is decided from the link and the project's connections.

WHAT IS AND IS NOT IMPLEMENTED, STATED PLAINLY
----------------------------------------------
`google` is complete, and it is complete because it delegates to `drive_media` unchanged rather
than being rewritten behind a new interface — a refactor that rewrites the one working provider
while introducing the abstraction is a refactor with nothing left to compare against.

`http` is complete: a direct link needs no credential, so there was nothing to stub.

`microsoft` and `s3` are REGISTERED BUT NOT IMPLEMENTED. Their links resolve correctly, their
capabilities and credential requirements are declared, and calling them raises a named error
saying so. This is deliberate and it is not laziness: writing Graph and SigV4 calls that no test
can exercise, against accounts that do not exist yet, produces code that looks finished and has
never run. An honest refusal is worth more than that, and the seam means adding either one is
additive — nothing above this module changes.
"""
from __future__ import annotations

import mimetypes
import os
import shutil
import uuid
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from sqlalchemy.engine import Connection

from ..config import Settings
from .media_providers import (
    AUTH_NONE,
    CAP_WRITE,
    LOCAL_PROVIDER,
    ResolvedLink,
    provider,
    resolve_link,
)

#: The biggest file BOSS will pull down a direct link. A video is large and the control plane's
#: disk is not, and an unbounded fetch from an operator-supplied URL is a way to fill it.
MAX_DIRECT_BYTES = 8 * 1024 * 1024 * 1024


class MediaStorageError(RuntimeError):
    """A fetch or store that could not be done, in terms of what to do about it."""


def default_connection_keys(provider_key: str) -> tuple[str, ...]:
    """Which connection to use when the caller did not name one, most-preferred first.

    GOOGLE CARRIES A LEGACY NAME AND MUST KEEP RESOLVING IT. Every Google connection that exists
    today — including the operator's own and every deployed install's — was created by
    `channel_oauth` with ``connection_key='youtube_default'``, because at the time the only
    reason to connect Google was to publish to YouTube. Defaulting to `google_default` alone
    would leave those installs looking unconnected while the row sat right there, which is the
    same failure the `settings.github_token` change made in preview clones: a credential that
    exists and is no longer looked for.
    """
    key = str(provider_key or "").strip().lower()
    if key == "google":
        return ("google_default", "youtube_default")
    return (f"{key}_default",)


def _resolve_connection_key(
    connection: Connection, project_id: str, provider_key: str, requested: Optional[str],
) -> str:
    """The connection to use: the one asked for, or the first default that actually exists."""
    if requested:
        return str(requested)
    candidates = default_connection_keys(provider_key)
    try:
        from sqlalchemy import text as _sql

        rows = connection.execute(
            _sql("""SELECT connection_key FROM integration_connections
                    WHERE project_id = :p AND connection_key = ANY(:keys)"""),
            {"p": project_id, "keys": list(candidates)},
        ).scalars().all()
        present = set(rows)
        for candidate in candidates:
            if candidate in present:
                return candidate
    except Exception:  # noqa: BLE001
        # A missing table or an older schema must not turn "fetch a video" into a stack trace
        # about integration_connections. Fall through to the preferred name and let the
        # credential lookup produce the real, specific error.
        pass
    return candidates[0]


def fetch(
    connection: Connection,
    settings: Settings,
    *,
    project_id: str,
    url: str = "",
    link: Optional[ResolvedLink] = None,
    dest_dir: str,
    connection_key: Optional[str] = None,
) -> dict[str, Any]:
    """Bring what a link points at onto local disk.

    Takes the LINK rather than a provider and an id, because that is what a person pastes and
    what a workflow input carries. Returns the same shape `drive_media.download` does, so the
    editing steps did not need to learn anything new.
    """
    target = link or resolve_link(url)
    entry = provider(target.provider_key)

    if target.provider_key == "google":
        from .drive_media import download

        key = _resolve_connection_key(connection, project_id, "google", connection_key)
        return download(connection, settings, project_id=project_id,
                        file_id=target.file_ref, dest_dir=dest_dir, connection_key=key)

    if target.provider_key == "http":
        return _fetch_direct(target, dest_dir=dest_dir)

    raise MediaStorageError(
        f"BOSS can place a {entry.display_name} link but cannot fetch from it yet. "
        f"{_not_implemented_hint(target.provider_key)}")


def media_url(artifact_id: str) -> str:
    """The console's link to a media artifact BOSS holds itself.

    Relative on purpose: the console proxies /v1 to the API behind its own sign-in, the same way
    the change record's PDF link works, so the link opens for whoever can read the project and
    for nobody else.
    """
    return f"/v1/artifacts/{artifact_id}/media"


def provider_connected(connection: Optional[Connection], project_id: str,
                       provider_key: str) -> bool:
    """Whether this project has an active connection to ``provider_key`` — counted, never read.

    The same question `workflow_readiness.check('google')` asks, so a run that readiness let
    start and the step that stores its result cannot disagree about whether Google is there.
    """
    key = str(provider_key or "").strip().lower()
    if key in (LOCAL_PROVIDER, "http"):
        return True
    if connection is None:
        return False
    try:
        from sqlalchemy import text as _sql

        with connection.begin_nested():
            found = connection.execute(
                _sql("""SELECT count(*) FROM integration_connections c
                        JOIN integration_definitions d ON d.id = c.integration_definition_id
                        WHERE c.project_id = :p AND d.integration_key = :k
                          AND c.status = 'active'"""),
                {"p": project_id, "k": key},
            ).scalar()
    except Exception:  # noqa: BLE001 — no table means no connection
        return False
    return bool(found)


def _segment(value: str, what: str) -> str:
    """A project id becomes a directory name, so it may not name any other directory."""
    cleaned = str(value or "").strip()
    if not cleaned or cleaned in (".", "..") or "/" in cleaned or "\\" in cleaned \
            or "\x00" in cleaned:
        raise MediaStorageError(f"{what} {value!r} cannot name a storage directory.")
    return cleaned


def _media_dir(settings: Settings, project_id: str) -> Path:
    return Path(settings.artifact_root).resolve() / _segment(project_id, "project") / "media"


def store_local(settings: Settings, *, project_id: str, local_path: str,
                name: str) -> dict[str, Any]:
    """Keep a finished file in BOSS's own artifact storage, as a NEW file.

    The destination is ``<artifacts>/<project>/media/<random>-<safe name>``: the project is the
    directory (so the route that serves it can check the file belongs to the project asking),
    the random prefix means two edits named the same never meet, and the file is opened with
    exclusive create, so even a collision fails rather than overwrites.
    """
    source = Path(local_path)
    if not source.is_file():
        raise MediaStorageError(f"nothing to store at {local_path}.")
    size = source.stat().st_size
    if size > MAX_DIRECT_BYTES:
        raise MediaStorageError(
            f"that file is {_size(size)}, over BOSS's {_size(MAX_DIRECT_BYTES)} limit for media "
            "it keeps itself.")
    directory = _media_dir(settings, project_id)
    directory.mkdir(parents=True, exist_ok=True)
    display = os.path.basename(str(name or "").replace("\\", "/")) or source.name
    target = directory / f"{uuid.uuid4().hex[:12]}-{_safe_name(display)}"
    try:
        with open(source, "rb") as reader, open(target, "xb") as writer:
            shutil.copyfileobj(reader, writer, 1024 * 1024)
    except FileExistsError as exc:  # pragma: no cover - 48 random bits colliding
        raise MediaStorageError("a stored file already has that name; not overwriting it.") \
            from exc
    except Exception:
        _discard(str(target))
        raise
    root = Path(settings.artifact_root).resolve()
    return {
        "provider": LOCAL_PROVIDER,
        "storage": LOCAL_PROVIDER,
        "local_path": str(target.relative_to(root)),
        "name": display,
        "bytes": size,
        "mime_type": mimetypes.guess_type(display)[0] or "application/octet-stream",
        "file_id": None,
        "view_url": None,
    }


def local_file_for(settings: Settings, project_id: str, content: dict[str, Any]) -> Path:
    """The file a locally stored media artifact names, refusing anything outside its project.

    ``local_path`` is a column value, and a column value is not trusted to choose a path: it is
    resolved (symlinks and ``..`` included) and must land inside THIS project's media directory.
    """
    relative = str((content or {}).get("local_path") or "").strip()
    if not relative:
        raise MediaStorageError("this artifact is not kept in BOSS's own storage.")
    root = Path(settings.artifact_root).resolve()
    expected = _media_dir(settings, project_id)
    path = (root / relative).resolve()
    if not path.is_relative_to(expected):
        raise MediaStorageError("that artifact's file is outside this project's media storage.")
    if not path.is_file():
        raise MediaStorageError(
            "that artifact's file is no longer in BOSS's storage (the artifacts volume may have "
            "been reset).")
    return path


def materialize(
    connection: Connection,
    settings: Settings,
    *,
    project_id: str,
    content: dict[str, Any],
    dest_dir: str,
    connection_key: Optional[str] = None,
) -> str:
    """A local path for a media artifact's file, wherever the artifact keeps it.

    Kept in BOSS: the stored file itself, read in place — the editing steps write to their own
    work directory and never modify their input, so copying gigabytes to read them is waste.
    Kept in Drive: downloaded into ``dest_dir`` with the project's Google connection.
    """
    if (content or {}).get("local_path"):
        return str(local_file_for(settings, project_id, content))
    file_id = str((content or {}).get("drive_file_id") or "").strip()
    if file_id:
        from .drive_media import download

        key = _resolve_connection_key(connection, project_id, "google", connection_key)
        return download(connection, settings, project_id=project_id, file_id=file_id,
                        dest_dir=dest_dir, connection_key=key)["path"]
    raise MediaStorageError("that artifact names no media file — neither stored nor in Drive.")


def store(
    connection: Connection,
    settings: Settings,
    *,
    project_id: str,
    local_path: str,
    name: str,
    provider_key: Optional[str] = None,
    container: str = "",
    connection_key: Optional[str] = None,
    actor: str = "system",
) -> dict[str, Any]:
    """Put a finished file where the project keeps its media, as a NEW file.

    Never an overwrite, for any provider. A pipeline that can overwrite is a pipeline that will
    eventually overwrite the only copy of something.

    WHERE IT GOES. BOSS's own artifact storage, unless the workflow named a provider AND that
    provider is connected. This used to default to Google, which made a Google connection a
    precondition of every edit — even of a video fetched from a direct link, edited, and only
    ever meant to be watched in the console. Google is needed to publish to YouTube, and to read
    or write Drive when a workflow asks for that; it is not needed to keep an intermediate file.
    A named provider that is not connected falls back to BOSS's storage and says so in `note`,
    rather than stopping a run whose only use of that provider was somewhere to put a file.
    """
    asked = str(provider_key or "").strip().lower()
    if asked in ("", LOCAL_PROVIDER, "local"):
        return store_local(settings, project_id=project_id, local_path=local_path, name=name)

    entry = provider(asked)
    if CAP_WRITE not in entry.capabilities:
        raise MediaStorageError(
            f"{entry.display_name} is read-only — an edited video cannot be written back to it. "
            "Give the workflow an output provider that can be written to.")

    if asked == "google":
        if not provider_connected(connection, project_id, "google"):
            stored = store_local(settings, project_id=project_id, local_path=local_path,
                                 name=name)
            stored["note"] = ("Google is not connected to this project, so this was kept in "
                              "BOSS's own storage instead of Drive.")
            return stored
        from .drive_media import upload

        key = _resolve_connection_key(connection, project_id, "google", connection_key)
        written = upload(connection, settings, project_id=project_id, local_path=local_path,
                         name=name, folder_id=container, connection_key=key, actor=actor)
        return {**written, "provider": "google", "storage": "google"}

    raise MediaStorageError(
        f"BOSS cannot store to {entry.display_name} yet. "
        f"{_not_implemented_hint(provider_key)}")


def _not_implemented_hint(provider_key: str) -> str:
    """What the operator can actually do about it, rather than "not implemented"."""
    if provider_key == "microsoft":
        return ("OneDrive and SharePoint need Microsoft Graph, which is a separate connection "
                "from Google and is not built yet. Copy the file to a connected Google Drive, "
                "or use a direct HTTPS link.")
    if provider_key == "s3":
        return ("S3-compatible storage needs request signing, which is not built yet. A "
                "pre-signed HTTPS URL to the object works today as a direct link.")
    return "Use a Google Drive link or a direct HTTPS link."


def _fetch_direct(target: ResolvedLink, *, dest_dir: str) -> dict[str, Any]:
    """Download a public URL, refusing anything that points back inside our own network.

    THE GUARD IS APPLIED TWICE AND THAT IS NOT REDUNDANT. `resolve_link` checked the hostname
    when the link was placed; between then and now the name can resolve to something else, which
    is a DNS-rebinding fetch of exactly the metadata endpoint the first check refused. So the
    address is resolved again here, and the TCP connection is pinned to THAT address — the HTTP
    client must not look the name up itself. Redirects are refused outright rather than
    followed: a public URL that 302s to 169.254.169.254 is the same attack wearing a hat.
    """
    from .media_providers import public_address_for

    url = target.original_url or target.file_ref
    host = urlparse(url).hostname or ""
    pinned_ip = public_address_for(host)

    try:
        import httpx
    except ImportError as exc:  # pragma: no cover - httpx ships in every BOSS image
        raise MediaStorageError("httpx is not available to fetch a direct link.") from exc

    os.makedirs(dest_dir, exist_ok=True)
    name = os.path.basename(urlparse(url).path) or "download"
    path = os.path.join(dest_dir, _safe_name(name))
    written = 0
    try:
        # follow_redirects=False DELIBERATELY. See the docstring: a redirect is how a link that
        # passed the address check reaches an address that would not have.
        # The transport connects to pinned_ip while the URL keeps the original host, so TLS
        # still checks the name on the certificate and a later DNS answer cannot redirect it.
        with httpx.Client(timeout=60.0, follow_redirects=False,
                          transport=_transport_pinned_to(host, pinned_ip)) as client:
            with client.stream("GET", url) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    raise MediaStorageError(
                        f"that link redirects to {response.headers.get('location', 'elsewhere')}. "
                        "BOSS does not follow redirects on a media link — give it the final URL.")
                response.raise_for_status()
                declared = response.headers.get("content-length")
                if declared and int(declared) > MAX_DIRECT_BYTES:
                    raise MediaStorageError(
                        f"that file is {_size(int(declared))}, over BOSS's "
                        f"{_size(MAX_DIRECT_BYTES)} limit for a direct download.")
                with open(path, "wb") as handle:
                    for chunk in response.iter_bytes(1024 * 256):
                        written += len(chunk)
                        # Checked while streaming as well, because content-length is a claim and
                        # a chunked response makes none at all.
                        if written > MAX_DIRECT_BYTES:
                            raise MediaStorageError(
                                "that download exceeded BOSS's size limit part-way through.")
                        handle.write(chunk)
    except MediaStorageError:
        _discard(path)
        raise
    except Exception as exc:  # noqa: BLE001
        _discard(path)
        raise MediaStorageError(f"could not fetch {url}: {exc}") from exc

    if written == 0:
        _discard(path)
        raise MediaStorageError(f"{url} returned an empty file.")
    return {"path": path, "name": os.path.basename(path), "bytes": written,
            "provider": "http", "source_url": url}


def _transport_pinned_to(hostname: str, ip: str):
    """An HTTP transport that dials ``ip`` whenever the request names ``hostname``.

    The URL itself is left alone. Rewriting it to the IP would make TLS check a certificate
    for the address rather than the name, and would put the raw IP in the Host header. Dialing
    the pinned address with the original URL keeps both, and leaves no second DNS lookup.
    """
    import httpcore
    import httpx

    aliases = {hostname, hostname.lower()}
    try:
        aliases.add(hostname.encode("idna").decode("ascii"))
    except UnicodeError:
        pass

    class _PinnedBackend(httpcore.SyncBackend):
        def connect_tcp(self, host, port, timeout=None, local_address=None,
                        socket_options=None):
            if host in aliases:
                host = ip
            return super().connect_tcp(
                host, port, timeout=timeout, local_address=local_address,
                socket_options=socket_options,
            )

    transport = httpx.HTTPTransport(retries=0)
    # ConnectionPool takes the backend only at construction, and HTTPTransport does not expose
    # that argument. The pool has not connected yet, so replacing the backend is the dial.
    transport._pool._network_backend = _PinnedBackend()
    return transport


def _size(count: int) -> str:
    """Readable at every magnitude. "0 GB" is not a limit anybody can act on."""
    for unit, scale in (("GB", 1024**3), ("MB", 1024**2), ("KB", 1024)):
        if count >= scale:
            return f"{count / scale:.1f} {unit}".replace(".0 ", " ")
    return f"{count} bytes"


def _discard(path: str) -> None:
    """A failed fetch leaves nothing behind. A truncated video is worse than no video: it
    probes, it plays for a second, and it looks like a source somebody could edit."""
    try:
        os.path.exists(path) and os.remove(path)
    except OSError:
        pass


def _safe_name(name: str) -> str:
    """A filename from a URL is operator-supplied, so it may not choose where it lands."""
    cleaned = "".join(c for c in name if c.isalnum() or c in "._- ").strip() or "download"
    return cleaned[:120]


def describe(url: str) -> dict[str, Any]:
    """Place a link and say what BOSS could do with it, without fetching anything.

    What a console field or a workflow's first step wants: whether this link is usable, by which
    connection, before anything commits to a download.
    """
    target = resolve_link(url)
    entry = provider(target.provider_key)
    fetchable = target.provider_key in ("google", "http")
    return {
        "provider": entry.key,
        "provider_name": entry.display_name,
        "file_ref": target.file_ref,
        "container": target.container,
        "needs_connection": entry.auth_style != AUTH_NONE,
        "default_connection_keys": list(default_connection_keys(entry.key)),
        "fetchable_today": fetchable,
        "note": None if fetchable else _not_implemented_hint(target.provider_key),
    }
