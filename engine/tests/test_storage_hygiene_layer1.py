"""Storage hygiene layer 1 (BOSSAPP-1145: ST-1..ST-5): BOSS keeps its own storage healthy.

One afternoon the operator's laptop ran out of room three times: 358 pre-upgrade backups
(170 GB) nothing ever removed, 29-34 GB of BOSS images five per upgrade, a build cache with no
cap, container logs with no cap, one slow query spilling 11 GB of Postgres temp files, and a
deploy that started on 7 GiB free and stopped half way. None of it was one mistake: nothing BOSS
creates had a retention rule or a guardrail. These tests pin the rules as classes, each with
its setting, against fake backups, a fake Docker and the real scripts; no daemon, no install.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
INSTALL = REPO / "scripts" / "install"
sys.path.insert(0, str(REPO / "engine"))
sys.path.insert(0, str(INSTALL))

import backup_archive  # noqa: E402  (scripts/install/backup_archive.py, the bundled helper)

yaml = pytest.importorskip("yaml")


def _sh(*args, env=None, check=True, cwd=None):
    return subprocess.run([str(a) for a in args], env=env, capture_output=True, text=True,
                          check=check, cwd=cwd, timeout=60)


def _exe(path: Path, body: str) -> Path:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


# ------------------------------------------------------------------------------------------ #
# ST-1 (BOSSAPP-1146): pre-upgrade backup retention
# ------------------------------------------------------------------------------------------ #

NOW = dt.datetime(2026, 11, 14, 12, 0, tzinfo=dt.timezone.utc)


def _backup(root: Path, days_ago: float, mb: int = 1, pid: int = 100) -> Path:
    when = NOW - dt.timedelta(days=days_ago)
    d = root / f"boss-{when.strftime('%Y%m%dT%H%M%SZ')}-{pid}"
    d.mkdir()
    (d / "boss.dump").write_bytes(b"\0" * (mb * 1_000_000))
    (d / "manifest.json").write_text("{}")
    return d


def _plan(root: Path, **kw):
    kw.setdefault("keep", 10)
    kw.setdefault("keep_days", 7)
    kw.setdefault("cap_bytes", 20e9)
    kept, removed = backup_archive.retention_plan(backup_archive.list_backups(root), now=NOW, **kw)
    return [b["path"].name for b in kept], [b["path"].name for b in removed]


def test_the_newest_ten_and_the_last_seven_days_stay_and_the_rest_go(tmp_path):
    # 20 backups, one a day: the newest 10 stay by count (days 0-9), 10-19 are older than 7 days
    names = [_backup(tmp_path, d, pid=d).name for d in range(20)]
    kept, removed = _plan(tmp_path)
    assert kept == names[:10]
    assert removed == list(reversed(names[10:]))             # oldest first
    # 30 backups within the last week stay even though they exceed "newest 10"
    week = tmp_path / "week"
    week.mkdir()
    for i in range(30):
        _backup(week, i / 5, pid=i)                           # every ~5 hours over 6 days
    kept, removed = _plan(week)
    assert len(kept) == 30 and removed == []


def test_the_cap_wins_over_count_and_age_oldest_first_never_the_backup_just_taken(tmp_path):
    names = [_backup(tmp_path, d, mb=3, pid=d).name for d in range(6)]   # 6 × 3 MB, all recent
    kept, removed = _plan(tmp_path, cap_bytes=13e6, protect=tmp_path / names[0])
    assert kept == names[:4]                                  # 12 MB fit under the 13 MB cap
    assert removed == list(reversed(names[4:]))
    # THE CAP NEVER GOES BELOW THE NEWEST 3: a rollback to the release before last stays possible
    kept, removed = _plan(tmp_path, cap_bytes=1e6, protect=tmp_path / names[0])
    assert kept == names[:3] and backup_archive.CAP_FLOOR == 3
    _, over = backup_archive.retention_plan(backup_archive.list_backups(tmp_path), keep=10,
                                            keep_days=7, cap_bytes=1e6, now=NOW)
    assert all(b["reason"] == "over the 0.0 GB cap" for b in over)   # one decimal, never "0 GB"
    # the protected backup is kept even when it is not among the newest
    kept, _ = _plan(tmp_path, cap_bytes=1e6, protect=tmp_path / names[4])
    assert kept == names[:3] + [names[4]]
    # and without --protect the newest three stay just the same
    kept, _ = _plan(tmp_path, cap_bytes=1e6)
    assert kept == names[:3]


def test_a_backup_stamped_in_the_future_is_not_the_newest_for_ever(tmp_path):
    future = _backup(tmp_path, -400, pid=9)                  # a host whose clock was wrong
    names = [_backup(tmp_path, d, pid=d).name for d in range(12)]
    kept, removed = _plan(tmp_path, keep=3, keep_days=0)
    assert kept == [future.name] + names[:2] and len(removed) == 10   # counted as taken now
    # but it does not displace the one really just taken
    kept, _ = _plan(tmp_path, keep=1, keep_days=0, protect=tmp_path / names[0])
    assert kept == [future.name, names[0]]


def test_only_directories_boss_named_are_candidates(tmp_path):
    newest = _backup(tmp_path, 30, pid=1)
    _backup(tmp_path, 40, pid=2)
    for other in ("legacy-pg-offline.Ab12Cd", "mine", "boss-notastamp-1", "boss-20261001T000000Z"):
        (tmp_path / other).mkdir()
        (tmp_path / other / "keep.me").write_text("x")
    (tmp_path / "boss-20261001T000000Z-7.txt").write_text("a file, not a backup")
    (tmp_path / "boss-20250101T000000Z-9").symlink_to(tmp_path / "mine")
    kept, removed = _plan(tmp_path, keep=1)
    assert kept == [newest.name] and len(removed) == 1       # the newest always stays
    _sh(sys.executable, INSTALL / "backup_archive.py", "retention", tmp_path, "--keep", "1")
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted([
        newest.name, "legacy-pg-offline.Ab12Cd", "mine", "boss-notastamp-1",
        "boss-20261001T000000Z", "boss-20261001T000000Z-7.txt", "boss-20250101T000000Z-9"])


def test_the_command_logs_one_line_per_removal_and_a_dry_run_removes_nothing(tmp_path):
    names = [_backup(tmp_path, d, pid=d).name for d in range(13)]
    dry = _sh(sys.executable, INSTALL / "backup_archive.py", "retention", tmp_path, "--dry-run",
              "--protect", tmp_path / names[0])
    assert dry.stdout == ""                                   # stdout stays the backup path's
    lines = [l for l in dry.stderr.splitlines() if "would remove" in l and "days old" in l]
    assert len(lines) == 3 and all(l.startswith("backup retention: would remove ") for l in lines)
    assert all((tmp_path / n).exists() for n in names)
    real = _sh(sys.executable, INSTALL / "backup_archive.py", "retention", tmp_path,
               "--protect", tmp_path / names[0])
    removed = [l for l in real.stderr.splitlines() if l.startswith("backup retention: removed ")]
    assert len(removed) == 3 and all("older than the newest 10" in l for l in removed)
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(names[:10])
    assert "kept 10" in real.stderr and "removed 3" in real.stderr
    # settings are validated, in the operator's words
    bad = _sh(sys.executable, INSTALL / "backup_archive.py", "retention", tmp_path, "--keep", "0",
              check=False)
    assert bad.returncode != 0 and "BOSS_BACKUP_KEEP" in bad.stderr
    # a missing directory is not an error (a first install has made no backup yet)
    none = _sh(sys.executable, INSTALL / "backup_archive.py", "retention", tmp_path / "nope")
    assert none.returncode == 0 and "nothing to do" in none.stderr


def test_backup_sh_applies_retention_after_every_complete_backup_from_the_bundled_helper():
    """install.sh bundles only backup.sh and backup_archive.py into the fetched recovery helper,
    so the rule must live in those two files and nowhere else."""
    backup = (INSTALL / "backup.sh").read_text()
    create = backup.index('"$ARCHIVE_HELPER" create "$BACKUP"')
    retention = backup.index('"$ARCHIVE_HELPER" retention "$BACKUP_ROOT"')
    assert create < retention < backup.index("printf '%s\\n' \"$BACKUP\"")
    assert '--protect "$BACKUP"' in backup and "</dev/null >&2" in backup[retention:retention + 200]
    for setting, default in (("BOSS_BACKUP_KEEP", "10"), ("BOSS_BACKUP_KEEP_DAYS", "7"),
                             ("BOSS_BACKUP_CAP_GB", "20")):
        assert f'env_or_dotenv {setting} {default}' in backup
    assert "--retention-dry-run" in backup and "--no-retention" in backup
    # a quoted .env value is the number inside; a non-number is the default, said once, never a
    # silently skipped rule (argparse would otherwise refuse '"10"')
    fn = backup[backup.index("env_or_dotenv() {"):backup.index("if [[ \"$RETENTION\" -eq 1 ]]")]
    probe = subprocess.run(["bash", "-c", fn + '\nENV_FILE=/dev/null\nBOSS_BACKUP_KEEP="\'10\'" '
                            'env_or_dotenv BOSS_BACKUP_KEEP 5; BOSS_BACKUP_KEEP=" 12 " env_or_dotenv '
                            'BOSS_BACKUP_KEEP 5; BOSS_BACKUP_KEEP=ten env_or_dotenv BOSS_BACKUP_KEEP 5'],
                           capture_output=True, text=True, check=True)
    assert probe.stdout.split() == ["10", "12", "5"] and "is not a number" in probe.stderr
    installer = (REPO / "install.sh").read_text()
    bundled = set(re.findall(r'scripts/install/(backup[a-z_]*\.(?:sh|py))" > "\$_backup_', installer))
    assert bundled == {"backup.sh", "backup_archive.py"}
    assert backup_archive.RETENTION_DEFAULTS == {"keep": 10, "keep_days": 7, "cap_gb": 20.0}


# ------------------------------------------------------------------------------------------ #
# ST-2 (BOSSAPP-1147): image and build-cache retention
# ------------------------------------------------------------------------------------------ #

FAKE_DOCKER = r"""#!/usr/bin/env bash
printf '%s\n' "$*" >> "$FAKE_DOCKER_LOG"
case "$1" in
  info) exit 0 ;;
  images)
    if [[ "$*" == *"--format {{.Repository}}"* && "$*" != *CreatedAt* ]]; then
      printf 'boss-community-api\nboss-community-worker\nboss-community-db\nboss-community-sidecar\n'
    elif [[ "$*" == *CreatedAt* && "$2" == boss-community-db ]]; then
      printf '2026-11-01 01|boss-community-db:pg16.12-vector0.8.1-age1.6.0\n'
      printf '2026-10-01 01|boss-community-db:pg16.10-vector0.8.0-age1.5.0\n'
    elif [[ "$*" == *CreatedAt* && "$2" == boss-community-sidecar ]]; then
      printf '2026-11-01 01|boss-community-sidecar:1.2.0\n'
      printf '2026-10-01 01|boss-community-sidecar:1.1.0\n'
    elif [[ "$*" == *CreatedAt* ]]; then
      repo="$2"
      printf '2026-11-12 03|%s:boss-community\n' "$repo"
      printf '2026-11-11 02|%s:prev-boss-community\n' "$repo"
      printf '2026-11-10 01|%s:old-one\n' "$repo"
      printf '2026-11-09 01|%s:older-one\n' "$repo"
      printf '2026-11-08 01|%s:custom\n' "$repo"
      printf '2026-11-07 01|%s:in-use\n' "$repo"
    fi
    exit 0 ;;
  ps) [[ "$*" == *"{{.Image}}"* && -z "${FAKE_NO_CONTAINERS:-}" ]] && printf 'boss-community-api:in-use\n'; exit 0 ;;
  *) exit 0 ;;
esac
"""


def _prune_world(tmp_path: Path, used_pct: int = 50) -> dict:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _exe(bindir / "docker", FAKE_DOCKER)
    _exe(bindir / "df", "#!/bin/sh\nprintf 'Filesystem 1024-blocks Used Available Capacity Mounted\\n"
                        f"/dev/test 100000000 {used_pct}000000 1000 {used_pct}%% /\\n'\n")
    install = tmp_path / "install"
    (install / "scripts" / "install").mkdir(parents=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith("BOSS_")}
    env.update(PATH=f"{bindir}{os.pathsep}{env['PATH']}", FAKE_DOCKER_LOG=str(tmp_path / "docker.log"))
    return {"env": env, "install": install}


def _would(result) -> list[str]:
    return [l.strip()[len("would run: "):] for l in result.stdout.splitlines() if "would run:" in l]


def test_prune_keeps_the_release_its_rollback_point_and_what_runs_and_removes_the_rest(tmp_path):
    w = _prune_world(tmp_path)
    out = _sh("bash", INSTALL / "prune.sh", "--dry-run", "--dir", w["install"], env=w["env"])
    calls = _would(out)
    removed = sorted(c.split()[-1] for c in calls if c.startswith("docker rmi "))
    assert removed == sorted([f"{r}:{t}" for r in ("boss-community-api", "boss-community-worker")
                              for t in ("old-one", "older-one", "custom")]
                             + ["boss-community-worker:in-use"])   # in use only as the api image
    assert not any("prev-boss-community" in c or ":boss-community" in c for c in calls if "rmi" in c)
    # the cap holds on every host, dedicated or not, below the disk threshold or not
    assert "docker builder prune -f --keep-storage 10gb" in calls
    assert not any("until=72h" in c for c in calls)        # the shared-machine rule still holds
    # the locally built database image (pinned by version, not pullable) and any repository
    # that is not tagged per release are never candidates
    assert not any("boss-community-db" in c or "boss-community-sidecar" in c for c in calls)


def test_prune_with_no_container_running_still_keeps_the_database_image_and_the_release(tmp_path):
    """The reviewer's case: `boss prune` on a stopped install printed `rmi boss-community-db:…`."""
    w = _prune_world(tmp_path)
    out = _sh("bash", INSTALL / "prune.sh", "--dry-run", "--dir", w["install"],
              env={**w["env"], "FAKE_NO_CONTAINERS": "1"})
    calls = _would(out)
    removed = sorted(c.split()[-1] for c in calls if c.startswith("docker rmi "))
    assert removed == sorted(f"{r}:{t}" for r in ("boss-community-api", "boss-community-worker")
                             for t in ("old-one", "older-one", "custom", "in-use"))
    assert not any("boss-community-db" in c or "boss-community-sidecar" in c for c in calls)
    assert not any(c.endswith(":boss-community") or ":prev-boss-community" in c for c in calls)


def test_the_current_tag_and_the_cache_cap_are_settings_read_from_the_installs_env(tmp_path):
    w = _prune_world(tmp_path, used_pct=90)
    # quoted and padded values are the value inside, as in any .env
    (w["install"] / ".env").write_text("BOSS_COMMUNITY_IMAGE_TAG=\"custom\"\nBOSS_BUILD_CACHE_CAP_GB= '25' \n"
                                       "BOSS_DEDICATED_HOST=1\n")
    out = _sh("bash", INSTALL / "prune.sh", "--dry-run", "--dir", w["install"], env=w["env"])
    calls = _would(out)
    assert "docker rmi boss-community-api:custom" not in calls
    assert "docker rmi boss-community-api:prev-boss-community" not in calls   # any prev- is safe
    assert "docker rmi boss-community-api:boss-community" in calls            # not the tag now
    assert "docker builder prune -f --keep-storage 25gb" in calls
    assert any("until=72h" in c for c in calls)            # dedicated + 90% used: the old rule too
    off = _sh("bash", INSTALL / "prune.sh", "--dry-run", "--dir", w["install"],
              env={**w["env"], "BOSS_BUILD_CACHE_CAP_GB": "0", "BOSS_DEDICATED_HOST": "0"})
    assert not any("builder prune" in c for c in _would(off))
    assert "cap off" in off.stdout
    bad = _sh("bash", INSTALL / "prune.sh", "--dry-run", "--dir", w["install"],
              env={**w["env"], "BOSS_BUILD_CACHE_CAP_GB": "ten"}, check=False)
    assert bad.returncode == 2 and "BOSS_BUILD_CACHE_CAP_GB" in bad.stderr


def test_a_successful_upgrade_prunes_before_it_reports_complete():
    text = (INSTALL / "upgrade.sh").read_text()
    installer = text.index('bash "$FETCHED_INSTALLER"')
    prune = text.index('bash "$INSTALL_DIR/scripts/install/prune.sh" --dir "$INSTALL_DIR"')
    assert installer < prune < text.index("printf 'Upgrade complete")
    assert '[[ -x "$INSTALL_DIR/scripts/install/prune.sh" ]]' in text   # an older release: skipped


# ------------------------------------------------------------------------------------------ #
# ST-3 (BOSSAPP-1148): container log caps
# ------------------------------------------------------------------------------------------ #

COMPOSE = yaml.safe_load((INSTALL / "community-compose.yml").read_text())


def test_every_boss_service_caps_its_container_log():
    services = COMPOSE["services"]
    assert len(services) >= 15
    for name, service in services.items():
        logging = service.get("logging")
        assert logging, f"{name} has no logging block: its log can grow without bound"
        assert logging["driver"] == "json-file"
        assert logging["options"]["max-size"] == "${BOSS_LOG_MAX_SIZE:-50m}", name
        assert str(logging["options"]["max-file"]) == "${BOSS_LOG_MAX_FILES:-3}", name


# ------------------------------------------------------------------------------------------ #
# ST-4 (BOSSAPP-1149): Postgres guardrails
# ------------------------------------------------------------------------------------------ #

def test_the_database_caps_temp_files_and_idle_transactions_but_never_times_out_migrations():
    command = COMPOSE["services"]["db"]["command"]
    flags = [command[i + 1] for i, part in enumerate(command) if part == "-c"]
    assert "shared_preload_libraries=age" in flags                 # unchanged
    assert "temp_file_limit=${BOSS_PG_TEMP_FILE_LIMIT:-5GB}" in flags
    assert "idle_in_transaction_session_timeout=${BOSS_PG_IDLE_IN_TRANSACTION_TIMEOUT:-15min}" in flags
    assert not any(f.startswith("statement_timeout") for f in flags)
    # the throwaway build-services Postgres lives on tmpfs: a temp spill there is RAM
    build_services = " ".join(COMPOSE["services"]["build-services"]["command"])
    assert "temp_file_limit=${BOSS_BUILD_SERVICES_TEMP_FILE_LIMIT:-1GB}" in build_services
    assert "idle_in_transaction_session_timeout=" in build_services
    # the worker's statements have a time limit; the API (migrations, index rebuilds) has none
    worker_env = COMPOSE["services"]["worker"]["environment"]
    assert worker_env["BOSS_DB_STATEMENT_TIMEOUT_SECONDS"] == \
        "${BOSS_WORKER_DB_STATEMENT_TIMEOUT_SECONDS:-1800}"
    assert "BOSS_DB_STATEMENT_TIMEOUT_SECONDS" not in COMPOSE["services"]["api"]["environment"]


def test_session_limits_are_set_per_connection_and_off_by_default():
    from boss_engine.db import connection_limits_sql

    assert connection_limits_sql(None, 0) == []
    assert connection_limits_sql(300, None) == ["SET idle_in_transaction_session_timeout = 300000"]
    assert connection_limits_sql(0, 1800) == ["SET statement_timeout = 1800000"]
    assert connection_limits_sql(300, 1800) == ["SET idle_in_transaction_session_timeout = 300000",
                                                "SET statement_timeout = 1800000"]


def test_in_database_backups_keep_six_hourly_by_default():
    from boss_engine.config import Settings
    from boss_engine.services.backup_scheduler import Policy

    assert Policy().keep_hourly == 6
    assert Settings(_env_file=None).backup_keep_hourly == 6
    assert COMPOSE["services"]["api"]["environment"]["BOSS_BACKUP_KEEP_HOURLY"] == \
        "${BOSS_BACKUP_KEEP_HOURLY:-6}"                    # the scheduler runs in the API


# ------------------------------------------------------------------------------------------ #
# ST-5a (BOSSAPP-1150): the upgrade checks the disk before it builds
# ------------------------------------------------------------------------------------------ #

UPGRADE = (INSTALL / "upgrade.sh").read_text()
VERDICT = re.search(r"# >>> boss_disk_admission\n(.*?)# <<< boss_disk_admission", UPGRADE, re.S).group(1)


def _verdict(host, docker, cache, images, floor=20, full=35, incr=5):
    r = subprocess.run(["bash", "-c", VERDICT + '\nboss_disk_verdict "$@"', "x",
                        str(host), str(docker), str(cache), str(images), str(floor), str(full), str(incr)],
                       capture_output=True, text=True)
    return r.returncode, r.stdout.strip()


def test_the_upgrade_predicts_its_own_size_from_the_build_cache():
    code, cold = _verdict(60, 90, 0.2, 1)
    assert code == 0 and "full rebuild (about 35 GB" in cold and "cache is cold" in cold
    assert "needs 55 GB" in cold
    code, warm = _verdict(60, 90, 12.0, 1)
    assert code == 0 and "incremental build (about 5 GB" in warm and "needs 25 GB" in warm
    code, fresh = _verdict(60, 90, "", 0)
    assert code == 0 and "full build (about 35 GB" in fresh and "no BOSS images here yet" in fresh
    code, unknown = _verdict(60, "", "", 1)
    assert code == 0 and "Docker's own disk could not be measured" in unknown


def test_the_upgrade_refuses_below_the_floor_in_plain_words_and_names_the_way_out():
    # the operator's afternoon: 13 GB free on the host, a cold cache
    code, out = _verdict(13, "", "", 1)
    assert code == 1 and out.startswith("refuse ")
    assert "this host has 13.0 GB free" in out and "needs at least 55 GB" in out
    assert "20 GB floor (BOSS_DISK_FLOOR_GB)" in out
    assert "boss prune" in out and "--skip-disk-check" in out and "Nothing changed" in out
    # the host has room, Docker Desktop's virtual disk does not
    code, out = _verdict(200, 30, 0.2, 1)
    assert code == 1 and "Docker's own disk has 30.0 GB free" in out and "Settings > Resources" in out
    # the settings move the line
    assert _verdict(13, "", 12.0, 1, floor=5)[0] == 0          # warm cache: 5 + 5 ≤ 13
    assert _verdict(13, "", 12.0, 1, floor=5, incr=9)[0] == 1  # 9 + 5 > 13


def test_the_disk_check_runs_before_anything_changes_and_can_be_switched_off():
    check = UPGRADE.index("disk_check\nelse")
    assert UPGRADE.index("# <<< boss_disk_admission") < check
    assert check < UPGRADE.index('keep_previous_images "$(image_tag)"')
    assert check < UPGRADE.index('bash "$FETCHED_INSTALLER"')
    assert "--skip-disk-check) DISK_CHECK=0; shift ;;" in UPGRADE
    assert 'env_or_dotenv BOSS_DISK_CHECK on)" != off' in UPGRADE
    # `boss upgrade --skip-disk-check` reaches upgrade.sh (the refusal text names it)
    boss = (INSTALL / "boss.sh").read_text()
    assert "--dry-run|--web|--no-web|--no-rollback|--skip-disk-check) args+=(\"$1\"); shift ;;" in boss
    for setting, default in (("BOSS_DISK_FLOOR_GB", "20"), ("BOSS_UPGRADE_FULL_BUILD_GB", "35"),
                             ("BOSS_UPGRADE_INCREMENTAL_BUILD_GB", "5")):
        assert f"env_or_dotenv {setting} {default}" in UPGRADE
    # a dry run reports the verdict and goes on; a real run stops
    assert 'echo "Would refuse: ${verdict#refuse }"' in UPGRADE
    assert 'echo "${verdict#refuse }" >&2\n      exit 1' in UPGRADE


# ------------------------------------------------------------------------------------------ #
# ST-5b (BOSSAPP-1150): the lease gate holds builds while the disk is low, says so, and resumes
# ------------------------------------------------------------------------------------------ #

class _Conn:
    def __init__(self):
        self.updates: list[dict] = []

    def execute(self, _statement, params=None):
        self.updates.append(params or {})


def _lease_service_and_events(monkeypatch):
    from boss_engine.services import jobs

    events: list[tuple] = []
    monkeypatch.setattr(jobs, "emit_event", lambda c, p, t, a, et, ei, d: events.append((t, d)))
    return jobs.JobLeaseService, events


def _candidate(last_error=None):
    return {"id": "job_1", "project_id": "proj_1", "kind": "code.task", "last_error": last_error}


def test_a_held_build_says_the_disk_is_the_reason_and_records_the_episode_once(monkeypatch):
    from boss_engine.services import admission
    from boss_engine.services.workload_profiles import DEFAULTS

    service, events = _lease_service_and_events(monkeypatch)
    low = admission.Capacity(total_mb=15991, available_mb=13029, cores=8, disk_free_gb=3.1,
                             disk_used_pct=97.0)
    verdict = admission.decide(low, DEFAULTS["code.task"], in_flight=0, slots=4, disk_floor_gb=8)
    assert verdict.disk_low
    conn = _Conn()
    service._defer_for_host_capacity(None, conn, _candidate(), verdict)
    held = json.loads(conn.updates[0]["last_error"])
    assert held["code"] == "host_capacity" and held["disk_low"] is True   # the run shows "waiting"
    assert "3.1 GB of disk free" in held["message"] and "resumes on its own" in held["message"]
    assert [t for t, _ in events] == ["job.capacity_deferred", "host.disk_low"]
    low_event = dict(events)["host.disk_low"]
    assert low_event["free_gb"] == 3.1 and low_event["floor_gb"] == 8.0
    assert "BOSS_BUILD_DISK_FLOOR_GB" in low_event["reason"] and low_event["resumes"] == "automatically"
    # the next poll, still low: the job stays held, no second event
    events.clear()
    service._defer_for_host_capacity(None, conn, _candidate(json.dumps(held)), verdict)
    assert events == []
    # space is back: the lease goes ahead and the episode is closed on the job that waited
    ok = admission.decide(admission.Capacity(**{**low.__dict__, "disk_free_gb": 40.0, "notes": []}),
                          DEFAULTS["code.task"], in_flight=0, slots=4, disk_floor_gb=8)
    assert ok.admitted
    service._note_disk_recovered(None, conn, _candidate(held), ok)
    assert [t for t, _ in events] == ["host.disk_recovered"]
    assert events[0][1]["free_gb"] == 40.0
    # a job that never waited for disk gets no such note
    events.clear()
    service._note_disk_recovered(None, conn, _candidate(), ok)
    service._note_disk_recovered(None, conn, _candidate({"code": "host_capacity"}), ok)
    assert events == []


def test_a_build_held_for_memory_is_not_called_a_disk_problem(monkeypatch):
    from boss_engine.services import admission
    from boss_engine.services.workload_profiles import DEFAULTS

    service, events = _lease_service_and_events(monkeypatch)
    thrashing = admission.Capacity(total_mb=7650, available_mb=600, cores=12, psi_full_avg10=12.0,
                                   psi_full_avg60=6.0, disk_free_gb=281.0, scope="docker_vm")
    verdict = admission.decide(thrashing, DEFAULTS["code.task"], in_flight=1, slots=2)
    assert not verdict.admitted and not verdict.disk_low
    conn = _Conn()
    service._defer_for_host_capacity(None, conn, _candidate(), verdict)
    held = json.loads(conn.updates[0]["last_error"])
    assert held["disk_low"] is False and held["message"].startswith("Queued: waiting for room")
    assert [t for t, _ in events] == ["job.capacity_deferred"]


def test_the_gate_is_the_one_place_a_build_is_admitted_and_every_run_shows_the_hold():
    """Both the console's "waiting" reason (stuck_work) and the queued count (admission.snapshot)
    read `last_error.code = host_capacity`: a disk hold must keep that code, with disk_low beside
    it, or the run would show nothing while it waits."""
    jobs_src = (REPO / "engine/boss_engine/services/jobs.py").read_text()
    assert '"code": "host_capacity", "deferred_reason": "host_capacity"' in jobs_src
    assert '"disk_low": disk_low' in jobs_src
    stuck = (REPO / "engine/boss_engine/services/stuck_work.py").read_text()
    assert 'if code == "host_capacity":' in stuck and "Waiting for room on this machine" in stuck
    for service in ("api", "worker"):
        assert COMPOSE["services"][service]["environment"]["BOSS_BUILD_DISK_FLOOR_GB"] == \
            "${BOSS_BUILD_DISK_FLOOR_GB:-8}"
