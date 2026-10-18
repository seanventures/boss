"""Mender is shown the failing operation's production path and the safety invariants of the code
it may touch, its red test must drive that path, and its stages run on the strongest lane while
the reviewers stay independent (BOSSAPP-571, spec §17.1).

THE CASE: the real-engine Replay A, third run. Every mechanical gate passed and the reviewers
refused the fix with two MAJORs: it loosened the guard that made a retire step refuse when a
foreign container was in the project (written down in that function's own docstring), and its
test called a lower helper on a path `apply_on_host`, the code the failing step ran, never takes.

No database, no model: the pure parts of self_fix_build and self_fix_integrity on a tiny tree.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from boss_engine.services import self_fix_build as b
from boss_engine.services import self_fix_integrity as si

DEPLOY = '''"""A tiny stack_deploy."""


def retire(rendered, projects):
    """Retire this app's own projects. Never `-v`; a project holding a foreign container makes
    the step REFUSE (exit 3) and nothing is removed."""
    return {**rendered, "install": [{"name": "retire", "run": "docker compose -p x down --remove-orphans"}]}


def render(manifest):
    return {"install": [{"name": "up", "run": "docker compose up -d"}]}


def apply_on_host(engine, settings, *, deployment_id):
    """The host work of a stack deployment."""
    rendered = render("m")
    rendered = retire(rendered, [])
    return rendered
'''
PINNING_TEST = '''from boss_engine.services import stack_deploy


def test_a_foreign_container_makes_the_retire_refuse():
    """The guard: a foreign container, nothing removed."""
    assert stack_deploy.retire({}, ["p"])
'''

DRIVES = '''from boss_engine.services import stack_deploy


def _steps():
    return stack_deploy.apply_on_host(None, None, deployment_id="d")["install"]


def test_the_release_retires_through_the_apply():
    assert any("rm -f" in s["run"] for s in _steps())


def test_a_helper_only():
    assert stack_deploy.retire({}, [])
'''
HELPER_ONLY = '''from boss_engine.services import stack_deploy


def test_a_helper_only():
    assert stack_deploy.retire({}, [])
'''


def _tree(tmp_path: Path) -> Path:
    (tmp_path / "engine/boss_engine/services").mkdir(parents=True)
    (tmp_path / "engine/tests").mkdir(parents=True)
    (tmp_path / "engine/boss_engine/services/stack_deploy.py").write_text(DEPLOY)
    (tmp_path / "engine/tests/test_retire_existing.py").write_text(PINNING_TEST)
    return tmp_path


SFX = {"step_kind": "deploy", "signature": "deploy:port_held:8000",
       "evidence": {"deployment_id": "sdep_example"}}
LOCATION = {"file": "engine/boss_engine/services/stack_deploy.py", "function": "retire"}
ENTRY = {"file": "engine/boss_engine/services/stack_deploy.py", "function": "apply_on_host"}


# ── the red test drives the entry point ─────────────────────────────────────────────────────

def test_a_test_reaches_the_entry_point_directly_or_through_a_helper_of_its_file():
    assert si.tests_driving(DRIVES, "apply_on_host") == ["test_the_release_retires_through_the_apply"]
    fixture = ('import pytest\nfrom boss_engine.services import stack_deploy\n\n\n'
               '@pytest.fixture\ndef steps():\n    return stack_deploy.apply_on_host(None, None)\n\n\n'
               'def test_uses_the_fixture(steps):\n    assert steps\n')
    assert si.tests_driving(fixture, "apply_on_host") == ["test_uses_the_fixture"]
    assert si.tests_driving(HELPER_ONLY, "apply_on_host") == []


def test_a_helper_only_test_is_refused_when_the_entry_point_is_known():
    out = si.drives_entry_point({"engine/tests/test_new.py": HELPER_ONLY}, ENTRY)
    assert not out.ok and "no new test calls apply_on_host" in out.why


def test_the_test_that_fails_on_the_base_must_be_the_one_that_drives_it():
    tests = {"engine/tests/test_new.py": DRIVES}
    only_helper_red = {"engine.tests.test_new::test_a_helper_only": "failed",
                       "engine.tests.test_new::test_the_release_retires_through_the_apply": "passed"}
    out = si.drives_entry_point(tests, ENTRY, only_helper_red)
    assert not out.ok and "pass on BOSS as it is" in out.why
    driving_red = {"engine.tests.test_new::test_a_helper_only": "passed",
                   "engine.tests.test_new::test_the_release_retires_through_the_apply[x]": "failed"}
    out = si.drives_entry_point(tests, ENTRY, driving_red)
    assert out.ok and out.detail["red_and_driving"] == {
        "engine/tests/test_new.py": ["test_the_release_retires_through_the_apply"]}


def test_no_known_entry_point_is_said_not_assumed():
    out = si.drives_entry_point({"engine/tests/test_new.py": HELPER_ONLY}, None)
    assert out.ok and out.detail["applied"] is False


def test_the_entry_point_is_known_for_a_deploy_steps_host_work_only(tmp_path):
    work = _tree(tmp_path)
    assert b.entry_point(SFX, work)["function"] == "apply_on_host"
    assert b.entry_point({**SFX, "evidence": {}}, work) is None, "a repair build's failure"
    assert b.entry_point({**SFX, "step_kind": "build"}, work) is None
    (work / ENTRY["file"]).write_text("def other():\n    pass\n")
    assert b.entry_point(SFX, work) is None, "a base that does not define it"


# ── what Mender and the reviewers are told ──────────────────────────────────────────────────

def test_mender_sees_the_production_path_and_the_invariants_in_bosss_own_words(tmp_path):
    work = _tree(tmp_path)
    path = b.call_path(work, ENTRY, LOCATION)
    assert "apply_on_host" in path and "render(" in path
    assert "retire(rendered, [])   <-- the diagnosed function" in path
    rules = b.safety_invariants(work, LOCATION, ENTRY)
    assert "makes the step REFUSE (exit 3) and nothing is removed" in rules
    assert "test_retire_existing.py::test_a_foreign_container_makes_the_retire_refuse" in rules
    assert "render:" not in rules, "a function that removes nothing carries no invariant"

    diagnosis = {"hypothesis": "h", "location": LOCATION}
    test = b.write_test_prompt(SFX, diagnosis, work=work)
    assert "must CALL apply_on_host" in test and "drives_entry_point" in test
    assert "put `stack_deploy` or `retire` in the failing assertion's message" in test
    assert "SAFETY INVARIANTS" in test and "THE PRODUCTION PATH" in test
    fix = b.implement_prompt(SFX, diagnosis, red_output="F", work=work)
    assert "NEVER WIDEN WHAT A HOST COMMAND REMOVES" in fix and "SAFETY INVARIANTS" in fix
    assert "THE PRODUCTION PATH" in b.diagnose_prompt(SFX, work=work)
    verdict = SimpleNamespace(tier=3, flags=["host_effect"], flag_detail={})
    security = b.review_prompt("security_check", SFX, diagnosis, "diff", verdict, "CONTEXT")
    assert "NEVER WIDEN" in security and "CONTEXT" in security
    assert "NEVER WIDEN" not in b.review_prompt("review_code", SFX, diagnosis, "d", verdict)


def test_a_finding_reaches_the_repair_round_word_for_word():
    text = ("FINDING: MAJOR engine/boss_engine/services/stack_deploy.py:1515 — The change loosens "
            "the guard.\nBefore, a foreign container made the step refuse.\n"
            "FINDING: MINOR x.py:1 — nit\nVERDICT: FINDINGS")
    parsed = b.parse_review(text)
    assert parsed["open"][0]["what"].endswith("Before, a foreign container made the step refuse.")
    assert [f["severity"] for f in parsed["findings"]] == ["MAJOR", "MINOR"]
    tests = {"engine/tests/test_new.py": ""}
    assert b._cites_tests([{"what": "engine/tests/test_new.py:47 — the test calls a helper"}], tests)
    assert not b._cites_tests([{"what": "stack_deploy.py:1 — quoting"}], tests)
    asked = ("stack_deploy.py:1543 — this path first deletes, then refuses. Keep the existing "
             "refusal guards ahead of the new destructive step and add a regression covering "
             "this combination.")
    assert b._cites_tests([{"what": asked}], tests), "a finding that asks for a test goes to RED"
    assert b.test_round([{"what": asked}], tests) == "add", "the red test stays; one is added"
    assert b.test_round([{"what": "engine/tests/test_new.py:47 — a helper"}], tests) == "rewrite"
    assert b.test_round([{"what": "stack_deploy.py:1 — quoting"}], tests) is None


# ── the lanes ───────────────────────────────────────────────────────────────────────────────

def _settings(**over):
    base = dict(code_agent_engine="engine_a", code_agent_model="", code_agent_strong_engine="",
                code_agent_strong_model="", code_review_engine="", code_review_model="")
    return SimpleNamespace(**{**base, **over})


def _clear(monkeypatch):
    for name in ("BOSS_SELF_FIX_ENGINE", "BOSS_SELF_FIX_REVIEW_ENGINE", "BOSS_SELF_FIX_MODEL",
                 "BOSS_SELF_FIX_REVIEW_MODEL", "BOSS_SELF_FIX_SECURITY_MODEL",
                 "BOSS_SCRIPTED_SELF_FIX"):
        monkeypatch.delenv(name, raising=False)


def test_mender_runs_on_the_hard_problems_lane_and_the_reviewers_stay_independent(monkeypatch):
    _clear(monkeypatch)
    s = _settings(code_agent_model="small", code_agent_strong_engine="engine_a",
                  code_agent_strong_model="strongest", code_review_engine="engine_b",
                  code_review_model="b-model")
    builder, reviewer = b.engine_names(None, s, "")
    assert (builder, reviewer) == ("engine_a", "engine_b")
    mender, review, security = b.lane_models(None, s, "", builder, reviewer)
    assert mender == "strongest", "diagnose, test and fix on the strongest configured model"
    assert review is None, "engine_b's review model is never handed to engine_a"
    assert security == "b-model", "the security seat on the other engine, with its own model"


def test_a_strong_model_of_another_engine_is_not_handed_to_mender(monkeypatch):
    _clear(monkeypatch)
    s = _settings(code_agent_model="build-model", code_agent_strong_engine="engine_c",
                  code_agent_strong_model="c-model")
    assert b.engine_names(None, s, "")[0] == "engine_c"
    monkeypatch.setenv("BOSS_SELF_FIX_ENGINE", "engine_a")
    builder, reviewer = b.engine_names(None, s, "")
    assert (builder, reviewer) == ("engine_a", "engine_a")
    mender, review, security = b.lane_models(None, s, "", builder, reviewer)
    assert mender == "build-model" and review is None and security is None


def test_the_environment_names_each_lane(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("BOSS_SELF_FIX_MODEL", "m1")
    monkeypatch.setenv("BOSS_SELF_FIX_REVIEW_MODEL", "m2")
    s = _settings()
    assert b.lane_models(None, s, "", "engine_a", "engine_a") == ("m1", "m2", "m2")
    engines = b.Engines(object(), object(), "engine_a", "engine_b", "m1", "m2", None)
    lanes = engines.lanes()
    assert lanes["fix"] == {"engine": "engine_a", "model": "m1"}
    assert lanes["review_code"] == {"engine": "engine_a", "model": "m2"}
    assert lanes["security_check"] == {"engine": "engine_b", "model": "default"}
