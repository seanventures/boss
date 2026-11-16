"""A loader is code that READS the item's file, found where the evidence points (AR-23 review).

The reviewer's probes of #155, each reproduced here:

  1. HIGH — `rules/naming.yaml`, a lint.go that never reads it and a routes.go with only
     `mux.Handle("/rules", …)`: any non-test file that MENTIONED the folder counted as the
     loader, so the item read MET. A loader is now the cited file, a file defining a symbol the
     evidence cites, or a function one of those calls — and its code must read the file (embed,
     open, load, glob it; its folder only inside a read call or through a glob in that file).
  2. With no data file named, any defined `file:Symbol` counted as the loader. It now must read
     some data or config file.
  7. The tree was read again for every answer of the outer layer (parse_exercised): it is read
     once per check.
  8. A block comment opened mid-line (`x := 1 /* …` then `func Hidden()`), a definition written
     inside a string (`"func Fake() {}"`) and a Python docstring were taken for definitions.
"""
from __future__ import annotations

from pathlib import Path

from boss_engine.services import acceptance_gate as ag
from boss_engine.services.acceptance import verdict as v

ITEM = "A rulebook in YAML, `rules/naming.yaml`, that the linter reads."


def _tree(root: Path, files: dict) -> Path:
    for rel, body in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(body, encoding="utf-8")
    return root


PROBE = {
    "rules/naming.yaml": "rules:\n  - id: kebab-case\n",
    "internal/lint/lint.go": ("package lint\n\nimport \"fmt\"\n\n"
                              "func Check(path string) error { fmt.Println(\"lint\", path); "
                              "return nil }\n"),
    "internal/server/routes.go": ("package server\n\nfunc Routes(mux *http.ServeMux) {\n"
                                  "\tmux.Handle(\"/rules\", rulesHandler())\n}\n"),
}


# ── 1: a mention of the folder is not a read ───────────────────────────────────────────────

def test_a_folder_route_is_not_the_rulebooks_loader(tmp_path):
    root = _tree(tmp_path, PROBE)
    why = ("`rules/naming.yaml` has the rules; the linter is `internal/lint/lint.go:Check`, "
           "served at `internal/server/routes.go`.")
    assert "loader not named" in v.wiring_problem(root, why, item_text=ITEM, needs_loader=True)
    claim = "rules/naming.yaml; LOADED BY internal/lint/lint.go:Check"
    assert "does not name rules/naming.yaml" in v.wiring_problem(
        root, claim, item_text=ITEM, needs_loader=True)
    # The whole check reads it PARTIAL, as the reviewer's probe expected.
    got = ag.parse_acceptance(f"ITEM 1: MET — {why}\n",
                              [{"n": 1, "text": ITEM, "needs_loader": True}], root=root)[0]
    assert got["status"] == "partial", got["evidence"]


def test_a_reader_elsewhere_in_the_tree_is_not_the_cited_loader(tmp_path):
    """Code that reads the file but that the evidence does not point at is not named."""
    root = _tree(tmp_path, {**PROBE, "tools/dump.go": (
        "package tools\n\nfunc Dump() { b, _ := os.ReadFile(\"rules/naming.yaml\"); "
        "fmt.Print(b) }\n")})
    why = "`rules/naming.yaml`; linted by `internal/lint/lint.go:Check`."
    assert "loader not named" in v.wiring_problem(root, why, item_text=ITEM, needs_loader=True)


def test_reading_the_file_holds_however_it_is_read(tmp_path):
    why = "`rules/naming.yaml`; read by `internal/lint/lint.go:Check`."
    for body in (
        # a path string in code that opens it
        "package lint\n\nfunc Check() error { b, err := os.ReadFile(\"rules/naming.yaml\"); "
        "return use(b, err) }\n",
        # the folder inside a read call
        "package lint\n\nfunc Check() error { es, err := os.ReadDir(\"rules\"); "
        "return use(es, err) }\n",
        # the folder through a glob in the same file
        "package lint\n\nfunc Check() error { fs, _ := filepath.Glob(filepath.Join(\"rules\", "
        "\"*.yaml\")); return use(fs) }\n",
    ):
        root = _tree(tmp_path / str(abs(hash(body))), {**PROBE, "internal/lint/lint.go": body})
        assert v.wiring_problem(root, why, item_text=ITEM, needs_loader=True) is None, body
    # Through a function the cited file calls (docs.go → rules.Load, AR-23 item 25).
    root = _tree(tmp_path / "hop", {**PROBE,
        "internal/lint/lint.go": "package lint\n\nimport \"northgate/rules\"\n\n"
                                 "func Check() error { rs, _ := rules.Load(); return use(rs) }\n",
        "rules/loader.go": "package rules\n\n//go:embed naming.yaml\nvar raw []byte\n\n"
                           "func Load() ([]Rule, error) { return parse(raw) }\n"})
    assert v.wiring_problem(root, why, item_text=ITEM, needs_loader=True) is None


# ── 2: with no data file named, a defined function alone is not a loader ──────────────────

def test_a_defined_function_that_reads_nothing_is_not_a_loader(tmp_path):
    root = _tree(tmp_path, PROBE)
    item = "The linter's rules are loaded from configuration, not hard-coded."
    why = "Loaded in `internal/lint/lint.go:Check`."
    assert "loader not named" in v.wiring_problem(root, why, item_text=item, needs_loader=True)
    (root / "internal/lint/lint.go").write_text(
        "package lint\n\nfunc Check() error { b, err := os.ReadFile(\"lint.yaml\"); "
        "return use(b, err) }\n", encoding="utf-8")
    assert v.wiring_problem(root, why, item_text=item, needs_loader=True) is None


# ── 7: the tree is read once per check ─────────────────────────────────────────────────────

def test_the_outer_layer_reads_the_tree_once(tmp_path, monkeypatch):
    root = _tree(tmp_path, {**PROBE, "internal/lint/lint.go": (
        "package lint\n\nfunc Check() error { b, err := os.ReadFile(\"rules/naming.yaml\"); "
        "return use(b, err) }\n")})
    seen: dict[str, int] = {}
    real = v._lex

    def counting(text, path=""):
        seen[path] = seen.get(path, 0) + 1
        return real(text, path)

    monkeypatch.setattr(v, "_lex", counting)
    items = [{"n": n, "text": ITEM, "needs_loader": True} for n in (1, 2, 3)]
    reply = "\n".join(f"ITEM {n}: MET — `rules/naming.yaml` read by "
                      "`internal/lint/lint.go:Check`" for n in (1, 2, 3))
    got = v.parse_exercised(reply, items, root=root, exec_log={})
    assert [i["status"] for i in got] == ["met"] * 3, [i["evidence"] for i in got]
    assert seen and max(seen.values()) == 1, seen


# ── 8: comments and strings are read the way the language reads them ──────────────────────

def test_a_block_comment_opened_mid_line_hides_what_it_holds(tmp_path):
    root = _tree(tmp_path, {"internal/lint/lint.go": (
        "package lint\n\nvar x = 1 /* old code:\nfunc Hidden() {}\n*/\n\nfunc Real() {}\n")})
    assert v.evidence_paths(root, "`lint.Hidden`") == ([], ["lint.Hidden"])
    assert v.evidence_paths(root, "`lint.Real`") == (["internal/lint/lint.go"], [])


def test_a_definition_inside_a_string_is_not_one(tmp_path):
    root = _tree(tmp_path, {
        "internal/lint/lint.go": "package lint\n\nvar tpl = \"func Fake() {}\"\n",
        "app/tool.py": ('def real():\n    """def Ghost(): documented, not defined"""\n'
                        "    return 'class Phantom: pass'\n")})
    assert v.evidence_paths(root, "`lint.Fake`")[0] == []
    assert v.evidence_paths(root, "`tool.Ghost`")[0] == []
    assert v.evidence_paths(root, "`tool.Phantom`")[0] == []
    assert v.evidence_paths(root, "`tool.real`") == (["app/tool.py"], [])


def test_a_string_holding_a_comment_marker_keeps_the_code_after_it(tmp_path):
    root = _tree(tmp_path, {"internal/web/client.go": (
        "package web\n\nvar base = \"http://example.test\"; func Fetch() {}\n")})
    assert v.evidence_paths(root, "`web.Fetch`") == (["internal/web/client.go"], [])


# ── the one hop is from the cited symbol's body, through an import or a type (re-review) ──

LOADER = {"rules/loader.go": "package rules\n\n//go:embed naming.yaml\nvar raw []byte\n\n"
                             "func Load() ([]Rule, error) { return parse(raw) }\n"}


def test_a_dead_function_beside_the_cited_one_is_not_followed(tmp_path):
    root = _tree(tmp_path, {**PROBE, **LOADER, "internal/lint/lint.go": (
        "package lint\n\nimport \"northgate/rules\"\n\n"
        "func Check(path string) error {\n\treturn nil\n}\n\n"
        "func unusedDebug() {\n\trules.Load()\n}\n")})
    why = "`rules/naming.yaml`; read by `internal/lint/lint.go:Check`."
    assert "loader not named" in v.wiring_problem(root, why, item_text=ITEM, needs_loader=True)
    # The same call inside the cited function is the loader, one hop away.
    (root / "internal/lint/lint.go").write_text(
        "package lint\n\nimport \"northgate/rules\"\n\n"
        "func Check(path string) error {\n\t_, err := rules.Load()\n\treturn err\n}\n",
        encoding="utf-8")
    assert v.wiring_problem(root, why, item_text=ITEM, needs_loader=True) is None


def test_a_local_variable_is_never_a_package(tmp_path):
    root = _tree(tmp_path, {**PROBE, "internal/lint/lint.go": (
        "package lint\n\nfunc Check(path string) error {\n\tl := newLinter()\n"
        "\treturn l.Load()\n}\n"), "internal/zrules/loader.go": (
        "package zrules\n\nfunc Load() error {\n\tl := 1\n\t_ = l\n"
        "\t_, err := os.ReadFile(\"rules/naming.yaml\")\n\treturn err\n}\n")})
    defs = v._Definitions(root)
    assert defs.symbol_file("l.Load") is None
    assert defs.symbol_file("zrules.Load") == "internal/zrules/loader.go"
    why = "`rules/naming.yaml`; read by `internal/lint/lint.go:Check`."
    assert "loader not named" in v.wiring_problem(root, why, item_text=ITEM, needs_loader=True)


def test_a_method_on_a_type_is_followed(tmp_path):
    root = _tree(tmp_path, {**PROBE, "internal/lint/lint.go": (
        "package lint\n\nfunc Check(path string) error {\n\treturn Rulebook.Read(path)\n}\n"),
        "internal/lint/rulebook.go": (
        "package lint\n\ntype Rulebook struct{}\n\nfunc (Rulebook) Read(p string) error {\n"
        "\t_, err := os.ReadFile(\"rules/naming.yaml\")\n\treturn err\n}\n")})
    why = "`rules/naming.yaml`; read by `internal/lint/lint.go:Check`."
    assert v.wiring_problem(root, why, item_text=ITEM, needs_loader=True) is None
