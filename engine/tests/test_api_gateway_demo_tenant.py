"""The API gateway onboarding demo tenant (examples/api-gateway-onboarding, BOSSAPP-1134) is what
the chain can run on: its bad file breaks at least two security and two guideline rules, each
visible in the report BOSS reads; the corrected file passes; the repo's .boss files parse with
BOSS's own readers; and reset.sh builds the same fresh state every time."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from boss_engine.services import contract_tests, rulesets, validators
from boss_engine.services.validator_adapters import parse as adapt

ROOT = Path(__file__).resolve().parents[2]
DEMO = ROOT / "examples" / "api-gateway-onboarding"
REPO = DEMO / "tenant-repo"
LINT = REPO / "tools" / "tenantlint.py"


def _lint(path: Path) -> tuple[int, dict]:
    done = subprocess.run([sys.executable, str(LINT), str(path), "--json"],
                          capture_output=True, text=True, cwd=REPO, check=False)
    return done.returncode, json.loads(done.stdout)


def test_the_bad_file_breaks_two_security_and_two_guideline_rules_each_in_the_report():
    code, report = _lint(DEMO / "partner" / "lumen-pay.bad.yaml")
    assert code == 1
    by_kind: dict[str, set[str]] = {}
    for v in report["violations"]:
        by_kind.setdefault(v["category"], set()).add(v["rule_id"])
        assert isinstance(v["line"], int) and v["line"] > 0
    assert len(by_kind["security"]) >= 2 and len(by_kind["guideline"]) >= 2, by_kind
    found = adapt("gwlint-json", json.dumps(report), "config/tenants/lumen-pay.yaml")
    assert {f["rule_id"] for f in found} == by_kind["security"] | by_kind["guideline"]


def test_the_corrected_file_and_the_existing_tenant_pass():
    for path in (DEMO / "partner" / "lumen-pay.corrected.yaml",
                 REPO / "config" / "tenants" / "orbit-maps.yaml"):
        code, report = _lint(path)
        assert (code, report["violations"]) == (0, []), path


def test_every_rule_the_checker_reports_is_in_the_ruleset_and_the_rules_doc():
    sys.path.insert(0, str(LINT.parent))
    try:
        import tenantlint
    finally:
        sys.path.pop(0)
    ruleset = rulesets.load(REPO, "northgate-tenants")
    doc = (REPO / "rules" / "TENANT_RULES.md").read_text()
    for rule in tenantlint.RULES:
        assert rule in ruleset["rules"], rule
        assert f'id="{rule}"' in doc, rule


def test_the_repo_declares_its_checker_and_contract_tests_the_way_boss_reads_them():
    v = validators.for_path(REPO, "config/tenants/lumen-pay.yaml")
    assert v and v["name"] == "tenantlint" and v["adapter"] == "gwlint-json"
    assert v["ruleset"] == "northgate-tenants"
    cases = contract_tests.from_repo((REPO / ".boss" / "contract_tests.yaml").read_text())
    assert len(cases["cases"]) >= 3


@pytest.mark.skipif(shutil.which("git") is None or shutil.which("bash") is None,
                    reason="needs git and bash")
def test_reset_builds_the_same_fresh_state_and_refuses_a_foreign_directory(tmp_path):
    target = tmp_path / "tenants"
    for _ in range(2):                                   # a second reset replaces the first
        subprocess.run(["bash", str(DEMO / "reset.sh"), "--local", str(target)], check=True,
                       capture_output=True)
        branches = subprocess.run(["git", "-C", str(target), "branch", "--format=%(refname:short)"],
                                  capture_output=True, text=True, check=True).stdout.split()
        assert sorted(branches) == ["main", "onboard/lumen-pay"]
        assert not (target / "config" / "tenants" / "lumen-pay.yaml").exists()   # main
        added = subprocess.run(["git", "-C", str(target), "diff", "--name-only", "main",
                                "onboard/lumen-pay"], capture_output=True, text=True,
                               check=True).stdout.split()
        assert added == ["config/tenants/lumen-pay.yaml"]
    foreign = tmp_path / "mine"
    foreign.mkdir()
    (foreign / "keep.txt").write_text("not a demo repo")
    done = subprocess.run(["bash", str(DEMO / "reset.sh"), "--local", str(foreign)],
                          capture_output=True, text=True)
    assert done.returncode == 1 and (foreign / "keep.txt").exists()
