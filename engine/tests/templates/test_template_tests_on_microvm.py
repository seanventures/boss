"""Template tests on the micro-VM build substrate (F2 §2.3, item s): NOT run there, and never
silently. The micro-VM guest returns a command's exit code and output tail but not the JUnit
report the template_tests gate counts, so:

- the gate fails closed with MICROVM_REFUSAL (what happened, why, what to do) and never calls a
  runner;
- a blocked build's error carries those words, not only the gate's name;
- an apply is refused up front (409 template_tests_need_local_sandbox) on an install that builds
  on the micro-VM, before any template row is read or a build is started;
- the trial is unaffected: it always runs in the local build sandbox, never in a guest."""
from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest

from boss_engine.services import governed_code_task_service as gcts
from boss_engine.templates import apply as apply_service
from boss_engine.templates import gate, trial
from boss_engine.templates.gate import MICROVM_REFUSAL, evaluate, microvm_refusal
from boss_engine.templates.service import TemplateError

from .test_template_gate import CHANGED, _hello


def test_the_gate_on_the_microvm_fails_closed_in_words_and_runs_nothing():
    tpl, files = _hello()
    ran = []
    r = evaluate(template=tpl, files=files, changed=CHANGED, cwd=None,
                 run_command=lambda c, w: ran.append(c), substrate="microvm")
    tt = r.evidence["template_tests"]
    assert r.gates["template_tests"] is False and not ran
    assert tt["error"] == MICROVM_REFUSAL and tt["not_run"] == "microvm"
    # The sentence says what, why and what to do.
    for words in ("micro-VM", "JUnit report", "BOSS_GOVERNED_SANDBOX_PROVIDER=local", "apply the template again"):
        assert words in MICROVM_REFUSAL
    # The secret scan still runs over the diff on the micro-VM: it needs no sandbox.
    assert r.gates["no_committed_secrets"] is True and "secret_scan" in r.evidence


def test_the_build_hook_tells_the_gate_where_the_build_ran():
    src = inspect.getsource(gcts._template_gates)
    assert 'substrate="microvm" if use_microvm else "local"' in src


@pytest.mark.parametrize("provider,refused", [
    ("microvm", True), ("firecracker", True), ("MicroVM", True),
    ("local", False), ("", False), (None, False)])
def test_microvm_refusal_follows_the_build_substrate_setting(provider, refused):
    settings = SimpleNamespace(governed_sandbox_provider=provider)
    assert (microvm_refusal(settings) == MICROVM_REFUSAL) is refused
    # The same answer the build itself would give for a run with no per-run override.
    assert gcts._resolve_use_microvm(SimpleNamespace(governed_sandbox_provider=provider or "local"), {}) is refused


def test_an_apply_on_a_microvm_install_is_refused_before_anything_is_read(monkeypatch):
    monkeypatch.setattr("boss_engine.config.get_settings",
                        lambda: SimpleNamespace(governed_sandbox_provider="microvm"))
    with pytest.raises(TemplateError) as err:
        # c=None: the refusal comes before any template row is read or any binding written.
        apply_service.plan(None, org_id="org_x", project_id="prj_x", template_ref="hello",
                           body={"stack": "node", "app_key": "a"}, actor_id="usr_x")
    assert err.value.status == 409
    assert err.value.body == {"error": "template_tests_need_local_sandbox", "message": MICROVM_REFUSAL}


def test_a_blocked_template_build_says_why_in_the_gates_own_words():
    error, reason = gcts.blocked_gate_messages(
        "qg_1", ["template_tests"], None, {"error": MICROVM_REFUSAL, "not_run": "microvm"})
    assert "failing gates: ['template_tests']" in error
    assert "Template tests: " + MICROVM_REFUSAL[:200] in error
    assert reason == "the applied template's tests did not pass, so the build is not done"
    # Unchanged for a block with no template gate failing.
    plain, why = gcts.blocked_gate_messages("qg_2", ["code_review_passed"], None, {"error": "x"})
    assert "Template tests" not in plain and why == "quality gate blocked and never_block is disabled"


def test_the_trial_never_runs_in_a_guest():
    # The trial is the local build sandbox (EphemeralSandbox under a sandbox uid) whatever the
    # build substrate: it never dispatches to the micro-VM provider, so it needs no refusal.
    src = inspect.getsource(trial)
    assert "EphemeralSandbox" in src and "MicroVM" not in src and "run_code_task" not in src
    assert gate.microvm_refusal.__module__ == "boss_engine.templates.gate"
