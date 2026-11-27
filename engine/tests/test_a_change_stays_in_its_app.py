"""A BUILD FOR ONE APP STAYS IN THAT APP (ENT-REC-51), AND THE SAME CAUSE IS ONE CAUSE (ENT-REC-52).

ENT-REC-51. Only a pinned benchmark app's implement and solo prompts said "Work ONLY under
`<app>/`. Do not edit sibling apps." For the app a brief or a lineage names, no writing turn —
the build, a repair round, a follow-up, a person's Retry — was told to stay inside it, and
nothing checked the diff. Now every writing turn of every engine carries the scope
(governed.AppScopedAgent, app_scope_rule), and a diff that changes another app's files fails
`stayed_in_app` (app_root.foreign_app_paths). The two-app lineage end to end is
test_recovery_faults_end_to_end.py (rec51).

ENT-REC-52. The recovery agent's cause signature joined every failing gate, so the same broken
test read as a new cause when a repair added `code_written` beside it, and "the same cause twice
asks a person" never fired. The signature now names the one gate that is the cause.
"""
from __future__ import annotations

from pathlib import Path

from boss_engine.services import app_root, factory_view, plain_words, quality_bar
from boss_engine.services import governed_code_task_service as governed
from boss_engine.services import step_recovery as sr

GATE = governed.STAYED_IN_APP_GATE


def _two_apps(tmp_path: Path) -> Path:
    for app in ("recipe-box", "neighborhood-board", "apps/admin"):
        (tmp_path / app / "src").mkdir(parents=True)
        (tmp_path / app / "package.json").write_text("{}")
    (tmp_path / "docs").mkdir()
    return tmp_path


# ── ENT-REC-51: the diff ──────────────────────────────────────────────────────────────────────

def test_a_change_to_another_apps_files_is_named(tmp_path):
    root = _two_apps(tmp_path)
    changed = ["recipe-box/src/slug.js", "neighborhood-board/src/board.js",
               "apps/admin/src/x.js", "README.md", "docs/notes.md", ".github/workflows/ci.yml"]
    assert app_root.foreign_app_paths(root, changed, "recipe-box") == [
        "apps/admin/src/x.js", "neighborhood-board/src/board.js"]


def test_the_apps_own_files_and_shared_root_files_are_not_foreign(tmp_path):
    root = _two_apps(tmp_path)
    assert app_root.foreign_app_paths(root, ["recipe-box/src/a.js", "README.md", "F-1",
                                             "recipe-box/package.json"], "recipe-box") == []
    # An app in a group folder: its own files are its own.
    assert app_root.foreign_app_paths(root, ["apps/admin/src/x.js"], "apps/admin") == []
    # No scope (a single-app repository): nothing is another app's.
    assert app_root.foreign_app_paths(root, ["neighborhood-board/src/board.js"], None) == []


def test_stayed_in_app_is_a_floor_gate_with_words_on_every_surface():
    assert GATE in governed._ALL_GATE_NAMES
    assert quality_bar._BLOCKS[GATE] == (True, True, True) and GATE in quality_bar.FLOOR
    assert factory_view.GATE_LABELS[GATE][0] == "Stayed in app"
    assert GATE in factory_view.GATE_NOUNS
    assert plain_words.FAILED_WORDS[GATE] == "Changed another app"
    web = (Path(__file__).resolve().parents[2] / "apps" / "web" / "lib" / "plain-words.ts").read_text()
    assert 'stayed_in_app: "Stayed in app"' in web and 'stayed_in_app: "Changed another app"' in web


# ── ENT-REC-51: every writing turn is told ───────────────────────────────────────────────────

class _Agent:
    def __init__(self):
        self.calls, self.budget_ceiling = [], None

    def run_role(self, prompt, cwd, *, read_only=False, **_kw):
        self.calls.append((prompt, read_only))
        return {"summary": "ok"}


def test_every_writing_turn_carries_the_app_scope_and_a_reviewer_is_left_alone():
    scope = [None]
    agent = governed.AppScopedAgent(_Agent(), lambda p: governed.app_scope_rule(scope[0], p))
    agent.run_role("implement it", "/tmp")
    scope[0] = "recipe-box"
    for prompt in ("implement it", "Autonomous review-repair: fix the test", "FIX ROUND 1"):
        agent.run_role(prompt, "/tmp")
    agent.run_role("review it", "/tmp", read_only=True)
    calls = agent._agent.calls
    assert calls[0] == ("implement it", False)                  # no scope known: unchanged
    for prompt, _ in calls[1:4]:
        assert "Work ONLY under `recipe-box/`" in prompt and "Do not edit sibling apps" in prompt
        assert f"`{GATE}`" in prompt
    assert calls[4] == ("review it", True)
    # Attributes are the agent's own (the budget ceiling BOSS sets on it).
    agent.budget_ceiling = 5
    assert agent._agent.budget_ceiling == 5 and agent.budget_ceiling == 5


def test_the_scope_is_said_once():
    once = governed.app_scope_rule("recipe-box", "x")
    assert governed.app_scope_rule("recipe-box", once) == once
    assert governed.app_scope_rule(None, "x") == "x"


def test_every_build_path_runs_its_agents_through_the_scope():
    """The class, enforced: one place makes the build's agents, and it wraps them; the scope is
    the named app, the brief's folder, else the folder the lineage recorded (a follow-up)."""
    src = Path(governed.__file__).read_text()
    assert src.count("make_code_agent(") == 1
    assert "a = AppScopedAgent(a, _scoped_prompt)" in src
    assert ("_write_scope[0] = (_app_root_mod.clean_rel(_app_id or _app_scope)\n"
            "                           or _app_root_mod.clean_rel(_recorded_app))") in src
    # The micro-VM guest's brief too.
    assert "_scoped_prompt(prompt)}" in src


# ── ENT-REC-52: one cause, one signature ─────────────────────────────────────────────────────

def _sig(*gates):
    return sr.cause_signature(sr.FINDINGS_KIND, {"failing_gates": list(gates),
                                                 "phase": sr.GATE_WAIT_PHASE})[0]


def test_the_same_broken_test_is_one_cause_whatever_fails_beside_it():
    first = _sig("written_tests_pass")
    assert first == "build:open_findings:written_tests_pass"
    assert _sig("code_written", "written_tests_pass") == first     # a round that wrote nothing
    assert _sig("written_tests_pass", "security_clean") == first   # a seat failing beside it
    assert _sig("acceptance_met", "written_tests_pass", "code_review_passed") == first


def test_a_different_cause_is_a_different_signature():
    assert _sig("security_clean") == "build:open_findings:security_clean"
    assert _sig("security_clean", "code_review_passed") == "build:open_findings:security_clean"
    assert _sig("app_serves", "written_tests_pass") == "build:open_findings:app_serves"
    assert _sig("code_written") == "build:open_findings:code_written"   # only a symptom: itself
    assert _sig("zz_unknown", "consistent") == "build:open_findings:consistent"


def test_the_same_cause_back_is_one_question_for_a_person():
    failure = {"failing_gates": ["written_tests_pass"], "phase": sr.GATE_WAIT_PHASE,
               "why": "the build stopped on its checks"}
    again = {**failure, "failing_gates": ["code_written", "written_tests_pass"]}
    sig, pattern = sr.cause_signature(sr.FINDINGS_KIND, failure)
    allowed = sr.KINDS[sr.FINDINGS_KIND]
    first = sr.coded_decision(sr.FINDINGS_KIND, {"failure": failure}, signature=sig,
                              pattern=pattern, last={}, allowed=allowed)
    sig2, pattern2 = sr.cause_signature(sr.FINDINGS_KIND, again)
    second = sr.coded_decision(sr.FINDINGS_KIND, {"failure": again}, signature=sig2,
                               pattern=pattern2, allowed=allowed,
                               last={"signature": sig, "action": first["action"]})
    assert first["action"] == sr.FIX_APP and second["action"] == sr.ASK_PERSON
    assert pattern2["gates"] == ["code_written", "written_tests_pass"]
    assert pattern2["cause"] == "written_tests_pass"
