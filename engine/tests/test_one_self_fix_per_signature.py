"""One self-fix per cause at a time, install-wide (BOSSAPP-571 R19, F14): a second step that hits
the same class signature waits on the fix being built instead of starting another, and resumes
with it."""
from __future__ import annotations

import pytest
from sqlalchemy import text

from boss_engine.services import self_fix as sf

from ._self_fix_support import harness, self_fix_on  # noqa: F401
from .test_workflow_capability_runner import eng, project  # noqa: F401


def test_the_second_run_waits_on_the_first_runs_fix(harness):
    first = harness.propose(run_id="wfr_first", signature="deploy:port_held:8000",
                            kind="deploy")
    second = harness.propose(run_id="wfr_second", signature="deploy:port_held:9000",
                             kind="deploy")
    assert first["outcome"] == "started"
    assert second["outcome"] == "waiting_on"
    assert second["self_fix"]["id"] == first["self_fix"]["id"]
    with harness.eng.begin() as c:
        count = c.execute(text("SELECT count(*) FROM self_fixes WHERE project_id = :p"),
                          {"p": harness.project}).scalar()
        waiting = sf.waiters(c, first["self_fix"]["id"])
    assert count == 1
    assert [(w["run_id"], w["step_id"]) for w in waiting] == [("wfr_second", "deploy")]
    handle = sf.handle_for(second["self_fix"], step_kind="deploy", waiting_on=True)
    with harness.eng.begin() as c:
        assert sf.handle_verdict(c, handle)[0] == "wait"
        c.execute(text("UPDATE self_fixes SET status = 'installed' WHERE id = :i"),
                  {"i": first["self_fix"]["id"]})
        assert sf.handle_verdict(c, handle)[0] == "resume", "it resumes with the first fix"


def test_a_finished_claim_frees_the_cause(harness):
    first = harness.propose(run_id="wfr_a", signature="deploy:port_held:8000", kind="deploy")
    with harness.eng.begin() as c:
        sf.refuse(c, first["self_fix"]["id"], "no_reproduction", "no test",
                  settings=harness.settings)
    again = harness.propose(run_id="wfr_b", signature="deploy:port_held:8000", kind="deploy")
    assert again["outcome"] == "started" and again["self_fix"]["id"] != first["self_fix"]["id"]


def test_the_attempts_per_cause_are_capped(harness):
    for n in range(2):
        out = harness.propose(run_id=f"wfr_{n}", signature="deploy:port_held:8000",
                              kind="deploy")
        assert out["outcome"] == "started"
        with harness.eng.begin() as c:
            sf.refuse(c, out["self_fix"]["id"], "tests", "did not hold",
                      settings=harness.settings)
    third = harness.propose(run_id="wfr_3", signature="deploy:port_held:8000", kind="deploy")
    assert third["outcome"] == "skipped" and "already tried" in third["why"]


# brec_9a4e09127375448294f0 (live, 2026-10-24): two tries were used up by refusals BOSS's own
# harness made — api_suite (no self-fix test database) and tests_weakened from a gate bug fixed
# in ef166d9cd — so a person's request was refused "already tried 2 time(s)" before BOSS ever
# completed one genuine attempt.

def _refused(harness, n, refusal):
    out = harness.propose(run_id=f"wfr_m{n}", signature="deploy:port_held:8000", kind="deploy")
    assert out["outcome"] == "started", out
    with harness.eng.begin() as c:
        sf.refuse(c, out["self_fix"]["id"], refusal, "x", settings=harness.settings)
    return out["self_fix"]["id"]


@pytest.mark.parametrize("refusal", sorted(sf.INFRA_REFUSALS))
def test_a_try_the_harness_could_not_run_is_not_a_try(harness, refusal):
    for n in range(3):
        _refused(harness, n, refusal)
    again = harness.propose(run_id="wfr_m9", signature="deploy:port_held:8000", kind="deploy")
    assert again["outcome"] == "started", again


def test_a_gate_refusal_counts_while_that_gate_is_unchanged(harness):
    for n in range(2):
        _refused(harness, n, "tests_weakened")
    third = harness.propose(run_id="wfr_m9", signature="deploy:port_held:8000", kind="deploy")
    assert third["outcome"] == "skipped" and "already tried" in third["why"]


def test_a_fixed_gate_gives_the_cause_its_tries_back(harness):
    ids = [_refused(harness, n, "secrets") for n in range(2)]
    with harness.eng.begin() as c:   # refused by an earlier build of the gates
        c.execute(text("UPDATE self_fixes SET gates = gates || '{\"gate_fingerprint\": \"old\"}'"
                       "::jsonb WHERE id = ANY(:i)"), {"i": ids})
    again = harness.propose(run_id="wfr_m9", signature="deploy:port_held:8000", kind="deploy")
    assert again["outcome"] == "started", again
