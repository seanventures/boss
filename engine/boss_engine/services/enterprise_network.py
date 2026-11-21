"""The enterprise network block: one place that says what each setting is and what it may hold.

An install inside a company network meets four things a home network does not: package
registries that must come from an internal mirror (npm, PyPI, the Go module proxy, Maven), an
outbound proxy, a TLS-intercepting gateway whose certificate only the company's own CA vouches
for, and a sandbox fence (boss-egress) that must be told about the internal hosts. Every one of
these is configured HERE, in .env, under one heading (docs/community/ENTERPRISE_NETWORK.md), and
every consumer reads this module instead of re-parsing the values:

* install.sh runs this file BY PATH (`python3 .../enterprise_network.py check FILE`) before it
  replaces .env, so a bad value never reaches a build or a run. It needs nothing but the
  standard library for that reason;
* the image build wrapper (scripts/install/build-network.sh) applies the same URL rule at
  BUILD time, so a credential in a registry URL never reaches `docker history`;
* agent_env gives sandboxed runs the package settings (`package_environment`);
* boss-egress allows the configured registry hosts and ports (`registry_endpoints`);
* toolchains writes the Maven mirror and the Go proxy (`go_environment`, `maven_mirror`).

THE RULES. A URL setting is https (a proxy may be http: that is how proxies are addressed), with
a host, and with NO user name, password, query or fragment: a token in a URL lands in image
history, process listings and logs. Registry credentials are not supported through these
settings at all. Errors name the setting, never the value. Empty means "today's behaviour".
"""
from __future__ import annotations

import base64
import ipaddress
import os
import re
import ssl
import sys
from typing import Iterable, Mapping, Optional
from urllib.parse import urlsplit

#: setting -> what it is (used in messages; never the value).
URL_SETTINGS: dict[str, str] = {
    "BOSS_NPM_REGISTRY": "the npm registry (also pnpm and yarn v1)",
    "BOSS_PIP_INDEX_URL": "the Python package index",
    "BOSS_GOPROXY": "the Go module proxy",
    "BOSS_MAVEN_MIRROR": "the Maven repository mirror",
    "BOSS_PLAYWRIGHT_DOWNLOAD_HOST": "the Playwright browser download host",
}
PROXY_SETTINGS: dict[str, str] = {
    "BOSS_UPSTREAM_PROXY": "the outbound HTTP(S) proxy BOSS's containers and boss-egress use",
}
#: Comma-separated lists.
LIST_SETTINGS: dict[str, str] = {
    "BOSS_UPSTREAM_NO_PROXY": "hosts reached without the upstream proxy",
    "BOSS_EGRESS_CONNECT_ALLOW": "extra hosts the sandbox fence allows (host, *.suffix, host:port)",
    "BOSS_EGRESS_PRIVATE_ALLOW": "internal hosts the fence may reach on a private address",
    "BOSS_GONOSUMDB": "Go module path patterns not checked against the checksum database",
}
OTHER_SETTINGS: dict[str, str] = {
    "BOSS_EXTRA_CA_FILE": "a PEM file of extra CA certificates (the company's root)",
    "BOSS_EXTRA_CA_PEM_B64": "the same certificates, as the containers receive them",
    "BOSS_GOSUMDB": "the Go checksum database",
    "BOSS_EGRESS_CONNECT_DEFAULTS": "on (default) or off: keep the fence's built-in host list",
}
#: Every setting of the block, in the order .env.example and the installer write them.
SETTINGS: tuple[str, ...] = (
    "BOSS_EXTRA_CA_FILE", "BOSS_EXTRA_CA_PEM_B64",
    "BOSS_NPM_REGISTRY", "BOSS_PIP_INDEX_URL", "BOSS_GOPROXY", "BOSS_GOSUMDB", "BOSS_GONOSUMDB",
    "BOSS_MAVEN_MIRROR", "BOSS_PLAYWRIGHT_DOWNLOAD_HOST",
    "BOSS_UPSTREAM_PROXY", "BOSS_UPSTREAM_NO_PROXY",
    "BOSS_EGRESS_CONNECT_ALLOW", "BOSS_EGRESS_CONNECT_DEFAULTS", "BOSS_EGRESS_PRIVATE_ALLOW",
)
#: Where every BOSS container (and so every sandboxed run) always reaches without a proxy.
BOSS_NO_PROXY: tuple[str, ...] = (
    "localhost", "127.0.0.1", "::1", "api", "egress", "worker", "runner", "db", "model-gateway",
    "kg-mcp", "mcp-tools", "infisical", "build-services", "web", "registry", "prometheus",
    "grafana", "mailpit", "host.docker.internal",
)
_HOST_RE = re.compile(r"^(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*$")
_GO_PATTERN_RE = re.compile(r"^[A-Za-z0-9._~*/-]+$")
_PEM_CERT = re.compile(rb"-----BEGIN CERTIFICATE-----\s+[A-Za-z0-9+/=\s]+?-----END CERTIFICATE-----")


class SettingError(ValueError):
    """A setting holds a value BOSS refuses. The message names the setting, never the value."""


def _clean(value: Optional[str]) -> str:
    return (value or "").strip()


def check_url(name: str, value: str, *, allow_http: bool = False) -> None:
    """Raise SettingError unless `value` is a URL BOSS may write into a build or a run."""
    what = URL_SETTINGS.get(name) or PROXY_SETTINGS.get(name) or name
    if any(ch.isspace() or ord(ch) < 32 for ch in value):
        raise SettingError(f"{name} ({what}) must be one URL with no spaces or line breaks")
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError as exc:
        raise SettingError(f"{name} ({what}) is not a valid URL") from exc
    schemes = ("https", "http") if allow_http else ("https",)
    if parts.scheme not in schemes:
        raise SettingError(f"{name} ({what}) must start with {' or '.join(s + '://' for s in schemes)}")
    if not parts.hostname:
        raise SettingError(f"{name} ({what}) has no host name")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise SettingError(
            f"{name} ({what}) must not contain a user name or password: a credential in a URL "
            "ends up in image history and process listings. See docs/community/ENTERPRISE_NETWORK.md "
            "(registry credentials; a local authenticating proxy for a proxy that needs a login)")
    if parts.query or parts.fragment:
        raise SettingError(f"{name} ({what}) must not have a query string or fragment (a token often hides there)")
    if port is not None and not 0 < port < 65536:
        raise SettingError(f"{name} ({what}) has an invalid port")


def _split(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def parse_host_port(entry: str, *, default_port: int = 443) -> tuple[str, int]:
    """`host` or `host:port` (lower-cased). Raises ValueError."""
    host, sep, port = entry.strip().lower().rpartition(":")
    if not sep:
        host, port = entry.strip().lower(), ""
    if port:
        if not port.isdigit() or not 0 < int(port) < 65536:
            raise ValueError("bad port")
        return host, int(port)
    return host, default_port


def _is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


def _check_allow_entry(name: str, entry: str, *, private: bool) -> None:
    try:
        host, _port = parse_host_port(entry)
    except ValueError as exc:
        raise SettingError(f"{name}: an entry has an invalid port") from exc
    bare = host[2:] if host.startswith("*.") and not private else host
    if private and ("*" in host or "." not in host):
        raise SettingError(
            f"{name}: each entry must be one exact host name with a dot (no wildcards, no BOSS "
            "service names), optionally :port")
    if _is_ip(bare):
        raise SettingError(f"{name}: use the host's DNS name, not an IP address")
    if not _HOST_RE.match(bare):
        raise SettingError(f"{name}: an entry is not a host name (host, *.suffix or host:port)")


def check_ca_file(path: str) -> None:
    """The file BOSS_EXTRA_CA_FILE names holds one or more PEM certificates and nothing secret."""
    if not os.path.isabs(path):
        raise SettingError("BOSS_EXTRA_CA_FILE must be an absolute path")
    try:
        with open(path, "rb") as fh:
            data = fh.read(4 * 1024 * 1024 + 1)
    except OSError as exc:
        raise SettingError(f"BOSS_EXTRA_CA_FILE cannot be read ({exc.strerror or 'unreadable'})") from exc
    _check_ca_bytes(data, "BOSS_EXTRA_CA_FILE")


def _check_ca_bytes(data: bytes, name: str) -> None:
    if len(data) > 4 * 1024 * 1024:
        raise SettingError(f"{name} is larger than 4 MB; give only the CA certificates")
    if b"PRIVATE KEY" in data:
        raise SettingError(
            f"{name} contains a PRIVATE KEY. Give only the CA's public certificate(s); never a key")
    if not _PEM_CERT.search(data):
        raise SettingError(f"{name} holds no PEM certificate (-----BEGIN CERTIFICATE-----)")
    try:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.load_verify_locations(cadata=data.decode("ascii", "replace"))
    except (ssl.SSLError, ValueError) as exc:
        raise SettingError(f"{name} holds a certificate that does not parse") from exc


def problems(env: Mapping[str, str]) -> list[str]:
    """Every refused value in `env`, as messages that name the setting and never the value."""
    out: list[str] = []
    for name in URL_SETTINGS:
        value = _clean(env.get(name))
        if value:
            try:
                check_url(name, value)
            except SettingError as exc:
                out.append(str(exc))
    for name in PROXY_SETTINGS:
        value = _clean(env.get(name))
        if value:
            try:
                check_url(name, value, allow_http=True)
            except SettingError as exc:
                out.append(str(exc))
    for name, private in (("BOSS_EGRESS_CONNECT_ALLOW", False), ("BOSS_EGRESS_PRIVATE_ALLOW", True)):
        for entry in _split(_clean(env.get(name))):
            try:
                _check_allow_entry(name, entry, private=private)
            except SettingError as exc:
                out.append(str(exc))
                break
    no_proxy = _clean(env.get("BOSS_UPSTREAM_NO_PROXY"))
    if no_proxy and any(ch.isspace() or ch in "@/?#" for ch in no_proxy.replace(", ", ",")):
        out.append("BOSS_UPSTREAM_NO_PROXY must be a comma-separated list of hosts, .domains or addresses")
    for pattern in _split(_clean(env.get("BOSS_GONOSUMDB"))):
        if not _GO_PATTERN_RE.match(pattern):
            out.append("BOSS_GONOSUMDB must be a comma-separated list of module path patterns")
            break
    gosumdb = _clean(env.get("BOSS_GOSUMDB"))
    if gosumdb:
        words = gosumdb.split()
        if words[0].lower() == "off" or len(words) > 2 or not re.match(r"^[A-Za-z0-9.+/-]+$", words[0]):
            out.append("BOSS_GOSUMDB must name a checksum database (`sum.golang.org`, optionally "
                       "followed by its https mirror URL); it cannot turn checking off")
        elif len(words) == 2:
            try:
                check_url("BOSS_GOSUMDB", words[1])
            except SettingError as exc:
                out.append(str(exc))
    defaults = _clean(env.get("BOSS_EGRESS_CONNECT_DEFAULTS")).lower()
    if defaults not in ("", "on", "off", "1", "0", "true", "false", "yes", "no"):
        out.append("BOSS_EGRESS_CONNECT_DEFAULTS must be on or off")
    ca_b64 = _clean(env.get("BOSS_EXTRA_CA_PEM_B64"))
    if ca_b64:
        try:
            _check_ca_bytes(base64.b64decode(ca_b64, validate=True), "BOSS_EXTRA_CA_PEM_B64")
        except (SettingError, ValueError) as exc:
            out.append(str(exc) if isinstance(exc, SettingError)
                       else "BOSS_EXTRA_CA_PEM_B64 is not base64 (the installer writes it from BOSS_EXTRA_CA_FILE)")
    ca_path = _clean(env.get("BOSS_EXTRA_CA_FILE"))
    if ca_path:
        try:
            check_ca_file(ca_path)
        except SettingError as exc:
            out.append(str(exc))
    return out


def require_valid(env: Mapping[str, str], names: Iterable[str] = ()) -> None:
    """Raise SettingError for the first refused value (all of them when `names` is empty)."""
    found = problems({k: v for k, v in env.items() if not names or k in set(names)})
    if found:
        raise SettingError(found[0])


# ---- what the settings turn into -------------------------------------------------------------

def package_environment(env: Mapping[str, str]) -> dict[str, str]:
    """The package-manager settings a sandboxed run (or a code agent) gets. Fails closed: an
    invalid value raises, because silently using the public registry would breach the policy the
    operator set. The installer and `boss doctor` catch it long before a run does."""
    require_valid(env, ("BOSS_NPM_REGISTRY", "BOSS_PIP_INDEX_URL", "BOSS_PLAYWRIGHT_DOWNLOAD_HOST"))
    out: dict[str, str] = {}
    npm = _clean(env.get("BOSS_NPM_REGISTRY"))
    if npm:
        out.update({"npm_config_registry": npm, "npm_config_strict_ssl": "true"})
    pip = _clean(env.get("BOSS_PIP_INDEX_URL"))
    if pip:
        out["PIP_INDEX_URL"] = pip
    playwright = _clean(env.get("BOSS_PLAYWRIGHT_DOWNLOAD_HOST"))
    if playwright:
        out["PLAYWRIGHT_DOWNLOAD_HOST"] = playwright.rstrip("/")
    return out


#: Every name `package_environment` can set.
PACKAGE_ENV_KEYS: tuple[str, ...] = (
    "npm_config_registry", "npm_config_strict_ssl", "PIP_INDEX_URL", "PLAYWRIGHT_DOWNLOAD_HOST")
#: The trust variables the runtime CA wrapper (scripts/install/with-extra-ca.sh) exports, so
#: every TLS client in a BOSS container trusts the extra CA: OpenSSL, Python ssl/httpx, Go and
#: Rust (SSL_CERT_FILE), requests, curl, pip, git, and Node (NODE_EXTRA_CA_CERTS).
CA_ENV_KEYS: tuple[str, ...] = (
    "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "PIP_CERT", "GIT_SSL_CAINFO",
    "NODE_EXTRA_CA_CERTS")
#: The outbound proxy, as the compose file passes it to BOSS's containers. Never a credential
#: (check_url refuses one), so an unsandboxed run may see it; a sandboxed run's proxy is egress.
PROXY_ENV_KEYS: tuple[str, ...] = (
    "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "NO_PROXY", "no_proxy")
#: Names a package environment must replace, whatever spelling the inherited env used.
PACKAGE_OVERRIDES: dict[str, tuple[str, ...]] = {
    "npm_config_registry": ("NPM_CONFIG_REGISTRY",),
    "npm_config_strict_ssl": ("NPM_CONFIG_STRICT_SSL",),
    "PIP_INDEX_URL": ("pip_index_url", "PIP_EXTRA_INDEX_URL", "PIP_TRUSTED_HOST"),
}


def go_environment(env: Mapping[str, str], *, default_proxy: str, default_sumdb: str) -> dict[str, str]:
    """GOPROXY / GOSUMDB / GONOSUMDB for a sandboxed run: the operator's mirror or the defaults."""
    require_valid(env, ("BOSS_GOPROXY", "BOSS_GOSUMDB", "BOSS_GONOSUMDB"))
    out = {"GOPROXY": _clean(env.get("BOSS_GOPROXY")) or default_proxy,
           "GOSUMDB": _clean(env.get("BOSS_GOSUMDB")) or default_sumdb}
    nosum = _clean(env.get("BOSS_GONOSUMDB"))
    if nosum:
        out["GONOSUMDB"] = ",".join(_split(nosum))
    return out


def maven_mirror(env: Mapping[str, str]) -> str:
    require_valid(env, ("BOSS_MAVEN_MIRROR",))
    return _clean(env.get("BOSS_MAVEN_MIRROR"))


def registry_endpoints(env: Mapping[str, str]) -> list[tuple[str, int]]:
    """(host, port) of every configured registry or mirror: the fence allows these without the
    operator listing them again. Invalid values contribute nothing (the fence fails closed)."""
    out: list[tuple[str, int]] = []
    candidates = [_clean(env.get(n)) for n in URL_SETTINGS]
    sumdb = _clean(env.get("BOSS_GOSUMDB")).split()
    if len(sumdb) == 2:
        candidates.append(sumdb[1])
    elif len(sumdb) == 1 and "." in sumdb[0] and sumdb[0] != "sum.golang.org":
        candidates.append("https://" + sumdb[0].split("+")[0])
    for value in candidates:
        if not value:
            continue
        try:
            check_url("registry", value)
            parts = urlsplit(value)
            out.append((parts.hostname.lower(), parts.port or 443))  # type: ignore[union-attr]
        except SettingError:
            continue
    return sorted(set(out))


def allow_entries(env: Mapping[str, str], name: str) -> list[tuple[str, Optional[int]]]:
    """The valid entries of an allow list as (host or *.suffix, port or None for 443)."""
    out: list[tuple[str, Optional[int]]] = []
    for entry in _split(_clean(env.get(name))):
        try:
            _check_allow_entry(name, entry, private=name == "BOSS_EGRESS_PRIVATE_ALLOW")
        except SettingError:
            continue
        host, port = parse_host_port(entry, default_port=0)
        out.append((host, port or None))
    return out


def upstream_proxy(env: Mapping[str, str]) -> Optional[tuple[str, int]]:
    value = _clean(env.get("BOSS_UPSTREAM_PROXY"))
    if not value:
        return None
    check_url("BOSS_UPSTREAM_PROXY", value, allow_http=True)
    parts = urlsplit(value)
    return parts.hostname.lower(), parts.port or (443 if parts.scheme == "https" else 80)  # type: ignore[union-attr]


def bypasses_proxy(host: str, env: Mapping[str, str]) -> bool:
    """True when `host` is reached directly: BOSS's own services or BOSS_UPSTREAM_NO_PROXY."""
    host = host.lower().rstrip(".")
    for item in list(BOSS_NO_PROXY) + [i.lower() for i in _split(_clean(env.get("BOSS_UPSTREAM_NO_PROXY")))]:
        if item == "*":
            return True
        suffix = item.lstrip("*")
        if host == item or (suffix.startswith(".") and (host.endswith(suffix) or host == suffix[1:])):
            return True
        try:
            if ipaddress.ip_address(host) in ipaddress.ip_network(item, strict=False):
                return True
        except ValueError:
            pass
    return False


# ---- the installer's and doctor's entry point --------------------------------------------------

def read_env_file(path: str) -> dict[str, str]:
    """KEY=VALUE lines, literally (never sourced; a value may hold shell metacharacters)."""
    out: dict[str, str] = {}
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.rstrip("\n")
            if not line or line.lstrip().startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if key.startswith("export "):
                key = key[7:].strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            out.setdefault(key, value)
    return out


def set_env_line(path: str, key: str, value: str) -> None:
    """Replace every `key=` line of an env file (or append one), keeping every other line."""
    lines: list[str] = []
    found = False
    with open(path, encoding="utf-8", errors="surrogateescape") as fh:
        for line in fh:
            if line.split("=", 1)[0].strip() == key and "=" in line:
                if not found:
                    lines.append(f"{key}={value}\n")
                found = True
                continue
            lines.append(line if line.endswith("\n") else line + "\n")
    if not found:
        lines.append(f"{key}={value}\n")
    tmp = f"{path}.enterprise.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8", errors="surrogateescape") as fh:
        fh.writelines(lines)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def finalize(path: str) -> list[str]:
    """The installer's last step on a new .env, before it replaces the live one: refuse any
    invalid value, take over the earlier name BOSS_BUILD_CA_FILE, and derive the containers' copy
    of the CA (BOSS_EXTRA_CA_PEM_B64) from BOSS_EXTRA_CA_FILE — or clear it when none is set."""
    env = read_env_file(path)
    if not _clean(env.get("BOSS_EXTRA_CA_FILE")) and _clean(env.get("BOSS_BUILD_CA_FILE")):
        set_env_line(path, "BOSS_EXTRA_CA_FILE", _clean(env["BOSS_BUILD_CA_FILE"]))
        env = read_env_file(path)
    env.pop("BOSS_EXTRA_CA_PEM_B64", None)
    found = problems(env)
    if found:
        return found
    ca_path = _clean(env.get("BOSS_EXTRA_CA_FILE"))
    encoded = ""
    if ca_path:
        with open(ca_path, "rb") as fh:
            encoded = base64.b64encode(fh.read()).decode("ascii")
    set_env_line(path, "BOSS_EXTRA_CA_PEM_B64", encoded)
    return []


def _main(argv: list[str]) -> int:
    usage = ("usage: enterprise_network.py check ENV_FILE | finalize ENV_FILE | "
             "check-ca PEM_FILE | summary ENV_FILE")
    if len(argv) != 2 or argv[0] not in ("check", "finalize", "check-ca", "summary"):
        print(usage, file=sys.stderr)
        return 2
    command, path = argv
    if command == "check-ca":
        try:
            check_ca_file(path)
        except SettingError as exc:
            print(exc, file=sys.stderr)
            return 1
        return 0
    if command == "finalize":
        try:
            found = finalize(path)
        except OSError as exc:
            print(f"cannot update {path}: {exc.strerror}", file=sys.stderr)
            return 2
        for message in found:
            print(message, file=sys.stderr)
        return 1 if found else 0
    try:
        env = read_env_file(path)
    except OSError as exc:
        print(f"cannot read {path}: {exc.strerror}", file=sys.stderr)
        return 2
    found = problems(env)
    if command == "check":
        for message in found:
            print(message, file=sys.stderr)
        return 1 if found else 0
    # summary: one line per configured setting, names and hosts only (never a path's contents).
    if found:
        for message in found:
            print("bad " + message)
    configured = [n for n in SETTINGS if _clean(env.get(n)) and n != "BOSS_EXTRA_CA_PEM_B64"
                  and not (n == "BOSS_EGRESS_CONNECT_DEFAULTS"
                           and _clean(env.get(n)).lower() in ("on", "1", "true", "yes"))]
    if not configured:
        print("ok enterprise network: not configured (public registries, no proxy, no extra CA)")
        return 1 if found else 0
    hosts = ", ".join(f"{h}:{p}" for h, p in registry_endpoints(env)) or "none"
    print(f"ok enterprise network: {', '.join(configured)} set; registry hosts the fence allows: {hosts}")
    if _clean(env.get("BOSS_EXTRA_CA_FILE")) and not _clean(env.get("BOSS_EXTRA_CA_PEM_B64")):
        print("bad BOSS_EXTRA_CA_FILE is set but the containers have no copy of it: run `boss upgrade`")
    return 1 if found else 0


if __name__ == "__main__":  # pragma: no cover - exercised through install.sh and its tests
    sys.exit(_main(sys.argv[1:]))
