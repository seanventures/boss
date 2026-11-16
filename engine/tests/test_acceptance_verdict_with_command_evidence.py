"""THE VERDICT WITH COMMAND EVIDENCE (AA-F, BOSSAPP-1220, epic BOSSAPP-1213).

One verdict module (acceptance/verdict.py) for both layers. The in-build gate's parse / evidence /
judge moved there unchanged (acceptance_gate re-exports them; its own 82 tests prove nothing
changed). The Acceptance Agent's reply adds COMMAND evidence: a MET citing a command must match a
command BOSS recorded, with the exit code and the output it claims. A MET BOSS cannot verify is
NOT JUDGED, never met (I2, I6) — a fabricated E-id, a misquote, a wrong exit, a README alone.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from boss_engine.services import acceptance_gate as ag
from boss_engine.services.acceptance import verdict as av
from boss_engine.services.acceptance.session import Command, CommandRecord

ITEMS = [{"id": "AC-1", "n": 1, "source": "ticket", "text": "Unauthenticated requests get 401"},
         {"id": "AC-2", "n": 2, "source": "ticket", "text": "make run-staging starts the gateway"},
         {"id": "AC-3", "n": 3, "source": "brief", "text": "The video reads clearly"}]


@pytest.fixture()
def tree(tmp_path: Path) -> Path:
    (tmp_path / "internal").mkdir()
    (tmp_path / "internal" / "auth.go").write_text("package auth\n\nfunc Verify() {}\n")
    (tmp_path / "README.md").write_text("# Gateway\nmake run-staging\n")
    return tmp_path


@pytest.fixture()
def record() -> CommandRecord:
    r = CommandRecord()
    r.add(Command(id="E1", cmd="HTTP GET http://127.0.0.1:8081/payments", kind="http",
                  exit=0, status=401, stdout='HTTP 401\n{"error":"missing token"}'))
    r.add(Command(id="E2", cmd="make run-staging", exit=2,
                  stderr="make: *** No rule to make target 'run-staging'.  Stop."))
    r.add(Command(id="E3", cmd="git push origin main", refused="git push is not allowed"))
    return r


def test_the_gate_reads_the_one_verdict_module():
    for name in ("parse_acceptance", "judge", "evidence_paths", "_resolve", "_DOC_PATH",
                 "wiring_problem", "passed", "findings_summary", "batch_summary", "MET",
                 "MAX_ITEMS", "MAX_PASSES", "_walk"):
        assert getattr(ag, name) is getattr(av, name), name


def test_a_met_citing_a_recorded_command_with_its_status_and_output_is_verified(tree, record):
    reply = ('AC-1: MET — evidence: E1 (exit 0) HTTP 401 "missing token"\n'
             'AC-2: MISSING — E2 (exit 2) "No rule to make target" — there is no target\n'
             "AC-3: HUMAN — a person must watch it\nACCEPTANCE: NOT MET")
    rec = av.judge(ITEMS, reply, {}, root=tree, exec_log=record)
    by = {i["id"]: i for i in rec["items"]}
    assert by["AC-1"]["status"] == "met"
    assert by["AC-1"]["evidence_refs"][0] == {"kind": "exec", "ref": "E1",
                                              "excerpt": "missing token", "verified": True}
    assert by["AC-2"]["status"] == "missing" and by["AC-3"]["status"] == "human"
    assert rec["verdict"] == "not_met" and rec["counts"]["human"] == 1
    assert rec["human"] == [3] and rec["unverified"] == []


@pytest.mark.parametrize("claim,why", [
    ('E9 (exit 0) "missing token"', "E9 is not a command this check ran"),        # fabricated
    ('E1 (exit 0) HTTP 401 "authorised"', "the quoted words are not in E1's output"),
    ('E2 "No rule to make target"', "E2 exited 2, not 0"),                       # implied exit 0
    ('E2 (exit 0) "No rule to make target"', "E2 exited 2, not 0"),               # wrong exit
    ('E1 (exit 0) HTTP 200 "missing token"', "E1 answered HTTP 401, not 200"),     # wrong status
    ('E3 (exit 0) "pushed"', "E3 was refused and never ran"),
    ("E1 (exit 0)", "E1 cited without the output it showed"),
    ("see README.md", "no verifiable evidence (only documents cited)"),            # README alone
    ("it works", "no verifiable evidence (no evidence cited)"),
])
def test_a_met_boss_cannot_verify_is_not_judged_never_met(tree, record, claim, why):
    rec = av.judge(ITEMS[:1], f"AC-1: MET — evidence: {claim}\nACCEPTANCE: MET", {}, root=tree,
                   exec_log=record)
    item = rec["items"][0]
    assert item["status"] == "unjudged" and item["unverified"] is True
    assert why in item["evidence"]
    assert rec["verdict"] == "not_met" and rec["unverified"] == [1]
    assert "could not verify" in rec["why"]


def test_one_fabricated_citation_sinks_a_met_that_also_cites_a_real_one(tree, record):
    reply = ('AC-1: MET — E1 (exit 0) "missing token" and E7 (exit 0) "401"\n'
             "ACCEPTANCE: MET")
    item = av.judge(ITEMS[:1], reply, {}, root=tree, exec_log=record)["items"][0]
    assert item["status"] == "unjudged"
    assert [r["verified"] for r in item["evidence_refs"]] == [True, False]


def test_an_expected_failure_is_met_when_its_exit_is_claimed_and_true(tree, record):
    """A MET may rest on a command that fails ("the bad config is rejected"): the exit it
    claims must be the one it had."""
    items = [{"id": "AC-1", "n": 1, "source": "ticket", "text": "the target is absent"}]
    reply = 'AC-1: MET — E2 (exit 2) "No rule to make target"\nACCEPTANCE: MET'
    rec = av.judge(items, reply, {}, root=tree, exec_log=record)
    assert rec["items"][0]["status"] == "met" and rec["verdict"] == "met"


def test_file_evidence_in_the_outer_layer_must_exist_at_that_line(tree, record):
    ok = av.judge(ITEMS[:1], "AC-1: MET — internal/auth.go:3 Verify\nACCEPTANCE: MET", {},
                  root=tree, exec_log=record)
    assert ok["items"][0]["status"] == "met" and ok["verdict"] == "met"
    past = av.judge(ITEMS[:1], "AC-1: MET — internal/auth.go:99\nACCEPTANCE: MET", {},
                    root=tree, exec_log=record)
    assert past["items"][0]["status"] == "unjudged"
    assert "past the end" in past["items"][0]["evidence"]


def test_human_items_never_count_as_met(tree, record):
    items = [ITEMS[0], ITEMS[2]]
    reply = 'AC-1: MET — E1 (exit 0) "missing token"\nAC-3: HUMAN — taste\nACCEPTANCE: MET'
    rec = av.judge(items, reply, {}, root=tree, exec_log=record)
    assert rec["verdict"] == "not_met" and rec["why"] == "1 items need a person"
    assert not av.passed(rec)


def test_items_a_replayed_probe_proved_are_met_without_a_reply(tree, record):
    replay = {"AC-1": {"result": "pass", "command_ids": ["E4"], "check_id": "acc_prev"},
              "AC-2": {"result": "pass", "command_ids": ["E5"], "check_id": "acc_prev"}}
    rec = av.judge(ITEMS[:2], "", {}, root=tree, exec_log=record, replay=replay)
    assert rec["verdict"] == "met" and rec["replayed"] == [1, 2]
    assert rec["items"][0]["evidence_refs"] == [{"kind": "replay", "ref": "E4",
                                                 "verified": True}]
    # A probe that failed again carries nothing: the item is the agent's to answer.
    failed = {"AC-1": {"result": "fail", "command_ids": ["E4"]}}
    rec = av.judge(ITEMS[:1], "", {}, root=tree, exec_log=record, replay=failed)
    assert rec["items"][0]["status"] == "unjudged" and rec["verdict"] == "not_run"


def test_without_a_command_record_the_gate_judges_exactly_as_before(tree):
    """The inner gate: no exec_log → #136 semantics (a README-only MET is PARTIAL, not
    unjudged; HUMAN is not an answer it reads)."""
    items = [{"n": 1, "source": "ticket", "text": "Unauthenticated requests get 401"}]
    rec = av.judge(items, "ITEM 1: MET — README.md\nACCEPTANCE: MET", {}, root=tree)
    assert rec["items"][0]["status"] == "partial" and rec["verdict"] == "not_met"
    assert "human" not in rec["counts"] and "unverified" not in rec


def test_diff_names_newly_met_regressed_and_still_open():
    prev = {"items": [{"id": "AC-1", "status": "missing"}, {"id": "AC-2", "status": "met"},
                      {"id": "AC-3", "status": "partial"}]}
    cur = {"items": [{"id": "AC-1", "status": "met"}, {"id": "AC-2", "status": "missing"},
                     {"id": "AC-3", "status": "partial"}, {"id": "AC-4", "status": "missing"}]}
    assert av.diff(prev, cur) == {"newly_met": ["AC-1"], "regressed": ["AC-2"],
                                  "still_open": ["AC-3"]}


def test_exec_claims_read_each_citation_between_e_ids():
    got = av.exec_claims('E2 (exit 2) "No rule" then E10 HTTP 401 “denied” and E3')
    assert got == [{"ref": "E2", "excerpt": "No rule", "exit": 2, "status": None},
                   {"ref": "E10", "excerpt": "denied", "exit": None, "status": 401},
                   {"ref": "E3", "excerpt": None, "exit": None, "status": None}]
