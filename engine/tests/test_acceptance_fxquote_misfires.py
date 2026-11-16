"""The acceptance checker's misfires on FXQuote (BOSSAPP-1333, KAN-11).

THE CASE (banking sample, 2026-11-16): four builds of a small Go service, eight acceptance
checks, 30 METs the reviewer evidenced that the checker overruled. Replayed against main at
c765240fd (#171), 25 still misfired:

  (a) "only manifests/lockfiles cited" on the dependency items (5 "Stack: Go. (one Go module,
      standard library only)", 19 "Single pinned dependency … confirmed via go.sum"): an item
      ABOUT what the project depends on is settled by READING the manifests (PR #172 review P1)
      — 19 holds; 5 is FALSE on this tree (go.mod requires gopkg.in/yaml.v3) and stays partial;
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
from typing import Optional

import pytest

from boss_engine.services import acceptance_gate as ag
from boss_engine.services.acceptance import checks as acc_checks
from boss_engine.services.acceptance import dependencies as _deps
from boss_engine.services.acceptance import verdict as v

FIX = Path(__file__).parent / "fixtures" / "acceptance_fxquote"
DATA = json.loads((FIX / "checks.json").read_text(encoding="utf-8"))
TEXT = {i["n"]: i["text"] for i in DATA["items"]}


def _reply(answers) -> str:
    return "\n\n".join(f"ITEM {a['n']}: {a['said']} — {a['evidence']}" for a in answers) \
        + "\n\nACCEPTANCE: MET\n"


@pytest.mark.parametrize("check_id", sorted(DATA["checks"]))
def test_every_met_the_reviewer_evidenced_stands_and_a_false_one_does_not(check_id):
    """Every overruled MET stands — except item 5, "Stack: Go. (one Go module, standard library
    only)", which is FALSE on this tree: go.mod requires gopkg.in/yaml.v3 (PR #172 review P1).
    The reviewer's own PARTIALs replay unchanged."""
    check = DATA["checks"][check_id]
    downgraded = [a["n"] for a in check["answers"] if a.get("recorded_reason")]
    assert downgraded, "each check had misfires"
    own = {a["n"] for a in check["answers"] if a["said"] != "MET"}
    rec = ag.judge(DATA["items"], _reply(check["answers"]), {}, root=FIX / check["tree"])
    left = sorted(i["n"] for i in rec["items"] if i["status"] != "met")
    assert left == sorted(own | {5}), [(i["n"], i["evidence"][-160:]) for i in rec["items"]
                                       if i["status"] != "met"]
    five = next(i for i in rec["items"] if i["n"] == 5)
    assert five["status"] == "partial" and "go.mod requires gopkg.in/yaml.v3" in five["evidence"]
    nineteen = next(i for i in rec["items"] if i["n"] == 19)
    assert nineteen["status"] == "met", nineteen


# ── (a) a dependency item is settled by READING the manifests (PR #172 review P1) ────────────

FXQ = FIX / "074abce"          # go.mod: require gopkg.in/yaml.v3 v3.0.1


def _one(text: str, root: Path, said: str = "MET — go.mod and go.sum list the modules"):
    return v.parse_acceptance(f"ITEM 1: {said}\nACCEPTANCE: MET\n", [{"n": 1, "text": text}],
                              root=root)[0]


@pytest.mark.parametrize("text,status,why", [
    # the reviewer's probes on FXQuote's own tree
    ("Rate YAML parsing uses only the standard library", "partial", "only manifests"),
    ("Standard library only, no external modules", "partial", "requires gopkg.in/yaml.v3"),
    ("Every dependency is licensed MIT", "human", "licence"),
    ("Quote IDs use no third-party library and are unguessable", "partial", "only manifests"),
    # what the manifest does settle
    (TEXT[19], "met", ""),
    ("Single dependency `gopkg.in/yaml.v3`", "met", ""),
    ("Single dependency `github.com/spf13/cobra`", "partial", "exactly github.com/spf13/cobra"),
    ("The yaml dependency is pinned to v3.0.1", "met", ""),
    ("The yaml dependency is pinned to v2", "partial", "not 2"),
    (TEXT[5], "partial", "requires gopkg.in/yaml.v3"),
])
def test_a_dependency_claim_is_checked_against_the_manifest(text, status, why):
    got = _one(text, FXQ)
    assert got["status"] == status and why in got["evidence"], got


def test_standard_library_only_holds_on_a_tree_with_no_requires(tmp_path):
    (tmp_path / "go.mod").write_text("module fxquote\n\ngo 1.23\n")
    (tmp_path / "main.go").write_text("package main\n\nfunc main() {}\n")
    assert _one("Standard library only, no external modules", tmp_path)["status"] == "met"
    assert _one(TEXT[5], tmp_path)["status"] == "met"
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "go.mod").write_text("module other\n")
    assert _one(TEXT[5], tmp_path)["status"] == "partial"     # two modules, not one


@pytest.mark.parametrize("files,deps", [
    ({"package.json": '{"dependencies": {"express": "4.19.2"}, "devDependencies": {"jest": "29"}, '
                      '"optionalDependencies": {"fsevents": "2"}, "peerDependencies": {"react": "18"}}'},
     {"express": "4.19.2", "fsevents": "2", "react": "18"}),
    ({"requirements.txt": "flask==3.0.0\n# comment\nrequests>=2\n"},
     {"flask": "3.0.0", "requests": "2"}),
    ({"pyproject.toml": '[project]\nname = "x"\ndependencies = ["httpx==0.27", "pydantic"]\n'},
     {"httpx": "0.27", "pydantic": ""}),
    ({"pom.xml": "<project><dependencies><dependency><groupId>g</groupId><artifactId>a</artifactId>"
                 "<version>1.0</version></dependency><dependency><groupId>t</groupId>"
                 "<artifactId>junit</artifactId><scope>test</scope></dependency>"
                 "</dependencies></project>"}, {"g:a": "1.0"}),
    ({"go.mod": "module m\n\nrequire (\n\tgithub.com/a/b v1.2.3\n\tgithub.com/c/d v0.1.0 // indirect\n)\n"},
     {"github.com/a/b": "v1.2.3", "github.com/c/d": "v0.1.0"}),     # an indirect one links in
])
def test_each_manifest_is_read_for_its_direct_runtime_requirements(tmp_path, files, deps):
    for name, text in files.items():
        (tmp_path / name).write_text(text)
    assert _deps.manifests(tmp_path)["deps"] == deps


@pytest.mark.parametrize("text", [
    "Rate YAML parsing uses only the standard library",
    "Quote IDs use no third-party library and are unguessable",
    "The service uses only the standard library to parse YAML and returns 400 on bad input",
    TEXT[6], TEXT[9], "a",
])
def test_an_item_mixing_behaviour_with_dependency_words_is_a_behaviour_item(text):
    assert not _deps.is_dependency_item(text, {"gopkg.in/yaml.v3": "v3.0.1"})


def test_a_behaviour_item_still_needs_code_not_a_manifest(tmp_path):
    (tmp_path / "go.mod").write_text("module fxquote\n")
    (tmp_path / "README.md").write_text("# fxquote\n")
    got = _one(TEXT[9], tmp_path, "MET — go.mod declares the module")
    assert got["status"] == "partial" and "only manifests/lockfiles cited" in got["evidence"]
    got = _one(TEXT[9], tmp_path, "MET — README.md says so")
    assert got["status"] == "partial" and "only documents cited" in got["evidence"]


def test_the_exercised_door_settles_a_dependency_claim_the_same_way():
    rows = v.parse_exercised(
        "AC-1: MET — go.mod lists only yaml.v3\nAC-2: MET — go.mod has no requires\n"
        "AC-3: MET — every dependency is MIT\n",
        [{"n": 1, "id": "AC-1", "text": TEXT[19]},
         {"n": 2, "id": "AC-2", "text": "Standard library only, no external modules"},
         {"n": 3, "id": "AC-3", "text": "Every dependency is licensed MIT"}],
        root=FXQ, exec_log=[])
    got = {r["n"]: r["status"] for r in rows}
    assert got == {1: "met", 2: "partial", 3: "human"}, rows


# ── (b) "same as ITEM N" carries evidence only between near-duplicates (P2) ─────────────────

def _tree(tmp_path):
    (tmp_path / "quote.go").write_text("package fxquote\n\nfunc Quote() int { return 1 }\n")
    return tmp_path


def test_same_as_carries_a_near_duplicates_evidence(tmp_path):
    items = [{"n": 1, "text": "A quote rounds half-even"},
             {"n": 2, "text": "A quote correctly rounds half-even"}]
    reply = "ITEM 1: MET — quote.go:3 `Quote`\nITEM 2: MET — same as ITEM 1.\n"
    got = {i["n"]: i for i in v.parse_acceptance(reply, items, root=_tree(tmp_path))}
    assert got[2]["status"] == "met" and got[2]["evidence"] == "same as ITEM 1.", got[2]
    assert v.near_duplicate(TEXT[1], TEXT[10]) and v.near_duplicate(TEXT[2], TEXT[11])


def test_same_as_on_a_different_item_is_not_judged(tmp_path):
    """The review's probe: "413 on >1 MB" passed on the evidence of "written in Go"."""
    items = [{"n": 1, "text": "Written in Go"},
             {"n": 2, "text": "A request body over 1 MB returns 413"}]
    reply = "ITEM 1: MET — quote.go:1 `package fxquote`\nITEM 2: MET — same as ITEM 1.\n"
    got = {i["n"]: i for i in v.parse_acceptance(reply, items, root=_tree(tmp_path))}
    assert got[2]["status"] == "unjudged" and got[2].get("unverified"), got[2]
    assert "not judged: cites another item's evidence, ITEM 1" in got[2]["evidence"]
    rec = ag.judge(items, reply + "ACCEPTANCE: MET\n", {}, root=_tree(tmp_path))
    assert rec["verdict"] == "not_met"
    assert not v.near_duplicate(TEXT[1], TEXT[19]) and not v.near_duplicate(TEXT[6], TEXT[1])


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
    items = [{"n": 1, "text": "Load rates from rates.yaml at start"},
             {"n": 2, "text": "Load the rates from rates.yaml at start", "needs_loader": True}]
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


def test_a_path_the_environment_configures_is_not_the_code_default(tmp_path):
    """P3: the Dockerfile sets the variable, so the code's default is not what runs; a path in
    the image is the tree file a COPY put there."""
    (tmp_path / "rates.yaml").write_text("pairs: {}\n")
    (tmp_path / "prod.yaml").write_text("pairs: {}\n")
    (tmp_path / "load.go").write_text(
        "package main\n\nimport \"os\"\n\nfunc load(p string) ([]byte, error) { return os.ReadFile(p) }\n")
    (tmp_path / "main.go").write_text(
        "package main\n\nimport \"os\"\n\n"
        "func main() {\n\tcfg := os.Getenv(\"RATES\")\n\tif cfg == \"\" {\n\t\tcfg = \"rates.yaml\"\n\t}\n"
        "\tload(cfg)\n}\n")
    d = v._Definitions(tmp_path)
    assert d.param_value("load.go", "load", "p") == "rates.yaml"        # nothing configures it
    assert d.body_reads("load.go", "load", ["rates.yaml"])
    (tmp_path / "Dockerfile").write_text("FROM x\nCOPY prod.yaml /app/prod.yaml\n"
                                         "ENV RATES=/app/prod.yaml\n")
    d = v._Definitions(tmp_path)
    assert d.param_value("load.go", "load", "p") == "prod.yaml"
    assert not d.body_reads("load.go", "load", ["rates.yaml"])
    assert d.body_reads("load.go", "load", ["prod.yaml"])
    (tmp_path / "docker-compose.yml").write_text("services:\n  app:\n    environment:\n"
                                                 "      RATES: /etc/other.yaml\n")
    d = v._Definitions(tmp_path)
    assert d.param_value("load.go", "load", "p") is None               # they disagree


# ── PR #172 re-review: "standard library only" never passes on a gap in the reading ─────────

STDLIB = "Standard library only, no external modules"


def _go_tree(tmp_path, go_mod="module github.com/acme/app\n\ngo 1.23\n", imports=()):
    (tmp_path / "go.mod").write_text(go_mod)
    block = "".join(f'\t"{i}"\n' for i in imports)
    (tmp_path / "main.go").write_text(f"package main\n\nimport (\n\t\"fmt\"\n{block})\n\n"
                                      "func main() { fmt.Println() }\n")
    return tmp_path


def test_stdlib_only_holds_on_a_clean_go_tree_and_its_own_packages(tmp_path):
    assert _one(STDLIB, _go_tree(tmp_path, imports=["github.com/acme/app/internal/rates"]))[
        "status"] == "met"


@pytest.mark.parametrize("case", ["indirect", "import", "single_x_but_imports_y"])
def test_stdlib_only_counts_indirect_requires_and_the_codes_imports(tmp_path, case):
    if case == "indirect":
        root = _go_tree(tmp_path, "module github.com/acme/app\n\nrequire github.com/c/d v0.1.0 // indirect\n")
        assert "requires github.com/c/d" in _one(STDLIB, root)["evidence"]
    elif case == "import":
        root = _go_tree(tmp_path, imports=["github.com/spf13/cobra"])
        got = _one(STDLIB, root)
        assert got["status"] == "partial" and "imports github.com/spf13/cobra" in got["evidence"]
    else:
        root = _go_tree(tmp_path, "module github.com/acme/app\n\nrequire gopkg.in/yaml.v3 v3.0.1\n",
                        imports=["gopkg.in/yaml.v3", "github.com/spf13/cobra"])
        got = _one("Single dependency `gopkg.in/yaml.v3`", root)
        assert got["status"] == "partial" and "imports github.com/spf13/cobra" in got["evidence"]
    assert _one(STDLIB, root)["status"] == "partial"


@pytest.mark.parametrize("files,why", [
    ({"requirements.txt": "-r base.txt\n", "base.txt": "flask==3.0.0\n"}, "flask"),
    ({"package.json": '{"optionalDependencies": {"fsevents": "2"}}'}, "fsevents"),
    ({"package.json": '{"peerDependencies": {"react": "18"}}'}, "react"),
    ({"requirements.txt": "", "app.py": "import requests\n"}, "imports requests"),
    ({"package.json": "{}", "index.js": "const _ = require('lodash');\n"}, "imports lodash"),
    ({"package.json": "{}", "index.ts": "import { z } from 'zod';\n"}, "imports zod"),
])
def test_stdlib_only_is_false_on_what_other_ecosystems_declare_or_import(tmp_path, files, why):
    for name, text in files.items():
        (tmp_path / name).write_text(text)
    got = _one(STDLIB, tmp_path)
    assert got["status"] == "partial" and why in got["evidence"], got


def test_stdlib_and_local_imports_are_not_third_party(tmp_path):
    (tmp_path / "requirements.txt").write_text("")
    (tmp_path / "rates").mkdir()
    (tmp_path / "rates" / "__init__.py").write_text("")
    (tmp_path / "app.py").write_text("import os, json\nfrom rates import load\nfrom . import x\n")
    (tmp_path / "package.json").write_text("{}")
    (tmp_path / "index.js").write_text("const fs = require('fs');\nimport p from 'node:path';\n"
                                       "import x from './x';\n")
    assert _deps.third_party_imports(tmp_path) == []
    assert _one(STDLIB, tmp_path)["status"] == "met"


@pytest.mark.parametrize("files", [
    {"requirements.txt": "-e git+https://github.com/acme/lib.git#egg=lib\n"},
    {"requirements.txt": "lib @ https://example.test/lib.whl\n"},
    {"requirements.txt": "-r missing.txt\n"},
    {"pyproject.toml": '[project]\nname = "x"\ndynamic = ["dependencies", "version"]\n'},
    {"setup.py": "from setuptools import setup\nsetup(install_requires=['x'])\n"},
    {"setup.cfg": "[options]\ninstall_requires = x\n"},
    {"Pipfile": "[packages]\nx = '*'\n"},
])
def test_what_the_parser_cannot_fully_read_needs_a_person(tmp_path, files):
    for name, text in files.items():
        (tmp_path / name).write_text(text)
    got = _one(STDLIB, tmp_path)
    assert got["status"] == "human" and "cannot be fully read" in got["evidence"], got


# ── PR #172 re-review: no evidence across opposite meanings ─────────────────────────────────

@pytest.mark.parametrize("a,b", [
    ("Rejects request bodies over 1 MB with 413", "Accepts request bodies over 1 MB"),
    ("Returns 400 for an unknown currency", "Does not return 400 for an unknown currency"),
    ("A quote id is valid for 30 seconds", "A quote id is valid for 60 seconds"),
    ("An expired quote id returns 410", "An expired quote id returns 404"),
    ("Admins are allowed to export the ledger", "Admins are denied the ledger export"),
])
def test_opposite_items_are_never_near_duplicates(a, b):
    assert not v.near_duplicate(a, b) and not v.near_duplicate(b, a)


def test_same_as_across_opposite_meanings_is_not_judged(tmp_path):
    items = [{"n": 1, "text": "Rejects request bodies over 1 MB with 413"},
             {"n": 2, "text": "Accepts request bodies over 1 MB"}]
    reply = "ITEM 1: MET — quote.go:3 `Quote`\nITEM 2: MET — same as ITEM 1.\n"
    got = {i["n"]: i for i in v.parse_acceptance(reply, items, root=_tree(tmp_path))}
    assert got[2]["status"] == "unjudged" and "cites another item's evidence" in got[2]["evidence"]


# ── PR #172 re-review: a compose env_file ───────────────────────────────────────────────────

def _env_tree(tmp_path, compose: str, env: Optional[str] = None):
    (tmp_path / "rates.yaml").write_text("pairs: {}\n")
    (tmp_path / "load.go").write_text(
        "package main\n\nimport \"os\"\n\nfunc load(p string) ([]byte, error) { return os.ReadFile(p) }\n")
    (tmp_path / "main.go").write_text(
        "package main\n\nimport \"os\"\n\nfunc main() {\n\tcfg := os.Getenv(\"RATES\")\n"
        "\tif cfg == \"\" {\n\t\tcfg = \"rates.yaml\"\n\t}\n\tload(cfg)\n}\n")
    (tmp_path / "docker-compose.yml").write_text(compose)
    if env is not None:
        (tmp_path / "prod.env").write_text(env)
    return v._Definitions(tmp_path)


def test_a_compose_env_file_in_the_tree_is_read(tmp_path):
    d = _env_tree(tmp_path, "services:\n  app:\n    env_file: prod.env\n", "RATES=prod.yaml\n")
    assert d.param_value("load.go", "load", "p") == "prod.yaml"


@pytest.mark.parametrize("compose", [
    "services:\n  app:\n    env_file: secrets.env\n",
    "services:\n  app:\n    env_file:\n      - prod.env\n      - secrets.env\n",
])
def test_a_compose_env_file_not_in_the_tree_leaves_the_path_unresolved(tmp_path, compose):
    d = _env_tree(tmp_path, compose, "OTHER=1\n")
    assert d.param_value("load.go", "load", "p") is None
    assert not d.body_reads("load.go", "load", ["rates.yaml"])
