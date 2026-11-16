"""The fake-disk scenario (LLD §9 e2e, scaled to a unit): a host reports a nearly full disk twice →
the watch opens a critical incident, sets the install-wide hold and plans tiers 1–3 → the
executor dry-runs and runs tier 1 through signed host jobs the (real) steward performs against a
fake Docker and a TEMPORARY backups folder → still short of target, the ONE tier-3 card asks →
approving it with the rule box ticked deletes exactly the dry run's backups, receipted → the host
reports free space above target → the incident resolves, the hold releases, the report says
lost_or_repeated = 0 → the next incident plans backups as tier 2 (automatic after one ask).

Never the laptop's install: Docker is a fake, the backups folder is tmp_path, the Postgres-side
tools are stubbed to "nothing to do" (the bench server is shared), and the hold is released on
the way out.
"""
from __future__ import annotations

import base64
import importlib.util
import json
import os
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import text

from boss_engine.services import (host_jobs, storage_cleanup, storage_incidents, storage_rules,
                                  storage_watch, work_hold)

PG_URL = os.environ.get("BOSS_TEST_DATABASE_URL", "")
ROOT = Path(__file__).resolve().parents[2]
HOST = f"host_fake_{uuid.uuid4().hex[:6]}"
INSTALL = f"inst_fake_{uuid.uuid4().hex[:6]}"


def _steward():
    spec = importlib.util.spec_from_file_location("_steward", ROOT / "scripts" / "install" / "steward.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def pg():
    if not PG_URL.startswith("postgres"):
        pytest.skip("needs BOSS_TEST_DATABASE_URL")
    from sqlalchemy import create_engine

    from boss_engine.db import ensure_schema

    eng = create_engine(PG_URL, future=True)
    ensure_schema(eng)
    with eng.begin() as c:
        work_hold.release(c, actor="test", reason="reset")
        c.execute(text("DELETE FROM retention_rules WHERE kind = 'backups'"))
    work_hold.invalidate_cache()
    yield eng
    with eng.begin() as c:
        work_hold.release(c, actor="test", reason="suite done")
        c.execute(text("DELETE FROM system_state WHERE key = :k"), {"k": work_hold.KEY})
        ids = [r[0] for r in c.execute(text("SELECT id FROM storage_incidents WHERE host_id = :h"), {"h": HOST}).all()]
        if ids:
            c.execute(text("DELETE FROM decisions WHERE recommendation->>'incident_id' = ANY(:ids)"), {"ids": ids})
            c.execute(text("DELETE FROM receipts WHERE dedupe_key LIKE ANY(:pats)"),
                      {"pats": [f"storage:{i}:%" for i in ids]})
        c.execute(text("DELETE FROM host_jobs WHERE host_id = :h"), {"h": HOST})
        c.execute(text("DELETE FROM storage_incidents WHERE host_id = :h"), {"h": HOST})
        c.execute(text("DELETE FROM storage_samples WHERE host_id = :h"), {"h": HOST})
        c.execute(text("DELETE FROM retention_rules WHERE kind = 'backups'"))
    work_hold.invalidate_cache()
    eng.dispose()


class FakeDocker:
    def __init__(self):
        self.removed = []
        self.cache_gb = 18.0

    def __call__(self, argv, timeout=120.0):
        a = argv[1:]
        out = ""
        if a[:2] == ["system", "df"]:
            out = "\n".join(json.dumps(r) for r in [
                {"Type": "Images", "Size": "30GB", "Reclaimable": "10GB"},
                {"Type": "Build Cache", "Size": f"{self.cache_gb:g}GB", "Reclaimable": f"{self.cache_gb:g}GB"},
                {"Type": "Local Volumes", "Size": "4GB", "Reclaimable": "4GB"},
                {"Type": "Containers", "Size": "600MB", "Reclaimable": "0B"}])
        elif a[:2] == ["builder", "prune"]:
            self.cache_gb = 10.0
            self.removed.append("build-cache")
        elif a[:1] == ["images"]:
            out = json.dumps({"Repository": "boss-community-api", "Tag": "v0", "ID": "sha3", "Size": "5GB"})
        elif a[:2] == ["image", "inspect"]:
            out = INSTALL
        elif a[:1] == ["ps"]:
            out = "boss-community-api:v1"
        elif a[:1] == ["rmi"]:
            self.removed.append(a[-1])
        elif a[:2] == ["volume", "ls"]:
            out = ""
        return subprocess.CompletedProcess(argv, 0, out, "")


@pytest.fixture()
def world(pg, tmp_path, monkeypatch):
    steward = _steward()
    install = tmp_path / "install"
    install.mkdir()
    (install / ".env").write_text(f"BOSS_INSTALL_ID={INSTALL}\nBOSS_HOST_ID={HOST}\n"
                                  "BOSS_HOST_API_KEY=fake\nBOSS_COMMUNITY_IMAGE_TAG=v1\nBOSS_DEDICATED_HOST=1\n")
    backups = tmp_path / "boss-backups"
    backups.mkdir()
    now = time.time()
    for i in range(14):
        # as backup.sh writes them: boss-<stamp>-<pid> with a complete manifest naming this install
        d = backups / f"boss-202610{i + 1:02d}T000000Z-{100 + i}"
        d.mkdir()
        (d / "boss.dump").write_bytes(b"x" * 4096)
        (d / "manifest.json").write_text(json.dumps({"complete": True, "install_id": INSTALL}))
        os.utime(d, (now - (40 - i) * 86400, now - (40 - i) * 86400))
    (backups / "my-photos").mkdir()
    (backups / "boss-backup-x").write_bytes(b"an operator's own file")
    env = steward.Env(str(install), api_url="http://127.0.0.1:1", backup_root=str(backups),
                      state_dir=str(tmp_path / "state"), docker=["fake-docker"])
    env.is_mac = False
    monkeypatch.setenv("BOSS_INSTALL_ID", INSTALL)
    # The Postgres-side tools never touch the shared bench server from a test.
    monkeypatch.setattr(storage_cleanup, "run_api_tool", lambda *a, **k: ([], 0))
    docker = FakeDocker()
    return {"steward": steward, "env": env, "backups": backups, "docker": docker}


def _sample(c, free_pct: float, *, backups_n: int, backups_gb: float, cache_gb: float = 18.0,
            images_gb: float = 30.0, at: datetime | None = None):
    total = 500.0
    s = storage_watch.clean_sample({"host_id": HOST, "host_label": "fake host", "total_gb": total,
                                    "free_gb": total * free_pct / 100.0, "cache_gb": cache_gb,
                                    "images_gb": images_gb, "backups_gb": backups_gb, "backups_n": backups_n},
                                   source="steward")
    if at is not None:
        s["at"] = at
    storage_watch.record_sample(c, s)
    return s


def _steward_turn(pg, world) -> dict | None:
    """One pass of the helper against BOSS's queue: lease → verify → gate → act → report."""
    steward, env, docker = world["steward"], world["env"], world["docker"]
    with pg.begin() as c:
        job = host_jobs.lease(c, host_id=HOST, leased_to="steward-test")
        pub = host_jobs.public_key(c)
    if job is None:
        return None
    # the API-side check agrees with the helper's own verifier
    assert host_jobs.verify(pub["public_key"], host_jobs.canonical(job["id"], job["host_id"], job["verb"],
                                                                   job["args"], job["expires_at"]), job["signature"])
    result = steward.perform(env, job, pub, runner=docker)
    with pg.begin() as c:
        host_jobs.record_result(c, job["id"], result)
    return {"job": job, "result": result}


def _incident(pg):
    with pg.begin() as c:
        return storage_incidents.current_for_host(c, HOST)


def _plan_states(inc) -> dict[str, str]:
    return {s["tool"]: s["state"] for s in (inc.get("plan") or [])}


def test_fake_disk_detect_hold_clean_ask_resume(pg, world):
    docker, backups = world["docker"], world["backups"]
    # ── detect: one bad reading does nothing; the second opens a critical incident + hold ──
    with pg.begin() as c:
        _sample(c, 9.0, backups_n=14, backups_gb=170.0)
    storage_watch.tick(pg, sample_api=False)
    assert _incident(pg) is None, "one blip never opens an incident"
    with pg.begin() as c:
        _sample(c, 9.0, backups_n=14, backups_gb=170.0)
    storage_watch.tick(pg, sample_api=False)
    inc = _incident(pg)
    assert inc is not None and inc["severity"] == "critical" and inc["status"] in ("critical", "asking")
    assert inc["cause_key"] == "backups.no_retention"
    with pg.begin() as c:
        assert work_hold.active(c, fresh=True) and work_hold.state(c)["incident_id"] == inc["id"]
        pub = storage_incidents.public(c, inc)
    assert pub["hold"]["active"] and [n["key"] for n in pub["flow"]][:3] == ["detected", "paused", "cleaning"]
    assert all(len(n["label"].split()) <= 4 for n in pub["flow"])
    states = _plan_states(inc)
    assert states["storage.prune_build_cache"] in ("planned", "queued") and "storage.remove_backups" in states
    assert next(s["tier"] for s in inc["plan"] if s["tool"] == "storage.remove_backups") == 3, "no rule yet"
    tiers = [s["tier"] for s in inc["plan"]]
    assert tiers == sorted(tiers), "safest first"

    # ── clean: tiers 1 run through signed host jobs the steward performs (dry run, then real) ──
    for _ in range(40):
        storage_cleanup.advance_all(pg)
        inc = _incident(pg)
        if inc["status"] == "asking":
            break
        turn = _steward_turn(pg, world)
        if turn is not None:
            assert turn["result"]["ok"], turn["result"]
    inc = _incident(pg)
    states = _plan_states(inc)
    assert inc["status"] == "asking", states
    assert states["storage.prune_build_cache"] == "done" and "build-cache" in docker.removed
    assert states["storage.remove_images"] == "done" and "boss-community-api:v0" in docker.removed
    assert states["storage.remove_backups"] == "ask"
    assert float(inc["freed_gb"]) > 0
    with pg.begin() as c:
        step = next(s for s in inc["plan"] if s["tool"] == "storage.remove_backups")
        decision = c.execute(text("SELECT * FROM decisions WHERE id = :d"), {"d": step["decision_id"]}).mappings().one()
        receipts = c.execute(text("SELECT status FROM receipts WHERE dedupe_key LIKE :p"),
                             {"p": f"storage:{inc['id']}:%"}).scalars().all()
        actions = c.execute(text("SELECT tool, dry_run, status FROM storage_actions WHERE incident_id = :i"),
                            {"i": inc["id"]}).mappings().all()
    from boss_engine.services.decision_actions import kind_of

    rec = decision["recommendation"]
    assert kind_of(decision) == "storage_delete" and decision["status"] == "pending"
    assert rec["items"] == 4 and rec["rule_proposal"]["kind"] == "backups"      # keep newest 10 of 14
    assert sorted(rec["to_delete"]) == [f"boss-202610{i:02d}T000000Z-{99 + i}" for i in (1, 2, 3, 4)]
    assert rec["plan_hash"] == step["plan_hash"]
    assert receipts and all(r == "succeeded" for r in receipts), "every real step is receipted"
    assert any(a["dry_run"] for a in actions) and any(not a["dry_run"] and a["status"] == "done" for a in actions)
    assert all(b.exists() for b in backups.iterdir()), "nothing irreversible happened without a person"

    # ── a job queued meanwhile is held, stamped with THIS incident ──
    from boss_engine.config import Settings
    from boss_engine.services.jobs import JobLeaseService

    with pg.begin() as c:
        pid = storage_incidents.default_project(c)
        job_id = f"job_fd_{uuid.uuid4().hex[:8]}"
        # run_after is Postgres now(). The claim compares it to the host clock, which
        # can sit a few dozen milliseconds behind the container, so a job stamped now()
        # is not due yet and the hold never sees it. Two seconds covers that skew.
        c.execute(text("INSERT INTO jobs (id, project_id, kind, payload, status, run_after) "
                       "VALUES (:id, :p, 'meeting.step', '{}'::jsonb, 'queued', "
                       "now() - interval '2 seconds')"), {"id": job_id, "p": pid})
        assert JobLeaseService(Settings()).claim_job(c, "w_fd", ["meeting.step"], 60) is None
        row = c.execute(text("SELECT last_error FROM jobs WHERE id = :id"), {"id": job_id}).scalar()
        assert row["code"] == "infra_hold" and row["incident_id"] == inc["id"]

    # ── ask → approve with the rule ticked: exactly the dry run's items go, receipted ──
    with pg.begin() as c:
        out = storage_cleanup.on_delete_decision(c, pg, dict(decision), verdict="approve", actor="person:ceo",
                                                 make_rule=True)
        assert out["state"] == "running", out
        rule = storage_rules.rule_for(c, "backups", HOST)
        assert rule and rule["source"] == "learned" and rule["rule"]["keep_newest"] == 10
    turn = _steward_turn(pg, world)
    assert turn and turn["result"]["ok"] and not turn["result"]["dry_run"]
    assert sorted(turn["result"]["items"]) == sorted(rec["to_delete"])
    storage_cleanup.advance_all(pg)
    inc = _incident(pg)
    assert _plan_states(inc)["storage.remove_backups"] == "done"
    assert not (backups / "boss-20261001T000000Z-100").exists() and (backups / "boss-20261005T000000Z-104").exists()
    assert (backups / "my-photos").exists() and (backups / "boss-20261014T000000Z-113").exists()
    assert (backups / "boss-backup-x").exists(), "an operator's file named like a backup is never ours"
    assert inc["status"] == "critical", "asking is over; still cleaning until the host reports"

    # ── resume: the host reports free space above target twice → resolved, released, reported ──
    for _ in range(2):
        with pg.begin() as c:
            _sample(c, 32.0, backups_n=10, backups_gb=9.0, cache_gb=10.0)
        storage_watch.tick(pg, sample_api=False)
    with pg.begin() as c:
        assert storage_incidents.current_for_host(c, HOST) is None
        assert not work_hold.active(c, fresh=True)
        done = c.execute(text("SELECT * FROM storage_incidents WHERE id = :i"), {"i": inc["id"]}).mappings().one()
        job = c.execute(text(
            "SELECT run_after <= now() + interval '2 seconds' AS due, last_error "
            "FROM jobs WHERE id = :id"), {"id": job_id}).mappings().one()
        c.execute(text("DELETE FROM jobs WHERE id = :id"), {"id": job_id})
        rep = done["report"]
        pub = storage_incidents.public(c, dict(done))
    assert done["status"] == "resolved" and done["hold_released_at"] is not None
    assert job["due"] and job["last_error"]["resumed_at"], "paused work resumes once the hold lifts"
    assert rep["lost_or_repeated"] == 0 and rep["freed_gb"] > 0 and rep["paused_runs"] >= 1
    assert any(r["by"] == "person" for r in rep["removed"]) and any(r["by"] == "rebuildable" for r in rep["removed"])
    assert rep["learned"]["rule_added"]["source"] == "learned", "the ticked rule is in force"
    assert [n["state"] for n in pub["flow"]].count("done") == len(pub["flow"]), pub["flow"]
    with pg.begin() as c:
        text_md = storage_incidents.report_markdown(c, dict(done))
    assert text_md.startswith("# Storage incident") and "Lost or repeated: 0" in text_md

    # ── learn: the same cause again plans backups as tier 2 — automatic after one ask ──
    for _ in range(2):
        with pg.begin() as c:
            _sample(c, 9.0, backups_n=30, backups_gb=120.0)
        storage_watch.tick(pg, sample_api=False)
    inc2 = _incident(pg)
    assert inc2 is not None and inc2["id"] != inc["id"]
    assert next(s["tier"] for s in inc2["plan"] if s["tool"] == "storage.remove_backups") == 2
    with pg.begin() as c:
        storage_incidents.resolve(c, inc2["id"], reason="test over")
        assert not work_hold.active(c, fresh=True)
