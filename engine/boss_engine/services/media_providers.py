"""Which media provider a link belongs to, and what BOSS may do with it.

WHY THIS EXISTS
---------------
The media lane was built against Google because that is the account that was connected. The
result reads as generic and is not: `channel_oauth` bakes in Google's three OAuth endpoints and
its scopes, `drive_media` bakes in Drive's REST URLs, and every storage call defaults its
`connection_key` to ``youtube_default`` — a YouTube-flavoured default on a file download. Adding
OneDrive that way means a second module with Microsoft's endpoints baked in, and adding S3 means
a third with no OAuth at all, because S3 has none.

So the provider-shaped facts move here, into data, and the code above it works in terms of
CAPABILITIES. That split was already the design's intent — `channel_oauth` says capabilities are
named rather than scoped "because a connection grants BOSS capabilities and the scopes are how
Google happens to express them" — but there was one provider, so there was nowhere to put it.

WHAT IS DELIBERATELY *NOT* HERE
-------------------------------
Credentials. A provider entry describes how to ASK for a credential; it never holds one. Those
stay in `secret_backends` behind a `secret_refs` row, reached through an
`integration_connections` row for the project. That indirection is the point: reading a token
from install-wide settings is how `settings.github_token` made every project's preview clone
with one credential, and `digitalocean_adapter` still reads `settings.digitalocean_token` the
same way. A provider added here cannot repeat it, because there is no settings field to read.
"""
from __future__ import annotations

import ipaddress
import re
import socket
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import parse_qs, unquote, urlparse


class MediaProviderError(ValueError):
    """A link or provider BOSS cannot work with. Carries what the operator should do instead."""


#: How a provider proves who BOSS is. The distinction is not cosmetic: an oauth2_refresh provider
#: needs a consent round trip and mints a short-lived token per call, an api_key provider is one
#: long-lived secret, and an anonymous one has no credential and therefore no per-project
#: connection to check — which is why it gets the tightest restrictions on what it may reach.
AUTH_OAUTH2_REFRESH = "oauth2_refresh"
AUTH_API_KEY = "api_key"
AUTH_SIGNED_REQUEST = "signed_request"
AUTH_NONE = "none"

#: Capabilities, named in BOSS's terms. A skill requires these; a provider maps them to whatever
#: its API calls them. `requires_connection: google` in eleven skill specs is what this replaces
#: — a provider name in a policy field, which is also enforced nowhere today.
CAP_READ = "storage.read"
CAP_WRITE = "storage.write"
CAP_PUBLISH = "video.publish"


@dataclass(frozen=True)
class MediaProvider:
    key: str
    display_name: str
    auth_style: str
    capabilities: tuple[str, ...]
    #: BOSS capability -> the provider's own scope/permission string. Empty for providers whose
    #: credential is not scoped at all, which is most api_key ones.
    scopes: dict[str, tuple[str, ...]] = field(default_factory=dict)
    auth_endpoint: str = ""
    token_endpoint: str = ""
    revoke_endpoint: str = ""
    #: What an operator must supply to connect. Named so the console and the installer can ask
    #: for the right things instead of a generic "paste a credential" box.
    credential_fields: tuple[str, ...] = ()
    setup_hint: str = ""

    def scopes_for(self, capabilities: tuple[str, ...]) -> tuple[str, ...]:
        """The provider scopes needed for these BOSS capabilities, deduplicated and ordered.

        Asking for a capability this provider does not have is an error rather than an empty
        result: a connection minted without the scope it needed fails later, at the call, with a
        provider error nobody can trace back to the consent screen.
        """
        wanted: list[str] = []
        for capability in capabilities:
            if capability not in self.capabilities:
                raise MediaProviderError(
                    f"{self.display_name} cannot do {capability!r} — it offers "
                    f"{', '.join(self.capabilities)}.")
            for scope in self.scopes.get(capability, ()):
                if scope not in wanted:
                    wanted.append(scope)
        return tuple(wanted)


PROVIDERS: dict[str, MediaProvider] = {
    "google": MediaProvider(
        key="google",
        display_name="Google Drive / YouTube",
        auth_style=AUTH_OAUTH2_REFRESH,
        capabilities=(CAP_READ, CAP_WRITE, CAP_PUBLISH),
        # ONE CONNECTION, BOTH USES. Keyed on the provider rather than on YouTube because the
        # same token reaches Drive, and an integration named for one of its two uses makes the
        # other look like it came from somewhere else.
        scopes={
            CAP_READ: ("https://www.googleapis.com/auth/drive",),
            CAP_WRITE: ("https://www.googleapis.com/auth/drive",),
            CAP_PUBLISH: ("https://www.googleapis.com/auth/youtube.upload",
                          "https://www.googleapis.com/auth/youtube.readonly"),
        },
        auth_endpoint="https://accounts.google.com/o/oauth2/v2/auth",
        token_endpoint="https://oauth2.googleapis.com/token",
        revoke_endpoint="https://oauth2.googleapis.com/revoke",
        credential_fields=("client_id", "client_secret"),
        setup_hint="A Google Cloud OAuth client (Desktop or Web) with the Drive and YouTube "
                   "Data APIs enabled.",
    ),
    "microsoft": MediaProvider(
        key="microsoft",
        display_name="OneDrive / SharePoint",
        auth_style=AUTH_OAUTH2_REFRESH,
        capabilities=(CAP_READ, CAP_WRITE),
        # NOT A GOOGLE ACCOUNT, which is worth stating because the two get conflated. OneDrive is
        # Microsoft Graph behind an Entra app registration: different endpoints, different scopes,
        # and a Google connection cannot reach it however many Drive scopes it holds.
        scopes={
            CAP_READ: ("offline_access", "Files.Read.All"),
            CAP_WRITE: ("offline_access", "Files.ReadWrite.All"),
        },
        # `common` rather than a tenant id: a personal OneDrive and a work SharePoint both work,
        # and pinning a tenant here would make this entry wrong for one of them.
        auth_endpoint="https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
        token_endpoint="https://login.microsoftonline.com/common/oauth2/v2.0/token",
        credential_fields=("client_id", "client_secret"),
        setup_hint="An Entra (Azure AD) app registration with delegated Files.ReadWrite.All "
                   "and offline_access.",
    ),
    "s3": MediaProvider(
        key="s3",
        display_name="S3-compatible storage (S3, R2, Backblaze B2)",
        auth_style=AUTH_SIGNED_REQUEST,
        capabilities=(CAP_READ, CAP_WRITE),
        # NO SCOPES AND NO CONSENT SCREEN. The credential is a key pair and every request is
        # signed. What it may reach is decided by the bucket policy on the provider's side, which
        # BOSS cannot see — so the connection's own max_risk_tier and granted_capabilities are the
        # only limits BOSS can enforce, and they matter more here than for an OAuth provider.
        credential_fields=("access_key_id", "secret_access_key", "endpoint_url", "region"),
        setup_hint="An access key scoped to the one bucket holding the media. BOSS cannot read "
                   "the bucket policy, so grant it narrowly there.",
    ),
    "http": MediaProvider(
        key="http",
        display_name="Direct HTTPS link",
        auth_style=AUTH_NONE,
        capabilities=(CAP_READ,),
        # READ ONLY, FOREVER. There is nothing to write to and no account to write as.
        setup_hint="No connection needed. The link must be publicly reachable, and BOSS refuses "
                   "anything on a private or loopback address.",
    ),
}


def provider(key: str) -> MediaProvider:
    entry = PROVIDERS.get(str(key or "").strip().lower())
    if not entry:
        raise MediaProviderError(
            f"unknown media provider {key!r} — BOSS has {', '.join(sorted(PROVIDERS))}.")
    return entry


# ---------------------------------------------------------------------------------------------
# Link resolution.
#
# The workflow's input is a link somebody pasted. Which provider it belongs to, and which file it
# names, is decided HERE and by pattern alone — no network call, so a malformed or hostile link
# costs nothing and the whole thing is testable without a single credential.
# ---------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class ResolvedLink:
    provider_key: str
    #: What the provider calls the thing. A Drive file id, a Graph share id, a bucket key.
    file_ref: str
    #: Set for S3-compatibles, where the file_ref alone is ambiguous without it.
    container: str = ""
    original_url: str = ""
    #: True when the link identifies a file the provider will name on fetch rather than now — a
    #: OneDrive share link is a token, not a filename.
    name_unknown: bool = False


_GOOGLE_FILE = re.compile(r"/(?:file|document|presentation|spreadsheets)/d/([A-Za-z0-9_-]{10,})")
_SHAREPOINT_HOST = re.compile(r"(^|\.)sharepoint\.com$", re.I)

#: Sites whose links are a PLAYER PAGE, not a file. Fetching one returns HTML, and the failure
#: used to surface as ffmpeg's "Invalid data found when processing input" — after a download —
#: which told nobody that the link itself was the problem (the video example, run live with a
#: YouTube watch link). Refused by host, before anything is fetched. Matched on the registrable
#: domain, so `www.`, `m.` and `music.` variants are covered.
_VIDEO_PAGE_SITES = {
    "youtube.com": "YouTube", "youtu.be": "YouTube", "youtube-nocookie.com": "YouTube",
    "vimeo.com": "Vimeo", "dailymotion.com": "Dailymotion", "dai.ly": "Dailymotion",
    "tiktok.com": "TikTok", "instagram.com": "Instagram", "facebook.com": "Facebook",
    "fb.watch": "Facebook", "twitch.tv": "Twitch", "x.com": "X", "twitter.com": "X",
}


def _video_page_site(host: str) -> str:
    for domain, site in _VIDEO_PAGE_SITES.items():
        if host == domain or host.endswith("." + domain):
            return site
    return ""


def resolve_link(url: str) -> ResolvedLink:
    """Work out which provider a pasted link belongs to, and what it points at.

    Refuses rather than guesses. A link BOSS cannot place is an instruction it cannot carry out,
    and the honest failure is at the start of the workflow — not three steps in, when an editing
    step gets a login page instead of a video.
    """
    raw = str(url or "").strip()
    if not raw:
        raise MediaProviderError("a media link is required.")
    if len(raw) > 2048:
        raise MediaProviderError("that link is implausibly long; BOSS will not follow it.")

    # s3://bucket/key, which has no host in the http sense.
    if raw.lower().startswith("s3://"):
        rest = raw[5:].strip("/")
        bucket, _, key = rest.partition("/")
        if not bucket or not key:
            raise MediaProviderError("an s3:// link must name a bucket and a key.")
        return ResolvedLink("s3", file_ref=key, container=bucket, original_url=raw)

    parsed = urlparse(raw)
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        # file://, gopher://, data: — a link that is not a web fetch has no business here, and
        # `file://` in particular would turn a pasted link into "read any path on the host".
        raise MediaProviderError(
            f"BOSS follows http and https links only — {scheme or 'that'} is refused.")
    host = (parsed.hostname or "").lower()
    if not host:
        raise MediaProviderError("that link names no host.")

    # --- Google ---------------------------------------------------------------------------
    if host in ("drive.google.com", "docs.google.com", "drive.usercontent.google.com"):
        found = _GOOGLE_FILE.search(parsed.path)
        if found:
            return ResolvedLink("google", file_ref=found.group(1), original_url=raw)
        ident = parse_qs(parsed.query).get("id", [""])[0]
        if ident:
            return ResolvedLink("google", file_ref=ident, original_url=raw)
        raise MediaProviderError(
            "that Google link names no file id. A share link looks like "
            "https://drive.google.com/file/d/<id>/view.")

    # --- Microsoft ------------------------------------------------------------------------
    if host in ("1drv.ms", "onedrive.live.com") or _SHAREPOINT_HOST.search(host):
        # A OneDrive or SharePoint share URL is an opaque token, not a path. Graph takes the
        # whole URL base64url-encoded as a share id, so the URL itself IS the reference and
        # there is nothing to parse out of it — including the filename.
        return ResolvedLink("microsoft", file_ref=raw, original_url=raw, name_unknown=True)

    # --- S3-compatible --------------------------------------------------------------------
    path = unquote(parsed.path or "").lstrip("/")
    for suffix, style in (
        (".r2.cloudflarestorage.com", "path"),      # <account>.r2.../<bucket>/<key>
        (".backblazeb2.com", "path"),
        (".digitaloceanspaces.com", "vhost"),       # <bucket>.<region>.digitalocean...
    ):
        if host.endswith(suffix):
            return _s3_link(host, path, suffix, style, raw)
    if ".amazonaws.com" in host and ("s3" in host):
        style = "vhost" if not host.startswith("s3.") and not host.startswith("s3-") else "path"
        return _s3_link(host, path, ".amazonaws.com", style, raw)

    # --- A video site's page, which is not a file ------------------------------------------
    site = _video_page_site(host)
    if site:
        raise MediaProviderError(
            f"that is a {site} page, not a video file — BOSS cannot download from {site}. "
            "Use a Google Drive share link, or a direct link to the file itself (.mp4, .mov).")

    # --- Anything else: a direct file, if it is safe to fetch -----------------------------
    _refuse_unroutable(host)
    if not path:
        raise MediaProviderError(
            "that link names a host but no file. A direct link must point at the file itself.")
    return ResolvedLink("http", file_ref=raw, original_url=raw,
                        name_unknown=not path.rpartition("/")[2])


def _s3_link(host: str, path: str, suffix: str, style: str, raw: str) -> ResolvedLink:
    if style == "vhost":
        bucket = host[: -len(suffix)].split(".")[0]
        key = path
    else:
        bucket, _, key = path.partition("/")
    if not bucket or not key:
        raise MediaProviderError(
            f"could not read a bucket and a key out of {raw!r}.")
    return ResolvedLink("s3", file_ref=key, container=bucket, original_url=raw)


def _address_is_unroutable(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Whether this address is inside a network a media link must not reach.

    ``is_private`` IS THE WRONG TEST BY ITSELF. Python documents that ``100.64.0.0/10``
    (RFC 6598 shared address space) has ``is_private is False`` — and ``is_global`` is False
    too, which is the only flag that catches it. That range is not a curiosity: ``100.100.100.200``
    is Alibaba Cloud's instance-metadata address, and ``100.64.0.0/10`` is the CGNAT block a
    tailnet uses. Checking only the private/loopback/link-local/reserved flags lets a pasted
    link fetch either one.

    Multicast stays listed on its own because ``224.0.0.0/4`` reports ``is_global`` True.
    """
    return (
        not ip.is_global
        or ip.is_multicast
        or ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_unspecified
    )


def _resolved_addresses(host: str) -> Optional[list[str]]:
    """TCP addresses for ``host``. None when DNS fails, so a placement-time check can
    stay lenient while a fetch treats that as a refusal."""
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError:
        return None
    addresses: list[str] = []
    for info in infos:
        address = info[4][0]
        if address not in addresses:
            addresses.append(address)
    return addresses


def _refuse_addresses(host: str, addresses: list[str]) -> None:
    for address in addresses:
        literal = address.split("%", 1)[0]
        try:
            ip = ipaddress.ip_address(literal)
        except ValueError:
            raise MediaProviderError(
                f"{host} resolves to {address}, which BOSS will not fetch.") from None
        if _address_is_unroutable(ip):
            raise MediaProviderError(
                f"{host} resolves to {ip}, which is inside BOSS's own network. A media link "
                "must point at something on the public internet — cloud metadata endpoints and "
                "internal services are refused.")


def _refuse_unroutable(host: str) -> None:
    """Refuse a direct link that points back inside the network BOSS runs in.

    THIS IS THE ONE THAT MATTERS. The link is an untrusted input — a workflow input a person
    pasted, or a URL a model put in a plan. Without this check, "fetch this video" fetches
    http://169.254.169.254/latest/meta-data/iam/security-credentials/ and hands the control
    plane's own cloud role to whoever asked, or reaches the BOSS API on localhost as a caller
    with no authentication to answer for.

    A name that does not resolve here is not refused: a DNS blip at the moment someone pastes
    a link should not reject a public file. The fetch resolves again and pins that answer, so
    a blip cannot become a second, unchecked lookup.
    """
    addresses = _resolved_addresses(host)
    if not addresses:
        # A name that does not resolve here is not necessarily hostile, and refusing on a DNS
        # blip would reject good links. The fetch resolves again and refuses if that fails too.
        return
    _refuse_addresses(host, addresses)


def public_address_for(host: str) -> str:
    """The single address a fetch of ``host`` must open a TCP connection to.

    ONE LOOKUP, USED FOR THE CONNECTION. Resolving again inside the HTTP client is how a name
    that passed this check connects to a different address — a public answer here, then
    ``169.254.169.254`` on the lookup the client does itself. The caller pins the returned
    address and must not resolve the name a second time.

    DNS failure is a refusal here, unlike placement. There is no safe fetch of a name that
    did not resolve to a public address.
    """
    addresses = _resolved_addresses(host)
    if not addresses:
        raise MediaProviderError(
            f"{host} did not resolve, so BOSS will not fetch it. A media link has to resolve "
            "to a public address.")
    _refuse_addresses(host, addresses)
    return addresses[0]


def describe_providers() -> list[dict]:
    """What an operator can connect, for the console and the setup docs.

    Carries `credential_fields` so a connect screen can ask for the right things rather than
    offering one box labelled "credential" for four providers that need different ones.
    """
    return [
        {
            "key": entry.key,
            "display_name": entry.display_name,
            "auth_style": entry.auth_style,
            "capabilities": list(entry.capabilities),
            "credential_fields": list(entry.credential_fields),
            "setup_hint": entry.setup_hint,
        }
        for entry in sorted(PROVIDERS.values(), key=lambda p: p.display_name)
    ]
