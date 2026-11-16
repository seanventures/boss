"""The acceptance checker's misfires on FXQuote (BOSSAPP-1333, KAN-11).

THE CASE (banking sample, 2026-11-16): four builds of a small Go service, eight acceptance
checks, 30 METs the reviewer evidenced that the checker overruled. Replayed against main at
c765240fd (#171), 25 still misfired:

  (a) "only manifests/lockfiles cited" on the dependency items (5 "Stack: Go. (one Go module,
      standard library only)", 19 "Single pinned dependency … confirmed via go.sum"): for an
      item ABOUT what the project depends on, go.mod and go.sum are the evidence;
  (b) "same as ITEM 1. (no file evidence cited)" (10, 11): the near-duplicate items pointed at
      an item met on fxquote.go, and that evidence did not carry over;
  (c) item 17 "only documents cited": the answer cited README.md AND boss.deploy.yaml, and a
      name with two dots was read as `boss.deploy`, absent; the history also cut its evidence
      mid-word (every stored copy is now `clip`ped);
  (d) item 6 "fxquote.go does not name rates.yaml": `loadRateTable(cfgPath)` reads the path its
      caller hands it, and main's `cfgPath := os.Getenv("FXQUOTE_CONFIG")` / `cfgPath =
      "rates.yaml"` resolved to the env var's NAME; and a module-root package (`package
      fxquote` in ./fxquote.go) had no call sites because its directory has no name.

The fixture is the real rows (read-only from the laptop): the contract's 19 items, each check's
answers, and the cited files of the commit each check judged. The first build's two checks
recorded the base commit (README only) as their head; they are replayed on the attempt's first
persisted commit (b409779), the tree the reviewer read. An answer the gate downgraded is
replayed as the reviewer gave it (MET, its words without BOSS's appended reason).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from boss_engine.services import acceptance_gate as ag
from boss_engine.services.acceptance import checks as acc_checks
from boss_engine.services.acceptance import verdict as v

FIX = Path(__file__).parent / "fixtures" / "acceptance_fxquote"
DATA = json.loads((FIX / "checks.json").read_text(encoding="utf-8"))
TEXT = {i["n"]: i["text"] for i in DATA["items"]}


def _reply(answers) -> str:
    return "\n\n".join(f"ITEM {a['n']}: {a['said']} — {a['evidence']}" for a in answers) \
        + "\n\nACCEPTANCE: MET\n"


@pytest.mark.parametrize("check_id", sorted(DATA["checks"]))
def test_every_met_the_reviewer_evidenced_stands(check_id):
    check = DATA["checks"][check_id]
    downgraded = [a["n"] for a in check["answers"] if a.get("recorded_reason")]
    assert downgraded, "each check had misfires"
    own = sorted(a["n"] for a in check["answers"] if a["said"] != "MET")
    rec = ag.judge(DATA["items"], _reply(check["answers"]), {}, root=FIX / check["tree"])
    left = sorted(i["n"] for i in rec["items"] if i["status"] != "met")
    assert left == own, [(i["n"], i["evidence"][-160:]) for i in rec["items"]
                         if i["status"] != "met" and i["n"] not in own]


# ── (a) an item about dependencies is evidenced by what declares them ───────────────────────

@pytest.mark.parametrize("text", [TEXT[5], TEXT[19], "Only the standard library; no third-party "
                                  "packages", "All dependencies are pinned to exact versions",
                                  "Every dependency's licence is MIT or Apache-2.0",
                                  "Upgrade lodash to 4.17.21"])
def test_a_dependency_item_is_about_what_the_project_depends_on(text):
    assert v.declares_dependencies(text)


@pytest.mark.parametrize("text", [
    TEXT[6], TEXT[9], TEXT[4], TEXT[14], "a",
    # behaviour that happens to name a dependency: a manifest cannot show it
    "The service uses only the standard library to parse YAML and returns 400 on bad input",
    "Load rates with the pinned YAML library at start",
    "`go.mod` names the module",                      # code in backticks is not the subject
])
def test_an_item_about_behaviour_is_not(text):
    assert not v.declares_dependencies(text)


def test_manifests_evidence_only_a_dependency_item(tmp_path):
    (tmp_path / "go.mod").write_text("module fxquote\n\ngo 1.22\n\nrequire gopkg.in/yaml.v3 v3.0.1\n")
    (tmp_path / "go.sum").write_text("gopkg.in/yaml.v3 v3.0.1 h1:x\n")
    (tmp_path / "README.md").write_text("# fxquote\n")
    items = [{"n": 1, "text": TEXT[19]}, {"n": 2, "text": TEXT[9]}, {"n": 3, "text": TEXT[5]}]
    reply = ("ITEM 1: MET — go.mod:5 requires only gopkg.in/yaml.v3; go.sum agrees\n"
             "ITEM 2: MET — go.mod declares the module\n"
             "ITEM 3: MET — README.md says it is stdlib only\n")
    got = {i["n"]: i for i in v.parse_acceptance(reply, items, root=tmp_path)}
    assert got[1]["status"] == "met", got[1]
    assert got[2]["status"] == "partial" and "only manifests/lockfiles cited" in got[2]["evidence"]
    assert got[3]["status"] == "partial" and "only documents cited" in got[3]["evidence"]


# ── (b) "same as ITEM N" carries that item's evidence, verified here ─────────────────────────

def _tree(tmp_path):
    (tmp_path / "quote.go").write_text("package fxquote\n\nfunc Quote() int { return 1 }\n")
    return tmp_path


def test_same_as_carries_a_met_items_evidence(tmp_path):
    items = [{"n": 1, "text": "A quote rounds half-even"},
             {"n": 2, "text": "A quote correctly rounds half-even"}]
    reply = "ITEM 1: MET — quote.go:3 `Quote`\nITEM 2: MET — same as ITEM 1.\n"
    got = {i["n"]: i for i in v.parse_acceptance(reply, items, root=_tree(tmp_path))}
    assert got[2]["status"] == "met" and got[2]["evidence"] == "same as ITEM 1.", got[2]


@pytest.mark.parametrize("first", [
    "ITEM 1: PARTIAL — quote.go:3 rounds half-up",       # the referenced item is not met
    "ITEM 1: MET — it is all there",                     # met, but on no evidence
    "ITEM 1: MET — same as ITEM 2.",                     # a cycle names no file either
])
def test_same_as_never_makes_evidence_out_of_nothing(tmp_path, first):
    items = [{"n": 1, "text": "A quote rounds half-even"},
             {"n": 2, "text": "A quote correctly rounds half-even"}]
    got = {i["n"]: i for i in v.parse_acceptance(f"{first}\nITEM 2: MET — same as ITEM 1.\n",
                                                  items, root=_tree(tmp_path))}
    assert got[2]["status"] == "partial" and "no file evidence cited" in got[2]["evidence"]


def test_same_as_evidence_is_still_checked_for_this_item(tmp_path):
    """Carried evidence is verified as THIS item's: a loader item still needs its loader."""
    (tmp_path / "rates.yaml").write_text("pairs: {}\n")
    items = [{"n": 1, "text": "A quote rounds half-even"},
             {"n": 2, "text": "Load rates from rates.yaml at start", "needs_loader": True}]
    got = {i["n"]: i for i in v.parse_acceptance(
        "ITEM 1: MET — quote.go:3 `Quote`\nITEM 2: MET — same as ITEM 1.\n", items,
        root=_tree(tmp_path))}
    assert got[2]["status"] == "partial", got[2]


# ── (c) a name with two dots; every stored copy clipped ─────────────────────────────────────

def test_a_file_name_with_two_dots_is_one_file(tmp_path):
    (tmp_path / "boss.deploy.yaml").write_text("health: []\n")
    (tmp_path / "cfg.go").write_text("package cfg\n\ntype Routes struct{}\n\nfunc (Routes) Match() {}\n")
    found, absent = v.evidence_paths(tmp_path, "boss.deploy.yaml:6 declares the smoke check")
    assert found == ["boss.deploy.yaml"] and not absent
    # a qualified name is still read as before, never as one missing three-part file
    _, absent = v.evidence_paths(tmp_path, "`cfg.Routes.Match` matches it")
    assert "cfg.Routes.Match" not in absent


def test_every_stored_copy_of_the_evidence_is_clipped_not_cut():
    long = ("README.md:17-92 has curl examples for every case. " * 30).strip() \
        + " (only documents cited)"
    row = acc_checks._items({"items": [{"n": 17, "status": "partial", "evidence": long}]})[0]
    assert len(row["evidence"]) <= 1200
    assert row["evidence"].endswith("… (only documents cited)"), row["evidence"][-60:]
    import inspect

    from boss_engine.services.acceptance import ledger

    assert 'clip(str(item.get("evidence") or ""), 600)' in inspect.getsource(ledger)


# ── (d) the path a caller hands the loader ──────────────────────────────────────────────────

def test_an_env_lookups_name_is_never_the_value_its_default_is(tmp_path):
    (tmp_path / "go.mod").write_text("module fxquote\n")
    (tmp_path / "rates.yaml").write_text("pairs: {}\n")
    (tmp_path / "fxquote.go").write_text(
        "package fxquote\n\nimport \"os\"\n\n"
        "func loadRateTable(cfgPath string) ([]byte, error) {\n\treturn os.ReadFile(cfgPath)\n}\n\n"
        "func NewServer(cfgPath string) error {\n\t_, err := loadRateTable(cfgPath)\n\treturn err\n}\n")
    (tmp_path / "cmd" / "fxquote").mkdir(parents=True)
    (tmp_path / "cmd" / "fxquote" / "main.go").write_text(
        "package main\n\nimport (\n\t\"os\"\n\n\t\"fxquote\"\n)\n\nfunc main() {\n"
        "\tcfgPath := os.Getenv(\"FXQUOTE_CONFIG\")\n\tif cfgPath == \"\" {\n"
        "\t\tcfgPath = \"rates.yaml\"\n\t}\n\t_ = fxquote.NewServer(cfgPath)\n}\n")
    d = v._Definitions(tmp_path)
    assert d.param_value("fxquote.go", "loadRateTable", "cfgPath") == "rates.yaml"
    assert d.body_reads("fxquote.go", "loadRateTable", ["rates.yaml"])
    # another file is not read by it
    assert not d.body_reads("fxquote.go", "loadRateTable", ["other.yaml"])


def test_callers_that_disagree_bind_nothing(tmp_path):
    (tmp_path / "fxquote.go").write_text(
        "package fxquote\n\nimport \"os\"\n\n"
        "func load(p string) ([]byte, error) { return os.ReadFile(p) }\n\n"
        "func A() { load(\"rates.yaml\") }\n\nfunc B() { load(\"other.yaml\") }\n")
    d = v._Definitions(tmp_path)
    assert d.param_value("fxquote.go", "load", "p") is None
    assert not d.body_reads("fxquote.go", "load", ["rates.yaml"])
