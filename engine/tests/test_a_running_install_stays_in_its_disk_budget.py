"""A RUNNING INSTALL STAYS IN ITS DISK BUDGET (BOSSAPP-1459).

An enterprise install reached about 47 GB, most of it grown by itself while BOSS ran: a second
image set kept for `boss rollback` for ever, test data on an install that runs no tests, hourly
backups with no size cap, sandboxes of runs that died, anonymous Postgres volumes, and a table that
kept its size after its rows went. These pin the class, not the instances:

* every append-only table in db/schema.sql has an owner (a retention, or why it is kept);
* every budget category says what bounds it, and every warning passes the failure_words contract;
* scheduled backups past their cap raise ONE storage decision; approving it removes exactly those
  (and the ticked rule makes the next ones automatic); the newest three always stay;
* a sandbox directory is reaped only when nothing holds it and nothing touched it for 24 h;
* test databases on the live server go only when no test Postgres runs and BOSS itself is not on
  a test database;
* prune.sh drops the rollback images only past the window and only while the API is healthy;
* every container BOSS-side tooling removes takes its anonymous volumes (`rm -v`).

Database tests run on a fresh `boss_scratch_*` database of BOSS_TEST_DATABASE_URL's server.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text

from boss_engine.services import backup_scheduler as bs
from boss_engine.services import disk_budget as dbu
from boss_engine.services import resource_reaper as rr
from boss_engine.services import sandbox_identity
from boss_engine.services import telemetry_retention as tr
from boss_engine.services.failure_words import problems

REPO = Path(__file__).resolve().parents[2]
SCHEMA = (REPO / "db" / "schema.sql").read_text()
INSTALL = REPO / "scripts" / "install"

#: What a table that only grows is called: logs, events, ticks, samples, signals, attempts, audits.
APPEND_ONLY = re.compile(r"(_events?|_logs?|_samples?|_scrapes|_ticks|_signals|_audit|_history|_turns"
                         r"|_calls|_heartbeats|_attempts|_spans|_reasoning|_archive)$|^(events|receipts)$")


# ── the census ──────────────────────────────────────────────────────────────────────────────

def _schema_tables() -> set[str]:
    return set(re.findall(r"CREATE TABLE (?:IF NOT EXISTS )?([a-z_0-9]+)", SCHEMA))


def test_every_append_only_table_has_an_owner():
    shaped = {t for t in _schema_tables() if APPEND_ONLY.search(t)}
    assert len(shaped) > 30          # the pattern still finds the tables it was written for
    missing = sorted(shaped - set(tr.RETENTION_OWNERS))
    assert not missing, ("A table that only grows needs a retention or a reason it is kept — add it "
                         "to telemetry_retention.RETENTION_OWNERS: " + ", ".join(missing))
    for table, owner in tr.RETENTION_OWNERS.items():
        assert owner.strip(), table
        assert table in _schema_tables() or table.startswith("events"), table


def test_every_policy_table_exists_has_an_id_and_its_timestamp():
    tables = _schema_tables()
    for p in tr.POLICIES:
        assert p.table in tables, p.table
        body = re.search(r"CREATE TABLE (?:IF NOT EXISTS )?" + p.table + r"\s*\((.*?)\n\)", SCHEMA, re.S).group(1)
        assert re.search(r"^\s*id\s+TEXT\b", body, re.M), f"{p.table}: batched deletes key on a text id"
        assert re.search(r"^\s*" + p.ts + r"\s+TIMESTAMPTZ", body, re.M), f"{p.table}.{p.ts}"
        if not p.partitioned and p.table != "events":
            assert any(t == p.table and c == p.ts for _, t, c in tr.TS_INDEXES), f"{p.table}: no ts index"


def test_the_budget_counts_every_table_a_retention_owns_as_telemetry():
    owned = {t for t, o in tr.RETENTION_OWNERS.items() if not o.startswith("kept")} | {"events_archive"}
    assert owned <= set(dbu.TELEMETRY_TABLES), sorted(owned - set(dbu.TELEMETRY_TABLES))


# ── the budget and its words ────────────────────────────────────────────────────────────────

def test_every_category_says_what_bounds_it_and_its_warning_is_usable():
    keys = [c.key for c in dbu.CATEGORIES]
    assert len(keys) == len(set(keys))
    for cat in dbu.CATEGORIES:
        assert cat.bounded_by.strip() and cat.action.strip(), cat.key
        over = {k: None for k in keys}
        over[cat.key] = cat.budget() * 2 + 1
        row = next(r for r in dbu.evaluate(over) if r["key"] == cat.key)
        assert row["level"] == "over", cat.key
        assert problems(what=row["what"], action=row["action"]) == [], (cat.key, row)


def test_a_category_warns_at_eighty_percent_and_unmeasured_is_unknown():
    rows = {r["key"]: r for r in dbu.evaluate({"images": 8.0, "build_cache": 5.0, "backups": 5.5})}
    assert rows["images"]["level"] == "near"          # 8 of 10: warned before it is reached
    assert rows["build_cache"]["level"] == "ok"
    assert rows["backups"]["level"] == "over"
    assert rows["sandboxes"]["level"] == "unknown" and rows["sandboxes"]["gb"] is None
    assert problems(what=rows["images"]["what"], action=rows["images"]["action"]) == []


def test_doctor_lists_each_healthy_budget_and_does_not_price_an_unavailable_measurement():
    rows = dbu.evaluate({"images": 6.5, "rollback_images": 6.8, "backups": 0})
    lines = dbu._lines({"rows": rows, "total": dbu.total(rows)})
    assert any("Rollback images: 6.8 GB" in line for line in lines)
    assert any("Backups: 0 GB" in line for line in lines)
    assert any("Test databases: measurement unavailable" in line for line in lines)


def test_rollback_images_past_their_window_are_over_whatever_their_size():
    rows = {r["key"]: r for r in dbu.evaluate({"rollback_images": 2.0}, rollback_age_hours=100)}
    assert rows["rollback_images"]["level"] == "over"
    assert "the window is 72 hours" in rows["rollback_images"]["what"]
    assert problems(what=rows["rollback_images"]["what"], action=rows["rollback_images"]["action"]) == []
    inside = {r["key"]: r for r in dbu.evaluate({"rollback_images": 2.0}, rollback_age_hours=10)}
    assert inside["rollback_images"]["level"] == "ok"


def test_test_data_has_no_budget_unless_a_test_postgres_runs():
    without = {r["key"]: r for r in dbu.evaluate({"test_data": 0.3})}
    assert without["test_data"]["level"] == "over"
    assert problems(what=without["test_data"]["what"], action=without["test_data"]["action"]) == []
    with_server = {r["key"]: r for r in dbu.evaluate({"test_data": 4.8}, test_server=True)}
    assert with_server["test_data"]["level"] == "ok"


def test_docker_sizes_parse():
    assert dbu.parse_size("1.85GB") == pytest.approx(1.85e9)
    assert dbu.parse_size("24.6kB") == pytest.approx(24600)
    assert dbu.parse_size("0B") == 0 and dbu.parse_size("garbage") == 0
    assert dbu.parse_size("2GiB") == 2 * 1024 ** 3


def test_image_bytes_count_each_layer_once_and_shared_layers_belong_to_the_running_release():
    """Docker lists a shared layer once per image: the "~30 GB" of rollback images was ~7 real."""
    images = [{"Repository": "boss-community-api", "Tag": "t", "Size": "6GB"},
              {"Repository": "boss-community-worker", "Tag": "t", "Size": "6GB"},
              {"Repository": "boss-community-api", "Tag": "prev-t", "Size": "6GB"},
              {"Repository": "boss-community-worker", "Tag": "prev-t", "Size": "6GB"},
              {"Repository": "postgres", "Tag": "16", "Size": "1GB"}]
    layers = {"boss-community-api:t": ["base", "cur"], "boss-community-worker:t": ["base", "cur"],
              "boss-community-api:prev-t": ["base", "old"], "boss-community-worker:prev-t": ["base", "old"]}
    sizes = {"base": "4GB", "cur": "2GB", "old": "2GB"}

    def run(argv):
        if argv[1] == "images":
            return subprocess.CompletedProcess(argv, 0, "\n".join(json.dumps(i) for i in images), "")
        ref = argv[-1]
        if argv[1:3] == ["image", "inspect"]:
            return subprocess.CompletedProcess(argv, 0, json.dumps(layers[ref]), "")
        # history: newest first, with an empty layer between
        hist = [sizes[x] for x in reversed(layers[ref])]
        return subprocess.CompletedProcess(argv, 0, "\n".join([hist[0], "0B", *hist[1:]]), "")

    got = dbu.measure_images(("docker",), tag="t", run=run)
    assert got["images"] == pytest.approx(6.0)        # base + cur, once
    assert got["rollback_images"] == pytest.approx(2.0)  # only what dropping them frees


def test_the_rollback_age_is_read_from_the_record_and_a_dropped_point_has_none(tmp_path):
    now = datetime(2026, 11, 24, 12, tzinfo=timezone.utc)
    rec = tmp_path / ".boss-rollback"
    rec.write_text("BOSS_PREV_SAVED_AT=2026-11-21T12:00:00Z\n")
    assert dbu.rollback_age_hours(tmp_path, now) == pytest.approx(72)
    rec.write_text("BOSS_PREV_SAVED_AT=2026-11-21T12:00:00Z\nBOSS_PREV_EXPIRED_AT=2026-11-24T00:00:00Z\n")
    assert dbu.rollback_age_hours(tmp_path, now) is None
    assert dbu.rollback_age_hours(tmp_path / "nowhere", now) is None


# ── backups past the cap (pure) ─────────────────────────────────────────────────────────────

T0 = datetime(2026, 11, 24, 10, 0, tzinfo=timezone.utc)


def _rows(n: int, gb_each: float) -> list[dict]:
    return [{"id": f"b{i}", "started_at": T0 - timedelta(hours=i), "bytes": gb_each * 1e9} for i in range(n)]


def test_the_cap_is_monotone_a_big_newer_backup_never_leaves_an_older_one_kept():
    rows = [{"id": "b0", "started_at": T0, "bytes": 1e9}, {"id": "b1", "started_at": T0 - timedelta(hours=1), "bytes": 1e9},
            {"id": "b2", "started_at": T0 - timedelta(hours=2), "bytes": 1e9},
            {"id": "b3", "started_at": T0 - timedelta(hours=3), "bytes": 4e9},
            {"id": "b4", "started_at": T0 - timedelta(hours=4), "bytes": 0.1e9}]
    assert [r["id"] for r in bs.over_cap(rows, max_count=12, cap_gb=5)] == ["b3", "b4"]


def test_other_numbers_on_the_card_never_delete_what_they_keep(monkeypatch):
    """"Keep 20 instead": the card listed b12, b13 under keep 12; with 20 nothing is past it."""
    rows = [{"id": f"b{i}", "started_at": T0 - timedelta(hours=i), "bytes": 1} for i in range(14)]

    class Conn:
        def __init__(self):
            self.updates = []

        def execute(self, stmt, params=None):
            sql = str(stmt)
            if "UPDATE backups" in sql:
                self.updates.append(params["i"])
            data = rows if "status = 'verified'" in sql and "ANY" not in sql and "LIMIT" not in sql else []
            return SimpleNamespace(mappings=lambda: [dict(r) for r in data], all=lambda: [])

    monkeypatch.setattr(bs, "s3_remover", lambda settings: None)
    conn = Conn()
    rec = {"tool": bs.CAP_TOOL, "to_delete": ["b12", "b13"],
           "rule_proposal": {"kind": bs.CAP_RULE_KIND, "rule": {"keep_newest": 12, "cap_gb": 5}}}
    got = bs.on_cap_decision(conn, {"id": "d1", "recommendation": rec}, verdict="approve", actor="p",
                             make_rule=False, args={"keep_newest": 20})
    assert got["removed"] == [] and conn.updates == []


def test_the_cap_takes_the_oldest_past_the_count_or_the_bytes_never_the_newest_three():
    assert [r["id"] for r in bs.over_cap(_rows(14, 0.1), max_count=12, cap_gb=5)] == ["b12", "b13"]
    assert [r["id"] for r in bs.over_cap(_rows(13, 0.5), max_count=12, cap_gb=5)] == ["b10", "b11", "b12"]
    # three huge ones are still kept: the floor beats the cap
    assert bs.over_cap(_rows(3, 10), max_count=12, cap_gb=5) == []
    assert [r["id"] for r in bs.over_cap(_rows(5, 10), max_count=12, cap_gb=5)] == ["b3", "b4"]


# ── prune.sh: the rollback window and the test volume ───────────────────────────────────────

FAKE_DOCKER = r"""#!/usr/bin/env bash
printf '%s\n' "$*" >> "$FAKE_DOCKER_LOG"
case "$1" in
  info) exit 0 ;;
  ps)
    if [[ "$*" == *"health=healthy"* ]]; then [[ -n "${FAKE_HEALTHY:-}" ]] && echo cid1; exit 0; fi
    if [[ "$*" == *"volume="* ]]; then [[ -n "${FAKE_VOLUME_USED:-}" ]] && echo cid2; exit 0; fi
    [[ "$*" == *"{{.Image}}"* ]] && printf 'boss-community-api:boss-community\n'
    exit 0 ;;
  image)
    # an image this fake already removed is gone
    if [[ "$2" == inspect ]]; then grep -qxF "rmi ${@: -1}" "$FAKE_DOCKER_LOG" && exit 1; exit 0; fi
    exit 0 ;;
  volume)
    if [[ "$2" == inspect ]]; then
      case "${@: -1}" in
        boss-community_boss_community_pg_test_v2) echo 'boss-community|boss_community_pg_test_v2'; exit 0 ;;
        boss-community_boss_community_pg_test) echo 'someone-else|boss_community_pg_test'; exit 0 ;;
      esac
      exit 1
    fi
    exit 0 ;;
  *) exit 0 ;;
esac
"""


def _world(tmp_path: Path, saved_hours_ago: float, **env_over) -> tuple[dict, Path]:
    bindir = tmp_path / "bin"
    bindir.mkdir(parents=True)
    for name, body in (("docker", FAKE_DOCKER),
                       ("df", "#!/bin/sh\nprintf 'F 1024-blocks Used Available Capacity M\\n"
                              "/dev/x 100000000 50000000 1000 50%% /\\n'\n")):
        (bindir / name).write_text(body)
        (bindir / name).chmod(0o755)
    install = tmp_path / "install"
    (install / "scripts" / "install").mkdir(parents=True)
    saved = (datetime.now(timezone.utc) - timedelta(hours=saved_hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    (install / ".boss-rollback").write_text(
        "BOSS_PREV_COMMIT=" + "a" * 40 + "\nBOSS_PREV_REF=v1.0-rc9\nBOSS_IMAGE_TAG=boss-community\n"
        "BOSS_PREV_IMAGE_TAG=prev-boss-community\nBOSS_PREV_IMAGES=boss-community-api boss-community-web\n"
        f"BOSS_PREV_SAVED_AT={saved}\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("BOSS_", "FAKE_"))}
    env.update(PATH=f"{bindir}{os.pathsep}{env['PATH']}", FAKE_DOCKER_LOG=str(tmp_path / "docker.log"),
               **env_over)
    return env, install


def _prune(env: dict, install: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(INSTALL / "prune.sh"), "--dir", str(install), *extra],
                          capture_output=True, text=True, env=env, timeout=60)


def _removed(tmp_path: Path) -> list[str]:
    log = (tmp_path / "docker.log").read_text().splitlines()
    return [ln for ln in log if ln.startswith("rmi ") or ln.startswith("volume rm ")]


def test_prune_keeps_the_rollback_images_inside_the_window(tmp_path):
    env, install = _world(tmp_path, saved_hours_ago=10, FAKE_HEALTHY="1")
    out = _prune(env, install)
    assert out.returncode == 0, out.stderr
    assert not any("prev-boss-community" in ln for ln in _removed(tmp_path))
    assert "BOSS_PREV_EXPIRED_AT" not in (install / ".boss-rollback").read_text()


def test_prune_drops_the_rollback_images_past_the_window_when_healthy_and_marks_the_record(tmp_path):
    env, install = _world(tmp_path, saved_hours_ago=80, FAKE_HEALTHY="1")
    out = _prune(env, install)
    assert out.returncode == 0, out.stderr
    removed = _removed(tmp_path)
    assert "rmi boss-community-api:prev-boss-community" in removed
    assert "rmi boss-community-web:prev-boss-community" in removed
    assert "BOSS_PREV_EXPIRED_AT=" in (install / ".boss-rollback").read_text()
    assert "dropped the rollback images of v1.0-rc9" in out.stdout
    # a second prune does nothing more
    (tmp_path / "docker.log").write_text("")
    _prune(env, install)
    assert not any("prev-" in ln for ln in _removed(tmp_path))


def test_an_unhealthy_release_keeps_its_way_back(tmp_path):
    env, install = _world(tmp_path, saved_hours_ago=200)
    out = _prune(env, install)
    assert not any("prev-" in ln for ln in _removed(tmp_path))
    assert "not healthy" in out.stdout


def test_the_window_is_a_setting_and_a_dry_run_changes_nothing(tmp_path):
    env, install = _world(tmp_path, saved_hours_ago=10, FAKE_HEALTHY="1", BOSS_ROLLBACK_KEEP_HOURS="5")
    out = _prune(env, install, "--dry-run")
    assert "would run: docker rmi boss-community-api:prev-boss-community" in out.stdout
    assert "BOSS_PREV_EXPIRED_AT" not in (install / ".boss-rollback").read_text()


def _steward():
    import importlib.util

    spec = importlib.util.spec_from_file_location("boss_steward_t", INSTALL / "steward.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_test_volume_goes_only_when_no_container_uses_it_and_only_this_projects(tmp_path):
    st = _steward()
    (tmp_path / ".env").write_text("COMPOSE_PROJECT_NAME=boss-community\nBOSS_INSTALL_ID=inst_x\n")
    env = st.Env(str(tmp_path))
    labels = {"boss-community_boss_community_pg_test_v2": ("boss-community", "boss_community_pg_test_v2"),
              "boss-community_boss_community_pg_test": ("someone-else", "boss_community_pg_test")}
    users: dict[str, list[str]] = {}
    calls: list[list[str]] = []

    def run(argv, timeout=None):
        calls.append(list(argv))
        if argv[1:3] == ["volume", "inspect"]:
            got = labels.get(argv[-1])
            if got is None:
                return subprocess.CompletedProcess(argv, 1, "", "no such volume")
            which = 0 if "project" in argv[-2] else 1
            return subprocess.CompletedProcess(argv, 0, got[which] + "\n", "")
        if argv[1:3] == ["ps", "-a"]:
            name = argv[argv.index("--filter") + 1].split("=", 1)[1]
            return subprocess.CompletedProcess(argv, 0, "\n".join(
                json.dumps({"ID": c, "Names": c, "State": "exited", "Labels": ""}) for c in users.get(name, [])), "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    users["boss-community_boss_community_pg_test_v2"] = ["db-test-1"]
    kept = st.remove_test_leftovers(env, run)
    assert [r["action"] for r in kept] == ["kept"]               # a stopped db-test still keeps it
    assert not any(c[1:3] == ["volume", "rm"] for c in calls)
    users.clear()
    assert st.remove_test_leftovers(env, run, apply=False)[0]["action"] == "would_remove"
    assert not any(c[1:3] == ["volume", "rm"] for c in calls)
    done = st.remove_test_leftovers(env, run)
    assert [r["action"] for r in done] == ["removed"]
    removed = [c[-1] for c in calls if c[1:3] == ["volume", "rm"]]
    # the retired key here belongs to another project (its Compose labels say so): untouched
    assert removed == ["boss-community_boss_community_pg_test_v2"]


def test_prune_itself_never_touches_a_volume():
    code = "\n".join(ln for ln in (INSTALL / "prune.sh").read_text().splitlines() if not ln.lstrip().startswith("#"))
    assert "volume rm" not in code and "volume inspect" not in code
    assert "--test-leftovers" in (INSTALL / "boss.sh").read_text()
    assert "--test-leftovers" in (INSTALL / "upgrade.sh").read_text()


def test_rollback_names_the_way_back_once_the_images_were_dropped():
    text_ = (INSTALL / "rollback.sh").read_text()
    assert "BOSS_PREV_EXPIRED_AT" in text_ and "boss upgrade --to $PREV_REF" in text_
    assert problems(what="The rollback images of v1 were dropped after the upgrade had run healthy.",
                    action="Go back with `boss upgrade --to v1` instead: it rebuilds that release.") == []


# ── anonymous volumes go with their containers ──────────────────────────────────────────────

def test_containers_are_removed_with_their_anonymous_volumes():
    for rel in ("engine/boss_engine/services/resource_reaper.py", "engine/boss_engine/services/container_runner.py",
                "engine/boss_engine/services/toolchain_images.py", "engine/boss_engine/services/mcp_stdio_supervisor.py",
                "engine/boss_engine/services/preview_service.py", "tests/e2e/harness/teardown.py"):
        src = (REPO / rel).read_text()
        assert not re.search(r'(?<!"volume", )"rm", "-f", (?!"-v")', src), f"{rel}: docker rm -f without -v"
    gate = (REPO / "scripts" / "e2e-gate.sh").read_text()
    assert 'docker rm -f "$PG"' not in gate and 'docker rm -f -v "$PG"' in gate
    assert "volume-label=boss-e2egate=1" in gate


# ── the sandbox reaper ──────────────────────────────────────────────────────────────────────

@pytest.fixture()
def sandbox_root(tmp_path, monkeypatch):
    root = tmp_path / "sandboxes"
    root.mkdir()
    shim = tmp_path / "shim"
    shim.mkdir()
    monkeypatch.setattr(sandbox_identity, "UID_BASE", os.getuid())
    monkeypatch.setattr(sandbox_identity, "UID_COUNT", 1)
    monkeypatch.setattr(sandbox_identity, "SHIM_ROOT", str(shim))
    monkeypatch.setattr(sandbox_identity, "helper_path", lambda: None)
    monkeypatch.setattr(rr, "_uid_has_processes", lambda uid: False)
    return root


def _run_dir(root: Path, owner: str, *, age_hours: float) -> Path:
    d = root / sandbox_identity.owner_key(owner)
    (d / "home" / "work").mkdir(parents=True)
    (d / "home" / "work" / "f").write_text("x" * 1000)
    old = time.time() - age_hours * 3600
    for p in (d, d / "home", d / "home" / "work", d / "home" / "work" / "f"):
        os.utime(p, (old, old))
    return d


def test_a_sandbox_left_by_a_dead_run_is_reaped_after_a_day_and_nothing_else(sandbox_root, monkeypatch):
    dead = _run_dir(sandbox_root, "crun_dead", age_hours=30)
    young = _run_dir(sandbox_root, "crun_young", age_hours=2)
    live = _run_dir(sandbox_root, "prev_live", age_hours=30)
    stranger = sandbox_root / "not-a-key"
    stranger.mkdir()
    os.utime(stranger, (0, 0))
    monkeypatch.setattr(rr.owned_process, "live_owner_ids", lambda kind=None: {"prev_live"})
    got = rr.reap_sandbox_dirs(None, root=sandbox_root)
    assert got["removed"] == 1
    assert not dead.exists()
    assert young.exists() and live.exists() and stranger.exists()


def test_a_sandbox_whose_uid_lease_is_held_stays(sandbox_root, monkeypatch):
    import fcntl

    held = _run_dir(sandbox_root, "crun_held", age_hours=48)
    fd = os.open(os.path.join(sandbox_identity.SHIM_ROOT, f"uid-{os.getuid()}.lock"), os.O_CREAT | os.O_RDWR, 0o600)
    monkeypatch.setattr(rr.owned_process, "live_owner_ids", lambda kind=None: set())
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # another process's lease: a child holds the same lock
        child = subprocess.Popen(["python3", "-c", "import fcntl,os,sys,time;fd=os.open(sys.argv[1],os.O_RDWR);"
                                  "fcntl.flock(fd,fcntl.LOCK_EX);print('x',flush=True);time.sleep(30)",
                                  os.path.join(sandbox_identity.SHIM_ROOT, f"uid-{os.getuid()}.lock")],
                                 stdout=subprocess.PIPE, text=True)
        fcntl.flock(fd, fcntl.LOCK_UN)
        child.stdout.readline()
        try:
            assert rr.reap_sandbox_dirs(None, root=sandbox_root)["removed"] == 0
            assert held.exists()
        finally:
            child.kill()
            child.wait()
    finally:
        os.close(fd)


# ── database: test databases and the backup cap ─────────────────────────────────────────────

@pytest.fixture(scope="module")
def db():
    from boss_engine.db import ensure_schema
    from boss_engine.dev.test_databases import scratch_database

    url = os.environ.get("BOSS_TEST_DATABASE_URL", "")
    if not url or "test" not in url.rsplit("/", 1)[-1]:
        pytest.skip("BOSS_TEST_DATABASE_URL must name a disposable boss_test* database")
    scratch = scratch_database(url, "diskbudget")
    try:
        scratch_url = scratch.__enter__()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"cannot create the scratch database: {type(exc).__name__}")
    engine = create_engine(scratch_url, future=True)
    ensure_schema(engine)
    with engine.begin() as c:
        c.execute(text("INSERT INTO orgs (id, name) VALUES ('org_db', 'db') ON CONFLICT DO NOTHING"))
        c.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES ('ws_db', 'org_db', 'db') "
                       "ON CONFLICT DO NOTHING"))
        c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES "
                       "('p_db', 'ws_db', 'db', 'db') ON CONFLICT DO NOTHING"))
    try:
        yield engine, scratch_url
    finally:
        engine.dispose()
        scratch.__exit__(None, None, None)


def test_test_databases_stay_while_a_test_postgres_runs_or_boss_is_on_a_test_database(db):
    engine, _ = db
    assert rr.reap_test_databases(engine, test_server=True)["dropped"] == []
    # this BOSS runs on a boss_scratch_* database: its siblings are never its to drop
    got = rr.reap_test_databases(engine, test_server=False)
    assert got["dropped"] == [] and "test database" in got["kept"]


def test_a_test_database_nobody_uses_is_dropped_from_a_live_server(db, monkeypatch):
    engine, scratch_url = db
    monkeypatch.delenv("BOSS_ENVIRONMENT", raising=False)
    base = scratch_url.rsplit("/", 1)[0]
    stamp = uuid.uuid4().hex[:8]
    # stamped and past its 24 h TTL (dev/test_databases), stamped and young, unstamped (test-tier's)
    live = f"bossdisk_live_{stamp}"
    leftover = f"boss_scratch_diskleft{stamp}_20200101t000000"
    busy = f"boss_scratch_diskbusy{stamp}_20200101t000000"
    young = f"boss_scratch_diskyoung{stamp}_{datetime.now(timezone.utc):%Y%m%dt%H%M%S}"
    shard = f"boss_test_disk{stamp}_t1"
    admin = create_engine(scratch_url, future=True).execution_options(isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        for name in (live, leftover, busy, young, shard):
            c.execute(text(f'CREATE DATABASE "{name}"'))
    live_engine = create_engine(f"{base}/{live}", future=True)
    holder = create_engine(f"{base}/{busy}", future=True).connect()
    try:
        only = (leftover, busy, young, shard)
        t = time.time()
        got = rr.reap_test_databases(live_engine, test_server=False, only=only, now=t)
        assert got["dropped"] == [leftover]          # the young one and a busy one stay
        assert got["waiting"] == [shard]             # an unstamped one is only noted, first time
        assert rr.reap_test_databases(live_engine, test_server=False, only=only, now=t + 60)["dropped"] == []
        later = rr.reap_test_databases(live_engine, test_server=False, only=only, now=t + 3700)
        assert later["dropped"] == [shard]           # idle and unchanged for an hour: a leftover
        with admin.connect() as c:
            names = {r[0] for r in c.execute(text("SELECT datname FROM pg_database"))}
        assert leftover not in names and shard not in names and busy in names and young in names
    finally:
        holder.close()
        live_engine.dispose()
        with admin.connect() as c:
            for name in (live, leftover, busy, young, shard):
                c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def _backups(engine, tmp_path: Path, n: int, gb_each: float) -> list[Path]:
    paths = []
    with engine.begin() as c:
        for i in range(n):
            p = tmp_path / f"boss-bkp_{i}.dump"
            p.write_bytes(b"x")
            paths.append(p)
            c.execute(text("INSERT INTO backups (id, kind, path, started_at, status, bytes, verified_at) "
                           "VALUES (:i, 'scheduled', :p, :t, 'verified', :b, :t)"),
                      {"i": f"bkp_{tmp_path.name}_{i}", "p": str(p), "t": T0 - timedelta(hours=i),
                       "b": int(gb_each * 1e9)})
    return paths


def _settings(**over):
    base = dict(backup_keep_hourly=50, backup_keep_daily=0, backup_keep_weekly=0, backup_keep_monthly=0,
                backup_max_count=12, backup_cap_gb=5.0, backup_s3_upload=False, s3_bucket=None)
    base.update(over)
    return SimpleNamespace(**base)


def test_backups_past_the_cap_ask_once_and_the_answer_removes_exactly_them(db, tmp_path, monkeypatch):
    from boss_engine.services import storage_cleanup

    engine, _ = db
    monkeypatch.setenv("BOSS_INSTALL_TIER", "prod")
    monkeypatch.setattr(bs, "s3_remover", lambda settings: None)
    with engine.begin() as c:
        c.execute(text("DELETE FROM backups"))
        c.execute(text("DELETE FROM retention_rules WHERE kind IN ('backups', 'scheduled_backups')"))
    paths = _backups(engine, tmp_path, 6, 2.0)           # 12 GB: three past the 5 GB cap
    assert bs.prune(engine, _settings()) == []            # nothing deleted without a yes
    assert bs.prune(engine, _settings()) == []
    with engine.begin() as c:
        cards = c.execute(text("SELECT * FROM decisions WHERE recommendation->>'tool' = :t"),
                          {"t": bs.CAP_TOOL}).mappings().all()
    assert len(cards) == 1                                # asked once, not every tick
    rec = cards[0]["recommendation"]
    assert rec["action"] == "storage_delete" and rec["items"] == 3
    assert rec["to_delete"] == [f"bkp_{tmp_path.name}_{i}" for i in (3, 4, 5)]
    assert all(p.exists() for p in paths)
    with engine.begin() as c:
        got = storage_cleanup.on_delete_decision(c, engine, dict(cards[0]), verdict="approve",
                                                 actor="person:ops", make_rule=True)
    assert got["state"] == "done" and len(got["removed"]) == 3
    assert [p.exists() for p in paths] == [True, True, True, False, False, False]
    with engine.begin() as c:
        rule = c.execute(text("SELECT source, rule FROM retention_rules WHERE kind = 'scheduled_backups'")).first()
        host_rule = c.execute(text("SELECT 1 FROM retention_rules WHERE kind = 'backups'")).first()
    assert rule[0] == "learned"
    # its own kind: approving it never makes deleting the host's recovery archives automatic
    assert host_rule is None
    # next time it is automatic: the approved rule removes the oldest without a card
    (tmp_path / "n").mkdir()
    extra = _backups(engine, tmp_path / "n", 5, 2.0)
    removed = bs.prune(engine, _settings())
    assert removed and not all(p.exists() for p in extra)
    with engine.begin() as c:
        assert c.execute(text("SELECT count(*) FROM decisions WHERE recommendation->>'tool' = :t"),
                         {"t": bs.CAP_TOOL}).scalar() == 1


def test_compaction_rewrites_a_table_that_kept_its_size_after_its_rows_went(db, monkeypatch):
    engine, _ = db
    monkeypatch.setattr(tr, "COMPACT_MIN_BYTES", 1024 * 1024)
    with engine.begin() as c:
        c.execute(text("CREATE TABLE disk_churn (id TEXT PRIMARY KEY, pad TEXT)"))
        c.execute(text("ALTER TABLE disk_churn ALTER pad SET STORAGE PLAIN"))   # no compression
        c.execute(text("INSERT INTO disk_churn SELECT g::text, repeat('x', 2000) FROM generate_series(1, 4000) g"))
        c.execute(text("DELETE FROM disk_churn WHERE id::int > 20"))
        c.execute(text("CREATE TABLE disk_full (id TEXT PRIMARY KEY, pad TEXT)"))
        c.execute(text("ALTER TABLE disk_full ALTER pad SET STORAGE PLAIN"))
        c.execute(text("INSERT INTO disk_full SELECT g::text, repeat('y', 2000) FROM generate_series(1, 4000) g"))
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as c:
        c.execute(text("ANALYZE disk_churn"))
        c.execute(text("ANALYZE disk_full"))
    found = {x["table"] for x in tr.compact(engine, dry_run=True)}
    assert "disk_churn" in found and "disk_full" not in found    # live data is not a candidate
    done = {x["table"]: x for x in tr.compact(engine)}
    assert done["disk_churn"]["after"] < done["disk_churn"]["bytes"] / 5
    with engine.begin() as c:
        assert c.execute(text("SELECT count(*) FROM disk_churn")).scalar() == 20


def test_development_cap_uses_autonomy_for_scheduled_dumps_only(db, tmp_path, monkeypatch):
    from boss_engine.config import get_settings
    from boss_engine.services import autonomy_gate

    engine, _ = db
    monkeypatch.setenv("BOSS_INSTALL_TIER", "dev")
    monkeypatch.setenv("BOSS_AUTONOMY_IMMEDIATE", "0")
    with engine.begin() as c:
        c.execute(text("DELETE FROM backups"))
        c.execute(text("DELETE FROM retention_rules WHERE kind = 'scheduled_backups'"))
        c.execute(text("DELETE FROM decisions WHERE recommendation->>'tool' = :t"), {"t": bs.CAP_TOOL})
    paths = _backups(engine, tmp_path, 6, 2.0)
    # The operator's separate copy must survive both the age policy and the byte cap.
    with engine.begin() as c:
        c.execute(text("UPDATE backups SET kind='manual' WHERE path=:p"), {"p": str(paths[-1])})
    bs.prune(engine, _settings())
    with engine.begin() as c:
        row = c.execute(text("SELECT * FROM decisions WHERE recommendation->>'tool'=:t"),
                        {"t": bs.CAP_TOOL}).mappings().one()
        assert row["risk_tier"] == 2 and row["recommendation"]["autonomy"]["route"] == "act"
        assert "deleted storage" in row["recommendation"]["autonomy"]["undo"]
        assert autonomy_gate.decide(c, get_settings(), row["id"])["decided"]
    with engine.begin() as c:
        approved = c.execute(text("SELECT approved_by FROM decisions WHERE id=:d"), {"d": row["id"]}).scalar_one()
        assert approved.startswith(autonomy_gate.ACTOR_PREFIX)
        assert not c.execute(text("SELECT 1 FROM retention_rules WHERE kind='scheduled_backups'")).first()
    assert [p.exists() for p in paths] == [True, True, True, False, False, True]
