"""The security_review workflow (APP_TEMPLATES_AND_SECURITY_LOOP.md §4.2, §4.9 "Workflow").

Two halves:

* the RULES, pure: the re-review passes only when every targeted finding is gone from the re-scan
  (fixed, or removed with its code) or justified by a person, and no new high or critical
  appeared; the Panel's answer is read only where it cites this scan's findings; nothing found is
  dropped (a finding the Panel left out gets a derived recommendation);
* the WIRING, through the real runner with the scanner, the panel and the build replaced by fakes
  that land finished rows where the runner waits: scan -> panel -> decide (a person's choice) ->
  fix -> rescan -> rereview, the second round when changes are asked, and the report.
"""
from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import text

from boss_engine.security_scan import review
from boss_engine.services import security_review_steps
from boss_engine.services import workflow_capability_runner as runner
from boss_engine.services import workflow_catalogue as wc

from .test_every_template_runs_end_to_end import FakeTool, Registry, _tidy  # noqa: F401
from .test_workflow_capability_runner import Ctx, _publish, _start, _steps, eng, project  # noqa: F401

SQLI, JWT, LODASH, CSP = "a" * 64, "b" * 64, "c" * 64, "d" * 64


@pytest.fixture(autouse=True)
def _findings_first(request):
    """Findings name their scan, not the project: removed before `_tidy` removes the scans."""
    if "eng" not in request.fixturenames:
        yield
        return
    request.getfixturevalue("_tidy")
    eng_, project_ = request.getfixturevalue("eng"), request.getfixturevalue("project")
    yield
    with eng_.begin() as c:
        c.execute(text("DELETE FROM security_findings WHERE scan_id IN "
                       "(SELECT id FROM security_scans WHERE project_id = :p)"), {"p": project_})
        c.execute(text("DELETE FROM security_recommendations WHERE project_id = :p"), {"p": project_})
NEW_HIGH, NEW_LOW = "e" * 64, "f" * 64


# ---- the rules (no database) -------------------------------------------------------------------

def _f(fp: str, title: str = "t", severity: str = "high", tool: str = "semgrep") -> dict:
    return {"fingerprint": fp, "title": title, "severity": severity, "tool": tool, "where": "app.js (f)",
            "rule_id": f"rule.{title}", "id": f"sf_{fp[:6]}", "status": "open"}


def _report(**buckets) -> dict:
    base = {k: [] for k in ("fixed", "still_open", "moved", "removed", "not_rescanned", "new")}
    base.update(buckets)
    base["rules_differ"] = False
    return base


def test_the_rereview_passes_when_every_target_is_gone():
    v = review.rereview_verdict(_report(fixed=[_f(SQLI), _f(JWT)], still_open=[_f(CSP, severity="low")]),
                                [SQLI, JWT])
    assert v["passed"] and v["fixed"] == 2 and not v["reasons"]


@pytest.mark.parametrize("bucket", ["still_open", "moved", "not_rescanned"])
def test_a_target_that_is_still_found_moved_or_unscanned_fails(bucket):
    v = review.rereview_verdict(_report(fixed=[_f(SQLI)], **{bucket: [_f(JWT)]}), [SQLI, JWT])
    assert not v["passed"]
    assert [r["outcome"] for r in v["failing"]] == [bucket]


def test_a_target_missing_from_the_report_is_not_counted_as_gone():
    assert not review.rereview_verdict(_report(fixed=[_f(SQLI)]), [SQLI, JWT])["passed"]


def test_removed_with_its_code_is_gone_and_a_person_s_justification_counts():
    v = review.rereview_verdict(_report(removed=[_f(SQLI)], still_open=[_f(JWT)]), [SQLI, JWT],
                                justified={JWT})
    assert v["passed"] and v["removed"] == 1 and v["justified"] == 1


def test_a_justification_does_not_excuse_a_moved_finding():
    assert not review.rereview_verdict(_report(moved=[_f(SQLI)]), [SQLI], justified={SQLI})["passed"]


@pytest.mark.parametrize("severity,passes", [("critical", False), ("high", False), ("medium", True),
                                             ("low", True), ("info", True)])
def test_a_new_finding_fails_the_rereview_only_when_high_or_critical(severity, passes):
    v = review.rereview_verdict(_report(fixed=[_f(SQLI)], new=[_f(NEW_HIGH, "new", severity)]), [SQLI])
    assert v["passed"] is passes
    assert bool(v["new_blocking"]) is (not passes)


def test_nothing_targeted_is_not_a_pass():
    assert not review.rereview_verdict(_report(), [])["passed"]


FINDINGS = [
    {**_f(SQLI, "SQL injection in findUser", "high"), "location": {"file": "app.js", "function": "findUser"}},
    {**_f(JWT, "Hardcoded JWT secret", "medium"), "location": {"file": "app.js", "function": "issueToken"}},
    {**_f(LODASH, "Prototype pollution in lodash", "high", "osv"),
     "location": {"package": "lodash", "version": "4.17.20", "lockfile": "package-lock.json"}},
    {**_f(CSP, "CSP header not set", "medium", "zap"), "location": {"method": "GET", "path": "/"}},
]

PANEL_MD = """# Security review
Findings argued.

```security_recommendations
[{"title": "Parameterise the user lookup", "refs": ["%s", "%s"], "fix": "prepared statement",
  "priority": "P0", "effort": "S", "risk_if_unfixed": "all accounts readable"},
 {"title": "Ignore this", "refs": ["%s"], "priority": "P9", "false_positive": "the test fixture"},
 {"title": "Cites nothing real", "refs": ["0000000000000000"], "priority": "P1"}]
```
""" % (SQLI[:12], "9" * 12, JWT[:12])


def test_the_panel_is_read_only_where_it_cites_this_scan():
    recs = review.parse_panel_recommendations(PANEL_MD, [f["fingerprint"] for f in FINDINGS])
    assert [r["title"] for r in recs] == ["Parameterise the user lookup", "Ignore this"]
    assert recs[0]["fingerprints"] == [SQLI]           # the unknown ref is dropped
    assert recs[1]["priority"] is None                  # P9 is not a priority
    assert recs[1]["false_positive_reason"] == "the test fixture"


def test_nothing_found_is_dropped_and_priorities_follow_severity():
    merged = review.merge_recommendations(
        review.parse_panel_recommendations(PANEL_MD, [f["fingerprint"] for f in FINDINGS]), FINDINGS)
    covered = {fp for r in merged for fp in r["fingerprints"]}
    assert covered == {f["fingerprint"] for f in FINDINGS}
    by_title = {r["title"]: r for r in merged}
    assert by_title["Ignore this"]["priority"] == "P2"          # its finding is medium
    assert by_title["Upgrade lodash (1 advisory)"]["source"] == "derived"
    assert by_title["Upgrade lodash (1 advisory)"]["priority"] == "P1"


def test_the_digest_is_enveloped_and_cites_short_refs():
    digest = review.findings_digest(FINDINGS, app_key="vuln", commit="abc123")
    assert digest.startswith(review.UNTRUSTED_OPEN) and digest.endswith(review.UNTRUSTED_CLOSE)
    assert f"ref {SQLI[:12]}" in digest and SQLI not in digest


def test_the_fix_brief_names_every_target_and_round_two_names_what_is_left():
    recs = [{"title": "Parameterise", "priority": "P0", "fix": "prepared", "fingerprints": [SQLI]}]
    brief, criteria = review.fix_brief(recs, {f["fingerprint"]: f for f in FINDINGS}, app_key="vuln")
    assert "SQL injection in findUser" in brief and "findUser" in brief
    assert criteria[-1] == "No new high or critical finding in the re-scan"
    brief2, _ = review.fix_brief(recs, {}, app_key="vuln",
                                 remaining=[{**_f(SQLI, "SQL injection in findUser"), "outcome": "still_open"}])
    assert "Still found by the re-scan" in brief2


def test_the_template_chains_the_loop_and_validates():
    entry = next(e for e in wc.CATALOGUE if e["key"] == "security_review")
    ids = [s["id"] for s in entry["steps"]]
    assert ids == ["scan", "panel", "decide", "fix", "rescan", "rereview", "fix_2", "rescan_2",
                   "rereview_2", "report"]
    caps = {s["id"]: s["binding"]["capability"] for s in entry["steps"]}
    assert caps["decide"] == "security_decide" and caps["rereview_2"] == "security_rereview"
    final = {s["id"]: s["binding"]["inputs"].get("final") for s in entry["steps"] if "rereview" in s["id"]}
    assert final == {"rereview": False, "rereview_2": True}
    assert wc.entry_spec(entry)["input_schema"]["required"] == ["app_key"]
    summary = next(s for s in wc.catalogue_summary(True) if s["key"] == "security_review")
    assert summary["gates"] == ["decide"]


# ---- the wiring (the real runner, fakes for the tools) -----------------------------------------

APP = "vulnapp"


def _scan_row(c, project_id: str, findings: list[dict], *, failed_tools=()) -> str:
    scan_id = f"scan_{uuid.uuid4().hex[:10]}"
    tools = [{"name": t, "status": "failed" if t in failed_tools else "done", "version": "1",
              "rules_digest": "sha256:x"} for t in ("semgrep", "osv", "zap")]
    c.execute(text(
        "INSERT INTO security_scans (id, project_id, app_key, commit_sha, status, tools, counts, target, "
        "started_at, ended_at) VALUES (:i, :p, :a, :sha, 'done', CAST(:t AS JSONB), CAST(:c AS JSONB), "
        "CAST(:tg AS JSONB), clock_timestamp(), clock_timestamp())"),
        {"i": scan_id, "p": project_id, "a": APP, "sha": uuid.uuid4().hex[:12], "t": json.dumps(tools),
         "c": json.dumps({"total": len(findings)}),
         "tg": json.dumps({"tools": ["semgrep", "osv", "zap"], "zap": {"mode": "baseline"},
                           "inventory": {"files": ["app.js", "package-lock.json"], "routes": ["/"]}})})
    for f in findings:
        c.execute(text(
            "INSERT INTO security_findings (id, scan_id, fingerprint, tool, rule_id, title, severity, "
            "location, evidence) VALUES (:i, :s, :fp, :tool, :r, :t, :sev, CAST(:loc AS JSONB), '{}'::jsonb)"),
            {"i": f"sf_{uuid.uuid4().hex[:10]}", "s": scan_id, "fp": f["fingerprint"], "tool": f["tool"],
             "r": f["rule_id"], "t": f["title"], "sev": f["severity"], "loc": json.dumps(f["location"])})
    return scan_id


class World:
    """The fakes: each scan the workflow asks for lands the next prepared set of findings."""

    def __init__(self, c, project_id: str, scans: list[list[dict]], panel_md: str = PANEL_MD):
        self.c, self.project_id = c, project_id
        self.scans = list(scans)
        self.panel_md = panel_md
        self.app_run = f"crun_{uuid.uuid4().hex[:10]}"
        c.execute(text("INSERT INTO code_task_runs (id, project_id, title, status, spec, finalize_ctx) "
                       "VALUES (:i, :p, 'vulnapp', 'completed', '{}'::jsonb, CAST(:f AS JSONB))"),
                  {"i": self.app_run, "p": project_id, "f": json.dumps({"app_id": APP})})
        self.tools = {name: FakeTool(name, fn) for name, fn in (
            ("run_security_scan", self.scan), ("request_meeting", self.meeting),
            ("create_code_task", self.build))}

    def scan(self, pid, args):
        return {"status": "queued", "scan_id": _scan_row(self.c, pid, self.scans.pop(0))}

    def meeting(self, pid, args):
        run_id = f"mrun_{uuid.uuid4().hex[:10]}"
        from .test_a_failed_step_stops_the_run import _insert_awaited

        _insert_awaited(self.c, pid, "meeting_runs", run_id, "completed")
        self.c.execute(text("UPDATE meeting_runs SET state = CAST(:s AS JSONB) WHERE id = :i"),
                       {"i": run_id, "s": json.dumps({"prd_markdown": self.panel_md})})
        return {"status": "running", "meeting": {"run_id": run_id}}

    def build(self, pid, args):
        run_id = f"crun_{uuid.uuid4().hex[:10]}"
        self.c.execute(text("INSERT INTO code_task_runs (id, project_id, title, status, spec, finalize_ctx) "
                            "VALUES (:i, :p, :t, 'completed', '{}'::jsonb, CAST(:f AS JSONB))"),
                       {"i": run_id, "p": pid, "t": args.get("title"), "f": json.dumps({"app_id": APP})})
        return {"status": "queued", "run_id": run_id}


def _run(eng, c, project_id: str, world: World, *, choose=None, ticks: int = 40) -> str:
    entry = next(e for e in wc.CATALOGUE if e["key"] == "security_review")
    spec = wc.entry_spec(entry)
    _d, version = _publish(c, project_id, "security_review", spec["steps"], spec["entry_step"])
    run = _start(c, project_id, version["id"], {"app_key": APP, "directive": "look at login"})
    for _ in range(ticks):
        c.execute(text("UPDATE workflow_step_runs SET run_after = now() - interval '1 second' "
                       "WHERE workflow_run_id = :r AND run_after > now()"), {"r": run["id"]})
        waiting = c.execute(text(
            "SELECT decision_id FROM workflow_step_runs WHERE workflow_run_id = :r AND step_id = 'decide' "
            "AND status = 'waiting' AND decision_id IS NOT NULL"), {"r": run["id"]}).scalar()
        if waiting:
            if choose is not None:
                recs = review.list_recommendations(c, run["id"])
                review.record_selection(c, project_id=project_id, run_id=run["id"],
                                        selected=[r["id"] for r in recs if choose(r)], actor="ceo")
            c.execute(text("UPDATE decisions SET status = 'approved', approved_by = 'ceo', approved_at = now() "
                           "WHERE id = :d AND status NOT IN ('approved', 'rejected')"), {"d": waiting})
        runner.tick(c, Ctx(eng), project_id=project_id)
        status = c.execute(text("SELECT status FROM workflow_runs WHERE id = :r"), {"r": run["id"]}).scalar()
        if status not in ("running", "validating", "waiting"):
            break
    return run["id"]


def _status(c, run_id):
    return c.execute(text("SELECT status FROM workflow_runs WHERE id = :r"), {"r": run_id}).scalar()


AFTER_CLEAN = [f for f in FINDINGS if f["fingerprint"] in (JWT, CSP)]        # SQLi + lodash fixed


def test_scan_panel_choice_fix_rescan_rereview_report(eng, project, monkeypatch):
    with eng.begin() as c:
        world = World(c, project, [FINDINGS, AFTER_CLEAN])
        monkeypatch.setattr(runner, "_tool_registry", lambda: Registry(list(world.tools.values())))
        # The person ticks the Panel's P0 and the derived lodash upgrade, nothing else.
        run_id = _run(eng, c, project, world,
                      choose=lambda r: r["title"] in ("Parameterise the user lookup", "Upgrade lodash (1 advisory)"))
        steps = _steps(c, run_id)
        assert _status(c, run_id) == "completed", {k: (v["status"], v["last_error"]) for k, v in steps.items()}
        assert steps["rereview"]["output"]["passed"] is True
        for later in ("fix_2", "rescan_2", "rereview_2"):
            assert steps[later]["output"]["status"] == "not_applicable", later
        # The panel sat with the security seats, as a security review (no PRD to publish).
        meeting_args = world.tools["request_meeting"].calls[0]
        assert meeting_args["seats"][0] == "security" and meeting_args["purpose"] == "security_review"
        assert "look at login" in meeting_args["directive"]
        # The fix is the app's next build, briefed with exactly the chosen findings.
        build = world.tools["create_code_task"].calls[0]
        assert build["extends_run_id"] == world.app_run
        assert "SQL injection in findUser" in build["instructions"] and "lodash" in build["instructions"]
        assert "Hardcoded JWT secret" not in build["instructions"]
        # The re-scan used the first scan's tools.
        assert world.tools["run_security_scan"].calls[1]["tools"] == ["semgrep", "osv", "zap"]
        report = c.execute(text("SELECT content FROM artifacts WHERE id = :a"),
                           {"a": steps["report"]["output"]["artifact_id"]}).scalar()
        assert report["passed"] is True and report["verdict"]["fixed"] == 2
        assert "2 of 2 targeted findings gone" in report["summary"]
        state = security_review_steps.review_state(c, run_id)
        assert state["report_artifact_id"] == steps["report"]["output"]["artifact_id"]
        assert [r["selected"] for r in state["recommendations"]].count(True) == 2


def test_changes_asked_runs_round_two_on_what_is_left(eng, project, monkeypatch):
    still = [f for f in FINDINGS if f["fingerprint"] != LODASH]               # SQLi survived round 1
    with eng.begin() as c:
        world = World(c, project, [FINDINGS, still, AFTER_CLEAN])
        monkeypatch.setattr(runner, "_tool_registry", lambda: Registry(list(world.tools.values())))
        run_id = _run(eng, c, project, world,
                      choose=lambda r: r["title"] in ("Parameterise the user lookup", "Upgrade lodash (1 advisory)"))
        steps = _steps(c, run_id)
        assert _status(c, run_id) == "completed", {k: (v["status"], v["last_error"]) for k, v in steps.items()}
        assert steps["rereview"]["output"]["passed"] is False
        assert steps["rereview"]["output"]["status"] == "changes_asked"
        assert steps["rereview_2"]["output"]["passed"] is True
        round2 = world.tools["create_code_task"].calls[1]
        assert "Still found by the re-scan" in round2["instructions"] and "(round 2)" in round2["title"]


def test_round_two_is_the_last_and_a_failed_rereview_stops_the_run(eng, project, monkeypatch):
    with eng.begin() as c:
        world = World(c, project, [FINDINGS, FINDINGS, FINDINGS])
        monkeypatch.setattr(runner, "_tool_registry", lambda: Registry(list(world.tools.values())))
        run_id = _run(eng, c, project, world, choose=lambda r: r["priority"] == "P0")
        steps = _steps(c, run_id)
        assert steps["rereview_2"]["status"] in ("failed", "deadlettered")
        assert "did not pass after round 2" in steps["rereview_2"]["last_error"]
        assert steps["report"]["status"] == "pending"
        assert _status(c, run_id) in ("failed", "deadlettered", "blocked")


def test_a_new_high_finding_fails_the_round_even_when_targets_are_gone(eng, project, monkeypatch):
    new_high = {**_f(NEW_HIGH, "Command injection", "high"), "location": {"file": "admin.js", "function": "run"}}
    with eng.begin() as c:
        world = World(c, project, [FINDINGS, AFTER_CLEAN + [new_high], AFTER_CLEAN])
        monkeypatch.setattr(runner, "_tool_registry", lambda: Registry(list(world.tools.values())))
        run_id = _run(eng, c, project, world,
                      choose=lambda r: r["title"] in ("Parameterise the user lookup", "Upgrade lodash (1 advisory)"))
        steps = _steps(c, run_id)
        first = steps["rereview"]["output"]
        assert first["passed"] is False and first["new_blocking"][0]["title"] == "Command injection"
        assert "NEW high" in world.tools["create_code_task"].calls[1]["instructions"]
        assert steps["rereview_2"]["output"]["passed"] is True


def test_approving_without_the_checklist_fixes_the_pre_ticked_p0_and_p1(eng, project, monkeypatch):
    with eng.begin() as c:
        world = World(c, project, [FINDINGS, AFTER_CLEAN])
        monkeypatch.setattr(runner, "_tool_registry", lambda: Registry(list(world.tools.values())))
        run_id = _run(eng, c, project, world, choose=None)
        chosen = {r["title"] for r in review.chosen(c, run_id)}
        assert chosen == {"Parameterise the user lookup", "Upgrade lodash (1 advisory)"}
        assert _status(c, run_id) == "completed"


def test_a_clean_scan_needs_no_panel_choice_or_fix(eng, project, monkeypatch):
    with eng.begin() as c:
        world = World(c, project, [[]])
        monkeypatch.setattr(runner, "_tool_registry", lambda: Registry(list(world.tools.values())))
        run_id = _run(eng, c, project, world)
        steps = _steps(c, run_id)
        assert _status(c, run_id) == "completed"
        for sid in ("panel", "decide", "fix", "rescan", "rereview"):
            assert steps[sid]["output"]["status"] == "not_applicable", sid
        assert not world.tools["request_meeting"].calls and not world.tools["create_code_task"].calls


def test_the_choice_is_refused_once_the_fix_has_started(eng, project, monkeypatch):
    with eng.begin() as c:
        world = World(c, project, [FINDINGS, AFTER_CLEAN])
        monkeypatch.setattr(runner, "_tool_registry", lambda: Registry(list(world.tools.values())))
        run_id = _run(eng, c, project, world, choose=lambda r: r["priority"] == "P0")
        with pytest.raises(review.ReviewRefused) as refused:
            review.record_selection(c, project_id=project, run_id=run_id, selected=[], actor="ceo")
        assert refused.value.code == "already_decided"
        with pytest.raises(review.ReviewRefused) as other:
            review.record_selection(c, project_id="p_other", run_id=run_id, selected=[], actor="ceo")
        assert other.value.status == 404
