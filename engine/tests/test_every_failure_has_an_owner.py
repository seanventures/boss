"""EVERY FAILURE HAS AN OWNER — the invariant, enforced (services/failure_owner.py, 2026-11-19).

The operator: "Any problems in the system the Agents should be actively involved … attempt to
fix, have a proper reason why it cannot be fixed, tag Human for Action Items, so they can fix
and retry. Just leaving it as Failed is not Agentic Software factory." On the laptop 739 dead
letters sat open with nothing ever tried, beside failed builds, runs, deploys and previews.

THE GUARD, TWO HALVES:
  * STATIC — every table the code writes a failure state to (`SET status = 'failed'`,
    'deadlettered', 'dead', 'error', 'failed_final') is a failure_owner surface or is in EXEMPT
    with the reason its failures still have an owner. A new failing table with neither fails.
  * BEHAVIOUR — failed rows shaped like the laptop's, one per surface, swept once: every one
    ends `fixing` (an agent acting, said in words), `needs_person` (ONE action item with the
    reason BOSS could not fix it and Done — retry) or `resolved`; `unowned()` is empty.
No test starts a real build: BOSS's attempts are stubbed where they would.
"""
from __future__ import annotations

import ast
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text

from boss_engine.services import build_failure, decision_actions, failure_owner as fo

from ._ambient_support import eng, make_project, purge  # noqa: F401

ROOT = Path(__file__).resolve().parents[2]
FAIL_WORDS = ("failed", "deadlettered", "dead", "error", "errored", "crashed", "failed_final")


# ───────────────────────────── static: no failing table without an owner ─────────────────────────────


def _tables_that_fail_rows() -> dict[str, set[str]]:
    pat = re.compile(r"UPDATE\s+(\w+)\s+(?:\w+\s+)?SET\b(.{0,600}?)(?:WHERE|RETURNING|$)",
                     re.S | re.I)
    word = re.compile(r"\b(?:status|state|outcome|run_state)\s*=\s*'(" + "|".join(FAIL_WORDS)
                      + r")'")
    found: dict[str, set[str]] = {}
    for base in ("engine/boss_engine", "apps/api/app", "apps/worker"):
        for path in (ROOT / base).rglob("*.py"):
            src = path.read_text(encoding="utf-8", errors="ignore")
            for m in pat.finditer(src):
                if word.search(m.group(2)):
                    found.setdefault(m.group(1).lower(), set()).add(
                        str(path.relative_to(ROOT)))
    return found


def test_every_table_the_code_fails_rows_in_has_an_owner():
    owned = {t for s in fo.SURFACES.values() for t in s.tables} | set(fo.EXEMPT)
    missing = {t: sorted(p)[:3] for t, p in _tables_that_fail_rows().items()
               if t not in owned and t != "failure_owners"}
    assert not missing, (
        "These tables get rows set to a failure state, and nothing owns those failures. Add a "
        f"failure_owner surface for each (or put it in EXEMPT with why it is owned): {missing}")


def test_the_scan_sees_the_failures_it_guards():
    found = _tables_that_fail_rows()
    for table in ("code_task_runs", "jobs", "workflow_runs", "app_previews", "backups"):
        assert table in found, f"the guard no longer sees {table}'s failures"


def test_every_exemption_says_why_and_names_a_real_table():
    schema = (ROOT / "db" / "schema.sql").read_text(encoding="utf-8")
    for table, why in fo.EXEMPT.items():
        assert len(why.split()) >= 5, f"{table}: say why its failures are owned"
        assert re.search(rf"CREATE TABLE IF NOT EXISTS {table}\b", schema), table


def test_every_surface_can_be_answered_and_its_card_has_its_buttons():
    assert fo.CARD_ACTION in decision_actions.CARRIED_OUT_ON_APPROVAL
    labels = [a["label"] for a in decision_actions.allowed_actions(
        {"status": "pending", "recommendation": {"action": fo.CARD_ACTION}})]
    assert labels[:2] == ["Done — retry", "Leave it"]
    src = (ROOT / "engine/boss_engine/services/decisions.py").read_text()
    assert 'action == "failure_owner.retry"' in src
    for key, surface in fo.SURFACES.items():
        assert surface.key == key and surface.tables and surface.noun
        for name in ("failed", "classify", "still_failed"):
            assert getattr(type(surface), name) is not None


def test_the_monitor_tick_runs_the_owner_loop_last():
    src = (ROOT / "engine/boss_engine/services/monitor_scheduler.py").read_text()
    tree = ast.parse(src)
    tick = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "tick")
    body = ast.get_source_segment(src, tick) or ""
    assert body.index('guard("failure_owner"') > body.index('guard("stuck_work"')


# ───────────────────────────── classification, from the laptop's own errors ─────────────────────────────


@pytest.mark.parametrize("error, fc, rc, want", [
    ("RuntimeError: login rejected before any model call: BOSS could not run codex: WARNING: "
     "failed to clean up stale arg0 temp dirs: Permission denied (os error 13)", None, None,
     ("platform", "platform")),
    ("RuntimeError: HTTP 401", None, None, ("person", "model_login")),
    ("No GitHub credentials for /: the App has no installation for it, BOSS_GITHUB_TOKEN is not "
     "set, and this project has no repository connected in Setup.", None, None,
     ("person", "no_repository")),
    ("Client error '409 Conflict' for url 'http://api:8000/v1/knowledge/repositories/index/execute'",
     None, None, ("platform", "platform")),
    ("Merge failed: PR #5 conflicts with its base branch, so GitHub would not merge it.", None,
     None, ("person", "merge_conflict")),
    ("'backend/web': symlink, special file or path outside the repository root refused", "infra",
     "pipeline_exception", ("platform", "platform:pipeline_exception")),
    ("BOSS could not run codex: not logged in", "needs_human", "harness_unavailable",
     ("person", "model_login")),
    ("the app exited (code 1) before serving HTTP: Error [ERR_MODULE_NOT_FOUND]: Cannot find "
     "package 'fastify'", None, None, ("app", "app")),
])
def test_a_failure_is_classified_from_its_own_record_then_its_words(error, fc, rc, want):
    cls, code, action = fo.classify(error, failure_class=fc, reason_code=rc)
    assert (cls, code) == want
    if cls == "person":
        assert "Done — retry" in action, "a person's item always says how to retry"


# ───────────────────────────── behaviour: rows shaped like the laptop's ─────────────────────────────

_OWN_TABLES = ("failure_owners", "boss_recommendations", "deadletters", "jobs", "app_previews",
               "stack_deployments", "deployment_targets", "meeting_runs", "code_task_runs",
               "workflow_step_runs", "workflow_runs", "workflow_versions",
               "workflow_definitions", "events", "decisions")


@pytest.fixture()
def project(eng):  # noqa: F811
    pid = make_project(eng)
    yield pid
    with eng.begin() as c:
        for table in _OWN_TABLES:
            try:
                with c.begin_nested():
                    c.execute(text(f"DELETE FROM {table} WHERE project_id = :p"), {"p": pid})
            except Exception:  # noqa: BLE001 — a table this install lacks
                pass
    purge(eng, pid)


def _settings():
    from boss_engine.config import get_settings

    return get_settings()


def _ago(**kw):
    return datetime.now(timezone.utc) - timedelta(**kw)


def _build(c, pid, *, status="failed", fctx=None, error="boom", at=None, title="Recipe box"):
    rid = f"crun_{uuid4().hex[:20]}"
    c.execute(text(
        "INSERT INTO code_task_runs (id, project_id, title, status, error, finalize_ctx, "
        "created_at, updated_at) VALUES (:i, :p, :t, :s, :e, CAST(:f AS JSONB), :at, :at)"),
        {"i": rid, "p": pid, "t": title, "s": status, "e": error, "f": json.dumps(fctx or {}),
         "at": at or _ago(minutes=5)})
    return rid


def _dead_job(c, pid, kind, error, *, dedupe=None, payload=None, at=None,
              principal="sys_worker"):
    jid, dl = f"job_{uuid4().hex[:20]}", f"dlq_{uuid4().hex[:20]}"
    c.execute(text(
        "INSERT INTO jobs (id, project_id, kind, status, payload, last_error, dedupe_key, "
        "on_behalf_of, on_behalf_system, created_at, updated_at) VALUES (:i, :p, :k, "
        "'deadlettered', CAST(:pl AS JSONB), CAST(:e AS JSONB), :d, :who, false, :at, :at)"),
        {"i": jid, "p": pid, "k": kind, "pl": json.dumps(payload or {}), "who": principal,
         "e": json.dumps({"message": error}), "d": dedupe, "at": at or _ago(minutes=5)})
    c.execute(text(
        "INSERT INTO deadletters (id, project_id, source_kind, source_id, reason, created_at) "
        "VALUES (:d, :p, 'job', :j, :r, :at)"),
        {"d": dl, "p": pid, "j": jid, "r": error[:200], "at": at or _ago(minutes=5)})
    return jid, dl


def _workflow(c, pid, *, step_output=None, step_error="deploy failed", at=None):
    wd, wv, wr = (f"wdef_{uuid4().hex[:12]}", f"wver_{uuid4().hex[:12]}",
                  f"wfr_{uuid4().hex[:20]}")
    c.execute(text("INSERT INTO workflow_definitions (id, project_id, workflow_key, name, "
                   "created_by, namespace) VALUES (:i, :p, :k, 'Ship it', 'test', 'product')"),
              {"i": wd, "p": pid, "k": wd})
    c.execute(text("INSERT INTO workflow_versions (id, project_id, workflow_definition_id, "
                   "version, spec, content_hash, created_by) VALUES (:i, :p, :d, 1, "
                   "'{}'::jsonb, :h, 'test')"), {"i": wv, "p": pid, "d": wd, "h": wv})
    c.execute(text("INSERT INTO workflow_runs (id, project_id, workflow_definition_id, "
                   "workflow_version_id, actor, status, error, updated_at) VALUES (:i, :p, :d, "
                   ":v, 'test', 'failed', :e, :at)"),
              {"i": wr, "p": pid, "d": wd, "v": wv, "e": step_error, "at": at or _ago(minutes=2)})
    c.execute(text("INSERT INTO workflow_step_runs (id, project_id, workflow_run_id, step_id, "
                   "step_kind, status, last_error, output) VALUES (:i, :p, :r, 'deploy', "
                   "'capability', 'failed', :e, CAST(:o AS JSONB))"),
              {"i": f"wsr_{uuid4().hex[:12]}", "p": pid, "r": wr, "e": step_error,
               "o": json.dumps(step_output or {})})
    return wr


@pytest.fixture()
def stubbed(monkeypatch):
    """BOSS's attempts recorded instead of started (no real build, preview or requeue)."""
    calls: list[tuple[str, str, str]] = []

    def stub(key):
        def retry(engine, settings, jobs, item, *, cls, attempt, note):
            calls.append((key, item.id, cls))
            return f"stubbed attempt {attempt}"
        return retry

    for key in ("code_task", "preview", "workflow_run", "meeting"):
        monkeypatch.setattr(fo.SURFACES[key], "retry", stub(key))
    return calls


def _owner(c, surface, key):
    return c.execute(text("SELECT * FROM failure_owners WHERE surface = :s AND item_key = :k"),
                     {"s": surface, "k": key}).mappings().first()


def _cards(c, pid):
    return [dict(r) for r in c.execute(text(
        "SELECT id, question, recommendation FROM decisions WHERE project_id = :p AND status = "
        "'pending' AND recommendation->>'action' = :a"), {"p": pid, "a": fo.CARD_ACTION}
    ).mappings()]


def test_a_platform_failure_gets_boss_s_bounded_attempt_said_in_words(eng, project, stubbed):  # noqa: F811
    with eng.begin() as c:
        rid = _build(c, project, fctx={"failure_class": "infra", "reason_code": "sandbox_error"},
                     error="[Errno 13] Permission denied: '/var/lib/boss/sandboxes/x'")
    fo.sweep(eng, _settings(), project_id=project)
    with eng.begin() as c:
        row = _owner(c, "code_task", rid)
    assert stubbed == [("code_task", rid, "platform")]
    assert row["state"] == "fixing" and row["attempts"] == 1
    assert row["words"].startswith("BOSS is fixing this: attempt 1 of 2")
    assert json.loads(json.dumps(row["history"]))[-1]["attempt"] == 1
    assert fo.unowned(eng, project_id=project) == []


def test_a_failure_only_a_person_can_fix_is_one_action_item_with_the_reason(eng, project, stubbed):  # noqa: F811
    with eng.begin() as c:
        rid = _build(c, project, fctx={"failure_class": "needs_human",
                                       "reason_code": "harness_unavailable"},
                     error="BOSS could not run codex: not logged in")
    fo.sweep(eng, _settings(), project_id=project)
    with eng.begin() as c:
        row = _owner(c, "code_task", rid)
        cards = _cards(c, project)
    assert stubbed == [], "BOSS never retries what only a person can fix"
    assert row["state"] == "needs_person" and row["reason"] and row["decision_id"]
    assert [k["id"] for k in cards] == [row["decision_id"]]
    rec = cards[0]["recommendation"]
    assert rec["why_a_person"] and "Done — retry" in rec["what_to_do"]
    assert fo.unowned(eng, project_id=project) == []


def test_spent_platform_retries_file_a_boss_self_fix_and_hand_over_with_why(eng, project, stubbed):  # noqa: F811
    with eng.begin() as c:
        root = _build(c, project, fctx={"failure_class": "infra"}, error="lease_lost")
        for n in (1, 2):
            last = _build(c, project, error="lease_lost", at=_ago(minutes=5 - n),
                          fctx={"failure_class": "infra", "root_run_id": root,
                                "parent_run_id": root, "auto_infra_retry": n})
    fo.sweep(eng, _settings(), project_id=project)
    with eng.begin() as c:
        row = _owner(c, "code_task", root)
        recs = c.execute(text("SELECT signature FROM boss_recommendations WHERE project_id = :p"),
                         {"p": project}).scalars().all()
    assert stubbed == [] and row["item_id"] == last
    assert row["state"] == "needs_person" and row["reason"].startswith("BOSS tried 2 times")
    assert any(s.startswith("failure_owner:code_task:") for s in recs)


def test_an_attempt_a_repair_continued_is_not_a_failure_to_own(eng, project, stubbed):  # noqa: F811
    with eng.begin() as c:
        root = _build(c, project, status="blocked", at=_ago(minutes=10),
                      error="Blocked: this attempt failed its checks; the repair continued in x")
        _build(c, project, status="running", fctx={"root_run_id": root, "parent_run_id": root})
    fo.sweep(eng, _settings(), project_id=project)
    with eng.begin() as c:
        assert _owner(c, "code_task", root) is None
    assert stubbed == [] and fo.unowned(eng, project_id=project) == []


def test_old_work_is_not_rerun_by_itself_and_says_why(eng, project, stubbed):  # noqa: F811
    with eng.begin() as c:
        rid = _build(c, project, fctx={"failure_class": "infra"}, error="Internal Server Error",
                     at=_ago(days=5))
    fo.sweep(eng, _settings(), project_id=project)
    with eng.begin() as c:
        row = _owner(c, "code_task", rid)
    assert stubbed == []
    assert row["state"] == "needs_person" and "within 72 hours" in row["reason"]


def test_a_workflow_step_whose_agent_asked_a_person_becomes_its_action_item(eng, project, stubbed):  # noqa: F811
    ask = {"action": "ask_person", "agent": "Pilot", "question": "Free port 8000 on the host?",
           "reasoning": "another app outside BOSS holds port 8000"}
    with eng.begin() as c:
        wr = _workflow(c, project, step_output={"recovery": {"decisions": [ask]}},
                       step_error="Bind for 0.0.0.0:8000 failed: port is already allocated")
    fo.sweep(eng, _settings(), project_id=project)
    with eng.begin() as c:
        row = _owner(c, "workflow_run", wr)
        cards = _cards(c, project)
    assert stubbed == []
    assert row["state"] == "needs_person" and "Free port 8000" in row["action"]
    assert "Pilot looked into it" in row["reason"]
    assert cards and cards[0]["id"] == row["decision_id"]


def test_many_jobs_failing_for_one_person_side_cause_are_one_card(eng, project, stubbed):  # noqa: F811
    err = ("No GitHub credentials for /: the App has no installation for it, BOSS_GITHUB_TOKEN is "
           "not set, and this project has no repository connected in Setup.")
    with eng.begin() as c:
        for n in range(3):
            _dead_job(c, project, "docs.update", err, dedupe=f"docs.update:{project}:e{n}",
                      payload={"subscription_id": f"sub_{n}", "entity_id": f"crun_{n}"})
    fo.sweep(eng, _settings(), project_id=project)
    with eng.begin() as c:
        cards = _cards(c, project)
        states = c.execute(text("SELECT state FROM failure_owners WHERE project_id = :p "
                                "AND surface = 'job'"), {"p": project}).scalars().all()
    assert states == ["needs_person"] * 3
    assert len(cards) == 1 and len(cards[0]["recommendation"]["items"]) == 3
    assert "Connect this project's repository" in cards[0]["question"]


def test_boss_s_own_background_job_is_requeued_by_boss_under_its_own_identity(eng, project):  # noqa: F811
    with eng.begin() as c:
        jid, dl = _dead_job(c, project, "knowledge.repository.index",
                            "Client error '409 Conflict' for url 'http://api:8000/v1/knowledge/x'",
                            dedupe=f"kg-index:{project}")
    fo.sweep(eng, _settings(), project_id=project)
    with eng.begin() as c:
        row = _owner(c, "job", f"knowledge.repository.index:kg-index:{project}")
        dead = c.execute(text("SELECT status, requeued_job_id FROM deadletters WHERE id = :d"),
                         {"d": dl}).mappings().first()
        new = c.execute(text("SELECT status, on_behalf_of, run_after FROM jobs WHERE id = :j"),
                        {"j": dead["requeued_job_id"]}).mappings().first()
    assert dead["status"] == "requeued" and new["status"] == "queued"
    assert new["on_behalf_of"] == "sys_worker", "the dead job's own principal, never wider"
    assert row["state"] == "fixing" and "attempt 1 of 2" in row["words"]
    assert fo.unowned(eng, project_id=project) == []


def test_a_model_check_dying_on_boss_s_side_is_its_schedule_s_until_it_repeats(eng, project):  # noqa: F811
    err = ("RuntimeError: login rejected before any model call: BOSS could not run codex: "
           "WARNING: failed to clean up stale arg0 temp dirs: Permission denied (os error 13)")
    with eng.begin() as c:
        _dead_job(c, project, "connection.proof", err, dedupe=f"job:connection:{project}:ceo:h0",
                  payload={"role": "primary"})
    fo.sweep(eng, _settings(), project_id=project)
    key = "connection.proof:role:primary"     # each proof has its own dedupe key: one lineage
    with eng.begin() as c:
        row = _owner(c, "job", key)
    assert row["state"] == "fixing" and row["owned_by"] == "proof_schedule"
    with eng.begin() as c:
        for n in range(fo.PROOF_REPEATS):
            _dead_job(c, project, "connection.proof", err,
                      dedupe=f"job:connection:{project}:ceo:h{n + 1}", payload={"role": "primary"})
    fo.sweep(eng, _settings(), project_id=project)
    with eng.begin() as c:
        row = _owner(c, "job", key)
    assert row["state"] == "needs_person" and "BOSS's own side" in row["reason"]


def test_a_recovered_item_resolves_and_its_card_is_withdrawn(eng, project, stubbed):  # noqa: F811
    with eng.begin() as c:
        rid = _build(c, project, fctx={"failure_class": "needs_human",
                                       "reason_code": "repo_access_denied"},
                     error="repository 'x' not found")
    fo.sweep(eng, _settings(), project_id=project)
    with eng.begin() as c:
        card = _owner(c, "code_task", rid)["decision_id"]
        # The person fixed it and retried from the build's own page: a newer attempt finished.
        _build(c, project, status="completed", fctx={"root_run_id": rid, "parent_run_id": rid},
               at=_ago(minutes=1))
    fo.sweep(eng, _settings(), project_id=project)
    with eng.begin() as c:
        row = _owner(c, "code_task", rid)
        status = c.execute(text("SELECT status FROM decisions WHERE id = :d"),
                           {"d": card}).scalar()
    assert row["state"] == "resolved" and row["words"].startswith("It works now")
    assert status == "withdrawn"


def test_done_retry_runs_it_again_as_the_person_and_leave_it_ends_ownership(eng, project, monkeypatch):  # noqa: F811
    ran: list[str] = []
    monkeypatch.setattr(fo.SURFACES["code_task"], "person_retry",
                        lambda engine, settings, jobs, ref, actor: ran.append(ref["id"]) or "x")
    with eng.begin() as c:
        rid = _build(c, project, fctx={"failure_class": "needs_human",
                                       "reason_code": "model_auth_rejected"}, error="HTTP 401")
    fo.sweep(eng, _settings(), project_id=project)
    with eng.begin() as c:
        card = c.execute(text("SELECT * FROM decisions WHERE id = :d"),
                         {"d": _owner(c, "code_task", rid)["decision_id"]}).mappings().first()
        fo.on_answered(c, _settings(), None, dict(card), verdict="approve", actor="user:ceo")
        row = _owner(c, "code_task", rid)
    assert ran == [rid]
    assert row["state"] == "fixing" and row["owned_by"] == "person"
    # Within the grace a person's retry gets, the next pass does not ask again.
    fo.sweep(eng, _settings(), project_id=project)
    with eng.begin() as c:
        assert _owner(c, "code_task", rid)["state"] == "fixing"
        fo.on_answered(c, _settings(), None, dict(card), verdict="reject", actor="user:ceo")
        row = _owner(c, "code_task", rid)
    assert row["state"] == "resolved" and "chose not to retry" in row["words"]


def test_the_project_s_failure_says_boss_is_fixing_it_or_needs_you(eng, project, stubbed):  # noqa: F811
    with eng.begin() as c:
        _build(c, project, fctx={"failure_class": "infra", "reason_code": "sandbox_error"},
               error="[Errno 13] Permission denied")
    fo.sweep(eng, _settings(), project_id=project)
    with eng.begin() as c:
        failed = build_failure.latest_failure(c, project)
    assert failed["word"] == "BOSS is fixing it" and failed["tone"] == "info"
    assert failed["doing"].startswith("BOSS is fixing this: attempt 1 of 2")
    card = build_failure.needs_card(failed)
    assert "failed" not in card["label"].lower().split(":")[0]


def test_a_preview_that_timed_out_on_boss_s_check_is_started_again(eng, project, stubbed):  # noqa: F811
    with eng.begin() as c:
        rid = _build(c, project, status="completed")
        pv = f"prev_{uuid4().hex[:20]}"
        c.execute(text("INSERT INTO app_previews (id, project_id, run_id, status, failure_kind, "
                       "error, created_at) VALUES (:i, :p, :r, 'failed', 'port_timeout', :e, :at)"),
                  {"i": pv, "p": project, "r": rid, "at": _ago(minutes=3),
                   "e": "the app IS listening on port 19519 and GET / returned 200, but the "
                        "health check rejected the page it served."})
    fo.sweep(eng, _settings(), project_id=project)
    with eng.begin() as c:
        row = _owner(c, "preview", f"preview:{rid}")
    assert ("preview", pv, "platform") in stubbed and row["state"] == "fixing"


def test_after_a_sweep_nothing_failed_is_left_bare(eng, project, stubbed):  # noqa: F811
    """THE INVARIANT over one of every shape at once."""
    with eng.begin() as c:
        _build(c, project, fctx={"failure_class": "infra"}, error="lease_lost")
        _build(c, project, fctx={"failure_class": "needs_human", "reason_code": "no_test_files"},
               error="Test author produced no test files", title="Another")
        _build(c, project, error="tests failed — see output", title="Third", at=_ago(days=4))
        _workflow(c, project, step_error="KeyError: 'monitoring_pack'")
        _dead_job(c, project, "meeting.step", "Server error '500 Internal Server Error'",
                  payload={"run_id": "mrun_x"})
        _dead_job(c, project, "github.pull_request.merge",
                  "Merge failed: PR #5 conflicts with its base branch", payload={
                      "pull_request_url": "https://github.com/o/r/pull/5"})
        c.execute(text("INSERT INTO meeting_runs (id, project_id, status, directive, error) "
                       "VALUES (:i, :p, 'failed', 'Design X', 'phase stalled (900s > 600s)')"),
                  {"i": f"mrun_{uuid4().hex[:12]}", "p": project})
    report = fo.sweep(eng, _settings(), project_id=project, max_acts=1)
    assert report["errors"] == 0, report
    assert fo.unowned(eng, project_id=project) == []
    with eng.begin() as c:
        rows = c.execute(text("SELECT state, words, reason, decision_id FROM failure_owners "
                              "WHERE project_id = :p"), {"p": project}).mappings().all()
    assert rows and all(r["words"] for r in rows)
    assert all(r["reason"] and r["decision_id"] for r in rows if r["state"] == "needs_person")


if os.environ.get("BOSS_FAILURE_OWNER_LIVE_CHECK"):          # pragma: no cover — on demand
    def test_the_install_has_no_bare_failure():
        from boss_engine.db import get_engine

        assert fo.unowned(get_engine()) == []


def test_owner_words_never_lose_the_attempt_count():
    assert fo.fixing_words(1, "running it again") == \
        "BOSS is fixing this: attempt 1 of 2 — running it again."


# ───────────────────────────── review #232 ─────────────────────────────


def test_a_person_s_done_on_a_host_deploy_plans_it_and_needs_their_deploy_permission(
        eng, project, monkeypatch):  # noqa: F811
    from boss_engine.services import delivery_pipeline, members

    got: list[dict] = []
    monkeypatch.setattr(delivery_pipeline, "redeploy",
                        lambda engine, settings, rid, **kw: got.append({"id": rid, **kw}) or
                        {"id": "dep_new"})
    ref = {"id": "dep_old", "key": "dep_old", "project_id": project, "hints": {"kind": "host"}}
    with pytest.raises(fo.NeedsPerson):          # not a member who may deploy here
        fo.SURFACES["deployment"].person_retry(eng, _settings(), None, ref, "user:stranger")
    assert got == []
    monkeypatch.setattr(members.MemberDirectoryService, "require_member",
                        lambda self, c, pid, actor, permission=None: got.append(
                            {"perm": permission}))
    fo.SURFACES["deployment"].person_retry(eng, _settings(), None, ref, "user:ceo")
    assert got[0] == {"perm": "providerops_manage"}
    assert got[1]["id"] == "dep_old" and got[1]["execute"] is False


def test_only_boss_s_own_dead_job_is_rerun_by_boss_a_person_s_is_their_item(eng, project):  # noqa: F811
    from boss_engine.services.deadletters import (DeadletterRemediationBlocked,
                                                  DeadletterRemediationService)

    with eng.begin() as c:
        _, dl = _dead_job(c, project, "knowledge.repository.index", "HTTP 500 from the indexer",
                          dedupe=f"kg-index:{project}:p", principal="usr_someone")
    with eng.begin() as c, pytest.raises(DeadletterRemediationBlocked):
        DeadletterRemediationService(_settings()).requeue_job_deadletter(
            c, deadletter_id=dl, actor=fo.ACTOR, as_boss=True)
    fo.sweep(eng, _settings(), project_id=project)
    with eng.begin() as c:
        status = c.execute(text("SELECT status FROM deadletters WHERE id = :d"),
                           {"d": dl}).scalar()
        row = c.execute(text("SELECT state, reason FROM failure_owners WHERE project_id = :p "
                             "AND surface = 'job'"), {"p": project}).mappings().first()
    assert status == "open"
    assert row["state"] == "needs_person" and "ran for a person" in row["reason"]


@pytest.mark.parametrize("state, want", [("fixing", "fixing"), ("stuck", "needs_person")])
def test_only_a_deploy_repair_that_is_really_under_way_counts_as_fixing(
        eng, project, monkeypatch, state, want):  # noqa: F811
    from boss_engine.services import deploy_repair

    with eng.begin() as c:
        tgt, dep = f"deptgt_{uuid4().hex[:12]}", f"sdep_{uuid4().hex[:12]}"
        c.execute(text("INSERT INTO deployment_targets (id, project_id, app_name, repo, "
                       "environment, target_kind, target_ref, created_by) VALUES (:t, :p, "
                       "'recipe-box', 'o/r', 'staging', 'stack', 'h', 'test')"),
                  {"t": tgt, "p": project})
        c.execute(text("INSERT INTO stack_deployments (id, project_id, target_id, repository, "
                       "ref, commit_sha, created_by, status, failure_reason, workflow_run_id) "
                       "VALUES (:d, :p, :t, 'o/r', 'main', 'abc', 'test', 'failed', "
                       "'Cannot find module /app/server.js', 'wfr_gone')"),
                  {"d": dep, "p": project, "t": tgt})
    monkeypatch.setattr(deploy_repair, "underway", lambda c, rows: {
        dep: {"state": state, "word": "x", "reason": f"repair is {state}"}})
    fo.sweep(eng, _settings(), project_id=project)
    with eng.begin() as c:
        row = _owner(c, "deployment", dep)
    assert row["state"] == want
    if want == "fixing":
        assert row["owned_by"] == "deploy_repair"


def test_an_old_build_is_never_rebuilt_whatever_was_tried_before(eng, project, stubbed):  # noqa: F811
    with eng.begin() as c:
        root = _build(c, project, fctx={"failure_class": "infra"}, error="lease_lost",
                      at=_ago(days=5, minutes=5))
        _build(c, project, error="lease_lost", at=_ago(days=5),
               fctx={"failure_class": "infra", "root_run_id": root, "parent_run_id": root,
                     "auto_infra_retry": 1})
    fo.sweep(eng, _settings(), project_id=project)
    with eng.begin() as c:
        row = _owner(c, "code_task", root)
    assert stubbed == []
    assert row["state"] == "needs_person" and "within 72 hours" in row["reason"]


def test_a_workflow_whose_authority_ended_is_the_person_s_and_one_from_before_a_restart_is_not_rerun(
        eng, project, monkeypatch):  # noqa: F811
    from boss_engine.services import code_task_service

    with eng.begin() as c:
        ended = _workflow(c, project, step_error="this run's authority has ended "
                          "(grant_does_not_name_agent:agt_x); renew it to continue.")
        expired = _workflow(c, project, step_error="the approval expired before anybody answered")
        before = _workflow(c, project, step_error="KeyError: 'monitoring_pack'",
                           at=_ago(hours=2))
    monkeypatch.setattr(code_task_service, "PROCESS_STARTED_AT", _ago(hours=1))
    fo.sweep(eng, _settings(), project_id=project)
    with eng.begin() as c:
        rows = {k: _owner(c, "workflow_run", k) for k in (ended, expired, before)}
    assert rows[ended]["state"] == "needs_person" and rows[ended]["cause_code"] == "grant"
    assert rows[expired]["state"] == "needs_person" and \
        rows[expired]["cause_code"] == "approval_expired"
    assert rows[before]["state"] == "needs_person" and "before BOSS last started" in \
        rows[before]["reason"]
