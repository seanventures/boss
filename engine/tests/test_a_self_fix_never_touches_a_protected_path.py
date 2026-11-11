"""P0: a BOSS self-fix never touches a protected path, and its tier follows what it touches.

BOSSAPP-571 (spec §8.1, §8.2, F16, F17, R4). The bar: nothing a self-fix does can widen what
BOSS or its agents may do. Protected paths are checked MECHANICALLY (a path match no model can
argue past), on the final diff AND on the diagnosis, renames as both paths; the gate symbols
inside BOSS's largest modules are protected hunk by hunk, so a plumbing fix in the same file is
still possible (tier 3) while a hunk inside a gate is not (never).

Every `never` rule of the policy is exercised here from the policy itself, so a rule added to the
YAML is covered the moment it exists.
"""
from __future__ import annotations

import re

import pytest

from boss_engine.services import self_fix_policy as sp
from boss_engine.services.self_fix_policy import FileChange

POLICY = sp.load()


def _sample(glob: str) -> str:
    """A concrete path a glob matches."""
    out = glob.replace("**/", "").replace("/**", "/inner/module.py").replace("**", "x.py")
    out = out.replace("*", "x")
    return out


def _edit(path: str, before: str = "a = 1\n", after: str = "a = 2\n", status: str = "M",
          old_path: str | None = None) -> FileChange:
    return FileChange(path=path, status=status, before=before, after=after, old_path=old_path)


@pytest.mark.parametrize("rule", [r for r in POLICY.never if r.group is None],
                         ids=lambda r: r.raw)
def test_every_never_path_refuses(rule):
    path = _sample(rule.glob)
    assert sp.matches(rule.glob, path), path
    verdict = sp.classify([_edit(path)], POLICY)
    assert not verdict.ok and verdict.tier is None
    assert verdict.never[0]["path"] == path
    assert "never changes by itself" in verdict.reason()
    # An ADDED file there is as protected as an edit (a new identity module, a new conftest).
    assert not sp.classify([FileChange(path=path, status="A", after="x = 1\n")], POLICY).ok


@pytest.mark.parametrize("rule", [r for r in POLICY.never if r.group is None],
                         ids=lambda r: r.raw)
def test_a_diagnosis_naming_a_never_path_stops_before_any_code(rule):
    assert sp.diagnosis_refusal([_sample(rule.glob)], policy=POLICY)


def test_a_rename_counts_as_both_paths():
    away = _edit("engine/boss_engine/services/elsewhere.py", status="R",
                 old_path="engine/boss_engine/identity/authz.py")
    into = _edit("engine/boss_engine/identity/authz_moved.py", status="R",
                 old_path="engine/boss_engine/services/plain.py")
    assert not sp.classify([away], POLICY).ok
    assert not sp.classify([into], POLICY).ok


def test_an_unlisted_path_is_tier_two():
    verdict = sp.classify([_edit("engine/boss_engine/services/app_start.py")], POLICY)
    assert verdict.ok and verdict.tier == 2 and verdict.flags == []


def test_a_tier_three_path_is_tier_three():
    verdict = sp.classify([_edit("engine/boss_engine/services/verification_target.py")], POLICY)
    assert verdict.ok and verdict.tier == 3 and verdict.tier3


# ── symbol rules (F17) ───────────────────────────────────────────────────────────────────────

GOVERNED = "engine/boss_engine/services/governed_code_task_service.py"
BASE = '''"""m."""
_TEST_GATES = frozenset({
    "written_tests_pass",
    "existing_tests_intact",
})


def plumbing(x):
    return x + 1


def _review_gate_from_findings(summary, verdict):
    return ("pass", 1.0)


def caller():
    gates = {"existing_tests_intact": True}
    return gates
'''


def _governed(after: str) -> sp.PathVerdict:
    return sp.classify([_edit(GOVERNED, BASE, after)], POLICY)


def test_a_hunk_inside_a_gate_symbol_is_never():
    v = _governed(BASE.replace('    "existing_tests_intact",\n', ""))
    assert not v.ok and "_TEST_GATES" in v.never[0]["why"]
    v = _governed(BASE.replace('return ("pass", 1.0)', 'return ("pass", 0.0)'))
    assert not v.ok and "_review_gate_from_findings" in v.never[0]["why"]


def test_a_line_inserted_inside_a_gate_symbol_is_never():
    v = _governed(BASE.replace('    "existing_tests_intact",\n',
                               '    "existing_tests_intact",\n    "extra_gate",\n'))
    assert not v.ok


def test_a_gate_renamed_away_is_never():
    v = _governed(BASE.replace("def _review_gate_from_findings(", "def review_gate_v2("))
    assert not v.ok and "renames" in v.never[0]["why"]


def test_a_changed_line_naming_a_gate_is_never_even_outside_the_symbols():
    v = _governed(BASE.replace('{"existing_tests_intact": True}',
                               '{"existing_tests_intact": False}'))
    assert not v.ok and "existing_tests_intact" in v.never[0]["why"]


def test_a_plumbing_hunk_in_the_same_file_is_tier_three():
    v = _governed(BASE.replace("return x + 1", "return x + 2"))
    assert v.ok and v.tier == 3


def test_the_verdict_grammar_is_protected_where_it_lives_now():
    """PR #34 moved the panel aggregation to review_verdict.aggregate_panel; the protection
    followed it (it had kept naming the old location, failing test_protected_symbols on main).
    A hunk inside the moved symbol is still never self-fixable."""
    verdict_file = "engine/boss_engine/services/review_verdict.py"
    base = ('def helper(x):\n    return x\n\n\n'
            'def aggregate_panel(verdicts, quorum):\n    return ("survived", 1.0)\n')
    v = sp.classify([_edit(verdict_file, base,
                           base.replace('"survived", 1.0', '"survived", 0.0'))], POLICY)
    assert not v.ok and "aggregate_panel" in v.never[0]["why"]
    v = sp.classify([_edit(verdict_file, base, base.replace("return x", "return x or x"))],
                    POLICY)
    assert v.ok, "a plumbing hunk beside the protected grammar is still fixable"


STACK = "engine/boss_engine/services/stack_deploy.py"
STACK_BASE = '''def plan(rendered):
    return rendered


def retire(project):
    return f"docker ps -aq --filter label={project}"
'''


def test_a_host_command_hunk_is_tier_three_and_flagged():
    after = STACK_BASE + '''

def retire_shared(ids):
    return f"for id in {ids}; do docker rm -f \\"$id\\"; done"
'''
    v = sp.classify([_edit(STACK, STACK_BASE, after)], POLICY)
    assert v.ok and v.tier == 3
    assert sp.HOST_EFFECT in v.flags and "docker rm" in " ".join(v.flag_detail[sp.HOST_EFFECT])


def test_a_non_command_hunk_of_the_same_file_is_tier_two():
    after = STACK_BASE.replace("return rendered", "return dict(rendered)")
    v = sp.classify([_edit(STACK, STACK_BASE, after)], POLICY)
    assert v.ok and v.tier == 2 and not v.flags


def test_a_host_effect_anywhere_makes_tier_three():
    path = "engine/boss_engine/services/app_start.py"
    v = sp.classify([_edit(path, "x = 1\n", 'x = 1\nCMD = "docker volume rm data"\n')], POLICY)
    assert v.tier == 3 and sp.HOST_EFFECT in v.flags


def test_importing_a_protected_module_is_flagged():
    path = "engine/boss_engine/services/app_start.py"
    v = sp.classify([_edit(path, "x = 1\n",
                           "from ..identity import approvals\nx = 1\n")], POLICY)
    assert v.ok and sp.PROTECTED_ADJACENT in v.flags


def test_a_diagnosis_naming_a_gate_symbol_is_refused_and_plumbing_is_not():
    assert sp.diagnosis_refusal([GOVERNED], ["_review_gate_from_findings"], policy=POLICY)
    assert sp.diagnosis_refusal([GOVERNED], ["plumbing"], policy=POLICY) is None


def test_the_policy_and_the_tests_that_hold_it_are_never():
    for path in ("packages/specpack/policies/self_fix_paths.yaml",
                 "engine/boss_engine/services/self_fix.py",
                 "engine/boss_engine/services/self_fix_policy.py",
                 "engine/tests/test_a_self_fix_never_touches_a_protected_path.py",
                 "engine/tests/test_protected_symbols_still_exist.py",
                 "scripts/install/self_fix.sh", "scripts/install/upgrade.sh",
                 # Phase 3–4: the upstream path, the denylist it checks sends against, the
                 # labeler that sorts agent PRs, and how BOSS counts what it solved itself.
                 "engine/boss_engine/services/self_fix_upstream.py",
                 "engine/boss_engine/services/private_infra.py",
                 "engine/boss_engine/services/autonomy_metric.py",
                 ".github/workflows/boss-agent-labels.yml",
                 ".github/PULL_REQUEST_TEMPLATE/boss-self-fix.md"):
        assert not sp.classify([_edit(path)], POLICY).ok, path


def test_a_missing_policy_fails_closed(tmp_path):
    with pytest.raises(sp.PolicyError):
        sp.load(tmp_path / "missing.yaml")
    bad = tmp_path / "bad.yaml"
    bad.write_text("version: 1\nnever: []\n")
    with pytest.raises(sp.PolicyError):
        sp.load(bad)


def test_the_area_label_follows_the_fixed_module():
    assert sp.area_of([STACK]) == "deploy"
    assert sp.area_of(["engine/tests/test_x.py", "engine/boss_engine/services/preview_service.py"]) \
        == "preview"
    assert re.fullmatch(r"[a-z]+", sp.area_of(["engine/boss_engine/services/whatever.py"]))
