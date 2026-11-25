"""Dry run 3: a config-only change adds no content-pinning test, and lesson ids never land in
code (remediation_guards, software_factory.commit_change)."""
from __future__ import annotations

from boss_engine.services import remediation_guards as rg
from boss_engine.services.software_factory import commit_change

ID = "kgn_782d6b9423394e24bff0"


def test_a_lesson_id_in_a_new_comment_is_removed():
    src = ("package main\n\n// The secret is gone (" + ID + ").\n"
           "func f() {} // see " + ID + "\n")
    out = rg.strip_lesson_ids(src)
    assert "kgn_" not in out
    assert "// The secret is gone." in out
    assert out.endswith("func f() {} // see\n")


def test_ids_already_on_the_branch_and_ids_in_code_are_kept():
    line = "# per " + ID + "\n"
    assert rg.strip_lesson_ids(line, base=line) == line
    code = 'FIXTURE = "' + ID + '"\n'
    assert rg.strip_lesson_ids(code) == code
    assert rg.strip_lesson_ids("no ids here\n") == "no ids here\n"


class _Host:
    def __init__(self, on_branch=None):
        self.on_branch = on_branch or {}
        self.committed = {}

    def read_text_file(self, ref, path):
        if path not in self.on_branch:
            raise FileNotFoundError(path)
        return self.on_branch[path]

    def commit_files(self, branch, files, message):
        self.committed.update(files)
        return "sha1"


def test_commit_change_strips_ids_from_added_comment_lines_only():
    old = "# kept " + ID + "\n"
    host = _Host({"a.py": old})
    commit_change(host, "b", [("a.py", old + "x = 1  # from " + ID + "\n"),
                              ("b.go", "// lesson " + ID + "\n"),
                              ("c.bin", None)], "m")
    assert host.committed["a.py"] == old + "x = 1  # from\n"
    assert host.committed["b.go"] == "// lesson\n"
    assert host.committed["c.bin"] is None


def test_a_new_test_that_reads_the_fixed_file_is_named(tmp_path):
    (tmp_path / "cmd").mkdir()
    (tmp_path / "cmd" / "pin_test.go").write_text(
        'b, _ := os.ReadFile("config/tenants/lumen-pay.yaml")\n')
    (tmp_path / "cmd" / "unit_test.go").write_text("func TestX(t *testing.T) {}\n")
    (tmp_path / "cmd" / "old_test.go").write_text('"lumen-pay.yaml"\n')
    contract = {"input": "config/tenants/lumen-pay.yaml",
                "output": "config/tenants/lumen-pay.yaml"}
    found = rg.pinning_tests(tmp_path, ["cmd/pin_test.go", "cmd/unit_test.go", "cmd/old_test.go"],
                             contract, preexisting=["cmd/old_test.go"])
    assert found == ["cmd/pin_test.go"]


def test_the_brief_rule_says_no_new_test_file():
    assert "NO new test file" in rg.CONFIG_ONLY_TESTS_RULE
