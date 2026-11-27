"""A disk warning counts only what can be freed, and the cleanup that answers it really frees it.

Enterprise validation of rc10.35 found two sides of one fault:

* ENT-SETUP-01: `boss doctor` warned about 12.4 GB of build cache and said "Run `boss prune` to
  cap it", while Docker reported only ~6 MB of it as reclaimable: the rest were layers the
  current and rollback images still use. Running the command freed nothing, and could not.
* ENT-DISK-02: after a real rc10.34→rc10.35 upgrade the installer said it pruned, and Docker
  still held 17.2 GB of build cache against the 10 GB cap.

The class fix: ONE reading of the build cache — what Docker can free (`docker system df`
Reclaimable) — is what the cap, the doctor, Govern › Storage, the cleanup plan, the helper's
estimate and the cause sentence count; the part installed images share is named as theirs, with
nothing to run. prune.sh enforces the cap on that same reading (falling back to `--all` when a
plain prune leaves it over), prints Docker's real before/after, and fails when it is still over;
the upgrade runs it the way the installer does after its build (--room). A category measured as
the running release's own images names no command that cannot shrink them.
"""
from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from boss_engine.services import disk_budget as dbu

REPO = Path(__file__).resolve().parents[2]
PRUNE = REPO / "scripts" / "install" / "prune.sh"


def _steward():
    spec = importlib.util.spec_from_file_location("_steward_cache", REPO / "scripts" / "install" / "steward.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _df_runner(cache: str, reclaimable: str):
    """`docker system df` as the enterprise doctor saw it; every other call answers empty."""
    def run(argv):
        if argv[1:3] == ["system", "df"] and "-v" not in argv:
            rows = [{"Type": "Images", "Size": "8.1GB", "Reclaimable": "0B (0%)"},
                    {"Type": "Build Cache", "Size": cache, "Reclaimable": reclaimable}]
            return subprocess.CompletedProcess(argv, 0, "\n".join(json.dumps(r) for r in rows), "")
        return subprocess.CompletedProcess(argv, 0, "", "")
    return run


# ── the reading: doctor and Govern ─────────────────────────────────────────────────────────

def test_cache_that_installed_images_share_is_not_warned_about_and_names_no_command():
    measured = dbu.measure_docker(["docker"], project="boss-community", tag="t",
                                  run=_df_runner("12.4GB", "6.16MB"))
    assert measured["build_cache"] == pytest.approx(0.00616)
    assert measured[dbu.CACHE_SHARED_KEY] == pytest.approx(12.39384)
    rows = {r["key"]: r for r in dbu.evaluate(measured)}
    assert rows["build_cache"]["level"] == "ok" and rows["build_cache"]["action"] == ""
    assert "layers installed images use" in rows["build_cache"]["note"]
    # It does not rule out what does free space: old BOSS images go with `boss prune`.
    assert "old BOSS ones go with `boss prune`" in rows["build_cache"]["note"]
    lines = [ln for ln in dbu._lines({"rows": list(rows.values()), "total": dbu.total(list(rows.values()))})
             if "Build cache" in ln]
    assert lines and not any(ln.startswith("warn") for ln in lines), lines
    assert not any("Run `boss prune`" in ln for ln in lines), lines
    assert "no cache prune frees it" in lines[0]


def test_cache_docker_can_free_past_the_cap_still_warns_with_the_command_that_frees_it():
    measured = dbu.measure_docker(["docker"], project="p", tag="t", run=_df_runner("17.2GB", "13.5GB"))
    row = next(r for r in dbu.evaluate(measured) if r["key"] == "build_cache")
    assert row["level"] == "over" and row["gb"] == 13.5 and "boss prune" in row["action"]


def test_a_docker_without_a_reclaimable_column_keeps_the_whole_cache_as_before():
    def run(argv):
        out = json.dumps({"Type": "Build Cache", "Size": "4.2GB"}) if argv[1:3] == ["system", "df"] else ""
        return subprocess.CompletedProcess(argv, 0, out, "")
    measured = dbu.measure_docker(["docker"], project="p", tag="t", run=run)
    assert measured["build_cache"] == pytest.approx(4.2) and measured[dbu.CACHE_SHARED_KEY] == 0


def test_the_running_releases_own_images_name_no_command_that_cannot_shrink_them():
    row = next(r for r in dbu.evaluate({"images": 8.1}) if r["key"] == "images")
    assert row["level"] == "near"
    assert row["action"].startswith("Nothing to do") and "Run `boss prune`" not in row["action"]


def test_govern_the_plan_and_the_cause_count_the_same_freeable_cache():
    from boss_engine.services import storage_cleanup, storage_incidents, storage_watch

    shared = {"cache_gb": 12.4, "images_gb": 8.1, "total_gb": 100, "free_gb": 5,
              "by_type": {storage_watch.CACHE_FREEABLE_KEY: 0.006}}
    assert storage_watch.cache_freeable_gb(shared) == 0.006
    assert storage_watch.cache_shared_gb(shared) == pytest.approx(12.394)
    rules = [{"kind": "build_cache", "enabled": True, "rule": {"cap_gb": 10}}]
    assert storage_watch.rules_reclaim_gb(shared, rules) == 0
    prune = next(t for t in storage_cleanup.TOOLS if t.name == "storage.prune_build_cache")
    assert not prune.applies(shared, {"build_cache": {"cap_gb": 10}})
    over = {**shared, "by_type": {storage_watch.CACHE_FREEABLE_KEY: 13.0}}
    assert prune.applies(over, {"build_cache": {"cap_gb": 10}})
    assert storage_watch.rules_reclaim_gb(over, rules) == pytest.approx(3.0)
    # The cause sentence: 8.1 GB of the release's images and shared cache is not "old images
    # and build cache" a cleanup could take.
    key, _ = storage_incidents.explain_cause(shared, None, {})
    assert key != "images.no_retention"
    # An older helper sent only the total: the best reading there is, as before.
    assert storage_watch.cache_freeable_gb({"cache_gb": 4.0}) == 4.0
    assert storage_watch.cache_shared_gb({"cache_gb": 4.0}) is None


# ── the helper: its reading and its estimate ───────────────────────────────────────────────

@pytest.fixture()
def host(tmp_path):
    install = tmp_path / "install"
    install.mkdir()
    (install / ".env").write_text("BOSS_INSTALL_ID=inst_abc\nBOSS_DEDICATED_HOST=1\n")
    steward = _steward()
    env = steward.Env(str(install), backup_root=str(tmp_path / "backups"), state_dir=str(tmp_path / "state"),
                      docker=["docker"])
    return steward, env


def _docker(cache: str, reclaimable: str, calls: list):
    def run(argv, timeout=120.0):
        calls.append(argv)
        if argv[1:3] == ["system", "df"] and "-v" not in argv:
            return subprocess.CompletedProcess(argv, 0, json.dumps(
                {"Type": "Build Cache", "Size": cache, "Reclaimable": reclaimable}), "")
        return subprocess.CompletedProcess(argv, 0, "", "")
    return run


def test_the_helper_reports_what_can_be_freed_and_plans_nothing_it_cannot_free(host):
    steward, env = host
    calls: list = []
    run = _docker("12.4GB", "6.16MB", calls)
    sample = steward.collect(env, run)
    assert sample["cache_gb"] == pytest.approx(12.4, abs=0.01)
    assert sample["by_type"]["build_cache_reclaimable"] == pytest.approx(0.0062, abs=0.001)
    items, over = steward.verb_prune_build_cache(env, {"keep_storage_gb": 10}, dry_run=True, runner=run)
    assert items == [] and over == 0.0
    items, over = steward.verb_prune_build_cache(env, {"keep_storage_gb": 10}, dry_run=True,
                                                 runner=_docker("17.2GB", "13.5GB", []))
    assert items and over == pytest.approx(3.5e9)


def test_the_helper_measures_the_budget_split_at_most_every_ten_minutes_and_never_loses_the_sample(host, monkeypatch):
    steward, env = host
    calls: list = []
    run = _docker("1GB", "1GB", calls)
    first = steward.budget_reading(env, run, now=1000.0)
    walked = len(calls)
    assert walked and "budget_build_cache" in first
    assert steward.budget_reading(env, run, now=1000.0 + 599) == first
    assert len(calls) == walked, "the split was walked again inside its interval"
    steward.budget_reading(env, run, now=1000.0 + 601)
    assert len(calls) > walked
    # A split that cannot be measured leaves it out; the disk reading still goes.
    monkeypatch.setattr(steward.runpy, "run_path", lambda *_a, **_k: (_ for _ in ()).throw(SyntaxError("x")))
    (env.state_dir / steward.BUDGET_FILE).unlink()
    assert steward.budget_reading(env, run, now=5000.0) == {}
    assert "total_gb" in steward.collect(env, run)


# ── prune.sh: the cap enforced on that reading, the real before/after ──────────────────────

# Docker's build cache as a state file "<size>|<reclaimable>". A plain prune frees nothing (the
# unused cache Docker's default prune passes over, ENT-DISK-02); --all frees down to the cap.
FAKE_DOCKER = r"""#!/usr/bin/env bash
printf '%s\n' "$*" >> "$FAKE_DOCKER_LOG"
case "$1" in
  info) exit 0 ;;
  system) IFS='|' read -r size free < "$FAKE_CACHE_STATE"
          printf 'Images|8.1GB|0B (0%%)\nBuild Cache|%s|%s\n' "$size" "$free"; exit 0 ;;
  builder)
    if [[ "$*" == *" -af "* || "$*" == *" -af" ]]; then printf '%s\n' "$FAKE_AFTER_ALL" > "$FAKE_CACHE_STATE"; fi
    exit 0 ;;
  *) exit 0 ;;
esac
"""


def _prune(tmp_path: Path, before: str, after_all: str, *args: str, own: bool = True,
           **extra: str) -> subprocess.CompletedProcess:
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    exe = bindir / "docker"
    exe.write_text(FAKE_DOCKER)
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    install = tmp_path / "install"
    install.mkdir(exist_ok=True)
    (tmp_path / "cache").write_text(before + "\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith("BOSS_") and not k.startswith("DOCKER_")}
    env.update({"PATH": f"{bindir}{os.pathsep}{env['PATH']}", "FAKE_DOCKER_LOG": str(tmp_path / "log"),
                "FAKE_CACHE_STATE": str(tmp_path / "cache"), "FAKE_AFTER_ALL": after_all,
                **({"BOSS_DEDICATED_HOST": "1"} if own else {}), **extra})
    (tmp_path / "log").unlink(missing_ok=True)
    return subprocess.run(["bash", str(PRUNE), "--cache-only", "--dir", str(install), *args], env=env,
                          capture_output=True, text=True, timeout=60)


def _calls(tmp_path: Path) -> list[str]:
    return ["docker " + ln for ln in (tmp_path / "log").read_text().splitlines()]


def test_an_upgrade_that_left_the_cache_over_its_cap_brings_it_under_and_prints_dockers_reading(tmp_path):
    out = _prune(tmp_path, "17.2GB|9.9GB", "7.4GB|0B")
    # 9.9 GB freeable is within the 10 GB cap: nothing more to drop, and the 7.3 GB that images
    # share is named as theirs — the 17.2 GB the doctor used to warn about.
    assert out.returncode == 0, out.stderr
    assert "docker builder prune -af --keep-storage 10gb" not in _calls(tmp_path)
    assert "build cache: 17.2 GB before (9.9 GB Docker can free), 17.2 GB after" in out.stdout
    assert "7.3 GB of it is layers installed images still use" in out.stdout

    out = _prune(tmp_path, "20.7GB|13.5GB", "8.6GB|1.4GB")
    assert out.returncode == 0, out.stderr
    calls = _calls(tmp_path)
    assert calls.index("docker builder prune -f --keep-storage 10gb") < \
        calls.index("docker builder prune -af --keep-storage 10gb")
    assert ("build cache: 20.7 GB before (13.5 GB Docker can free), 8.6 GB after (1.4 GB Docker can "
            "free); the cap is 10 GB") in out.stdout
    # --all never goes below the cap on a shared machine: every --all prune keeps the cap.
    assert all("--keep-storage 10gb" in c for c in calls if " -af" in c)


def test_on_a_shared_docker_the_rest_is_left_to_the_person_with_the_command(tmp_path):
    out = _prune(tmp_path, "20.7GB|13.5GB", "8.6GB|1.4GB", own=False)
    assert out.returncode == 0, out.stderr
    assert not any(" -af" in c for c in _calls(tmp_path))
    assert "build cache left to you" in out.stdout
    assert "13.5 GB of build cache Docker can free is over the 10 GB cap. To free it yourself: " \
           "docker builder prune -af --keep-storage 10gb" in out.stdout


def test_a_cache_still_over_its_cap_is_a_failure_not_a_claim(tmp_path):
    out = _prune(tmp_path, "20.7GB|13.5GB", "20.7GB|13.5GB")
    assert out.returncode == 1
    assert "still 13.5 GB, over the 10 GB cap" in out.stderr


def test_a_dry_run_changes_nothing_and_says_what_it_would_run(tmp_path):
    out = _prune(tmp_path, "20.7GB|13.5GB", "1GB|0B", "--dry-run")
    assert out.returncode == 0
    assert not any("prune" in c for c in _calls(tmp_path))
    assert "would run: docker builder prune -af --keep-storage 10gb" in out.stdout


def test_the_upgrade_runs_the_prune_the_installer_runs_after_its_build():
    upgrade = (REPO / "scripts" / "install" / "upgrade.sh").read_text()
    assert 'bash "$INSTALL_DIR/scripts/install/prune.sh" --dir "$INSTALL_DIR" --room </dev/null' in upgrade
    install = (REPO / "install.sh").read_text()
    block = install[install.index("# ---- room to work after the build"):install.index("# ---- models and subscriptions")]
    # The installer shows Docker's reading whichever way the prune went, never only a claim.
    assert '"prune: "*' in block and block.index("ui_info") < block.index('ui_ok "Old images pruned')


# ── review of the port: BOSS clears only its own cache; its builder is read as such ────────

def test_on_a_shared_docker_doctor_and_govern_say_the_rest_is_the_persons_with_the_command():
    measured = {"build_cache": 13.5, dbu.CACHE_OWNED_KEY: False}
    row = next(r for r in dbu.evaluate(measured) if r["key"] == "build_cache")
    assert row["level"] == "over"
    assert "This Docker is shared" in row["action"] and "Run `boss prune`" not in row["action"]
    assert "docker builder prune -af --keep-storage 10gb" in row["action"]
    assert problems_free(row)
    own = next(r for r in dbu.evaluate({**measured, dbu.CACHE_OWNED_KEY: True}) if r["key"] == "build_cache")
    assert own["action"] == "Run `boss prune` to cap it."


def problems_free(row) -> bool:
    from boss_engine.services.failure_words import problems

    return problems(what=row["what"], action=row["action"]) == []


def test_the_doctor_knows_whose_cache_it_is_from_the_install_settings(tmp_path):
    seen = {}

    def run(argv):
        seen.setdefault("calls", []).append(argv)
        if argv[1:3] == ["buildx", "du"]:
            return subprocess.CompletedProcess(argv, 0, "Shared:\t6GB\nPrivate:\t2GB\nReclaimable:\t8GB\nTotal:\t8GB\n", "")
        if argv[1:3] == ["buildx", "inspect"]:
            driver = "docker" if argv[-1] == "default" else "docker-container"
            return subprocess.CompletedProcess(argv, 0, f"Name: {argv[-1]}\nDriver: {driver}\n", "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    (tmp_path / ".env").write_text("BOSS_BUILDX_BUILDER=default\n")
    report = dbu.host_report(tmp_path, run=run)
    row = next(r for r in report["rows"] if r["key"] == "build_cache")
    assert row["note"] == "" or "buildx" not in row["note"]
    assert not any(c[1:3] == ["buildx", "du"] for c in seen["calls"]), "the daemon's builder is not BOSS's"
    (tmp_path / ".env").write_text("BOSS_BUILDX_BUILDER=boss-builder\n")
    report = dbu.host_report(tmp_path, run=run)
    row = next(r for r in report["rows"] if r["key"] == "build_cache")
    # buildx du counts shared layers as Reclaimable; what a prune frees is Private.
    assert row["gb"] == 2.0 and "6.0 GB more is layers installed images use" in row["note"]
    assert any(c[1:5] == ["buildx", "du", "--builder", "boss-builder"] for c in seen["calls"])
    (tmp_path / ".env").write_text("")
    shared = dbu.host_report(tmp_path, run=_df_runner("30GB", "25GB"))
    row = next(r for r in shared["rows"] if r["key"] == "build_cache")
    assert row["level"] == "over" and "This Docker is shared" in row["action"]


def test_the_helper_reads_and_plans_its_own_builder_and_says_whether_the_cache_is_its_own(host):
    steward, env = host
    env.dedicated, env.buildx_builder = False, "boss-builder"
    calls: list = []
    driver = ["docker-container"]

    def run(argv, timeout=120.0):
        calls.append(argv)
        if argv[1:3] == ["buildx", "du"]:
            return subprocess.CompletedProcess(argv, 0, "Shared:\t6GB\nPrivate:\t13GB\nReclaimable:\t19GB\nTotal:\t19GB\n", "")
        if argv[1:3] == ["buildx", "inspect"]:
            return subprocess.CompletedProcess(argv, 0, f"Driver: {driver[0]}\n", "")
        if argv[1:3] == ["system", "df"] and "-v" not in argv:   # the daemon: not what this prune touches
            return subprocess.CompletedProcess(argv, 0, json.dumps(
                {"Type": "Build Cache", "Size": "40GB", "Reclaimable": "40GB"}), "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    sample = steward.collect(env, run)
    assert sample["by_type"]["build_cache_scope"] == pytest.approx(19.0)
    assert sample["by_type"]["build_cache_reclaimable"] == pytest.approx(13.0)
    assert sample["by_type"]["build_cache_owned"] == 1.0
    items, over = steward.verb_prune_build_cache(env, {"keep_storage_gb": 10}, dry_run=True, runner=run)
    assert items == ["build-cache:builder:boss-builder:above-10GB"] and over == pytest.approx(3e9)
    env.buildx_builder = ""
    assert steward.collect(env, run)["by_type"]["build_cache_owned"] == 0.0
    # The daemon's own builder (docker driver) is not BOSS's: refused, and read as the daemon.
    driver[0] = "docker"
    env.buildx_builder = "desktop-linux"
    assert steward.collect(env, run)["by_type"]["build_cache_owned"] == 0.0
    with pytest.raises(steward.Refused, match="not one with the docker driver"):
        steward.verb_prune_build_cache(env, {"keep_storage_gb": 10}, dry_run=True, runner=run)


def test_a_shared_cache_is_no_cleanup_step_and_govern_names_the_shared_part_of_the_builder():
    from boss_engine.services import storage_cleanup, storage_watch

    sample = {"cache_gb": 40.0, "by_type": {"build_cache_scope": 19.0, storage_watch.CACHE_FREEABLE_KEY: 13.0,
                                            "build_cache_owned": 0.0}}
    prune = next(t for t in storage_cleanup.TOOLS if t.name == "storage.prune_build_cache")
    assert not prune.applies(sample, {"build_cache": {"cap_gb": 10}})
    assert storage_watch.cache_shared_gb(sample) == pytest.approx(6.0)   # of the builder, not the daemon
    assert storage_watch.cache_owned(sample) is False
    sample["by_type"]["build_cache_owned"] = 1.0
    assert prune.applies(sample, {"build_cache": {"cap_gb": 10}})
    assert storage_watch.cache_owned({"by_type": {}}) is None


def test_a_service_snapshot_older_than_the_helper_window_is_marked_stale(monkeypatch):
    from datetime import datetime, timedelta, timezone

    from boss_engine.services import admission, obs_view

    now = datetime.now(timezone.utc)
    snap = {"services": [{"name": "boss-api-1", "service": "api", "state": "running"}]}
    monkeypatch.setattr(admission, "_state", lambda c, k: {**snap, "at": (now - timedelta(minutes=11)).isoformat()})
    assert obs_view._compose_snapshot(None)["stale"] is True
    monkeypatch.setattr(admission, "_state", lambda c, k: {**snap, "at": (now - timedelta(minutes=1)).isoformat()})
    assert obs_view._compose_snapshot(None)["stale"] is False
    monkeypatch.setattr(admission, "_state", lambda c, k: {})
    assert obs_view._compose_snapshot(None) == {}
