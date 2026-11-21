#!/usr/bin/env bash
# Network checks the installer runs before the image build (docs/community/ENTERPRISE_NETWORK.md).
# Sourced by install.sh, which runs with `set -euo pipefail`: every command that may fail here is
# guarded, so a refused connection is a finding, never an abort. each function prints its own guidance and returns non-zero on a problem
# that would make the build fail later, after many minutes, with a less useful message.
#
#   boss_tls_diagnose CA_FILE PROXY HOST[:PORT]...
#       Is TLS to each host intercepted (a corporate gateway re-signing traffic)? A host the
#       machine reaches with a certificate no public root vouches for, and that BOSS_EXTRA_CA_FILE
#       does not cover, fails: the images would fail the same way. It names the issuer and, on a
#       Mac, the command that exports that CA from the keychain. TLS is never turned off.
#   boss_docker_credential_helper_check
#       ~/.docker/config.json names a credential helper (credsStore) that is not installed: every
#       image pull fails with "docker-credential-…: executable file not found". The usual cause is
#       a Mac where Docker Desktop was removed (credsStore: desktop) in favour of Colima.

boss_tls_issuer() {
  # The issuer of the certificate HOST presents, as curl reports it (no verification).
  local target="$1" proxy="$2"
  { curl -sv -k -o /dev/null --max-time 10 ${proxy:+--proxy "$proxy"} "https://$target/" 2>&1 || true; } |
    sed -n 's/^\*[[:space:]]*issuer:[[:space:]]*//p' | head -1 || true
}

boss_tls_cn() {
  # CN=… out of an issuer line ("C=US; O=Example; CN=Example Root" or "/C=US/O=…/CN=…").
  printf '%s\n' "$1" | tr ';/,' '\n\n\n' | sed -n 's/^[[:space:]]*CN[[:space:]]*=[[:space:]]*//p' | head -1
}

boss_tls_diagnose() {
  local ca="$1" proxy="$2"; shift 2
  local target rc issuer cn failed=0 reached=0
  local -a intercepted=()
  for target in "$@"; do
    rc=0
    curl -sS -o /dev/null --max-time 10 ${proxy:+--proxy "$proxy"} "https://$target/" >/dev/null 2>&1 || rc=$?
    case "$rc" in
      0|22) reached=1; continue ;;
      60|77|35|51|58|83|90|91)
        reached=1
        if [ -n "$ca" ] && curl -sS -o /dev/null --max-time 10 --cacert "$ca" ${proxy:+--proxy "$proxy"} \
             "https://$target/" >/dev/null 2>&1; then
          printf '  ✓ %s: trusted through BOSS_EXTRA_CA_FILE\n' "$target"
          continue
        fi
        issuer="$(boss_tls_issuer "$target" "$proxy")"
        intercepted+=("$target|$issuer")
        failed=1 ;;
      *)
        printf '  ! %s: not reachable from this machine (curl exit %s). If this network reaches the internet only through a proxy, set BOSS_UPSTREAM_PROXY.\n' "$target" "$rc" ;;
    esac
  done
  [ "$failed" -eq 1 ] || return 0
  printf '\n  ✗ TLS to these hosts is intercepted: the certificate they present is not issued by a public CA.\n'
  local item first_cn=""
  for item in "${intercepted[@]}"; do
    target="${item%%|*}"; issuer="${item#*|}"
    cn="$(boss_tls_cn "$issuer")"
    [ -n "$first_cn" ] || first_cn="$cn"
    printf '      %s — issued by: %s\n' "$target" "${issuer:-unknown}"
  done
  printf '    BOSS keeps TLS verification on. Give it your organisation'"'"'s root CA instead:\n'
  printf '      1. Export the root CA certificate (PEM) of the gateway that issued the certificate above.\n'
  if [ "$(uname -s 2>/dev/null)" = Darwin ]; then
    printf '         On a Mac it is usually in the System keychain:\n'
    printf '           security find-certificate -a -c "%s" -p /Library/Keychains/System.keychain > "$HOME/boss-extra-ca.pem"\n' "${first_cn:-<issuer name>}"
    printf '         If that file is empty, search by the organisation name instead of "%s", or ask IT for the gateway'"'"'s root CA.\n' "${first_cn:-<issuer name>}"
  else
    printf '         Ask IT for it, or find it in the system store (e.g. /usr/local/share/ca-certificates).\n'
  fi
  printf '      2. export BOSS_EXTRA_CA_FILE="$HOME/boss-extra-ca.pem"   (an absolute path; certificates only, never a key)\n'
  printf '      3. Run the installer again. See docs/community/ENTERPRISE_NETWORK.md.\n'
  [ -z "$ca" ] || printf '    BOSS_EXTRA_CA_FILE is set but does not cover these hosts: it may hold the wrong CA (an intermediate rather than the root).\n'
  return 1
}

boss_docker_credential_helper_check() {
  local cfg="${DOCKER_CONFIG:-$HOME/.docker}/config.json" store
  [ -f "$cfg" ] || return 0
  store="$(python3 -c 'import json, sys
try:
    print(json.load(open(sys.argv[1])).get("credsStore") or "")
except Exception:
    print("")' "$cfg" 2>/dev/null)"
  [ -n "$store" ] || return 0
  command -v "docker-credential-$store" >/dev/null 2>&1 && return 0
  printf '  ✗ Docker is set to use the credential helper "docker-credential-%s" (credsStore in %s), and it is not installed.\n' "$store" "$cfg"
  printf '    Every image pull will fail with "executable file not found". Keep your saved logins; fix the setting:\n'
  case "$store" in
    desktop)
      printf '      Docker Desktop was removed (or is not on PATH) but its helper is still named. Back up the file, then\n'
      printf '      either reinstall/start Docker Desktop, or: brew install docker-credential-helper and change\n'
      printf '      "credsStore": "desktop" to "credsStore": "osxkeychain" in %s.\n' "$cfg" ;;
    osxkeychain)
      printf '      brew install docker-credential-helper   (the macOS keychain helper)\n' ;;
    *)
      printf '      Install docker-credential-%s, or back up %s and remove its "credsStore" line.\n' "$store" "$cfg" ;;
  esac
  printf '    See docs/community/COLIMA.md (Docker credential helper).\n'
  return 1
}
