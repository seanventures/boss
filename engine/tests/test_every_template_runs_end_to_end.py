"""Every catalogue template runs from Start to its last step — against fakes, through the real
runner.

THE GAP (boss-1 map, 2026-10-02). Nothing drove a catalogue template through the runner. The
catalogue tests checked validation, counts and seeding; on boss-1 three runs of one template had
ever happened and none finished. Whether the other eleven could finish at all — whether each
step's output reached the next step's input, whether each gate opened and closed, whether a
seven-day grace period ever ended — was unknown, so R5 could not be claimed or protected.

This starts every entry in CATALOGUE with only the inputs its own input_schema requires, and
ticks the real runner: research, the panel, the build, the preview, the launch-readiness repair
and the deployment are fakes that record what they were asked and land their work as finished
rows in the tables the runner waits on; every gate a person answers is approved; the grace
period is fast-forwarded. The run must complete with every step done or honestly skipped. A
template added to the catalogue is driven here without anyone listing it.
"""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import text

from boss_engine.services import workflow_capability_runner as runner
from boss_engine.services import workflow_catalogue as wc

from .test_a_failed_step_stops_the_run import _insert_awaited
from .test_workflow_capability_runner import (  # noqa: F401 - fixtures used by name
    Ctx,
    _publish,
    _start,
    _steps,
    eng,
    project,
)

TITLE = "Shift swap board"


@pytest.fixture(autouse=True)
def _tidy(eng, project):
    """Everything a run here leaves behind — builds, previews, deployments, records — removed
    before the shared fixture deletes the project, whatever order the foreign keys demand."""
    yield
    with eng.begin() as c:
        tables = [r[0] for r in c.execute(text(
            "SELECT table_name FROM information_schema.columns WHERE column_name = 'project_id' "
            "AND table_schema = current_schema() AND table_name <> 'projects'")).all()]
        for _ in range(6):
            left = []
            for table in tables:
                try:
                    with c.begin_nested():
                        c.execute(text(f"DELETE FROM {table} WHERE project_id = :p"),  # noqa: S608
                                  {"p": project})
                except Exception:  # noqa: BLE001 — a row another table still points at
                    left.append(table)
            if not left:
                break
            tables = left


class FakeTool:
    """A tool that does its work by landing a FINISHED row where the runner will look for it."""

    def __init__(self, name, handler):
        self.name = name
        self.kind = "write"
        self.description = name
        self.parameters = {}
        self._handler = handler
        self.calls: list[dict] = []

    def handler(self, ctx, *, project_id, actor, args):
        self.calls.append(dict(args))
        return self._handler(project_id, dict(args))


class Registry:
    def __init__(self, tools):
        self._tools = tools

    def specs(self):
        return list(self._tools)


def _fakes(c, project_id: str) -> dict[str, FakeTool]:
    def new(prefix: str) -> str:
        return f"{prefix}_{uuid.uuid4().hex[:10]}"

    def research(pid, args):
        run_id = new("drun")
        _insert_awaited(c, pid, "discovery_runs", run_id, "completed")
        c.execute(text("UPDATE discovery_runs SET title = :t, state = CAST(:s AS JSONB) "
                       "WHERE id = :i"),
                  {"i": run_id, "t": "Shift swapping today",
                   "s": json.dumps({"summary": "Teams swap shifts over chat; managers lose "
                                               "track of approvals."})})
        return {"status": "started", "discovery_id": run_id}

    def meeting(pid, args):
        run_id = new("mrun")
        _insert_awaited(c, pid, "meeting_runs", run_id, "completed")
        c.execute(text("UPDATE meeting_runs SET state = CAST(:s AS JSONB) WHERE id = :i"),
                  {"i": run_id, "s": json.dumps({"prd_markdown": (
                      "# PRD\nOffer, claim, approve.\n"
                      # Incremental Build reads its plan from here: every increment runs.
                      + "".join(f"## Increment {k}\nPart {k}.\n" for k in range(1, 5)))})})
        return {"status": "started", "meeting_run_id": run_id}

    def code_task(pid, args):
        run_id = new("crun")
        _insert_awaited(c, pid, "code_task_runs", run_id, "completed")
        return {"status": "queued", "run_id": run_id}

    def extend(pid, args):
        run_id = new("crun")
        _insert_awaited(c, pid, "code_task_runs", run_id, "completed")
        return {"status": "extending", "result": {"run_id": run_id}}

    def preview(pid, args):
        preview_id = new("prv")
        _insert_awaited(c, pid, "app_previews", preview_id, "running")
        c.execute(text("UPDATE app_previews SET run_id = :r WHERE id = :i"),
                  {"i": preview_id, "r": args.get("run_id")})
        return {"status": "starting", "preview_id": preview_id}

    def security_scan(pid, args):
        # A finished scan with nothing found: the review's later steps say "not needed".
        scan_id = new("scan")
        c.execute(text("INSERT INTO security_scans (id, project_id, app_key, status, counts, "
                       "started_at, ended_at) VALUES (:i, :p, :a, 'done', "
                       "'{\"total\": 0}'::jsonb, now(), now())"),
                  {"i": scan_id, "p": pid, "a": args.get("app_key")})
        return {"status": "queued", "scan_id": scan_id}

    made_targets: dict = {}

    def deploy(pid, args):
        # The app's target, made by its first deploy and reused by the next (Deploy again).
        target_id = made_targets.get(pid)
        if not target_id:
            target_id = made_targets[pid] = new("dt")
            _insert_awaited(c, pid, "deployment_targets", target_id, "active")
            c.execute(text("UPDATE deployment_targets SET app_name = :a, "
                           "domains = '[\"swap.test\"]' WHERE id = :i"),
                      {"i": target_id, "a": TITLE})
        run_id = new("drn")
        # The parent row _insert_awaited makes generically is named apart from the last one,
        # so a second deploy (Deploy again) does not collide with it.
        c.execute(text("UPDATE deployment_targets SET app_name = 'x-' || id "
                       "WHERE project_id = :p AND app_name = 'x'"), {"p": pid})
        _insert_awaited(c, pid, "deployment_runs", run_id, "succeeded")
        c.execute(text("UPDATE deployment_runs SET deployment_target_id = :t WHERE id = :i"),
                  {"i": run_id, "t": target_id})
        return {"status": "planned", "deployment_run_id": run_id}

    return {name: FakeTool(name, handler) for name, handler in (
        ("create_discovery", research), ("request_meeting", meeting),
        ("create_code_task", code_task), ("extend_code_task", extend),
        ("start_preview", preview), ("deploy_app", deploy),
        ("run_security_scan", security_scan))}


def _incident(c, project_id: str, monkeypatch) -> str:
    """An app incident as its own monitor opens one (incident_response.on_alert's record), for
    the template that starts from one. The investigation's outside reads find nothing (no host
    key, no GitHub) and say so; recovery is judged clear at once."""
    from boss_engine.services import incident_response

    target, inc = f"dt_{uuid.uuid4().hex[:10]}", f"inc_{uuid.uuid4().hex[:10]}"
    c.execute(text("INSERT INTO deployment_targets (id, project_id, app_name, repo, environment, "
                   " target_kind, target_ref, status, created_by) VALUES (:t, :p, 'order-api', "
                   " 'acme/app', 'staging', 'stack', 'order-api', 'active', 'test')"),
              {"t": target, "p": project_id})
    c.execute(text("INSERT INTO stack_deployments (id, project_id, target_id, repository, ref, "
                   " commit_sha, manifest, status, created_by) VALUES (:i, :p, :t, 'acme/app', "
                   " 'main', 'c0ffee', CAST(:m AS JSONB), 'healthy', 'test')"),
              {"i": f"sd_{uuid.uuid4().hex[:8]}", "p": project_id, "t": target,
               "m": json.dumps({"metrics": {"path": "/metrics"}})})
    ctx = {"app": {"target_id": target, "app_name": "order-api", "environment": "staging"},
           "deploy": {"commit_sha": "c0ffee"},
           "alert": {"error_rate": 0.2, "requests": 100, "rule": {"error_rate_percent": 5}},
           "occurrence": "2026-10-18T10:00:00+00:00"}
    c.execute(text("INSERT INTO incidents (id, project_id, dedupe_key, title, service, status, "
                   " finalize_ctx) VALUES (:i, :p, :k, 'order-api: 20% failing', 'order-api', "
                   " 'open', CAST(:f AS JSONB))"),
              {"i": inc, "p": project_id, "k": "app_error_rate:" + target, "f": json.dumps(ctx)})
    monkeypatch.setattr(incident_response, "judge_recovery", lambda *a, **k: "clear")
    monkeypatch.setattr(incident_response, "DEFAULT_STABLE_MINUTES", 0)
    return inc


def _drive(eng, c, project_id: str, run_id: str, ticks: int = 40) -> None:
    """Tick the real runner, answering every gate a person answers and ending any wait."""
    for _ in range(ticks):
        c.execute(text("UPDATE workflow_step_runs SET run_after = now() - interval '1 second' "
                       "WHERE workflow_run_id = :r AND status IN ('ready', 'waiting', "
                       "'pending') AND run_after > now()"), {"r": run_id})
        c.execute(text("UPDATE decisions SET status = 'approved', approved_by = 'ceo', "
                       "approved_at = now() WHERE status NOT IN ('approved', 'rejected') "
                       "AND id IN (SELECT decision_id FROM workflow_step_runs "
                       "WHERE workflow_run_id = :r AND decision_id IS NOT NULL)"),
                  {"r": run_id})
        runner.tick(c, Ctx(eng), project_id=project_id)
        status = c.execute(text("SELECT status FROM workflow_runs WHERE id = :r"),
                           {"r": run_id}).scalar()
        if status not in ("running", "validating", "waiting"):
            return


@pytest.mark.parametrize("key", sorted(e["key"] for e in wc.CATALOGUE))
def test_every_catalogue_template_runs_to_its_last_step(eng, project, monkeypatch, key):
    entry = next(e for e in wc.CATALOGUE if e["key"] == key)
    spec = wc.entry_spec(entry)
    from boss_engine.services import mattermost_adapter

    posted: list[tuple[str, str]] = []
    monkeypatch.setattr(mattermost_adapter.MattermostAdapter, "post_message",
                        lambda self, channel, subject, body: (
                            posted.append((channel, body)) or "mattermost-post:p1"))
    with eng.begin() as c:
        fakes = _fakes(c, project)
        monkeypatch.setattr(runner, "_tool_registry", lambda: Registry(list(fakes.values())))
        _definition, version = _publish(c, project, f"{key}_{uuid.uuid4().hex[:6]}",
                                        spec["steps"], spec["entry_step"])
        c.execute(text("UPDATE workflow_versions SET spec = spec || CAST(:extra AS JSONB) "
                       "WHERE id = :v"),
                  {"v": version["id"],
                   "extra": json.dumps({"input_schema": spec["input_schema"],
                                        "objective": spec["objective"],
                                        "workflow_name": spec["workflow_name"],
                                        **({"more_tests": spec["more_tests"]}
                                           if "more_tests" in spec else {})})})
        # WAITING FOR CI: the pull request's checks pass (a finished wait, as ci_gate records).
        def _ci_passed(_c, _p, _ctx, _inputs):
            wait_id = f"ciw_{uuid.uuid4().hex[:10]}"
            _insert_awaited(c, project, "ci_waits", wait_id, "passed")
            return {"capability": "wait_for_ci", "tool": None, "status": "awaiting",
                    "await": {"kind": "ci_wait", "id": wait_id}, "ci_wait_id": wait_id}
        monkeypatch.setattr(runner, "_start_ci_wait", _ci_passed)
        # SCAN FOR BUSINESS RULES: the scan is a fake that reports it read the scope and
        # proposed nothing, so the review gate steps aside (rule_scan.has_open_proposals).
        from boss_engine.services.knowledge import rule_scan

        # The scan is stubbed below, and so is its start check (workflows.START_INPUT_CHECKS
        # resolves the scope against the code graph, which this harness does not build).
        monkeypatch.setattr(rule_scan, "validate_scope",
                            lambda _c, _p, scope, **_k: list(scope or []))

        monkeypatch.setattr(rule_scan, "run_step", lambda *_a, **_k: {
            "capability": "scan_business_rules", "tool": None, "status": "succeeded",
            "scan_id": f"rscan_{uuid.uuid4().hex[:10]}", "files_read": 3, "rules_proposed": 0,
            "spend_usd": 0.0, "stopped_by": "scope_exhausted", "report": {},
            "review_link": {}})
        # ONLY WHAT START WOULD REQUIRE — the form's required fields and nothing else.
        brief = {"title": TITLE, "directive": wc.example_for(key)["directive"],
                 "notify_channel": "mattermost:announcements", "app_key": "swap-board",
                 "scope": ["pkg:owner/swap-board:core:com.example.swaps"]}
        if "incident_id" in spec["input_schema"]["required"]:
            brief["incident_id"] = _incident(c, project, monkeypatch)
        run_input = {name: brief[name] for name in spec["input_schema"]["required"]}
        run = _start(c, project, version["id"], run_input)
        _drive(eng, c, project, run["id"])
        status = c.execute(text("SELECT status FROM workflow_runs WHERE id = :r"),
                           {"r": run["id"]}).scalar()
        steps = _steps(c, run["id"])
    # SUCCEEDED, every one. A `skipped` row here would be a step the run never reached
    # (`path_not_reached`), which is the silent failure this test exists to catch.
    unfinished = {sid: (s["status"], s.get("last_error")) for sid, s in steps.items()
                  if s["status"] != "succeeded"
                  # The branch a condition did not take, by design (Incremental Build: every
                  # increment is in the plan, so no "not in the plan" step runs).
                  and not (s["status"] == "skipped" and sid.endswith("_not_planned"))}
    assert status == "completed", (key, status, unfinished)
    assert not unfinished, (key, unfinished)
    assert set(steps) == {s["id"] for s in entry["steps"]}, key
    # A notify with nobody to tell says so instead of claiming it told someone.
    for step in entry["steps"]:
        if (step.get("binding") or {}).get("capability") == "notify":
            out = steps[step["id"]]["output"]
            if "notify_channel" in spec["input_schema"]["required"]:
                assert out["posted"] is True, key
            else:
                assert out["status"] == "not_applicable" and out["posted"] is False, key
    # Every step that commissions work actually commissioned it.
    called = {name for name, tool in fakes.items() if tool.calls}
    for step in entry["steps"]:
        capability = (step.get("binding") or {}).get("capability")
        expected = {"research": "create_discovery", "meeting": "request_meeting",
                    "build": "create_code_task", "deploy": "deploy_app"}.get(capability)
        if expected:
            assert expected in called, (key, step["id"])
    # A workflow whose input requires a channel told it; one that did not was skipped visibly.
    if "notify_channel" in spec["input_schema"]["required"]:
        assert posted and posted[0][0] == "mattermost:announcements", key
