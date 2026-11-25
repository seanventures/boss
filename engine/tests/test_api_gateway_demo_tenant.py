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


# ── reset.sh --github fails closed and talks to one host (review of PR #326) ─────────────────

FAKE_GH = r"""#!/usr/bin/env bash
# Records every call, then answers from $FAKE_GH_MODE.
printf 'GH_HOST=%s | %s\n' "${GH_HOST:-}" "$*" >> "$FAKE_LOG"
case "$*" in
  *"/commits?"*)
    case "$FAKE_GH_MODE" in
      error)  echo "gh: Bad credentials (HTTP 401)" >&2; exit 1 ;;
      empty)  echo "gh: Git Repository is empty. (HTTP 409)" >&2; exit 1 ;;
      *)      echo '[{"sha": "abc"}]' ;;
    esac ;;
  *".northgate-demo"*)
    [ "$FAKE_GH_MODE" = "demo" ] && echo '{}' && exit 0
    echo "gh: Not Found (HTTP 404)" >&2; exit 1 ;;
  "pr list"*) echo "7" ;;
esac
exit 0
"""

FAKE_GIT = r"""#!/usr/bin/env bash
# Records pushes and remote reads instead of making them; everything else is real git.
for a in "$@"; do
  case "$a" in
    push) printf 'git %s\n' "$*" >> "$FAKE_LOG"; exit 0 ;;
    ls-remote) printf 'git %s\n' "$*" >> "$FAKE_LOG"
               printf 'a\trefs/heads/main\nb\trefs/heads/old-demo\n'; exit 0 ;;
    remote) printf 'git %s\n' "$*" >> "$FAKE_LOG" ;;
  esac
done
exec "$REAL_GIT" "$@"
"""


def _github_reset(tmp_path, mode: str, repo: str = "northgate-demo/tenants", gh_host: str = ""):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for name, body in (("gh", FAKE_GH), ("git", FAKE_GIT)):
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    log = tmp_path / "calls.log"
    log.write_text("")
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp_path), "FAKE_LOG": str(log),
           "FAKE_GH_MODE": mode, "REAL_GIT": shutil.which("git"), "TMPDIR": str(tmp_path)}
    if gh_host:
        env["GH_HOST"] = gh_host
    done = subprocess.run(["bash", str(DEMO / "reset.sh"), "--github", repo, "--yes"],
                          capture_output=True, text=True, env=env)
    return done, log.read_text()


def _changed_anything(calls: str) -> bool:
    return any(w in calls for w in ("git push", "pr close", "pr create", "remote add"))


needs_git = pytest.mark.skipif(shutil.which("git") is None or shutil.which("bash") is None,
                               reason="needs git and bash")


@needs_git
@pytest.mark.parametrize("mode", ["error", "no-marker"])
def test_github_reset_stops_before_any_change_when_it_cannot_confirm_a_demo_repo(tmp_path, mode):
    done, calls = _github_reset(tmp_path, mode)
    assert done.returncode == 1, done.stderr
    assert "Refusing" in done.stderr
    assert not _changed_anything(calls), calls


@needs_git
def test_github_reset_refuses_a_repo_name_that_is_not_owner_slash_repo(tmp_path):
    done, calls = _github_reset(tmp_path, "demo", repo="evil.example/org/repo")
    assert done.returncode == 1 and calls == ""


@needs_git
@pytest.mark.parametrize("mode", ["demo", "empty"])
def test_github_reset_pins_github_com_for_every_check_and_the_push(tmp_path, mode):
    done, calls = _github_reset(tmp_path, mode, gh_host="ghes.example.internal")
    assert done.returncode == 0, done.stderr
    lines = calls.splitlines()
    gh_calls = [line for line in lines if not line.startswith("git ")]
    assert gh_calls and all(line.startswith("GH_HOST= |") for line in gh_calls), gh_calls
    assert all("--hostname github.com" in line for line in gh_calls if " api " in f" {line}")
    assert all("--repo github.com/northgate-demo/tenants" in line
               for line in gh_calls if " pr " in f" {line}")
    assert any("remote add origin https://github.com/northgate-demo/tenants.git" in line
               for line in lines)
    assert "ghes.example.internal" not in calls
    assert any("push" in line and "--delete old-demo" in line for line in lines)
    assert not any("--delete main" in line for line in lines)


@needs_git
def test_local_reset_refuses_the_example_itself():
    for inside in (DEMO / "tenant-repo", DEMO, DEMO / "tenant-repo" / "config"):
        done = subprocess.run(["bash", str(DEMO / "reset.sh"), "--local", str(inside)],
                              capture_output=True, text=True)
        assert done.returncode == 1, inside
    assert (REPO / "tools" / "tenantlint.py").is_file()
