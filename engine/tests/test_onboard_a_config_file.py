"""R5: "Onboard a config file", built only from standard steps (requirement R5, PR #287).

Input file → Build (fix it) → Review → Merge → Staging (config deploy) → Contract tests, with two
standard pieces any workflow can use: the input type "Start from a file" (start_from_file; the
original is the change's first commit, so its diff reads original → fixed) and "Contract tests
after staging" (contract_test with no cases runs the repository's `.boss/contract_tests.yaml`).
No gateway-specific prompt: the checker, rules and guides come from the repository.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from boss_engine.services import contract_tests, finding_overrides, input_fidelity as f
from boss_engine.services import start_from_file as sff
from boss_engine.services import workflow_catalogue as wc
from boss_engine.services import workflow_outcomes
from boss_engine.services.workflow_capabilities import CAPABILITIES

FIX = Path(__file__).parent / "fixtures" / "validation"
TARGET = "config/tenants/lumen-pay.yaml"
ENTRY = next(e for e in wc.CATALOGUE if e["key"] == "onboard_config_file")


# ── the catalogue workflow ─────────────────────────────────────────────────────────────────────

def test_it_is_built_only_from_standard_steps_in_order():
    caps = [(s.get("binding") or {}).get("capability") for s in ENTRY["steps"]]
    assert caps == ["build", "review", "merge", "config_deploy", "contract_test"]
    assert all(c in CAPABILITIES for c in caps)
    assert [s["title"] for s in ENTRY["steps"] if s.get("title")] == [
        "Fix the file", "Staging", "Contract tests"]


def test_its_spec_says_checks_must_pass_and_starts_from_a_file():
    spec = wc.entry_spec(ENTRY)
    assert spec["checks_must_pass"] is True and spec["more_tests"] is False
    schema = spec["input_schema"]
    assert {"start_from", "target_path"} <= set(schema["required"])
    assert "start_from_content" in schema["properties"]
    build = spec["steps"][0]["binding"]["inputs"]
    assert build["start_from"] == {"from_input": "start_from"}
    assert spec["steps"][3]["binding"]["inputs"]["source_file"] == {"from_input": "target_path"}
    assert spec["steps"][4]["binding"]["inputs"] == {
        "base_url": {"from_step": "staging", "field": "deployed_url"}}


def test_no_gateway_specific_prompt():
    words = json.dumps(wc.entry_spec(ENTRY)["steps"]).lower()
    for word in ("gwlint", "lumen", "northgate", "tenant", "admin", "sk_live"):
        assert word not in words, word


def test_no_other_catalogue_entry_changed_shape():
    """Only the new entry carries the setting; every other entry's spec is as before."""
    others = [e for e in wc.CATALOGUE if e["key"] != ENTRY["key"]]
    assert all("checks_must_pass" not in wc.entry_spec(e) for e in others)
    assert all((s.get("binding") or {}).get("capability") != "merge"
               for e in others for s in e["steps"])


def test_merge_and_repo_contract_tests_are_standard_capabilities():
    assert CAPABILITIES["merge"].tool is None and not CAPABILITIES["merge"].is_gate
    assert workflow_outcomes.VERDICTS["merge"] is workflow_outcomes.awaited_work
    assert "cases" not in CAPABILITIES["contract_test"].required_inputs
    assert "cases" in CAPABILITIES["contract_test"].optional_inputs
    build = CAPABILITIES["build"].optional_inputs
    assert {"start_from", "start_from_content", "target_path"} <= set(build)
    assert "source_file" in CAPABILITIES["config_deploy"].optional_inputs


# ── "Start from a file" ────────────────────────────────────────────────────────────────────────

def test_an_upload_becomes_an_in_place_remediation_at_its_target():
    out = sff.apply({"title": "x", "start_from": "lumen-pay.yaml",
                     "start_from_content": "a: 1\n", "target_path": TARGET})
    assert set(out) == {"title", "remediate"}
    assert out["remediate"] == {"input": TARGET, "output": TARGET, "content": "a: 1\n",
                                "start_from": True}


def test_a_repo_path_is_fixed_in_place_unless_a_target_is_named():
    assert sff.remediate({"start_from": "./config/a.yaml"}) == {
        "input": "config/a.yaml", "output": "config/a.yaml", "original_path": "config/a.yaml",
        "start_from": True}
    assert sff.remediate({"start_from": "examples/a.yaml", "target_path": TARGET})["output"] \
        == TARGET
    assert sff.remediate({"title": "no file"}) is None


def test_a_path_outside_the_repo_is_refused():
    with pytest.raises(sff.StartFromFileError):
        sff.remediate({"start_from": "../etc/passwd"})


def test_an_explicit_remediate_wins():
    out = sff.apply({"remediate": {"input": "a"}, "start_from": "b"})
    assert out == {"remediate": {"input": "a"}}


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "ng"
    for d in ("config/tenants", ".boss"):
        (root / d).mkdir(parents=True)
    shutil.copy(FIX / "validators.yaml", root / ".boss" / "validators.yaml")
    return root


def _gwlint(root: Path, ran: list):
    def run(cmd: str):
        ran.append(cmd)
        if "--fix" in cmd:
            (root / cmd.split()[-1].strip("'")).write_text(
                (FIX / "lumen-pay.fixed.yaml").read_text())
            return SimpleNamespace(stdout="", exit_code=1)
        text = (root / cmd.split()[-2].strip("'")).read_text()
        if text == (FIX / "lumen-pay.bad.yaml").read_text():
            return SimpleNamespace(stdout=(FIX / "gwlint.before.json").read_text(), exit_code=1)
        if text == (FIX / "lumen-pay.fixed.yaml").read_text():
            return SimpleNamespace(stdout=(FIX / "gwlint.after.json").read_text(), exit_code=1)
        return SimpleNamespace(stdout='{"path": "x", "violations": []}', exit_code=0)
    return run


def _uploaded(tmp_path):
    root = _repo(tmp_path)
    spec = {"remediate": sff.remediate({
        "start_from": "lumen-pay.yaml", "target_path": TARGET,
        "start_from_content": (FIX / "lumen-pay.bad.yaml").read_text()})}
    return root, f.contract(spec, "", root)


def test_the_upload_is_the_original_and_is_placed_for_the_first_commit(tmp_path):
    root, c = _uploaded(tmp_path)
    bad = (FIX / "lumen-pay.bad.yaml").read_text()
    assert c["in_place"] and c["input"] == c["output"] == TARGET and c["from"] == "upload"
    assert c["_input_text"] == bad
    assert "content" not in f.public(c), "the uploaded text is not copied onto the run state"
    # A fresh change: the target is not on the base, so the original is placed (and committed).
    assert f.place_original(root, c, None) == bad
    assert (root / TARGET).read_text() == bad
    # Placed already: nothing more to commit.
    assert f.place_original(root, c, None) is None


def test_a_follow_up_build_never_puts_the_original_back(tmp_path):
    root, c = _uploaded(tmp_path)
    (root / TARGET).write_text("fixed: true\n")          # the change already carries fixes
    assert f.place_original(root, c, None) is None
    assert (root / TARGET).read_text() == "fixed: true\n"
    ran: list = []
    assert f.seed_auto_fix(root, c, _gwlint(root, ran)) is None
    assert c["auto_fix_seed"]["why"] == "the target already carries this change's fixes"


def test_the_fixer_seeds_in_place_and_the_record_reads_original_to_fixed(tmp_path):
    root, c = _uploaded(tmp_path)
    f.place_original(root, c, None)
    ran: list = []
    assert f.seed_auto_fix(root, c, _gwlint(root, ran))
    assert (root / TARGET).read_text() == (FIX / "lumen-pay.fixed.yaml").read_text()
    rec = f.run_validation(root, c, _gwlint(root, ran))
    assert not rec.get("guard"), rec.get("guard")
    assert len(rec["before"]["findings"]) == 9
    assert rec["input"]["file"] == rec["target"] == TARGET
    assert any(x.get("kind") == "auto" or x.get("fix_kind") == "auto"
               for x in rec.get("fixes") or [])


def test_a_remediation_with_a_separate_output_is_unchanged(tmp_path):
    """The existing contract (API onboarding's shape): not in place, guard as before."""
    root = _repo(tmp_path)
    (root / "examples").mkdir()
    shutil.copy(FIX / "lumen-pay.bad.yaml", root / "examples" / "lumen-pay.bad.yaml")
    c = f.contract({"remediate": {"input": "examples/lumen-pay.bad.yaml", "output": TARGET}},
                   "", root)
    assert not c.get("in_place")
    assert f.place_original(root, c, None) is None


# ── re-runs read the original where the record says it came from ───────────────────────────────

def test_a_rerun_reads_the_original_at_its_ref(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    git = ["git", "-C", str(repo), "-c", "user.email=b@example.invalid", "-c", "user.name=b"]
    subprocess.run([*git[:3], "init", "-q"], check=True)
    (repo / "a.yaml").write_text("original\n")
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-qm", "original"], check=True)
    sha = subprocess.run([*git[:3], "rev-parse", "HEAD"], capture_output=True, text=True,
                         check=True).stdout.strip()
    (repo / "a.yaml").write_text("fixed\n")
    subprocess.run([*git, "commit", "-qam", "fixed"], check=True)
    assert finding_overrides._original_at(repo, sha, "a.yaml") == "original\n"
    assert finding_overrides._original_at(repo, "-x", "a.yaml") is None


# ── contract tests after staging ───────────────────────────────────────────────────────────────

def test_the_projects_contract_tests_come_from_the_repo():
    got = contract_tests.from_repo(
        "cases:\n  - {name: healthz, path: /healthz, expect_status: 200}\n"
        "headers: {X-Probe: boss}\n")
    assert got == {"cases": [{"name": "healthz", "path": "/healthz", "expect_status": 200}],
                   "headers": {"X-Probe": "boss"}}
    for bad in (None, "", "cases: []\n", "nope: 1\n", "cases: [\n"):
        with pytest.raises(contract_tests.CaseError):
            contract_tests.from_repo(bad)


# ── one first-commit implementation (input_fidelity.commit_original, #288) ─────────────────────

class _Host:
    """A repo host on a bare local repo (the local substrate's git calls)."""

    def __init__(self, git_dir: Path):
        from boss_engine.services import local_repo

        self.lr, self.git_dir = local_repo, git_dir

    def ensure_branch(self, branch, base=None):
        return self.lr.ensure_branch(self.git_dir, branch, base)

    def commit_files(self, branch, files, message):
        self.lr.ensure_branch(self.git_dir, branch)
        return self.lr.commit_files(self.git_dir, branch, files, message)

    def read_text_file(self, branch, path):
        return self.lr.read_file(self.git_dir, branch, path)


@pytest.fixture()
def host(tmp_path: Path) -> _Host:
    from boss_engine.services import local_repo

    if not local_repo.git_available():
        pytest.skip("git is not installed")
    git_dir = local_repo.init_bare(tmp_path / "northgate.git")
    local_repo.commit_files(git_dir, "main", {"README.md": "Northgate\n"}, "init")
    return _Host(git_dir)


def test_an_upload_is_the_first_commit_through_the_same_function(tmp_path, host):
    _root, c = _uploaded(tmp_path)
    sha = f.commit_original(host, "boss/code/crun_1", "main", c)
    assert sha and c["original_commit"] == sha
    assert host.read_text_file("boss/code/crun_1", TARGET) == (FIX / "lumen-pay.bad.yaml").read_text()
    # A follow-up on the same branch (the target now carries fixes) commits nothing more.
    host.commit_files("boss/code/crun_1", {TARGET: "fixed: true\n"}, "governed: implement")
    c2 = dict(c)
    c2.pop("original_commit")
    assert f.commit_original(host, "boss/code/crun_1", "main", c2) is None


def test_a_repo_file_fixed_in_place_needs_no_first_commit(host):
    from boss_engine.services import local_repo

    local_repo.commit_files(host.git_dir, "main", {TARGET: "a: 1\n"}, "tenant")
    c = {"input": TARGET, "output": TARGET, "in_place": True, "_input_text": "a: 1\n"}
    assert f.commit_original(host, "boss/code/crun_2", "main", c) is None
