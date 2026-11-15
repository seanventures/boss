#!/usr/bin/env python3
"""boss steward — the host helper of the Infra Agent (BOSSAPP-1157; boss-infra-agent-lld.md §7.2).

BOSS's containers cannot see this machine's disk, reach Docker, or see the backups folder. This
small program runs on the host instead (launchd on macOS, a systemd timer on Linux, every 30 s)
and does exactly two things, in this order:

  1. POST one reading to BOSS:  df of /, `docker system df -v`, the size of Docker.raw (macOS),
     the backups folder, container logs.
  2. GET zero or one typed job from BOSS and run it — only if it passes every gate:
       * the Ed25519 signature checks against the key BOSS publishes (and its key id);
       * it has not expired;
       * its verb is on the allow-list below (no shell strings exist anywhere in this program);
       * its install id is THIS install's (BOSS_INSTALL_ID in .env);
       * THE LABEL GATE, enforced HERE, not in BOSS: images and volumes must carry
         `boss.install_id=<this install>`; backups must sit under BACKUP_ROOT, be named
         `boss-backup-*`, and any manifest they carry must name this install;
       * a real run must carry the plan_hash of a dry run, and the item list it would act on
         now must hash to the same value — a thing that appeared since the dry run is left alone.
     It dry-runs first when asked, acts, and POSTs the exact items and bytes back.

Pull only. No listener, no socket. Standard library only (an installed `cryptography` is used
when present; otherwise a small pure-Python Ed25519 verifier). Every run appends one line to
~/.boss/steward.log. It never prints a credential.
"""
from __future__ import annotations

import argparse
import base64
import datetime as _dt
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional

VERSION = "1"
VERBS = ("du", "prune_build_cache", "remove_images", "remove_orphan_volumes", "cap_logs",
         "docker_trim", "remove_backups")
#: Verb → the tool name BOSS's planner hashes under (storage_cleanup.plan_hash).
TOOL_OF = {"prune_build_cache": "storage.prune_build_cache", "remove_images": "storage.remove_images",
           "remove_orphan_volumes": "storage.remove_orphan_volumes", "cap_logs": "storage.cap_logs",
           "docker_trim": "storage.docker_trim", "remove_backups": "storage.remove_backups", "du": "storage.du"}
LABEL = "boss.install_id"
BACKUP_PREFIX = "boss-backup-"
#: Pinned so a `docker run` of it is never "whatever :latest is today" (LLD §7.2). Alpine 3.20.3.
TRIM_IMAGE = "alpine@sha256:beefdbd8a1da6d2915566fde36db9db0b524eb737fc57cd1367effd16dc0d06d"
DOCKER_RAW_CANDIDATES = (
    "~/Library/Containers/com.docker.docker/Data/vms/0/data/Docker.raw",
    "~/Library/Containers/com.docker.docker/Data/vms/0/Docker.raw",
)


class Refused(Exception):
    """A job this helper will not run, and why (one sentence, no secret)."""


# ── small helpers ───────────────────────────────────────────────────────────────────────────

def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def parse_iso(value: str) -> _dt.datetime:
    v = str(value).replace("Z", "+00:00")
    out = _dt.datetime.fromisoformat(v)
    return out if out.tzinfo else out.replace(tzinfo=_dt.timezone.utc)


def canonical(id_: str, host_id: str, verb: str, args: dict, expires_at: str) -> bytes:
    """Byte-for-byte what BOSS signed (services/host_jobs.canonical)."""
    return json.dumps({"id": id_, "host_id": host_id, "verb": verb, "args": dict(args),
                       "expires_at": expires_at}, sort_keys=True, separators=(",", ":"),
                      default=str).encode("utf-8")


def plan_hash(tool: str, host_id: str, args: dict, items: list[str]) -> str:
    """Byte-for-byte what BOSS's planner computes (services/storage_cleanup.plan_hash)."""
    clean = {k: v for k, v in dict(args).items() if k not in ("dry_run", "plan_hash")}
    raw = json.dumps({"tool": tool, "host_id": host_id, "args": clean, "items": sorted(str(i) for i in items)},
                     sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def gb(n: float) -> float:
    return round(float(n) / 1e9, 3)


# ── Ed25519 verify: cryptography when present, else the pure-Python reference ───────────────

def verify_signature(public_key_b64: str, payload: bytes, signature_b64: str) -> bool:
    try:
        pk = base64.b64decode(public_key_b64)
        sig = base64.b64decode(signature_b64)
    except (ValueError, TypeError):
        return False
    if len(pk) != 32 or len(sig) != 64:
        return False
    try:
        from cryptography.exceptions import InvalidSignature  # type: ignore
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey  # type: ignore

        try:
            Ed25519PublicKey.from_public_bytes(pk).verify(sig, payload)
            return True
        except InvalidSignature:
            return False
    except ImportError:
        return _ed25519_verify_pure(pk, payload, sig)


# The reference Ed25519 (D. J. Bernstein et al., public domain), verify only, with pow(·,·,q).
_Q = 2 ** 255 - 19
_L = 2 ** 252 + 27742317777372353535851937790883648493
_D = (-121665 * pow(121666, _Q - 2, _Q)) % _Q
_I = pow(2, (_Q - 1) // 4, _Q)


def _edwards_add(P: tuple, Q: tuple) -> tuple:
    x1, y1 = P
    x2, y2 = Q
    x3 = (x1 * y2 + x2 * y1) * pow(1 + _D * x1 * x2 * y1 * y2, _Q - 2, _Q)
    y3 = (y1 * y2 + x1 * x2) * pow(1 - _D * x1 * x2 * y1 * y2, _Q - 2, _Q)
    return (x3 % _Q, y3 % _Q)


def _scalarmult(P: tuple, e: int) -> tuple:
    R: tuple = (0, 1)
    while e > 0:
        if e & 1:
            R = _edwards_add(R, P)
        P = _edwards_add(P, P)
        e >>= 1
    return R


def _xrecover(y: int) -> int:
    xx = (y * y - 1) * pow(_D * y * y + 1, _Q - 2, _Q)
    x = pow(xx, (_Q + 3) // 8, _Q)
    if (x * x - xx) % _Q != 0:
        x = (x * _I) % _Q
    if x % 2 != 0:
        x = _Q - x
    return x


def _decodepoint(s: bytes) -> tuple:
    y = sum(2 ** i * ((s[i // 8] >> (i % 8)) & 1) for i in range(0, 255))
    x = _xrecover(y)
    if x & 1 != ((s[31] >> 7) & 1):
        x = _Q - x
    P = (x, y)
    if not _isoncurve(P):
        raise ValueError("decoding point that is not on curve")
    return P


def _isoncurve(P: tuple) -> bool:
    x, y = P
    return (-x * x + y * y - 1 - _D * x * x * y * y) % _Q == 0


def _decodeint(s: bytes) -> int:
    return sum(2 ** i * ((s[i // 8] >> (i % 8)) & 1) for i in range(0, 256))


def _ed25519_verify_pure(pk: bytes, m: bytes, s: bytes) -> bool:
    try:
        A = _decodepoint(pk)
        R = _decodepoint(s[:32])
    except ValueError:
        return False
    S = _decodeint(s[32:64])
    if S >= _L:
        return False
    By = 4 * pow(5, _Q - 2, _Q) % _Q
    B = (_xrecover(By), By)
    # The hash is read as a 512-bit little-endian integer (RFC 8032 §5.1.7), not 256.
    h = int.from_bytes(hashlib.sha512(s[:32] + pk + m).digest(), "little")
    return _scalarmult(B, S) == _edwards_add(R, _scalarmult(A, h))


# ── the environment ─────────────────────────────────────────────────────────────────────────

class Env:
    """Where this install lives and how to reach BOSS. The host key is read from the install's
    .env (BOSS_HOST_API_KEY) and never printed."""

    def __init__(self, install_dir: str, *, api_url: Optional[str] = None,
                 backup_root: Optional[str] = None, log_path: Optional[str] = None,
                 docker: Optional[list[str]] = None, state_dir: Optional[str] = None) -> None:
        self.install_dir = Path(install_dir).expanduser()
        self.dotenv = self._read_dotenv(self.install_dir / ".env")
        self.install_id = self._env("BOSS_INSTALL_ID", "")
        self.host_id = self._env("BOSS_HOST_ID", "") or self.install_id or platform.node()[:120]
        self.host_label = self._env("BOSS_HOST_LABEL", "") or (
            "this Mac" if platform.system() == "Darwin" else platform.node()[:80])
        self.api_url = (api_url or self._env("BOSS_STEWARD_API_URL", "") or self._api_url()).rstrip("/")
        self.host_key = self._env("BOSS_HOST_API_KEY", "")
        self.backup_root = Path(backup_root or self._env("BOSS_BACKUP_DIR", "") or "~/boss-backups").expanduser()
        self.state_dir = Path(state_dir or self._env("BOSS_STEWARD_STATE", "") or "~/.boss").expanduser()
        self.log_path = Path(log_path or self._env("BOSS_STEWARD_LOG", "") or (self.state_dir / "steward.log"))
        self.docker = docker or ["docker"]
        self.image_tag = self._env("BOSS_COMMUNITY_IMAGE_TAG", "") or self._env("COMPOSE_PROJECT_NAME", "boss-community")
        self.is_mac = platform.system() == "Darwin"

    @staticmethod
    def _read_dotenv(path: Path) -> dict[str, str]:
        out: dict[str, str] = {}
        try:
            for line in path.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
        except OSError:
            pass
        return out

    def _env(self, key: str, default: str) -> str:
        return (os.environ.get(key) or self.dotenv.get(key) or default).strip()

    def _api_url(self) -> str:
        bind = self._env("BOSS_API_BIND", "")
        port = self._env("BOSS_API_PORT", "8000") or "8000"
        if bind in ("", "127.0.0.1", "0.0.0.0"):
            return f"http://127.0.0.1:{port}"
        if ":" in bind and not bind.startswith("["):
            return f"http://[{bind}]:{port}"
        return f"http://{bind}:{port}"


def log_line(env: Env, record: dict) -> None:
    try:
        env.log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with env.log_path.open("a") as fh:
            fh.write(json.dumps({"at": now_iso(), **record}, default=str) + "\n")
    except OSError:
        pass


# ── talking to BOSS ─────────────────────────────────────────────────────────────────────────

def api(env: Env, method: str, path: str, body: Optional[dict] = None, *, timeout: float = 15.0) -> Any:
    if not env.host_key:
        raise Refused("no host key: run `boss identity host-key`")
    data = json.dumps(body, default=str).encode("utf-8") if body is not None else None
    req = urllib.request.Request(env.api_url + path, data=data, method=method,
                                 headers={"Authorization": f"Bearer {env.host_key}",
                                          "Content-Type": "application/json",
                                          "User-Agent": f"boss-steward/{VERSION}"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — the local API
        raw = resp.read()
    return json.loads(raw) if raw else {}


def public_key(env: Env, fetch: Callable[[], dict]) -> dict:
    """BOSS's signing key, cached on disk; refreshed when the job names another key id."""
    cache = env.state_dir / "steward-pubkey.json"
    try:
        cached = json.loads(cache.read_text())
    except (OSError, ValueError):
        cached = None
    if cached is None:
        cached = fetch()
        try:
            env.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            cache.write_text(json.dumps(cached))
        except OSError:
            pass
    return cached


# ── readings ────────────────────────────────────────────────────────────────────────────────

def run_cmd(argv: list[str], *, timeout: float = 120.0) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)


def _parse_size(text: str) -> float:
    """'12.3GB' / '456MB' / '7.8kB' → bytes (docker's human sizes)."""
    t = str(text).strip().replace(" ", "")
    units = {"B": 1, "kB": 1e3, "KB": 1e3, "MB": 1e6, "GB": 1e9, "TB": 1e12}
    for unit in ("TB", "GB", "MB", "kB", "KB", "B"):
        if t.endswith(unit):
            try:
                return float(t[: -len(unit)]) * units[unit]
            except ValueError:
                return 0.0
    try:
        return float(t)
    except ValueError:
        return 0.0


def docker_df(env: Env, runner: Callable = run_cmd) -> dict[str, float]:
    """images/cache/volumes/containers in bytes, and the reclaimable share, from `docker system df`."""
    out = {"images": 0.0, "cache": 0.0, "volumes": 0.0, "containers": 0.0, "reclaimable_cache": 0.0}
    try:
        res = runner([*env.docker, "system", "df", "--format", "{{json .}}"])
    except (OSError, subprocess.TimeoutExpired):
        return out
    if res.returncode != 0:
        return out
    for line in res.stdout.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        kind = str(row.get("Type") or "")
        size = _parse_size(row.get("Size") or "0")
        reclaim = _parse_size(str(row.get("Reclaimable") or "0").split(" ")[0])
        if kind.startswith("Images"):
            out["images"] = size
        elif kind.startswith("Build Cache"):
            out["cache"] = size
            out["reclaimable_cache"] = reclaim
        elif kind.startswith("Local Volumes"):
            out["volumes"] = size
        elif kind.startswith("Containers"):
            out["containers"] = size
    return out


def docker_raw(env: Env) -> tuple[Optional[float], Optional[float]]:
    """(allocated bytes on APFS, apparent size) of Docker Desktop's disk image; (None, None) elsewhere."""
    if not env.is_mac:
        return None, None
    for cand in DOCKER_RAW_CANDIDATES:
        p = Path(cand).expanduser()
        if p.exists():
            st = p.stat()
            return float(st.st_blocks * 512), float(st.st_size)
    return None, None


def walk_backups(env: Env) -> list[dict[str, Any]]:
    """Every backup under BACKUP_ROOT: {path, name, bytes, mtime, install_id|None}. Only entries
    named boss-backup-*; anything else in the folder is not ours and is never listed."""
    root = env.backup_root
    out: list[dict[str, Any]] = []
    if not root.is_dir():
        return out
    for entry in sorted(root.iterdir()):
        if not entry.name.startswith(BACKUP_PREFIX):
            continue
        try:
            if entry.is_dir():
                size = sum(f.stat().st_size for f in entry.rglob("*") if f.is_file())
            else:
                size = entry.stat().st_size
            mtime = entry.stat().st_mtime
        except OSError:
            continue
        install = None
        for manifest in ("manifest.json", ".metadata.json", "metadata.json"):
            m = entry / manifest if entry.is_dir() else None
            if m is not None and m.is_file():
                try:
                    data = json.loads(m.read_text())
                    install = str(data.get("install_id") or data.get("boss.install_id") or "") or None
                except (OSError, ValueError):
                    install = None
                break
        out.append({"path": str(entry), "name": entry.name, "bytes": float(size), "mtime": mtime,
                    "install_id": install})
    return out


def collect(env: Env, runner: Callable = run_cmd) -> dict[str, Any]:
    usage = shutil.disk_usage("/")
    df = docker_df(env, runner)
    alloc, _apparent = docker_raw(env)
    backups = walk_backups(env)
    docker_used = df["images"] + df["cache"] + df["volumes"] + df["containers"]
    sample: dict[str, Any] = {
        "host_id": env.host_id, "host_label": env.host_label, "source": "steward", "at": now_iso(),
        "total_gb": gb(usage.total), "free_gb": gb(usage.free),
        "images_gb": gb(df["images"]), "cache_gb": gb(df["cache"]), "volumes_gb": gb(df["volumes"]),
        "logs_gb": gb(df["containers"]),
        "backups_gb": gb(sum(b["bytes"] for b in backups)), "backups_n": len(backups),
        "steward_version": VERSION,
    }
    if alloc is not None:
        sample["docker_alloc_gb"] = gb(alloc)
        sample["docker_used_gb"] = gb(docker_used)
    sample["by_type"] = {"images": sample["images_gb"], "build_cache": sample["cache_gb"],
                         "volumes": sample["volumes_gb"], "logs": sample["logs_gb"],
                         "backups": sample["backups_gb"]}
    return sample


# ── the verbs (each: dry run → items, bytes; act → items, bytes) ────────────────────────────

def _labelled(env: Env, runner: Callable, kind: str, extra: list[str] = ()) -> list[dict[str, Any]]:
    """Images or volumes carrying boss.install_id=<this install> — the label gate's first half."""
    if not env.install_id:
        raise Refused("this install has no BOSS_INSTALL_ID; the label gate cannot pass anything")
    argv = [*env.docker, kind, "ls" if kind == "volume" else "images", "--filter",
            f"label={LABEL}={env.install_id}", "--format", "{{json .}}", *extra]
    if kind == "image":
        argv = [*env.docker, "images", "--filter", f"label={LABEL}={env.install_id}", "--format", "{{json .}}", *extra]
    res = runner(argv)
    rows = []
    for line in (res.stdout or "").splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def _inspect_label(env: Env, runner: Callable, kind: str, ref: str) -> str:
    res = runner([*env.docker, kind, "inspect", "--format", f'{{{{index .Labels "{LABEL}"}}}}' if kind == "volume"
                  else f'{{{{index .Config.Labels "{LABEL}"}}}}', ref])
    return (res.stdout or "").strip()


def verb_du(env: Env, args: dict, *, dry_run: bool, runner: Callable) -> tuple[list[str], float]:
    s = collect(env, runner)
    return [f"{k}={v}" for k, v in sorted(s.get("by_type", {}).items())], 0.0


def verb_prune_build_cache(env: Env, args: dict, *, dry_run: bool, runner: Callable) -> tuple[list[str], float]:
    keep = int(args.get("keep_storage_gb") or 10)
    df = docker_df(env, runner)
    over = max(0.0, df["cache"] - keep * 1e9)
    items = [f"build-cache:above-{keep}GB"] if over > 0 else []
    if dry_run or not items:
        return items, over
    res = runner([*env.docker, "builder", "prune", "-f", "--keep-storage", f"{keep}gb"], timeout=900)
    if res.returncode != 0:
        raise Refused(f"docker builder prune failed: {(res.stderr or '').strip()[:160]}")
    after = docker_df(env, runner)
    return items, max(0.0, df["cache"] - after["cache"])


def verb_remove_images(env: Env, args: dict, *, dry_run: bool, runner: Callable) -> tuple[list[str], float]:
    """Labelled images other than the current tag, the prev-<tag> rollback point and any in use."""
    in_use = set()
    ps = runner([*env.docker, "ps", "-a", "--format", "{{.Image}}"])
    for line in (ps.stdout or "").splitlines():
        in_use.add(line.strip())
    keep_tags = {env.image_tag, f"prev-{env.image_tag}"}
    items, size = [], 0.0
    for row in _labelled(env, runner, "image"):
        repo, tag = str(row.get("Repository") or ""), str(row.get("Tag") or "")
        ref = f"{repo}:{tag}" if tag and tag != "<none>" else str(row.get("ID") or "")
        if tag in keep_tags or tag.startswith("prev-") or ref in in_use or f"{repo}:{tag}" in in_use:
            continue
        if not ref:
            continue
        items.append(ref)
        size += _parse_size(row.get("Size") or "0")
    if dry_run:
        return items, size
    done, freed = [], 0.0
    for ref in items:
        # THE LABEL GATE, AGAIN, per item at the moment of removal.
        if _inspect_label(env, runner, "image", ref) != env.install_id:
            continue
        res = runner([*env.docker, "rmi", ref], timeout=300)
        if res.returncode == 0:
            done.append(ref)
    after = docker_df(env, runner)
    freed = size if done else 0.0
    return done, freed if after else freed


def verb_remove_orphan_volumes(env: Env, args: dict, *, dry_run: bool, runner: Callable) -> tuple[list[str], float]:
    older_h = float(args.get("older_than_hours") or 24)
    cutoff = time.time() - older_h * 3600
    items, size = [], 0.0
    for row in _labelled(env, runner, "volume", ["--filter", "dangling=true"]):
        name = str(row.get("Name") or "")
        if not name:
            continue
        ins = runner([*env.docker, "volume", "inspect", "--format", "{{.CreatedAt}}", name])
        created = (ins.stdout or "").strip()
        try:
            if created and parse_iso(created.replace("Z", "+00:00")[:25]).timestamp() > cutoff:
                continue
        except ValueError:
            pass
        items.append(name)
        size += _parse_size(str(row.get("Size") or "0"))
    if dry_run:
        return items, size
    done = []
    for name in items:
        if _inspect_label(env, runner, "volume", name) != env.install_id:
            continue
        res = runner([*env.docker, "volume", "rm", name], timeout=120)
        if res.returncode == 0:
            done.append(name)
    return done, size if done else 0.0


def verb_cap_logs(env: Env, args: dict, *, dry_run: bool, runner: Callable) -> tuple[list[str], float]:
    """Truncate json logs above the cap for labelled containers (Linux, when the file is ours to
    write). On macOS the files live inside Docker Desktop's VM: nothing to do here (the compose
    `logging:` cap is the Layer 1 fix)."""
    if env.is_mac:
        return [], 0.0
    cap = int(args.get("max_size_mb") or 50) * 1_000_000
    ps = runner([*env.docker, "ps", "-a", "--filter", f"label={LABEL}={env.install_id}", "--format", "{{.ID}}"])
    items, size = [], 0.0
    paths: dict[str, Path] = {}
    for cid in (ps.stdout or "").split():
        ins = runner([*env.docker, "inspect", "--format", "{{.LogPath}}", cid])
        p = Path((ins.stdout or "").strip())
        try:
            if p.is_file() and p.stat().st_size > cap:
                items.append(cid)
                size += p.stat().st_size - cap
                paths[cid] = p
        except OSError:
            continue
    if dry_run:
        return items, size
    done, freed = [], 0.0
    for cid in items:
        p = paths[cid]
        try:
            before = p.stat().st_size
            with p.open("r+b") as fh:
                fh.truncate(0)
            done.append(cid)
            freed += before
        except OSError:
            continue
    return done, freed


def verb_docker_trim(env: Env, args: dict, *, dry_run: bool, runner: Callable) -> tuple[list[str], float]:
    """Docker Desktop: fstrim inside the VM so Docker.raw hands blocks back to APFS. Linux: no-op."""
    if not env.is_mac:
        return [], 0.0
    alloc, _ = docker_raw(env)
    if alloc is None:
        return [], 0.0
    df = docker_df(env, runner)
    used = df["images"] + df["cache"] + df["volumes"] + df["containers"]
    reclaim = max(0.0, alloc - used)
    items = ["Docker.raw"] if reclaim > 0 else []
    if dry_run or not items:
        return items, reclaim
    res = runner([*env.docker, "run", "--rm", "--privileged", "--pid=host", TRIM_IMAGE,
                  "nsenter", "-t", "1", "-m", "-u", "-n", "-i", "fstrim", "/var/lib/docker"], timeout=900)
    if res.returncode != 0:
        raise Refused(f"fstrim inside the Docker VM failed: {(res.stderr or '').strip()[:160]}")
    time.sleep(2)
    after, _ = docker_raw(env)
    return items, max(0.0, alloc - (after or alloc))


def select_backups(backups: list[dict[str, Any]], *, keep_newest: int, keep_days: float, cap_gb: float,
                   install_id: str, root: Path, now: Optional[float] = None) -> list[dict[str, Any]]:
    """The label gate for backups + the retention rule, pure: under `root`, named boss-backup-*,
    a manifest (if any) naming THIS install; keep the newest N and anything younger than
    keep_days; then the oldest go until the rest fit under cap_gb."""
    now = now or time.time()
    root_real = root.resolve()
    ours = []
    for b in backups:
        p = Path(b["path"])
        try:
            if root_real not in p.resolve().parents:
                continue
        except OSError:
            continue
        if not p.name.startswith(BACKUP_PREFIX):
            continue
        if b.get("install_id") and install_id and b["install_id"] != install_id:
            continue
        ours.append(b)
    ours.sort(key=lambda b: b["mtime"], reverse=True)       # newest first
    protected = set(b["path"] for b in ours[:max(0, int(keep_newest))])
    protected |= set(b["path"] for b in ours if now - b["mtime"] < float(keep_days) * 86400)
    candidates = [b for b in ours if b["path"] not in protected]      # oldest at the end
    total = sum(b["bytes"] for b in ours)
    to_delete: list[dict[str, Any]] = []
    # Beyond keep-N / keep-days: every candidate is past the rule. Within the cap on top: the
    # oldest first until the kept set fits.
    cap = float(cap_gb) * 1e9
    for b in reversed(candidates):                           # oldest first
        if total <= cap and len(to_delete) >= 0 and cap > 0 and total - sum(x["bytes"] for x in to_delete) <= cap:
            # Under the cap already: still remove what the keep rules do not protect.
            pass
        to_delete.append(b)
    return to_delete


def verb_remove_backups(env: Env, args: dict, *, dry_run: bool, runner: Callable) -> tuple[list[str], float]:
    backups = walk_backups(env)
    chosen = select_backups(backups, keep_newest=int(args.get("keep_newest") or 10),
                            keep_days=float(args.get("keep_days") or 7), cap_gb=float(args.get("cap_gb") or 20),
                            install_id=env.install_id, root=env.backup_root)
    items = [b["name"] for b in chosen]
    size = sum(b["bytes"] for b in chosen)
    if dry_run:
        return items, size
    done, freed = [], 0.0
    root_real = env.backup_root.resolve()
    for b in chosen:
        p = Path(b["path"])
        if root_real not in p.resolve().parents or not p.name.startswith(BACKUP_PREFIX):
            continue  # the gate, once more, at the moment of deletion
        try:
            if p.is_dir() and not p.is_symlink():
                shutil.rmtree(p)
            else:
                p.unlink()
            done.append(b["name"])
            freed += b["bytes"]
        except OSError:
            continue
    return done, freed


VERB_FN: dict[str, Callable[..., tuple[list[str], float]]] = {
    "du": verb_du, "prune_build_cache": verb_prune_build_cache, "remove_images": verb_remove_images,
    "remove_orphan_volumes": verb_remove_orphan_volumes, "cap_logs": verb_cap_logs,
    "docker_trim": verb_docker_trim, "remove_backups": verb_remove_backups,
}


# ── the gates, then the job ─────────────────────────────────────────────────────────────────

def check_job(env: Env, job: dict, pubkey: dict, *, now: Optional[_dt.datetime] = None) -> dict:
    """Every gate but the label gate (which each verb applies to its own items). Returns the
    typed arguments; raises Refused."""
    for k in ("id", "host_id", "verb", "args", "expires_at", "signature"):
        if k not in job:
            raise Refused(f"job is missing {k}")
    if job.get("key_id") and pubkey.get("key_id") and job["key_id"] != pubkey["key_id"]:
        raise Refused("job signed by a key this helper does not know")
    payload = canonical(str(job["id"]), str(job["host_id"]), str(job["verb"]), dict(job["args"] or {}),
                        str(job["expires_at"]))
    if not verify_signature(str(pubkey.get("public_key") or ""), payload, str(job["signature"])):
        raise Refused("signature does not verify")
    now = now or _dt.datetime.now(_dt.timezone.utc)
    if parse_iso(str(job["expires_at"])) <= now:
        raise Refused("job expired")
    verb = str(job["verb"])
    if verb not in VERBS:
        raise Refused(f"verb {verb!r} is not allowed")
    if str(job["host_id"]) != env.host_id:
        raise Refused("job is for another host")
    args = dict(job["args"] or {})
    if verb != "du" and str(args.get("install_id") or "") != env.install_id:
        raise Refused("job names another install")
    if not bool(args.get("dry_run", False)) and verb != "du" and not args.get("plan_hash"):
        raise Refused("a real run needs the dry run's plan_hash")
    return args


def perform(env: Env, job: dict, pubkey: dict, *, runner: Callable = run_cmd,
            now: Optional[_dt.datetime] = None) -> dict:
    """Gate, dry-run or act, and the result BOSS records. Never raises: a refusal is a result."""
    try:
        args = check_job(env, job, pubkey, now=now)
        verb = str(job["verb"])
        dry = bool(args.get("dry_run", False))
        fn = VERB_FN[verb]
        if dry or verb == "du":
            items, size = fn(env, args, dry_run=True, runner=runner)
            return {"ok": True, "dry_run": True, "items": items, "bytes": int(size),
                    "plan_hash": plan_hash(TOOL_OF[verb], env.host_id, args, items)}
        # A real run: what would go NOW must be exactly what the dry run showed.
        items_now, _ = fn(env, args, dry_run=True, runner=runner)
        expected = plan_hash(TOOL_OF[verb], env.host_id, args, items_now)
        if expected != str(args.get("plan_hash")):
            raise Refused("the items changed since the dry run; plan again")
        items, size = fn(env, args, dry_run=False, runner=runner)
        return {"ok": True, "dry_run": False, "items": items, "bytes": int(size),
                "plan_hash": str(args.get("plan_hash"))}
    except Refused as exc:
        return {"ok": False, "dry_run": bool((job.get("args") or {}).get("dry_run", False)), "items": [],
                "bytes": 0, "error": str(exc), "plan_hash": str((job.get("args") or {}).get("plan_hash") or "")}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "dry_run": bool((job.get("args") or {}).get("dry_run", False)), "items": [],
                "bytes": 0, "error": f"{type(exc).__name__}: {exc}"[:300],
                "plan_hash": str((job.get("args") or {}).get("plan_hash") or "")}


def once(env: Env, *, runner: Callable = run_cmd, sample_only: bool = False) -> dict:
    report: dict[str, Any] = {"host_id": env.host_id}
    try:
        sample = collect(env, runner)
        got = api(env, "POST", "/v1/infra/storage/samples", sample)
        report["sample"] = {"free_gb": sample["free_gb"], "level": got.get("level"),
                            "incident_id": got.get("incident_id")}
    except (urllib.error.URLError, Refused, OSError, ValueError) as exc:
        report["sample_error"] = f"{type(exc).__name__}: {exc}"[:200]
        log_line(env, report)
        return report
    if sample_only:
        log_line(env, report)
        return report
    try:
        leased = api(env, "GET", f"/v1/host-jobs/lease?host_id={urllib.request.quote(env.host_id)}&leased_to=steward")
        job = (leased or {}).get("job")
        if not job:
            report["job"] = None
            log_line(env, report)
            return report
        pubkey = public_key(env, lambda: api(env, "GET", "/v1/host-jobs/public-key"))
        if job.get("key_id") and pubkey.get("key_id") != job.get("key_id"):
            pubkey = api(env, "GET", "/v1/host-jobs/public-key")     # rotated: refresh once
            try:
                (env.state_dir / "steward-pubkey.json").write_text(json.dumps(pubkey))
            except OSError:
                pass
        result = perform(env, job, pubkey, runner=runner)
        api(env, "POST", f"/v1/host-jobs/{job['id']}/result", result)
        report["job"] = {"id": job["id"], "verb": job["verb"], "ok": result["ok"], "dry_run": result["dry_run"],
                         "items": len(result["items"]), "bytes": result["bytes"], "error": result.get("error")}
    except (urllib.error.URLError, Refused, OSError, ValueError) as exc:
        report["job_error"] = f"{type(exc).__name__}: {exc}"[:200]
    log_line(env, report)
    return report


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="boss steward: the Infra Agent's host helper (pull-only).")
    ap.add_argument("--install-dir", default=os.environ.get("BOSS_INSTALL_DIR") or os.path.expanduser("~/boss"))
    ap.add_argument("--api-url", default=None)
    ap.add_argument("--backup-dir", default=None)
    ap.add_argument("--sample-only", action="store_true", help="post the reading; take no job")
    ap.add_argument("--print", action="store_true", help="print the reading and exit (no network)")
    ap.add_argument("--version", action="store_true")
    ns = ap.parse_args(argv)
    if ns.version:
        print(VERSION)
        return 0
    env = Env(ns.install_dir, api_url=ns.api_url, backup_root=ns.backup_dir)
    if ns.print:
        print(json.dumps(collect(env), indent=2, default=str))
        return 0
    report = once(env, sample_only=ns.sample_only)
    print(json.dumps(report, default=str))
    return 0 if not report.get("sample_error") else 1


if __name__ == "__main__":
    sys.exit(main())
