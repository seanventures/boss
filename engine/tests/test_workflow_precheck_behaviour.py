"""The pre-check's behaviour families (services.workflow_precheck_behaviour): history, logs, data;
the code-aware tree checks; the auto-fix catalogue; --explain; and the doc line every check id
carries.

No database: every read goes through `behaviour._rows`, which these tests answer from synthetic
rows (`Store`), routed by the table and the phrase each query is known by."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from boss_engine.services import workflow_precheck as wp
from boss_engine.services import workflow_precheck_behaviour as bh
from boss_engine.tools import workflow_precheck as cli

NOW = datetime(2026, 10, 13, 23, 59, tzinfo=timezone.utc)


def ago(minutes: float) -> datetime:
    return NOW - timedelta(minutes=minutes)


class _Settings:
    plan_budget_usd_micros = 1_500_000
    code_task_budget_usd_micros = 2_000_000
    code_agent_max_concurrency = 1
    code_agent_review_timeout_seconds = 420


class Store:
    """Synthetic rows, answered by table and phrase."""

    def __init__(self) -> None:
        self.runs: list[dict] = []
        self.started: list[str] = []
        self.builds: list[dict] = []
        self.gates: list[dict] = []
        self.step_failures: list[dict] = []
        self.events: list[dict] = []
        self.llm: list[dict] = []
        self.denied: list[dict] = []
        self.retry_denied: list[dict] = []
        self.starts: list[dict] = []
        self.drains: list[dict] = []
        self.app_logs: list[dict] = []
        self.jobs: list[dict] = []
        self.ended: list[dict] = []
        self.bindings: list[dict] = []
        self.applications: list[dict] = []
        self.targets: list[dict] = []
        self.leases: dict[str, list[dict]] = {"budget": [], "gateway": [], "branch": []}
        self.decisions: list[dict] = []

    def ev(self, kind: str, entity: str, at: datetime, data: dict | None = None) -> None:
        self.events.append({"event_type": kind, "entity_type": "code_task_run",
                            "entity_id": entity, "created_at": at, "data": data or {}})

    def rows(self, sql: str, params: dict) -> list[dict]:  # noqa: C901 — a router, flat on purpose
        ids = params.get("ids")
        if "FROM workflow_runs r JOIN workflow_definitions" in sql:
            return self.runs
        if "output->>'code_task_run_id' AS c" in sql:
            return [{"c": c} for c in self.started]
        if "FROM code_task_runs WHERE project_id = :p AND created_at" in sql:
            return self.builds
        if "FROM quality_gates" in sql:
            rows = [g for g in self.gates if g["run"] in (ids or [])]
            if "status <> 'pass'" in sql:
                rows = [g for g in rows if g.get("status", "fail") != "pass"]
            return [{"run": g["run"], "created_at": g.get("created_at", NOW),
                     "gates_failed": g["gates_failed"]} for g in rows]
        if "FROM workflow_step_runs WHERE workflow_run_id = ANY(:ids) AND status = 'failed'" in sql:
            return self.step_failures
        if sql.startswith("SELECT event_type, entity_type, entity_id"):
            out = [e for e in self.events if e["event_type"] in params["k"]]
            if "e" in params:
                out = [e for e in out if e["entity_id"] in params["e"]]
            return sorted(out, key=lambda e: e["created_at"])
        if "FROM events" in sql and "GROUP BY 1\"" not in sql and "shepherd.retry" in sql:
            n = {}
            for e in self.events:
                if e["event_type"] == "shepherd.retry":
                    k = (e["entity_id"], e["data"].get("reason"))
                    n[k] = n.get(k, 0) + 1
            return [{"entity_id": k[0], "reason": k[1], "n": v} for k, v in n.items() if v > 3]
        if "FROM events" in sql and "count(DISTINCT entity_id)" in sql:
            out: dict[str, dict] = {}
            for e in self.events:
                if e["event_type"] in params["k"]:
                    out.setdefault(e["event_type"], {"event_type": e["event_type"], "n": 0,
                                                     "ents": 0})["n"] += 1
            return list(out.values())
        if "captain.coordinated" in sql:
            return [{"n": sum(1 for e in self.events if e["event_type"] == "captain.coordinated"
                              and not e["data"].get("assignments"))}]
        if "FROM llm_calls" in sql:
            return [r for r in self.llm if r["latency_ms"] >= params["near"]]
        if "details->>'reason' = 'step_up_required'" in sql:
            return self.denied
        if "permission = 'code_task:retry'" in sql:
            return self.retry_denied
        if "No one''s authority" in sql and "count(*)" in sql:
            n = [b for b in self.builds if str(b.get("error") or "").startswith("No one's")]
            return [{"n": len(n), "last": NOW}]
        if "logger = 'boss_engine.services.log_capture'" in sql:
            return self.starts
        if "logger = 'boss.drain'" in sql:
            return self.drains
        if "FROM app_logs WHERE level IN" in sql:
            return self.app_logs
        if "FROM jobs WHERE grant_id" in sql:
            return [j for j in self.jobs if j["grant_id"] in params["g"]]
        if "FROM jobs" in sql:
            return []
        if "event_type IN ('delegation.expired'" in sql:
            return self.ended
        if "SELECT DISTINCT app_key FROM app_secret_bindings" in sql:
            keys = {b["app_key"] for b in self.bindings} | {a["app_key"] for a in
                                                            self.applications}
            return [{"app_key": k} for k in keys]
        if "FROM app_template_applications WHERE" in sql:
            return [a for a in self.applications if a["app_key"] in params["k"]]
        if "FROM app_secret_bindings WHERE project_id = :p AND app_key = ANY(:k)" in sql:
            return [b for b in self.bindings if b["app_key"] in params["k"]]
        if "split_part(title" in sql:
            return []
        if "FROM deployment_targets t" in sql:
            return self.targets
        if "FROM budget_reservations" in sql:
            return self.leases["budget"]
        if "FROM model_gateway_leases" in sql:
            return self.leases["gateway"]
        if "FROM github_branch_leases" in sql:
            return self.leases["branch"]
        if "FROM decisions d" in sql:
            return self.decisions
        return []


@pytest.fixture
def store(monkeypatch):
    s = Store()

    def fake_rows(ctx, sql, params=None, default=None):
        return s.rows(sql, {"p": ctx.project_id, "d": 7, **(params or {})})
    monkeypatch.setattr(bh, "_rows", fake_rows)
    monkeypatch.setattr(wp, "_app_name", lambda ctx: "HelpDesk AI")
    return s


def _ctx(*, app_dir: Path | None = None, run_input: dict | None = None,
         spec: dict | None = None, memo: dict | None = None) -> wp.Context:
    ctx = wp.Context(connection=None, settings=_Settings(), project_id="p1", actor="ceo",
                     spec=spec or {"steps": []}, run_input=run_input or {},
                     checkpoint=wp.PRE_DEPLOY, workflow_key="helpdesk_delivery_d1",
                     app_dir=app_dir, offline=True, families=bh.FAMILIES)
    ctx.memo.update({"template_declared": {}, "binding": None, "identity_applies": True,
                     "_target_results": [], "declared_needs": {}})
    ctx.memo.update(memo or {})
    return ctx


def _build(rid: str, *, status: str = "blocked", error: str = "", start: float = 60,
           end: float = 30, title: str = "HelpDesk AI: add login", **extra: Any) -> dict:
    return {"id": rid, "title": title, "status": status, "phase": "done", "error": error,
            "created_at": ago(start), "updated_at": ago(end), "finalize_ctx": {},
            "app_serves": extra.pop("app_serves", None), "panel_parallel": extra.pop(
                "panel_parallel", None), "panel_size": extra.pop("panel_size", None), **extra}


def _write(root: Path, files: dict[str, str]) -> Path:
    for rel, body in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    return root


# ── the registry ──────────────────────────────────────────────────────────────────────────────

def _emitted_ids() -> set[str]:
    """Every check id the two modules can put in a report."""
    import inspect

    ids = set(wp.CHECKS) | set(bh.RUN_CHECKS)
    for module in (wp, bh):
        src = inspect.getsource(module)
        ids |= set(re.findall(r"result\(\s*\"([a-z_]+\.[a-z_]+)\"", src))
    ids |= {f"app_env.{env}" for env in ("build", "preview", "deploy")}
    ids.discard("app_env")      # the registered id; its results are per environment
    return ids


def test_every_check_id_has_a_doc_line():
    missing = sorted(i for i in _emitted_ids() if not wp.CHECK_DOCS.get(i, "").strip())
    assert not missing, f"check ids with no doc line in CHECK_DOCS: {missing}"
    stale = sorted(set(wp.CHECK_DOCS) - _emitted_ids())
    assert not stale, f"CHECK_DOCS names checks that no longer exist: {stale}"


def test_every_family_check_is_registered_with_a_label_and_family():
    assert {c.family for c in bh.RUN_CHECKS.values()} == set(bh.FAMILIES)
    for check in bh.RUN_CHECKS.values():
        assert check.label and check.id.startswith(check.family + ".")


def test_every_autofix_a_check_names_is_in_the_catalogue():
    import inspect

    named = set()
    for module in (wp, bh):
        named |= set(re.findall(r"\"fix\": \"([a-z_.]+)\"", inspect.getsource(module)))
    assert named and named <= set(bh.FIXES), named - set(bh.FIXES)


def test_the_families_never_run_at_the_start_door(tmp_path, store):
    ctx = _ctx()
    report = wp.run_checks(ctx, only_fast=True)
    assert not [s for s in report["steps"] if s["step_id"] in bh.FAMILY_STEP.values()]
    report = wp.run_checks(ctx)
    assert {s["step_id"] for s in report["steps"]} >= {"(history)", "(logs)", "(data)"}


# ── history.* ─────────────────────────────────────────────────────────────────────────────────

def test_fingerprint_folds_paths_ids_and_numbers():
    a = bh.fingerprint("[Errno 13] Permission denied: '/var/lib/boss/sandboxes/89f7/crun_8be4/x.mjs'")
    b = bh.fingerprint("[Errno 13] Permission denied: '/var/lib/boss/sandboxes/0e4a/crun_5344/y.mjs'")
    assert a == b
    assert bh.fingerprint("tsx: Permission denied")[0] != a[0]


def _tsx_failures(store: Store, *, api_start_minutes_ago: float) -> None:
    err = ("the app exited (code 127) before serving HTTP: error: sh: 1: tsx: Permission denied "
           "log tail: > tsx scripts/migrate.ts")
    store.builds = [_build("crun_a", start=300, end=280, app_serves={"ok": False, "error": err}),
                    _build("crun_b", start=200, end=180, app_serves={"ok": False, "error": err}),
                    _build("crun_c", start=40, end=20, app_serves={"ok": False, "error": err})]
    store.gates = [{"run": b["id"], "gates_failed": ["app_serves"],
                    "created_at": b["updated_at"]} for b in store.builds]
    store.starts = [{"service": "api", "created_at": ago(api_start_minutes_ago)}]


def test_a_failure_that_recurs_on_the_running_boss_will_fail(store):
    _tsx_failures(store, api_start_minutes_ago=100)
    found = bh.check_recurring(_ctx())
    assert found["verdict"] == "will_fail"
    assert "failed 3× at gate app_serves" in found["evidence"]
    assert "tsx: Permission denied" in found["evidence"]
    assert "still occurs on" in found["evidence"]
    assert found["details"]["recurring"][0]["count"] == 3


def test_a_failure_not_seen_since_the_restart_is_a_question_not_a_verdict(store):
    _tsx_failures(store, api_start_minutes_ago=10)
    store.builds.append(_build("crun_d", status="completed", start=9, end=5))
    found = bh.check_recurring(_ctx())
    assert found["verdict"] == "unknown"
    assert "fixed since? unproven" in found["evidence"]


def test_no_failures_is_ready(store):
    store.builds = [_build("crun_ok", status="completed")]
    assert bh.check_recurring(_ctx())["verdict"] == "ready"


def test_review_panel_degradation_is_reported_with_its_evidence(store):
    store.builds = [_build("crun_r", panel_parallel=False, panel_size=3)]
    store.ev("code_task.review_degraded", "crun_r", ago(30),
             {"errors": ["1 review check(s) exceeded 420s — degraded"], "deadline_s": 420})
    store.llm = [{"run": "crun_r", "stage": "review_code", "latency_ms": 420_000, "ok": False,
                  "created_at": ago(31)}]
    store.ev("code_task.pr_review_again_requested", "crun_r", ago(20))
    store.ev("code_task.pr_review_again_done", "crun_r", ago(10))
    found = bh.check_review_panel(_ctx())
    assert found["verdict"] == "unknown"
    ev = found["evidence"]
    assert "review_degraded 1×" in ev and "max 420s" in ev and "serially" in ev
    assert "ran past the panel deadline" in ev            # 600 s re-review > 420 s


def test_a_quiet_panel_is_ready(store):
    store.builds = [_build("crun_r")]
    assert bh.check_review_panel(_ctx())["verdict"] == "ready"


def test_warm_rounds_that_change_nothing_and_template_builds_with_no_round(store):
    store.builds = [_build("crun_w"), _build("crun_t")]
    for n in (1, 2):
        store.ev("code_task.inrun_convergence_round", "crun_w", ago(50 - n),
                 {"round": n, "of": 2, "failing_gates": ["app_serves"]})
    store.gates = [{"run": "crun_w", "gates_failed": ["app_serves"]},
                   {"run": "crun_t", "gates_failed": ["template_tests", "code_review_passed"]}]
    found = bh.check_warm_rounds(_ctx())
    assert found["verdict"] == "unknown"
    assert "crun_w (app_serves)" in found["evidence"]
    assert "NO warm round" in found["evidence"] and "crun_t (blocked by template_tests)" in \
        found["evidence"]
    assert found["details"]["no_warm_round"] == ["crun_t"]


def test_approvals_held_by_step_up_are_counted_with_the_wait(store):
    store.denied = [{"ts": ago(30), "permission": "decision:approve", "who": "u1"},
                    {"ts": ago(29), "permission": "connection:use_own", "who": "u1"}]
    store.ev("decision.approved", "dec_1", ago(27))
    found = bh.check_step_up(_ctx())
    assert found["verdict"] == "unknown"
    assert "1 of them approvals" in found["evidence"] and "180–180s later" in found["evidence"]
    assert "does not replay" in found["evidence"]


def test_step_up_on_other_calls_only_is_not_a_run_risk(store):
    store.denied = [{"ts": ago(30), "permission": "connection:use_own", "who": "u1"}]
    assert bh.check_step_up(_ctx())["verdict"] == "ready"


def test_refused_automatic_retries_are_named(store):
    store.builds = [_build("crun_r1", error="No one's authority covers this build: it was "
                                            "started by BOSS itself")]
    store.retry_denied = [{"ts": ago(5)}]
    found = bh.check_retry_refused(_ctx())
    assert found["verdict"] == "unknown"
    assert "crun_r1" in found["evidence"] and "1 code_task:retry call(s) denied" in found["evidence"]


def test_event_noise_over_budget_and_duplicate_retries(store):
    for i in range(600):
        store.ev("shepherd.proposed", f"crun_{i % 7}", ago(i % 600))
    for i in range(5):
        store.ev("shepherd.retry", "crun_spam", ago(i), {"reason": "same"})
    found = bh.check_noise(_ctx())
    assert found["verdict"] == "unknown"
    assert "shepherd.proposed 600 (budget 500)" in found["evidence"]
    assert "crun_spam ×5" in found["evidence"]


def test_a_restart_inside_a_failed_build_is_named(store):
    store.builds = [_build("crun_x", status="failed", start=60, end=10,
                           error="claude code agent exited 143")]
    store.starts = [{"service": "api", "created_at": ago(30)},
                    {"service": "worker", "created_at": ago(29)}]   # one restart, two lines
    store.drains = [{"created_at": ago(31), "message": "drained for shutdown: 0 build(s) "
                                                       "interrupted, 1 process tree(s) ended"}]
    found = bh.check_restarts(_ctx())
    assert found["verdict"] == "unknown"
    ev = found["evidence"]
    assert ev.startswith("1 restart(s)")
    assert "crun_x" in ev and "exit 143" in ev and "1 process tree(s) ended" in ev


def test_a_grant_ended_under_a_live_job_is_named(store):
    store.ended = [{"ts": ago(30), "grant_id": "dlg_1", "details": {"runs_halted": 0}}]
    store.jobs = [{"id": "job_1", "kind": "code.task", "status": "deadlettered",
                   "grant_id": "dlg_1", "created_at": ago(60), "updated_at": ago(10),
                   "last_error": {}}]
    found = bh.check_grant_ended(_ctx())
    assert found["verdict"] == "unknown" and "dlg_1" in found["evidence"] and "job_1" in \
        found["evidence"]


class _PgStat:
    """A connection whose pg_stat_activity shows one long idle transaction."""

    def begin_nested(self):
        class _P:
            def rollback(self):
                pass
        return _P()

    def execute(self, statement, params=None):
        sql = str(statement)

        class _R:
            def scalar(self):
                return "0"

            def mappings(self):
                return self

            def all(self):
                if "pg_stat_activity" in sql:
                    return [{"pid": 42, "application_name": "boss", "age": 37.0,
                             "query": "UPDATE workflow_step_runs SET ..."}]
                return []
        return _R()


def test_a_transaction_left_open_is_caught_in_the_samples(monkeypatch):
    monkeypatch.setattr(bh.time, "sleep", lambda s: None)
    ctx = _ctx()
    ctx.connection = _PgStat()
    found = bh.check_idle_tx(ctx)
    assert found["verdict"] == "unknown"
    assert "pid 42 37s" in found["evidence"] and "(unbounded)" in found["evidence"]


# ── logs.* ────────────────────────────────────────────────────────────────────────────────────

def _start_failure(store: Store, rid: str, log: str, environment: dict | None) -> None:
    store.builds.append(_build(rid))
    store.ev("code_task.app_serve_checked", rid, ago(5),
             {"ok": False, "error": f"the app exited (code 1) before serving HTTP: {log}",
              "environment": environment})


_CONFIG_THEN_DIE = ("config: APP_BASE_URL=(unset) PORT=3000\n"
                    "Error: DEMO_PASSWORD must be set (the seed refuses to run without it)")


def test_the_matcher_on_a_real_log_blames_the_wrong_variable(store, monkeypatch):
    # The matcher the build used to run (run_env.unset_named_in before 9d11267f) took the FIRST
    # unset name the log mentions; this app printed its config before dying on the variable it
    # named. The pre-check must still catch that class if the matcher ever regresses to it.
    def first_unset_mentioned(log, environment):
        return next((n for n in environment.get("unset") or [] if n in log), None)

    monkeypatch.setattr(bh, "blame", first_unset_mentioned)
    _start_failure(store, "crun_l", _CONFIG_THEN_DIE,
                   {"unset": ["APP_BASE_URL", "DEMO_PASSWORD"], "generated": ["SESSION_SECRET"]})
    found = bh.check_crash_blame(_ctx())
    wrong = [f for f in found if f["verdict"] == "will_fail"]
    assert wrong, found
    assert wrong[0]["details"] == {"run": "crun_l", "named": "DEMO_PASSWORD",
                                   "blamed": "APP_BASE_URL"}
    assert "BOSS bug" in wrong[0]["fix"]["action"]


def test_the_build_matcher_now_blames_the_line_the_app_died_on(store):
    # The real matcher (fixed in 9d11267f: the LAST error line wins) reads the same log right,
    # so the pre-check has nothing to flag.
    _start_failure(store, "crun_l", _CONFIG_THEN_DIE,
                   {"unset": ["APP_BASE_URL", "DEMO_PASSWORD"], "generated": ["SESSION_SECRET"]})
    found = bh.check_crash_blame(_ctx())
    assert [f["verdict"] for f in found] == ["ready"], found
    assert "matcher blames DEMO_PASSWORD" in found[0]["evidence"]


def test_a_log_the_matcher_reads_right_is_ready(store):
    _start_failure(store, "crun_ok", "Error: DEMO_PASSWORD must be set",
                   {"unset": ["DEMO_PASSWORD"], "generated": []})
    found = bh.check_crash_blame(_ctx())
    assert [f["verdict"] for f in found] == ["ready"]


def test_an_app_refusing_a_value_boss_generates_is_its_own_class(store):
    _start_failure(store, "crun_g", "Error: SESSION_SECRET must be set to at least 32 "
                                    "characters", {"unset": [], "generated": ["SESSION_SECRET"]})
    found = bh.check_crash_blame(_ctx())
    assert found[0]["verdict"] == "unknown" and "BOSS GENERATES" in found[0]["evidence"]


def test_the_throw_site_in_the_tree_says_which_variable_the_code_reads(store, tmp_path):
    app = _write(tmp_path / "app", {"src/server/auth/router.ts": (
        "export function createAuth() {\n"
        "  const s = process.env.AUTH_SESSION_KEY;\n"
        "  if (!s || s.length < 32) {\n"
        "    throw new Error('SESSION_SECRET must be set to at least 32 characters');\n"
        "  }\n}\n")})
    _start_failure(store, "crun_s", "Error: SESSION_SECRET must be set to at least 32 characters"
                   "\n    at createAuth (/var/lib/boss/x/app/src/server/auth/router.ts:4:11)",
                   {"unset": [], "generated": ["SESSION_SECRET"]})
    found = bh.check_crash_blame(_ctx(app_dir=app))
    assert found[0]["verdict"] == "will_fail"
    assert found[0]["details"]["blamed"] == "AUTH_SESSION_KEY"


def test_start_logs_are_redacted():
    assert "sk-live" not in bh._clip("OPENAI_API_KEY=sk-liveabcdefghijklmnopqrstuvwxyz0123456789")


def test_the_last_start_failure_repeats_until_its_variable_is_bound(store, monkeypatch):
    _start_failure(store, "crun_p", "Error: DEMO_PASSWORD must be set", None)
    monkeypatch.setattr(wp, "_has_binding", lambda ctx, n, env: False)
    ctx = _ctx(run_input={"app_key": "helpdesk ai"}, memo={"current_env": {"unset": [
        "DEMO_PASSWORD"], "generated": []}})
    found = bh.check_app_start(ctx)
    assert found["verdict"] == "will_fail" and found["details"]["named"] == "DEMO_PASSWORD"
    monkeypatch.setattr(wp, "_has_binding", lambda ctx, n, env: True)
    assert bh.check_app_start(_ctx(run_input={"app_key": "helpdesk ai"}, memo={
        "current_env": {"unset": [], "generated": []}}))["verdict"] == "unknown"


def _unbound_start(store, monkeypatch) -> wp.Context:
    _start_failure(store, "crun_p", "Error: DEMO_PASSWORD must be set", None)   # at ago(5)
    monkeypatch.setattr(wp, "_has_binding", lambda ctx, n, env: False)
    monkeypatch.delenv("BOSS_BUILT_AT", raising=False)
    return _ctx(run_input={"app_key": "helpdesk ai"}, memo={"current_env": {"unset": [
        "DEMO_PASSWORD"], "generated": []}})


def test_a_start_failure_from_before_this_version_is_a_question_not_a_verdict(store, monkeypatch):
    # D1: a 10-13 23:51 failure, fixed by the upgrade after it, was called will_fail.
    ctx = _unbound_start(store, monkeypatch)
    store.starts = [{"service": "api", "created_at": ago(2)}]
    found = bh.check_app_start(ctx)
    assert found["verdict"] == "unknown"
    assert "seen before this version; may be fixed" in found["evidence"]
    assert found["details"]["before_version"] is True


def test_the_same_failure_after_this_version_started_still_will_fail(store, monkeypatch):
    ctx = _unbound_start(store, monkeypatch)
    store.starts = [{"service": "api", "created_at": ago(10)}]
    assert bh.check_app_start(ctx)["verdict"] == "will_fail"


def test_a_restart_of_the_same_image_does_not_hide_a_failure(store, monkeypatch):
    """The version's start is its image's FIRST start (at/after BOSS_BUILT_AT), not the last
    restart: a restart after the failure fixes nothing."""
    ctx = _unbound_start(store, monkeypatch)
    monkeypatch.setenv("BOSS_BUILT_AT", ago(30).isoformat().replace("+00:00", "Z"))
    store.starts = [{"service": "api", "created_at": ago(60)},     # the previous image
                    {"service": "api", "created_at": ago(20)},     # this image, first start
                    {"service": "api", "created_at": ago(1)}]      # a restart
    assert bh.version_started(ctx) == ago(20)
    assert bh.check_app_start(ctx)["verdict"] == "will_fail"


def test_without_a_start_record_the_verdict_stands(store, monkeypatch):
    ctx = _unbound_start(store, monkeypatch)
    assert bh.version_started(ctx) is None
    assert bh.check_app_start(ctx)["verdict"] == "will_fail"


def test_what_boss_said_before_this_version_is_history_not_a_verdict(store, monkeypatch):
    log = "Error: DEMO_PASSWORD must be set\nthe app needs a secret: APP_BASE_URL"
    _start_failure(store, "crun_s", log, {"unset": ["APP_BASE_URL", "DEMO_PASSWORD"],
                                          "generated": []})
    monkeypatch.delenv("BOSS_BUILT_AT", raising=False)
    store.starts = [{"service": "api", "created_at": ago(10)}]
    assert [f["verdict"] for f in bh.check_crash_blame(_ctx())] == ["will_fail"]
    store.starts = [{"service": "api", "created_at": ago(2)}]
    found = bh.check_crash_blame(_ctx())
    assert found[0]["verdict"] == "unknown"
    assert "seen before this version; may be fixed" in found[0]["evidence"]


def test_swallowed_programming_errors_group_across_line_numbers(store):
    tb = ('caught AttributeError:\nTraceback (most recent call last):\n  File '
          '"/app/engine/boss_engine/services/governed_code_task_service.py", line {n}, in '
          '_run_governed_code_task\n    x = budget.status_for_project(c)\n'
          "AttributeError: 'NoopBudgetPort' object has no attribute 'status_for_project'")
    store.app_logs = [{"service": "api", "logger": "x", "message": tb.format(n=n),
                       "created_at": ago(n)} for n in (3183, 3263, 3443)]
    store.app_logs.append({"service": "worker", "logger": "boss.monitor", "created_at": ago(1),
                           "message": "could not check dashboards: KeyError: 'app'"})
    found = bh.check_swallowed(_ctx())
    assert found["verdict"] == "unknown"
    groups = found["details"]["groups"]
    assert groups[0]["count"] == 3 and groups[0]["exception"] == "AttributeError"
    assert any(g["exception"] == "KeyError" for g in groups)


# ── data.* ────────────────────────────────────────────────────────────────────────────────────

def test_related_keys_are_the_same_app_under_another_name():
    assert bh.related_keys("helpdesk ai", ["helpdesk", "helpdesk ai", "notes", "help"]) == \
        ["helpdesk"]


def test_secrets_under_the_old_key_are_fixable_by_copy(store):
    store.applications = [{"app_key": "helpdesk", "template_id": "tpl_1"},
                          {"app_key": "helpdesk ai", "template_id": "tpl_1"}]
    store.bindings = [{"app_key": "helpdesk", "env": "deploy", "name": "DEMO_PASSWORD",
                       "mode": "test"},
                      {"app_key": "helpdesk", "env": "deploy", "name": "STRIPE_KEY",
                       "mode": "live"},
                      {"app_key": "helpdesk", "env": "build", "name": "GOOGLE_CLIENT_ID",
                       "mode": "stub"},
                      {"app_key": "helpdesk ai", "env": "build", "name": "GOOGLE_CLIENT_ID",
                       "mode": "stub"}]
    found = bh.check_split_keys(_ctx(run_input={"app_key": "helpdesk ai"}))
    assert found["verdict"] == "fixable" and found["fix"]["auto"]
    items = found["details"]["autofix"]["args"]["items"]
    assert items == [{"from": "helpdesk", "env": "deploy", "name": "DEMO_PASSWORD"}]
    assert "STRIPE_KEY is live: a person sets it" in found["fix"]["action"]


def test_one_key_is_ready(store):
    store.bindings = [{"app_key": "helpdesk ai", "env": "build", "name": "X", "mode": "stub"}]
    assert bh.check_split_keys(_ctx(run_input={"app_key": "helpdesk ai"}))["verdict"] == "ready"


def test_a_never_deployed_target_holding_a_port_is_fixable(store):
    store.targets = [{"id": "deptgt_1", "app_name": "Old", "environment": "staging",
                      "status": "active", "domains": ["192.0.2.202:8000"],
                      "created_at": NOW - timedelta(hours=5), "ok_runs": 0, "live_runs": 0},
                     {"id": "deptgt_2", "app_name": "Live", "environment": "staging",
                      "status": "active", "domains": ["192.0.2.202:8001"],
                      "created_at": NOW - timedelta(days=5), "ok_runs": 3, "live_runs": 0}]
    found = bh.check_stale_targets(_ctx())
    assert found["verdict"] == "fixable"
    assert found["details"]["autofix"] == {"fix": "targets.release_port",
                                           "args": {"ids": ["deptgt_1"]}}


def test_leases_past_expiry_are_fixable(store):
    store.leases["gateway"] = [{"id": "mlease_1"}]
    found = bh.check_leaked(_ctx())
    assert found["verdict"] == "fixable" and "mlease_1" in found["evidence"]


def test_a_pending_decision_of_a_dead_run_is_a_persons_choice(store):
    store.decisions = [{"id": "dec_1", "question": "Approve the PRD?", "created_at": ago(3000),
                        "owner_status": "failed", "owner": "wfr_1"}]
    found = bh.check_dead_decisions(_ctx())
    assert found["verdict"] == "fixable" and not found["fix"]["auto"]


def test_generated_keys_missing_for_deploy_are_fixable(store, monkeypatch):
    from boss_engine.templates import secrets

    monkeypatch.setattr(secrets, "declared_specs", lambda c, **k: {
        "SESSION_SECRET": {"test_default": "generated"}, "X": {"test_default": "none"}})
    ctx = _ctx(run_input={"app_key": "helpdesk ai"})
    ctx.connection = _PgStat()      # any connection: the savepoint runs the patched read
    monkeypatch.setattr(wp, "_has_binding", lambda ctx, n, env: False)
    found = bh.check_generated(ctx)
    assert found["verdict"] == "fixable" and found["details"]["autofix"]["fix"] == \
        "secrets.generate"


# ── code-aware checks ─────────────────────────────────────────────────────────────────────────

def _pkg(scripts: dict, **extra: Any) -> str:
    return json.dumps({"name": "a", "scripts": scripts, **extra})


def test_a_start_command_that_runs_a_file_the_build_never_emits_will_fail(tmp_path):
    app = _write(tmp_path, {"package.json": _pkg({"start": "node dist/server/index.js",
                                                  "build": "vite build"})})
    found = wp.check_start_command(_ctx(app_dir=app), {"id": "deploy"})
    assert found["verdict"] == "will_fail" and "client bundle" in found["evidence"]


def test_a_start_command_the_build_emits_is_ready(tmp_path):
    app = _write(tmp_path, {"package.json": _pkg({"start": "node dist/server/index.js",
                                                  "build": "vite build && tsc -p server"})})
    assert wp.check_start_command(_ctx(app_dir=app), {"id": "deploy"})["verdict"] == "ready"


def test_production_refusing_a_value_boss_binds_for_deploy_will_fail(tmp_path, monkeypatch):
    app = _write(tmp_path, {"package.json": _pkg({}), "src/config.ts": (
        "const isProd = process.env.NODE_ENV === 'production';\n"
        "if (isProd && process.env.ALLOW_DEMO_SEED) {\n"
        "  throw new Error('ALLOW_DEMO_SEED must not be set in production');\n"
        "}\n")})
    monkeypatch.setattr(wp, "_deploy_bindings", lambda ctx: {"ALLOW_DEMO_SEED": "test"})
    found = wp.check_prod_refusal(_ctx(app_dir=app), {"id": "deploy"})
    assert found["verdict"] == "will_fail" and "ALLOW_DEMO_SEED" in found["evidence"]
    monkeypatch.setattr(wp, "_deploy_bindings", lambda ctx: {})
    assert wp.check_prod_refusal(_ctx(app_dir=app), {"id": "deploy"})["verdict"] == "ready"


def test_a_lockfile_out_of_sync_is_fixable_by_a_dry_run(tmp_path):
    app = _write(tmp_path, {"package.json": _pkg({}, dependencies={"express": "^5.0.0",
                                                                   "pg": "^8.0.0"}),
                            "package-lock.json": json.dumps({"lockfileVersion": 3, "packages": {
                                "": {"dependencies": {"express": "^4.0.0"}}}})})
    found = wp.check_lockfile(_ctx(app_dir=app), {"id": "build"})
    assert found["verdict"] == "fixable"
    assert "express ^4.0.0 → ^5.0.0" in found["evidence"] and "pg missing" in found["evidence"]
    assert found["details"]["autofix"]["fix"] == "lockfile.regenerate"
    assert bh.FIXES["lockfile.regenerate"].dry_run_only


def test_a_lockfile_in_sync_is_ready(tmp_path):
    app = _write(tmp_path, {"package.json": _pkg({}, dependencies={"pg": "^8.0.0"}),
                            "package-lock.json": json.dumps({"lockfileVersion": 3, "packages": {
                                "": {"dependencies": {"pg": "^8.0.0"}}}})})
    assert wp.check_lockfile(_ctx(app_dir=app), {"id": "build"})["verdict"] == "ready"


def test_an_uncached_browser_is_warmed_by_boss(tmp_path, monkeypatch):
    from boss_engine.services import playwright_cache

    app = _write(tmp_path, {"package.json": _pkg({}), "package-lock.json": json.dumps({
        "packages": {"node_modules/@playwright/test": {"version": "1.64.0"}}})})
    monkeypatch.setattr(playwright_cache, "browsers_dir", lambda create=True: tmp_path)
    monkeypatch.setattr(playwright_cache, "is_cached", lambda v, base=None: False)
    found = wp.check_browser_cache(_ctx(app_dir=app), {"id": "build"})
    assert found["verdict"] == "fixable"
    assert found["details"]["autofix"] == {"fix": "cache.playwright",
                                           "args": {"version": "1.64.0"}}


def test_pass_two_pushing_beside_pass_ones_deploy_is_named():
    from boss_engine.services import two_pass

    spec = two_pass.with_more_tests({"entry_step": "build", "steps": [
        {"id": "build", "kind": "capability", "binding": {"capability": "build"},
         "next": ["deploy"]},
        {"id": "deploy", "kind": "capability", "binding": {"capability": "deploy"}, "next": []}]})
    found = [c for c in wp.lifecycle_checks(_ctx(spec=spec), {})
             if c["id"] == "lifecycle.concurrent_push"]
    assert found and "more_tests (pushes to the branch)" in found[0]["evidence"]
    assert "deploy (ships the branch)" in found[0]["evidence"]


# ── auto-fixes ────────────────────────────────────────────────────────────────────────────────

class _Engine:
    def __init__(self):
        self.began = 0

    def begin(self):
        engine = self

        class _Tx:
            def __enter__(self_inner):
                engine.began += 1
                return "conn"

            def __exit__(self_inner, *exc):
                return False
        return _Tx()


def _report_with(fix_id: str, args: dict) -> dict:
    return {"steps": [{"step_id": "(data)", "checks": [wp.result(
        "data.x", "fixable", "e", action="fixable: BOSS can do this", auto=True,
        details={"autofix": {"fix": fix_id, "args": args}})]}]}


def test_fixes_are_planned_by_default_and_applied_only_when_asked(monkeypatch):
    calls = []
    monkeypatch.setitem(bh.FIXES, "t.fix", bh.Fix("t.fix", "a test fix",
                                                   lambda c, s, p, a: calls.append(a) or
                                                   {"changed": [a]}))
    engine = _Engine()
    planned = bh.apply_fixes(engine, None, "p1", _report_with("t.fix", {"n": 1}), apply=False)
    assert planned[0]["dry_run"] and not planned[0]["applied"] and not calls
    assert planned[0]["planned"] == {"n": 1}
    applied = bh.apply_fixes(engine, None, "p1", _report_with("t.fix", {"n": 1}), apply=True)
    assert applied[0]["applied"] and calls == [{"n": 1}] and engine.began == 1


def test_a_dry_run_only_fix_never_writes_even_under_fix(monkeypatch):
    engine = _Engine()
    out = bh.apply_fixes(engine, None, "p1", _report_with("lockfile.regenerate",
                                                          {"report": "in sync afterwards"}),
                         apply=True)
    assert out[0]["dry_run"] and not out[0]["applied"] and engine.began == 0


def test_a_failing_fix_says_why_and_the_others_still_run(monkeypatch):
    def boom(c, s, p, a):
        raise RuntimeError("no")
    monkeypatch.setitem(bh.FIXES, "t.boom", bh.Fix("t.boom", "boom", boom))
    out = bh.apply_fixes(_Engine(), None, "p1", _report_with("t.boom", {}), apply=True)
    assert out[0]["error"] == "RuntimeError: no" and not out[0]["applied"]


class _Conn:
    """Enough of a connection for the re-file fix: two bindings, the canonical one maybe set."""

    def __init__(self, canonical_has: bool):
        self.canonical_has = canonical_has
        self.inserted: list[dict] = []

    def execute(self, statement, params=None):
        sql, p = str(statement), dict(params or {})
        conn = self

        class _R:
            def first(self_inner):
                if sql.startswith("SELECT 1 FROM app_secret_bindings"):
                    return (1,) if conn.canonical_has else None
                return None

            def mappings(self_inner):
                return self_inner

        if sql.startswith("SELECT mode, vault_item_id"):
            class _M:
                def mappings(self_inner):
                    return self_inner

                def first(self_inner):
                    return {"mode": "test", "vault_item_id": "vlt_1", "shape": "unknown"}
            return _M()
        if sql.startswith("INSERT INTO app_secret_bindings"):
            self.inserted.append(p)
        return _R()


def test_refile_copies_never_deletes_records_and_is_idempotent(monkeypatch):
    from boss_engine.identity import vault

    recorded = []
    monkeypatch.setattr(bh, "_org", lambda c, p: "org_1")
    monkeypatch.setattr(bh, "_record", lambda c, p, f, et, eid, ch: recorded.append((f, ch)))
    monkeypatch.setattr(vault, "read_app_secret", lambda *a, **k: "s3cret")
    monkeypatch.setattr(vault, "store_app_secret", lambda **k: "vlt_2")
    args = {"canonical": "helpdesk ai", "items": [{"from": "helpdesk", "env": "deploy",
                                                   "name": "DEMO_PASSWORD"}]}
    conn = _Conn(canonical_has=False)
    out = bh.fix_refile(conn, None, "p1", args)
    assert out["changed"] == [{"name": "DEMO_PASSWORD", "env": "deploy", "mode": "test",
                               "from": "helpdesk"}]
    assert conn.inserted[0]["a"] == "helpdesk ai" and conn.inserted[0]["by"] == bh.ACTOR
    assert recorded == [("secrets.refile", {"copied": out["changed"], "deleted": []})]
    again = bh.fix_refile(_Conn(canonical_has=True), None, "p1", args)
    assert again["changed"] == [] and "already under" in again["skipped"][0]


def test_the_fix_events_are_written_by_boss_precheck(monkeypatch):
    from boss_engine.services import core

    seen = []
    monkeypatch.setattr(core, "emit_event", lambda *a: seen.append(a))
    bh._record("c", "p1", "secrets.generate", "app", "helpdesk ai", {"generated": ["X"]})
    assert seen[0][2] == "precheck.autofix_applied" and seen[0][3] == "boss precheck"


# ── explain and the CLI ───────────────────────────────────────────────────────────────────────

def _report() -> dict:
    return {"workflow_name": "D1", "checkpoint": "pre_run", "project_id": "p1", "steps": [
        {"step_id": "build", "checks": [
            wp.result("app_env.build", "will_fail", "DEMO_PASSWORD refused",
                      action="BOSS change: accept a build binding"),
            wp.result("logs.app_start", "will_fail", "DEMO_PASSWORD unbound",
                      action="Set DEMO_PASSWORD for env build in the app's Secrets.")]},
        {"step_id": "(data)", "checks": [wp.result(
            "data.split_app_keys", "fixable", "old key", action="fixable: BOSS can do this",
            auto=True, details={"autofix": {"fix": "secrets.refile", "args": {}}})]}],
        "summary": {"ready": 0, "fixable": 1, "will_fail": 2, "unknown": 0, "blocking": [
            "build:app_env.build", "build:logs.app_start"], "sentence": "2 will fail"}}


def test_explain_gives_each_failure_its_shortest_path():
    report = _report()
    items = bh.explain(report)
    kinds = {i["check"]: i["kind"] for i in items}
    assert kinds == {"app_env.build": "BOSS bug", "logs.app_start": "person",
                     "data.split_app_keys": "auto-fix"}
    bug = next(i for i in items if i["kind"] == "BOSS bug")
    assert re.search(r"known-issue pc-[0-9a-f]{10}", bug["path"])
    assert [i["verdict"] for i in items] == ["will_fail", "will_fail", "fixable"]
    assert bh.known_issue_fingerprint(report["steps"][0]["checks"][0]) == \
        bh.known_issue_fingerprint(dict(report["steps"][0]["checks"][0]))


def test_the_cli_prints_the_fix_plan_and_the_explanation():
    report = _report()
    report["fixes"] = [{"check": "(data):data.split_app_keys", "fix": "secrets.refile",
                        "label": "copy", "applied": False, "dry_run": True, "planned": {}}]
    report["explain"] = bh.explain(report)
    out = cli.render(report)
    assert "FIXABLE: BOSS CAN DO THIS (dry run; --fix applies)" in out
    assert "(data):data.split_app_keys" in out
    text = cli.render_explain(report)
    assert "build:logs.app_start → person: Set DEMO_PASSWORD" in text


def test_fix_is_off_by_default_in_the_cli(monkeypatch):
    seen = {}

    def fake_precheck(engine, settings, **kw):
        seen.update(kw)
        r = _report()
        r["fixes"], r["explain"] = [], []
        return r
    import boss_engine.config as config
    import boss_engine.db as db

    monkeypatch.setattr(config, "get_settings", lambda: None)
    monkeypatch.setattr(db, "get_engine", lambda s: None)
    monkeypatch.setattr(wp, "precheck", fake_precheck)
    cli.main(["--workflow", "D1", "--project", "p1"])
    assert seen["fix"] is False and set(seen["families"]) == set(bh.FAMILIES)
    cli.main(["--workflow", "D1", "--project", "p1", "--fix", "--families", "data"])
    assert seen["fix"] is True and seen["families"] == ["data"]


def test_an_expired_hold_with_money_on_it_is_a_persons_reconcile_not_an_auto_fix(store):
    # The auto-fix (BudgetGovernor.expire_stale_reservations) expires only zero-amount holds,
    # so offering it for res_… with money on it reported "applied" and changed nothing.
    store.leases["budget"] = [{"id": "res_money", "amount_usd_micros": 2_500_000}]
    found = bh.check_leaked(_ctx())
    assert found["verdict"] == "fixable" and not found["fix"]["auto"]
    assert "details" not in found
    assert "res_money $2.50" in found["evidence"] and "Decisions" in found["fix"]["action"]


def test_a_zero_amount_expired_hold_is_still_auto_fixed_beside_one_with_money(store):
    store.leases["budget"] = [{"id": "res_zero", "amount_usd_micros": 0},
                              {"id": "res_money", "amount_usd_micros": 1_000_000}]
    found = bh.check_leaked(_ctx())
    assert found["fix"]["auto"]
    assert found["details"]["autofix"]["args"]["budget"] == 1
    assert "res_zero" in found["evidence"] and "res_money" in found["evidence"]
