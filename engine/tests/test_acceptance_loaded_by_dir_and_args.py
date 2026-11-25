"""BOSSAPP-1353: a data file counts as loaded when its directory is read, or when a test /
Makefile / compose / package.json / README command hands it to the reader; a file nobody reads
still fails. Tree shape: the gateway (config/tenants/*.yaml read by directory) and gwlint (files
named on its command line)."""
from __future__ import annotations

from pathlib import Path

from boss_engine.services.acceptance import verdict as v

LOADER = '''package config

import (
\t"os"
\t"path/filepath"
)

func Load(dir string) error {
\ttenantsDir := filepath.Join(dir, "tenants")
\tentries, err := os.ReadDir(tenantsDir)
\t_ = entries
\treturn err
}
'''
LINT_DOC = '''package lint

import "os"

func LoadDoc(path string) error {
\t_, err := os.ReadFile(path)
\treturn err
}
'''
GWLINT_MAIN = '''package main

import (
\t"os"
\t"northgate/internal/lint"
)

func main() { lint.LoadDoc(os.Args[1]) }
'''


def _tree(root: Path, files: dict) -> Path:
    for rel, body in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(body, encoding="utf-8")
    return root


BASE = {
    "internal/config/loader.go": LOADER,
    "config/tenants/lumen-pay.yaml": "id: lumen-pay\n",
    "internal/lint/doc.go": LINT_DOC,
    "cmd/gwlint/main.go": GWLINT_MAIN,
    "examples/lumen-pay.bad.yaml": "id: x\n",
    "orphans/unused.yaml": "a: 1\n",
}
ITEM_T = "A tenant file `config/tenants/lumen-pay.yaml` that the gateway loads."
ITEM_E = "An example `examples/lumen-pay.bad.yaml` that gwlint reads."


def _problem(root, claim, item):
    return v.wiring_problem(root, claim, item_text=item, needs_loader=True)


def test_a_directory_read_loads_every_file_of_the_directory(tmp_path):
    root = _tree(tmp_path, BASE)
    why = "config/tenants/lumen-pay.yaml; LOADED BY internal/config/loader.go:Load"
    assert _problem(root, why, ITEM_T) is None


def test_a_directory_read_on_another_folder_does_not_load_it(tmp_path):
    root = _tree(tmp_path, {**BASE, "config/partners/p.yaml": "a: 1\n"})
    why = "config/partners/p.yaml; LOADED BY internal/config/loader.go:Load"
    assert "does not name" in _problem(root, why, "`config/partners/p.yaml` is loaded.")


def test_a_directory_read_that_filters_by_another_extension_does_not_load_it(tmp_path):
    loader = LOADER.replace('os.ReadDir(tenantsDir)', 'os.ReadDir(tenantsDir)\n\t_ = ".json"')
    root = _tree(tmp_path, {**BASE, "internal/config/loader.go": loader})
    why = "config/tenants/lumen-pay.yaml; LOADED BY internal/config/loader.go:Load"
    assert "does not name" in _problem(root, why, ITEM_T)


def test_a_file_passed_by_a_makefile_target_to_the_reader_is_loaded(tmp_path):
    root = _tree(tmp_path, {**BASE, "Makefile": "lint:\n\tgo run ./cmd/gwlint validate "
                            "examples/lumen-pay.bad.yaml\n"})
    why = "examples/lumen-pay.bad.yaml; LOADED BY internal/lint/doc.go:LoadDoc"
    assert _problem(root, why, ITEM_E) is None


def test_a_file_passed_by_a_test_in_the_reader_cli_is_loaded(tmp_path):
    test = ('package main\n\nfunc TestBad() {\n\trun("validate", "../../examples/lumen-pay.bad.yaml")\n}\n')
    root = _tree(tmp_path, {**BASE, "cmd/gwlint/gwlint_test.go": test})
    why = "examples/lumen-pay.bad.yaml; LOADED BY internal/lint/doc.go:LoadDoc"
    assert _problem(root, why, ITEM_E) is None


def test_a_file_passed_by_a_readme_command_a_package_script_or_compose_is_loaded(tmp_path):
    why = "examples/lumen-pay.bad.yaml; LOADED BY internal/lint/doc.go:LoadDoc"
    for name, body in {
        "README.md": "## Quick start\n\n```\ngwlint validate examples/lumen-pay.bad.yaml\n```\n",
        "package.json": '{"scripts": {"lint": "gwlint validate examples/lumen-pay.bad.yaml"}}',
        "docker-compose.yml": "services:\n  lint:\n    command: gwlint examples/lumen-pay.bad.yaml\n",
    }.items():
        root = _tree(tmp_path / name.replace(".", "_"), {**BASE, name: body})
        assert _problem(root, why, ITEM_E) is None, name


def test_python_and_node_readers_handed_a_file_or_reading_its_directory(tmp_path):
    py = {"app/loader.py": "import os\n\ndef load_all():\n    return os.listdir('conf/tenants')\n",
          "conf/tenants/a.yaml": "a: 1\n"}
    root = _tree(tmp_path / "py", py)
    assert _problem(root, "conf/tenants/a.yaml; LOADED BY app/loader.py:load_all",
                    "`conf/tenants/a.yaml` is loaded.") is None
    js = {"src/loader.js": "const fs = require('fs');\nfunction loadAll() {\n"
                           "  return fs.readdirSync('conf/tenants');\n}\n",
          "conf/tenants/a.json": "{}"}
    root = _tree(tmp_path / "js", js)
    assert _problem(root, "conf/tenants/a.json; LOADED BY src/loader.js:loadAll",
                    "`conf/tenants/a.json` is loaded.") is None
    cli = {"tools/lint.py": "def check(p):\n    return open(p).read()\n",
           "samples/bad.yaml": "a: 1\n",
           "tests/test_lint.py": "from tools.lint import check\n\ndef test_it():\n"
                                 "    check('samples/bad.yaml')\n"}
    root = _tree(tmp_path / "cli", cli)
    assert _problem(root, "samples/bad.yaml; LOADED BY tools/lint.py:check",
                    "`samples/bad.yaml` is read.") is None


def test_a_file_nobody_reads_still_fails(tmp_path):
    # Mentioned in a README and a Makefile with no reader, and in another folder's loader.
    root = _tree(tmp_path, {**BASE, "README.md": "Files live in orphans/unused.yaml.\n",
                            "Makefile": "clean:\n\trm -f orphans/unused.yaml\n"})
    for loader, sym in (("internal/config/loader.go", "Load"), ("internal/lint/doc.go", "LoadDoc")):
        why = f"orphans/unused.yaml; LOADED BY {loader}:{sym}"
        assert "does not name" in _problem(root, why, "`orphans/unused.yaml` is loaded.")
    # No LOADED BY: the evidence names the loader, the file is still not wired.
    got = _problem(root, "orphans/unused.yaml exists; see internal/config/loader.go:Load",
                   "`orphans/unused.yaml` is loaded.")
    assert got and "loader not named" in got


def test_the_real_fixture_tenant_is_loaded_by_the_gateways_directory_read():
    fx = Path(__file__).parent / "fixtures" / "acceptance_ar29" / "tree"
    why = "config/tenants/lumen-pay.yaml; LOADED BY internal/config/loader.go:Load"
    assert _problem(fx, why, ITEM_T) is None


# ── the replayed shape (a real gateway project: BOSSAPP-1353 review) ─────────────────────────

FIXTURE = Path(__file__).parent / "fixtures" / "acceptance_loaded_by" / "tree"
GW_LOAD = "LOADED BY internal/config/loader.go:Load"
LINT_LOAD = "LOADED BY internal/lint/doc.go:LoadDoc"


def _fx(why, item):
    return v.wiring_problem(FIXTURE, why, item_text=item, needs_loader=True)


def test_the_replayed_tree_passes_what_is_really_wired():
    assert _fx(f"config/tenants/lumen-pay.yaml; {GW_LOAD}", ITEM_T) is None
    assert _fx(f"examples/lumen-pay.bad.yaml; {LINT_LOAD}", ITEM_E) is None     # a test hands it
    assert _fx(f"examples/lumen-pay.good.yaml; {LINT_LOAD}",
               "An example `examples/lumen-pay.good.yaml`.") is None            # Makefile + README
    # AC-22: an item naming no file; the CLI reader reads the path its caller gives it.
    assert _fx(f"`cmd/gwlint/main.go` runs it; {LINT_LOAD}",
               "**`gwlint` CLI**: `gwlint validate <file|dir>` prints a report.") is None


def test_the_replayed_trees_unread_files_all_fail():
    for f, ld in (("config/legacy/old.yaml", GW_LOAD),      # only README prose / a comment
                  ("config/tenants/ghost.json", GW_LOAD),   # the loader's filter is .yaml
                  ("data/unread.yaml", GW_LOAD), ("data/unread.yaml", LINT_LOAD),
                  ("examples/unused.yaml", LINT_LOAD), ("config/legacy/old.yaml", LINT_LOAD)):
        got = _fx(f"{f}; {ld}", f"`{f}` is loaded.")
        assert got and "does not name" in got, (f, ld, got)


def test_a_bare_glob_a_comment_and_a_folder_alone_wire_nothing(tmp_path):
    files = {**BASE,
             "Makefile": "lint:\n\tgo run ./cmd/gwlint validate *\n\tgo run ./cmd/gwlint *.yaml\n"
                         "\tgo run ./cmd/gwlint examples\n",
             "cmd/gwlint/gwlint_test.go": "package main\n// examples/lumen-pay.bad.yaml\n"
                                          "func TestX() {}\n"}
    root = _tree(tmp_path, files)
    why = "examples/lumen-pay.bad.yaml; LOADED BY internal/lint/doc.go:LoadDoc"
    got = _problem(root, why, ITEM_E)
    assert got and "does not name" in got, got


def test_a_bare_list_or_walk_call_is_not_a_directory_read(tmp_path):
    loader = ("package config\n\nfunc Load(dir string) error {\n\tx := list(\"tenants\")\n"
              "\twalk(\"tenants\")\n\treturn nil\n}\n")
    root = _tree(tmp_path, {**BASE, "internal/config/loader.go": loader})
    why = "config/tenants/lumen-pay.yaml; LOADED BY internal/config/loader.go:Load"
    got = _problem(root, why, ITEM_T)
    assert got and "does not name" in got, got


def test_a_cli_reader_needs_an_item_about_the_command_line(tmp_path):
    # The item names no file and is not about command-line input: LoadDoc reading a path its
    # callers pass proves nothing about "the rules YAML".
    got = _fx(f"Rate limit rules apply. {LINT_LOAD}", "Rate limit rules come from the rules YAML.")
    assert got and "reads no data or config file" in got, got
    # A reader nothing in a command calls (only tests / library code) is no CLI reader either.
    root = _tree(tmp_path, {"internal/lint/doc.go": LINT_DOC,
                            "internal/lint/engine.go": "package lint\n\nfunc Run(p string) {"
                                                       " LoadDoc(p) }\n"})
    got = v.wiring_problem(root, f"`gwlint validate <file>`; {LINT_LOAD}",
                           item_text="**`gwlint` CLI**: `gwlint validate <file|dir>`.",
                           needs_loader=True)
    assert got and "reads no data or config file" in got, got
