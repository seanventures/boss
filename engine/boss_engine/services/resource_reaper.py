"""THE REAPER: everything BOSS made, and nobody owns any more, is removed (stability spec §4a).

Runs on its own thread in the API every minute — never on the worker's job loop, which is
blind for the whole of a build. Each pass works from BOTH sides of the ownership ledger:

* from the rows to the host — an open `owned_resources` row whose owner has ended has its
  resource ended (a process tree by its token, a directory by its path) and is released;
* from the host to the rows — a process carrying a BOSS token whose creator is gone, a
  container or image labelled `boss.owner` (or named `boss-prev-*`) with no live preview, a
  temp directory under one of BOSS's prefixes that nothing claims: removed.

Plus the periodic jobs that used to happen only lazily or never: lost job leases (reap_jobs),
preview expiry and idle-stop,
the 75%-disk prune of BOSS's own Docker artefacts, and table retention for the tick tables.

Every action is scoped to what BOSS created. Never `docker system prune`, never `--volumes`,
never a container or image without a BOSS label or the legacy preview name, never a directory
outside BOSS's own temp prefixes.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Optional, Sequence

from sqlalchemy import text

from . import owned_process
from .resource_ledger import claimed_dirs, node_id

_log = logging.getLogger("boss.resource_reaper")

INTERVAL_SECONDS = 60.0
#: An unclaimed temp directory younger than this is left alone: something may be about to
#: claim it (mkdtemp and claim_dir are two calls).
DIR_MIN_AGE_SECONDS = 30 * 60
#: BOSS's own temp prefixes (spec §4a.7). The preview root has its own sweep.
DIR_PREFIXES = ("boss-serve-proof-", "boss-mcp-", "boss-deploy-", "boss-inspect-",
                "boss-agent-code-")
SANDBOX_ROOT_NAME = "boss-sandboxes"
#: Legacy preview containers/images were named, not labelled.
LEGACY_PREVIEW_PREFIX = "boss-prev-"


# ------------------------------------------------------------------------------------------ #
# processes
# ------------------------------------------------------------------------------------------ #

def reap_processes(engine: Any = None) -> dict[str, Any]:
    """Collect exited leaders, end orphaned trees, release rows for trees that are gone."""
    collected = owned_process.poll_all()
    orphans = owned_process.sweep_orphans()
    released = 0
    if engine is not None:
        live_tokens = {f"token:{p.token}" for p in owned_process.live()}
        node = node_id()
        try:
            with engine.begin() as c:
                rows = c.execute(text(
                    "SELECT id, handle, node FROM owned_resources "
                    "WHERE resource_kind = 'process_group' AND released_at IS NULL "
                    "AND created_at < now() - interval '1 minute'")).mappings().all()
            for row in rows:
                if row["handle"] in live_tokens:
                    continue
                if row["node"] == node and str(row["handle"]).startswith("token:"):
                    # Ours, not live in this process: end anything still carrying its token.
                    owned_process.terminate_tree(None, str(row["handle"])[6:], grace=2.0)
                # A row from another node is from a container that has since been replaced
                # (a deploy): its processes went with it, so the row only needs closing.
                with engine.begin() as c:
                    released += c.execute(text(
                        "UPDATE owned_resources SET released_at = now(), release_reason = :r "
                        "WHERE id = :id AND released_at IS NULL"),
                        {"id": row["id"], "r": "reaped: owner gone"}).rowcount or 0
        except Exception:  # noqa: BLE001
            _log.debug("process ledger pass failed", exc_info=True)
    return {"collected": collected, "orphan_trees": orphans["trees"],
            "orphan_processes": orphans["processes"], "released_rows": released}


# ------------------------------------------------------------------------------------------ #
# directories
# ------------------------------------------------------------------------------------------ #

def _dir_candidates(tmp: Path) -> list[Path]:
    out: list[Path] = []
    try:
        for entry in tmp.iterdir():
            if entry.is_dir() and entry.name.startswith(DIR_PREFIXES):
                out.append(entry)
    except OSError:
        pass
    sandboxes = tmp / SANDBOX_ROOT_NAME
    try:
        out.extend(p for p in sandboxes.iterdir() if p.is_dir())
    except OSError:
        pass
    return out


def reap_dirs(*, tmp: Optional[Path] = None, min_age: float = DIR_MIN_AGE_SECONDS,
              now: Optional[float] = None) -> dict[str, Any]:
    """Remove BOSS temp directories nothing claims and nothing touched for `min_age`."""
    tmp = tmp or Path(tempfile.gettempdir())
    now = now if now is not None else time.time()
    claimed = claimed_dirs()
    removed, kept, freed = [], 0, 0
    for path in _dir_candidates(tmp):
        key = str(path)
        if key in claimed or any(c.startswith(key + os.sep) for c in claimed):
            kept += 1
            continue
        try:
            age = now - path.stat().st_mtime
        except OSError:
            continue
        if age < min_age:
            kept += 1
            continue
        freed += _du(path)
        shutil.rmtree(path, ignore_errors=True)
        if not path.exists():
            removed.append(key)
            from .resource_ledger import release
            release("dir", key, "reaped: unclaimed")
    return {"removed": len(removed), "kept": kept, "freed_mb": round(freed / 1e6, 1),
            "paths": removed[:20]}


#: A sandbox run directory nothing holds and nothing touched for this long is a leftover.
SANDBOX_ORPHAN_SECONDS = 24 * 3600
_HEX_KEY = re.compile(r"^[0-9a-f]{24}$")


def _newest_mtime(path: Path, depth: int = 2) -> float:
    """The newest mtime of `path` and what is under it to `depth` levels (a run writes into its
    workspaces and HOME, not into the run directory itself). Unreadable entries are skipped."""
    newest = 0.0
    stack = [(path, 0)]
    while stack:
        p, d = stack.pop()
        try:
            st = p.lstat()
        except OSError:
            continue
        newest = max(newest, st.st_mtime)
        if d < depth and p.is_dir() and not p.is_symlink():
            try:
                stack.extend((c, d + 1) for c in p.iterdir())
            except OSError:
                continue
    return newest


def _uid_lock_free(uid: int) -> bool:
    """No process in this container holds the run lease of `uid` (sandbox_identity._lease_uid)."""
    import fcntl

    from . import sandbox_identity

    path = os.path.join(sandbox_identity.SHIM_ROOT, f"uid-{uid}.lock")
    try:
        fd = os.open(path, os.O_RDWR)
    except OSError:
        return True             # never leased here since the container started
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return True
    except OSError:
        return False
    finally:
        os.close(fd)


def _uid_has_processes(uid: int) -> bool:
    for status in Path("/proc").glob("[0-9]*/status"):
        try:
            for line in status.read_text().splitlines():
                if line.startswith("Uid:"):
                    if str(uid) in line.split()[1:2]:
                        return True
                    break
        except OSError:
            continue
    return False


def reap_sandbox_dirs(engine: Any = None, *, root: Optional[Path] = None,
                      min_age: float = SANDBOX_ORPHAN_SECONDS,
                      now: Optional[float] = None) -> dict[str, Any]:
    """A SANDBOX THAT OUTLIVED ITS RUN (BOSSAPP-1459). `sandbox_identity.release` removes a run's
    directory when the run ends; a run that died with its process (an API restart, a deploy)
    never got there, and its workspace stayed: 2.7 GB of them on the operator's laptop, the oldest
    a week old. A directory under the sandbox root is a leftover only when ALL hold:

    * it is one BOSS made: a 24-hex owner key, owned by a uid of the sandbox pool;
    * no lease on that uid is held in this container, and no process runs as it;
    * it is not the directory of a live owner (a running preview, an unended build, a lease of
      this process): BOSS holds a preview's directory across restarts;
    * nothing under it (two levels deep) changed for `min_age` (24 h).

    Then it is removed the way `release` removes one (as the run's uid, then as BOSS)."""
    from . import sandbox_identity

    root = root or sandbox_identity.run_root()
    now = now if now is not None else time.time()
    live: set[str] = set(owned_process.live_owner_ids())
    with sandbox_identity._ACTIVE_LOCK:
        live |= set(sandbox_identity._ACTIVE)
    if engine is not None:
        try:
            live |= live_preview_handles(engine)["ids"]
            tasks = _live_sandbox_tasks(engine)
        except Exception:  # noqa: BLE001 — unknown: keep every sandbox
            return {"removed": 0, "kept": "live owners unknown"}
        if "*" in tasks:
            return {"removed": 0, "kept": "live owners unknown"}
        live |= tasks
    live_keys = {sandbox_identity.owner_key(o) for o in live}
    removed, kept, freed = [], 0, 0
    try:
        entries = sorted(p for p in root.iterdir() if p.is_dir() and not p.is_symlink())
    except OSError:
        return {"removed": 0, "kept": 0}
    pool = range(sandbox_identity.UID_BASE, sandbox_identity.UID_BASE + sandbox_identity.UID_COUNT)
    for path in entries:
        try:
            uid = path.lstat().st_uid
        except OSError:
            continue
        if not _HEX_KEY.match(path.name) or uid not in pool or path.name in live_keys:
            kept += 1
            continue
        if now - _newest_mtime(path) < min_age or not _uid_lock_free(uid) or _uid_has_processes(uid):
            kept += 1
            continue
        size = _du(path)
        sandbox_identity._purge(sandbox_identity.SandboxIdentity(key=path.name, uid=uid, run_dir=path))
        if not path.exists():
            removed.append(path.name)
            freed += size
    return {"removed": len(removed), "kept": kept, "freed_mb": round(freed / 1e6, 1),
            "keys": removed[:20]}


# ------------------------------------------------------------------------------------------ #
# test databases on the live server
# ------------------------------------------------------------------------------------------ #

#: The test Postgres's Compose service name (`boss dev up`). Resolvable on this network only
#: while an install runs one: then the test data is someone's, and it stays.
TEST_SERVER_HOST = "db-test"


def test_server_present(host: str = TEST_SERVER_HOST) -> bool:
    import socket

    try:
        socket.getaddrinfo(host, 5432)
        return True
    except OSError:
        return False


#: An unstamped test database (`boss_test_t1`) is a leftover only once it was seen idle, with no
#: transaction committed in it, on two passes at least this far apart.
TEST_DB_QUIET_SECONDS = 3600.0
_TEST_DB_SEEN: dict[str, tuple[float, int]] = {}


def reap_test_databases(engine: Any, *, test_server: Optional[bool] = None,
                        only: Optional[Sequence[str]] = None,
                        now: Optional[float] = None) -> dict[str, Any]:
    """TEST DATABASES ON AN INSTALL THAT RUNS NO TESTS (BOSSAPP-1459). Only the test suites and
    scratch work make `boss_test*` / `boss_scratch*` databases, and they belong on the test
    Postgres (`boss dev up`, BOSSAPP-1152). Found on the live server of an install that runs no
    tests (eight of them on the operator's laptop), they are leftovers: each one nobody is
    connected to is dropped. Kept, all of them, when:

    * this install runs a test Postgres (the `db-test` host resolves): a developer works here;
    * the database BOSS itself runs on is a test one (a test suite's own API, a CI shard):
      the others on that server are its siblings', never this process's to drop.

    And each database only when nobody is connected AND it is provably done: a stamped one
    (dev/test_databases.parse_stamped) past its 24 h TTL, or an unstamped one (`boss_test_t1`,
    which an older branch's test-tier.sh still makes here) seen idle with the same commit count
    on two passes TEST_DB_QUIET_SECONDS apart — never one a suite is between sessions in."""
    from ..dev.test_databases import parse_stamped
    from .disk_budget import is_test_database

    clock = now if now is not None else time.time()

    if test_server is None:
        test_server = test_server_present()
    if test_server:
        return {"dropped": [], "kept": "a test Postgres runs on this install"}
    with engine.connect() as c:
        current = str(c.execute(text("SELECT current_database()")).scalar() or "")
        if is_test_database(current) or os.environ.get("BOSS_ENVIRONMENT", "").strip().lower() in \
                ("test", "e2e", "ci"):
            return {"dropped": [], "kept": "this BOSS runs on a test database"}
        rows = c.execute(text(
            "SELECT d.datname, COALESCE(s.xact_commit, 0) FROM pg_database d "
            "LEFT JOIN pg_stat_database s ON s.datname = d.datname "
            "WHERE NOT d.datistemplate AND d.datname <> current_database() "
            "AND (d.datname LIKE 'boss\\_test%' OR d.datname LIKE 'boss\\_scratch%')")).all()
    dropped: list[str] = []
    waiting: list[str] = []
    for name, commits in ((str(r[0]), int(r[1] or 0)) for r in rows):
        if only is not None and name not in only:
            continue        # a test's own databases only, never a neighbour's on the same server
        if not is_test_database(name) or not re.fullmatch(r"[a-z0-9_]{1,63}", name):
            continue
        stamped = parse_stamped(name)
        if stamped is not None:
            if not stamped.expired():
                continue
        else:
            seen = _TEST_DB_SEEN.get(name)
            if seen is None or seen[1] != commits:
                _TEST_DB_SEEN[name] = (clock, commits)      # first sight, or it was used since
                waiting.append(name)
                continue
            if clock - seen[0] < TEST_DB_QUIET_SECONDS:
                waiting.append(name)
                continue
        try:
            with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as c:
                try:
                    c.execute(text("SET lock_timeout = '2s'"))
                    busy = c.execute(text("SELECT 1 FROM pg_stat_activity WHERE datname = :n LIMIT 1"),
                                     {"n": name}).first()
                    if busy:
                        _TEST_DB_SEEN.pop(name, None)
                        continue
                    c.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
                finally:
                    c.execute(text("RESET lock_timeout"))    # a pooled connection keeps no limit
            dropped.append(name)
            _TEST_DB_SEEN.pop(name, None)
        except Exception:  # noqa: BLE001 — a connection arrived, or no right: the next pass
            _log.info("test database %s not dropped", name, exc_info=True)
    return {"dropped": dropped, "waiting": waiting}


def _du(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path, onerror=lambda _e: None):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


# ------------------------------------------------------------------------------------------ #
# docker: containers and images BOSS made
# ------------------------------------------------------------------------------------------ #

def docker_available(binary: str = "docker") -> bool:
    if shutil.which(binary) is None:
        return False
    try:
        return owned_process.run([binary, "info", "--format", "{{.ServerVersion}}"],
                                 capture_output=True, text=True, timeout=10).returncode == 0
    except Exception:  # noqa: BLE001
        return False


def _docker(args: list[str], *, binary: str = "docker", timeout: float = 60) -> str:
    try:
        done = owned_process.run([binary, *args], capture_output=True, text=True,
                                 timeout=timeout)
    except Exception:  # noqa: BLE001
        return ""
    return done.stdout if done.returncode == 0 else ""


def live_preview_handles(engine: Any) -> dict[str, set[str]]:
    """Container ids/names and preview ids that a `running`/`starting` preview still owns."""
    ids: set[str] = set()
    containers: set[str] = set()
    if engine is None:
        return {"ids": ids, "containers": containers}
    with engine.begin() as c:
        for r in c.execute(text(
                "SELECT id, container_id FROM app_previews "
                "WHERE status IN ('running', 'starting')")).mappings():
            ids.add(str(r["id"]))
            if r["container_id"]:
                containers.add(str(r["container_id"]))
    from .preview_service import preview_image_tag

    containers |= {preview_image_tag(i) for i in ids}
    return {"ids": ids, "containers": containers}


#: A container or image younger than this is never swept: a preview's container and image
#: exist for up to a build + 20 s before its row says `running`, and a sandbox container is
#: created at the start of its build.
MIN_DOCKER_AGE_SECONDS = 15 * 60


def _age_seconds(created: str) -> float:
    """Age from docker's `CreatedAt` ("2026-10-05 01:27:08 +0000 UTC") or an ISO stamp."""
    from datetime import datetime, timezone

    raw = (created or "").strip()
    for fmt, cut in (("%Y-%m-%d %H:%M:%S %z", 25), ("%Y-%m-%dT%H:%M:%S", 19)):
        try:
            stamp = datetime.strptime(raw[:cut], fmt)
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - stamp).total_seconds()
        except ValueError:
            continue
    return 0.0          # unknown age: treated as young, never swept


def _live_sandbox_tasks(engine: Any) -> set[str]:
    """Build ids whose sandbox container must stay: live in this process, or not ended."""
    live = set(owned_process.live_owner_ids())
    if engine is None:
        return live
    try:
        with engine.begin() as c:
            live |= {str(r[0]) for r in c.execute(text(
                "SELECT id FROM code_task_runs WHERE status IN ('running', 'queued', "
                "'repairing', 'starting')")).all()}
    except Exception:  # noqa: BLE001 — unknown: keep every sandbox
        return {"*"}
    return live


def reap_docker(engine: Any, *, install_id: Optional[str], binary: str = "docker",
                legacy_prefix: str = LEGACY_PREVIEW_PREFIX,
                live: Optional[dict[str, set[str]]] = None,
                min_age: float = MIN_DOCKER_AGE_SECONDS) -> dict[str, Any]:
    """The substrate side (spec §4a.6): containers and images labelled by THIS install, or
    named like a preview (legacy, unlabelled), that nothing live owns and that are older than
    `min_age`. A sandbox container is kept while its build is not ended."""
    live = live if live is not None else live_preview_handles(engine)
    sandboxes: Optional[set[str]] = None
    removed_c, removed_i = [], []
    fmt = ('{{.ID}}\t{{.Names}}\t{{.Label "boss.preview_id"}}\t{{.Label "boss.owner"}}\t'
           '{{.Label "boss.task_id"}}\t{{.Label "boss.install_id"}}\t{{.CreatedAt}}')
    rows: list[list[str]] = []
    if install_id:
        rows += [(line.split("\t") + [""] * 7)[:7] for line in _docker(
            ["ps", "-a", "--filter", f"label=boss.install_id={install_id}", "--format", fmt],
            binary=binary).splitlines() if line.strip()]
    rows += [r for r in ((line.split("\t") + [""] * 7)[:7] for line in _docker(
        ["ps", "-a", "--filter", f"name=^{legacy_prefix}", "--format", fmt],
        binary=binary).splitlines() if line.strip())
        if r[1].startswith(legacy_prefix) and not r[5]]
    for cid, name, preview_id, owner, task_id, _inst, created in rows:
        if _age_seconds(created) < min_age:
            continue
        if owner == "mcp_stdio":
            # A stdio MCP server's container is its supervisor's (BOSSAPP-850): the supervisor's
            # own step below stops idle ones and removes those whose creating process is gone.
            continue
        if owner == "sandbox":
            if sandboxes is None:
                sandboxes = _live_sandbox_tasks(engine)
            if "*" in sandboxes or not task_id or task_id in sandboxes:
                continue
        elif preview_id in live["ids"] or name in live["containers"] or \
                cid in live["containers"] or \
                any(cid.startswith(x) or x.startswith(cid) for x in live["containers"] if x):
            continue
        _docker(["rm", "-f", "-v", cid], binary=binary)
        removed_c.append(name or cid)
    # Images: same two ways in. An image a remaining container uses, or a young one, stays.
    used = set(_docker(["ps", "-a", "--format", "{{.Image}}"], binary=binary).split())
    ifmt = "{{.Repository}}:{{.Tag}}\t{{.CreatedAt}}"
    images: list[tuple[str, str]] = []
    if install_id:
        images += [tuple((x.split("\t") + [""])[:2]) for x in _docker(
            ["images", "--filter", f"label=boss.install_id={install_id}", "--format", ifmt],
            binary=binary).splitlines() if x.strip()]
    images += [tuple((x.split("\t") + [""])[:2]) for x in _docker(
        ["images", "--format", ifmt], binary=binary).splitlines()
        if x.startswith(legacy_prefix)]
    for ref, created in sorted(set(images)):
        repo = ref.split(":", 1)[0]
        # The rollback point upgrade.sh keeps (BOSSAPP-839): never reaped.
        if repo.startswith("boss-community-") and ref.rsplit(":", 1)[-1].startswith("prev-"):
            continue
        if _age_seconds(created) < min_age:
            continue
        if repo in live["containers"] or ref in used or repo in used:
            continue
        if any(repo == f"{legacy_prefix}{i.replace('_', '-').lower()}" for i in live["ids"]):
            continue
        if repo.startswith(legacy_prefix) and not install_id and _image_install_label(ref, binary):
            continue
        if not repo.startswith(legacy_prefix) or not _image_install_label(ref, binary) or \
                _image_install_label(ref, binary) == install_id:
            _docker(["rmi", "-f", ref], binary=binary, timeout=120)
            removed_i.append(ref)
    return {"containers": removed_c, "images": removed_i}


def _image_install_label(ref: str, binary: str) -> str:
    return _docker(["image", "inspect", "--format",
                    '{{index .Config.Labels "boss.install_id"}}', ref], binary=binary).strip()


# ------------------------------------------------------------------------------------------ #
# disk
# ------------------------------------------------------------------------------------------ #

DISK_PRUNE_AT = 0.75


def disk_usage(path: str = "/") -> dict[str, float]:
    st = os.statvfs(path)
    total = st.f_blocks * st.f_frsize
    free = st.f_bavail * st.f_frsize
    used = total - free
    return {"total_gb": round(total / 1e9, 1), "free_gb": round(free / 1e9, 1),
            "used_fraction": round(used / total, 3) if total else 0.0}


def prune_disk(*, binary: str = "docker", install_id: Optional[str] = None,
               force: bool = False, usage: Optional[dict[str, float]] = None) -> dict[str, Any]:
    """Above 75% disk, remove BOSS's own Docker artefacts (spec §4a.8): dangling images BOSS
    labelled, and — only on a host BOSS owns (BOSS_DEDICATED_HOST=1, written by the remote
    installer) — build cache older than three days down to 20% of the disk. Never images or
    cache another project on a shared machine depends on; never volumes."""
    usage = usage or disk_usage("/")
    if not force and usage["used_fraction"] < DISK_PRUNE_AT:
        return {"pruned": False, "reason": "below threshold", **usage}
    did: list[str] = []
    if install_id:
        _docker(["image", "prune", "-f", "--filter", f"label=boss.install_id={install_id}"],
                binary=binary, timeout=300)
        did.append("dangling BOSS images")
    if os.environ.get("BOSS_DEDICATED_HOST", "").strip() in ("1", "true", "yes"):
        keep_gb = max(1, int(usage["total_gb"] * 0.20))
        _docker(["builder", "prune", "-f", "--filter", "until=72h",
                 "--keep-storage", f"{keep_gb}gb"], binary=binary, timeout=900)
        did.append(f"build cache older than 72h (kept {keep_gb} GB)")
    after = disk_usage("/")
    return {"pruned": True, "actions": did, "before": usage, "after": after}


# ------------------------------------------------------------------------------------------ #
# tables
# ------------------------------------------------------------------------------------------ #

def prune_tables(engine: Any) -> dict[str, int]:
    """Tick tables keep 7 days (and one row an hour for 90) (spec §4a.9); tick and heartbeat
    events are telemetry_retention's (ST-8). Bounded deletes, so one pass never holds a long lock."""
    out: dict[str, int] = {}
    statements = {
        **{table: (
            f"DELETE FROM {table} WHERE id IN (SELECT id FROM {table} "
            "WHERE created_at < now() - interval '7 days' AND (created_at < now() - "
            "interval '90 days' OR id NOT IN (SELECT DISTINCT ON (date_trunc('hour', created_at)) "
            f"id FROM {table} WHERE created_at < now() - interval '7 days' "
            "ORDER BY date_trunc('hour', created_at), created_at)) LIMIT 5000)")
           for table in ("metatron_watch_ticks", "process_manager_ticks")},
        # `events` is NOT pruned here any more (ST-8): telemetry_retention owns it (policy
        # `events_ticks`, the same 14 days). `events` is range-partitioned by month now, and a
        # `ctid IN (SELECT ctid …)` delete on a partitioned table matches the same physical
        # position in EVERY partition — it deleted live audit events beside the ticks.
        # test_no_ctid_delete_on_a_partitioned_table guards the class.
        "owned_resources": (
            "DELETE FROM owned_resources WHERE ctid IN (SELECT ctid FROM owned_resources "
            "WHERE released_at < now() - interval '14 days' LIMIT 5000)"),
        # L1 baseline-aware verification: a base commit's cached outcomes are only useful while
        # that commit is still a live build's base, and a signature's count only means something
        # while it keeps recurring — both age out instead of growing forever.
        "code_task_baseline_cache": (
            "DELETE FROM code_task_baseline_cache WHERE ctid IN (SELECT ctid FROM "
            "code_task_baseline_cache WHERE updated_at < now() - interval '14 days' LIMIT 5000)"),
        "code_task_environment_signatures": (
            "DELETE FROM code_task_environment_signatures WHERE ctid IN (SELECT ctid FROM "
            "code_task_environment_signatures WHERE last_seen < now() - interval '30 days' "
            "LIMIT 5000)"),
    }
    # The activity log's own retention runs here too: it ran only on the worker's idle path,
    # so a worker that was always busy never aged anything out.
    try:
        from .activity_retention import prune as _prune_activity
        with engine.begin() as c:
            got = _prune_activity(c, days=None)
        out["activity_log"] = int((got or {}).get("deleted") or 0) if isinstance(got, dict) else 0
    except Exception:  # noqa: BLE001
        out["activity_log"] = 0
    # The run ledger (on by default since AP-P0): monthly partitions made ahead and dropped
    # past the window, never a row rewritten.
    try:
        from .ledger_retention import prune as _prune_ledger
        with engine.begin() as c:
            got = _prune_ledger(c)
        out["run_events_partitions"] = len(got.get("dropped") or [])
    except Exception:  # noqa: BLE001
        out["run_events_partitions"] = 0
    # Run traces (AP-P4): span payloads hot → cold → expired, span rows past their window.
    try:
        from .trace_payloads import backend_from_settings, sweep as _sweep_payloads
        got = _sweep_payloads(engine, backend=backend_from_settings())
        out["trace_payloads_moved"] = int(got.get("moved") or 0)
        out["trace_payloads_expired"] = int(got.get("expired") or 0)
        out["trace_spans"] = int(got.get("spans_deleted") or 0)
    except Exception:  # noqa: BLE001 — a schema without the tables is not an error
        out["trace_payloads_moved"] = out["trace_payloads_expired"] = out["trace_spans"] = 0
    # The knowledge graph's archival flag: computed by the learning layer, persisted here.
    try:
        from .knowledge.service import KnowledgeService
        with engine.begin() as c:
            if c.execute(text("SELECT to_regclass('kg_fact_stats')")).scalar() is not None:
                out["kg_facts_archived"] = int(KnowledgeService().archive_sweep(c)["archived"])
    except Exception:  # noqa: BLE001
        out["kg_facts_archived"] = 0
    for table, stmt in statements.items():
        try:
            with engine.begin() as c:
                out[table] = int(c.execute(text(stmt)).rowcount or 0)
        except Exception:  # noqa: BLE001 — a table an older schema lacks is not an error
            out[table] = 0
    return out


# ------------------------------------------------------------------------------------------ #
# the pass and the thread
# ------------------------------------------------------------------------------------------ #

_LAST: dict[str, Any] = {}
_TABLES_EVERY = 3600.0
_last_tables = 0.0


def reap_jobs(engine: Any, settings: Any = None) -> dict[str, int]:
    """Jobs whose holder is gone: a lease an hour past expiry that nobody took back. Spent
    attempts end (deadlettered, the build told why); the rest are queued again
    (JobLeaseService.reap_lost_leases). Before this, 18 `connection.proof` jobs on the laptop had
    said `running` since October and an `architecture.draft` was `leased` to a worker id that no
    longer existed."""
    from ..config import get_settings
    from .jobs import JobLeaseService

    with engine.begin() as c:
        return JobLeaseService(settings or get_settings()).reap_lost_leases(c, actor="reaper")


def tick(engine: Any, settings: Any = None) -> dict[str, Any]:
    """One reaper pass. Every part is independent: one failing never stops the others."""
    global _last_tables
    from ..db import background

    report: dict[str, Any] = {"at": time.time()}
    with background():
        report["processes"] = _safe(lambda: reap_processes(engine))
        if engine is not None:
            from . import preview_service

            def _reap_previews() -> Any:
                # Under pressure (L1+) an unwatched preview goes after 5 minutes, not 20.
                from .admission import L1, current_level
                with engine.begin() as c:
                    level = current_level(c)
                return preview_service.reap_previews(
                    engine, settings, idle_minutes=5 if level >= L1 else None)

            report["previews"] = _safe(_reap_previews)
            report["preview_workdirs"] = _safe(
                lambda: preview_service.sweep_preview_workdirs(engine))
        if engine is not None:
            report["jobs"] = _safe(lambda: reap_jobs(engine, settings))

            def _reconcile_unknown() -> Any:
                # A job parked in unknown_outcome (an irreversible effect whose call ended without
                # an answer, BOSSAPP-986) is only released once the provider says what happened.
                from ..config import get_settings
                from .durable_operations import reconcile_unknown_outcomes

                return reconcile_unknown_outcomes(engine, settings or get_settings())

            report["unknown_outcomes"] = _safe(_reconcile_unknown)
        if engine is not None:
            # A build that died without teardown must not hold an execution
            # environment forever (BOSSAPP-824): held leases past their TTL expire.
            from . import execution_environments

            report["environment_leases"] = _safe(
                lambda: execution_environments.reap_leases(engine, settings))
            # Workspace snapshots past their TTL go with their objects (BOSSAPP-826).
            # No store configured -> it returns before touching the table.
            from . import workspace_snapshots

            report["workspace_snapshots"] = _safe(
                lambda: workspace_snapshots.reap_expired(engine, settings))
        # Stdio MCP servers idle past their template's TTL, dead ones, and containers whose
        # creating process is gone (BOSSAPP-850).
        from . import mcp_stdio_supervisor

        report["mcp_stdio"] = _safe(mcp_stdio_supervisor.reap_tick)
        report["dirs"] = _safe(lambda: reap_dirs())
        # A run that died never dropped its build-services database (build_services).
        from . import build_services

        report["build_databases"] = _safe(lambda: build_services.reap_tick(engine))
        if engine is not None and docker_available():
            from .resource_ledger import install_id

            def _install() -> Optional[str]:
                with engine.begin() as c:
                    return install_id(c)

            inst = _safe(_install)
            inst = inst if isinstance(inst, str) else None
            report["docker"] = _safe(lambda: reap_docker(engine, install_id=inst))
            report["disk"] = _safe(lambda: prune_disk(install_id=inst))
        if engine is not None and time.time() - _last_tables > _TABLES_EVERY:
            _last_tables = time.time()
            report["tables"] = _safe(lambda: prune_tables(engine))
            # BOSSAPP-1459: what a run or a test left behind and nothing uses any more. Background
            # work (db.background): its transactions get the tick's lock and idle limits.
            report["sandbox_dirs"] = _safe(lambda: reap_sandbox_dirs(engine))
            report["test_databases"] = _safe(lambda: reap_test_databases(engine))
    _LAST.clear()
    _LAST.update(report)
    return report


def last_report() -> dict[str, Any]:
    return dict(_LAST)


def _safe(fn: Any) -> Any:
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001
        _log.debug("reaper step failed", exc_info=True)
        return {"error": f"{type(exc).__name__}: {exc}"[:300]}


_THREAD: Optional[threading.Thread] = None


def start(engine: Any, settings: Any, *, interval: float = INTERVAL_SECONDS) -> threading.Thread:
    global _THREAD
    if _THREAD is not None and _THREAD.is_alive():
        return _THREAD

    def _loop() -> None:
        while True:
            time.sleep(interval)
            try:
                report = tick(engine, settings)
                acted = {k: v for k, v in report.items()
                         if isinstance(v, dict) and any(
                             (isinstance(x, (int, float)) and x and k2 not in ("checked", "kept"))
                             or (isinstance(x, list) and x)
                             for k2, x in v.items())}
                if acted:
                    _log.info("reaper acted: %s", acted)
            except Exception:  # noqa: BLE001
                _log.debug("reaper tick failed", exc_info=True)

    _THREAD = threading.Thread(target=_loop, name="boss-reaper", daemon=True)
    _THREAD.start()
    return _THREAD
