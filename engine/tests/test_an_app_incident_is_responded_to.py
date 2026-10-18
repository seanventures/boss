"""An app's error-rate alert carries what a response needs, and the response is checkable.

D2 pre-mortem (production incident detection and recovery). What the alert used to leave behind
was an incidents row with a percentage. These tests pin the pieces that make it a response:

  * the monitor counts each request ONCE however many sweepers run (the laptop's three worker
    slots stored HelpDesk AI's traffic three times over);
  * the scrape says WHICH route fails;
  * the incident records the serving release, the previous one and the build that owns the
    app, and says so as a dispatchable `incident.opened` event — once per occurrence;
  * the investigation correlates metrics, the app's logs and the release diff into a
    hypothesis with a stated basis, and never reads anything an operator could not;
  * recovery is judged from BOSS's own scrapes against the pre-incident baseline;
  * the report keeps billed and subscription-included spend apart.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import text

from ._obs_seed import pg_engine, project
from .test_an_app_reports_its_own_traffic import FakeApp
from boss_engine.config import get_settings
from boss_engine.services import app_metrics, deployment_health, incident_response as ir
from boss_engine.services.app_metrics import aggregate, extract, parse_exposition, window_delta


def orders_page(ok_post: int, err_post: int, ok_get: int) -> str:
    return (
        "# TYPE http_requests_total counter\n"
        f'http_requests_total{{method="POST",route="/orders",status="201"}} {ok_post}\n'
        f'http_requests_total{{method="POST",route="/orders",status="500"}} {err_post}\n'
        f'http_requests_total{{method="GET",route="/orders/{{order_id}}",status="200"}} {ok_get}\n')


@pytest.fixture(scope="module")
def app():
    a = FakeApp()
    yield a
    a.srv.shutdown()


@pytest.fixture()
def pg():
    eng = pg_engine()
    with eng.begin() as c:
        pid = project(c)
    return eng, pid


def _seed(c, pid: str, url: str, *, releases: list[tuple[str, str | None]],
          metrics: dict | None = None) -> str:
    """A stack target with healthy releases, oldest first: (commit, code_task_run_id|None)."""
    tid = f"t_{uuid4().hex[:10]}"
    c.execute(text(
        "INSERT INTO deployment_targets (id, project_id, app_name, repo, environment, target_kind, "
        " target_ref, status, created_by, domains, public_url) VALUES (:t, :p, 'order-api', "
        " 'acme/sandbox', 'staging', 'stack', 'order-api', 'active', 'test', '[]'::jsonb, :u)"),
        {"t": tid, "p": pid, "u": url})
    base = datetime.now(timezone.utc) - timedelta(hours=2)
    for i, (commit, run) in enumerate(releases):
        manifest = {"name": "order-api", "compose_project": "order-api", "workdir": "order-api",
                    "health": [], "metrics": metrics or {"path": "/metrics", "via": "public"}}
        at = base + timedelta(minutes=10 * i)
        c.execute(text(
            "INSERT INTO stack_deployments (id, project_id, target_id, repository, ref, "
            " commit_sha, manifest, status, created_by, created_at, finished_at, "
            " code_task_run_id) VALUES (:id, :p, :t, 'acme/sandbox', 'boss/code/crun_root', :c, "
            " CAST(:m AS jsonb), 'healthy', 'usr_op', :at, :at, :run)"),
            {"id": f"sd_{uuid4().hex[:10]}", "p": pid, "t": tid, "c": commit, "at": at,
             "m": json.dumps(manifest), "run": run})
    return tid


def _target(eng, tid: str) -> dict:
    with eng.begin() as c:
        return dict(c.execute(text(
            "SELECT t.*, " + deployment_health.STACK_OPEN_URL_COLUMN.format(alias="t")
            + " FROM deployment_targets t WHERE t.id = :t"), {"t": tid}).mappings().one())


def _scrape(eng, tid: str, at: datetime, **kw) -> dict:
    t = _target(eng, tid)
    with eng.begin() as c:
        decl = app_metrics.declarations(c, [tid])[tid]
    return app_metrics.scrape_target(eng, get_settings(), t, decl, now=at, **kw)


def _rows(eng, tid: str) -> int:
    with eng.begin() as c:
        return int(c.execute(text("SELECT count(*) FROM app_metrics_scrapes WHERE "
                                  "deployment_target_id = :t"), {"t": tid}).scalar())


# ── pure ────────────────────────────────────────────────────────────────────────────────────
def test_the_scrape_says_which_route_is_failing():
    a = extract(parse_exposition(orders_page(80, 0, 100)))
    b = extract(parse_exposition(orders_page(100, 30, 150)))
    d = window_delta(a, b)
    assert d["routes"] == {"POST /orders": {"requests": 50.0, "errors": 30.0},
                           "GET /orders/{order_id}": {"requests": 50.0, "errors": 0.0}}
    w = aggregate([{"requests": d["requests"], "errors": d["errors"], "interval_s": 60,
                    "route_deltas": d["routes"]}])
    assert w["routes"][0] == {"route": "POST /orders", "requests": 50.0, "errors": 30.0,
                              "error_rate": 0.6}


def test_structured_logs_and_plain_tracebacks_become_error_signatures():
    logs = "\n".join([
        'order-api-1  | {"ts":"2026-10-18T10:00:01Z","level":"info","route":"/orders","status":201}',
        'order-api-1  | {"ts":"2026-10-18T10:00:02Z","level":"error","method":"POST",'
        '"route":"/orders","status":500,"exc_type":"TypeError","exc_message":'
        '"unsupported operand type(s) for *: \'NoneType\' and \'int\'",'
        '"location":"/app/app/pricing.py:41"}',
        'order-api-1  | {"ts":"2026-10-18T10:00:03Z","level":"error","method":"POST",'
        '"route":"/orders","status":500,"exc_type":"TypeError","exc_message":"x token=ghp_'
        + "a" * 36 + '","location":"/app/app/pricing.py:41"}',
        "Traceback (most recent call last):",
        '  File "/app/app/main.py", line 88, in create_order',
        "    total = price_order(items)",
        '  File "/app/app/pricing.py", line 41, in price_order',
        "    return unit * qty",
        "TypeError: unsupported operand type(s) for *: 'NoneType' and 'int'",
        "INFO: 127.0.0.1 GET /health 200",
    ])
    events = ir.parse_log_events(logs)
    assert len(events) == 3
    sigs = ir.error_signatures(events)
    assert sigs[0]["count"] == 2 and sigs[0]["location"] == "/app/app/pricing.py:41"
    assert sigs[0]["route"] == "POST /orders" and sigs[0]["exc_type"] == "TypeError"
    assert "ghp_" not in json.dumps(sigs), "a credential in a log line is never carried on"
    tb = [s for s in sigs if not s["route"]][0]
    assert tb["location"] == "/app/app/pricing.py:41" and tb["exc_type"] == "TypeError"


def test_the_hypothesis_says_its_basis_and_never_more():
    onset = datetime(2026, 10, 18, 10, 5, tzinfo=timezone.utc)
    deploy = {"commit_sha": "f" * 40, "deployed_at": "2026-10-18T10:00:00+00:00"}
    alert = {"routes": [{"route": "POST /orders", "requests": 50, "errors": 30,
                         "error_rate": 0.6}]}
    sigs = [{"exc_type": "TypeError", "location": "/app/app/pricing.py:41",
             "route": "POST /orders", "count": 12, "sample": ""}]
    diff = {"files": [{"filename": "order-api/app/pricing.py"}, {"filename": "README.md"}]}
    hot = ir.hypothesis(deploy, alert, sigs, diff, onset=onset)
    assert hot["basis"] == "release_changed_failing_frame" and hot["confidence"] == 0.9
    assert hot["suspect_file"] == "order-api/app/pricing.py"
    assert hot["suspect_commit"] == "f" * 40 and hot["minutes_after_release"] == 5.0
    assert "POST /orders carries the failures (60.0%" in hot["root_cause"]
    assert "went out 5.0 min before the alert fired" in hot["root_cause"]
    told = ir.hypothesis(deploy, alert, sigs, diff, onset=onset,
                         onset_was="the first customer report")
    assert "went out 5.0 min before the first customer report" in told["root_cause"]
    cold = ir.hypothesis(deploy, alert, sigs, {"files": [{"filename": "README.md"}]},
                         onset=onset)
    assert cold["basis"] == "release_before_onset" and cold["suspect_file"] is None
    old = ir.hypothesis({**deploy, "deployed_at": "2026-10-17T10:00:00+00:00"}, alert, sigs,
                        diff, onset=onset)
    assert old["basis"] == "no_release_near_onset" and old["suspect_commit"] is None


def test_recovery_needs_traffic_and_the_baseline():
    rule = {"error_rate_percent": 5.0, "min_requests": 20}
    assert ir.judge_recovery({"requests": 10, "error_rate": 0.0}, {"error_rate": 0}, rule) \
        == "quiet"
    assert ir.judge_recovery({"requests": 200, "error_rate": 0.005}, {"error_rate": 0}, rule) \
        == "clear"
    # under the alert threshold but well above where it was before the incident
    assert ir.judge_recovery({"requests": 200, "error_rate": 0.04}, {"error_rate": 0.0}, rule) \
        == "failing"


# ── against Postgres ────────────────────────────────────────────────────────────────────────
def test_two_sweepers_count_the_traffic_once(pg, app):
    eng, pid = pg
    with eng.begin() as c:
        tid = _seed(c, pid, app.origin + "/", releases=[("a" * 40, "crun_root")])
    t0 = datetime.now(timezone.utc).replace(microsecond=0)
    app.status, app.body = 200, orders_page(10, 0, 10)
    _scrape(eng, tid, t0, only_if_due=True)
    app.body = orders_page(40, 0, 40)
    first = _scrape(eng, tid, t0 + timedelta(seconds=130), only_if_due=True)
    second = _scrape(eng, tid, t0 + timedelta(seconds=130, milliseconds=20), only_if_due=True)
    assert first["requests"] == 60 and second.get("skipped") is True
    assert _rows(eng, tid) == 2
    # a slow fetch that lands after a newer scrape is dropped, never stored as a "reset"
    late = _scrape(eng, tid, t0 + timedelta(seconds=100))
    assert late.get("skipped") is True and _rows(eng, tid) == 2


def test_the_alert_records_the_release_and_says_so_once(pg, app):
    eng, pid = pg
    with eng.begin() as c:
        tid = _seed(c, pid, app.origin + "/",
                    releases=[("a" * 40, "crun_root"), ("b" * 40, None)])
    t0 = datetime.now(timezone.utc).replace(microsecond=0)
    app.status, app.body = 200, orders_page(100, 0, 100)
    _scrape(eng, tid, t0)
    app.body = orders_page(130, 30, 160)
    fired = _scrape(eng, tid, t0 + timedelta(seconds=60))
    assert fired["alert"]["action"] == "opened" and fired["alert"]["event_id"]
    with eng.begin() as c:
        inc = c.execute(text("SELECT id, finalize_ctx FROM incidents WHERE dedupe_key = :k"),
                        {"k": app_metrics.INCIDENT_PREFIX + tid}).mappings().one()
        kinds = [r[0] for r in c.execute(text(
            "SELECT kind FROM incident_evidence WHERE incident_id = :i ORDER BY created_at"),
            {"i": inc["id"]}).all()]
        event = c.execute(text("SELECT data FROM events WHERE id = :e"),
                          {"e": fired["alert"]["event_id"]}).scalar()
    ctx = inc["finalize_ctx"]
    assert ctx["deploy"]["commit_sha"] == "b" * 40
    assert ctx["deploy"]["previous_commit_sha"] == "a" * 40
    # the hand-deployed release has no build; the app's lineage still owns the fix
    assert ctx["deploy"]["extends_run_id"] == "crun_root"
    assert ctx["alert"]["routes"][0]["route"] == "POST /orders"
    assert kinds == ["alert", "deployment"]
    assert event["incident_id"] == inc["id"] and event["commit_sha"] == "b" * 40
    assert event["extends_run_id"] == "crun_root" and event["directive"].startswith("Respond")

    # a response is in flight: the alert firing again starts no second one
    with eng.begin() as c:
        c.execute(text(
            "INSERT INTO workflow_definitions (id, project_id, workflow_key, name, created_by) "
            "VALUES (:d, :p, :k, 'x', 't')"), {"d": "wfd_" + tid, "p": pid, "k": "ir_" + tid})
        c.execute(text(
            "INSERT INTO workflow_versions (id, project_id, workflow_definition_id, version, "
            " spec, content_hash, created_by) VALUES (:v, :p, :d, 1, '{}'::jsonb, :h, 't')"),
            {"v": "wfv_" + tid, "p": pid, "d": "wfd_" + tid, "h": tid})
        c.execute(text(
            "INSERT INTO workflow_runs (id, project_id, workflow_definition_id, "
            " workflow_version_id, actor, status, input) VALUES (:r, :p, :d, :v, 't', "
            " 'running', CAST(:i AS jsonb))"),
            {"r": "wfr_" + tid, "p": pid, "d": "wfd_" + tid, "v": "wfv_" + tid,
             "i": json.dumps({"incident_id": inc["id"]})})
    # ...and a dip under the threshold mid-fix is not recovery: the response's verify step
    # closes the incident, not the monitor (D2: resolved at 07:12:43 mid-fix, reopened 07:13:04)
    app.body = orders_page(400, 30, 460)
    dip = _scrape(eng, tid, t0 + timedelta(seconds=120))
    assert dip["alert"]["verdict"] == "ok" and dip["alert"]["action"] is None
    with eng.begin() as c:
        assert c.execute(text("SELECT status FROM incidents WHERE id = :i"),
                         {"i": inc["id"]}).scalar() == "open"
    # a person closed it meanwhile; it fires again while the fix is still in flight: evidence
    # only — the occurrence, the alert and the response it reports on stay the first ones
    with eng.begin() as c:
        c.execute(text("UPDATE incidents SET status = 'resolved' WHERE id = :i"),
                  {"i": inc["id"]})
    app.body = orders_page(430, 90, 520)
    again = _scrape(eng, tid, t0 + timedelta(seconds=180))
    assert again["alert"]["action"] == "reopened" and again["alert"]["event_id"] is None
    with eng.begin() as c:
        after = c.execute(text("SELECT finalize_ctx FROM incidents WHERE id = :i"),
                          {"i": inc["id"]}).scalar()
    assert after.get("occurrence") == ctx.get("occurrence")
    assert after["alert"]["fired_at"] == ctx["alert"]["fired_at"]


class FakeHost:
    def __init__(self, text_: str):
        self.text, self.commands = text_, []

    def run(self, command, *, timeout=120, write=False, **_):
        assert write is False, "the investigation never writes on the app's host"
        self.commands.append(command)
        return SimpleNamespace(returncode=0, stdout=self.text, stderr="")


class FakeGitHub:
    def compare_summary(self, base, head):
        return {"commits": [{"sha": head, "message": "Release 1.1: bulk pricing"}],
                "files": [{"filename": "order-api/app/pricing.py", "status": "modified",
                           "additions": 6, "deletions": 1,
                           "patch": "@@ -38 +38 @@\n-    unit = sku.price\n+    unit = sku.bulk"}]}


def _fired_incident(eng, pid, app, *, t0) -> tuple[str, str]:
    with eng.begin() as c:
        tid = _seed(c, pid, app.origin + "/",
                    releases=[("a" * 40, "crun_root"), ("b" * 40, None)])
    app.status, app.body = 200, orders_page(100, 0, 100)
    _scrape(eng, tid, t0 - timedelta(minutes=20))
    app.body = orders_page(300, 0, 300)
    _scrape(eng, tid, t0 - timedelta(minutes=10))              # the pre-incident baseline: 0%
    app.body = orders_page(330, 30, 360)
    _scrape(eng, tid, t0)
    with eng.begin() as c:
        inc = c.execute(text("SELECT id FROM incidents WHERE dedupe_key = :k"),
                        {"k": app_metrics.INCIDENT_PREFIX + tid}).scalar()
    return tid, inc


def test_the_investigation_correlates_metrics_logs_and_the_release(pg, app):
    eng, pid = pg
    t0 = datetime.now(timezone.utc).replace(microsecond=0)
    tid, inc = _fired_incident(eng, pid, app, t0=t0)
    host = FakeHost('{"level":"error","method":"POST","route":"/orders","status":500,'
                    '"exc_type":"TypeError","location":"/app/app/pricing.py:41"}\n' * 5)
    with eng.begin() as c:
        got = ir.investigate(c, get_settings(), pid, inc, host_access=host, github=FakeGitHub())
        kinds = {r[0] for r in c.execute(text(
            "SELECT kind FROM incident_evidence WHERE incident_id = :i"), {"i": inc}).all()}
        status = c.execute(text("SELECT status FROM incidents WHERE id = :i"),
                           {"i": inc}).scalar()
    assert "label=com.docker.compose.project=order-api" in host.commands[0]
    assert got["hypothesis"]["basis"] == "release_changed_failing_frame"
    assert got["suspect_commit"] == "b" * 40 and got["extends_run_id"] == "crun_root"
    assert got["title"].startswith("Fix order-api: TypeError on POST /orders")
    brief = got["incident_brief"]
    assert "FAILS on bbbbbbbbbbbb" in brief and "smallest change" in brief
    assert "Release 1.1: bulk pricing" in brief and "sku.bulk" in brief
    assert {"logs", "release_diff", "rca_hypothesis"} <= kinds and status == "diagnosing"


def test_recovery_is_judged_by_the_monitor_and_the_report_keeps_spend_apart(pg, app):
    eng, pid = pg
    t0 = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=30)
    tid, inc = _fired_incident(eng, pid, app, t0=t0)
    fixed_at = t0 + timedelta(minutes=10)
    with eng.begin() as c:
        check = ir.start_recovery_check(c, pid, inc, since=fixed_at, stable_minutes=3,
                                        timeout_minutes=20, now=fixed_at)
    assert check["status"] == "waiting"
    app.body = orders_page(18, 2, 20)                        # the fix restarted the app
    _scrape(eng, tid, fixed_at + timedelta(minutes=1))
    with eng.begin() as c:
        still = ir.look_at_recovery(c, check["id"], now=fixed_at + timedelta(minutes=1))
    assert still["status"] == "waiting" and "Still failing" in still["summary"], still
    app.body = orders_page(218, 2, 220)
    _scrape(eng, tid, fixed_at + timedelta(minutes=2))
    app.body = orders_page(418, 2, 420)
    _scrape(eng, tid, fixed_at + timedelta(minutes=4))
    app.body = orders_page(618, 2, 620)
    _scrape(eng, tid, fixed_at + timedelta(minutes=6))
    with eng.begin() as c:
        done = ir.look_at_recovery(c, check["id"], now=fixed_at + timedelta(minutes=6))
        inc_status = c.execute(text("SELECT status FROM incidents WHERE id = :i"),
                               {"i": inc}).scalar()
    assert done["status"] == "recovered" and done["summary"].startswith("Recovered: 0.0%")
    assert inc_status == "resolved"

    with eng.begin() as c:
        c.execute(text("INSERT INTO code_task_runs (id, project_id, title, finalize_ctx) "
                       "VALUES (:r, :p, 'fix', CAST(:f AS jsonb))"),
                  {"r": "crun_" + tid, "p": pid, "f": json.dumps({"workflow_run_id": "wfr_x" + tid})})
        for micros, included in ((1_250_000, False), (4_000_000, True)):
            c.execute(text(
                "INSERT INTO llm_calls (id, project_id, code_task_run_id, stage, engine, model, "
                " cost_usd_micros, included_in_subscription, ok, total_tokens) VALUES (:i, :p, "
                " :r, 'implement', 'claude_code', 'm', :c, :inc, true, 1000)"),
                {"i": f"llm_{uuid4().hex[:8]}", "p": pid, "r": "crun_" + tid, "c": micros,
                 "inc": included})
        rep = ir.report(c, pid, inc, workflow_run_id="wfr_x" + tid)
        stored = c.execute(text("SELECT payload->>'kind' FROM sre_postmortems WHERE "
                                "incident_id = :i"), {"i": inc}).scalar()
    assert rep["cost"]["billed_usd"] == 1.25 and rep["cost"]["included_list_usd"] == 4.0
    assert "Billed model spend: $1.25" in rep["markdown"]
    assert "list-price equivalent, not billed): $4.00" in rep["markdown"]
    assert rep["faulty_release"] == "b" * 40 and rep["status"] == "resolved"
    assert any("Alert fired" in t["what"] for t in rep["timeline"])
    assert any(t["kind"] == "verification" for t in rep["timeline"])
    assert stored == "incident_report"


# ── The whole path, through the real runner (the D2 plumbing check) ─────────────────────────
def test_an_alert_is_answered_end_to_end_by_incident_response(pg, app, monkeypatch):
    """Alert -> incident.opened -> Incident Response (started by the event) -> investigate
    (metrics x logs x diff) -> the fix built AS THE APP'S NEXT CHANGE with the brief -> CI ->
    a person approves the deploy -> recovery judged from BOSS's own scrapes -> the report.

    The build, CI and the deploy are fakes that land finished rows where the runner looks; the
    monitor, the investigation, the recovery check and the report are real."""
    from boss_engine.services import event_dispatch, event_subscription_service as subs
    from boss_engine.services import workflow_capability_runner as runner
    from boss_engine.services import workflow_catalogue as wc
    from .test_a_failed_step_stops_the_run import _insert_awaited
    from .test_every_template_runs_end_to_end import FakeTool, Registry, _drive
    from .test_workflow_capability_runner import Ctx, _publish, _steps

    eng, pid = pg
    now = datetime.now(timezone.utc).replace(microsecond=0)
    with eng.begin() as c:
        tid = _seed(c, pid, app.origin + "/",
                    releases=[("a" * 40, "crun_root"), ("b" * 40, None)])
    # the release before the fault was healthy; then the regression
    app.status, app.body = 200, orders_page(100, 0, 100)
    _scrape(eng, tid, now - timedelta(minutes=12))
    app.body = orders_page(300, 0, 300)
    _scrape(eng, tid, now - timedelta(minutes=10))
    app.body = orders_page(330, 30, 360)
    fired = _scrape(eng, tid, now - timedelta(minutes=8))
    assert fired["alert"]["action"] == "opened"

    monkeypatch.setattr(ir, "_host_for_target", lambda c, t: {"name": "vm"})
    import boss_engine.services.stack_deploy as sd
    monkeypatch.setattr(sd, "host_access_for", lambda c, s, h: FakeHost(
        '{"level":"error","method":"POST","route":"/orders","status":500,'
        '"exc_type":"TypeError","location":"/app/app/pricing.py:41"}\n' * 4))
    monkeypatch.setattr(ir, "_github_for", lambda *a, **k: FakeGitHub())
    # two minutes clear since the fix shipped (it shipped three minutes ago, below)
    monkeypatch.setattr(ir, "DEFAULT_STABLE_MINUTES", 2)
    # The stack plan (SSH preflight, approval card) is its own suite; here the deploy is the
    # tool fake below, which lands the healthy release the recovery is judged from.
    monkeypatch.setattr(sd, "stack_target_for_deploy", lambda *a, **k: None)

    builds: list[dict] = []
    with eng.begin() as c:
        entry = next(e for e in wc.CATALOGUE if e["key"] == "incident_response")
        spec = wc.entry_spec(entry)
        _definition, version = _publish(c, pid, "incident_response", spec["steps"],
                                        spec["entry_step"])
        c.execute(text("UPDATE workflow_versions SET spec = spec || CAST(:x AS JSONB) "
                       "WHERE id = :v"),
                  {"v": version["id"], "x": json.dumps({"more_tests": False,
                                                       "input_schema": spec["input_schema"]})})
        c.execute(text("UPDATE workflow_definitions SET active_version_id = :v WHERE id = :d"),
                  {"v": version["id"], "d": _definition["id"]})

        def code_task(p, args):
            builds.append(args)
            run_id = f"crun_fix_{uuid4().hex[:6]}"
            _insert_awaited(c, p, "code_task_runs", run_id, "completed")
            c.execute(text("UPDATE code_task_runs SET pull_request_url = :u WHERE id = :i"),
                      {"u": "https://github.com/acme/sandbox/pull/12", "i": run_id})
            return {"status": "queued", "run_id": run_id,
                    "pull_request_url": "https://github.com/acme/sandbox/pull/12"}

        def deploy(p, args):
            sdep, drn = f"sd_{uuid4().hex[:8]}", f"drn_{uuid4().hex[:8]}"
            c.execute(text(
                "INSERT INTO stack_deployments (id, project_id, target_id, repository, ref, "
                " commit_sha, manifest, status, created_by, created_at, finished_at) VALUES "
                " (:i, :p, :t, 'acme/sandbox', 'pull/12', :c, CAST(:m AS JSONB), 'healthy', "
                " 'usr_op', :at, :at)"),
                {"i": sdep, "p": p, "t": tid, "c": "c" * 40, "at": now - timedelta(minutes=3),
                 "m": json.dumps({"name": "order-api", "compose_project": "order-api",
                                  "metrics": {"path": "/metrics", "via": "public"}})})
            _insert_awaited(c, p, "deployment_runs", drn, "succeeded")
            return {"status": "planned", "deployment_run_id": drn, "stack_deployment_id": sdep}

        tools = {n: FakeTool(n, h) for n, h in (("create_code_task", code_task),
                                                ("deploy_app", deploy))}
        monkeypatch.setattr(runner, "_tool_registry", lambda: Registry(list(tools.values())))

        def ci_passed(_c, _p, _ctx, _inputs):
            wait_id = f"ciw_{uuid4().hex[:10]}"
            _insert_awaited(c, pid, "ci_waits", wait_id, "passed")
            return {"capability": "wait_for_ci", "tool": None, "status": "awaiting",
                    "await": {"kind": "ci_wait", "id": wait_id}, "ci_wait_id": wait_id,
                    "ci_status": "passed"}
        monkeypatch.setattr(runner, "_start_ci_wait", ci_passed)

        # THE ALERT'S OWN EVENT STARTS THE RESPONSE (a subscription; no person in this test)
        # (this bare test project has no model, repository or release manager: accepted, as an
        # operator can when setting a subscription up — the readiness refusal is its own test)
        subs.create_event_subscription(c, pid, "incident.opened", "workflow",
                                       "incident_response", actor="test",
                                       config={"start_anyway": ["model", "repository",
                                                                "role:release_manager"]})
        event_dispatch.invalidate_subscription_cache(pid)
        data = c.execute(text("SELECT data FROM events WHERE id = :e"),
                         {"e": fired["alert"]["event_id"]}).scalar()
        started = event_dispatch.dispatch_event(
            c, event_id=fired["alert"]["event_id"], project_id=pid,
            event_type="incident.opened", actor="monitor", entity_type="incident",
            entity_id=data["incident_id"], data=data)
        assert any(s.get("run_id") for s in started), started
        run_id = next(s["run_id"] for s in started if s.get("kind") == "workflow")

        # recovery: the fixed release serves cleanly, scraped after it shipped
        app.body = orders_page(30, 0, 30)
        _scrape(eng, tid, now - timedelta(minutes=2))
        app.body = orders_page(230, 0, 230)
        _scrape(eng, tid, now - timedelta(seconds=30))
        _drive(eng, c, pid, run_id, ticks=30)
        status = c.execute(text("SELECT status FROM workflow_runs WHERE id = :r"),
                           {"r": run_id}).scalar()
        steps = _steps(c, run_id)
        inc = c.execute(text("SELECT status FROM incidents WHERE id = :i"),
                        {"i": data["incident_id"]}).scalar()
        rep = c.execute(text("SELECT payload FROM sre_postmortems WHERE incident_id = :i"),
                        {"i": data["incident_id"]}).scalar()
        # The card lists the steps in the order the person lives them, not by row time. D2: rows
        # created together tie on created_at, and the card put "approve" after "Write-up".
        # Make creation time disagree with the workflow outright, so only the declared order wins.
        c.execute(text("UPDATE workflow_step_runs SET created_at = now() - "
                       "(CASE step_id WHEN 'approve' THEN interval '0 s' ELSE "
                       " interval '1 min' * ascii(step_id) END) WHERE workflow_run_id = :r"),
                  {"r": run_id})
        view = ir.response_view(c, pid, data["incident_id"])

    assert [s["step_id"] for s in view["response"]["steps"]] == [
        "investigate", "build", "ci", "report", "approve", "deploy", "verify", "incident_report"]
    unfinished = {k: (v["status"], v["last_error"]) for k, v in steps.items()
                  if v["status"] != "succeeded"}
    assert status == "completed" and not unfinished, (status, unfinished)
    assert list(steps) == ["investigate", "build", "ci", "report", "approve", "deploy",
                           "verify", "incident_report"]
    brief = builds[0]["instructions"]
    assert brief.startswith("INCIDENT FIX") and "pricing.py" in brief
    assert builds[0].get("extends_run_id") == "crun_root", builds[0]
    assert any("FAILS on the faulty release" in a for a in builds[0]["acceptance_criteria"])
    assert inc == "resolved"
    assert rep["kind"] == "incident_report" and rep["root_cause"]["suspect_commit"] == "b" * 40
    kinds = [t["kind"] for t in rep["timeline"]]
    assert {"alert", "deployment", "rca_hypothesis", "verification", "deploy"} <= set(kinds)
    # the person's release approval is in the timeline, with who (D2: only "Step approve
    # succeeded" was, and an independent check read the fix deploy as unapproved)
    approvals = [t["what"] for t in rep["timeline"] if t["kind"] == "approval"]
    assert any(w.startswith("Approve the release") and w.endswith("approved by ceo")
               for w in approvals), approvals
