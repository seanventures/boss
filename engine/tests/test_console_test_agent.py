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
