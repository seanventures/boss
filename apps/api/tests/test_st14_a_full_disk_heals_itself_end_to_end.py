"""ST-14 (BOSSAPP-1159, epic BOSSAPP-1145): THE PROOF that the Infra Agent handles storage
pressure by itself, end to end, through the same seams the laptop install uses.

    healthy host ──(statvfs falls)──► BOSS notices ──► explains ──► cleans what is safe
         ▲                                  │ hold                      │ asks for the rest
         └──── resumes + reports ◄──────────┴──── free ≥ target ◄───────┘
                                   a cause seen twice ──► a permanent rule is proposed

Nothing touches the laptop's disk or Docker. The disk is a FAKE statvfs behind the real host
helper (`scripts/install/steward.py` `collect()` reads `shutil.disk_usage`), Docker is a fake
whose state the helper's verbs really change, the backups are a few KB in pytest's tmp_path
(each one *counts* as 5 GB on the fake disk), and the Postgres side is real but confined: this
run's own schema, plus three tiny databases on the bench server named for this run alone
(dropped on the way out). Every surface is read the way the console reads it — over HTTP.

What it proves (the ST-14 checklist):
1. healthy → one bad reading does nothing → the second opens a CRITICAL incident;
2. BOSS notices: the hold is set, nothing new is claimed (a build stays queued, stamped), a
   running step sees the hold at its checkpoint and hands its job back with no attempt spent,
   and the strip (`/v1/infra/storage/incidents/current`) and the Command Center
   (`/v1/command-center/view`) both say "Infra Agent is cleaning";
3. it explains: a plain-sentence cause, and a receipt for every real action;
4. it cleans only what is safe — labelled build cache, an old labelled image, an orphan TEST
   volume, an EXPIRED stamped `boss_scratch_*` database, expired telemetry — and leaves
   unlabelled images/volumes, a labelled DATA volume, an unstamped scratch database, a
   non-scratch database and an operator's files alone; the backups (irreversible) become ONE
   approval card, and only the dry run's items go when a person approves it;
5. free space back above target resumes paused work (hysteresis: one good reading resumes,
   two resolve), a report is posted, and the same cause seen a second time proposes a
   permanent rule which, approved, makes the next cleanup of it automatic (tier 2).
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import time
import types
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from boss_engine.services import (host_jobs, storage_cleanup, storage_incidents, storage_rules,
                                  storage_watch, work_hold)

ROOT = Path(__file__).resolve().parents[3]
TAG = uuid.uuid4().hex[:6]
HOST = f"host_st14_{TAG}"
INSTALL = f"inst_st14_{TAG}"
GB = 1e9
BACKUP_GB = 5.0                      # each tiny backup dir counts as 5 GB on the fake disk
TOTAL_GB = 500.0


def _steward():
    spec = importlib.util.spec_from_file_location("_steward_st14", ROOT / "scripts" / "install" / "steward.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── the fakes: a Docker whose state the helper's verbs change, and a statvfs over it ────────

class FakeDocker:
    """Enough of the docker CLI for the helper's verbs, honouring --filter like the real one."""

    def __init__(self) -> None:
        self.cache = 18 * GB
        self.images = [  # (repo, tag, id, bytes, install label)
            {"Repository": "boss-community-api", "Tag": "v1", "ID": "sha1", "bytes": 9 * GB, "label": INSTALL},
            {"Repository": "boss-community-api", "Tag": "prev-v1", "ID": "sha2", "bytes": 13 * GB, "label": INSTALL},
            {"Repository": "boss-community-api", "Tag": "v0", "ID": "sha3", "bytes": 5 * GB, "label": INSTALL},
            {"Repository": "my/tool", "Tag": "latest", "ID": "sha9", "bytes": 3 * GB, "label": ""},
        ]
        old = (datetime.now(timezone.utc) - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.volumes = {
            # an orphan TEST volume BOSS made: rebuildable, labelled → automatic
            f"boss_test_pg_{TAG}": {"labels": {"boss.install_id": INSTALL, "boss.kind": "test"},
                                    "bytes": 40 * GB, "dangling": True, "created": old},
            # a labelled DATA volume left dangling (a stopped preview's database): irreversible
            f"boss_preview_pgdata_{TAG}": {"labels": {"boss.install_id": INSTALL, "boss.kind": "data"},
                                           "bytes": 6 * GB, "dangling": True, "created": old},
            # someone else's volume: unlabelled → never BOSS's to remove
            f"my_own_data_{TAG}": {"labels": {}, "bytes": 3 * GB, "dangling": True, "created": old},
        }
        self.calls: list[list[str]] = []

    # sizes the fake disk adds up
    def images_bytes(self) -> float:
        return sum(i["bytes"] for i in self.images)

    def volumes_bytes(self) -> float:
        return sum(v["bytes"] for v in self.volumes.values())

    @staticmethod
    def _filters(a: list[str]) -> list[str]:
        return [a[i + 1] for i, x in enumerate(a) if x == "--filter" and i + 1 < len(a)]

    @staticmethod
    def _label_key(fmt: str) -> str:
        # {{index .Labels "boss.kind"}} / {{index .Config.Labels "boss.install_id"}}
        return fmt.split('"')[1] if '"' in fmt else ""

    def __call__(self, argv, timeout=120.0):
        a = list(argv[1:])
        self.calls.append(a)
        out, rc = "", 0
        if a[:2] == ["system", "df"] and "-v" in a:
            # `docker system df -v`: the only place Docker reports each volume's size
            out = json.dumps({"Images": [], "Containers": [], "BuildCache": [],
                              "Volumes": [{"Name": n, "Size": f"{v['bytes'] / GB:g}GB"}
                                          for n, v in self.volumes.items()]})
        elif a[:2] == ["system", "df"]:
            out = "\n".join(json.dumps(r) for r in [
                {"Type": "Images", "Size": f"{self.images_bytes() / GB:g}GB", "Reclaimable": "5GB"},
                {"Type": "Build Cache", "Size": f"{self.cache / GB:g}GB", "Reclaimable": f"{self.cache / GB:g}GB"},
                {"Type": "Local Volumes", "Size": f"{self.volumes_bytes() / GB:g}GB", "Reclaimable": "0B"},
                {"Type": "Containers", "Size": "0B", "Reclaimable": "0B"}])
        elif a[:2] == ["builder", "prune"]:
            keep = float(a[a.index("--keep-storage") + 1].lower().rstrip("gb")) * GB
            self.cache = min(self.cache, keep)
        elif a[:1] == ["images"]:
            want = next((f.split("=", 2)[2] for f in self._filters(a) if f.startswith("label=boss.install_id=")), None)
            out = "\n".join(json.dumps({"Repository": i["Repository"], "Tag": i["Tag"], "ID": i["ID"],
                                        "Size": f"{i['bytes'] / GB:g}GB"})
                            for i in self.images if want is None or i["label"] == want)
        elif a[:2] == ["image", "inspect"]:
            ref = a[-1]
            img = next((i for i in self.images if f"{i['Repository']}:{i['Tag']}" == ref or i["ID"] == ref), None)
            out = (img or {}).get("label", "")
        elif a[:1] == ["ps"]:
            out = "" if "--filter" in a else "boss-community-api:v1"
        elif a[:1] == ["rmi"]:
            self.images = [i for i in self.images if f"{i['Repository']}:{i['Tag']}" != a[-1]]
        elif a[:2] == ["volume", "ls"]:
            rows = []
            for name, v in self.volumes.items():
                ok = True
                for f in self._filters(a):
                    if f.startswith("label="):
                        k, _, val = f[len("label="):].partition("=")
                        ok = ok and v["labels"].get(k) == val
                    elif f == "dangling=true":
                        ok = ok and v["dangling"]
                if ok:
                    # `docker volume ls` prints no size ("N/A"); only `system df -v` knows it.
                    rows.append({"Name": name, "Driver": "local", "Size": "N/A",
                                 "Labels": ",".join(f"{k}={val}" for k, val in v["labels"].items())})
            out = "\n".join(json.dumps(r) for r in rows)
        elif a[:2] == ["volume", "inspect"]:
            v = self.volumes.get(a[-1])
            fmt = a[a.index("--format") + 1] if "--format" in a else ""
            if v is None:
                rc = 1
            elif fmt == "{{.CreatedAt}}":
                out = v["created"]
            else:
                out = v["labels"].get(self._label_key(fmt), "")
        elif a[:2] == ["volume", "rm"]:
            rc = 0 if self.volumes.pop(a[-1], None) is not None else 1
        return subprocess.CompletedProcess(argv, rc, out, "")


class FakeDisk:
    """statvfs of the host's `/`: what is used is a base (everything that is not BOSS's) plus the
    fake Docker's images, cache and volumes, plus 5 GB per backup BOSS can see."""

    def __init__(self, docker: FakeDocker, backups: Path) -> None:
        self.docker, self.backups = docker, backups
        self.base = 268 * GB

    def used(self) -> float:
        n = sum(1 for p in self.backups.iterdir() if p.is_dir() and (p / "manifest.json").is_file())
        return self.base + self.docker.images_bytes() + self.docker.cache + self.docker.volumes_bytes() \
            + n * BACKUP_GB * GB

    def usage(self, _path="/"):
        total = TOTAL_GB * GB
        used = self.used()
        return types.SimpleNamespace(total=total, used=used, free=total - used)

    def free_pct(self) -> float:
        return 100.0 * (TOTAL_GB * GB - self.used()) / (TOTAL_GB * GB)


def _make_backups(root: Path, ages_days: list[int], start: int) -> None:
    now = time.time()
    for k, age in enumerate(ages_days):
        d = root / f"boss-202610{start + k:02d}T000000Z-{100 + start + k}"
        d.mkdir()
        (d / "boss.dump").write_bytes(b"x" * 1024)
        (d / "manifest.json").write_text(json.dumps({"complete": True, "install_id": INSTALL}))
        os.utime(d, (now - age * 86400, now - age * 86400))


# ── the bench-server databases this run owns ────────────────────────────────────────────────

@pytest.fixture()
def databases(engine):
    """An EXPIRED stamped scratch DB (BOSS's to drop), an unstamped scratch DB (no birthday: not
    automatic) and a non-scratch DB (never automatic). Tiny; dropped on the way out."""
    stamp_old = (datetime.now(timezone.utc) - timedelta(days=3)).strftime("%Y%m%dt%H%M%S")
    names = {"expired": f"boss_scratch_st14x{TAG}_{stamp_old}",
             "unstamped": f"boss_scratch_st14nostamp{TAG}",
             "keep": f"boss_st14keep{TAG}"}
    auto = create_engine(engine.url, future=True, isolation_level="AUTOCOMMIT")
    with auto.connect() as c:
        for name in names.values():
            c.execute(text(f'CREATE DATABASE "{name}"'))
    yield names
    with auto.connect() as c:
        for name in names.values():
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
    auto.dispose()


@pytest.fixture()
def world(engine, tmp_path, monkeypatch, databases):
    steward = _steward()
    install = tmp_path / "install"
    install.mkdir()
    (install / ".env").write_text(f"BOSS_INSTALL_ID={INSTALL}\nBOSS_HOST_ID={HOST}\nBOSS_HOST_LABEL=st14 laptop\n"
                                  "BOSS_HOST_API_KEY=fake\nBOSS_COMMUNITY_IMAGE_TAG=v1\nBOSS_DEDICATED_HOST=1\n")
    backups = tmp_path / "boss-backups"
    backups.mkdir()
    _make_backups(backups, list(range(39, 25, -1)), start=1)            # 14 backups, 26–39 days old
    (backups / "my-photos").mkdir()
    (backups / "boss-backup-x").write_bytes(b"an operator's own file")
    docker = FakeDocker()
    disk = FakeDisk(docker, backups)
    env = steward.Env(str(install), api_url="http://127.0.0.1:1", backup_root=str(backups),
                      state_dir=str(tmp_path / "state"), docker=["fake-docker"])
    env.is_mac = False
    # THE STATVFS SEAM: the helper's collect() reads shutil.disk_usage("/").
    monkeypatch.setattr(steward, "shutil", types.SimpleNamespace(disk_usage=disk.usage, rmtree=shutil.rmtree))
    real_walk = steward.walk_backups

    def walk_scaled(e):
        return [{**b, "bytes": BACKUP_GB * GB} for b in real_walk(e)]

    monkeypatch.setattr(steward, "walk_backups", walk_scaled)
    monkeypatch.setenv("BOSS_INSTALL_ID", INSTALL)
    # The scratch-DB step sees the WHOLE bench server; this run only ever offers its own
    # databases to it (another agent's scratch DB on the shared server is not ours to test on).
    real_api_tool = storage_cleanup.run_api_tool

    def own_dbs_only(eng, conn, tool, args, *, dry_run, expected=None):
        items, size = real_api_tool(eng, conn, tool, args, dry_run=dry_run, expected=expected)
        if tool == "storage.drop_scratch_dbs" and dry_run:
            mine = [i for i in items if TAG in i]
            if len(mine) != len(items):
                size = sum(int(conn.execute(text("SELECT pg_database_size(:n)"), {"n": n}).scalar() or 0)
                           for n in mine)
            items = mine
        return items, size

    monkeypatch.setattr(storage_cleanup, "run_api_tool", own_dbs_only)
    with engine.begin() as c:
        work_hold.release(c, actor="test", reason="reset")
        c.execute(text("DELETE FROM retention_rules WHERE kind = 'backups'"))
        c.execute(text("UPDATE storage_incidents SET status = 'resolved', resolved_at = now() "
                       "WHERE status <> 'resolved'"))
    work_hold.invalidate_cache()
    yield {"steward": steward, "env": env, "docker": docker, "disk": disk, "backups": backups,
           "dbs": databases}
    with engine.begin() as c:
        work_hold.release(c, actor="test", reason="suite done")
        c.execute(text("DELETE FROM system_state WHERE key = :k"), {"k": work_hold.KEY})
        c.execute(text("DELETE FROM retention_rules WHERE kind = 'backups'"))
    work_hold.invalidate_cache()


# ── one pass of each actor ──────────────────────────────────────────────────────────────────

def _report_reading(engine, world) -> dict:
    """The helper's sampling half: collect() over the fake statvfs → the reading BOSS records
    (exactly what POST /v1/infra/storage/samples does with it), then the watch's tick."""
    s = world["steward"].collect(world["env"], runner=world["docker"])
    s.pop("at", None)                         # BOSS stamps it (sub-second: two readings never merge)
    with engine.begin() as c:
        storage_watch.record_sample(c, storage_watch.clean_sample(s, source="steward"))
    return storage_watch.tick(engine, sample_api=False)


def _helper_turn(engine, world) -> dict | None:
    """The helper's job half: lease → verify signature → gate → act on the fake Docker/backups → report."""
    with engine.begin() as c:
        job = host_jobs.lease(c, host_id=HOST, leased_to="steward-st14")
        pub = host_jobs.public_key(c)
    if job is None:
        return None
    result = world["steward"].perform(world["env"], job, pub, runner=world["docker"])
    with engine.begin() as c:
        host_jobs.record_result(c, job["id"], result)
    return {"job": job, "result": result}


def _incident(engine) -> dict | None:
    with engine.begin() as c:
        return storage_incidents.current_for_host(c, HOST)


def _states(inc) -> dict[str, str]:
    return {s["tool"]: s["state"] for s in (inc.get("plan") or [])}


def _clean_until_it_asks(engine, world, *, rounds: int = 60) -> dict:
    """Let the executor and the helper run (reading the disk as they go) until the plan waits on
    a person or has nothing left to do."""
    for _ in range(rounds):
        storage_cleanup.advance_all(engine)
        turn = _helper_turn(engine, world)
        if turn is not None:
            assert turn["result"]["ok"], turn["result"]
        inc = _incident(engine)
        if inc is None:
            return {}
        if inc["status"] == "asking" or (turn is None and all(
                s["state"] in storage_cleanup.TERMINAL or s["state"] == "ask" for s in inc["plan"])):
            return inc
    raise AssertionError(f"the plan never settled: {_states(_incident(engine) or {})}")


def _dbs_present(engine) -> set[str]:
    with engine.connect() as c:
        return set(c.execute(text("SELECT datname FROM pg_database")).scalars().all())


# ── THE PROOF ───────────────────────────────────────────────────────────────────────────────

def test_a_full_disk_is_noticed_explained_cleaned_safely_asked_resumed_reported_and_learned(
        client, engine, world):
    docker, disk, backups, dbs = world["docker"], world["disk"], world["backups"], world["dbs"]
    from boss_engine.config import Settings
    from boss_engine.services.jobs import JobLeaseService

    svc = JobLeaseService(Settings())
    with engine.begin() as c:
        project = storage_incidents.default_project(c)
        # expired telemetry (anomaly signals are kept 30 days) and a fresh one that stays
        for i, age in enumerate((60, 61, 62, 1)):
            c.execute(text("INSERT INTO anomaly_signals (id, project_id, kind, signature, ts) VALUES "
                           "(:id, :p, 'metric_spike', 'st14', now() - make_interval(days => :d))"),
                      {"id": f"as_st14_{TAG}_{i}", "p": project, "d": age})
        # agent work in flight: a RUNNING step (leased) and a build waiting its turn
        c.execute(text("INSERT INTO jobs (id, project_id, kind, payload, status, run_after, created_at) VALUES "
                       "(:id, :p, 'meeting.step', '{}'::jsonb, 'queued', now(), clock_timestamp())"),
                  {"id": f"job_st14_run_{TAG}", "p": project})
        running = svc.claim_job(c, "w_st14", ["meeting.step"], 300)
        assert running is not None and str(running.get("job_id") or running.get("id")) == f"job_st14_run_{TAG}"
        c.execute(text("INSERT INTO jobs (id, project_id, kind, payload, status, run_after, created_at) VALUES "
                       "(:id, :p, 'code.task', '{}'::jsonb, 'queued', now(), clock_timestamp())"),
                  {"id": f"job_st14_build_{TAG}", "p": project})

    # ── 0. healthy: 13 % free is coming, but first the host is fine ──
    disk.base -= 70 * GB
    assert disk.free_pct() > 25
    for _ in range(2):
        _report_reading(engine, world)
    assert _incident(engine) is None, "a healthy host opens nothing"

    # ── 1. the disk fills: ONE bad reading is a blip, the second opens a critical incident ──
    disk.base += 70 * GB
    assert disk.free_pct() < 15, disk.free_pct()
    _report_reading(engine, world)
    assert _incident(engine) is None, "one bad reading never opens an incident (hysteresis)"
    _report_reading(engine, world)
    inc = _incident(engine)
    assert inc is not None and inc["severity"] == "critical", inc

    # ── 2. it explains: one plain sentence, the biggest avoidable tenant ──
    assert inc["cause_key"] == "backups.no_retention"
    assert "backups hold 70 GB" in inc["cause_text"] and "nothing removed the old ones" in inc["cause_text"]
    tiers = [s["tier"] for s in inc["plan"]]
    assert tiers == sorted(tiers), "safest first"
    assert {s["tool"]: s["tier"] for s in inc["plan"]}["storage.remove_backups"] == 3, \
        "irreversible with no rule: a person decides"

    # ── 3. it pauses agent work: nothing new is claimed; the running step hands back ──
    work_hold.invalidate_cache()
    with engine.begin() as c:
        assert work_hold.active(c, fresh=True) and work_hold.state(c)["incident_id"] == inc["id"]
        assert svc.claim_job(c, "w_st14", ["code.task", "meeting.step"], 300) is None, "no new work claimed"
        build = c.execute(text("SELECT status, attempt, last_error FROM jobs WHERE id = :i"),
                          {"i": f"job_st14_build_{TAG}"}).mappings().one()
        assert build["status"] == "queued" and build["attempt"] == 0
        assert build["last_error"]["code"] == "infra_hold" and build["last_error"]["incident_id"] == inc["id"]
    # the running step reaches its next checkpoint, sees the hold, and hands its job back
    assert work_hold.drain_or_hold(engine, None, "meeting.step", None)() is True
    with engine.begin() as c:
        out = work_hold.hand_back(c, svc, worker_id="w_st14", job_id=f"job_st14_run_{TAG}",
                                  lease_token=running["lease_token"])
        assert out["handed_back"] and out["incident_id"] == inc["id"]
        step_row = c.execute(text("SELECT status, attempt, lease_owner FROM jobs WHERE id = :i"),
                             {"i": f"job_st14_run_{TAG}"}).mappings().one()
        assert step_row["status"] == "queued" and step_row["attempt"] == 0 and step_row["lease_owner"] is None

    # the strip and the Command Center say so, over the API the console polls
    cur = client.get("/v1/infra/storage/incidents/current")
    assert cur.status_code == 200, cur.text
    strip = next(i for i in cur.json()["incidents"] if i["id"] == inc["id"])
    assert strip["severity"] == "critical" and strip["hold"]["active"]
    assert strip["hold"]["paused_runs"] >= 2, strip["hold"]
    assert strip["cause_text"] == inc["cause_text"]
    assert [n["key"] for n in strip["flow"]][:3] == ["detected", "paused", "cleaning"]
    assert all(len(n["label"].split()) <= 4 for n in strip["flow"])
    assert cur.json()["hold"]["active"] and cur.json()["hold"]["incident_id"] == inc["id"]
    view = client.get("/v1/command-center/view", params={"project_id": project})
    assert view.status_code == 200, view.text
    assert view.json()["status"]["key"] == "infra_incident", view.json()["status"]
    assert view.json()["status"]["text"] == "Cleanup queued"
    # Complete the signed dry run. The resulting real cleanup job now owns the running step.
    dry_run = _helper_turn(engine, world)
    assert dry_run and dry_run["result"]["ok"] and dry_run["result"]["dry_run"]
    storage_cleanup.advance_all(engine)
    active = client.get("/v1/command-center/view", params={"project_id": project})
    assert active.json()["status"]["text"] == "Infra Agent is cleaning"
    assert view.json()["infra_incident"]["id"] == inc["id"]

    # ── 4. it cleans what is safe, by itself, and asks ONE question for the rest ──
    inc = _clean_until_it_asks(engine, world)
    states = _states(inc)
    assert inc["status"] == "asking", states
    assert states["storage.prune_build_cache"] == "done" and docker.cache == 10 * GB
    assert states["storage.remove_images"] == "done"
    assert states["storage.remove_orphan_volumes"] == "done"
    vol_step = next(s for s in inc["plan"] if s["tool"] == "storage.remove_orphan_volumes")
    assert vol_step["freed_gb"] == 40.0, "the orphan test volume's measured size, not 0 (BOSSAPP-1193)"
    assert states["storage.drop_scratch_dbs"] == "done"
    assert states["storage.drop_partitions"] == "done", "telemetry past its default rule: tier 2, automatic"
    assert states["storage.remove_backups"] == "ask"
    tags = {f"{i['Repository']}:{i['Tag']}" for i in docker.images}
    assert "boss-community-api:v0" not in tags, "the old labelled image went"
    assert {"boss-community-api:v1", "boss-community-api:prev-v1", "my/tool:latest"} <= tags, \
        "current, rollback and unlabelled images stay"
    assert f"boss_test_pg_{TAG}" not in docker.volumes, "the orphan test volume went"
    assert f"boss_preview_pgdata_{TAG}" in docker.volumes, "a labelled DATA volume is never automatic"
    assert f"my_own_data_{TAG}" in docker.volumes, "an unlabelled volume is never BOSS's"
    present = _dbs_present(engine)
    assert dbs["expired"] not in present, "the expired stamped scratch DB went"
    assert dbs["unstamped"] in present, "a scratch DB with no birthday in its name is not automatic"
    assert dbs["keep"] in present, "a non-scratch database is never automatic"
    with engine.begin() as c:
        left = set(c.execute(text("SELECT id FROM anomaly_signals WHERE id LIKE :p"),
                             {"p": f"as_st14_{TAG}_%"}).scalars().all())
    assert left == {f"as_st14_{TAG}_3"}, "expired telemetry went; the fresh signal stayed"
    assert sum(1 for p in backups.iterdir() if p.name.startswith("boss-2026")) == 14, \
        "nothing irreversible happened without a person"
    # still short of the target (25 %) but above the critical line: it stays critical, no flap
    _report_reading(engine, world)
    assert 15 < disk.free_pct() < 25 and _incident(engine)["status"] == "asking"
    with engine.begin() as c:
        assert work_hold.active(c, fresh=True), "the hold stays while the host is below target"

    # every real action is receipted; every one was dry-run first
    detail = client.get(f"/v1/infra/storage/incidents/{inc['id']}")
    assert detail.status_code == 200, detail.text
    body = detail.json()
    real = [a for a in body["actions"] if not a["dry_run"] and a["status"] == "done"]
    assert {a["tool"] for a in real} >= {"storage.prune_build_cache", "storage.remove_images",
                                         "storage.remove_orphan_volumes", "storage.drop_scratch_dbs",
                                         "storage.drop_partitions"}
    assert all(a["receipt_id"] for a in real), real
    receipts = {r["id"]: r for r in body["receipts"]}
    assert all(receipts[a["receipt_id"]]["status"] == "succeeded" for a in real)
    dry = {a["tool"] for a in body["actions"] if a["dry_run"]}
    assert {a["tool"] for a in real} <= dry, "no real run without its dry run"
    card = next(d for d in body["decisions"] if d["kind"] == "storage_delete")
    assert card["status"] == "pending"

    with engine.begin() as c:
        rec = c.execute(text("SELECT recommendation FROM decisions WHERE id = :d"), {"d": card["id"]}).scalar()
    assert rec["items"] == 10 and rec["rule_proposal"]["kind"] == "backups"
    assert rec["rule_proposal"]["preticked"] is False, "first time: no rule pre-ticked"
    assert "data volumes" in rec["never_automatic"] and "non-scratch databases" in rec["never_automatic"]

    # ── a person approves the card (no rule): exactly the dry run's ten go ──
    ok = client.post(f"/v1/decisions/{card['id']}/approve", json={"actor": "ceo", "answer": {"make_rule": False}})
    assert ok.status_code == 200, ok.text
    for _ in range(6):
        storage_cleanup.advance_all(engine)
        if _helper_turn(engine, world) is None and _states(_incident(engine))["storage.remove_backups"] == "done":
            break
    assert _states(_incident(engine))["storage.remove_backups"] == "done"
    kept = sorted(p.name for p in backups.iterdir() if p.name.startswith("boss-2026"))
    assert len(kept) == 4, kept
    assert sorted(set(f"boss-202610{i:02d}T000000Z-{100 + i}" for i in range(1, 15)) - set(kept)) == \
        sorted(rec["to_delete"]), "only the dry run's items"
    assert (backups / "my-photos").exists() and (backups / "boss-backup-x").exists()

    # ── 5. free space back above target: ONE good reading resumes work, TWO resolve ──
    assert disk.free_pct() >= 25, disk.free_pct()
    _report_reading(engine, world)
    inc = _incident(engine)
    assert inc is not None and inc["status"] == "resuming", "one good reading resumes, it does not resolve"
    with engine.begin() as c:
        assert not work_hold.active(c, fresh=True), "paused work resumes once the target is reached"
        due = c.execute(text("SELECT id FROM jobs WHERE id = ANY(:ids) AND status = 'queued' "
                             "AND run_after <= now() AND last_error->>'resumed_at' IS NOT NULL"),
                        {"ids": [f"job_st14_run_{TAG}", f"job_st14_build_{TAG}"]}).scalars().all()
    assert len(due) == 2, "both the handed-back step and the waiting build are due again"
    _report_reading(engine, world)
    assert _incident(engine) is None, "two good readings resolve it"
    with engine.begin() as c:
        done = dict(c.execute(text("SELECT * FROM storage_incidents WHERE id = :i"),
                              {"i": inc["id"]}).mappings().one())
    rep = done["report"]
    assert done["status"] == "resolved" and rep["lost_or_repeated"] == 0 and rep["paused_runs"] == 2
    by = {r["type"]: r["by"] for r in rep["removed"]}
    assert by["Remove old backups"] == "person" and by["Prune build cache"] == "rebuildable"
    assert rep["learned"]["rule_proposed"] is None, "a cause seen once proposes nothing yet"
    md = client.get(f"/v1/infra/storage/incidents/{inc['id']}/report.md")
    assert md.status_code == 200 and md.text.startswith("# Storage incident") and "Lost or repeated: 0" in md.text
    assert "by person" in md.text and "receipt" in md.text
    view = client.get("/v1/command-center/view", params={"project_id": project}).json()
    assert view["status"]["key"] != "infra_incident" and view["infra_incident"] is None

    # ── 6. the same cause again: the card comes pre-ticked; "Not now" deletes nothing; resolving
    #       proposes a permanent rule; approving it makes the next cleanup automatic ──
    _make_backups(backups, list(range(20, 10, -1)), start=15)          # ten more, 11–20 days old
    disk.base += 50 * GB
    assert disk.free_pct() < 15, disk.free_pct()
    for _ in range(2):
        _report_reading(engine, world)
    inc2 = _incident(engine)
    assert inc2 is not None and inc2["id"] != inc["id"] and inc2["cause_key"] == "backups.no_retention"
    inc2 = _clean_until_it_asks(engine, world)
    assert f"boss_preview_pgdata_{TAG}" in docker.volumes and f"my_own_data_{TAG}" in docker.volumes
    step = next(s for s in inc2["plan"] if s["tool"] == "storage.remove_backups")
    assert step["state"] == "ask" and step["tier"] == 3
    with engine.begin() as c:
        rec2 = c.execute(text("SELECT recommendation FROM decisions WHERE id = :d"),
                         {"d": step["decision_id"]}).scalar()
    assert rec2["rule_proposal"]["preticked"] is True, "seen twice: the rule box comes ticked"
    no = client.post(f"/v1/decisions/{step['decision_id']}/reject", json={"actor": "ceo"})
    assert no.status_code == 200, no.text
    n_before = sum(1 for p in backups.iterdir() if p.name.startswith("boss-2026"))
    storage_cleanup.advance_all(engine)
    assert _helper_turn(engine, world) is None, "a declined card runs nothing"
    assert sum(1 for p in backups.iterdir() if p.name.startswith("boss-2026")) == n_before == 14
    # the operator frees space their own way; BOSS resumes and resolves
    disk.base -= 70 * GB
    for _ in range(2):
        _report_reading(engine, world)
    assert _incident(engine) is None
    with engine.begin() as c:
        done2 = c.execute(text("SELECT report FROM storage_incidents WHERE id = :i"), {"i": inc2["id"]}).scalar()
    proposed = done2["learned"]["rule_proposed"]
    assert proposed and proposed["kind"] == "backups", done2["learned"]
    with engine.begin() as c:
        rule_card = c.execute(text("SELECT status, recommendation FROM decisions WHERE id = :d"),
                              {"d": proposed["decision_id"]}).mappings().one()
    assert rule_card["status"] == "pending" and rule_card["recommendation"]["action"] == "storage_rule"
    yes = client.post(f"/v1/decisions/{proposed['decision_id']}/approve", json={"actor": "ceo"})
    assert yes.status_code == 200, yes.text
    with engine.begin() as c:
        rule = storage_rules.rule_for(c, "backups", HOST)
        assert rule and rule["source"] == "learned"
        plan = storage_cleanup.build_plan(c, {"id": "si_probe", "host_id": HOST},
                                          {"backups_gb": 70.0, "total_gb": TOTAL_GB, "free_gb": 60.0})
    assert next(s["tier"] for s in plan if s["tool"] == "storage.remove_backups") == 2, \
        "approved once: the next backups cleanup is automatic"
