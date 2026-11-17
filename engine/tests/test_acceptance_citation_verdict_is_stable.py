"""AR-31 (BOSSAPP-1345, a re-open of AR-23): the citation check gives one verdict for one kind of
evidence — a rewording of the same real evidence must not flip met to partial.

THE CASE (Northgate re-check, 2026-11-17): the same code was 29 met (crun_579975fa542645f0a9d5)
then 27 met / 2 partial (crun_4db2096201f94f1da054). The two items are in the fixture with the
reviewer's words from both runs:
  * AC-5 "... match actual Makefile/compose/handler behavior" → "cited paths not in the tree:
    Makefile/compose/handler": prose listing three things was read as one path;
  * AC-24 "... present and correct in Makefile." → "no file evidence cited": the sentence's
    full stop hid the file name, and the `go vet` / `go test` runs were not read as evidence
    although the run had recorded them.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from boss_engine.services.acceptance import verdict as v

RUNS = json.loads((Path(__file__).parent / "fixtures" / "acceptance_ar31" /
                   "items.json").read_text(encoding="utf-8"))

TREE = {
    "go.mod": "module northgate\n\ngo 1.23\n",
    "Makefile": "test:\n\tgo vet ./... && go test ./...\nrun-staging:\n\tdocker compose up\n",
    "docker-compose.yml": "services:\n  gateway:\n    build: .\n",
    "README.md": "# Northgate\n\nmake test\n",
    "cmd/northgate-gateway/main.go": "package main\n\nfunc main() {}\n",
    "internal/handler/handler.go": "package handler\n\nfunc Serve() {}\n",
}
RECORDED = [{"command": "go vet ./... && go test ./...", "ok": True}]


@pytest.fixture()
def tree(tmp_path):
    for rel, body in TREE.items():
        f = tmp_path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(body, encoding="utf-8")
    return tmp_path


def _judge(tree, items, *, checks=None):
    reply = "\n".join(f"ITEM {i['n']}: MET — {i['evidence']}" for i in items)
    return {i["id"]: i for i in v.parse_acceptance(reply, items, root=tree, checks=checks)}


@pytest.mark.parametrize("item_id", ["AC-5", "AC-24"])
def test_both_runs_evidence_gets_the_same_verdict(tree, item_id):
    verdicts = {}
    for run, items in RUNS.items():
        got = _judge(tree, items, checks=RECORDED)[item_id]
        verdicts[run] = got["status"]
        assert got["status"] == v.MET, (run, got["evidence"])
    assert len(set(verdicts.values())) == 1


@pytest.mark.parametrize("item_id", ["AC-5", "AC-24"])
def test_the_new_wording_is_met_without_the_recorded_run(tree, item_id):
    """The file evidence alone holds (the Makefile is in the tree): no check is needed."""
    items = RUNS["crun_4db2096201f94f1da054"]
    assert _judge(tree, items)[item_id]["status"] == v.MET


def test_prose_with_slashes_is_not_a_missing_path(tree):
    found, absent = v.evidence_paths(tree, "match actual Makefile/compose/handler behavior")
    assert found == ["Makefile"] and absent == []
    found, absent = v.evidence_paths(tree, "both pass/fail and read/write paths hold")
    assert found == [] and absent == []


def test_a_bare_file_name_at_the_end_of_a_sentence_resolves(tree):
    found, _ = v.evidence_paths(tree, "targets present and correct in Makefile.")
    assert found == ["Makefile"]
    found, _ = v.evidence_paths(tree, "see Makefile, and go.mod")
    assert "Makefile" in found


def test_a_command_the_run_recorded_is_evidence_for_an_item_about_it(tree):
    item = {"n": 1, "id": "AC-1", "text": "`go vet` and `go test ./...` clean.",
            "evidence": "`go vet ./...` and `go test ./...` run clean in this tree"}
    assert _judge(tree, [item], checks=RECORDED)["AC-1"]["status"] == v.MET
    assert _judge(tree, [item])["AC-1"]["status"] == v.PARTIAL      # nothing recorded
    failed = [{"command": "go vet ./... && go test ./...", "ok": False}]
    assert _judge(tree, [item], checks=failed)["AC-1"]["status"] == v.PARTIAL


def test_a_recorded_test_run_does_not_evidence_another_item(tree):
    item = {"n": 1, "id": "AC-1", "text": "Exports invoices as CSV.",
            "evidence": "`go test ./...` run clean in this tree"}
    assert _judge(tree, [item], checks=RECORDED)["AC-1"]["status"] == v.PARTIAL


def test_a_cited_path_that_is_not_in_the_tree_stays_partial(tree):
    item = {"n": 1, "id": "AC-1", "text": "The gateway routes by tenant.",
            "evidence": "routing is in internal/router/tenant.go and cmd/missing/main.go"}
    got = _judge(tree, [item], checks=RECORDED)["AC-1"]
    assert got["status"] == v.PARTIAL
    assert "cited paths not in the tree: internal/router/tenant.go" in got["evidence"]


def test_a_folder_style_path_without_an_extension_that_is_missing_stays_absent(tree):
    _, absent = v.evidence_paths(tree, "wired in internal/router/tenant")
    assert absent == ["internal/router/tenant"]


@pytest.mark.parametrize("recorded", ["false && go vet ./...; true", "go vet ./... || true",
                                      "true; go vet ./...", "go test ./... | tee out",
                                      "(go vet ./...)"])
def test_a_chain_that_may_not_have_run_a_part_credits_only_the_whole(tree, recorded):
    item = {"n": 1, "id": "AC-1", "text": "`go vet` is clean.",
            "evidence": "`go vet ./...` run clean in this tree"}
    got = _judge(tree, [item], checks=[{"command": recorded, "ok": True}])["AC-1"]
    assert got["status"] == v.PARTIAL


def test_a_passed_and_chain_credits_each_part_and_the_whole(tree):
    item = {"n": 1, "id": "AC-1", "text": "`go vet` is clean.",
            "evidence": "`go vet ./...` run clean in this tree"}
    for ran in ("go vet ./... && go test ./...", "go vet ./..."):
        assert _judge(tree, [item], checks=[{"command": ran, "ok": True}])["AC-1"]["status"] == v.MET


def test_command_evidence_never_excuses_a_cited_path_that_is_missing(tree):
    item = {"n": 1, "id": "AC-1", "text": "`go vet` is clean.",
            "evidence": "`go vet ./...` clean; wired in src/imaginary.go"}
    got = _judge(tree, [item], checks=RECORDED)["AC-1"]
    assert got["status"] == v.PARTIAL
    assert "src/imaginary.go" in got["evidence"]
