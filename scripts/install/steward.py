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
import contextlib
import base64
import datetime as _dt
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import runpy
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
#: What a labelled volume holds. Only a REBUILDABLE kind is removed by a job: a test database a
#: run recreates, a cache, a scratch area, a build workspace. A volume of ours with any other
#: kind — `data`, `preview`, or no kind at all — may be the only copy of something (a stopped
#: install's Postgres, a preview app's database a reviewer typed into): irreversible, never
#: removed by the helper (ST-14). `preview` is NOT rebuildable (BOSSAPP-1193): a preview runs the
#: app's own Compose, and its database holds whatever a person entered while reviewing it.
KIND_LABEL = "boss.kind"
REBUILDABLE_VOLUME_KINDS = ("test", "cache", "scratch", "build")
#: The rebuildable volumes of BOSS's own Compose project (scripts/install/community-compose.yml
#: `volumes:` key → boss.kind), labelled from creation. Data volumes are deliberately not here and
#: carry no boss.* labels (see the compose file). A guard test keeps this equal to Compose.
REBUILDABLE_COMPOSE_VOLUMES = {"boss_community_pg_test_v2": "test", "boss_community_toolchain_cache_v2": "cache"}
#: Rebuildable keys of earlier releases, replaced by the labelled `_v2` keys above. Docker cannot
#: add a label to an existing volume, and Compose asks "Recreate (data will be lost)?" when the
#: labels of one change, so the labels came with a NEW key instead: the services move to it on
#: their next `up` and the old volume is left unmounted and unlabelled. `--relabel-volumes`
#: removes it once nothing mounts it; until then the helper reports it as NEEDS_RELABEL instead
#: of passing over it in silence. It never stops a service to get there.
RETIRED_COMPOSE_VOLUMES = {"boss_community_pg_test": "test", "boss_community_toolchain_cache": "cache"}
#: Stopped containers `--relabel-volumes` may remove to free a rebuildable volume: Compose
#: recreates them on the next `up` (`boss dev up` for db-test). Any other user keeps the volume.
RELABEL_REMOVABLE_SERVICES = ("db-test",)
COMPOSE_PROJECT_LABEL = "com.docker.compose.project"
COMPOSE_VOLUME_LABEL = "com.docker.compose.volume"
NEEDS_RELABEL = "unlabelled BOSS volume — needs relabel"
#: What backup.sh names a backup (`boss-<UTC stamp>-<pid>`, backup_archive.BACKUP_NAME). A
#: directory is a candidate ONLY when its manifest.json is complete and names THIS install.
BACKUP_NAME_PREFIX = "boss-"
#: The cap wins over keep-N / keep-days, but never below this many newest backups
#: (backup_archive.CAP_FLOOR): a rollback to the release before last stays possible.
CAP_FLOOR = 3
#: The orphan-volume job's default age (storage_cleanup asks for 24 h); the reading uses it too,
#: so the reclaimable figure is what a job would actually remove.
ORPHAN_AGE_HOURS = 24.0
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
        docker_context = self._env("BOSS_DOCKER_CONTEXT", "")
        self.docker = docker or (["docker", "--context", docker_context] if docker_context else ["docker"])
        self.image_tag = self._env("BOSS_COMMUNITY_IMAGE_TAG", "") or self._env("COMPOSE_PROJECT_NAME", "boss-community")
        self.compose_project = self._env("COMPOSE_PROJECT_NAME", "boss-community")
        #: Build cache is Docker-wide. It is pruned only through a BOSS-owned buildx builder
        #: (BOSS_BUILDX_BUILDER) or on a host that runs nothing but BOSS (BOSS_DEDICATED_HOST=1).
        self.buildx_builder = self._env("BOSS_BUILDX_BUILDER", "")
        self.dedicated = self._env("BOSS_DEDICATED_HOST", "0").lower() in ("1", "true", "yes")
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


PIN_FILE = "steward-pubkey.json"


def pinned_key(env: Env) -> Optional[dict]:
    try:
        data = json.loads((env.state_dir / PIN_FILE).read_text())
        return data if isinstance(data, dict) and data.get("public_key") else None
    except (OSError, ValueError):
        return None


def pin_key(env: Env, key: dict) -> None:
    env.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = env.state_dir / PIN_FILE
    path.write_text(json.dumps({"key_id": key.get("key_id"), "algorithm": key.get("algorithm"),
                                "public_key": key.get("public_key"), "pinned_at": now_iso()}))
    path.chmod(0o600)


def unpin_key(env: Env) -> bool:
    """`boss steward rekey`: forget the pin; the next run pins whatever BOSS publishes (TOFU again)."""
    try:
        (env.state_dir / PIN_FILE).unlink()
        return True
    except OSError:
        return False


def public_key(env: Env, fetch: Callable[[], dict]) -> dict:
    """TRUST ON FIRST USE. The first run pins the key BOSS publishes; from then on the pin is the
    truth. A job (or a published key) under another key id is REFUSED, never re-fetched: a
    rotated or impersonated API cannot talk the helper into a new key. The operator confirms a
    real rotation with `boss steward rekey`."""
    pinned = pinned_key(env)
    if pinned is not None:
        return pinned
    fetched = fetch()
    pin_key(env, fetched)
    return pinned_key(env) or fetched


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


def colima_profile(env: Env) -> Optional[str]:
    import re

    if "--context" not in env.docker:
        return None
    context = env.docker[env.docker.index("--context") + 1]
    if context == "colima":
        return "default"
    if context.startswith("colima-") and re.fullmatch(r"[a-zA-Z0-9_-]+", context[7:]):
        return context[7:]
    return None


def docker_raw(env: Env) -> tuple[Optional[float], Optional[float]]:
    """Allocated and apparent disk bytes for the selected local macOS container runtime."""
    if not env.is_mac:
        return None, None
    profile = colima_profile(env)
    if profile:
        name = "colima" if profile == "default" else f"colima-{profile}"
        root = Path(env._env("COLIMA_HOME", "~/.colima")).expanduser() / "_lima"
        paths = [root / name / "diffdisk", root / name / "disk",
                 root / "_disks" / name / "datadisk"]
        disks = {path.resolve() for path in paths if path.is_file()}
        if not disks:
            return None, None
        return (float(sum(path.stat().st_blocks * 512 for path in disks)),
                float(sum(path.stat().st_size for path in disks)))
    for cand in DOCKER_RAW_CANDIDATES:
        p = Path(cand).expanduser()
        if p.exists():
            st = p.stat()
            return float(st.st_blocks * 512), float(st.st_size)
    return None, None


def backup_manifest(entry: Path) -> Optional[dict[str, Any]]:
    """The backup's manifest.json when it is a complete BOSS backup, else None."""
    m = entry / "manifest.json"
    if not entry.is_dir() or entry.is_symlink() or not m.is_file():
        return None
    try:
        data = json.loads(m.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("complete") is not True:
        return None
    return data


def walk_backups(env: Env) -> list[dict[str, Any]]:
    """THE BACKUP LABEL GATE (LLD §7.2). Only a directory under BACKUP_ROOT that is named
    boss-*, is not a symlink, and carries a complete manifest.json whose install_id is THIS
    install's is a backup of ours. Anything else in the folder — an operator's own copy, a
    folder named boss-backup-x, another install's backup — is never listed, so never sampled,
    never a candidate, never deleted."""
    root = env.backup_root
    out: list[dict[str, Any]] = []
    if not root.is_dir() or not env.install_id:
        return out
    for entry in sorted(root.iterdir()):
        if not entry.name.startswith(BACKUP_NAME_PREFIX):
            continue
        manifest = backup_manifest(entry)
        if manifest is None or str(manifest.get("install_id") or "") != env.install_id:
            continue
        try:
            size = sum(f.stat().st_size for f in entry.rglob("*") if f.is_file() and not f.is_symlink())
            mtime = entry.stat().st_mtime
        except OSError:
            continue
        out.append({"path": str(entry), "name": entry.name, "bytes": float(size), "mtime": mtime,
                    "install_id": env.install_id})
    return out


def collect(env: Env, runner: Callable = run_cmd) -> dict[str, Any]:
    usage = shutil.disk_usage("/")
    df = docker_df(env, runner)
    alloc, _apparent = docker_raw(env)
    backups = walk_backups(env)
    docker_used = df["images"] + df["cache"] + df["volumes"] + df["containers"]
    reclaimable, relabel = _volume_reading(env, runner)
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
                         "backups": sample["backups_gb"],
                         # What the orphan-volume job could really free (measured, ours, rebuildable,
                         # dangling, past the 24 h age), and how many BOSS volumes it cannot judge
                         # until they are relabelled. `volumes` above is every volume on the disk.
                         "volumes_reclaimable": gb(reclaimable),
                         "volumes_needs_relabel_n": float(len(relabel))}
    # The budget prices the layers of this release and its rollback set. The whole
    # daemon footprint above is still used by host pressure and cleanup decisions.
    budget_module = Path(__file__).resolve().parents[2] / "engine/boss_engine/services/disk_budget.py"
    if budget_module.is_file():
        budget = runpy.run_path(str(budget_module))
        for key, value in budget["measure_docker"](env.docker, project=env.compose_project,
                                                   tag=env.image_tag, run=runner).items():
            if value is not None:
                sample["by_type"][f"budget_{key}"] = value
        age = budget["rollback_age_hours"](env.install_dir)
        if age is not None:
            sample["by_type"]["rollback_age_hours"] = age
            sample["by_type"]["rollback_keep_hours"] = budget["rollback_keep_hours"](env.dotenv)
    return sample


def _volume_reading(env: Env, runner: Callable) -> tuple[float, list[str]]:
    """(measured bytes the orphan-volume job could free now, BOSS volumes needing a relabel).
    Sizes are measured only when there is a candidate: `docker system df -v` walks every volume."""
    if not env.install_id:
        return 0.0, []
    try:
        names = _orphan_volumes(env, runner, ORPHAN_AGE_HOURS)
        sizes = volume_sizes(env, runner) if names else {}
        return sum(sizes.get(n, 0.0) for n in names), unlabelled_boss_volumes(env, runner)
    except (Refused, OSError, subprocess.TimeoutExpired):
        return 0.0, []


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


def _inspect_label(env: Env, runner: Callable, kind: str, ref: str, label: str = LABEL) -> str:
    res = runner([*env.docker, kind, "inspect", "--format", f'{{{{index .Labels "{label}"}}}}' if kind == "volume"
                  else f'{{{{index .Config.Labels "{label}"}}}}', ref])
    return (res.stdout or "").strip()


def _rebuildable_volume(env: Env, runner: Callable, name: str) -> bool:
    """Ours AND of a rebuildable kind, read from the volume itself at the moment it is asked."""
    return (bool(env.install_id) and _inspect_label(env, runner, "volume", name) == env.install_id
            and _inspect_label(env, runner, "volume", name, KIND_LABEL) in REBUILDABLE_VOLUME_KINDS)


def _labels_of(row: dict[str, Any]) -> dict[str, str]:
    """`docker volume ls --format {{json .}}` prints Labels as "k=v,k=v"."""
    raw = row.get("Labels")
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items()}
    out: dict[str, str] = {}
    for part in str(raw or "").split(","):
        k, sep, v = part.partition("=")
        if sep and k.strip():
            out[k.strip()] = v.strip()
    return out


def volume_sizes(env: Env, runner: Callable) -> dict[str, float]:
    """Each local volume's size in bytes, from `docker system df -v` (`docker volume ls` prints
    none). {} when Docker cannot say: then nothing is claimed as freed."""
    try:
        res = runner([*env.docker, "system", "df", "-v", "--format", "{{json .}}"], timeout=600)
    except (OSError, subprocess.TimeoutExpired):
        return {}
    if res.returncode != 0:
        return {}
    out: dict[str, float] = {}
    for line in (res.stdout or "").splitlines():
        try:
            data = json.loads(line)
        except ValueError:
            continue
        rows = data.get("Volumes") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            continue
        for row in rows:
            if isinstance(row, dict) and row.get("Name"):
                out[str(row["Name"])] = _parse_size(str(row.get("Size") or "0"))
    return out


def unlabelled_boss_volumes(env: Env, runner: Callable) -> list[str]:
    """Rebuildable volumes of BOSS's own Compose project the label gate cannot pass: a retired
    pre-label key, or a current one missing this install's id or its kind. Never removed by a
    job — reported, and removed by `--relabel-volumes` once nothing mounts it."""
    res = runner([*env.docker, "volume", "ls", "--filter", f"label={COMPOSE_PROJECT_LABEL}={env.compose_project}",
                  "--format", "{{json .}}"])
    out = []
    for line in (res.stdout or "").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        name = str(row.get("Name") or "")
        labels = _labels_of(row)
        if not name or labels.get(COMPOSE_PROJECT_LABEL, env.compose_project) != env.compose_project:
            continue
        key = labels.get(COMPOSE_VOLUME_LABEL, "")
        want = REBUILDABLE_COMPOSE_VOLUMES.get(key)
        if key in RETIRED_COMPOSE_VOLUMES or (want and (not env.install_id or labels.get(LABEL) != env.install_id
                                                        or labels.get(KIND_LABEL) != want)):
            out.append(name)
    return sorted(out)


def _orphan_volumes(env: Env, runner: Callable, older_h: float) -> list[str]:
    """Dangling volumes of THIS install, of a rebuildable kind, older than `older_h` hours."""
    cutoff = time.time() - older_h * 3600
    names = []
    for row in _labelled(env, runner, "volume", ["--filter", "dangling=true"]):
        name = str(row.get("Name") or "")
        if not name or not _rebuildable_volume(env, runner, name):
            continue  # a data volume (or one with no kind) is never the helper's to remove
        ins = runner([*env.docker, "volume", "inspect", "--format", "{{.CreatedAt}}", name])
        created = (ins.stdout or "").strip()
        try:
            if created and parse_iso(created.replace("Z", "+00:00")[:25]).timestamp() > cutoff:
                continue
        except ValueError:
            pass
        names.append(name)
    return names


def verb_du(env: Env, args: dict, *, dry_run: bool, runner: Callable) -> tuple[list[str], float]:
    s = collect(env, runner)
    return [f"{k}={v}" for k, v in sorted(s.get("by_type", {}).items())], 0.0


def verb_prune_build_cache(env: Env, args: dict, *, dry_run: bool, runner: Callable) -> tuple[list[str], float]:
    keep = int(args.get("keep_storage_gb") or 10)
    if not env.buildx_builder and not env.dedicated:
        raise Refused("the build cache is Docker-wide; on a shared machine BOSS prunes only its own "
                      "buildx builder (set BOSS_BUILDX_BUILDER) or a dedicated host (BOSS_DEDICATED_HOST=1)")
    df = docker_df(env, runner)
    over = max(0.0, df["cache"] - keep * 1e9)
    scope = f"builder:{env.buildx_builder}" if env.buildx_builder else "dedicated-host"
    items = [f"build-cache:{scope}:above-{keep}GB"] if over > 0 else []
    if dry_run or not items:
        return items, over
    if env.buildx_builder:
        argv = [*env.docker, "buildx", "prune", "--builder", env.buildx_builder, "-f", "--keep-storage", f"{keep}gb"]
    else:
        argv = [*env.docker, "builder", "prune", "-f", "--keep-storage", f"{keep}gb"]
    res = runner(argv, timeout=900)
    if res.returncode != 0:
        raise Refused(f"docker builder prune failed: {(res.stderr or '').strip()[:160]}")
    after = docker_df(env, runner)
    return items, max(0.0, df["cache"] - after["cache"])


#: How long `boss rollback`'s images stay after a healthy upgrade (prune.sh, disk_budget; BOSSAPP-1459).
ROLLBACK_KEEP_HOURS = 72.0


def rollback_expired(env: Env, runner: Callable, now: Optional[_dt.datetime] = None) -> bool:
    """The prev-<tag> rollback point is past its window: .boss-rollback was written more than
    BOSS_ROLLBACK_KEEP_HOURS ago, was not already dropped, and the API is healthy now (an
    unhealthy release keeps its way back)."""
    record: dict[str, str] = {}
    try:
        for line in (env.install_dir / ".boss-rollback").read_text().splitlines():
            k, sep, v = line.partition("=")
            if sep and not k.startswith("#"):
                record[k.strip()] = v.strip()
    except OSError:
        return False
    if record.get("BOSS_PREV_EXPIRED_AT") or record.get("BOSS_PREV_IMAGE_TAG") != f"prev-{env.image_tag}":
        return False
    try:
        keep = float(env._env("BOSS_ROLLBACK_KEEP_HOURS", "") or ROLLBACK_KEEP_HOURS)
        saved = parse_iso(record.get("BOSS_PREV_SAVED_AT", ""))
    except ValueError:
        return False
    age_h = ((now or _dt.datetime.now(_dt.timezone.utc)) - saved).total_seconds() / 3600
    if age_h < max(1.0, keep):
        return False
    res = runner([*env.docker, "ps", "-q", "--filter", f"label={COMPOSE_PROJECT_LABEL}={env.compose_project}",
                  "--filter", "label=com.docker.compose.service=api", "--filter", "health=healthy"])
    return bool((res.stdout or "").strip())


def _mark_rollback_expired(env: Env) -> None:
    with contextlib.suppress(OSError):
        with open(env.install_dir / ".boss-rollback", "a", encoding="utf-8") as fh:
            fh.write(f"BOSS_PREV_EXPIRED_AT={_dt.datetime.now(_dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}\n")


def verb_remove_images(env: Env, args: dict, *, dry_run: bool, runner: Callable) -> tuple[list[str], float]:
    """Labelled images other than the current tag, the prev-<tag> rollback point and any in use.
    The rollback point too once it is past its window (`rollback_expired`)."""
    in_use = set()
    ps = runner([*env.docker, "ps", "-a", "--format", "{{.Image}}"])
    for line in (ps.stdout or "").splitlines():
        in_use.add(line.strip())
    prev = f"prev-{env.image_tag}"
    expired = rollback_expired(env, runner)
    keep_tags = {env.image_tag} | (set() if expired else {prev})
    items, size = [], 0.0
    for row in _labelled(env, runner, "image"):
        repo, tag = str(row.get("Repository") or ""), str(row.get("Tag") or "")
        ref = f"{repo}:{tag}" if tag and tag != "<none>" else str(row.get("ID") or "")
        if tag in keep_tags or (tag.startswith("prev-") and not (expired and tag == prev)) \
                or ref in in_use or f"{repo}:{tag}" in in_use:
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
    if expired and any(r.endswith(f":{prev}") for r in done):
        still = [r for r in items if r.endswith(f":{prev}") and runner(
            [*env.docker, "image", "inspect", r]).returncode == 0]
        if not still:            # only when every kept image is really gone
            _mark_rollback_expired(env)
    after = docker_df(env, runner)
    freed = size if done else 0.0
    return done, freed if after else freed


def verb_remove_orphan_volumes(env: Env, args: dict, *, dry_run: bool, runner: Callable) -> tuple[list[str], float]:
    items = _orphan_volumes(env, runner, float(args.get("older_than_hours") or ORPHAN_AGE_HOURS))
    # Measured, before anything goes: `docker volume ls` has no size, so the dry run's bytes and
    # the freed bytes come from `docker system df -v` (BOSSAPP-1193; it used to report 0).
    sizes = volume_sizes(env, runner) if items else {}
    if dry_run:
        return items, sum(sizes.get(n, 0.0) for n in items)
    done, freed = [], 0.0
    for name in items:
        if not _rebuildable_volume(env, runner, name):
            continue  # THE GATE, AGAIN, per volume at the moment of removal
        res = runner([*env.docker, "volume", "rm", name], timeout=120)
        if res.returncode == 0:
            done.append(name)
            freed += sizes.get(name, 0.0)
    return done, freed


def _containers_using(env: Env, runner: Callable, volume: str) -> list[dict[str, str]]:
    res = runner([*env.docker, "ps", "-a", "--filter", f"volume={volume}", "--format", "{{json .}}"])
    rows = []
    for line in (res.stdout or "").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        labels = _labels_of(row)
        rows.append({"id": str(row.get("ID") or ""), "name": str(row.get("Names") or ""),
                     "state": str(row.get("State") or "").lower(),
                     "project": labels.get(COMPOSE_PROJECT_LABEL, ""),
                     "service": labels.get("com.docker.compose.service", "")})
    return rows


def _relabel_words(key: str, *, done: bool) -> str:
    if key in RETIRED_COMPOSE_VOLUMES:
        return ("removed: retired; its labelled successor is in use" if done
                else "retired (its labelled successor replaces it): would be removed")
    kind = REBUILDABLE_COMPOSE_VOLUMES.get(key, "")
    return (f"removed; the next compose up recreates it with boss.kind={kind}" if done
            else f"would be removed; the next compose up recreates it with boss.kind={kind}")


def relabel_volumes(env: Env, runner: Callable = run_cmd, *, apply: bool = True) -> list[dict[str, str]]:
    """THE UPGRADE STEP for labels (BOSSAPP-1193; upgrade.sh and `boss dev up` run it, after the
    services have moved to the labelled `_v2` volumes). Docker cannot relabel a volume, so a
    REBUILDABLE BOSS volume the gate cannot pass — a retired pre-label key, or a current key made
    without this install's id — is removed when nothing uses it (a current one comes back,
    labelled, on the next `up`). Only keys in RETIRED_COMPOSE_VOLUMES / REBUILDABLE_COMPOSE_VOLUMES
    of this install's Compose project qualify; a data volume is never one of them. A volume a
    running container uses, or that a container outside BOSS (or a BOSS service Compose would not
    recreate) holds, is left as it is and reported NEEDS_RELABEL. Never stops a service; never
    raises."""
    out: list[dict[str, str]] = []
    if not env.install_id:
        return [{"volume": "*", "action": "skipped",
                 "reason": "no BOSS_INSTALL_ID in .env: a recreated volume would be labelled with nothing"}]
    try:
        names = unlabelled_boss_volumes(env, runner)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [{"volume": "*", "action": "skipped", "reason": f"docker unavailable: {type(exc).__name__}"}]
    for name in names:
        try:
            users = _containers_using(env, runner, name)
            running = [u["name"] or u["id"] for u in users if u["state"] not in ("exited", "created", "dead")]
            foreign = [u["name"] or u["id"] for u in users
                       if u["project"] != env.compose_project or u["service"] not in RELABEL_REMOVABLE_SERVICES]
            if running or foreign:
                why = f"in use by {', '.join(running)}" if running else f"held by {', '.join(foreign)}"
                out.append({"volume": name, "action": "skipped", "reason": f"{NEEDS_RELABEL} ({why})"})
                continue
            # THE GATE, AGAIN, at the moment of removal: still BOSS's, still a rebuildable key.
            key = _inspect_label(env, runner, "volume", name, COMPOSE_VOLUME_LABEL)
            if (_inspect_label(env, runner, "volume", name, COMPOSE_PROJECT_LABEL) != env.compose_project
                    or key not in {**REBUILDABLE_COMPOSE_VOLUMES, **RETIRED_COMPOSE_VOLUMES}):
                out.append({"volume": name, "action": "skipped", "reason": "no longer a rebuildable BOSS volume"})
                continue
            if not apply:
                out.append({"volume": name, "action": "would_relabel",
                            "reason": _relabel_words(key, done=False)})
                continue
            for u in users:  # stopped db-test only (checked above); compose recreates it
                runner([*env.docker, "rm", u["id"]], timeout=60)
            res = runner([*env.docker, "volume", "rm", name], timeout=120)
            if res.returncode == 0:
                out.append({"volume": name, "action": "relabelled",
                            "reason": _relabel_words(key, done=True)})
            else:
                out.append({"volume": name, "action": "skipped",
                            "reason": f"{NEEDS_RELABEL} (docker volume rm: {(res.stderr or '').strip()[:120]})"})
        except (OSError, subprocess.TimeoutExpired) as exc:
            out.append({"volume": name, "action": "skipped", "reason": f"{NEEDS_RELABEL} ({type(exc).__name__})"})
    return out


def remove_test_leftovers(env: Env, runner: Callable = run_cmd, *, apply: bool = True) -> list[dict[str, str]]:
    """TEST DATA ONLY WITH A TEST POSTGRES (BOSSAPP-1459; upgrade.sh and `boss prune` run it). Only
    `boss dev up` makes the test Postgres, yet its volume (4.8 GB on the operator's laptop) was
    found on installs that run no tests. Its volume is removed when NO container — running or
    stopped — uses it: a db-test container means someone runs tests here, and its data stays. Only
    this install's Compose test volumes (REBUILDABLE/RETIRED keys of kind `test`), checked against
    the volume's own Compose labels at the moment of removal. Never stops anything; never raises."""
    out: list[dict[str, str]] = []
    keys = [k for k, kind in {**REBUILDABLE_COMPOSE_VOLUMES, **RETIRED_COMPOSE_VOLUMES}.items() if kind == "test"]
    for key in keys:
        name = f"{env.compose_project}_{key}"
        try:
            if _inspect_label(env, runner, "volume", name, COMPOSE_PROJECT_LABEL) != env.compose_project \
                    or _inspect_label(env, runner, "volume", name, COMPOSE_VOLUME_LABEL) != key:
                continue            # absent, or not this project's: never ours to touch
            users = _containers_using(env, runner, name)
            if users:
                out.append({"volume": name, "action": "kept",
                            "reason": "a test Postgres uses it (boss dev up): test data stays"})
                continue
            if not apply:
                out.append({"volume": name, "action": "would_remove",
                            "reason": "test data with no test Postgres: would be removed"})
                continue
            res = runner([*env.docker, "volume", "rm", name], timeout=120)
            out.append({"volume": name, "action": "removed" if res.returncode == 0 else "skipped",
                        "reason": "test data with no test Postgres; `boss dev up` makes a new one"
                        if res.returncode == 0 else f"docker volume rm: {(res.stderr or '').strip()[:120]}"})
        except (OSError, subprocess.TimeoutExpired) as exc:
            out.append({"volume": name, "action": "skipped", "reason": f"docker unavailable: {type(exc).__name__}"})
    return out


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
    """Discard unused VM blocks for the selected Colima or Docker Desktop runtime."""
    if not env.is_mac:
        return [], 0.0
    alloc, _ = docker_raw(env)
    if alloc is None:
        return [], 0.0
    df = docker_df(env, runner)
    used = df["images"] + df["cache"] + df["volumes"] + df["containers"]
    reclaim = max(0.0, alloc - used)
    profile = colima_profile(env)
    items = [f"colima:{profile}" if profile else "Docker.raw"] if reclaim > 0 else []
    if dry_run or not items:
        return items, reclaim
    command = (["colima", "--profile", profile, "ssh", "--", "sudo", "fstrim", "-a"]
               if profile else
               [*env.docker, "run", "--rm", "--privileged", "--pid=host", TRIM_IMAGE,
                "nsenter", "-t", "1", "-m", "-u", "-n", "-i", "fstrim", "/var/lib/docker"])
    res = runner(command, timeout=900)
    if res.returncode != 0:
        raise Refused(f"fstrim inside the Docker VM failed: {(res.stderr or '').strip()[:160]}")
    time.sleep(2)
    after, _ = docker_raw(env)
    return items, max(0.0, alloc - (after or alloc))


def select_backups(backups: list[dict[str, Any]], *, keep_newest: int, keep_days: float, cap_gb: float,
                   install_id: str, root: Path, now: Optional[float] = None) -> list[dict[str, Any]]:
    """The retention rule, pure (backup_archive's rule, so the script and the agent agree):
    only backups under `root` named boss-* whose manifest named THIS install; keep the newest N
    and anything younger than keep_days; everything else goes. Then the cap: while the kept set
    is over cap_gb and more than CAP_FLOOR remain, the oldest kept one goes too. Oldest first."""
    now = now or time.time()
    root_real = root.resolve()
    ours = []
    for b in backups:
        p = Path(b["path"])
        try:
            if p.is_symlink() or root_real not in p.resolve().parents:
                continue
        except OSError:
            continue
        if not p.name.startswith(BACKUP_NAME_PREFIX):
            continue
        if not install_id or b.get("install_id") != install_id:
            continue
        ours.append(b)
    ours.sort(key=lambda b: b["mtime"], reverse=True)       # newest first
    keep_n = max(0, int(keep_newest))
    protected = {b["path"] for b in ours[:keep_n]}
    protected |= {b["path"] for b in ours if now - b["mtime"] < float(keep_days) * 86400}
    to_delete = [b for b in reversed(ours) if b["path"] not in protected]     # oldest first
    kept = [b for b in ours if b["path"] in protected]                      # newest first
    cap = float(cap_gb) * 1e9
    if cap > 0:
        while kept and len(kept) > CAP_FLOOR and sum(b["bytes"] for b in kept) > cap:
            to_delete.append(kept.pop())                                    # the oldest kept
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
        manifest = backup_manifest(p)
        if (p.is_symlink() or root_real not in p.resolve().parents
                or not p.name.startswith(BACKUP_NAME_PREFIX) or manifest is None
                or str(manifest.get("install_id") or "") != env.install_id):
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
        raise Refused("job signed by a key this helper does not know (pinned key differs; if BOSS "
                      "rotated its key on purpose, run `boss steward rekey`)")
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
    if verb != "du":
        if not env.install_id:
            # "" == "" would pass: an install with no id can never prove a thing is its own.
            raise Refused("this install has no BOSS_INSTALL_ID in .env: nothing can be labelled as "
                          "ours, so nothing is pruned (run `boss update` or set BOSS_INSTALL_ID)")
        if str(args.get("install_id") or "") != env.install_id:
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
            out = {"ok": True, "dry_run": True, "items": items, "bytes": int(size),
                   "plan_hash": plan_hash(TOOL_OF[verb], env.host_id, args, items)}
            if verb == "remove_orphan_volumes":
                # Not silence: a BOSS volume made before its labels cannot be judged, so it is
                # named in the plan (never an item, never removed by a job).
                out["needs_relabel"] = unlabelled_boss_volumes(env, runner)
            return out
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
    if not env.install_id:
        # Loud, every run: without the id the backups and images of this install are invisible
        # to the gate, so the headline cause can never be cleaned.
        report["warning"] = ("no BOSS_INSTALL_ID in .env: backups and images cannot be told apart "
                             "from a stranger's, so none are sampled or pruned")
        print(f"boss steward: {report['warning']}", file=sys.stderr)
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
        # A key id other than the pinned one is refused inside perform/check_job ("signed by a
        # key this helper does not know"); nothing here refreshes the pin — `steward rekey` does.
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
    ap.add_argument("--rekey", action="store_true",
                    help="forget the pinned signing key; the next run pins BOSS's current one")
    ap.add_argument("--relabel-volumes", action="store_true",
                    help="recreate BOSS's rebuildable volumes that predate their labels, when unused "
                         "(the upgrade step; data volumes are never touched)")
    ap.add_argument("--test-leftovers", action="store_true",
                    help="remove the test Postgres volume when no container uses it (BOSSAPP-1459)")
    ap.add_argument("--dry-run", action="store_true",
                    help="with --relabel-volumes or --test-leftovers: say what would change")
    ap.add_argument("--version", action="store_true")
    ns = ap.parse_args(argv)
    if ns.version:
        print(VERSION)
        return 0
    env = Env(ns.install_dir, api_url=ns.api_url, backup_root=ns.backup_dir)
    if ns.rekey:
        forgot = unpin_key(env)
        log_line(env, {"host_id": env.host_id, "rekey": forgot})
        print("pinned signing key forgotten; the next run pins BOSS's current key" if forgot
              else "no pinned key to forget")
        return 0
    if ns.print:
        print(json.dumps(collect(env), indent=2, default=str))
        return 0
    if ns.relabel_volumes:
        done = relabel_volumes(env, run_cmd, apply=not ns.dry_run)
        log_line(env, {"host_id": env.host_id, "relabel_volumes": done})
        for row in done:
            print(f"{row['volume']}: {row['action']} — {row['reason']}")
        return 0
    if ns.test_leftovers:
        done = remove_test_leftovers(env, run_cmd, apply=not ns.dry_run)
        log_line(env, {"host_id": env.host_id, "test_leftovers": done})
        for row in done:
            print(f"{row['volume']}: {row['action']} — {row['reason']}")
        return 0
    report = once(env, sample_only=ns.sample_only)
    print(json.dumps(report, default=str))
    return 0 if not report.get("sample_error") else 1


if __name__ == "__main__":
    sys.exit(main())
