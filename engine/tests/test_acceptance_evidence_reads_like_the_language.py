"""The acceptance evidence check reads code and commands the way their language does
(BOSSAPP-1329).

THE CASE (Northgate round 7b, 2026-11-16): with #164 the reviewer finally read the real tree,
and every PARTIAL left was the checker overruling the reviewer's own confirmed evidence —
build crun_673c3dcd 27 met / 3 partial, follow-up crun_f4360a71 24 / 6:

  (a) "cited paths not in the tree: ./..., Makefile/docker-compose" (items 5, 28): Go package
      patterns and a slash-joined list of names read as file paths;
  (b) "loader not named" / "does not name config/tenants/lumen-pay.yaml" (items 2, 11, 17, 25):
      the watcher reaches the loader that reads the tenants FOLDER through a path join; the
      generator reaches the embedded rulebook through `rulesyaml.Load()`; the handler takes
      the loaded config type; `internal/lint:RenderReadme` names a package, not a file;
  (c) "no file evidence cited" on the naming item (26), a judgement about the whole codebase;
  (d) the history cut item 11's reason mid-sentence.

The fixture is the real rows (the four acceptance checks of the two runs, read-only from the
laptop: each item's text and tags from the contract, each answer as the reviewer gave it) and a
minimal Go tree built from the change's branch: the loader chains verbatim, every other cited
file a stub with its package and definitions.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from boss_engine.services import acceptance_gate as ag
from boss_engine.services.acceptance import verdict as v

FIX = Path(__file__).parent / "fixtures" / "acceptance_ar29"
DATA = json.loads((FIX / "r7b_checks.json").read_text(encoding="utf-8"))
TREE = FIX / "tree"


def _reply(answers) -> str:
    return "\n\n".join(f"ITEM {a['n']}: {a['said']} — {a['evidence']}" for a in answers) \
        + "\n\nACCEPTANCE: MET\n"


@pytest.mark.parametrize("check_id,run", [
    ("acc_b2239214c5bc45a9b4c4", "crun_673c3dcd34d6447e924b"),
    ("acc_2841e1f5536945928a79", "crun_673c3dcd34d6447e924b"),
    ("acc_2c775eb7064541238af1", "crun_f4360a7195f945a58b62"),
    ("acc_867e506d842a445ca76c", "crun_f4360a7195f945a58b62"),
])
def test_every_met_the_reviewer_evidenced_stands(check_id, run):
    check = DATA["checks"][check_id]
    assert check["run"] == run
    downgraded = {a["n"] for a in check["answers"] if a["recorded"] == "partial"}
    assert check["recorded"]["partial"] == len(downgraded) >= 3
    rec = ag.judge(DATA["items"], _reply(check["answers"]), {}, root=TREE)
    assert rec["counts"] == {"met": 30, "partial": 0, "missing": 0, "unjudged": 0}, [
        (i["n"], i["evidence"][-160:]) for i in rec["items"] if i["status"] != "met"]
    assert rec["verdict"] == "met"


# ── (a) package patterns and commands are not file paths ────────────────────────────────────

@pytest.mark.parametrize("cited,expect", [
    ("`go vet ./...` and `go test ./...` pass", ["."]),
    ("`go test -race ./internal/config/...` passes", ["internal/config"]),
    ("Makefile/docker-compose targets exist", ["Makefile", "docker-compose.yml"]),
])
def test_package_patterns_and_name_lists_are_what_they_name(cited, expect):
    found, absent = v.evidence_paths(TREE, cited)
    assert absent == [] and set(expect) <= set(found), (found, absent)


def test_python_and_maven_module_arguments_name_their_code(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app/main.py").write_text("def run():\n    pass\n")
    (tmp_path / "services/api/src").mkdir(parents=True)
    (tmp_path / "services/api/src/App.java").write_text("class App {}\n")
    assert v.evidence_paths(tmp_path, "`python -m app.main` runs") == (["app/main.py"], [])
    assert v.evidence_paths(tmp_path, "`mvn -pl services/api test` passes") == (
        ["services/api"], [])
    # A pattern naming nothing in the tree is no evidence, and no missing file either.
    assert v.evidence_paths(tmp_path, "`go test ./pkg/...`") == ([], [])


# ── (b) the loader is followed from the cited code, the #155 negatives still fail ───────────

ITEM2 = DATA["items"][1]["text"]


def test_a_folder_read_through_a_path_join_is_a_read():
    code = (TREE / "internal/config/loader.go").read_text()
    lexed = v._code_lines(code, "internal/config/loader.go")
    assert v._reads(lexed, "config/tenants/lumen-pay.yaml", "internal/config/loader.go")
    # The extension filter must admit the file: a .json filter never reads the YAML.
    json_only = lexed.replace('".yaml"', '".json"').replace('".yml"', '".json"')
    assert not v._reads(json_only, "config/tenants/lumen-pay.yaml", "internal/config/loader.go")


def test_the_watcher_reaches_the_loader_two_calls_down():
    why = ("LOADED BY `internal/config/watcher.go:Watch` (polls `dir`/`tenants` mtimes, calls "
           "`ReloadOnce`→`Load`).")
    assert v.wiring_problem(TREE, why, item_text=ITEM2, needs_loader=True) is None


@pytest.mark.parametrize("files,why", [
    # dead code beside the cited function still never counts
    ({"internal/lint/lint.go": "package lint\n\nimport \"northgate/rules\"\n\nfunc Check() error {"
      "\n\treturn nil\n}\n\nfunc unusedDebug() {\n\trules.Load()\n}\n"},
     "`rules/naming.yaml`; read by `internal/lint/lint.go:Check`."),
    # a local variable is never a package
    ({"internal/lint/lint.go": "package lint\n\nfunc Check() error {\n\tl := newLinter()\n"
      "\treturn l.Load()\n}\n"},
     "`rules/naming.yaml`; read by `internal/lint/lint.go:Check`."),
    # a route string is not a read of the folder
    ({"internal/lint/lint.go": "package lint\n\nfunc Check() error { return nil }\n",
      "internal/server/routes.go": "package server\n\nfunc Routes(mux *http.ServeMux) {\n"
      "\tmux.Handle(\"/rules\", h())\n}\n"},
     "`rules/naming.yaml`; read by `internal/lint/lint.go:Check`, served at "
     "`internal/server/routes.go`."),
])
def test_the_155_negatives_still_fail(tmp_path, files, why):
    base = {"rules/naming.yaml": "rules: []\n",
            "rules/loader.go": "package rules\n\n//go:embed naming.yaml\nvar raw []byte\n\n"
                               "func Load() error { return nil }\n"}
    for rel, body in {**base, **files}.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(body)
    item = "A rulebook in YAML, `rules/naming.yaml`, that the linter reads."
    assert "loader not named" in v.wiring_problem(tmp_path, why, item_text=item,
                                                   needs_loader=True)


# ── (c) a judgement about the whole codebase needs no file ─────────────────────────────────

def test_a_judgement_item_needs_no_file_and_a_checkable_one_still_does():
    naming = next(i for i in DATA["items"] if i.get("judgement"))
    why = "naming across `internal/*` packages is plain and consistent."
    got = v.parse_acceptance(f"ITEM {naming['n']}: MET — {why}\n", [naming], root=TREE)[0]
    assert got["status"] == "met", got["evidence"]
    plain = {**naming, "judgement": False, "text": "The gateway logs every request."}
    got = v.parse_acceptance(f"ITEM {naming['n']}: MET — {why}\n", [plain], root=TREE)[0]
    assert got["status"] == "partial" and "no file evidence cited" in got["evidence"]


# ── (d) never cut mid-sentence ─────────────────────────────────────────────────────────────

def test_a_long_answer_is_cut_at_a_sentence_and_keeps_bosss_reason():
    reason = "(loader not named: this item's file must be read by the code (LOADED BY))"
    text = ("CORS: `internal/proxy/handler.go:applyCORS`. " * 20) + "The end " + reason
    out = v.clip(text, 400)
    assert len(out) <= 400 and out.endswith(reason)
    body = out[:-len(reason)].rstrip()
    assert body.endswith("applyCORS`. …"), body[-40:]
    history: list = []
    ag.keep_history(history, {"verdict": "not_met", "counts": {}, "items": [
        {"n": 11, "status": "partial", "evidence": text}]})
    assert history[0]["items"][0]["evidence"].endswith(reason)
