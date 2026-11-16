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


#: Every real check replays 30/0. Item 11, where the reviewer cited only the code that USES
#: the route config (applyCORS, Against), holds through the value passed on: main builds
#: `store`, hands it to config.ReloadOnce (→ Load reads the YAML) and to proxy.NewHandler,
#: whose ServeHTTP calls applyCORS — never because a config type is merely taken (#171 (a)).
EXPECT = {
    "acc_b2239214c5bc45a9b4c4": ("crun_673c3dcd34d6447e924b", 30, []),
    "acc_2841e1f5536945928a79": ("crun_673c3dcd34d6447e924b", 30, []),
    "acc_2c775eb7064541238af1": ("crun_f4360a7195f945a58b62", 30, []),
    "acc_867e506d842a445ca76c": ("crun_f4360a7195f945a58b62", 30, []),
}


@pytest.mark.parametrize("check_id", sorted(EXPECT))
def test_every_met_the_reviewer_evidenced_stands(check_id):
    run, met, partial = EXPECT[check_id]
    check = DATA["checks"][check_id]
    assert check["run"] == run
    downgraded = {a["n"] for a in check["answers"] if a["recorded"] == "partial"}
    assert check["recorded"]["partial"] == len(downgraded) >= 3
    rec = ag.judge(DATA["items"], _reply(check["answers"]), {}, root=TREE)
    assert rec["counts"]["met"] == met and rec["partial"] == partial, [
        (i["n"], i["evidence"][-160:]) for i in rec["items"] if i["status"] != "met"]
    for n in partial:
        item = next(i for i in rec["items"] if i["n"] == n)
        assert "loader not named" in item["evidence"]


# ── (a) package patterns and commands are not file paths ────────────────────────────────────

@pytest.mark.parametrize("cited", [
    "`go vet ./...` and `go test ./...` pass",
    "`go test -race ./internal/config/...` passes",
])
def test_a_package_pattern_is_neither_evidence_nor_missing(cited):
    assert v.evidence_paths(TREE, cited) == ([], [])


def test_a_name_list_is_its_parts():
    found, absent = v.evidence_paths(TREE, "Makefile/docker-compose targets exist")
    assert absent == [] and {"Makefile", "docker-compose.yml"} <= set(found)


def test_python_and_maven_module_arguments(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app/main.py").write_text("def run():\n    pass\n")
    (tmp_path / "services/api/src").mkdir(parents=True)
    (tmp_path / "services/api/src/App.java").write_text("class App {}\n")
    assert v.evidence_paths(tmp_path, "`python -m app.main` runs") == (["app/main.py"], [])
    # A module FOLDER is not file evidence, and not a missing file.
    assert v.evidence_paths(tmp_path, "`mvn -pl services/api test` passes") == ([], [])
    assert v.evidence_paths(tmp_path, "`go test ./pkg/...`") == ([], [])


@pytest.mark.parametrize("item,why,status", [
    # (d) the probe: a test run is not evidence for anything but the test run itself
    ("Secrets are never written to logs.", "`go test ./...` passes", "partial"),
    ("The gateway retries an upstream once.", "`go test -race ./internal/proxy/...` passes",
     "partial"),
    # an item that IS the suite run is evidenced by that run
    ("All tests pass; the README quick start works as written.",
     "`go vet ./...` clean, `go test ./...` all packages pass (verified live).", "met"),
])
def test_a_test_run_evidences_only_the_suite_item(item, why, status):
    got = v.parse_acceptance(f"ITEM 1: MET — {why}\n", [{"n": 1, "text": item}], root=TREE)[0]
    assert got["status"] == status, got["evidence"]
    files, _no = v._file_refs(TREE, why)
    assert all(f["ref"] != "." for f in files), "the repository root is never file evidence"


# ── (b) the loader is followed from the cited code, the #155 negatives still fail ───────────

ITEM2 = DATA["items"][1]["text"]


def test_a_folder_read_through_a_path_join_is_a_read():
    rel = "internal/config/loader.go"
    lexed = v._code_lines((TREE / rel).read_text(), rel)
    # The join's base is a parameter: alone, "tenants" is any tenants folder (#171 review (b)).
    assert not v._reads(lexed, "config/tenants/lumen-pay.yaml", rel)
    # Every caller passes "config" (main's CONFIG_DIR default, through ReloadOnce): it reads.
    defs = v._Definitions(TREE)
    assert defs.param_value(rel, "Load", "dir") == "config"
    assert defs.body_reads(rel, "Load", ["config/tenants/lumen-pay.yaml"])
    # The extension filter is the read call's own: a .json filter never reads the YAML.
    json_only = v._bind_joins(lexed, {"dir": "config"}).replace('".yaml"', '".json"') \
        .replace('".yml"', '".json"')
    assert not v._reads(json_only, "config/tenants/lumen-pay.yaml", rel)


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


def _tree(tmp_path, files):
    for rel, body in files.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(body)
    return tmp_path


LIMITS = "Rate limits come from `config/limits.yaml`, not from code."


def test_a_config_type_merely_taken_is_not_a_loader(tmp_path):
    """(a1) yaml-tagged RateLimit, a Load() nobody calls, main builds RateLimit{Rate: 5}."""
    root = _tree(tmp_path, {
        "config/limits.yaml": "rate: 5\n",
        "internal/config/types.go": "package config\n\ntype RateLimit struct {\n"
                                    "\tRate int `yaml:\"rate\"`\n}\n",
        "internal/config/load.go": "package config\n\nfunc Load() (*RateLimit, error) {\n"
                                   "\tb, err := os.ReadFile(\"config/limits.yaml\")\n"
                                   "\treturn parse(b), err\n}\n",
        "internal/proxy/limit.go": "package proxy\n\nimport \"northgate/internal/config\"\n\n"
                                   "func Limit(r config.RateLimit) bool { return r.Rate > 0 }\n",
        "cmd/gw/main.go": "package main\n\nimport (\n\t\"northgate/internal/config\"\n\t\"northgate/internal/proxy\"\n)\n\n"
                          "func main() {\n\tproxy.Limit(config.RateLimit{Rate: 5})\n}\n"})
    why = "`internal/proxy/limit.go:Limit` takes `config.RateLimit`; built in `cmd/gw/main.go`."
    assert "loader not named" in v.wiring_problem(root, why, item_text=LIMITS,
                                                   needs_loader=True)
    # main calling the loader too changes nothing while the limit it passes is built by hand
    (root / "cmd/gw/main.go").write_text(
        "package main\n\nimport (\n\t\"northgate/internal/config\"\n\t\"northgate/internal/proxy\"\n)\n\nfunc main() {\n"
        "\tcfg, _ := config.Load()\n\t_ = cfg\n\tproxy.Limit(config.RateLimit{Rate: 5})\n}\n")
    why = "`internal/proxy/limit.go:Limit` takes `config.RateLimit`."
    assert "loader not named" in v.wiring_problem(root, why, item_text=LIMITS,
                                                   needs_loader=True)
    # the loaded value passed on to the cited code is the wiring
    (root / "cmd/gw/main.go").write_text(
        "package main\n\nimport (\n\t\"northgate/internal/config\"\n\t\"northgate/internal/proxy\"\n)\n\nfunc main() {\n"
        "\tcfg := config.Load()\n\tproxy.Limit(cfg)\n}\n")
    assert v.wiring_problem(root, why, item_text=LIMITS, needs_loader=True) is None


def test_a_sibling_beside_a_dead_loader_is_not_the_loader(tmp_path):
    """(a2) the cited Defaults() reads nothing; a dead Load() in the same file does."""
    root = _tree(tmp_path, {
        "config/limits.yaml": "rate: 5\n",
        "internal/config/config.go": "package config\n\nfunc Defaults() *RateLimit {\n"
                                     "\treturn &RateLimit{Rate: 5}\n}\n\nfunc Load() error {\n"
                                     "\t_, err := os.ReadFile(\"config/limits.yaml\")\n"
                                     "\treturn err\n}\n"})
    for why in ("LOADED BY `internal/config/config.go:Defaults`",
                "loaded in `internal/config/config.go:Defaults`"):
        assert v.wiring_problem(root, why, item_text=LIMITS, needs_loader=True), why


def test_a_folder_named_like_the_data_folder_is_not_it(tmp_path):
    """(b) a cache dir called tenants is not config/tenants; the extension filter is the read
    call's own."""
    item = "Editing `config/tenants/acme.yaml` is picked up without restart."
    base = {"config/tenants/acme.yaml": "id: acme\n"}
    cache = _tree(tmp_path / "cache", {**base, "internal/cache/cache.go": (
        "package cache\n\nfunc Load() error {\n\tdir := filepath.Join(os.TempDir(), "
        "\"tenants\")\n\tentries, err := os.ReadDir(dir)\n\t_ = entries\n\treturn err\n}\n")})
    why = "LOADED BY `internal/cache/cache.go:Load`"
    assert "does not name" in v.wiring_problem(cache, why, item_text=item, needs_loader=True)
    json_only = _tree(tmp_path / "json", {**base, "internal/config/load.go": (
        "package config\n\nvar ext = \".yaml\"\n\nfunc Load() error {\n"
        "\tentries, err := os.ReadDir(\"config/tenants\")\n\tfor _, e := range entries {\n"
        "\t\tif strings.HasSuffix(e.Name(), \".json\") { read(e) }\n\t}\n\treturn err\n}\n")})
    why = "LOADED BY `internal/config/load.go:Load`"
    assert v.wiring_problem(json_only, why, item_text=item, needs_loader=True)
    elsewhere = _tree(tmp_path / "elsewhere", {**base, "internal/config/load.go": (
        "package config\n\nfunc Load() error {\n\tentries, err := os.ReadDir(\"config/tenants\")"
        "\n\tfor _, e := range entries {\n\t\tif strings.HasSuffix(e.Name(), \".json\") "
        "{ read(e) }\n\t}\n\treturn err\n}\n\nfunc Other() bool {\n\treturn "
        "strings.HasSuffix(x, \".yaml\")\n}\n")})
    assert v.wiring_problem(elsewhere, why, item_text=item, needs_loader=True), \
        "a .yaml string in another function is not this read's filter"
    whole = _tree(tmp_path / "whole", {**base, "internal/config/load.go": (
        "package config\n\nfunc Load(dir string) error {\n\tentries, err := "
        "os.ReadDir(filepath.Join(dir, \"tenants\"))\n\t_ = entries\n\treturn err\n}\n"),
        "cmd/gw/main.go": "package main\n\nimport \"northgate/internal/config\"\n\n"
                          "func main() {\n\tdir := getenv(\"CONFIG_DIR\", \"config\")\n"
                          "\tconfig.Load(dir)\n}\n"})
    assert v.wiring_problem(whole, why, item_text=item, needs_loader=True) is None, \
        "a join whose base every caller sets to config is config/tenants"


# ── (c) a judgement item still stands only on evidence ─────────────────────────────────────

def test_a_judgement_item_needs_evidence_and_never_covers_secrets_or_logging():
    naming = next(i for i in DATA["items"] if i.get("judgement"))
    bare = "naming is plain and consistent; code reads cleanly for a non-author."
    got = v.parse_acceptance(f"ITEM {naming['n']}: MET — {bare}\n", [naming], root=TREE)[0]
    assert got["status"] == "partial" and "no file evidence cited" in got["evidence"]
    assert v._advisory(got), "a judgement PARTIAL is advisory, never failing"
    quoted = "naming across `proxy` and `auth` (`TokenBucket`, `ReloadState`) is plain."
    got = v.parse_acceptance(f"ITEM {naming['n']}: MET — {quoted}\n", [naming], root=TREE)[0]
    assert got["status"] == "met", got["evidence"]
    from boss_engine.services.acceptance import contract as ac

    for text in ("Secrets are never written to logs.", "Retries back off politely.",
                 "Errors read well to an operator.", "Auth failures are clear.",
                 "The security posture is sensible."):
        assert "judgement" not in ac.floor_tags(text, {"judgement": True}), text
    assert ac.floor_tags("Plain, concise naming.", {"judgement": True}) == {"judgement": True}


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
