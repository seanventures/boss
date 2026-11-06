"""Each signal adapter reads its real store into the one signal shape (BOSSAPP-903; design §4 L2).

Five stores, five pull adapters, one sweep: a firing app alert, a failed and a deadlettered
workflow step, a failed build, an open doctor finding, an open shepherd escalation and a PR whose
merge question has waited days each become ONE signal with the right source, kind, subject,
severity and evidence. Sweeping again records nothing new; a re-fire of the alert is a second
occurrence of the same signal; one broken store is reported and never stops the others.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import text

from boss_engine.services.ambient import adapters, signals

from ._ambient_support import eng, project  # noqa: F401


def _workflow_run(c, pid: str) -> tuple[str, str]:
    tag = uuid4().hex[:8]
    c.execute(text("INSERT INTO workflow_definitions (id, project_id, workflow_key, name, "
                   "created_by) VALUES (:d, :p, :k, 'Ship it', 't')"),
              {"d": f"wfd_{tag}", "p": pid, "k": f"ship_{tag}"})
    c.execute(text("INSERT INTO workflow_versions (id, project_id, workflow_definition_id, "
                   "version, spec, content_hash, created_by) VALUES (:v, :p, :d, 1, '{}'::jsonb, "
                   ":h, 't')"), {"v": f"wfv_{tag}", "p": pid, "d": f"wfd_{tag}", "h": tag})
    c.execute(text("INSERT INTO workflow_runs (id, project_id, workflow_definition_id, "
                   "workflow_version_id, actor, status) VALUES (:r, :p, :d, :v, 't', 'failed')"),
              {"r": f"wfr_{tag}", "p": pid, "d": f"wfd_{tag}", "v": f"wfv_{tag}"})
    return f"wfr_{tag}", f"wfd_{tag}"


def _step(c, pid: str, run: str, step: str, status: str, error: str, iteration: int = 0) -> str:
    sid = f"wsr_{uuid4().hex[:10]}"
    c.execute(text("INSERT INTO workflow_step_runs (id, project_id, workflow_run_id, step_id, "
                   "step_kind, iteration, status, last_error) VALUES (:i, :p, :r, :s, "
                   "'capability', :it, :st, :e)"),
              {"i": sid, "p": pid, "r": run, "s": step, "st": status, "e": error,
               "it": iteration})
    return sid


def _alert(c, pid: str, *, fire_count: int = 1) -> str:
    aid = f"al_{uuid4().hex[:8]}"
    c.execute(text(
        "INSERT INTO app_alerts (id, project_id, rule_kind, alertname, fingerprint, labels, "
        "annotations, state, active_at, fired_at, last_eval_at, fire_count, value, threshold, "
        "unit) VALUES (:i, :p, 'error_rate', 'HighErrorRate', :f, CAST(:l AS JSONB), "
        "CAST(:a AS JSONB), 'firing', now(), now(), now(), :n, 6.6, 5, '%')"),
        {"i": aid, "p": pid, "f": f"afp_{pid}", "n": fire_count,
         "l": json.dumps({"severity": "sev2", "app": "order-api"}),
         "a": json.dumps({"summary": "order-api 5xx at 6.6%"})})
    return aid


def _mine(c, pid: str) -> dict[str, dict]:
    rows = c.execute(text("SELECT * FROM signals WHERE project_id = :p"), {"p": pid}).mappings()
    return {r["kind"]: dict(r) for r in rows}


def test_a_firing_alert_becomes_one_alert_signal_and_a_refire_is_a_second_occurrence(
        eng, project):
    with eng.begin() as c:
        aid = _alert(c, project)
        first = signals.sweep(c, project_id=project, only=["app_alerts"])
        again = signals.sweep(c, project_id=project, only=["app_alerts"])
        c.execute(text("UPDATE app_alerts SET fire_count = 2, last_eval_at = now() "
                       "WHERE id = :i"), {"i": aid})
        refire = signals.sweep(c, project_id=project, only=["app_alerts"])
        sig = _mine(c, project)["alert.firing"]
    assert first["app_alerts"]["outcomes"] == {"created": 1}
    assert again["app_alerts"]["outcomes"] == {"duplicate": 1}, "a re-sweep is a no-op"
    assert refire["app_alerts"]["outcomes"] == {"repeat": 1}
    assert sig["source"] == "alert" and sig["app"] == "order-api" and sig["severity"] == "high"
    assert sig["title"] == "order-api 5xx at 6.6%" and sig["occurrences"] == 2
    assert sig["evidence"][0]["kind"] == "alert" and sig["evidence"][0]["ref"] == aid


def test_failed_steps_fold_by_workflow_step_and_error_shape_and_a_failed_build_is_its_own(
        eng, project):
    with eng.begin() as c:
        run_a, _ = _workflow_run(c, project)
        _step(c, project, run_a, "build", "failed", "Timeout after 600s talking to crun_abc12345")
        _step(c, project, run_a, "build", "failed", "Timeout after 900s talking to crun_ffff9999",
              iteration=1)
        _step(c, project, run_a, "deploy", "deadlettered", "host unreachable")
        c.execute(text("INSERT INTO code_task_runs (id, project_id, title, status, error) VALUES "
                       "(:i, :p, 'Fix the cart', 'failed', 'pytest: 3 failed')"),
                  {"i": f"crun_{uuid4().hex[:8]}", "p": project})
        report = signals.sweep(c, project_id=project, only=["workflow_failures",
                                                             "build_failures"])
        rows = c.execute(text("SELECT kind, title, severity, occurrences FROM signals WHERE "
                              "project_id = :p ORDER BY title"), {"p": project}).mappings().all()
    assert report["workflow_failures"]["outcomes"] == {"created": 2, "repeat": 1}
    assert report["build_failures"]["outcomes"] == {"created": 1}
    steps = [r for r in rows if r["kind"] == "workflow.step_failed"]
    assert [(r["title"], r["occurrences"], r["severity"]) for r in steps] == [
        ("Step build failed in Ship it", 2, "medium"),
        ("Step deploy deadlettered in Ship it", 1, "high")]
    assert [r["kind"] for r in rows if r["kind"] == "build.failed"] == ["build.failed"]


def test_an_open_doctor_finding_becomes_a_doctor_signal(eng, project):
    fid = f"sf_test_{uuid4().hex[:8]}"
    with eng.begin() as c:
        c.execute(text("INSERT INTO system_findings (id, project_id, kind, severity, title) "
                       "VALUES (:i, :p, :k, 'critical', 'Disk is 97% full')"),
                  {"i": fid, "p": project, "k": f"disk_full_{fid}"})
        signals.sweep(c, project_id=project, only=["system_doctor"])
        twice = signals.sweep(c, project_id=project, only=["system_doctor"])
        sig = next(s for k, s in _mine(c, project).items() if k.startswith("doctor."))
        c.execute(text("DELETE FROM system_findings WHERE id = :i"), {"i": fid})
    assert sig["source"] == "doctor" and sig["severity"] == "critical"
    assert sig["evidence"] == [{"kind": "finding", "ref": fid}]
    assert twice["system_doctor"]["outcomes"].get("created", 0) == 0


def test_an_open_shepherd_escalation_becomes_a_shepherd_signal(eng, project):
    with eng.begin() as c:
        c.execute(text("INSERT INTO shepherd_escalations (id, project_id, task_ref, question) "
                       "VALUES (:i, :p, 'crun_x1', 'Build stuck in coding for 40 minutes — how "
                       "should this proceed?')"), {"i": f"shesc_{uuid4().hex[:8]}", "p": project})
        signals.sweep(c, project_id=project, only=["shepherd"])
        sig = _mine(c, project)["shepherd.escalation"]
    assert sig["source"] == "shepherd" and sig["severity"] == "high"
    assert sig["subject_id"] == "crun_x1" and sig["title"].startswith("Build stuck")


def test_a_pr_whose_merge_question_waited_days_becomes_a_stale_pr_signal(eng, project):
    with eng.begin() as c:
        did = f"dec_{uuid4().hex[:8]}"
        c.execute(text("INSERT INTO decisions (id, project_id, question, status, created_at) "
                       "VALUES (:d, :p, 'Merge PR #41?', 'pending', now() - interval '5 days')"),
                  {"d": did, "p": project})
        fresh = f"dec_{uuid4().hex[:8]}"
        c.execute(text("INSERT INTO decisions (id, project_id, question, status) VALUES "
                       "(:d, :p, 'Merge PR #42?', 'pending')"), {"d": fresh, "p": project})
        for n, dec in ((41, did), (42, fresh)):
            c.execute(text(
                "INSERT INTO code_task_runs (id, project_id, title, status, pull_request_url, "
                "pull_request_number, decision_id) VALUES (:i, :p, 'Cart fix', 'completed', :u, "
                ":n, :d)"), {"i": f"crun_{uuid4().hex[:8]}", "p": project, "n": n, "d": dec,
                             "u": f"https://github.com/acme/shop/pull/{n}"})
        signals.sweep(c, project_id=project, only=["stale_prs"])
        again = signals.sweep(c, project_id=project, only=["stale_prs"])
        stale = c.execute(text("SELECT * FROM signals WHERE project_id = :p AND kind = 'pr.stale'"),
                          {"p": project}).mappings().all()
    assert len(stale) == 1, "only the PR waiting past the threshold is stale"
    assert stale[0]["title"] == "PR #41 waiting 5d for a merge decision"
    assert stale[0]["subject_id"].endswith("/pull/41")
    assert again["stale_prs"]["outcomes"] == {"duplicate": 1}


def test_one_broken_store_is_reported_and_the_others_still_sweep(eng, project, monkeypatch):
    def broken(connection, since, project_id=None):
        raise RuntimeError("the store is down")
        yield  # pragma: no cover

    monkeypatch.setitem(signals.ADAPTERS, "broken_store", broken)
    with eng.begin() as c:
        _alert(c, project)
        report = signals.sweep(c, project_id=project, only=["broken_store", "app_alerts"])
        cursor = c.execute(text("SELECT cursor_at, last_error FROM signal_adapter_cursors WHERE "
                                "adapter = :a"), {"a": f"broken_store@{project}"}).one()
        c.execute(text("DELETE FROM signal_adapter_cursors WHERE adapter LIKE :a"),
                  {"a": f"%@{project}"})
    assert "the store is down" in report["broken_store"]["error"]
    assert report["app_alerts"]["outcomes"] == {"created": 1}
    assert cursor.cursor_at is None and "the store is down" in cursor.last_error, \
        "a failed adapter's cursor does not move: its rows are read again next time"


def test_a_sweep_that_hits_an_adapters_row_limit_resumes_after_the_last_row_it_read(
        eng, project, monkeypatch):
    rows = [{"project_id": project, "source": "scan", "kind": "scan.todo", "title": f"TODO {i}",
             "fingerprint_parts": ("todo", i), "source_ref": f"todo:{i}",
             "_cursor": datetime.now(timezone.utc) - timedelta(hours=5 - i)} for i in range(3)]
    monkeypatch.setitem(signals.ADAPTERS, "limited", lambda c, since, p=None: rows[:2])
    with eng.begin() as c:
        signals.sweep(c, project_id=project, only=["limited"])
        cursor = c.execute(text("SELECT cursor_at FROM signal_adapter_cursors WHERE adapter = :a"),
                           {"a": f"limited@{project}"}).scalar()
        c.execute(text("DELETE FROM signal_adapter_cursors WHERE adapter LIKE :a"),
                  {"a": f"%@{project}"})
    assert cursor == rows[1]["_cursor"], "the cursor is the last row read, not the sweep's start"


def test_the_built_in_adapters_are_registered():
    assert set(adapters.register()) <= set(signals.ADAPTERS)
    assert adapters.error_class("Timeout after 600s on crun_abc12345") == \
        adapters.error_class("Timeout after 900s on crun_9f9f9f9f")
