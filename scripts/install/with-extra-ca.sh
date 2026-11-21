#!/bin/sh
# Start a BOSS service inside the install's enterprise network (docs/community/ENTERPRISE_NETWORK.md).
#
# Every BOSS image built from python-runtime starts through this (its ENTRYPOINT, and the compose
# entrypoints that override it), so the service and every process it starts — the coding CLIs, a
# sandboxed build's npm, pip, go, mvn and git, boss-egress's upstream calls, the model SDKs —
# trust the same certificates and use the same outbound proxy:
#
#   * the company CA, a FILE mounted read-only at /etc/boss/extra-ca.pem (the installer copies
#     BOSS_EXTRA_CA_FILE to <install>/.boss/extra-ca.pem, which compose mounts; empty = none).
#     Never an environment value: Linux caps one environment string at 128 KiB, and a bundle
#     past that stopped every role from starting. ADDED to the system trust in one combined
#     bundle, never replacing it,
#     exported as SSL_CERT_FILE (OpenSSL, Python ssl and httpx, Go, Rust), REQUESTS_CA_BUNDLE,
#     CURL_CA_BUNDLE, PIP_CERT, GIT_SSL_CAINFO, and NODE_EXTRA_CA_CERTS (Node keeps its own roots
#     and adds these). With a JDK in the image, a Java trust store with the CA added
#     (BOSS_JAVA_TRUST_STORE; toolchains.write_proxy_config points Maven and Gradle at it).
#     Files are world-readable: a sandboxed run's uid reads them too.
#   * BOSS_UPSTREAM_PROXY: HTTPS_PROXY; BOSS_UPSTREAM_HTTP_PROXY, only when the operator sets
#     it: HTTP_PROXY (plain http to previews, app sidecars or a LAN model server must not go to
#     a company proxy by default); BOSS_UPSTREAM_NO_PROXY: NO_PROXY, with BOSS's own services
#     always in it (enterprise_network.BOSS_NO_PROXY; a test holds the two lists equal).
#
# Unset, it changes nothing and execs the service. A bad CA value is reported loudly and skipped:
# the console must still start so an operator can see and fix it (`boss doctor` names it).

_wec_warn() { echo "boss: $*" >&2; }

_wec_src="${BOSS_EXTRA_CA_PATH:-/etc/boss/extra-ca.pem}"
if [ -f "$_wec_src" ] && [ -s "$_wec_src" ]; then
  _wec_dir=/var/lib/boss/trust
  if ! { [ -d "$_wec_dir" ] && [ -w "$_wec_dir" ]; }; then
    _wec_dir="${TMPDIR:-/tmp}/boss-trust-$(id -u)"
    mkdir -p "$_wec_dir" && chmod 755 "$_wec_dir"
  fi
  _wec_extra="$_wec_dir/extra-ca.pem"
  if [ "$(wc -c < "$_wec_src")" -le 512000 ] && cp "$_wec_src" "$_wec_extra.tmp" 2>/dev/null &&
     grep -q "BEGIN CERTIFICATE" "$_wec_extra.tmp" && ! grep -q "PRIVATE KEY" "$_wec_extra.tmp"; then
    _wec_system=""
    for _wec_f in /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt /etc/ssl/cert.pem; do
      if [ -s "$_wec_f" ]; then _wec_system="$_wec_f"; break; fi
    done
    { [ -z "$_wec_system" ] || cat "$_wec_system"; echo; cat "$_wec_extra.tmp"; } > "$_wec_dir/ca-bundle.pem.tmp"
    chmod 644 "$_wec_extra.tmp" "$_wec_dir/ca-bundle.pem.tmp"
    mv -f "$_wec_extra.tmp" "$_wec_extra"
    mv -f "$_wec_dir/ca-bundle.pem.tmp" "$_wec_dir/ca-bundle.pem"
    export SSL_CERT_FILE="$_wec_dir/ca-bundle.pem" REQUESTS_CA_BUNDLE="$_wec_dir/ca-bundle.pem" \
      CURL_CA_BUNDLE="$_wec_dir/ca-bundle.pem" PIP_CERT="$_wec_dir/ca-bundle.pem" \
      GIT_SSL_CAINFO="$_wec_dir/ca-bundle.pem" NODE_EXTRA_CA_CERTS="$_wec_extra"
    # Java does not read SSL_CERT_FILE: a copy of the JDK's trust store with the CA added.
    _wec_jdk="${BOSS_JDK_HOME:-/opt/toolchains/java/jdk}"
    if [ -x "$_wec_jdk/bin/keytool" ] && [ -f "$_wec_jdk/lib/security/cacerts" ]; then
      rm -rf "$_wec_dir/jks.tmp" && mkdir -p "$_wec_dir/jks.tmp" &&
        cp "$_wec_jdk/lib/security/cacerts" "$_wec_dir/jks.tmp/cacerts" &&
        awk -v d="$_wec_dir/jks.tmp" '/BEGIN CERTIFICATE/ { n++; f = d "/cert-" n ".pem" } n { print > f } /END CERTIFICATE/ { close(f) }' "$_wec_extra"
      _wec_jks_ok=1
      for _wec_c in "$_wec_dir"/jks.tmp/cert-*.pem; do
        [ -f "$_wec_c" ] || continue
        "$_wec_jdk/bin/keytool" -importcert -noprompt -storepass changeit \
          -keystore "$_wec_dir/jks.tmp/cacerts" -alias "boss-extra-$(basename "$_wec_c" .pem)" \
          -file "$_wec_c" >/dev/null 2>&1 || _wec_jks_ok=0
      done
      if [ "$_wec_jks_ok" = 1 ]; then
        chmod 644 "$_wec_dir/jks.tmp/cacerts" && mv -f "$_wec_dir/jks.tmp/cacerts" "$_wec_dir/cacerts" &&
          export BOSS_JAVA_TRUST_STORE="$_wec_dir/cacerts"
      else
        _wec_warn "could not add the extra CA to the Java trust store; Maven and Gradle use the JDK's own"
      fi
      rm -rf "$_wec_dir/jks.tmp"
    fi
  else
    rm -f "$_wec_extra.tmp"
    _wec_warn "$_wec_src is not a PEM certificate list of at most 500 KB; the extra CA is NOT trusted."
    _wec_warn "Fix BOSS_EXTRA_CA_FILE in .env and run 'boss upgrade' (boss doctor shows the problem)."
  fi
fi

if [ -n "${BOSS_UPSTREAM_PROXY:-}" ]; then
  case "$BOSS_UPSTREAM_PROXY" in
    *@*) _wec_warn "BOSS_UPSTREAM_PROXY contains a user name or password; ignored (use a local authenticating proxy)" ;;
    http://?*|https://?*)
      export HTTPS_PROXY="$BOSS_UPSTREAM_PROXY" https_proxy="$BOSS_UPSTREAM_PROXY"
      _wec_np="localhost,127.0.0.1,::1,api,egress,worker,runner,db,model-gateway,kg-mcp,mcp-tools,infisical,build-services,web,registry,prometheus,grafana,mailpit,host.docker.internal"
      [ -z "${BOSS_UPSTREAM_NO_PROXY:-}" ] || _wec_np="$_wec_np,$BOSS_UPSTREAM_NO_PROXY"
      export NO_PROXY="$_wec_np" no_proxy="$_wec_np" ;;
    *) _wec_warn "BOSS_UPSTREAM_PROXY must start with http:// or https://; ignored" ;;
  esac
fi
case "${BOSS_UPSTREAM_HTTP_PROXY:-}" in
  "") ;;
  *@*) _wec_warn "BOSS_UPSTREAM_HTTP_PROXY contains a user name or password; ignored" ;;
  http://?*|https://?*)
    export HTTP_PROXY="$BOSS_UPSTREAM_HTTP_PROXY" http_proxy="$BOSS_UPSTREAM_HTTP_PROXY" ;;
  *) _wec_warn "BOSS_UPSTREAM_HTTP_PROXY must start with http:// or https://; ignored" ;;
esac
unset _wec_src _wec_dir _wec_extra _wec_system _wec_f _wec_jdk _wec_c _wec_jks_ok _wec_np

exec "$@"
