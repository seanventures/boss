#!/bin/sh
# The enterprise network for one image build step (docs/community/ENTERPRISE_NETWORK.md).
#
# Every RUN in Dockerfile.community that reaches the network starts with
#
#     . /usr/local/libexec/boss-build-network && ...
#
# with this file bind-mounted there (never COPYed: it is not part of any image), the extra CA as
# the BuildKit secret `boss_build_ca`, and a tmpfs at /run/boss-build-network for the combined
# trust bundle, so nothing it writes is kept in a layer. Run as a command (`sh build-network.sh
# CMD ...`) it does the same and then execs CMD.
#
# It applies, for that step only:
#   * the registry mirrors: BOSS_NPM_REGISTRY (npm, pnpm), BOSS_PIP_INDEX_URL (pip) and
#     BOSS_PLAYWRIGHT_DOWNLOAD_HOST (the browser download), each https with NO user name,
#     password, query or fragment. A build argument is recorded in `docker history`, so a URL
#     carrying a credential is refused HERE, at build time, as well as by the installer;
#   * the outbound proxy: BOSS_UPSTREAM_PROXY / BOSS_UPSTREAM_NO_PROXY, as HTTP(S)_PROXY and
#     NO_PROXY (exported only when set, so an empty value never reaches a tool);
#   * the extra CA (the company's root, for a TLS-intercepting gateway): ADDED to the system
#     trust, never replacing it, for OpenSSL/Python/urllib (SSL_CERT_FILE), requests and pip
#     (REQUESTS_CA_BUNDLE, PIP_CERT), curl (CURL_CA_BUNDLE), git (GIT_SSL_CAINFO), apt (its own
#     CAInfo setting) and Node, npm, pnpm and Playwright (NODE_EXTRA_CA_CERTS). Certificate and
#     host name verification stay on. There is no switch that turns TLS verification off.
#
# Errors name the setting, never its value. POSIX sh, no `set -e`: it is sourced into the RUN's
# own shell, whose options it must not change.

_bbn_fail() {
  echo "boss build: $*" >&2
  echo "boss build: see docs/community/ENTERPRISE_NETWORK.md" >&2
  exit 64
}

# _bbn_url NAME VALUE SCHEMES: refuse anything but a plain URL of one of SCHEMES ("https" or
# "http https").
_bbn_url() {
  [ -n "$2" ] || return 0
  _bbn_ok=0
  for _bbn_s in $3; do
    case "$2" in "$_bbn_s"://?*) _bbn_ok=1 ;; esac
  done
  [ "$_bbn_ok" = 1 ] || _bbn_fail "$1 must start with $(echo "$3" | sed 's/\([a-z]*\)/\1:\/\//g; s/ / or /g')"
  _bbn_rest="${2#*://}"
  _bbn_auth="${_bbn_rest%%/*}"
  case "$_bbn_auth" in
    *@*) _bbn_fail "$1 must not contain a user name or password: a build argument is kept in the image history. Remove it from .env" ;;
    ""|:*) _bbn_fail "$1 has no host name" ;;
  esac
  case "$2" in
    *\?*|*\#*) _bbn_fail "$1 must not have a query string or fragment" ;;
    *" "*|*"	"*) _bbn_fail "$1 must be one URL with no spaces" ;;
  esac
  return 0
}

_bbn_url BOSS_NPM_REGISTRY "${BOSS_NPM_REGISTRY:-}" https
_bbn_url BOSS_PIP_INDEX_URL "${BOSS_PIP_INDEX_URL:-}" https
_bbn_url BOSS_PLAYWRIGHT_DOWNLOAD_HOST "${BOSS_PLAYWRIGHT_DOWNLOAD_HOST:-}" https
_bbn_url BOSS_UPSTREAM_PROXY "${BOSS_UPSTREAM_PROXY:-}" "http https"

if [ -n "${BOSS_NPM_REGISTRY:-}" ]; then
  export npm_config_registry="$BOSS_NPM_REGISTRY" npm_config_strict_ssl=true
fi
if [ -n "${BOSS_PIP_INDEX_URL:-}" ]; then
  export PIP_INDEX_URL="$BOSS_PIP_INDEX_URL"
fi
if [ -n "${BOSS_PLAYWRIGHT_DOWNLOAD_HOST:-}" ]; then
  export PLAYWRIGHT_DOWNLOAD_HOST="${BOSS_PLAYWRIGHT_DOWNLOAD_HOST%/}"
fi
if [ -n "${BOSS_UPSTREAM_PROXY:-}" ]; then
  export HTTPS_PROXY="$BOSS_UPSTREAM_PROXY" https_proxy="$BOSS_UPSTREAM_PROXY" \
    HTTP_PROXY="$BOSS_UPSTREAM_PROXY" http_proxy="$BOSS_UPSTREAM_PROXY"
  if [ -n "${BOSS_UPSTREAM_NO_PROXY:-}" ]; then
    export NO_PROXY="$BOSS_UPSTREAM_NO_PROXY" no_proxy="$BOSS_UPSTREAM_NO_PROXY"
  fi
fi

_bbn_ca="${BOSS_BUILD_CA_SECRET:-/run/secrets/boss_build_ca}"
if [ -f "$_bbn_ca" ] && [ -s "$_bbn_ca" ]; then
  if grep -q "PRIVATE KEY" "$_bbn_ca"; then
    _bbn_fail "BOSS_EXTRA_CA_FILE contains a private key; give only the CA certificate(s)"
  fi
  grep -q "BEGIN CERTIFICATE" "$_bbn_ca" || _bbn_fail "BOSS_EXTRA_CA_FILE holds no PEM certificate"
  # Node reads its extra roots from a file and keeps its own: the CA file alone is right.
  export NODE_EXTRA_CA_CERTS="$_bbn_ca"
  _bbn_system=""
  for _bbn_f in /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt /etc/ssl/cert.pem; do
    if [ -s "$_bbn_f" ]; then _bbn_system="$_bbn_f"; break; fi
  done
  # Everything else REPLACES its trust with the file it is given: give it system + extra, never
  # the extra CA alone. An image without a system bundle (node slim) keeps its tools' defaults.
  if [ -n "$_bbn_system" ]; then
    _bbn_dir="${BOSS_BUILD_NETWORK_DIR:-/run/boss-build-network}"
    if ! { mkdir -p "$_bbn_dir" 2>/dev/null && [ -w "$_bbn_dir" ]; }; then
      _bbn_dir="$(mktemp -d)" || _bbn_fail "cannot write the combined CA bundle"
    fi
    { cat "$_bbn_system"; echo; cat "$_bbn_ca"; } > "$_bbn_dir/ca-bundle.pem" ||
      _bbn_fail "cannot write the combined CA bundle"
    printf 'Acquire::https::CAInfo "%s";\n' "$_bbn_dir/ca-bundle.pem" > "$_bbn_dir/apt.conf"
    export SSL_CERT_FILE="$_bbn_dir/ca-bundle.pem" REQUESTS_CA_BUNDLE="$_bbn_dir/ca-bundle.pem" \
      CURL_CA_BUNDLE="$_bbn_dir/ca-bundle.pem" PIP_CERT="$_bbn_dir/ca-bundle.pem" \
      GIT_SSL_CAINFO="$_bbn_dir/ca-bundle.pem" APT_CONFIG="$_bbn_dir/apt.conf"
  fi
fi
unset _bbn_ok _bbn_s _bbn_rest _bbn_auth _bbn_ca _bbn_system _bbn_f _bbn_dir

if [ "$#" -gt 0 ]; then
  exec "$@"
fi
