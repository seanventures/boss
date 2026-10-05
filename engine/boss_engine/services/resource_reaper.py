"""THE REAPER: everything BOSS made, and nobody owns any more, is removed (stability spec §4a).

Runs on its own thread in the API every minute — never on the worker's job loop, which is
blind for the whole of a build. Each pass works from BOTH sides of the ownership ledger:

* from the rows to the host — an open `owned_resources` row whose owner has ended has its
  resource ended (a process tree by its token, a directory by its path) and is released;
* from the host to the rows — a process carrying a BOSS token whose creator is gone, a
  container or image labelled `boss.owner` (or named `boss-prev-*`) with no live preview, a
  temp directory under one of BOSS's prefixes that nothing claims: removed.

Plus the periodic jobs that used to happen only lazily or never: preview expiry and idle-stop,
the 75%-disk prune of BOSS's own Docker artefacts, and table retention for the tick tables.

Every action is scoped to what BOSS created. Never `docker system prune`, never `--volumes`,
never a container or image without a BOSS label or the legacy preview name, never a directory
outside BOSS's own temp prefixes.
"""
from __future__ import annotations

import logging
import os
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Optional

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


def reap_docker(engine: Any, *, install_id: Optional[str], binary: str = "docker",
                legacy_prefix: str = LEGACY_PREVIEW_PREFIX,
                live: Optional[dict[str, set[str]]] = None) -> dict[str, Any]:
    """The substrate side (spec §4a.6): containers and images labelled `boss.owner` by THIS
    install, or named like a preview (legacy, unlabelled), whose preview is not live."""
    live = live if live is not None else live_preview_handles(engine)
    removed_c, removed_i = [], []
    # Containers: labelled by this install …
    rows: list[tuple[str, str, str]] = []
    if install_id:
        for line in _docker(["ps", "-a", "--filter", f"label=boss.install_id={install_id}",
                             "--format", '{{.ID}}\t{{.Names}}\t{{.Label "boss.preview_id"}}'],
                            binary=binary).splitlines():
            parts = (line.split("\t") + ["", "", ""])[:3]
            rows.append((parts[0], parts[1], parts[2]))
    # … and legacy: named like a preview, carrying no install label at all.
    for line in _docker(["ps", "-a", "--filter", f"name=^{legacy_prefix}",
                         "--format", '{{.ID}}\t{{.Names}}\t{{.Label "boss.install_id"}}'],
                        binary=binary).splitlines():
        parts = (line.split("\t") + ["", "", ""])[:3]
        if parts[1].startswith(legacy_prefix) and not parts[2]:
            rows.append((parts[0], parts[1], ""))
    for cid, name, preview_id in rows:
        if preview_id in live["ids"] or name in live["containers"] or cid in live["containers"] \
                or any(cid.startswith(x) or x.startswith(cid) for x in live["containers"] if x):
            continue
        _docker(["rm", "-f", cid], binary=binary)
        removed_c.append(name or cid)
    # Images: same two ways in. An image a remaining container uses is never removed.
    used = set(_docker(["ps", "-a", "--format", "{{.Image}}"], binary=binary).split())
    images: list[str] = []
    if install_id:
        images += [x for x in _docker(
            ["images", "--filter", f"label=boss.install_id={install_id}",
             "--format", "{{.Repository}}:{{.Tag}}"], binary=binary).split() if x]
    images += [x for x in _docker(["images", "--format", "{{.Repository}}:{{.Tag}}"],
                                  binary=binary).split()
               if x.startswith(legacy_prefix) and not _image_install_label(x, binary)]
    for ref in sorted(set(images)):
        repo = ref.split(":", 1)[0]
        if repo in live["containers"] or ref in used or repo in used:
            continue
        if any(repo == f"{legacy_prefix}{i.replace('_', '-').lower()}" for i in live["ids"]):
            continue
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
    """Tick tables keep 7 days (and one row an hour for 90); tick and heartbeat events 14
    days (spec §4a.9). Bounded deletes, so one pass never holds a long lock."""
    out: dict[str, int] = {}
    statements = {
        **{table: (
            f"DELETE FROM {table} WHERE id IN (SELECT id FROM {table} "
            "WHERE created_at < now() - interval '7 days' AND (created_at < now() - "
            "interval '90 days' OR id NOT IN (SELECT DISTINCT ON (date_trunc('hour', created_at)) "
            f"id FROM {table} WHERE created_at < now() - interval '7 days' "
            "ORDER BY date_trunc('hour', created_at), created_at)) LIMIT 5000)")
           for table in ("metatron_watch_ticks", "process_manager_ticks")},
        "events": (
            "DELETE FROM events WHERE ctid IN (SELECT ctid FROM events WHERE "
            "(event_type LIKE '%.ticked' OR event_type = 'job.heartbeat') "
            "AND created_at < now() - interval '14 days' LIMIT 5000)"),
        "owned_resources": (
            "DELETE FROM owned_resources WHERE ctid IN (SELECT ctid FROM owned_resources "
            "WHERE released_at < now() - interval '14 days' LIMIT 5000)"),
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


def tick(engine: Any, settings: Any = None) -> dict[str, Any]:
    """One reaper pass. Every part is independent: one failing never stops the others."""
    global _last_tables
    report: dict[str, Any] = {"at": time.time()}
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
    report["dirs"] = _safe(lambda: reap_dirs())
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
