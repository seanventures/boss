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


def test_shared_code_and_folders_the_brief_names_are_never_another_app(tmp_path):
    root = _two_apps(tmp_path)
    for lib in ("packages/ui", "libs/auth", "e2e"):
        (root / lib).mkdir(parents=True, exist_ok=True)
        (root / lib / "package.json").write_text("{}")
    changed = ["packages/ui/button.js", "libs/auth/x.js", "e2e/flow.spec.js",
               "neighborhood-board/src/board.js"]
    assert app_root.foreign_app_paths(root, changed, "recipe-box") == [
        "neighborhood-board/src/board.js"]
    # A folder the brief names is the brief's to change.
    assert app_root.foreign_app_paths(root, changed, "recipe-box",
                                      exempt=["neighborhood-board"]) == []


def test_a_workspace_member_nothing_runs_is_shared(tmp_path):
    import json

    (tmp_path / "package.json").write_text(json.dumps({"workspaces": ["apps/*", "ui-kit"]}))
    for app, scripts in (("apps/web", {"dev": "vite"}), ("apps/api", {"start": "node ."}),
                         ("ui-kit", {"build": "tsc"})):
        (tmp_path / app).mkdir(parents=True)
        (tmp_path / app / "package.json").write_text(json.dumps({"scripts": scripts}))
    assert app_root.foreign_app_paths(tmp_path, ["ui-kit/a.ts", "apps/api/b.ts"],
                                      "apps/web") == ["apps/api/b.ts"]


def test_the_folders_a_brief_names(tmp_path):
    root = _two_apps(tmp_path)
    assert app_root.named_folders(root, "Build it in the `recipe-box/` folder.") == ["recipe-box"]
    assert app_root.named_folders(
        root, "Move the login from neighborhood-board into `recipe-box/`") == [
        "neighborhood-board", "recipe-box"]
    assert app_root.named_folders(root, "Add a counter to the slug service.") == []


# ── ENT-REC-51: the pull request's whole change ───────────────────────────────────────────────

def _git(cwd, *args):
    import subprocess

    return subprocess.run(["git", "-c", "user.email=t@example.com", "-c", "user.name=t", *args],
                          cwd=cwd, capture_output=True, text=True, check=True).stdout


def _origin(tmp_path):
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main")
    for app in ("recipe-box", "neighborhood-board", "crm", "helpdesk"):
        (seed / app / "src").mkdir(parents=True)
        (seed / app / "package.json").write_text('{"scripts": {"start": "node ."}}')
        (seed / app / "src" / "index.js").write_text(f"// {app}\n")
    (seed / "crm" / "src" / "login.js").write_text("export const login = 1;\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-qm", "base")
    bare = tmp_path / "origin.git"
    _git(tmp_path, "clone", "-q", "--bare", str(seed), str(bare))
    work = tmp_path / "work"
    _git(tmp_path, "clone", "-q", str(bare), str(work))
    _git(work, "checkout", "-qb", "boss/code/crun_1")
    return bare, seed, work


def _main_moves_in_a_sibling_app(tmp_path, bare):
    other = tmp_path / "other"
    _git(tmp_path, "clone", "-q", str(bare), str(other))
    (other / "neighborhood-board" / "src" / "index.js").write_text("// moved on main\n")
    _git(other, "commit", "-qam", "main moves")
    _git(other, "push", "-q", "origin", "main")


def test_what_main_merged_since_is_not_this_change(tmp_path):
    from boss_engine.services import whole_change

    bare, _seed, work = _origin(tmp_path)
    (work / "recipe-box" / "src" / "index.js").write_text("// the counter\n")
    _git(work, "commit", "-qam", "the counter")
    _main_moves_in_a_sibling_app(tmp_path, bare)
    _git(work, "fetch", "-q", "origin", "main:refs/remotes/origin/main")
    two_dot = _git(work, "diff", "--name-only", "origin/main").split()
    assert "neighborhood-board/src/index.js" in two_dot          # what the old diff counted
    paths = whole_change.paths_since_base(work, "refs/remotes/origin/main")
    assert paths == ["recipe-box/src/index.js"]
    assert app_root.foreign_app_paths(work, paths, "recipe-box") == []


def test_a_file_moved_out_of_another_app_is_that_apps_change(tmp_path):
    from boss_engine.services import whole_change

    _bare, _seed, work = _origin(tmp_path)
    _git(work, "mv", "crm/src/login.js", "helpdesk/src/login.js")
    _git(work, "commit", "-qm", "move the login")
    paths = whole_change.paths_since_base(work, "refs/remotes/origin/main")
    assert paths == ["crm/src/login.js", "helpdesk/src/login.js"]
    assert app_root.foreign_app_paths(work, paths, "helpdesk") == ["crm/src/login.js"]


def test_a_shallow_checkout_is_deepened_to_its_merge_base(tmp_path):
    from boss_engine.services import whole_change

    bare, _seed, work = _origin(tmp_path)
    (work / "recipe-box" / "src" / "index.js").write_text("// one\n")
    _git(work, "commit", "-qam", "one")
    (work / "recipe-box" / "src" / "index.js").write_text("// two\n")
    _git(work, "commit", "-qam", "two")
    _git(work, "push", "-q", "origin", "boss/code/crun_1")
    _main_moves_in_a_sibling_app(tmp_path, bare)
    shallow = tmp_path / "shallow"
    _git(tmp_path, "clone", "-q", "--depth", "1", "--branch", "boss/code/crun_1",
         f"file://{bare}", str(shallow))
    _git(shallow, "fetch", "-q", "--depth", "1", "origin", "main:refs/remotes/origin/main")
    assert whole_change.paths_since_base(shallow, "refs/remotes/origin/main") is None
    deepened = []

    def deepen():
        deepened.append(1)
        _git(shallow, "fetch", "-q", "--deepen", "50", "origin",
             "main:refs/remotes/origin/main", "boss/code/crun_1")

    paths = whole_change.paths_since_base(shallow, "refs/remotes/origin/main", deepen=deepen)
    assert deepened and paths == ["recipe-box/src/index.js"]


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
    assert ("_write_scope[0] = _scope_for_writing(\n"
            "            engine, run_id, project_id, root, spec, fctx, run, _app_id,\n"
            "            _app_root_mod.clean_rel(_app_id or _app_scope)\n"
            "            or _app_root_mod.clean_rel(_recorded_app), _write_exempt)") in src
    # The micro-VM guest's brief too.
    assert "_scoped_prompt(prompt)}" in src
    # The gate judges the pull request's whole change, with the brief's folders exempt.
    assert "root, _whole_change_paths(), _write_scope[0], exempt=_write_exempt)" in src


def test_a_cross_app_brief_has_no_single_app_scope(tmp_path):
    """"Move the login from `crm/` to `helpdesk/`" changes two apps by design: no scope, so no
    writing turn is told to stay in one and the gate never fails it."""
    root = _two_apps(tmp_path)
    exempt: list = []
    one = governed._scope_for_writing(None, "crun_1", "p", root, {
        "instructions": "Build it in the `recipe-box/` folder."}, {}, None, None,
        "recipe-box", exempt)
    assert one == "recipe-box" and exempt == ["recipe-box"]
    both = governed._scope_for_writing(None, "crun_1", "p", root, {
        "instructions": "Move the board from `neighborhood-board/` into `recipe-box/`."}, {},
        None, None, "recipe-box", exempt)
    assert both is None and exempt == ["neighborhood-board", "recipe-box"]
    # A pinned benchmark app keeps its scope whatever the brief names.
    assert governed._scope_for_writing(None, "crun_1", "p", root, {
        "instructions": "Use `neighborhood-board/` as the reference."}, {}, None,
        "recipe-box", "recipe-box", exempt) == "recipe-box"


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
