"""Red CI on a BOSS PR, a vulnerable dependency and a cost jump each reach the one signal inbox
(BOSSAPP-903; design §5 classes 1, 3 and 14).

Each adapter reads its real store: a failed CI wait on a PR BOSS opened (ci_waits), an open OSV
finding from the security scan (security_findings), an open cost anomaly (cost_anomalies) and a
day's spend far above its trailing mean (cost_receipts). Each becomes one signal with the right
source, kind, subject and checked evidence; a re-sweep adds nothing; the same check failing again
on the same PR is the same problem; the security adapter reads only what the scan found, so an
offline install with no scan results records nothing and errors nothing.
"""
from __future__ import annotations

from uuid import uuid4

from sqlalchemy import text

from boss_engine.services.ambient import adapters, signals

from ._ambient_support import eng, project  # noqa: F401

PR = "https://github.com/acme/shop/pull/41"


def _ci_wait(c, pid, *, checks, status="failed"):
    wid = f"ciw_{uuid4().hex[:10]}"
    c.execute(text(
        "INSERT INTO ci_waits (id, project_id, repository, head_sha, pull_request_url, "
        "code_task_run_id, status, summary, checks, failing_url, timeout_seconds, deadline_at, "
        "finished_at) VALUES (:i, :p, 'acme/shop', 'abc1234def5678', :u, NULL, :s, "
        "'CI failed: unit-tests failure', CAST(:ch AS JSONB), "
        "'https://github.com/acme/shop/actions/runs/1', 900, now() + interval '1 hour', now())"),
        {"i": wid, "p": pid, "u": PR, "s": status, "ch": __import__("json").dumps(checks)})
    return wid


def _sweep(c, pid, only):
    return signals.sweep(c, project_id=pid, only=[only])[only]


def _mine(c, pid, kind):
    return [s for s in signals.list_signals(c, project_id=pid, include_install_wide=False)
            if s["kind"] == kind]


FAILED = [{"name": "unit-tests", "kind": "check_run", "status": "completed",
           "conclusion": "failure", "url": None},
          {"name": "lint", "kind": "check_run", "status": "completed", "conclusion": "success",
           "url": None}]


def test_a_failed_ci_wait_on_a_boss_pr_is_one_ci_signal_about_that_pr(eng, project):
    with eng.begin() as c:
        wid = _ci_wait(c, project, checks=FAILED)
        _ci_wait(c, project, checks=FAILED, status="passed")
        report = _sweep(c, project, "ci_checks")
        assert report["error"] is None and report["outcomes"] == {"created": 1}
        [sig] = _mine(c, project, "ci.failed")
        assert sig["source"] == "ci" and sig["subject_id"] == "pr:acme/shop#41"
        assert sig["severity"] == "high" and "unit-tests" in sig["title"]
        assert "lint" not in sig["title"], "only the failed checks name the problem"
        assert {e["kind"] for e in sig["evidence"]} == {"ci_wait", "url"}
        assert sig["evidence"][0]["ref"] == wid
        # Swept again: nothing new. The same check failing again: one more occurrence.
        assert _sweep(c, project, "ci_checks")["outcomes"] == {"duplicate": 1}
        _ci_wait(c, project, checks=FAILED)
        assert _sweep(c, project, "ci_checks")["outcomes"] == {"duplicate": 1, "repeat": 1}
        assert _mine(c, project, "ci.failed")[0]["occurrences"] == 2


def test_a_different_check_failing_on_the_same_pr_is_a_different_problem(eng, project):
    with eng.begin() as c:
        _ci_wait(c, project, checks=FAILED)
        _ci_wait(c, project, checks=[{"name": "e2e", "status": "completed",
                                      "conclusion": "timed_out"}])
        _sweep(c, project, "ci_checks")
        assert len(_mine(c, project, "ci.failed")) == 2


def _osv_finding(c, pid, *, package="lodash", version="4.17.20", advisory="GHSA-35jh-r3h4-6jhm",
                 severity="high", status="open"):
    scan = f"scan_{uuid4().hex[:8]}"
    fid = f"sf_{uuid4().hex[:8]}"
    c.execute(text("INSERT INTO security_scans (id, project_id, app_key, status, started_at, "
                   "ended_at) VALUES (:i, :p, 'web', 'done', now(), now())"),
              {"i": scan, "p": pid})
    c.execute(text(
        "INSERT INTO security_findings (id, scan_id, fingerprint, tool, rule_id, title, severity, "
        "location, evidence, status) VALUES (:i, :s, :f, 'osv', :r, 'Prototype pollution', :sev, "
        "CAST(:loc AS JSONB), CAST(:ev AS JSONB), :st)"),
        {"i": fid, "s": scan, "f": uuid4().hex, "r": advisory, "sev": severity, "st": status,
         "loc": f'{{"package": "{package}", "version": "{version}", "lockfile": '
                f'"package-lock.json"}}',
         "ev": f'{{"advisory": "https://osv.dev/vulnerability/{advisory}"}}'})
    return fid


def test_an_open_osv_finding_is_a_security_signal_about_the_package(eng, project):
    with eng.begin() as c:
        fid = _osv_finding(c, project)
        _osv_finding(c, project, package="minimist", status="fixed")
        report = _sweep(c, project, "dependency_advisories")
        assert report["error"] is None and report["outcomes"] == {"created": 1}
        [sig] = _mine(c, project, "osv.vulnerable_dependency")
        assert sig["source"] == "osv" and sig["subject_id"] == "lodash" and sig["app"] == "web"
        assert sig["title"] == "lodash 4.17.20 has GHSA-35jh-r3h4-6jhm"
        assert sig["evidence"][0] == {"kind": "security_finding", "ref": fid,
                                      "note": "lodash@4.17.20 GHSA-35jh-r3h4-6jhm"}
        # The nightly scan finds it again: the same problem, counted.
        _osv_finding(c, project)
        _sweep(c, project, "dependency_advisories")
        [again] = _mine(c, project, "osv.vulnerable_dependency")
        assert again["occurrences"] == 2


def test_with_no_scan_results_the_security_adapter_records_nothing_and_errors_nothing(
        eng, project):
    with eng.begin() as c:
        report = _sweep(c, project, "dependency_advisories")
        assert report == {**report, "error": None, "outcomes": {}}


def test_an_open_cost_anomaly_is_a_cost_signal(eng, project):
    with eng.begin() as c:
        aid = f"ca_{uuid4().hex[:8]}"
        c.execute(text(
            "INSERT INTO cost_anomalies (id, project_id, provider, anomaly_type, severity, "
            "amount_usd_micros, threshold_usd_micros, summary) VALUES (:i, :p, 'openai', "
            "'retry_storm', 'high', 42000000, 5000000, 'Retry storm on openai: $42 in an hour')"),
            {"i": aid, "p": project})
        assert _sweep(c, project, "cost_anomalies")["outcomes"] == {"created": 1}
        [sig] = _mine(c, project, "cost.anomaly")
        assert sig["source"] == "cost" and sig["severity"] == "high"
        assert sig["evidence"][0]["ref"] == aid and "$42.00 vs $5.00" in sig["evidence"][0]["note"]


def _spend(c, pid, usd, *, days_ago=0.0, category="llm", included=False):
    c.execute(text(
        "INSERT INTO cost_receipts (id, project_id, category, skill_or_model_id, "
        "estimated_usd_micros, actual_usd_micros, status, created_at, included_in_subscription) "
        "VALUES (:i, :p, :c, 'gpt', :m, :m, 'settled', now() - make_interval(secs => :s), :inc)"),
        {"i": f"cr_{uuid4().hex[:10]}", "p": pid, "c": category, "m": int(usd * 1e6),
         "s": int(days_ago * 86400), "inc": included})


def test_a_day_of_spend_far_above_the_weekly_mean_is_one_spike_signal_a_day(eng, project):
    with eng.begin() as c:
        for day in range(1, 8):
            _spend(c, project, 0.50, days_ago=day + 0.1)
        _spend(c, project, 9.00, days_ago=0.1)
        _spend(c, project, 50.00, days_ago=0.1, included=True)   # subscription: $0
        assert _sweep(c, project, "spend_spikes")["outcomes"] == {"created": 1}
        [sig] = _mine(c, project, "cost.spend_spike")
        assert sig["title"].startswith("Spend $9.00 in 24h, 18×")
        assert sig["subject_id"] == "llm" and sig["severity"] == "high"
        assert _sweep(c, project, "spend_spikes")["outcomes"] == {"duplicate": 1}


def test_ordinary_spend_is_not_a_spike(eng, project):
    with eng.begin() as c:
        for day in range(0, 8):
            _spend(c, project, 2.00, days_ago=day + 0.1)
        assert _sweep(c, project, "spend_spikes")["outcomes"] == {}


def test_the_new_adapters_are_registered_with_the_sweep():
    assert {"ci_checks", "dependency_advisories", "cost_anomalies", "spend_spikes"} <= set(
        adapters.register())
    assert adapters.pr_ref(PR) == "pr:acme/shop#41"
