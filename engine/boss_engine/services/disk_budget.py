"""ONE DISK BUDGET: where a running install's disk goes, and the line each part must stay under
(BOSSAPP-1459).

An enterprise install reached about 47 GB. Measured on the operator's laptop the same day
(2026-11-24): a full second set of images kept for `boss rollback` and never dropped (~15 GB
real, ~30 GB as Docker lists it), a test Postgres volume and eight test databases on an install
that runs no tests (~5 GB), hourly in-app backups with no size cap (6.4 GB), sandboxes of runs that
ended without cleaning up (2.7 GB), anonymous Postgres volumes left by test containers (~6 GB), and
a database whose vector table held 924 MB for 145 rows. Each grew by itself while BOSS ran, and
nothing said where the space had gone.

THIS MODULE IS THE ONE LIST. Every category a running install fills is named here with:

* its budget: the warn line for a typical install (one to three projects, daily use). `boss
  doctor` (scripts/install/checkup.sh) and the Storage card in Govern › Storage read the same
  numbers and warn at 80 % of a budget, before it is reached, and again past it;
* what keeps it bounded (`bounded_by`): the mechanism, by module or script, so a category with
  no mechanism cannot be added without saying so (test_a_running_install_stays_in_its_budget);
* the sentence a person sees when it is over, checked against the failure_words contract.

docs/operations/DISK_BUDGET.md has the measured steady state per category and how it was measured.

Standard library only: checkup.sh runs this file BY PATH on the host (`python3 disk_budget.py
report --install-dir DIR`), where BOSS's Python packages are not installed. The API imports it.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

GB = 1e9
#: Warn this far before a budget is reached (a fraction of it).
WARN_AT = 0.8
#: How long `boss rollback` keeps the previous release's images after a healthy upgrade. Past it
#: the next prune (`boss prune`, an upgrade, an update, or a storage cleanup) drops them.
ROLLBACK_KEEP_HOURS_DEFAULT = 72.0
ROLLBACK_KEEP_ENV = "BOSS_ROLLBACK_KEEP_HOURS"
#: Databases that only tests and scratch work create (engine/boss_engine/dev/test_databases.py).
TEST_DATABASE_PREFIXES = ("boss_test", "boss_scratch")
#: The test Postgres's volumes (current labelled key and the retired one), per Compose project.
TEST_VOLUME_KEYS = ("boss_community_pg_test_v2", "boss_community_pg_test")


@dataclass(frozen=True)
class Category:
    key: str
    label: str
    #: The warn line for a typical install, in GB.
    budget_gb: float
    #: What keeps it bounded: the mechanism a person can read.
    bounded_by: str
    #: What to do when it is over: a sentence with a place (a console area, a `boss` command, or
    #: BOSS doing it by itself) — the failure_words contract.
    action: str
    #: The line while this install runs a test Postgres (`boss dev up`), where it differs.
    test_server_budget_gb: Optional[float] = None
    #: Measured where: "host" (Docker; `boss doctor`, the steward) or "db" (the API, the doctor).
    where: str = "host"

    def budget(self, test_server: bool = False) -> float:
        if test_server and self.test_server_budget_gb is not None:
            return self.test_server_budget_gb
        return self.budget_gb


CATEGORIES: tuple[Category, ...] = (
    Category("images", "BOSS images", 10.0,
             "one image set per release; the build size is BOSSAPP-1458's budget",
             "Run `boss prune` to remove image tags other than the running release."),
    Category("rollback_images", "Rollback images", 10.0,
             f"one previous set only, dropped {ROLLBACK_KEEP_HOURS_DEFAULT:g} h after a healthy upgrade "
             "(scripts/install/prune.sh) or by the next upgrade",
             "Run `boss prune` to drop them once the upgrade is known good."),
    Category("build_cache", "Build cache", 10.0,
             "capped at BOSS_BUILD_CACHE_CAP_GB by every prune (scripts/install/prune.sh)",
             "Run `boss prune` to cap it."),
    Category("db_records", "Database: records", 3.0,
             "the live records; projects deleted in Projects take their rows with them",
             "Open Govern › Storage to see which tables grew.", where="db"),
    Category("db_knowledge", "Database: knowledge graph", 3.0,
             "one graph per project, rebuilt in place; a deleted project drops its graph; bloated "
             "tables are compacted by the hourly retention pass",
             "Open Knowledge Graph and delete the projects whose code no longer needs indexing.",
             where="db"),
    Category("db_telemetry", "Database: telemetry", 2.0,
             "per-table retention, hourly (telemetry_retention.POLICIES and its owners)",
             "Nothing to do: retention trims it by itself every hour; Govern › Storage shows the tables.",
             where="db"),
    Category("test_data", "Test databases", 0.0,
             "only `boss dev up` makes a test Postgres; without one, test databases and its volume "
             "are leftovers BOSS removes by itself (the API reaper; the steward after an upgrade and on boss prune)",
             "Nothing to do: BOSS removes test leftovers by itself when no test Postgres runs; with "
             "one, `boss dev sweep-test-db` drops old test databases.", test_server_budget_gb=8.0),
    Category("backups", "Backups", 5.0,
             "tiered retention, then a count and size cap (backup_scheduler): automatic on a dev-tier "
             "install, one storage decision on a production install",
             "Approve the backup decision in Decisions; or set BOSS_BACKUP_MAX_COUNT and "
             "BOSS_BACKUP_SCHEDULED_CAP_GB in .env and run `boss update`."),
    Category("sandboxes", "Sandboxes", 3.0,
             "a run's directory goes when the run ends; one left behind is reaped after 24 h "
             "(resource_reaper.reap_sandbox_dirs)",
             "Nothing to do: BOSS removes sandboxes of ended runs by itself after 24 hours."),
    Category("volumes_other", "Other BOSS volumes", 5.0,
             "sign-in homes, toolchain cache, artifacts and repositories; the toolchain cache is "
             "rebuildable and removed by a storage cleanup",
             "Open Govern › Storage to see which volume grew."),
    Category("dangling_volumes", "Unused volumes", 1.0,
             "BOSS removes the anonymous volumes of every container it removes (`docker rm -v`); "
             "volumes BOSS did not label are never touched",
             "Review them with `docker volume ls --filter dangling=true` and remove the ones you "
             "know are not needed; BOSS leaves volumes it did not label alone."),
    Category("logs", "Containers and logs", 2.0,
             "capped per container by Compose (50 MB × 3)",
             "Nothing to do: Compose caps each log by itself."),
)
BY_KEY = {c.key: c for c in CATEGORIES}

#: The tables whose size counts as telemetry: every table a retention pass owns (kept equal to
#: telemetry_retention's registry by test_a_running_install_stays_in_its_budget).
TELEMETRY_TABLES: tuple[str, ...] = (
    "events", "events_archive", "receipts", "anomaly_signals", "app_metrics_scrapes",
    "deployment_health_samples", "agent_turns", "app_logs", "metatron_watch_ticks",
    "process_manager_ticks", "llm_calls", "tool_call_audit", "run_events", "trace_spans",
    "trace_payloads", "storage_samples", "identity_egress_log", "focused_agent_ticks",
    "worker_autoscaling_history", "trace_export_attempts", "a2a_transport_attempts",
    "policy_eval_log", "identity_egress_credentials", "intake_events",
)

#: The database split, as SQL returning bytes. The same statements run in the API (storage
#: summary) and through `psql` on the host (boss doctor). Catalogue reads only.
_TEL_LIST = ", ".join(f"'{t}'" for t in TELEMETRY_TABLES)
DB_SQL: dict[str, str] = {
    "db_total": "SELECT pg_database_size(current_database())",
    "db_knowledge": (
        "SELECT COALESCE(sum(pg_total_relation_size(c.oid)), 0) FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE c.relkind IN ('r', 'm') AND ((n.nspname = 'public' AND c.relname LIKE 'kg\\_%') "
        "OR n.nspname LIKE 'kg\\_%')"),
    "db_telemetry": (
        "SELECT COALESCE(sum(pg_total_relation_size(p.relid)), 0) FROM unnest(ARRAY["
        + _TEL_LIST + "]::text[]) AS t(name) "
        "CROSS JOIN LATERAL pg_partition_tree(to_regclass(t.name)) p "
        "WHERE to_regclass(t.name) IS NOT NULL AND p.isleaf"),
    "test_databases": (
        "SELECT COALESCE(sum(pg_database_size(datname)), 0) FROM pg_database "
        "WHERE datname LIKE 'boss\\_test%' OR datname LIKE 'boss\\_scratch%'"),
}


# ── settings ────────────────────────────────────────────────────────────────────────────────

def rollback_keep_hours(environ: Optional[Mapping[str, str]] = None) -> float:
    env = os.environ if environ is None else environ
    try:
        hours = float(str(env.get(ROLLBACK_KEEP_ENV) or "").strip() or ROLLBACK_KEEP_HOURS_DEFAULT)
    except ValueError:
        hours = ROLLBACK_KEEP_HOURS_DEFAULT
    return max(1.0, hours)


def is_test_database(name: str) -> bool:
    return str(name or "").lower().startswith(TEST_DATABASE_PREFIXES)


# ── sizes ───────────────────────────────────────────────────────────────────────────────────

_SIZE = re.compile(r"^\s*([0-9.]+)\s*([kKmMgGtT]?i?[bB])?\s*$")
_UNIT = {"b": 1, "kb": 1e3, "mb": 1e6, "gb": 1e9, "tb": 1e12,
         "kib": 1024, "mib": 1024 ** 2, "gib": 1024 ** 3, "tib": 1024 ** 4}


def parse_size(text: Any) -> float:
    """Docker's "1.85GB", "24.6kB", "0B" → bytes (0.0 when unreadable)."""
    m = _SIZE.match(str(text or ""))
    if not m:
        return 0.0
    return float(m.group(1)) * _UNIT.get((m.group(2) or "b").lower(), 1)


# ── evaluation ──────────────────────────────────────────────────────────────────────────────

def evaluate(measured: Mapping[str, Optional[float]], *, test_server: bool = False,
             rollback_age_hours: Optional[float] = None,
             keep_hours: float = ROLLBACK_KEEP_HOURS_DEFAULT) -> list[dict[str, Any]]:
    """One row per category: {key, label, gb, budget_gb, pct, level, bounded_by, what, action}.

    level: "ok" below WARN_AT of the budget, "near" from it, "over" past it, "unknown" when it
    was not measured. Rollback images past their window are "over" whatever their size: the
    window, not the size, is their budget."""
    rows = []
    for cat in CATEGORIES:
        raw = measured.get(cat.key)
        gb = None if raw is None else round(max(0.0, float(raw)), 2)
        budget = cat.budget(test_server)
        level = "unknown"
        what = ""
        if gb is not None:
            if cat.key == "rollback_images" and gb >= 0.05 and rollback_age_hours is not None \
                    and rollback_age_hours > keep_hours:
                level = "over"
                what = (f"Rollback images: {gb:.1f} GB, kept {rollback_age_hours / 24:.1f} days "
                        f"after the upgrade; the window is {keep_hours:g} hours.")
            elif budget <= 0:
                level = "over" if gb >= 0.05 else "ok"
            elif gb > budget:
                level = "over"
            elif gb >= WARN_AT * budget:
                level = "near"
            else:
                level = "ok"
            if level in ("near", "over") and not what:
                if budget <= 0:
                    what = (f"{cat.label}: {gb:.1f} GB with no test Postgres running, where the "
                            "budget is none.")
                else:
                    what = (f"{cat.label}: {gb:.1f} GB, "
                            + ("over" if level == "over" else "near")
                            + f" the {budget:g} GB budget.")
        rows.append({"key": cat.key, "label": cat.label, "gb": gb, "budget_gb": budget,
                     "pct": (round(100.0 * gb / budget, 1) if gb is not None and budget > 0 else None),
                     "level": level, "bounded_by": cat.bounded_by,
                     "what": what, "action": cat.action if level in ("near", "over") else ""})
    return rows


def total(rows: Sequence[Mapping[str, Any]]) -> dict[str, float]:
    used = sum(float(r["gb"] or 0) for r in rows)
    budget = sum(float(r["budget_gb"] or 0) for r in rows if r["key"] != "rollback_images")
    return {"gb": round(used, 2), "budget_gb": round(budget, 2)}


# ── measuring on the host (boss doctor) ─────────────────────────────────────────────────────

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


def _run(argv: Sequence[str]) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(list(argv), capture_output=True, text=True, timeout=300, check=False)


def _json_lines(text_: str) -> list[Any]:
    out = []
    for line in (text_ or "").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def image_layer_sizes(docker: Sequence[str], ref: str, run: Runner = _run) -> dict[str, float]:
    """{layer diff id: bytes} for one image: RootFS layers aligned with the non-empty history
    rows (oldest first). {} when the two do not line up (then the caller falls back)."""
    layers = run([*docker, "image", "inspect", "--format", "{{json .RootFS.Layers}}", ref])
    hist = run([*docker, "history", "--no-trunc", "--format", "{{.Size}}", ref])
    if layers.returncode != 0 or hist.returncode != 0:
        return {}
    try:
        ids = [str(x) for x in json.loads(layers.stdout or "[]")]
    except ValueError:
        return {}
    sizes = [parse_size(s) for s in reversed((hist.stdout or "").split())]
    nonzero = [s for s in sizes if s > 0]
    if len(nonzero) == len(ids):
        return dict(zip(ids, nonzero))
    if len(sizes) == len(ids):
        return dict(zip(ids, sizes))
    return {}


def measure_images(docker: Sequence[str], *, tag: str, run: Runner = _run) -> dict[str, float]:
    """{"images": current release GB, "rollback_images": prev-<tag> GB}. Each layer counts once,
    for the current release when any current (or running) image holds it, else for the rollback
    set. Docker's own list counts a shared layer once per image (the "~30 GB" of the report)."""
    res = run([*docker, "images", "--format", "{{json .}}"])
    rows = [r for r in _json_lines(res.stdout) if isinstance(r, dict)]
    current, rollback = [], []
    for r in rows:
        repo, t = str(r.get("Repository") or ""), str(r.get("Tag") or "")
        if not repo.startswith("boss-community-"):
            continue
        ref = f"{repo}:{t}"
        if t == f"prev-{tag}":
            rollback.append((ref, parse_size(r.get("Size"))))
        elif t == tag or repo == "boss-community-db":
            current.append((ref, parse_size(r.get("Size"))))
    layers: dict[str, dict[str, float]] = {}
    for ref, _ in current + rollback:
        layers[ref] = image_layer_sizes(docker, ref, run)
    if all(layers.get(ref) for ref, _ in current + rollback):
        held = {lid for ref, _ in current for lid in layers[ref]}
        cur = {lid: s for ref, _ in current for lid, s in layers[ref].items()}
        rb = {lid: s for ref, _ in rollback for lid, s in layers[ref].items() if lid not in held}
        return {"images": sum(cur.values()) / GB, "rollback_images": sum(rb.values()) / GB}
    # Fallback: Docker's per-image sizes (an over-count when layers are shared).
    return {"images": sum(s for _, s in current) / GB, "rollback_images": sum(s for _, s in rollback) / GB}


def measure_docker(docker: Sequence[str], *, project: str, tag: str,
                   run: Runner = _run) -> dict[str, Optional[float]]:
    """The host categories, in GB. A category Docker cannot report is None (shown as unknown)."""
    out: dict[str, Optional[float]] = {k: None for k in
                                       ("images", "rollback_images", "build_cache", "backups",
                                        "sandboxes", "volumes_other", "dangling_volumes",
                                        "logs", "test_volumes", "db_volume")}
    try:
        out.update(measure_images(docker, tag=tag, run=run))
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        df = run([*docker, "system", "df", "--format", "{{json .}}"])
        for row in _json_lines(df.stdout):
            kind = str(row.get("Type") or "")
            if kind.startswith("Build Cache"):
                out["build_cache"] = parse_size(row.get("Size")) / GB
            elif kind.startswith("Containers"):
                out["logs"] = parse_size(row.get("Size")) / GB
        vols = run([*docker, "system", "df", "-v", "--format", "{{json .Volumes}}"])
        sizes: dict[str, float] = {}
        for data in _json_lines(vols.stdout):
            for v in data if isinstance(data, list) else []:
                if isinstance(v, dict) and v.get("Name"):
                    sizes[str(v["Name"])] = parse_size(v.get("Size"))
        dangling = set((run([*docker, "volume", "ls", "-q", "--filter", "dangling=true"]).stdout
                        or "").split())
    except (OSError, subprocess.TimeoutExpired):
        return out
    if sizes:
        prefix = f"{project}_"
        by: dict[str, float] = {"backups": 0.0, "sandboxes": 0.0, "volumes_other": 0.0,
                                "dangling_volumes": 0.0, "test_volumes": 0.0, "db_volume": 0.0}
        for name, size in sizes.items():
            key = name[len(prefix):] if name.startswith(prefix) else None
            if key == "boss_community_pg":
                by["db_volume"] += size
            elif key == "boss_community_backups":
                by["backups"] += size
            elif key == "boss_community_sandboxes":
                by["sandboxes"] += size
            elif key in TEST_VOLUME_KEYS:
                by["test_volumes"] += size
            elif key is not None:
                by["volumes_other"] += size
            elif name in dangling:
                by["dangling_volumes"] += size
        out.update({k: v / GB for k, v in by.items()})
    return out


def measure_db(psql: Sequence[str], run: Runner = _run) -> dict[str, Optional[float]]:
    """The database split through `psql -Atc` (the doctor): records / knowledge / telemetry, and
    the test databases on the live server."""
    got: dict[str, Optional[float]] = {}
    for key, sql in DB_SQL.items():
        try:
            res = run([*psql, "-Atc", sql])
            got[key] = float((res.stdout or "").strip() or "nan") if res.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired, ValueError):
            got[key] = None
        if got[key] is not None and got[key] != got[key]:   # NaN
            got[key] = None
    return split_db(got)


def split_db(raw: Mapping[str, Optional[float]]) -> dict[str, Optional[float]]:
    """Bytes from DB_SQL → the GB categories (records = the rest of the live database)."""
    tot, kg, tel = raw.get("db_total"), raw.get("db_knowledge"), raw.get("db_telemetry")
    out: dict[str, Optional[float]] = {
        "db_knowledge": None if kg is None else kg / GB,
        "db_telemetry": None if tel is None else tel / GB,
        "test_databases": None if raw.get("test_databases") is None else float(raw["test_databases"]) / GB,
    }
    out["db_records"] = (None if tot is None else max(0.0, tot - (kg or 0.0) - (tel or 0.0)) / GB)
    return out


def rollback_age_hours(install_dir: Path, now: Optional[datetime] = None) -> Optional[float]:
    """Hours since upgrade.sh kept the rollback images (.boss-rollback BOSS_PREV_SAVED_AT); None
    when there is no rollback point or it was already dropped."""
    record = read_env_file(install_dir / ".boss-rollback")
    if record.get("BOSS_PREV_EXPIRED_AT") or not record.get("BOSS_PREV_SAVED_AT"):
        return None
    try:
        saved = datetime.fromisoformat(record["BOSS_PREV_SAVED_AT"].replace("Z", "+00:00"))
    except ValueError:
        return None
    if saved.tzinfo is None:
        saved = saved.replace(tzinfo=timezone.utc)
    return ((now or datetime.now(timezone.utc)) - saved).total_seconds() / 3600.0


def read_env_file(path: Path) -> dict[str, str]:
    """KEY=value lines (no interpolation, no shell); a missing file is {}."""
    out: dict[str, str] = {}
    try:
        text_ = path.read_text()
    except OSError:
        return out
    for line in text_.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip().removeprefix("export ").strip()] = v.strip().strip("\"'")
    return out


def host_report(install_dir: Path, *, docker: Sequence[str] = ("docker",),
                run: Runner = _run) -> dict[str, Any]:
    env = {**read_env_file(install_dir / ".env"),
           **{k: v for k, v in os.environ.items() if k == ROLLBACK_KEEP_ENV}}
    project = env.get("COMPOSE_PROJECT_NAME") or "boss-community"
    tag = env.get("BOSS_COMMUNITY_IMAGE_TAG") or project
    try:
        test_server = bool((run([*docker, "ps", "-aq", "--filter",
                                 f"label=com.docker.compose.project={project}", "--filter",
                                 "label=com.docker.compose.service=db-test"]).stdout or "").strip())
    except (OSError, subprocess.TimeoutExpired):
        test_server = False
    measured = measure_docker(docker, project=project, tag=tag, run=run)
    db = measure_db([*docker, "exec", f"{project}-db-1", "psql", "-U", "boss", "-d", "boss"], run=run)
    measured.update({k: db.get(k) for k in ("db_records", "db_knowledge", "db_telemetry")})
    tv, td = measured.pop("test_volumes", None), db.get("test_databases")
    measured["test_data"] = None if tv is None and td is None else (tv or 0.0) + (td or 0.0)
    measured.pop("db_volume", None)
    rows = evaluate(measured, test_server=test_server, rollback_age_hours=rollback_age_hours(install_dir),
                    keep_hours=rollback_keep_hours(env))
    return {"tier": env.get("BOSS_INSTALL_TIER") or "unset", "test_server": test_server,
            "rows": rows, "total": total(rows)}


def _lines(report: Mapping[str, Any]) -> list[str]:
    """`ok|warn|note <sentence>` lines for checkup.sh."""
    out = []
    t = report["total"]
    calm = all(r["level"] in ("ok", "unknown") for r in report["rows"])
    out.append(f"{'ok' if calm else 'note'} disk: BOSS uses {t['gb']:.1f} GB (budget "
               f"{t['budget_gb']:g} GB; docs/operations/DISK_BUDGET.md)")
    for r in report["rows"]:
        if r["level"] in ("near", "over"):
            out.append(f"warn disk: {r['what']} {r['action']}")
        elif r["level"] == "unknown":
            continue
    measured = [r for r in report["rows"] if r["gb"] is not None]
    if measured:
        biggest = sorted(measured, key=lambda r: -(r["gb"] or 0))[:4]
        out.append("note disk: biggest — " + ", ".join(f"{r['label'].lower()} {r['gb']:.1f} GB"
                                                       for r in biggest))
    return out


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Where this BOSS install's disk goes, against its budget.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    rep = sub.add_parser("report")
    rep.add_argument("--install-dir", required=True)
    rep.add_argument("--json", action="store_true")
    rep.add_argument("--sudo", action="store_true", help="run docker through sudo -n")
    sub.add_parser("budget", help="print the budget table")
    args = ap.parse_args(argv)
    if args.cmd == "budget":
        for c in CATEGORIES:
            dev = (f" (with a test Postgres {c.test_server_budget_gb:g})"
                   if c.test_server_budget_gb is not None else "")
            print(f"{c.label:<28} {c.budget_gb:>5g} GB{dev}  {c.bounded_by}")
        return 0
    docker = ("sudo", "-n", "docker") if args.sudo else ("docker",)
    report = host_report(Path(args.install_dir), docker=docker)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print("\n".join(_lines(report)))
    return 0


if __name__ == "__main__":  # pragma: no cover - run by checkup.sh
    sys.exit(main())
