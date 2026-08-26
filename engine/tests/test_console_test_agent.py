"""The console test agent — including the tests that keep it honest.

The agent's job is to stop the operator being the test harness. That only works if the agent
itself cannot quietly pass everything, so a good half of this file is about its failure modes
rather than its successes.
"""

import pytest

from boss_engine.services import console_test_agent as cta
from boss_engine.services.console_test_agent import (
    BROKEN, MISSING, MOVED, OK, Baseline, Control, Request, compare, summarise,
)


def _c(area, label, reqs, **kw):
    return Control(area=area, label=label,
                   requests=[Request(m, p) for m, p in reqs], **kw)


def _bl(console, controls):
    return Baseline(console=console, captured_at="2026-08-26T00:00:00Z", controls=controls)


# --------------------------------------------------------------------------- #
# What it must catch
# --------------------------------------------------------------------------- #
def test_a_control_that_did_not_survive_the_migration_is_MISSING():
    """The Unbind case: v1 could do something, v2 has no equivalent at all."""
    v1 = _bl("v1", [_c("code_tasks", "Unbind target",
                       [("DELETE", "/v1/deployment-targets/deptgt_1")])])
    v2 = _bl("v2", [])
    f = compare(v1, v2)[0]
    assert f.verdict == MISSING
    assert f.is_failure()


def test_a_control_that_now_does_something_else_is_BROKEN():
    """The dangerous case: it LOOKS migrated. Same label, different behaviour — which is
    exactly how "deploy Living Colors" shipped Invoice-Chaser and reported success."""
    v1 = _bl("v1", [_c("code_tasks", "Deploy now", [("POST", "/v1/deployments")])])
    v2 = _bl("v2", [_c("missions", "Deploy now", [("POST", "/v1/code-tasks/retry")])])
    f = compare(v1, v2)[0]
    assert f.verdict == BROKEN
    assert f.is_failure()
    assert "different requests" in f.detail


def test_relocation_is_reported_but_is_not_a_failure():
    """v2 deliberately moves nearly everything. If moving counted as breaking, the report
    would be all noise and would stop being read."""
    v1 = _bl("v1", [_c("decisions", "Approve", [("POST", "/v1/decisions/d1/approve")])])
    v2 = _bl("v2", [_c("panels", "Approve", [("POST", "/v1/decisions/d1/approve")])])
    f = compare(v1, v2)[0]
    assert f.verdict == MOVED
    assert not f.is_failure()


def test_matching_is_on_behaviour_not_on_label():
    """v2 renames things. A rename must not read as a loss."""
    v1 = _bl("v1", [_c("code_tasks", "Set target", [("POST", "/v1/code-tasks/x/setup-deployment")])])
    v2 = _bl("v2", [_c("missions", "Bind host", [("POST", "/v1/code-tasks/x/setup-deployment")])])
    assert compare(v1, v2)[0].verdict in (OK, MOVED)


def test_query_strings_do_not_defeat_matching():
    """Ids and cache-busters differ per run; the route is what matters."""
    v1 = _bl("v1", [_c("a", "X", [("GET", "/v1/deployment-hosts?project_id=p&cb=1")])])
    v2 = _bl("v2", [_c("a", "X", [("GET", "/v1/deployment-hosts?project_id=p&cb=999")])])
    assert compare(v1, v2)[0].verdict == OK


# --------------------------------------------------------------------------- #
# How it must not lie
# --------------------------------------------------------------------------- #
def test_an_unverifiable_control_is_not_counted_as_passing():
    """A control that issues no request cannot be checked this way. Marking it OK silently
    would be the agent inventing confidence it does not have."""
    v1 = _bl("v1", [_c("a", "Expand section", [])])
    f = compare(v1, _bl("v2", []))[0]
    assert "not verifiable" in f.detail


def test_summary_fails_when_anything_is_missing_or_broken():
    v1 = _bl("v1", [
        _c("a", "Gone", [("DELETE", "/v1/x")]),
        _c("a", "Fine", [("GET", "/v1/y")]),
    ])
    v2 = _bl("v2", [_c("b", "Fine", [("GET", "/v1/y")])])
    s = summarise(compare(v1, v2))
    assert s["passed"] is False
    assert s["counts"][MISSING] == 1
    assert len(s["failures"]) == 1


def test_the_model_never_decides_pass_or_fail():
    """The one rule that keeps this suite worth running. A model asked "did it work?" will
    eventually say yes, because yes is the agreeable answer — and an agreeable test suite is
    worse than none: it turns unknown risk into false confidence."""
    from engine.tests._source import code_only
    src = code_only(cta)
    for forbidden in ("openai", "anthropic", "llm", "complete(", "chat("):
        assert forbidden not in src.lower(), (
            f"a model call ({forbidden}) reached the comparison path")


def test_a_baseline_survives_a_round_trip():
    """Baselines outlive the migration; a recording that cannot be reloaded months later is
    not a baseline."""
    v1 = _bl("v1", [_c("code_tasks", "Retry", [("POST", "/v1/delivery/deployments/d/redeploy")],
                       testid="app-deployment-retry", note="failed deploy")])
    again = Baseline.from_json(v1.to_json())
    assert again.controls[0].behaviour() == v1.controls[0].behaviour()
    assert again.controls[0].testid == "app-deployment-retry"


# --------------------------------------------------------------------------- #
# The agent's own acceptance test
# --------------------------------------------------------------------------- #
def test_it_knows_which_real_defects_it_must_be_able_to_find():
    ids = {d["id"] for d in cta.KNOWN_V1_DEFECTS}
    assert ids == {"unbind-missing", "audit-export-on-every-page",
                   "set-target-hangs", "wrong-app-deployed"}
    for d in cta.KNOWN_V1_DEFECTS:
        assert d["observable"] and d["detects_with"], f"{d['id']} has no way to be observed"


def test_an_empty_recording_claims_no_coverage():
    """The failure mode that matters: a baseline that recorded nothing must not report that
    everything is fine."""
    cov = cta.defect_coverage(_bl("v1", []))
    assert all(not c["covered"] for c in cov)


def test_a_real_recording_covers_the_known_defects():
    rich = _bl("v1", [
        _c("overview", "load", [("GET", "/v1/audit/export")]),
        _c("code_tasks", "Set target", [("POST", "/v1/code-tasks/x/setup-deployment")],
           state_after={"elapsed_ms": 115000, "host": "hetzner-ci-01"}),
    ])
    cov = {c["id"]: c for c in cta.defect_coverage(rich)}
    assert cov["audit-export-on-every-page"]["covered"], "cannot see a feed fetched needlessly"
    assert cov["set-target-hangs"]["covered"], "cannot see a control that hangs"
    assert cov["unbind-missing"]["covered"], "cannot see a missing control"
    assert cov["wrong-app-deployed"]["covered"], "cannot see the wrong app being acted on"


# --------------------------------------------------------------------------- #
# Telling a slow control apart from a stalled harness
#
# The first real baseline recorded a control at 16.7 MINUTES. The same control, on six other
# areas, took 2,020-2,029ms — and issued no requests at all. Six identical readings and one
# enormous one is far better explained by the harness stalling than by the console hanging, and
# reporting it as a defect would have sent someone hunting a bug that was never there.
# --------------------------------------------------------------------------- #
from boss_engine.services.console_test_agent import (  # noqa: E402
    OUTLIER_FACTOR,
    defect_coverage,
    slow_controls,
)


def _timed(area, label, elapsed_ms, request_ms=0):
    return Control(area=area, label=label,
                   state_after={"elapsed_ms": elapsed_ms, "slowest_request_ms": request_ms})


def _base(*controls):
    return Baseline(console="v1", captured_at="2026-08-26T00:00:00+00:00", controls=list(controls))


def test_a_reading_wildly_out_of_line_with_its_siblings_is_an_outlier():
    """The real case, reproduced: one 16.7-minute reading beside six two-second ones."""
    controls = [_timed(f"area{i}", "Jump to ⌘K", 2_020) for i in range(6)]
    controls.append(_timed("monitoring", "Jump to ⌘K", 1_004_869))
    rows = slow_controls(_base(*controls))
    worst = rows[0]
    assert worst["verdict"] == "outlier"
    assert "stalled harness" in worst["detail"]
    assert "2020ms elsewhere" in worst["detail"]


def test_a_consistently_slow_control_is_a_finding_not_an_outlier():
    """If every reading is slow, the control is slow. That is the thing to fix."""
    rows = slow_controls(_base(
        _timed("a", "Refresh", 9_000, request_ms=8_500),
        _timed("b", "Refresh", 9_200, request_ms=8_700),
    ))
    assert all(r["verdict"] == "finding" for r in rows)


def test_waiting_far_longer_than_the_request_is_the_defect():
    """"Set target" settled in 115s while its own call took under a second. The GAP is the bug,
    and reporting only the total would blame the API."""
    rows = slow_controls(_base(_timed("code_tasks", "Set target", 115_000, request_ms=800)))
    assert rows[0]["verdict"] == "finding"
    assert "while its slowest request took 800ms" in rows[0]["detail"]


def test_a_slow_control_with_no_requests_at_all_is_unexplained_not_a_finding():
    """Nothing was measured, so nothing is proven. Calling it a finding would assert a cause
    the data does not support."""
    rows = slow_controls(_base(_timed("monitoring", "Factory", 4_997)))
    assert rows[0]["verdict"] == "unexplained"
    assert "issued no requests" in rows[0]["detail"]


def test_a_control_with_no_siblings_is_never_called_an_outlier():
    """One reading cannot be out of line with itself. Guessing here would hide a real hang."""
    rows = slow_controls(_base(_timed("a", "Only once", 900_000)))
    assert rows[0]["verdict"] != "outlier"


def test_the_outlier_bar_is_high_enough_that_ordinary_variance_is_not_dismissed():
    """A control that is merely three times slower on one area is a finding, not noise — the
    danger of this whole mechanism is explaining away a real defect."""
    controls = [_timed(f"a{i}", "Open", 1_000, request_ms=900) for i in range(5)]
    controls.append(_timed("slow", "Open", 4_000, request_ms=3_800))
    rows = slow_controls(_base(*controls))
    assert [r["verdict"] for r in rows] == ["finding"]
    assert OUTLIER_FACTOR >= 10


def test_fast_controls_are_not_reported():
    assert slow_controls(_base(_timed("a", "Quick", 120, request_ms=90))) == []


def test_rows_come_back_slowest_first():
    rows = slow_controls(_base(
        _timed("a", "Mid", 5_000, request_ms=4_000),
        _timed("b", "Worst", 20_000, request_ms=19_000),
    ))
    assert [r["label"] for r in rows] == ["Worst", "Mid"]


# --------------------------------------------------------------------------- #
# A fixed defect is not a coverage gap
# --------------------------------------------------------------------------- #
def test_a_defect_that_was_fixed_is_reported_as_such_not_as_a_gap():
    """The baseline cannot observe the audit export any more, and that is the POINT — it was
    gated to the ops area. Reporting it as "not covered, widen the areas" is advice to hunt a
    bug that is not there, which is how a green result becomes busywork."""
    b = _base(_timed("code_tasks", "Set target", 5_000, request_ms=100))
    rows = {r["id"]: r for r in defect_coverage(b)}
    audit = rows["audit-export-on-every-page"]
    assert audit["status"] == "fixed"
    assert audit["covered"] is False
    assert "gated to the ops area" in audit["fixed"]


def test_a_genuine_gap_is_still_a_gap():
    """The marker must not become a way to make any uncovered defect look fine."""
    empty = Baseline(console="v1", captured_at="2026-08-26T00:00:00+00:00", controls=[])
    rows = {r["id"]: r for r in defect_coverage(empty)}
    assert rows["unbind-missing"]["status"] == "not-covered"
    assert rows["set-target-hangs"]["status"] == "not-covered"


def test_covered_beats_fixed():
    """If a baseline CAN still observe it, that is the more useful fact."""
    b = Baseline(
        console="v1", captured_at="2026-08-26T00:00:00+00:00",
        controls=[Control(area="overview", label="(page load)",
                          requests=[Request("GET", "/v1/audit/export", 200)],
                          state_after={"elapsed_ms": 10})],
    )
    rows = {r["id"]: r for r in defect_coverage(b)}
    assert rows["audit-export-on-every-page"]["status"] == "covered"


def test_every_known_defect_reports_a_status():
    b = _base(_timed("a", "x", 100))
    assert all(r["status"] in {"covered", "fixed", "not-covered"} for r in defect_coverage(b))
