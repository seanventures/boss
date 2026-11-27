"""boss-egress reverse proxy for authenticated package registries (BOSSAPP-1418).

    /npm/<path>     -> BOSS_NPM_REGISTRY
    /pypi/<path>    -> BOSS_PIP_INDEX_URL
    /maven/<path>   -> BOSS_MAVEN_MIRROR

A sandbox talks plain HTTP to http://egress:8090/{npm,pypi,maven}/ (which is already open to
sandbox UIDs in scripts/sandbox/entrypoint.sh and bypassed in NO_PROXY). This proxy loads the
registry credential from BOSS's secret store (or egress-only environment variables), strips all
inbound credential headers, injects Authorization only for the configured upstream origin, rewrites
tarball/wheel URLs in npm packuments and PyPI Simple indexes back through egress, and never
forwards Authorization on cross-origin redirects.
"""
from __future__ import annotations

import base64
import html
import json
import logging
import os
import re
from typing import Any, Mapping, Optional
from urllib.parse import SplitResult, quote, unquote, urljoin, urlsplit

from boss_engine.identity import egress_rules
from boss_engine.services import enterprise_network

from .proxy import EgressRequest, EgressResponse, Upstream, upstream_url

log = logging.getLogger("boss.egress.registry")

REGISTRY_BASE_ENV: Mapping[str, str] = {
    "npm": "BOSS_NPM_REGISTRY",
    "pypi": "BOSS_PIP_INDEX_URL",
    "maven": "BOSS_MAVEN_MIRROR",
}
REGISTRY_ENV_PREFIX: Mapping[str, str] = {
    "npm": "NPM",
    "pypi": "PIP",
    "maven": "MAVEN",
}

_REGISTRY_FORWARD = frozenset({
    "accept",
    "accept-encoding",
    "user-agent",
    "if-none-match",
    "if-modified-since",
    "range",
    "npm-auth-type",
    "npm-command",
})
_REFUSE_HEADERS = frozenset({"x-http-method-override", "x-http-method", "x-method-override"})
_PASS_RESPONSE = frozenset({
    "content-type",
    "cache-control",
    "etag",
    "last-modified",
    "accept-ranges",
    "content-range",
    "x-pypi-last-serial",
})
_OTHER_ENCODED_STRUCTURE = re.compile(r"%(5c|2e|25|00)", re.IGNORECASE)
_ENCODED_SLASH = re.compile(r"%2f", re.IGNORECASE)
_PCHAR_SAFE = ":@!$&'()*+,;=-._~"
_HREF_RE = re.compile(r"""href\s*=\s*(["'])([^"']+)\1""", re.IGNORECASE)
_MAX_REDIRECTS = 3


def _default_port(parts: SplitResult) -> int:
    if parts.port is not None:
        return parts.port
    return 443 if parts.scheme.lower() == "https" else 80


def _origin_tuple(parts: SplitResult) -> tuple[str, str, int]:
    return (parts.scheme.lower(), (parts.hostname or "").lower(), _default_port(parts))


def canonical_registry_path(raw: str, kind: str) -> tuple[str, str]:
    """Validate and canonicalize a registry request path.

    Allows `%2f` / `%2F` ONLY inside `@scope%2fpkg` segments for `npm`; refuses traversal,
    backslashes, control chars, double slashes, and all other encoded structural characters.
    """
    if not raw.startswith("/") or len(raw) > 2048:
        raise egress_rules.PathRefused("bad_path")
    if "\\" in raw or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in raw):
        raise egress_rules.PathRefused("bad_path_characters")
    if _OTHER_ENCODED_STRUCTURE.search(raw):
        raise egress_rules.PathRefused("encoded_path_separator")
    if kind != "npm" and _ENCODED_SLASH.search(raw):
        raise egress_rules.PathRefused("encoded_path_separator")
    if raw == "/":
        return "/", "/"
    trailing = raw.endswith("/")
    trimmed = raw[1:-1] if trailing else raw[1:]
    segments = trimmed.split("/")
    decoded_segments: list[str] = []
    encoded_segments: list[str] = []
    for seg in segments:
        if seg in ("", ".", ".."):
            raise egress_rules.PathRefused("dot_or_empty_segment")
        slash_matches = list(_ENCODED_SLASH.finditer(seg))
        if slash_matches:
            if kind != "npm" or not seg.startswith("@") or len(slash_matches) != 1:
                raise egress_rules.PathRefused("encoded_path_separator")
            sep = slash_matches[0].group(0)
            raw_scope, raw_pkg = seg[:slash_matches[0].start()], seg[slash_matches[0].end():]
            if not raw_scope or not raw_pkg or raw_scope == "@":
                raise egress_rules.PathRefused("dot_or_empty_segment")
            try:
                dec_scope = unquote(raw_scope, errors="strict")
                dec_pkg = unquote(raw_pkg, errors="strict")
            except UnicodeDecodeError as exc:
                raise egress_rules.PathRefused("bad_encoding") from exc
            for part in (dec_scope, dec_pkg):
                if (
                    part in ("", ".", "..")
                    or "/" in part
                    or "\\" in part
                    or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in part)
                ):
                    raise egress_rules.PathRefused("dot_or_empty_segment")
            decoded_segments.append(f"{dec_scope}/{dec_pkg}")
            encoded_segments.append(
                f"{quote(dec_scope, safe=_PCHAR_SAFE)}{sep}{quote(dec_pkg, safe=_PCHAR_SAFE)}"
            )
            continue
        try:
            dec = unquote(seg, errors="strict")
        except UnicodeDecodeError as exc:
            raise egress_rules.PathRefused("bad_encoding") from exc
        if (
            dec in ("", ".", "..")
            or "/" in dec
            or "\\" in dec
            or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in dec)
        ):
            raise egress_rules.PathRefused("dot_or_empty_segment")
        decoded_segments.append(dec)
        encoded_segments.append(quote(dec, safe=_PCHAR_SAFE))
    suffix = "/" if trailing else ""
    return "/" + "/".join(decoded_segments) + suffix, "/" + "/".join(encoded_segments) + suffix


def _format_auth_header(raw_secret: str, kind: str) -> Optional[dict[str, str]]:
    secret = (raw_secret or "").strip()
    if not secret:
        return None
    if secret.lstrip().startswith("{"):
        try:
            parsed = json.loads(secret)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            user = str(parsed.get("username") or "").strip()
            pw = str(parsed.get("password") or "").strip()
            if user and pw:
                encoded = base64.b64encode(f"{user}:{pw}".encode("utf-8")).decode("ascii")
                return {"Authorization": f"Basic {encoded}"}
            if pw and kind == "pypi":
                encoded = base64.b64encode(f"__token__:{pw}".encode("utf-8")).decode("ascii")
                return {"Authorization": f"Basic {encoded}"}
            tok = str(parsed.get("token") or parsed.get("auth") or "").strip()
            if tok:
                secret = tok
    lower = secret.lower()
    if lower.startswith("bearer "):
        return {"Authorization": "Bearer " + secret[7:].strip()}
    if lower.startswith("basic "):
        return {"Authorization": "Basic " + secret[6:].strip()}
    if ":" in secret:
        encoded = base64.b64encode(secret.encode("utf-8")).decode("ascii")
        return {"Authorization": f"Basic {encoded}"}
    if kind == "pypi":
        encoded = base64.b64encode(f"__token__:{secret}".encode("utf-8")).decode("ascii")
        return {"Authorization": f"Basic {encoded}"}
    return {"Authorization": f"Bearer {secret}"}


def _read_store_key(key: str, db: Any) -> Optional[str]:
    from boss_engine.config import get_settings
    from boss_engine.services import secret_backends

    settings = get_settings()
    try:
        val = secret_backends.resolve_system_ref(settings, [key], connection=db)
        if val:
            return val
    except secret_backends.SecretUnavailable:
        raise
    except Exception:  # noqa: BLE001
        pass
    try:
        backend_name = secret_backends.default_secret_backend(settings)
        backend = secret_backends.backend_for(backend_name, settings)
        val = backend.get(key)
    except secret_backends.SecretNotFound:
        return None
    if val:
        secret_backends.record_system_read(settings, key, connection=db)
    return val


def resolve_registry_credential(kind: str, db: Any = None) -> Optional[dict[str, str]]:
    """Resolve upstream Authorization header for `npm` / `pypi` / `maven`, never exposed to sandboxes."""
    upper = REGISTRY_ENV_PREFIX.get(kind, kind.upper())
    ref = (os.environ.get(f"BOSS_{upper}_CREDENTIAL_REF") or "").strip()
    if ref.startswith("secret_ref:"):
        from boss_engine.config import get_settings
        from boss_engine.services import secret_backends

        ref_id = ref[len("secret_ref:"):].strip()
        if not ref_id or db is None:
            raise secret_backends.SecretUnavailable(f"BOSS_{upper}_CREDENTIAL_REF requires a database session")
        raw = secret_backends.resolve_secret_ref(db, ref_id, get_settings())
        return _format_auth_header(raw, kind)
    if ref.startswith("store:"):
        key = ref[len("store:"):].strip()
        raw = _read_store_key(key, db)
        return _format_auth_header(raw or "", kind)

    token = (os.environ.get(f"BOSS_{upper}_TOKEN") or "").strip()
    username = (os.environ.get(f"BOSS_{upper}_USERNAME") or "").strip()
    password = (os.environ.get(f"BOSS_{upper}_PASSWORD") or "").strip()
    if username and password:
        return _format_auth_header(f"{username}:{password}", kind)
    if token:
        return _format_auth_header(token, kind)
    if password:
        user = username or ("__token__" if kind == "pypi" else "")
        return _format_auth_header(f"{user}:{password}" if user else password, kind)
    if ref.lower() in ("env", "1", "true", "yes"):
        raw = _read_store_key(f"boss/registry/{kind}", db)
        if raw:
            return _format_auth_header(raw, kind)
    return None


def _registry_upstream(method: str, url: str, headers: Mapping[str, str], body: bytes) -> EgressResponse:
    import ssl

    import httpx

    ca = (os.environ.get("SSL_CERT_FILE") or os.environ.get("GIT_SSL_CAINFO") or "").strip()
    verify: Any = ssl.create_default_context(cafile=ca) if ca else True
    with httpx.Client(timeout=60.0, follow_redirects=False, verify=verify) as client:
        request = client.build_request(method, httpx.URL(url), headers=dict(headers), content=body or None)
        resp = client.send(request)
        return EgressResponse(resp.status_code, dict(resp.headers), resp.content)


def _proxy_base_url(req: EgressRequest) -> str:
    if req.url:
        parts = urlsplit(req.url)
        if parts.scheme and parts.netloc:
            return f"{parts.scheme}://{parts.netloc}"
    host = (req.headers.get("host") or "").strip()
    if host and not any(ch.isspace() or ch in "/\\" for ch in host):
        return f"http://{host}"
    return (os.environ.get("BOSS_EGRESS_URL") or "").strip().rstrip("/")


def _rewrite_url(
    target: str,
    *,
    kind: str,
    base_parts: SplitResult,
    proxy_base: str,
) -> str:
    raw = target.strip()
    if not raw or raw.startswith(("#", "data:", "javascript:", "mailto:")):
        return target
    if raw.startswith("//"):
        abs_url = f"{base_parts.scheme}:{raw}"
    elif raw.startswith("/"):
        abs_url = f"{base_parts.scheme}://{base_parts.netloc}{raw}"
    elif raw.startswith(("http://", "https://")):
        abs_url = raw
    else:
        return target
    try:
        tp = urlsplit(abs_url)
    except ValueError:
        return target
    if _origin_tuple(tp) != _origin_tuple(base_parts):
        return target
    base_path = base_parts.path.rstrip("/")
    if tp.path == base_path or tp.path.startswith(base_path + "/"):
        tail = tp.path[len(base_path):] or "/"
        rewritten = f"{proxy_base}/{kind}{tail}"
    else:
        rewritten = f"{proxy_base}/{kind}/_origin{tp.path}"
    if tp.query:
        rewritten += f"?{tp.query}"
    if tp.fragment:
        rewritten += f"#{tp.fragment}"
    return rewritten


def _rewrite_npm_json(body: bytes, *, base_parts: SplitResult, proxy_base: str) -> bytes:
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return body

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            for k, v in list(node.items()):
                if k == "tarball" and isinstance(v, str):
                    node[k] = _rewrite_url(v, kind="npm", base_parts=base_parts, proxy_base=proxy_base)
                else:
                    walk(v)
        elif isinstance(node, list):
            for item in node:
                walk(item)
        return node

    walk(data)
    return json.dumps(data, separators=(",", ":")).encode("utf-8")


def _rewrite_pypi_json(body: bytes, *, base_parts: SplitResult, proxy_base: str) -> bytes:
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return body
    if isinstance(data, dict) and isinstance(data.get("files"), list):
        for entry in data["files"]:
            if isinstance(entry, dict) and isinstance(entry.get("url"), str):
                entry["url"] = _rewrite_url(
                    entry["url"], kind="pypi", base_parts=base_parts, proxy_base=proxy_base
                )
    return json.dumps(data, separators=(",", ":")).encode("utf-8")


def _rewrite_pypi_html(body: bytes, *, base_parts: SplitResult, proxy_base: str) -> bytes:
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return body

    def sub(match: re.Match[str]) -> str:
        quote_char, raw_href = match.group(1), match.group(2)
        decoded_href = html.unescape(raw_href)
        rewritten = _rewrite_url(decoded_href, kind="pypi", base_parts=base_parts, proxy_base=proxy_base)
        return f"href={quote_char}{html.escape(rewritten, quote=True)}{quote_char}"

    return _HREF_RE.sub(sub, text).encode("utf-8")


def _error_response(status: int, code: str) -> EgressResponse:
    return EgressResponse(
        status,
        {"content-type": "application/json"},
        json.dumps({"error": code}).encode("utf-8"),
    )


class RegistryProxy:
    def __init__(self, engine: Any = None, *, upstream: Optional[Upstream] = None):
        self.engine = engine
        self.upstream = upstream or _registry_upstream

    def handle(self, kind: str, req: EgressRequest) -> EgressResponse:
        if kind not in REGISTRY_BASE_ENV:
            return _error_response(404, "unknown_registry")
        method = req.method.upper()
        if method not in ("GET", "HEAD"):
            return _error_response(405, "method_not_allowed")
        if any(h.lower() in _REFUSE_HEADERS for h in req.headers):
            return _error_response(403, "method_override")

        prefix = f"/{kind}"
        rest_raw = req.path[len(prefix):] if req.path.startswith(prefix) else req.path
        if not rest_raw:
            rest_raw = "/"
        origin_mode = rest_raw == "/_origin" or rest_raw.startswith("/_origin/")
        if origin_mode:
            rest_raw = rest_raw[len("/_origin"):] or "/"
        try:
            _decoded, encoded = canonical_registry_path(rest_raw, kind)
        except egress_rules.PathRefused as exc:
            return _error_response(403, str(exc))

        env_name = REGISTRY_BASE_ENV[kind]
        base = (os.environ.get(env_name) or "").strip()
        if not base:
            return _error_response(404, "registry_not_configured")
        try:
            enterprise_network.check_url(env_name, base)
        except enterprise_network.SettingError:
            return _error_response(503, "invalid_registry_url")

        base_parts = urlsplit(base)
        target_base = f"{base_parts.scheme}://{base_parts.netloc}" if origin_mode else base.rstrip("/")
        try:
            url = upstream_url(target_base, encoded, req.query)
        except egress_rules.PathRefused as exc:
            return _error_response(403, str(exc))

        try:
            if self.engine is not None:
                with self.engine.begin() as db:
                    auth_headers = resolve_registry_credential(kind, db)
            else:
                auth_headers = resolve_registry_credential(kind, None)
        except Exception as exc:  # noqa: BLE001
            log.warning("registry credential lookup failed for %s: %s", kind, type(exc).__name__)
            return _error_response(503, "credential_unavailable")

        forward_headers = {k.lower(): v for k, v in req.headers.items() if k.lower() in _REGISTRY_FORWARD}
        auth_value = (auth_headers or {}).get("Authorization", "")
        resp = self._send_with_redirects(method, url, forward_headers, auth_value, base_parts)

        content_type = next(
            (v.lower() for k, v in resp.headers.items() if k.lower() == "content-type"),
            "",
        )
        body = resp.body
        proxy_base = _proxy_base_url(req)
        if method == "GET" and 200 <= resp.status < 300 and body:
            if kind == "npm" and "json" in content_type:
                body = _rewrite_npm_json(body, base_parts=base_parts, proxy_base=proxy_base)
            elif kind == "pypi":
                if "json" in content_type:
                    body = _rewrite_pypi_json(body, base_parts=base_parts, proxy_base=proxy_base)
                elif "html" in content_type or b"<a " in body[:4096].lower():
                    body = _rewrite_pypi_html(body, base_parts=base_parts, proxy_base=proxy_base)

        out_headers = {k: v for k, v in resp.headers.items() if k.lower() in _PASS_RESPONSE}
        if "location" in {k.lower() for k in resp.headers}:
            loc = next(v for k, v in resp.headers.items() if k.lower() == "location")
            out_headers["location"] = _rewrite_url(
                loc, kind=kind, base_parts=base_parts, proxy_base=proxy_base
            )
        return EgressResponse(resp.status, out_headers, body)

    def _send_with_redirects(
        self,
        method: str,
        url: str,
        base_headers: Mapping[str, str],
        auth_value: str,
        upstream_parts: SplitResult,
    ) -> EgressResponse:
        upstream_origin = _origin_tuple(upstream_parts)
        cur_url = url
        cur_method = method
        for _ in range(_MAX_REDIRECTS + 1):
            send_headers = dict(base_headers)
            cur_parts = urlsplit(cur_url)
            if auth_value and _origin_tuple(cur_parts) == upstream_origin:
                send_headers["Authorization"] = auth_value
            try:
                resp = self.upstream(cur_method, cur_url, send_headers, b"")
            except Exception:  # noqa: BLE001
                return _error_response(502, "upstream_unreachable")
            if resp.status not in (301, 302, 303, 307, 308):
                return resp
            loc = next((v for k, v in resp.headers.items() if k.lower() == "location"), "")
            if not loc:
                return resp
            next_url = urljoin(cur_url, loc)
            next_parts = urlsplit(next_url)
            if next_parts.scheme.lower() not in ("https", "http"):
                return resp
            if cur_parts.scheme.lower() == "https" and next_parts.scheme.lower() != "https":
                return _error_response(502, "insecure_redirect")
            cur_url = next_url
            if resp.status == 303:
                cur_method = "GET"
        return resp
