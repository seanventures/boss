"""The system doctor (stability spec §4c-§4d).

Safe fixes happen on their own — rate-limited, logged, with a circuit breaker. Anything that
would stop a person's work is only ever an option on a `system_health` card, in the spec's words.
"""
from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from boss_engine.services import system_doctor as doc  # noqa: E402
from boss_engine.services.admission import Capacity  # noqa: E402

CALM = Capacity(total_mb=15991, available_mb=13029, cores=8, disk_free_gb=40, disk_used_pct=48)


def _sig(**kw) -> doc.Signals:
    base = dict(cap=CALM, biggest_request_mb=1200)
    base.update(kw)
    return doc.Signals(**base)


def _kinds(found):
    return {f["kind"]: f for f in found}


# ------------------------------------------------------------------------------------------ #
# the six cards, in the spec's words
# ------------------------------------------------------------------------------------------ #

def test_an_idle_box_has_no_card():
    assert [f for f in doc.diagnose(_sig()) if f["needs_choice"]] == []


def test_builds_running_out_of_memory():
    cap = Capacity(total_mb=7800, available_mb=700, cores=4, psi_full_avg10=14,
                   psi_full_avg60=12, disk_free_gb=40)
    builds = [{"run_id": "r1", "name": "Docs board", "mem_mb": 2355},
              {"run_id": "r2", "name": "Lead inbox", "mem_mb": 1600},
              {"run_id": "r3", "name": "Sudoku", "mem_mb": 1267}]
    f = _kinds(doc.diagnose(_sig(cap=cap, builds=builds, boss_mb=5300, used_mb=7100)))[
        "memory_pressure_from_builds"]
    assert f["title"] == "Builds are running out of memory"
    assert f["detail"] == ("3 builds are using 5.1 of 7.6 GB; the machine has been waiting on "
                           "memory 12% of the last minute.")
    labels = [o["label"] for o in f["options"]]
    assert labels == ["Queue new builds (recommended)", "Pause “Docs board” build", "Keep going"]
    tips = {o["key"]: o["tip"] for o in f["options"]}
    assert tips["queue_new_builds"] == ("Run one build at a time until memory frees up. "
                                        "Nothing running is stopped.")
    assert tips["pause_largest_build"].startswith("Stops the largest build (2.3 GB).")
    assert tips["keep_going"] == "Change nothing. Builds may slow down or fail."
    assert f["severity"] == "critical"
    assert {o["key"]: o["confirm"] for o in f["options"]}["pause_largest_build"] is True


def test_something_else_is_using_the_memory():
    cap = Capacity(total_mb=7650, available_mb=1300, cores=12, psi_full_avg60=3,
                   disk_free_gb=281, scope="docker_vm")
    host = {"swap_used_mb": 12390, "swap_total_mb": 13619,
            "top": [{"name": "Chrome", "mb": 4300}, {"name": "Docker", "mb": 4198}]}
    f = _kinds(doc.diagnose(_sig(cap=cap, host_sample=host, boss_mb=1945, used_mb=6350)))[
        "memory_pressure_external"]
    assert f["title"] == "Your computer is low on memory, and most of it is not BOSS"
    assert f["detail"] == "Swap 12.1 of 13.3 GB used. Chrome 4.2 GB, Docker 4.1 GB, BOSS 1.9 GB."
    assert [o["label"] for o in f["options"]] == [
        "Run one build at a time", "Give Docker less memory: how", "Dismiss for today"]


def test_disk_filling_and_above_ninety_only_reports():
    cap = Capacity(total_mb=15991, available_mb=13029, cores=8, disk_free_gb=16,
                   disk_used_pct=80.4)
    f = _kinds(doc.diagnose(_sig(cap=cap, reclaimable_disk_gb=46)))["disk_filling"]
    assert f["title"] == "Disk is 80% full and grows ~1.3 GB per update"
    assert f["detail"] == "BOSS can free 46 GB of old build cache and images it made."
    assert [o["label"] for o in f["options"]] == ["Free 46 GB now", "Remind me at 90%"]
    assert f["options"][0]["tip"] == ("Removes only BOSS's own old builds; your apps and data "
                                      "are untouched.")
    full = Capacity(**{**cap.__dict__, "disk_used_pct": 93.0, "notes": []})
    g = _kinds(doc.diagnose(_sig(cap=full, reclaimable_disk_gb=46)))["disk_filling"]
    assert "prune_boss_disk" in g["auto"] and [o["key"] for o in g["options"]] == ["acknowledge"]


def test_a_stuck_build():
    last = datetime.now(timezone.utc).replace(microsecond=0)
    from datetime import timedelta
    stuck = [{"job_id": "j1", "run_id": "r1", "title": "Lead inbox", "phase": "npm install",
              "heartbeat_at": last, "updated_at": last - timedelta(minutes=18)}]
    f = _kinds(doc.diagnose(_sig(stuck=stuck)))["stuck_job"]
    assert f["title"] == "“Lead inbox” build has not moved for 18 min"
    assert f["detail"].startswith("Last activity: npm install. No output since ")
    assert [o["label"] for o in f["options"]] == [
        "Restart this step", "Cancel build", "Wait 15 more minutes"]


def test_updates_restarting_builds():
    f = _kinds(doc.diagnose(_sig(deploy_restarts_hour=4)))["deploy_churn_killing_builds"]
    assert f["title"] == "Updates restarted a build 4 times in an hour"
    assert f["detail"] == "Each restart redid clone and install (~6 min)."
    assert [o["label"] for o in f["options"]] == [
        "Hold updates while builds run (recommended)", "Keep updating immediately"]
    assert "deploy_churn_killing_builds" not in _kinds(
        doc.diagnose(_sig(deploy_restarts_hour=4, deploy_hold=True)))


def test_a_machine_too_small():
    cap = Capacity(total_mb=2900, available_mb=2500, cores=2, disk_free_gb=30)
    f = _kinds(doc.diagnose(_sig(cap=cap, biggest_request_mb=2560)))[
        "host_too_small_for_workload"]
    assert f["title"] == ("This app needs about 2.5 GB to build; this machine has 1.8 GB to "
                          "spare")
    assert [o["label"] for o in f["options"]] == ["Build anyway, one at a time",
                                                  "Use a bigger place"]


# ------------------------------------------------------------------------------------------ #
# what may happen on its own
# ------------------------------------------------------------------------------------------ #

def test_the_doctor_never_stops_work_on_its_own():
    assert not (doc.AUTO_ACTIONS & doc.DESTRUCTIVE)
    assert doc.DESTRUCTIVE <= doc.OFFERED_OPTIONS
    cap = Capacity(total_mb=7800, available_mb=500, cores=4, psi_full_avg10=30,
                   psi_full_avg60=25, disk_free_gb=1, disk_used_pct=97)
    from datetime import timedelta
    old = datetime.now(timezone.utc) - timedelta(hours=1)
    everything = doc.diagnose(_sig(
        cap=cap, builds=[{"run_id": "r", "name": "x", "mem_mb": 5000}], boss_mb=6000,
        used_mb=7300, oom_kills_new=2, stuck=[{"job_id": "j", "run_id": "r", "title": "x",
                                               "phase": "p", "heartbeat_at": old,
                                               "updated_at": old}],
        deploy_restarts_hour=9, orphan_trees=3, zombies=5, pid1_is_init=False,
        stale_previews=2, db={"idle_tx_long": 1}, biggest_request_mb=9000))
    for f in everything:
        assert set(f["auto"]) <= doc.AUTO_ACTIONS, f["kind"]
        for o in f["options"]:
            assert o["key"] in doc.OFFERED_OPTIONS, o["key"]
            assert o["key"] in doc._CHOICES, f"no handler for {o['key']}"
        assert len(f["options"]) <= 3


# ------------------------------------------------------------------------------------------ #
# with the database: rate limit, breaker, audit, cards, choices
# ------------------------------------------------------------------------------------------ #

PG_URL = os.environ.get("BOSS_TEST_DATABASE_URL", "")


@pytest.fixture(scope="module")
def pg():
    if not PG_URL.startswith("postgres"):
        pytest.skip("needs BOSS_TEST_DATABASE_URL")
    from sqlalchemy import create_engine, text

    from boss_engine.db import ensure_schema

    eng = create_engine(PG_URL, future=True)
    ensure_schema(eng)
    with eng.begin() as c:
        for t in ("system_findings", "system_doctor_actions"):
            c.execute(text(f"DELETE FROM {t}"))
        c.execute(text("DELETE FROM system_state WHERE key IN ('degradation', "
                       "'builds_at_once', 'deploy_hold')"))
    yield eng
    with eng.begin() as c:
        for t in ("system_findings", "system_doctor_actions"):
            c.execute(text(f"DELETE FROM {t}"))
        c.execute(text("DELETE FROM system_state WHERE key IN ('degradation', "
                       "'builds_at_once', 'deploy_hold')"))
    eng.dispose()


def test_rate_limit_and_circuit_breaker(pg):
    from sqlalchemy import text
    target = f"t_{uuid.uuid4().hex[:6]}"
    with pg.begin() as c:
        assert doc.allowed_auto(c, "reap_previews", target) == (True, "")
        doc.record_action(c, finding_id=None, kind="stale_previews", action="reap_previews",
                          target=target, actor="boss", automatic=True, before={}, outcome="ok")
        ok, why = doc.allowed_auto(c, "reap_previews", target)
        assert not ok and "5 minutes" in why
        # Three verified tries that changed nothing: the breaker opens for that action.
        for i in range(3):
            doc.record_action(c, finding_id=None, kind="leaked_processes",
                              action="end_orphaned_processes", target=f"{target}{i}",
                              actor="boss", automatic=True, before={"orphan_trees": 2},
                              outcome="ok")
        c.execute(text("UPDATE system_doctor_actions SET verified_at = now(), improved = false "
                       "WHERE action = 'end_orphaned_processes'"))
        ok, why = doc.allowed_auto(c, "end_orphaned_processes", f"{target}new")
        assert not ok and "did not improve" in why


def test_a_tick_opens_a_card_audits_and_a_choice_resolves_it(pg, monkeypatch):
    from sqlalchemy import text

    from boss_engine.services import command_center

    sig = _sig(deploy_restarts_hour=3)
    report = doc.tick(pg, None, sig=sig)
    assert "deploy_churn_killing_builds" in report["findings"]
    with pg.begin() as c:
        cards = doc.card_items(c, "p_none")
    card = next(i for i in cards if i["finding_kind"] == "deploy_churn_killing_builds")
    assert card["kind"] == "system_health" and card["id"].startswith("system:")
    # The Command Center lists it among what needs you, with the options as its answers.
    out = command_center.assemble(project_id="p", runs=[], steps=[], decisions=[],
                                  readiness=[], names={}, now=datetime.now(timezone.utc),
                                  system=[card])
    assert out["needs_you"][0]["kind"] == "system_health"
    assert [a["key"] for a in out["needs_you"][0]["answers"]] == ["hold_updates",
                                                                   "keep_updating"]
    got = doc.act(pg, None, card["finding_id"], "hold_updates", actor="ceo")
    assert got["result"] == "Updates now wait for running builds to finish."
    with pg.begin() as c:
        status = c.execute(text("SELECT status, chosen_by FROM system_findings WHERE id=:id"),
                           {"id": card["finding_id"]}).mappings().first()
        hold = doc._state(c, "deploy_hold")
        audit = c.execute(text("SELECT actor, automatic, before_metrics FROM "
                               "system_doctor_actions WHERE finding_id=:id"),
                          {"id": card["finding_id"]}).mappings().first()
        from boss_engine.services.drain import deploy_readiness
        ready = deploy_readiness(c)
    assert status["status"] == "resolved" and status["chosen_by"] == "ceo"
    assert hold["hold"] is True and ready["hold_while_building"] is True
    assert audit["actor"] == "ceo" and audit["automatic"] is False
    with pytest.raises(doc.ActRefused):
        doc.act(pg, None, card["finding_id"], "keep_updating", actor="ceo")


def test_a_clear_signal_resolves_the_finding_after_two_minutes(pg):
    from datetime import timedelta

    from sqlalchemy import text
    with pg.begin() as c:                      # a fresh breaker for this action
        c.execute(text("DELETE FROM system_doctor_actions"))
    doc.tick(pg, None, sig=_sig(orphan_trees=1))
    with pg.begin() as c:
        fid = c.execute(text("SELECT id FROM system_findings WHERE kind='leaked_processes' AND "
                             "status='open'")).scalar()
        assert fid
        assert all(i["finding_kind"] != "leaked_processes" for i in doc.card_items(c, "p")), \
            "an automatic fix never becomes a card"
    doc.tick(pg, None, sig=_sig())
    with pg.begin() as c:
        c.execute(text("UPDATE system_findings SET clear_since = now() - interval '3 minutes' "
                       "WHERE id = :id"), {"id": fid})
    doc.tick(pg, None, sig=_sig())
    with pg.begin() as c:
        assert c.execute(text("SELECT status FROM system_findings WHERE id=:id"),
                         {"id": fid}).scalar() == "resolved"
        acted = c.execute(text("SELECT count(*) FROM system_doctor_actions WHERE "
                               "finding_id=:id AND action='end_orphaned_processes'"),
                          {"id": fid}).scalar()
    assert acted == 1


def test_one_at_a_time_ends_when_the_pressure_does(pg):
    from sqlalchemy import text

    from boss_engine.services import admission
    with pg.begin() as c:
        c.execute(text("INSERT INTO system_findings (id, kind, title, options, needs_choice) "
                       "VALUES ('sf_mem', 'memory_pressure_external', 't', CAST(:o AS JSONB), "
                       "true) ON CONFLICT DO NOTHING"),
                  {"o": '[{"key": "one_build_at_a_time", "label": "Run one build at a time"}]'})
    doc.act(pg, None, "sf_mem", "one_build_at_a_time", actor="ceo")
    with pg.begin() as c:
        assert admission.builds_at_once(c, CALM, 1200)["value"] == 1
        doc._on_resolved(c, "sf_mem", "memory_pressure_external")
        assert admission.builds_at_once(c, CALM, 1200)["auto"] is True
