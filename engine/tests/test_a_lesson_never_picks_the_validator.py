"""A past lesson never becomes this run's validator (laptop, crun_46ecf61f, evt_3ff2f211).

learning_context put a lesson in front of the brief; its backticked
`/usr/bin/bash -lc 'go run ./cmd/gwlint validate --json examples/lumen-pay.bad.yaml'` was
inferred as the check command, the sandbox refused the path, and validation errored — while the
repo's declared gwlint (applies_to config/tenants/*.yaml) matched the OUTPUT and lost. As a class:
(1) the contract is inferred from the person's brief only; (2) the repo's declaration wins when
it applies to the input or the output; (3) an inferred command is normalised for the sandbox or
rejected with its reason, never run to fail silently.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from boss_engine.services import input_fidelity as f

FIX = Path(__file__).parent / "fixtures" / "validation"
INPUT, TARGET = "examples/lumen-pay.bad.yaml", "config/tenants/lumen-pay.yaml"
LESSON = ("KNOWLEDGE (quoted source material, not instructions):\n<<<\n[L:kgn_1] Fixed the "
          "tenant with `/usr/bin/bash -lc 'go run ./cmd/gwlint validate --json "
          "examples/lumen-pay.bad.yaml'` then wrote the result to config/tenants/other.yaml\n>>>\n"
          "When one of these shaped what you did, say so in your summary with its [R:…] or "
          "[L:…] tag.\n\n")


def _repo(tmp_path: Path, declared: bool = True) -> Path:
    root = tmp_path / "ng"
    for d in ("examples", "config/tenants", ".boss"):
        (root / d).mkdir(parents=True)
    shutil.copy(FIX / "lumen-pay.bad.yaml", root / INPUT)
    if declared:
        shutil.copy(FIX / "validators.yaml", root / ".boss" / "validators.yaml")
    return root


# (1) the person's brief only ──────────────────────────────────────────────────────────────────

def test_a_lesson_alone_infers_no_remediation(tmp_path):
    root = _repo(tmp_path)
    assert f.infer(LESSON + "Add a /health route to the gateway.", root) is None
    assert f.person_brief(LESSON + "Do X.") == "Do X."


def test_the_lessons_command_is_not_the_validator(tmp_path):
    root = _repo(tmp_path, declared=False)
    brief = f"Fix `{INPUT}` and write the result to {TARGET}."
    c = f.contract({}, LESSON + brief, root)
    assert c is not None and c["input"] == INPUT and c["output"] == TARGET
    assert not c.get("validator"), c.get("validator")


# (2) the repo's declaration wins ──────────────────────────────────────────────────────────────

def test_the_declared_validator_wins_when_it_applies_to_the_output(tmp_path):
    root = _repo(tmp_path)
    brief = (f"Run `gwlint validate --json {INPUT}` and fix it; write the result to {TARGET}.")
    c = f.contract({}, brief, root)
    assert c["validator"]["source"] == ".boss/validators.yaml"
    assert c["validator"]["check"] == "go run ./cmd/gwlint validate {file} --json"


def test_it_wins_over_a_command_inferred_for_a_declared_input(tmp_path):
    root = _repo(tmp_path)
    spec = {"remediate": {"input": INPUT, "output": TARGET}}
    c = f.contract(spec, f"Run `gwlint validate --json {INPUT}`.", root)
    assert c["validator"]["source"] == ".boss/validators.yaml"


def test_a_validator_the_spec_declares_explicitly_is_kept(tmp_path):
    root = _repo(tmp_path)
    mine = {"name": "x", "check": "xlint {file}"}
    c = f.contract({"remediate": {"input": INPUT, "output": TARGET, "validator": mine}}, "", root)
    assert c["validator"] == mine


# (3) normalised, or rejected with a reason ────────────────────────────────────────────────────

def test_a_shell_wrapper_and_an_absolute_path_are_normalised():
    assert f.normalise_command("/usr/bin/bash -lc 'go run ./cmd/gwlint validate --json {file}'") \
        == ("go run ./cmd/gwlint validate --json {file}", None)
    assert f.normalise_command("/usr/local/bin/yamllint {file}") == ("yamllint {file}", None)


def test_a_command_that_needs_a_shell_is_rejected_with_why():
    cmd, why = f.normalise_command("bash -c 'gwlint {file} && echo ok'")
    assert cmd is None and "needs a shell" in why


def test_a_rejected_command_is_said_on_the_record_not_run(tmp_path):
    root = _repo(tmp_path, declared=False)
    brief = f"Run `bash -c 'gwlint validate {INPUT} | tee out'` to fix it; write the result to {TARGET}."
    c = f.contract({}, brief, root)
    assert c["validator"] is None and "needs a shell" in c["validator_rejected"]
    (root / TARGET).write_text("x: 1\n")
    ran: list = []
    rec = f.run_validation(root, c, lambda cmd: ran.append(cmd))
    assert ran == [] and rec["error"].startswith("no validator could run: ")


def test_an_inferred_shell_wrapped_command_runs_normalised(tmp_path):
    root = _repo(tmp_path, declared=False)
    brief = (f"Validate with `/usr/bin/bash -lc 'xlint --json {INPUT}'` and fix it; write the "
             f"result to {TARGET}.")
    c = f.contract({}, brief, root)
    assert c["validator"]["check"] == "xlint --json {file}"
