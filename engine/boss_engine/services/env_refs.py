"""WHICH OF THIS HOST'S OWN MATERIAL A SECRET REFERENCE MAY NAME (id-sec-final2, the /v1/secret-refs lead).

A secret reference of kind `env` reads an environment variable of the process that resolves it, and
one of kind `local_runner` reads a file on its host. Both were unrestricted: a project Admin (who
holds `secret:manage` and `integration:manage` at their project) could name `env:DATABASE_URL`,
`env:BOSS_MASTER_KEY` or `local_runner:/proc/self/environ` and bind it to an integration whose base
URL they choose — a work tracker's test call then carried the value to that URL as its API key.
`work_tracker_adapters.resolve_credential` read `env:<anything>` straight from the process too.

The rule, in one place, applied where the value is READ (secret_backends.EnvBackend / FileBackend,
resolve_target_secret, work_tracker_adapters.resolve_credential) and where a reference is CREATED
(secret_refs.parse_credential_ref), so no consumer can route around it:

  * an `env` name must be on the ALLOWLIST: the provider credentials BOSS documents (KNOWN, and the
    provider families in KNOWN_PREFIXES), plus what the operator adds on the host in
    BOSS_SECRET_REF_ENV_ALLOW (comma-separated names; `PREFIX*` for a family). A person can never
    extend it: it is the API process's own environment.
  * and it is NEVER one of BOSS's own secrets, whatever the allowlist says (NEVER, NEVER_SHAPED):
    the database, the master key, BOSS's bearer keys and session/egress secrets.
  * a `local_runner` path is never under /proc, /sys, /dev or /etc, nor BOSS's identity directory
    or master-key file.
"""
from __future__ import annotations

import base64
import os
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional
from urllib.parse import urlsplit

#: Provider credentials BOSS's own integrations name as `env:` references.
KNOWN: frozenset[str] = frozenset({
    "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY", "GROQ_API_KEY", "DEEPSEEK_API_KEY",
    "GOOGLE_API_KEY", "GEMINI_API_KEY", "XAI_API_KEY", "MISTRAL_API_KEY",
    "PLANE_API_KEY", "PLANE_API_TOKEN", "JIRA_API_TOKEN", "LINEAR_API_KEY", "AZURE_DEVOPS_PAT",
    "ASANA_ACCESS_TOKEN", "BOSS_TRACKER_TOKEN",
    "BOSS_GITHUB_TOKEN", "GITHUB_TOKEN", "GH_TOKEN",
    "DIGITALOCEAN_TOKEN", "DO_API_TOKEN", "CLOUDFLARE_API_TOKEN",
})

#: Provider families: every variable under one of these is a provider credential or setting.
KNOWN_PREFIXES: tuple[str, ...] = (
    "BOSS_AWS_", "BOSS_AZURE_", "BOSS_GCP_", "BOSS_DIGITALOCEAN_", "BOSS_CLOUDFLARE_", "BOSS_OVH_",
    "BOSS_SUPABASE_", "BOSS_HETZNER_", "BOSS_LINODE_", "BOSS_VULTR_", "BOSS_SSH_",
)

#: A GitHub token the operator named for a connection (GitHub › Easy connect's
#: `env_credential_key`, e.g. BOSS_ACME_GITHUB_TOKEN): the shape names what it is.
KNOWN_SUFFIXES: tuple[str, ...] = ("_GITHUB_TOKEN",)

ALLOW_VAR = "BOSS_SECRET_REF_ENV_ALLOW"

#: BOSS's own secrets. Never resolvable through a reference, not even when allowed.
NEVER: frozenset[str] = frozenset({
    "DATABASE_URL", "BOSS_DATABASE_URL", "AC_DATABASE_URL", "POSTGRES_PASSWORD", "POSTGRES_USER",
    "PGPASSWORD", "PGPASSFILE",
    "BOSS_MASTER_KEY", "BOSS_MASTER_KEY_FILE",
    "BOSS_API_KEY", "BOSS_API_KEYS", "BOSS_CONSOLE_API_KEY", "BOSS_HOST_API_KEY", "BOSS_MCP_API_KEY",
    "BOSS_CONSOLE_PASSWORD", "BOSS_SESSION_SECRET", "BOSS_CSRF_SECRET", "BOSS_SECRET_KEY",
    "BOSS_EGRESS_KEY", "BOSS_EGRESS_SERVICE_KEY", "BOSS_SETUP_TOKEN",
    "BOSS_VAULT_TOKEN", "VAULT_TOKEN", "BOSS_INFISICAL_TOKEN", "INFISICAL_TOKEN",
    "MEILI_MASTER_KEY", "MINIO_ROOT_PASSWORD", "OPS_TOKEN", "X_OPS_TOKEN",
})
#: …and the shape of any future one.
NEVER_SHAPED = re.compile(
    r"(DATABASE_URL|^PG|^POSTGRES_|MASTER_KEY|^BOSS_(?!(AWS|AZURE|GCP|DIGITALOCEAN|CLOUDFLARE|OVH|"
    r"SUPABASE|HETZNER|LINODE|VULTR|SSH)_).*(API_KEYS?|SESSION|CSRF|EGRESS|SIGNING|JWT|IDENTITY))")

_NAME = re.compile(r"[A-Z_][A-Z0-9_]*")


def _operator_allowed() -> tuple[set[str], tuple[str, ...]]:
    raw = os.environ.get(ALLOW_VAR, "")
    names, prefixes = set(), []
    for item in (p.strip() for p in raw.split(",")):
        if not item:
            continue
        if item.endswith("*") and len(item) > 1:
            prefixes.append(item[:-1])
        else:
            names.add(item)
    return names, tuple(prefixes)


def env_name_allowed(name: str) -> bool:
    """May a secret reference name this environment variable?"""
    name = (name or "").strip()
    if not _NAME.fullmatch(name) or name == ALLOW_VAR:
        return False
    if name in NEVER or NEVER_SHAPED.search(name):
        return False
    names, prefixes = _operator_allowed()
    return (name in KNOWN or name.startswith(KNOWN_PREFIXES) or name.endswith(KNOWN_SUFFIXES)
            or name in names or name.startswith(prefixes))


def refusal(name: str) -> str:
    """The words for a refused name (never its value)."""
    return (f"A secret reference cannot name the environment variable {name!r}: only the provider "
            f"credentials BOSS knows, or ones the operator lists in {ALLOW_VAR} on the host, and "
            f"never one of BOSS's own secrets.")


_BLOCKED_DIRS = ("/proc", "/sys", "/dev", "/etc")


def _identity_paths() -> tuple[str, ...]:
    out = ["/var/lib/boss/identity"]
    for var in ("BOSS_MASTER_KEY_FILE", "BOSS_EGRESS_SERVICE_KEY_FILE"):
        value = os.environ.get(var)
        if value:
            out += [value, os.path.dirname(value) or value]
    return tuple(os.path.realpath(p) for p in out)


def file_path_allowed(path: str) -> bool:
    """May a `local_runner` (file) reference read this path? Judged on the real path, so a symlink
    into a refused place is refused too."""
    if not path:
        return False
    real = os.path.realpath(os.path.expanduser(path))
    blocked_dirs = _BLOCKED_DIRS + tuple(os.path.realpath(d) for d in _BLOCKED_DIRS)
    for blocked in blocked_dirs + _identity_paths():
        if real == blocked or real.startswith(blocked.rstrip("/") + "/"):
            return False
    base = os.path.basename(real).lower()
    return not (base == ".env" or base.endswith(".env") or base == "master.key")


# =================================================================================================
# WHERE A HOST CREDENTIAL MAY BE SENT (rc6 N-1).
#
# The allowlist above says which of the host's credentials a reference may NAME. It said nothing
# about where the value then GOES, and a project Admin chooses both the reference and the URL: a
# Plane "test connection" with api_base_url https://evil.example and credential_ref
# env:BOSS_GITHUB_TOKEN carried the org's GitHub token there (and the same for every cloud and
# model key). The rule, as a class:
#
#   A HOST CREDENTIAL is sent only to the provider it belongs to, at that provider's official
#   endpoints (PROVIDER_HOSTS), or at an endpoint the OPERATOR configured for that provider on the
#   host (OPERATOR_BASE_URLS, BOSS_CREDENTIAL_HOSTS). A host credential is
#     * the value of an allowlisted env variable that carries a secret (env: and secret: refs,
#       the trackers' own fallback, EnvBackend, Settings fields read from the same variables);
#     * an org- or workspace-scoped secret reference, an `env`/`identity_github` reference, and the
#       org's GitHub connection (App installation tokens, the org token): `register_host_credential`
#       records them as they are resolved.
#   A credential a person stored for their own project is not one: it may go to the custom base
#   URL they chose for that integration.
#
# Enforced where the value is ATTACHED to an outbound request, not at an API route: every request
# BOSS sends over http.client (urllib) and httpx (and the model SDKs on top of it) passes
# `check_outbound` (install_outbound_guard, run when boss_engine is imported), as do the
# identity fetcher's raw socket and SMTP logins. The tracker adapters also check by name before
# the request is built (`assert_may_send`), so a refusal names the variable and the host.
# =================================================================================================

@dataclass(frozen=True)
class Binding:
    """The provider a credential belongs to, and the hosts it may be sent to."""
    provider: str
    hosts: tuple[str, ...] = ()      # "api.github.com", "*.atlassian.net"


#: Each provider's official endpoints (https only). `*.x` matches any subdomain of x.
PROVIDER_HOSTS: dict[str, tuple[str, ...]] = {
    "github": ("api.github.com", "github.com", "uploads.github.com", "codeload.github.com"),
    "openai": ("api.openai.com",),
    "anthropic": ("api.anthropic.com",),
    "openrouter": ("openrouter.ai",),
    "groq": ("api.groq.com",),
    "deepseek": ("api.deepseek.com",),
    "google": ("generativelanguage.googleapis.com",),
    "xai": ("api.x.ai",),
    "mistral": ("api.mistral.ai",),
    "plane": ("api.plane.so", "app.plane.so"),
    "jira": ("*.atlassian.net", "api.atlassian.com"),
    "linear": ("api.linear.app",),
    "azure_devops": ("dev.azure.com", "*.dev.azure.com", "*.visualstudio.com"),
    "asana": ("app.asana.com",),
    "digitalocean": ("api.digitalocean.com", "inference.do-ai.run"),
    "cloudflare": ("api.cloudflare.com",),
    "aws": ("*.amazonaws.com", "*.aws.amazon.com"),
    "azure": ("management.azure.com", "login.microsoftonline.com", "*.azure.com",
              "*.windows.net", "*.azure.net"),
    "gcp": ("*.googleapis.com",),
    "ovh": ("*.ovh.com", "*.ovh.net", "*.ovhcloud.com", "eu.api.ovh.com", "ca.api.ovh.com"),
    "supabase": ("api.supabase.com", "*.supabase.co"),
    "hetzner": ("api.hetzner.cloud", "api.hetzner.com"),
    "linode": ("api.linode.com",),
    "vultr": ("api.vultr.com",),
    # A key FILE: used to sign an SSH handshake, never sent over HTTP.
    "ssh": (),
    # BOSS_TRACKER_TOKEN names no provider: it goes where the operator says (BOSS_CREDENTIAL_HOSTS).
    "tracker": (),
}

#: Host settings whose URL is the operator's own endpoint for a provider (a GitHub Enterprise,
#: a self-hosted Plane, an OpenAI-compatible gateway). The operator sets these on the host; a
#: person cannot.
OPERATOR_BASE_URLS: dict[str, tuple[str, ...]] = {
    "github": ("BOSS_GITHUB_API_URL", "BOSS_GITHUB_WEB_URL", "BOSS_GITHUB_GIT_URL", "GITHUB_API_URL"),
    "openai": ("OPENAI_BASE_URL", "OPENAI_API_BASE"),
    "anthropic": ("ANTHROPIC_BASE_URL",),
    "openrouter": ("OPENROUTER_BASE_URL",),
    "groq": ("GROQ_BASE_URL",),
    "deepseek": ("DEEPSEEK_BASE_URL",),
    "google": ("GEMINI_BASE_URL",),
    "xai": ("XAI_BASE_URL",),
    "mistral": ("MISTRAL_BASE_URL",),
    "plane": ("PLANE_BASE_URL", "PLANE_API_URL", "PLANE_PROJECT_URL"),
    "jira": ("JIRA_BASE_URL",),
    "digitalocean": ("BOSS_DO_INFERENCE_BASE_URL",),
}

#: Operator-only: `provider=host1|host2;provider2=host` adds endpoints to a provider.
HOSTS_VAR = "BOSS_CREDENTIAL_HOSTS"

_NAME_PROVIDER: dict[str, str] = {
    "OPENAI_API_KEY": "openai", "ANTHROPIC_API_KEY": "anthropic", "OPENROUTER_API_KEY": "openrouter",
    "GROQ_API_KEY": "groq", "DEEPSEEK_API_KEY": "deepseek", "GOOGLE_API_KEY": "google",
    "GEMINI_API_KEY": "google", "XAI_API_KEY": "xai", "MISTRAL_API_KEY": "mistral",
    "PLANE_API_KEY": "plane", "PLANE_API_TOKEN": "plane", "JIRA_API_TOKEN": "jira",
    "LINEAR_API_KEY": "linear", "AZURE_DEVOPS_PAT": "azure_devops",
    "ASANA_ACCESS_TOKEN": "asana", "BOSS_TRACKER_TOKEN": "tracker",
    "BOSS_GITHUB_TOKEN": "github", "GITHUB_TOKEN": "github", "GH_TOKEN": "github",
    "DIGITALOCEAN_TOKEN": "digitalocean", "DO_API_TOKEN": "digitalocean",
    "CLOUDFLARE_API_TOKEN": "cloudflare",
}
_PREFIX_PROVIDER: dict[str, str] = {
    "BOSS_AWS_": "aws", "BOSS_AZURE_": "azure", "BOSS_GCP_": "gcp",
    "BOSS_DIGITALOCEAN_": "digitalocean", "BOSS_CLOUDFLARE_": "cloudflare", "BOSS_OVH_": "ovh",
    "BOSS_SUPABASE_": "supabase", "BOSS_HETZNER_": "hetzner", "BOSS_LINODE_": "linode",
    "BOSS_VULTR_": "vultr", "BOSS_SSH_": "ssh",
}
_SUFFIX_PROVIDER: dict[str, str] = {"_GITHUB_TOKEN": "github"}
#: A secret reference's `provider` column, as people and the console spell it.
_PROVIDER_ALIASES: dict[str, str] = {
    "gemini": "google", "github_issues": "github", "digital_ocean": "digitalocean", "do": "digitalocean",
    "azure_openai": "azure", "gcp": "gcp", "google_cloud": "gcp", "amazon": "aws",
}

#: The shape of a variable that holds a secret (rather than a region, an id, a path or a URL).
_SECRET_NAME = re.compile(r"(TOKEN|SECRET|PASSWORD|PASSPHRASE|_PAT$|API_KEY|_KEY$|CREDENTIAL)")
_NOT_SECRET_NAME = re.compile(r"(_ID|_FILE|_PATH|_URL|_REGION|_HOST|_USER|_USERNAME|_NAME|_EMAIL)$")
#: Shorter values are not matched in outbound requests (a false match would refuse a real call).
MIN_MATCH_LENGTH = 12


class CredentialMisdirected(ValueError):
    """A host credential was about to be sent somewhere other than its provider. The message names
    the credential and the host, never the value."""

    def __init__(self, label: str, binding: Binding, host: str):
        self.label, self.binding, self.host = label, binding, host
        where = ", ".join(_official_and_operator_hosts(binding)) or "nowhere over HTTP"
        super().__init__(
            f"{label} belongs to {binding.provider} and is sent only to {where}; refusing to send "
            f"it to {host or 'that address'}. A custom URL needs a credential stored for that "
            f"integration (a project secret), or the operator lists the host in {HOSTS_VAR}.")


def _operator_host_overrides() -> dict[str, tuple[str, ...]]:
    out: dict[str, list[str]] = {}
    for part in (os.environ.get(HOSTS_VAR) or "").split(";"):
        provider, sep, hosts = part.partition("=")
        if not sep:
            continue
        out.setdefault(provider.strip().lower(), []).extend(
            h.strip().lower().rstrip(".") for h in hosts.split("|") if h.strip())
    return {k: tuple(v) for k, v in out.items()}


def _operator_hosts(provider: str) -> tuple[str, ...]:
    hosts = list(_operator_host_overrides().get(provider, ()))
    for var in OPERATOR_BASE_URLS.get(provider, ()):
        raw = (os.environ.get(var) or "").strip()
        host = (urlsplit(raw).hostname or "") if raw else ""
        if host:
            hosts.append(host.lower().rstrip("."))
    return tuple(hosts)


def _official_and_operator_hosts(binding: Binding) -> tuple[str, ...]:
    return tuple(dict.fromkeys(binding.hosts + _operator_hosts(binding.provider)))


def provider_binding(provider: Optional[str], extra_hosts: Iterable[str] = ()) -> Binding:
    """The binding of a provider by name (a reference's `provider`), plus hosts an org admin set."""
    key = (provider or "").strip().lower()
    key = _PROVIDER_ALIASES.get(key, key)
    extra = tuple(str(h).strip().lower().rstrip(".") for h in extra_hosts if str(h).strip())
    return Binding(key or "unknown", PROVIDER_HOSTS.get(key, ()) + extra)


def binding_for(name: str) -> Optional[Binding]:
    """The provider and endpoints an allowlisted env variable belongs to. None when the name is
    not a host credential a reference may name at all."""
    name = (name or "").strip()
    if not env_name_allowed(name):
        return None
    provider = _NAME_PROVIDER.get(name)
    if provider is None:
        provider = next((p for pre, p in _PREFIX_PROVIDER.items() if name.startswith(pre)), None)
    if provider is None:
        provider = next((p for suf, p in _SUFFIX_PROVIDER.items() if name.endswith(suf)), None)
    if provider is None:
        # Added by the operator in BOSS_SECRET_REF_ENV_ALLOW: bound to what BOSS_CREDENTIAL_HOSTS
        # lists under its own name (e.g. `MY_TOKEN=api.example.com`), else to nothing.
        return Binding(name.lower(), ())
    return Binding(provider, PROVIDER_HOSTS.get(provider, ()))


def carries_secret(name: str) -> bool:
    return bool(_SECRET_NAME.search(name)) and not _NOT_SECRET_NAME.search(name)


def _host_matches(host: str, pattern: str) -> bool:
    if pattern.startswith("*."):
        return host.endswith(pattern[1:]) and host != pattern[2:]
    return host == pattern


def destination_allowed(binding: Binding, url: str) -> bool:
    """May a credential with this binding be sent to `url`? Official hosts need https; a host the
    operator configured is taken with whatever scheme the operator gave it."""
    parts = urlsplit(url or "")
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        return False
    if any(_host_matches(host, p) for p in _operator_hosts(binding.provider)):
        return True
    if (parts.scheme or "").lower() not in ("https", "wss"):
        return False
    return any(_host_matches(host, p) for p in binding.hosts)


def assert_may_send(name: str, url: str) -> None:
    """Refuse to send the host credential in env variable `name` to `url` (by name, before the
    request is built). Names that are not host credentials are not judged here."""
    binding = binding_for(name)
    if binding is not None and not destination_allowed(binding, url):
        raise CredentialMisdirected(f"The host credential {name}", binding,
                                    (urlsplit(url or "").hostname or ""))


# ---- credentials resolved from org-level material (not in the environment) -----------------------

_REGISTRY_LIMIT = 512
_registered: "OrderedDict[str, tuple[str, Binding]]" = OrderedDict()
_registry_lock = threading.Lock()


def register_host_credential(value: Optional[str], binding: Binding, label: str) -> None:
    """Record an org-level credential as it is resolved, so `check_outbound` holds it to its
    provider wherever it is attached. Values live only in this process's memory, as the resolved
    value already does."""
    value = (value or "").strip()
    if len(value) < MIN_MATCH_LENGTH:
        return
    with _registry_lock:
        _registered[value] = (label, binding)
        _registered.move_to_end(value)
        while len(_registered) > _REGISTRY_LIMIT:
            _registered.popitem(last=False)


def _host_credentials() -> list[tuple[str, str, Binding]]:
    out: list[tuple[str, str, Binding]] = []
    for name, value in os.environ.items():
        value = (value or "").strip()
        if len(value) < MIN_MATCH_LENGTH or not carries_secret(name):
            continue
        binding = binding_for(name)
        if binding is not None:
            out.append((f"The host credential {name}", value, binding))
    with _registry_lock:
        out.extend((label, value, binding) for value, (label, binding) in _registered.items())
    return out


def _texts(url: str, headers: Any, body: Any) -> list[str]:
    hay = [url or ""]
    items: Iterable[Any] = ()
    if isinstance(headers, Mapping):
        items = headers.items()
    elif headers is not None:
        try:
            items = list(headers)
        except TypeError:
            items = ()
    for item in items:
        value = item[1] if isinstance(item, tuple) and len(item) == 2 else item
        text_value = value.decode("latin-1") if isinstance(value, (bytes, bytearray)) else str(value)
        hay.append(text_value)
        hay.extend(_basic_decoded(text_value))
    if isinstance(body, (bytes, bytearray)):
        if len(body) <= 4 * 1024 * 1024:
            text_body = bytes(body).decode("utf-8", errors="ignore")
            hay.append(text_body)
            hay.extend(_basic_decoded(text_body))
    elif isinstance(body, str):
        hay.append(body)
    return hay


_BASIC = re.compile(r"Basic\s+([A-Za-z0-9+/=]{8,})", re.IGNORECASE)


def _basic_decoded(text_value: str) -> list[str]:
    out = []
    for match in _BASIC.finditer(text_value):
        try:
            out.append(base64.b64decode(match.group(1) + "==", validate=False).decode("utf-8", "ignore"))
        except Exception:  # noqa: BLE001 — not base64: nothing to decode
            continue
    return out


def check_outbound(url: str, headers: Any = None, body: Any = None) -> None:
    """THE host-binding check: refuse when a host credential appears in this request (its URL,
    headers, or body) and the destination is not one of that credential's provider's endpoints."""
    creds = _host_credentials()
    if not creds:
        return
    hay = _texts(url, headers, body)
    for label, value, binding in creds:
        if any(value in h for h in hay) and not destination_allowed(binding, url):
            raise CredentialMisdirected(label, binding, (urlsplit(url or "").hostname or ""))


# ---- the outbound guard: every request BOSS sends over http.client and httpx ---------------------

_GUARD_MARK = "__boss_credential_guard__"


def _guard_http_client() -> None:
    import http.client

    conn_cls = http.client.HTTPConnection
    if getattr(conn_cls.send, _GUARD_MARK, False):
        return
    original_send = conn_cls.send

    def send(self, data):  # type: ignore[no-untyped-def]
        if isinstance(data, (bytes, bytearray)) and data:
            host = getattr(self, "_tunnel_host", None) or getattr(self, "host", "") or ""
            explicit = _HOST_LINE.search(bytes(data[:8192]))
            if explicit:
                named = explicit.group(1).decode("latin-1").strip()
                try:
                    import ipaddress

                    ipaddress.ip_address(host.strip("[]"))
                    host = named.rsplit(":", 1)[0] if not named.startswith("[") else named
                except ValueError:
                    pass
            scheme = "https" if isinstance(self, http.client.HTTPSConnection) else "http"
            check_outbound(f"{scheme}://{host}/", None, bytes(data))
        return original_send(self, data)

    setattr(send, _GUARD_MARK, True)
    conn_cls.send = send  # type: ignore[method-assign]


_HOST_LINE = re.compile(rb"\r\nHost:\s*([^\r\n]+)", re.IGNORECASE)


def _guard_httpx() -> None:
    try:
        import httpx
    except ImportError:  # pragma: no cover — httpx is a dependency
        return

    def _request_parts(request):  # type: ignore[no-untyped-def]
        try:
            body = request.content
        except Exception:  # noqa: BLE001 — a stream not yet read: headers and URL still checked
            body = None
        return str(request.url), request.headers.raw, body

    sync_cls = httpx.HTTPTransport
    if not getattr(sync_cls.handle_request, _GUARD_MARK, False):
        original = sync_cls.handle_request

        def handle_request(self, request):  # type: ignore[no-untyped-def]
            check_outbound(*_request_parts(request))
            return original(self, request)

        setattr(handle_request, _GUARD_MARK, True)
        sync_cls.handle_request = handle_request  # type: ignore[method-assign]

    async_cls = httpx.AsyncHTTPTransport
    if not getattr(async_cls.handle_async_request, _GUARD_MARK, False):
        original_async = async_cls.handle_async_request

        async def handle_async_request(self, request):  # type: ignore[no-untyped-def]
            check_outbound(*_request_parts(request))
            return await original_async(self, request)

        setattr(handle_async_request, _GUARD_MARK, True)
        async_cls.handle_async_request = handle_async_request  # type: ignore[method-assign]


def install_outbound_guard() -> None:
    """Hold every request this process sends to `check_outbound`. Idempotent."""
    _guard_http_client()
    _guard_httpx()


def outbound_guard_installed() -> bool:
    import http.client

    import httpx

    return all(getattr(f, _GUARD_MARK, False) for f in (
        http.client.HTTPConnection.send, httpx.HTTPTransport.handle_request,
        httpx.AsyncHTTPTransport.handle_async_request))


# ---- who may NAME a host credential (rc6 N-1, the creation side) ----------------------------------

class HostCredentialAuthorityRequired(ValueError):
    """Naming one of the host's credentials (an `env:` reference) or making a reference every
    project of the org or workspace can use is an org-level act: integration:manage at the org."""
    status_code = 403
    permission = "integration:manage"


def may_name_host_credentials(connection: Any, project_id: str) -> bool:
    """Inside an enforced request: does the caller hold integration:manage at the ORG the project
    belongs to? Outside a request (the host shell, seeds, migrations) the in-process caller is
    trusted, as everywhere else (github_setup.may_change_org_credentials)."""
    from sqlalchemy import text

    from ..identity import authz, lineage
    from ..identity.permissions_gen import P
    from ..identity.scope import ResolvedScope

    if not lineage.request_enforced():
        return True
    ctx = lineage.current_context()
    org_id = connection.execute(text(
        "SELECT w.org_id FROM projects p JOIN workspaces w ON w.id = p.workspace_id "
        "WHERE p.id = :p"), {"p": project_id}).scalar()
    if ctx is None or not org_id or ctx.principal.org_id != org_id:
        return False
    return authz.check(ctx, P.INTEGRATION_MANAGE, ResolvedScope.org_level(org_id), db=connection).allowed


def require_host_credential_authority(connection: Any, project_id: str, what: str) -> None:
    if not may_name_host_credentials(connection, project_id):
        raise HostCredentialAuthorityRequired(
            f"{what} is the host's or the whole organisation's credential, so it needs "
            f"integration:manage for the organisation (an Owner or an org Admin). A project's own "
            f"integration uses a secret stored for that project.")
