#!/usr/bin/env bash
# THE ENTERPRISE LANE (BOSSAPP-1430; docs/operations/TEST_TIERS.md, "The enterprise lane").
#
# Runs BOSS the way a locked-down company box runs it, on a fresh Linux VM (Ubuntu 24.04,
# 4 vCPU / 8 GB / 30 GB), and installs and exercises it end to end:
#
#   1. host       the VM is the small one (cores, memory, disk) and has AppArmor
#   2. network    a "company network" on private addresses: an HTTPS-inspecting egress proxy
#                 (mitmproxy) with its OWN CA, a private npm mirror (verdaccio) and PyPI mirror
#                 (proxpi) behind TLS signed by that CA, and a company DNS for *.entlane.internal.
#                 Then the box is locked down: nothing leaves except through the proxy, Docker
#                 pulls included
#   3. install    install.sh from scratch with --install-tier dev, given the CA, the proxy and the
#                 mirrors the way ENTERPRISE_NETWORK.md says
#   4. sandbox    engine inner-sandbox support on: the AppArmor profile loaded,
#                 BOSS_ENGINE_INNER_SANDBOX=seccomp+apparmor (seccomp where AppArmor is missing),
#                 `boss start`, and `boss doctor`'s engine sandbox line
#   5. codex      a MANAGED Codex policy (requirements.toml forbidding full access) in the api and
#                 worker containers; the launcher check: Codex's own sandbox through the real
#                 launcher (boss-sandbox-exec) under a build's identity, writing and reaching the
#                 private registry. No model: with no Codex key the builds use the scripted engine
#   6. plumbing   scripts/e2e-gate.sh plumbing, in a container with the install's security profiles
#   7. engines    scripts/e2e-gate.sh engines, the same
#   8. build      one scripted build (the engines gate's clean scenario) INSIDE the installed
#                 api container, where builds run: its real profiles, fence, CA and launcher
#
# Output: one PASS/FAIL line and a report file (Markdown) with every step, its time and its
# evidence. Exit 0 only on PASS.
#
# Usage (on the VM, from a BOSS checkout; scripts/enterprise-lane/lima.sh makes the VM on a Mac):
#   scripts/enterprise-lane.sh [--report FILE] [--reuse-install] [--only STEP[,STEP...]]
#
#   --reuse-install  keep an install already made by an earlier lane run (skips step 3)
#   --only           run only these steps (host and network always run: later steps need them)
#
# It needs sudo (packages, the dummy interface, iptables, Docker's daemon settings, AppArmor).
# Credentials: the lane makes none it prints. A model key is never needed; when CODEX_API_KEY or
# OPENAI_API_KEY is exported it is handed to the installer in the environment (never printed),
# exactly as a person would.
#
# Everything it creates is inside this VM: the CA under $ENTLANE_STATE (default
# ~/boss-ent-lane), the install under $ENTLANE_INSTALL_DIR (default ~/boss-lane-install).
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HERE="$ROOT/scripts/enterprise-lane"
STATE="${ENTLANE_STATE:-$HOME/boss-ent-lane}"
INSTALL="${ENTLANE_INSTALL_DIR:-$HOME/boss-lane-install}"
ARGS=("$@")
REPORT=""
REUSE=0
ONLY=""
while [ $# -gt 0 ]; do
  case "$1" in
    --report) REPORT="${2:?--report needs a file}"; shift 2 ;;
    --reuse-install) REUSE=1; shift ;;
    --only) ONLY=",${2:?--only needs steps},"; shift 2 ;;
    -h|--help) sed -n '2,45p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
[ "$(uname -s)" = Linux ] || { echo "enterprise-lane: run this on the Linux VM (scripts/enterprise-lane/lima.sh on a Mac)" >&2; exit 2; }
[ "$(id -u)" != 0 ] || { echo "enterprise-lane: run as a normal user with sudo, not as root" >&2; exit 2; }
# It rewrites Docker's daemon settings, adds iptables REJECT rules, prunes Docker's build cache and
# deletes its install directory: only on a VM made for it (lima.sh sets this).
[ "${ENTLANE_THROWAWAY_VM:-}" = 1 ] || {
  echo "enterprise-lane: this reconfigures the whole machine (Docker, firewall, apt). Run it only on a" >&2
  echo "throwaway VM made for it, with ENTLANE_THROWAWAY_VM=1 (scripts/enterprise-lane/lima.sh sets it)." >&2
  exit 2; }

mkdir -p "$STATE/logs" "$STATE/ca" "$STATE/mitm"
REPORT="${REPORT:-$STATE/report.md}"
LOGS="$STATE/logs"
RESULTS="$STATE/results.tsv"

# The lane's network. 10.77.0.0/24 is the "company LAN" (a dummy interface on the VM), 10.78.0.0/24
# the Docker network the mirrors run on. Nothing here is the Docker host's gateway address, so
# BOSS's fence treats the registries as ordinary internal hosts.
PROXY_IP=10.77.0.10
REG_IP=10.77.0.20
DNS_IP=10.77.0.53
CORP_NET=entlane-corp
CORP_SUBNET=10.78.0.0/24
PROXY_UID=4242
NPM_HOST=npm.entlane.internal
PYPI_HOST=pypi.entlane.internal
IMG_MITM=mitmproxy/mitmproxy:12.3.1
IMG_VERDACCIO=verdaccio/verdaccio:6.11
IMG_PROXPI=epicwink/proxpi:1.3.1
IMG_NGINX=nginx:1.27-alpine
IMG_ALPINE=alpine:3.20
IMG_DNS=entlane-dnsmasq:1

want() {  # want STEP: is this step selected (--only)?
  [ -z "$ONLY" ] || [[ "$ONLY" == *",$1,"* ]]
}

# ---- results ---------------------------------------------------------------------------------
STARTED=$(date +%s)
: > "$RESULTS"
record() {  # record STEP STATUS SECONDS DETAIL
  printf '%s\t%s\t%s\t%s\n' "$1" "$2" "$3" "$(printf '%s' "$4" | tr '\t\n' '  ' | cut -c1-400)" >> "$RESULTS"
  printf '[lane] %-10s %-4s %5ss  %s\n' "$1" "$2" "$3" "$(printf '%s' "$4" | tr '\n' ' ' | cut -c1-200)"
}
# run_step NAME FUNCTION: runs FUNCTION with its output in logs/NAME.log. The function prints its
# one-line evidence as the LAST line of output; non-zero exit is a FAIL.
run_step() {
  local name="$1" fn="$2" t0 rc detail
  t0=$(date +%s)
  echo "[lane] $name ..."
  "$fn" > "$LOGS/$name.log" 2>&1
  rc=$?
  detail="$(grep -v '^[[:space:]]*$' "$LOGS/$name.log" | tail -n 1)"
  if [ "$rc" = 0 ]; then record "$name" PASS $(( $(date +%s) - t0 )) "$detail"
  else record "$name" FAIL $(( $(date +%s) - t0 )) "$detail (log: $LOGS/$name.log)"; fi
  return $rc
}
skip() { record "$1" SKIP 0 "$2"; }

# ---- 1. host ---------------------------------------------------------------------------------
step_host() {
  local cpus mem_gb disk_gb aa
  cpus=$(nproc); mem_gb=$(awk '/MemTotal/ {print int($2/1048576 + 0.5)}' /proc/meminfo)
  disk_gb=$(df -Pk "$HOME" | awk 'NR==2 {print int($4/1048576)}')
  aa=none
  if [ -d /sys/kernel/security/apparmor ]; then
    aa=apparmor3; [ -d /sys/kernel/security/apparmor/features/namespaces ] && \
      grep -qs userns /sys/kernel/security/apparmor/features/namespaces/* 2>/dev/null && aa=apparmor4
    [ -e /sys/kernel/security/apparmor/features/namespaces/userns_create ] && aa=apparmor4
  fi
  echo "$aa" > "$STATE/apparmor"
  echo "cpus=$cpus mem=${mem_gb}G free_disk=${disk_gb}G apparmor=$aa kernel=$(uname -r)"
  if [ "$disk_gb" -lt 15 ]; then
    # A rerun's previous install (images, build cache) is removed by the install step.
    if [ -f "$INSTALL/.env" ]; then echo "WARNING: only ${disk_gb} GB free before the previous lane install is removed"
    else echo "only ${disk_gb} GB free; the lane needs 15 GB"; return 1; fi
  fi
  if [ "$cpus" -gt 4 ] || [ "$mem_gb" -gt 9 ]; then
    echo "WARNING: this VM is bigger than the enterprise box (4 vCPU / 8 GB); small-host gaps may hide"
  fi
  echo "host: ${cpus} vCPU, ${mem_gb} GB, ${disk_gb} GB free, $aa"
}

# ---- 2. the company network --------------------------------------------------------------------
write_apt_proxy() {  # apt through the proxy, as IT configures a company box
  printf 'Acquire::http::Proxy "http://%s:8080";\nAcquire::https::Proxy "http://%s:8080";\n' \
    "$PROXY_IP" "$PROXY_IP" | sudo tee /etc/apt/apt.conf.d/90entlane-proxy >/dev/null
}

provision_packages() {
  local need=()
  for c in docker git make python3 curl openssl iptables jq apparmor_parser; do
    command -v "$c" >/dev/null 2>&1 || need+=("$c")
  done
  docker compose version >/dev/null 2>&1 || need+=(compose)
  docker buildx version >/dev/null 2>&1 || need+=(buildx)
  [ ${#need[@]} -eq 0 ] && return 0
  echo "installing: ${need[*]}"
  # A VM an earlier run locked down reaches the archive only through the lane's proxy.
  if sudo iptables -S ENTLANE-OUT >/dev/null 2>&1; then write_apt_proxy; fi
  sudo apt-get update -q &&
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -yq docker.io docker-buildx docker-compose-v2 \
    git make python3 python3-venv curl openssl iptables jq apparmor apparmor-utils ca-certificates
}

make_ca() {
  local ca="$STATE/ca"
  if [ ! -s "$ca/ca.crt" ]; then
    openssl req -x509 -newkey rsa:2048 -nodes -days 60 -keyout "$ca/ca.key" -out "$ca/ca.crt" \
      -subj "/O=BOSS Enterprise Lane/CN=BOSS Enterprise Lane Root CA" \
      -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign,cRLSign" \
      2>/dev/null || return 1
  fi
  if [ ! -s "$ca/registry.crt" ]; then
    openssl req -newkey rsa:2048 -nodes -keyout "$ca/registry.key" -out "$ca/registry.csr" \
      -subj "/O=BOSS Enterprise Lane/CN=$NPM_HOST" 2>/dev/null || return 1
    printf 'subjectAltName=DNS:%s,DNS:%s\nextendedKeyUsage=serverAuth\nbasicConstraints=CA:FALSE\n' \
      "$NPM_HOST" "$PYPI_HOST" > "$ca/registry.ext"
    openssl x509 -req -in "$ca/registry.csr" -CA "$ca/ca.crt" -CAkey "$ca/ca.key" -CAcreateserial \
      -days 60 -extfile "$ca/registry.ext" -out "$ca/registry.crt" 2>/dev/null || return 1
  fi
  # mitmproxy's confdir: the CA key and certificate in one file, owned by the proxy's uid.
  sudo rm -rf "$STATE/mitm"; mkdir -p "$STATE/mitm"
  cat "$ca/ca.key" "$ca/ca.crt" > "$STATE/mitm/mitmproxy-ca.pem"
  cp "$ca/ca.crt" "$STATE/mitm/mitmproxy-ca-cert.pem"
  sudo chown -R "$PROXY_UID:$PROXY_UID" "$STATE/mitm"
  chmod 644 "$ca/registry.key"   # read by the nginx container; a throwaway lane certificate
}

corp_lan() {
  sudo ip link show entlane0 >/dev/null 2>&1 || sudo ip link add entlane0 type dummy || return 1
  sudo ip link set entlane0 up
  for ip in "$PROXY_IP" "$REG_IP" "$DNS_IP"; do
    ip -4 addr show dev entlane0 | grep -q "inet $ip/" || sudo ip addr add "$ip/32" dev entlane0
  done
  grep -q "$NPM_HOST" /etc/hosts || echo "$REG_IP $NPM_HOST $PYPI_HOST" | sudo tee -a /etc/hosts >/dev/null
}

upstream_dns() {
  awk '/^nameserver/ {print $2; exit}' /run/systemd/resolve/resolv.conf 2>/dev/null || echo 1.1.1.1
}

start_infra() {
  local up; up="$(upstream_dns)"
  docker network inspect "$CORP_NET" >/dev/null 2>&1 ||
    docker network create --subnet "$CORP_SUBNET" "$CORP_NET" >/dev/null || return 1
  docker image inspect "$IMG_DNS" >/dev/null 2>&1 ||
    docker build -q -t "$IMG_DNS" -f "$HERE/dnsmasq.Dockerfile" "$HERE" >/dev/null || return 1
  for img in "$IMG_MITM" "$IMG_VERDACCIO" "$IMG_PROXPI" "$IMG_NGINX" "$IMG_ALPINE"; do
    docker image inspect "$img" >/dev/null 2>&1 || docker pull -q "$img" >/dev/null || return 1
  done
  docker rm -f entlane-dns entlane-proxy entlane-npm entlane-pypi entlane-tls >/dev/null 2>&1
  mkdir -p "$STATE/verdaccio"; sudo chown 10001:65533 "$STATE/verdaccio" 2>/dev/null || true
  docker run -d --name entlane-dns --restart unless-stopped --network host "$IMG_DNS" \
    --listen-address="$DNS_IP" --bind-interfaces --no-resolv --server="$up" \
    --address=/entlane.internal/"$REG_IP" >/dev/null || return 1
  # As PROXY_UID itself (the egress rule below lets that uid out), not through the image's
  # entrypoint, which would re-own the confdir.
  docker run -d --name entlane-proxy --restart unless-stopped --network host \
    --user "$PROXY_UID:$PROXY_UID" -e HOME=/tmp --entrypoint mitmdump \
    -v "$STATE/mitm":/entlane-mitm "$IMG_MITM" \
    --set confdir=/entlane-mitm --listen-host "$PROXY_IP" --listen-port 8080 \
    --set flow_detail=1 >/dev/null || return 1
  docker run -d --name entlane-npm --restart unless-stopped --network "$CORP_NET" \
    -p 127.0.0.1:47873:4873 -v "$HERE/verdaccio.yaml":/verdaccio/conf/config.yaml:ro \
    -v "$STATE/verdaccio":/verdaccio/storage "$IMG_VERDACCIO" >/dev/null || return 1
  docker run -d --name entlane-pypi --restart unless-stopped --network "$CORP_NET" \
    -p 127.0.0.1:45000:5000 "$IMG_PROXPI" >/dev/null || return 1
  docker run -d --name entlane-tls --restart unless-stopped --network host \
    -v "$HERE/nginx.conf":/etc/nginx/nginx.conf:ro \
    -v "$STATE/ca/registry.crt":/etc/entlane/registry.crt:ro \
    -v "$STATE/ca/registry.key":/etc/entlane/registry.key:ro "$IMG_NGINX" >/dev/null || return 1
}

docker_daemon_corp() {
  # The company CA in the VM's trust store, Docker's pulls through the proxy, and the company DNS
  # for every container (ENTERPRISE_NETWORK.md, "Docker's own downloads", Linux).
  sudo cp "$STATE/ca/ca.crt" /usr/local/share/ca-certificates/entlane-ca.crt
  sudo update-ca-certificates >/dev/null 2>&1
  write_apt_proxy
  local want
  want=$(jq -n --arg p "http://$PROXY_IP:8080" --arg d "$DNS_IP" \
    '{dns: [$d], proxies: {"http-proxy": $p, "https-proxy": $p,
      "no-proxy": "localhost,127.0.0.1,.entlane.internal,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"}}')
  if ! sudo cmp -s <(echo "$want") /etc/docker/daemon.json 2>/dev/null; then
    echo "$want" | sudo tee /etc/docker/daemon.json >/dev/null
    sudo systemctl restart docker || return 1
    for _ in $(seq 1 60); do docker info >/dev/null 2>&1 && break; sleep 1; done
  fi
}

lockdown() {
  # NOTHING LEAVES EXCEPT THROUGH THE PROXY: the VM itself (installer, git, Docker's pulls) and
  # every container. Allowed: loopback, private ranges (the company LAN, Docker's networks, the
  # VM's own resolver), DNS, the proxy's own uid, and the mirrors' network (an Artifactory has
  # its own way out). IPv6 out is refused outright.
  local priv="10.0.0.0/8 172.16.0.0/12 192.168.0.0/16"
  sudo iptables -N ENTLANE-OUT 2>/dev/null; sudo iptables -F ENTLANE-OUT
  sudo iptables -C OUTPUT -j ENTLANE-OUT 2>/dev/null || sudo iptables -I OUTPUT 1 -j ENTLANE-OUT
  sudo iptables -A ENTLANE-OUT -o lo -j RETURN
  sudo iptables -A ENTLANE-OUT -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN
  for n in $priv; do sudo iptables -A ENTLANE-OUT -d "$n" -j RETURN; done
  sudo iptables -A ENTLANE-OUT -p udp --dport 53 -j RETURN
  sudo iptables -A ENTLANE-OUT -p tcp --dport 53 -j RETURN
  sudo iptables -A ENTLANE-OUT -p udp --dport 123 -j RETURN
  sudo iptables -A ENTLANE-OUT -m owner --uid-owner "$PROXY_UID" -j RETURN
  sudo iptables -A ENTLANE-OUT -j REJECT
  sudo iptables -N ENTLANE-FWD 2>/dev/null; sudo iptables -F ENTLANE-FWD
  sudo iptables -C DOCKER-USER -j ENTLANE-FWD 2>/dev/null || sudo iptables -I DOCKER-USER 1 -j ENTLANE-FWD
  sudo iptables -A ENTLANE-FWD -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN
  sudo iptables -A ENTLANE-FWD -s "$CORP_SUBNET" -j RETURN
  for n in $priv; do sudo iptables -A ENTLANE-FWD -d "$n" -j RETURN; done
  sudo iptables -A ENTLANE-FWD -p udp --dport 53 -j RETURN
  sudo iptables -A ENTLANE-FWD -p tcp --dport 53 -j RETURN
  sudo iptables -A ENTLANE-FWD -j REJECT
  sudo ip6tables -N ENTLANE-OUT6 2>/dev/null; sudo ip6tables -F ENTLANE-OUT6
  sudo ip6tables -C OUTPUT -j ENTLANE-OUT6 2>/dev/null || sudo ip6tables -I OUTPUT 1 -j ENTLANE-OUT6
  sudo ip6tables -A ENTLANE-OUT6 -o lo -j RETURN
  sudo ip6tables -A ENTLANE-OUT6 -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN
  sudo ip6tables -A ENTLANE-OUT6 -d fe80::/10 -j RETURN
  sudo ip6tables -A ENTLANE-OUT6 -j REJECT
  sudo ip6tables -N ENTLANE-FWD6 2>/dev/null; sudo ip6tables -F ENTLANE-FWD6
  sudo ip6tables -C FORWARD -j ENTLANE-FWD6 2>/dev/null || sudo ip6tables -I FORWARD 1 -j ENTLANE-FWD6
  sudo ip6tables -A ENTLANE-FWD6 -j REJECT
}

wait_http() {  # wait_http URL [curl args]: up to 90 s for a 2xx
  local url="$1"; shift
  for _ in $(seq 1 45); do
    curl -fsS -o /dev/null --max-time 10 "$@" "$url" 2>/dev/null && return 0
    sleep 2
  done
  return 1
}

step_network() {
  provision_packages || { echo "could not install the VM's packages"; return 1; }
  docker info >/dev/null 2>&1 || { echo "docker is not usable by $(id -un) (docker group?)"; return 1; }
  make_ca || { echo "could not make the lane CA"; return 1; }
  corp_lan || { echo "could not add the company LAN addresses"; return 1; }
  start_infra || { echo "could not start the proxy, mirrors and DNS"; return 1; }
  docker_daemon_corp || { echo "could not point Docker at the proxy and company DNS"; return 1; }
  lockdown || { echo "could not lock the VM's egress down"; return 1; }
  local ca="$STATE/ca/ca.crt" issuer bad=0
  for _ in $(seq 1 30); do (exec 3<>"/dev/tcp/$PROXY_IP/8080") 2>/dev/null && break; sleep 1; done
  # The proxy re-signs public HTTPS with the lane's CA (it inspects).
  issuer=$(echo | timeout 20 openssl s_client -proxy "$PROXY_IP:8080" -connect registry.npmjs.org:443 \
    -servername registry.npmjs.org 2>/dev/null | openssl x509 -noout -issuer 2>/dev/null)
  echo "proxy issuer: $issuer"
  [[ "$issuer" == *"BOSS Enterprise Lane"* ]] || { echo "FAIL the proxy does not re-sign with the lane CA"; bad=1; }
  # Direct egress is refused, from the VM and from a container.
  if curl -fsS -o /dev/null --max-time 8 https://pypi.org/simple/ 2>/dev/null; then
    echo "FAIL the VM reaches the internet without the proxy"; bad=1; fi
  if docker run --rm "$IMG_ALPINE" wget -q -T 8 -O /dev/null https://pypi.org/simple/ >/dev/null 2>&1; then
    echo "FAIL a container reaches the internet without the proxy"; bad=1; fi
  # Through the proxy it works.
  curl -fsS -o /dev/null --max-time 20 --proxy "http://$PROXY_IP:8080" --cacert "$ca" \
    https://pypi.org/simple/pip/ || { echo "FAIL pypi.org through the proxy"; bad=1; }
  # The company DNS answers inside containers; the mirrors serve over the lane's TLS.
  docker run --rm "$IMG_ALPINE" nslookup "$NPM_HOST" 2>/dev/null | grep -q "$REG_IP" ||
    { echo "FAIL $NPM_HOST does not resolve inside a container"; bad=1; }
  wait_http "https://$NPM_HOST/-/ping" --cacert "$ca" || { echo "FAIL the npm mirror"; bad=1; }
  wait_http "https://$NPM_HOST/is-number" --cacert "$ca" || { echo "FAIL the npm mirror cannot fetch a package"; bad=1; }
  wait_http "https://$PYPI_HOST/index/six/" --cacert "$ca" || { echo "FAIL the PyPI mirror"; bad=1; }
  # Docker's own pulls go through the proxy.
  docker pull -q hello-world:latest >/dev/null 2>&1 || { echo "FAIL docker pull through the proxy"; bad=1; }
  [ "$bad" = 0 ] || return 1
  echo "company network up: inspecting proxy $PROXY_IP:8080 (lane CA), npm + PyPI mirrors on $REG_IP, direct egress refused"
}

# ---- 3. install ---------------------------------------------------------------------------------
compose_project() { awk -F= '/^COMPOSE_PROJECT_NAME=/ {print $2}' "$INSTALL/.env" 2>/dev/null | tail -n1; }
ctr() { echo "$(compose_project || true)-$1-1" | sed 's/^-/boss-community-/'; }
boss_cmd() { (cd "$INSTALL" && env -u BOSS_INSTALL_DIR bash scripts/install/boss.sh --dir "$INSTALL" "$@"); }

step_install() {
  if [ -e "$INSTALL" ]; then
    { [ -f "$INSTALL/.entlane" ] || grep -qxF "$INSTALL" "$STATE/owned-install" 2>/dev/null; } ||
      { echo "$INSTALL exists and is not a lane install; refusing to remove it"; return 1; }
    echo "removing the previous lane install at $INSTALL"
    if [ -f "$INSTALL/.env" ]; then
      (cd "$INSTALL" && docker compose $(bash scripts/install/compose_files.sh . 2>/dev/null | tr '\n' ' ') \
        --env-file .env down -v --remove-orphans --rmi all) >/dev/null 2>&1 || true
    fi
    # Whatever compose could not take down (a half-made install): its containers would recreate
    # their bind-mount sources inside the directory as root.
    local proj; proj="$(compose_project)"; proj="${proj:-boss-community}"
    docker ps -aq --filter "label=com.docker.compose.project=$proj" | xargs -r docker rm -f >/dev/null 2>&1
    sudo rm -rf "$INSTALL"
  fi
  # Every run builds from scratch on a clean disk: no BOSS image, volume or build cache left
  # from an earlier run (the lane's own proxy, mirrors and DNS keep theirs: they are running).
  docker image prune -af >/dev/null 2>&1 || true
  docker volume prune -af >/dev/null 2>&1 || true
  docker builder prune -af >/dev/null 2>&1 || true
  echo "disk before install: $(df -Pk "$HOME" | awk 'NR==2 {print int($4/1048576)}') GB free"
  local key_note="no model key (scripted engine only)"
  if [ -n "${CODEX_API_KEY:-}${OPENAI_API_KEY:-}" ]; then
    export OPENAI_API_KEY="${OPENAI_API_KEY:-${CODEX_API_KEY:-}}"
    key_note="an OpenAI/Codex key from the environment (not printed)"
  fi
  echo "$INSTALL" > "$STATE/owned-install"   # the lane made it: a later run may remove it
  echo "installing $(git -C "$ROOT" rev-parse --short HEAD) into $INSTALL with $key_note"
  # install.sh copies THIS checkout's HEAD (a local installer tree), as a person who downloaded
  # the release would get it. The enterprise block, the way ENTERPRISE_NETWORK.md says.
  ( cd "$ROOT" &&
    BOSS_EXTRA_CA_FILE="$STATE/ca/ca.crt" \
    BOSS_UPSTREAM_PROXY="http://$PROXY_IP:8080" \
    BOSS_UPSTREAM_HTTP_PROXY="http://$PROXY_IP:8080" \
    BOSS_UPSTREAM_NO_PROXY=".entlane.internal" \
    BOSS_NPM_REGISTRY="https://$NPM_HOST/" \
    BOSS_PIP_INDEX_URL="https://$PYPI_HOST/index/" \
    BOSS_EGRESS_PRIVATE_ALLOW="$NPM_HOST,$PYPI_HOST" \
    ./install.sh --dir "$INSTALL" --yes --skip-model-key --install-tier dev --no-discover \
      --no-local-model < /dev/null ) || { echo "install.sh failed (see this log)"; return 1; }
  local health
  health=$(curl -fsS --max-time 10 "http://127.0.0.1:$(awk -F= '/^BOSS_API_PORT=/ {print $2}' "$INSTALL/.env")/health" 2>&1) ||
    { echo "the API does not answer /health after the install: $health"; return 1; }
  touch "$INSTALL/.entlane"   # marks the directory as the lane's own, for the next run
  local before after
  before=$(df -Pk "$HOME" | awk 'NR==2 {print int($4/1048576)}')
  # The 30 GB box: an install leaves its build cache behind (about 10 GB), and the e2e gate
  # needs 5 GB free. Free it as an operator would (`docker builder prune`).
  docker builder prune -af >/dev/null 2>&1 || true
  after=$(df -Pk "$HOME" | awk 'NR==2 {print int($4/1048576)}')
  echo "disk after install: ${before} GB free, ${after} GB after pruning the build cache"
  echo "installed: $(git -C "$INSTALL" rev-parse --short HEAD), ${before}->${after} GB free, tier $(awk -F= '/^BOSS_INSTALL_TIER=/ {print $2}' "$INSTALL/.env"), API healthy"
}

# ---- 4. engine inner sandbox ---------------------------------------------------------------------
SANDBOX_MODE=seccomp
step_sandbox() {
  [ "$(cat "$STATE/apparmor" 2>/dev/null)" = none ] || SANDBOX_MODE=seccomp+apparmor
  if [ "$SANDBOX_MODE" = seccomp+apparmor ]; then
    sudo "$INSTALL/scripts/sandbox/load_apparmor.sh" || { echo "the AppArmor profile did not load"; return 1; }
  fi
  sed -i '/^BOSS_ENGINE_INNER_SANDBOX=/d' "$INSTALL/.env"
  echo "BOSS_ENGINE_INNER_SANDBOX=$SANDBOX_MODE" >> "$INSTALL/.env"
  boss_cmd start || { echo "boss start failed after turning the inner sandbox on"; return 1; }
  local label
  label=$(docker exec "$(ctr worker)" cat /proc/1/attr/current 2>/dev/null | tr -d '\0')
  echo "worker AppArmor label: ${label:-none}"
  boss_cmd doctor 2>&1 | sed 's/\x1b\[[0-9;]*m//g' > "$LOGS/doctor.txt"
  cat "$LOGS/doctor.txt"
  local line; line=$(grep -i 'engine sandbox' "$LOGS/doctor.txt" | head -n1)
  [ -n "$line" ] || { echo "boss doctor printed no engine sandbox line"; return 1; }
  # doctor prints "ok" or "bad"/"✗" first on each line.
  grep -qE '^[[:space:]]*(✗|FAIL|bad|BAD|warn|WARN)\b' <<<"$line" && { echo "boss doctor: $line"; return 1; }
  grep -qE '^[[:space:]]*(✓|ok|OK)\b' <<<"$line" || { echo "boss doctor: $line"; return 1; }
  echo "inner sandbox $SANDBOX_MODE on; boss doctor: $(echo "$line" | sed 's/^[^a-zA-Z]*//' | cut -c1-160)"
}

# ---- 5. managed Codex policy + the launcher check --------------------------------------------------
step_codex() {
  local c
  for c in "$(ctr api)" "$(ctr worker)"; do
    docker exec -u 0 "$c" mkdir -p /etc/codex &&
    docker cp "$HERE/codex-requirements.toml" "$c:/etc/codex/requirements.toml" &&
    docker exec -u 0 "$c" chmod 644 /etc/codex/requirements.toml ||
      { echo "could not place the managed Codex policy in $c"; return 1; }
  done
  docker cp "$HERE/launcher_check.py" "$(ctr api):/tmp/entlane_launcher_check.py" || return 1
  local out rc
  # As `boss`, the user the API runs builds as (docker exec defaults to root).
  out=$(docker exec -u boss "$(ctr api)" /usr/local/libexec/boss/with-extra-ca \
        python3 /tmp/entlane_launcher_check.py boss-dogfood 2>&1); rc=$?
  echo "$out"
  [ "$rc" = 0 ] || { echo "launcher check failed: $(echo "$out" | tail -n1 | cut -c1-300)"; return 1; }
  echo "managed Codex policy placed; Codex's own sandbox starts, writes and reaches the private npm mirror through boss-sandbox-exec"
}

# ---- 6/7. the e2e gate, with the install's security profiles ---------------------------------------
gate() {
  local tier="$1" out rc
  # This checkout (the commit the install was made from), in the installed worker image. Not the
  # install directory itself: the installer makes it private to its owner (umask 077).
  bash "$ROOT/scripts/e2e-gate.sh" "$tier" --image "$(docker inspect -f '{{.Config.Image}}' "$(ctr worker)")" \
    --inner-sandbox "$SANDBOX_MODE" --enterprise-env "$INSTALL/.env" --keep 2>&1; rc=$?
  return $rc
}
step_plumbing() { gate plumbing; }
step_engines() { gate engines; }

# ---- 8. one scripted build inside the installed stack (api container, where builds run) ---------------------------------------------
step_build() {
  # Builds run in the api container (the worker leases jobs; the API container holds the CLIs,
  # boss-sandbox-exec and the fence), so that is where the real build path is.
  local w; w="$(ctr api)"
  local db; db="$(ctr db)"
  local pw; pw=$(awk -F= '/^BOSS_DB_PASSWORD=/ {print $2}' "$INSTALL/.env")
  local envf; envf="$(mktemp)"; chmod 600 "$envf"
  # The gate's database lives on the install's own Postgres (a lane VM's throwaway install).
  pw=$(printf '%s' "$pw" | python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.stdin.read(), safe=""))')
  printf 'BOSS_E2E_GATE_PG_URL=postgresql://boss:%s@db:5432/postgres\n' "$pw" > "$envf"
  unset pw
  docker exec -u 0 "$w" rm -rf /tmp/entlane-gate && docker exec -u 0 "$w" mkdir -p /tmp/entlane-gate &&
  docker cp "$ROOT/tests/e2e" "$w:/tmp/entlane-gate/e2e" &&
  docker exec -u 0 "$w" chown -R boss /tmp/entlane-gate || { rm -f "$envf"; echo "could not copy the gate into the api container"; return 1; }
  local rc
  # The gate enters builds without a person (identity in shadow), as the engines tier does: it
  # tests the pipeline in this container, not who may start it.
  docker exec -u boss --env-file "$envf" -e BOSS_AUTHZ_MODE=shadow -e BOSS_ARTIFACTS_DIR=/tmp/entlane-gate/artifacts \
    -e BOSS_BUILD_SERVICES_PREFIX=boss_entlane_ -w /tmp "$w" \
    /usr/local/libexec/boss/with-extra-ca python3 /tmp/entlane-gate/e2e/gate/engines.py \
    --scenario clean --out /tmp/entlane-gate/results.json; rc=$?
  rm -f "$envf"
  docker cp "$w:/tmp/entlane-gate/results.json" "$LOGS/build-results.json" 2>/dev/null
  python3 - "$LOGS/build-results.json" <<'PY'
import json, sys
try:
    rows = json.load(open(sys.argv[1]))["results"]
except Exception as exc:
    print(f"no results: {exc}"); sys.exit(1)
for r in rows:
    print(f"{r['status']:5} {r['check']}: {r.get('detail', '')[:300]}")
bad = [r for r in rows if r["status"] != "PASS"]
print(("FAIL " if bad else "") + f"scripted build in the installed stack: {len(rows) - len(bad)}/{len(rows)} checks pass")
sys.exit(1 if bad or not rows else 0)
PY
  [ $? = 0 ] && [ "$rc" = 0 ]
}

# ---- the run --------------------------------------------------------------------------------------
run_step host step_host || echo "[lane] the host is smaller than the lane needs; continuing"
# Docker for this user: install it on a fresh VM, then run the rest in the docker group (once).
if ! docker info >/dev/null 2>&1; then
  provision_packages > "$LOGS/packages.log" 2>&1 || { echo "enterprise-lane: could not install Docker (see $LOGS/packages.log)" >&2; exit 2; }
  if [ -z "${ENTLANE_REEXEC:-}" ]; then
    getent group docker | grep -qw "$(id -un)" || sudo usermod -aG docker "$(id -un)"
    exec sg docker -c "ENTLANE_REEXEC=1 $(printf '%q ' "$0" "${ARGS[@]}")"
  fi
  echo "enterprise-lane: docker is not usable by $(id -un)" >&2; exit 2
fi
NET_OK=0; run_step network step_network && NET_OK=1
INSTALL_OK=0
if [ "$NET_OK" != 1 ]; then
  for s in install sandbox codex plumbing engines build; do skip "$s" "the company network is not up"; done
else
  if [ "$REUSE" = 1 ] && [ -f "$INSTALL/.env" ]; then
    record install PASS 0 "reused the install at $INSTALL (--reuse-install)"; INSTALL_OK=1
  elif want install; then
    run_step install step_install && INSTALL_OK=1
  else
    [ -f "$INSTALL/.env" ] && INSTALL_OK=1
  fi
  if [ "$INSTALL_OK" != 1 ]; then
    for s in sandbox codex plumbing engines build; do skip "$s" "no install"; done
  else
    [ "$(cat "$STATE/apparmor" 2>/dev/null)" = none ] || SANDBOX_MODE=seccomp+apparmor
    want sandbox && run_step sandbox step_sandbox
    want codex && run_step codex step_codex
    want plumbing && run_step plumbing step_plumbing
    want engines && run_step engines step_engines
    want build && run_step build step_build
  fi
fi

# ---- summary + report -------------------------------------------------------------------------------
TOTAL=$(( $(date +%s) - STARTED ))
FAILS=$(awk -F'\t' '$2 != "PASS"' "$RESULTS" | wc -l | tr -d ' ')
VERDICT=PASS; [ "$FAILS" = 0 ] || VERDICT=FAIL
# Only a full run from a fresh install is release evidence.
if [ "$VERDICT" = PASS ] && { [ -n "$ONLY" ] || [ "$REUSE" = 1 ]; }; then VERDICT=PARTIAL; fi
{
  echo "# Enterprise lane report"
  echo
  echo "- Verdict: **$VERDICT** ($FAILS of $(wc -l < "$RESULTS" | tr -d ' ') steps not passing), $(( TOTAL / 60 )) min"
  echo "- BOSS: $(git -C "$ROOT" rev-parse --short HEAD) ($(git -C "$ROOT" log -1 --format=%s | cut -c1-80))"
  echo "- Host: $(nproc) vCPU, $(awk '/MemTotal/ {print int($2/1048576 + 0.5)}' /proc/meminfo) GB, kernel $(uname -r), $(cat "$STATE/apparmor" 2>/dev/null), inner sandbox $SANDBOX_MODE"
  echo "- Network: egress only through an inspecting proxy with its own CA; npm + PyPI mirrors on a private address"
  echo "- Date: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo
  echo "| Step | Result | Seconds | Evidence |"
  echo "|---|---|---|---|"
  awk -F'\t' '{gsub(/\|/, "/", $4); printf "| %s | %s | %s | %s |\n", $1, $2, $3, $4}' "$RESULTS"
  echo
  echo "Logs: \`$LOGS\` (one per step; the e2e gate tables are at the end of plumbing.log and engines.log)."
  if docker logs entlane-proxy 2>/dev/null | head -1 >/dev/null; then
    echo
    echo "Hosts reached through the proxy (top 15):"
    echo
    docker logs entlane-proxy 2>&1 | grep -oE 'https?://[^/: ]+' | sort | uniq -c | sort -rn | head -15 |
      awk '{printf "- %s (%s)\n", $2, $1}'
  fi
} > "$REPORT"
echo
echo "== enterprise lane: $VERDICT ($FAILS failing) in $(( TOTAL / 60 )) min. Report: $REPORT"
[ "$VERDICT" != FAIL ]
