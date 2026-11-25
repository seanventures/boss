"""The enterprise lane's network step reruns from a known state (2026-11-24).

A second lane run on the same VM failed its `network` step in 0 s: the first run had pointed
Docker's daemon at the lane's own "company proxy", and bringing that proxy back up needed an image
pull that went through the proxy that was not up ("proxyconnect tcp: dial tcp 10.77.0.10:8080:
connect: connection refused"). A bootstrap loop the first run on a fresh VM never meets.

The class fix, held here without a VM: the network step (1) takes back every setting an earlier run
made, (2) fetches every image it runs while egress is still direct, (3) starts the proxy, mirrors and
DNS from those images with no pull, and only then (4) points Docker, apt and the trust store at the
proxy and (5) locks egress down. A fake Docker refuses any pull while its daemon points at a proxy
that is not running, exactly as the real one did, so these tests fail on the old order.
"""
from __future__ import annotations

import json
import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

from boss_engine.services import failure_words as fw

REPO = Path(__file__).resolve().parents[2]
LANE = REPO / "scripts" / "enterprise-lane.sh"

# One fake for docker, sudo, iptables, ip6tables, systemctl and update-ca-certificates. Its state
# lives in $FAKE: img/<name> (images on the VM), daemon-proxy (the proxy the running daemon uses),
# proxy-up (the lane's proxy container runs), ipt/<tool>-<hook>-<chain> (a firewall hook).
FAKE = r"""#!/usr/bin/env bash
me="$(basename "$0")"
printf '%s %s\n' "$me" "$*" >> "$FAKE/calls"
key() { printf '%s' "$1" | tr '/:' '__'; }
case "$me" in
  sudo) exec "$@" ;;
  update-ca-certificates|apt-get) exit 0 ;;
  systemctl)
    [ "$1 $2" = "restart docker" ] || exit 0
    [ -n "${FAKE_RESTART_KEEPS_PROXY:-}" ] && exit 0
    if [ -f "$ENTLANE_DAEMON_JSON" ] && grep -q '"https-proxy"' "$ENTLANE_DAEMON_JSON"; then
      python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["proxies"]["https-proxy"])' \
        "$ENTLANE_DAEMON_JSON" > "$FAKE/daemon-proxy"
    else rm -f "$FAKE/daemon-proxy"; fi
    exit 0 ;;
  iptables|ip6tables)
    mkdir -p "$FAKE/ipt"
    case "$1" in
      -C) [ -e "$FAKE/ipt/$me-$2-$4" ]; exit $? ;;
      -I) touch "$FAKE/ipt/$me-$2-$5"; exit 0 ;;
      -D) rm -f "$FAKE/ipt/$me-$2-$4"; exit 0 ;;
      *) exit 0 ;;
    esac ;;
  docker)
    fetch_ok() {  # a pull/build reaches the internet only directly, or through a proxy that is up
      if [ -s "$FAKE/daemon-proxy" ] && [ ! -e "$FAKE/proxy-up" ]; then
        echo "failed to resolve reference \"docker.io/library/$1\": proxyconnect tcp: dial tcp 10.77.0.10:8080: connect: connection refused" >&2
        return 1
      fi
      [ "$1" != "${FAKE_PULL_FAIL:-}" ] || { echo "pull refused for $1" >&2; return 1; }
    }
    case "$1" in
      info)
        if [[ "$*" == *HTTPSProxy* ]]; then cat "$FAKE/daemon-proxy" 2>/dev/null; fi
        exit 0 ;;
      image) [ -e "$FAKE/img/$(key "$3")" ]; exit $? ;;
      pull) img="${@: -1}"; fetch_ok "$img" || exit 1; mkdir -p "$FAKE/img"; touch "$FAKE/img/$(key "$img")"; exit 0 ;;
      build)
        tag=""; prev=""; file=""
        for a in "$@"; do [ "$prev" = -t ] && tag="$a"; [ "$prev" = -f ] && file="$a"; prev="$a"; done
        [ -f "$file" ] || { echo "no Dockerfile at $file" >&2; exit 1; }
        fetch_ok alpine:3.20 || exit 1
        mkdir -p "$FAKE/img"; touch "$FAKE/img/$(key "$tag")"; exit 0 ;;
      run)
        [[ "$*" == *"--pull never"* ]] || { echo "docker run without --pull never" >&2; exit 1; }
        [ -n "${FAKE_RUN_FAIL:-}" ] && exit 1
        for a in "$@"; do
          [ -e "$FAKE/img/$(key "$a")" ] && found=1
        done
        [ -n "${found:-}" ] || { echo "no such image" >&2; exit 1; }
        [[ "$*" == *"--name entlane-proxy"* ]] && touch "$FAKE/proxy-up"
        exit 0 ;;
      rm) [[ "$*" == *entlane-proxy* ]] && rm -f "$FAKE/proxy-up"; exit 0 ;;
      rmi) shift; for a in "$@"; do rm -f "$FAKE/img/$(key "$a")"; done; exit 0 ;;
      *) exit 0 ;;
    esac ;;
esac
exit 0
"""

TOOLS = ("docker", "sudo", "iptables", "ip6tables", "systemctl", "update-ca-certificates", "apt-get")

# The step itself, with the parts that need a real Linux box stubbed: packages, the CA, the dummy
# LAN, and the post-bring-up checks (verify_network: proven on the VM, held statically below).
DRIVER = r"""
source "$LANE"
provision_packages() { :; }
make_ca() { :; }
corp_lan() { :; }
verify_network() { echo "company network up"; }
step_network
"""


class World:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.fake = tmp / "fake"
        self.fake.mkdir()
        bindir = tmp / "bin"
        bindir.mkdir()
        exe = bindir / "fake"
        exe.write_text(FAKE)
        exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
        for tool in TOOLS:
            (bindir / tool).symlink_to(exe)
        etc = tmp / "etc"
        etc.mkdir()
        state = tmp / "state"
        (state / "ca").mkdir(parents=True)
        (state / "ca" / "ca.crt").write_text("lane CA\n")
        self.daemon_json = etc / "daemon.json"
        self.apt = etc / "90entlane-proxy"
        self.trust = etc / "entlane-ca.crt"
        self.env = {**{k: v for k, v in os.environ.items() if not k.startswith(("BOSS_", "ENTLANE_"))},
                    "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}", "FAKE": str(self.fake),
                    "LANE": str(LANE), "ENTLANE_STATE": str(state),
                    "ENTLANE_DAEMON_JSON": str(self.daemon_json), "ENTLANE_APT_PROXY_FILE": str(self.apt),
                    "ENTLANE_CA_TRUST_FILE": str(self.trust)}

    def network(self, **extra: str) -> subprocess.CompletedProcess:
        (self.fake / "calls").unlink(missing_ok=True)
        return subprocess.run(["bash", "-c", DRIVER], env={**self.env, **extra},
                              capture_output=True, text=True, timeout=120)

    def calls(self) -> list[str]:
        return (self.fake / "calls").read_text().splitlines()

    def reboot(self) -> None:
        """What a VM restart keeps (files) and loses (firewall, running containers)."""
        for p in (self.fake / "ipt").glob("*"):
            p.unlink()
        (self.fake / "proxy-up").unlink(missing_ok=True)

    def install_pruned(self) -> None:
        """The install step's `docker image prune -af`: images no container uses are gone."""
        for name in ("alpine_3.20", "hello-world_latest"):
            (self.fake / "img" / name).unlink(missing_ok=True)

    def settings(self) -> dict:
        return {"daemon": self.daemon_json.read_text() if self.daemon_json.exists() else None,
                "apt": self.apt.read_text() if self.apt.exists() else None,
                "trust": self.trust.exists(),
                "firewall": sorted(p.name for p in (self.fake / "ipt").glob("*")),
                "proxy_up": (self.fake / "proxy-up").exists()}


def _usable(line: str) -> list[str]:
    """failure_words contract over one lane evidence line: the first sentence is what happened,
    the rest the next action."""
    m = re.match(r"^(.*?[.)])\s+([A-Z].*)$", line.strip())
    assert m, f"no next action after what happened: {line!r}"
    return fw.problems(what=m.group(1), action=m.group(2))


def _last(out: str) -> str:
    return [ln for ln in out.splitlines() if ln.strip()][-1]


def test_a_fresh_vm_fetches_everything_before_any_proxy_is_set(tmp_path):
    w = World(tmp_path)
    r = w.network()
    assert r.returncode == 0, r.stdout + r.stderr
    calls = w.calls()
    fetches = [i for i, c in enumerate(calls) if c.startswith(("docker pull", "docker build"))]
    proxy_set = next(i for i, c in enumerate(calls) if c.startswith("systemctl restart docker"))
    assert fetches and max(fetches) < proxy_set, calls
    # Only now is the VM on the company network: Docker through the proxy, apt too, the CA trusted,
    # and the egress firewall hooked in.
    s = w.settings()
    assert json.loads(s["daemon"])["proxies"]["https-proxy"] == "http://10.77.0.10:8080"
    assert "10.77.0.10:8080" in s["apt"] and s["trust"] and s["proxy_up"]
    assert "iptables-OUTPUT-ENTLANE-OUT" in s["firewall"] and "iptables-DOCKER-USER-ENTLANE-FWD" in s["firewall"]


@pytest.mark.parametrize("rebooted", [False, True], ids=["rerun", "rerun-after-a-vm-restart"])
def test_a_rerun_ends_where_the_first_run_did(tmp_path, rebooted):
    w = World(tmp_path)
    assert w.network().returncode == 0
    first = w.settings()
    w.install_pruned()          # the probe image is gone: the rerun has to pull it again
    if rebooted:
        w.reboot()              # the proxy is down, Docker's daemon still points at it
    r = w.network()
    assert r.returncode == 0, r.stdout + r.stderr
    assert "took the previous run's proxy and DNS out" in r.stdout
    assert w.settings() == first
    calls = w.calls()
    reset = next(i for i, c in enumerate(calls) if c.startswith("systemctl restart docker"))
    pull = next(i for i, c in enumerate(calls) if c.startswith("docker pull"))
    assert reset < pull, calls   # the previous run's proxy came out before the pull


def test_the_fetch_refuses_while_docker_points_at_a_proxy_that_is_down(tmp_path):
    """The bootstrap loop of 2026-11-24, met head on: the fetch stops with words, never pulls."""
    w = World(tmp_path)
    assert w.network().returncode == 0
    w.install_pruned()
    w.reboot()
    (w.fake / "calls").unlink()
    r = subprocess.run(["bash", "-c", 'source "$LANE"; fetch_infra_images'], env=w.env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 1
    assert "points at a proxy before the lane's own images are fetched" in r.stdout
    assert _usable(_last(r.stdout)) == []
    assert not any(c.startswith(("docker pull", "docker build")) for c in w.calls())


def test_the_reset_alone_takes_back_everything_a_run_set(tmp_path):
    w = World(tmp_path)
    assert w.network().returncode == 0
    (w.fake / "calls").unlink()
    cache = Path(w.env["ENTLANE_STATE"]) / "verdaccio" / "storage"
    cache.mkdir(parents=True)
    (cache / "is-number.tgz").write_text("cached")
    r = subprocess.run(["bash", "-c", 'source "$LANE"; reset_lane_network'], env=w.env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    assert w.settings() == {"daemon": None, "apt": None, "trust": False, "firewall": [],
                            "proxy_up": False}
    assert not (w.fake / "daemon-proxy").exists()
    # The mirror fetches again, as on the first run (and its cache is disk the install needs).
    assert not cache.parent.exists()


def test_every_run_starts_from_a_fresh_checkout():
    """Fetching the bundle into the old checkout added a whole-history pack per run (2.5 GB after
    21 runs): the rerun's install ran the 30 GB box out of disk."""
    lima = (REPO / "scripts" / "enterprise-lane" / "lima.sh").read_text()
    assert "rm -rf ~/boss-src" in lima and "git clone -q /tmp/boss.bundle ~/boss-src" in lima
    assert "fetch -q /tmp/boss.bundle" not in lima


def test_a_daemon_still_holding_a_proxy_with_a_clean_file_gets_one_restart(tmp_path):
    w = World(tmp_path)
    (w.fake / "daemon-proxy").write_text("http://10.77.0.10:8080\n")   # file clean, daemon not
    r = subprocess.run(["bash", "-c", 'source "$LANE"; reset_lane_network'], env=w.env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "systemctl restart docker" in w.calls()


def test_a_proxy_the_reset_cannot_take_back_stops_the_step_and_says_where(tmp_path):
    w = World(tmp_path)
    assert w.network().returncode == 0
    w.reboot()
    r = w.network(FAKE_RESTART_KEEPS_PROXY="1")
    assert r.returncode == 1
    line = _last(r.stdout)
    assert "still uses a proxy after the reset" in line and _usable(line) == []
    assert not any(c.startswith(("docker pull", "docker build", "docker run")) for c in w.calls())


def test_a_failed_pull_names_the_image_and_the_command(tmp_path):
    w = World(tmp_path)
    r = w.network(FAKE_PULL_FAIL="nginx:1.27-alpine")
    assert r.returncode == 1
    line = _last(r.stdout)
    assert "could not pull nginx:1.27-alpine before the proxy was set" in line
    assert "`docker pull nginx:1.27-alpine`" in line and _usable(line) == []
    assert w.settings()["daemon"] is None   # nothing was pointed at a proxy that never started


def test_a_container_that_does_not_start_says_where_to_look(tmp_path):
    w = World(tmp_path)
    r = w.network(FAKE_RUN_FAIL="1")
    assert r.returncode == 1
    line = _last(r.stdout)
    assert "could not start the proxy, mirrors and DNS" in line and _usable(line) == []


def test_every_bring_up_failure_line_passes_the_contract():
    src = LANE.read_text()
    samples = {"$img": "alpine:3.20", "$IMG_DNS": "entlane-dnsmasq:1", "$chain": "ENTLANE-OUT",
               "$tool": "iptables", "$hook": "OUTPUT", "$DAEMON_JSON": "/etc/docker/daemon.json"}
    found = 0
    for fn in ("reset_lane_network", "fetch_infra_images", "start_infra", "step_network"):
        body = src[src.index(f"{fn}() {{"):]
        body = body[:body.index("\n}\n")]
        for msg in re.findall(r'echo "([^"]*)"; return 1', body):
            text = msg.replace("\\`", "`")
            for var, val in samples.items():
                text = text.replace(var, val)
            found += 1
            assert _usable(text) == [], text
    assert found >= 14


def test_the_egress_proof_is_unchanged():
    """The fix moves bring-up; it never weakens what the step proves after it."""
    src = LANE.read_text()
    verify = src[src.index("verify_network() {"):]
    verify = verify[:verify.index("\n}\n")]
    for proof in ("the VM reaches the internet without the proxy",
                  "a container reaches the internet without the proxy",
                  "the proxy does not re-sign with the lane CA",
                  "docker pull through the proxy"):
        assert proof in verify, proof
    # The pull through the proxy is a real one every run, not a cached image.
    assert verify.index("docker rmi -f hello-world:latest") < verify.index("docker pull -q hello-world:latest")
    step = src[src.index("step_network() {"):]
    step = step[:step.index("\n}\n")]
    order = ["reset_lane_network", "fetch_infra_images", "start_infra", "docker_daemon_corp",
             "lockdown", "verify_network"]
    assert [step.index(f) for f in order] == sorted(step.index(f) for f in order)
