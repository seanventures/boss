"""Engine inner-sandbox support: `boss doctor` and the Setup engine check say which layer stops a
coding engine's own sandbox, in plain words, with the fix (docs/community/ENGINE_SANDBOX.md).

The install opts in with BOSS_ENGINE_INNER_SANDBOX in .env; scripts/install/compose_files.sh
puts the same variable in the containers it creates, so the container knows the mode it was
created with. Off by default: nothing is probed and nothing changes.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from boss_engine.config import get_settings
from boss_engine.services import engine_inner_sandbox as eis
from boss_engine.services import engine_policy as ep

ROOT = Path(__file__).resolve().parents[2]
BWRAP = ("bwrap: No permissions to create a new namespace, likely because the kernel does not "
         "allow non-privileged user namespaces.")


def _ran(code=0, out=""):
    return lambda argv, **_: SimpleNamespace(returncode=code, stdout=out, stderr="")


def test_mode_comes_from_the_container_and_defaults_off():
    assert eis.mode({}) == "off"
    assert eis.mode({eis.ENV: ""}) == "off"
    assert eis.mode({eis.ENV: "seccomp"}) == "seccomp"
    assert eis.mode({eis.ENV: '"seccomp+apparmor"'}) == "seccomp+apparmor"
    assert eis.mode({eis.ENV: "something-else"}) == "off"


def test_the_probe_is_codex_sandbox_running_true_with_no_model():
    seen = []

    def run(argv, **kw):
        seen.append(argv)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    assert eis.probe("codex", run=run)["ok"]
    assert seen == [["codex", "sandbox", "-c", 'sandbox_mode="workspace-write"', "--", "true"]]


def test_a_missing_binary_or_a_hang_is_reported_not_raised():
    def missing(argv, **_):
        raise FileNotFoundError("codex")

    def hang(argv, **_):
        raise subprocess.TimeoutExpired(argv, 60)

    assert "could not run" in eis.probe(run=missing)["output"]
    assert "did not finish" in eis.probe(run=hang)["output"]


@pytest.mark.parametrize("expected,actual,label,result,layer,words", [
    # .env says on, the containers predate it.
    ("seccomp", "off", None, {"ok": False, "output": BWRAP}, "not_applied", "boss start"),
    # AppArmor host, the overlay applied, but the container is not under boss-engine.
    ("seccomp+apparmor", "seccomp+apparmor", "docker-default (enforce)", {"ok": True},
     "apparmor_profile", "load_apparmor.sh"),
    # Under boss-engine, bubblewrap still refused: AppArmor is the layer.
    ("seccomp+apparmor", "seccomp+apparmor", "boss-engine (enforce)",
     {"ok": False, "output": BWRAP}, "apparmor", "load_apparmor.sh"),
    # No AppArmor, seccomp applied, still refused: the host kernel.
    ("seccomp", "seccomp", None, {"ok": False, "output": BWRAP}, "kernel",
     "user.max_user_namespaces"),
    # Something else entirely.
    ("seccomp", "seccomp", None, {"ok": False, "output": "codex: not found"}, "engine",
     "codex --version"),
])
def test_each_failing_layer_is_named_in_plain_words_with_the_fix(expected, actual, label, result,
                                                                 layer, words):
    found = eis.diagnose(expected=expected, actual=actual, label=label, result=result)
    assert not found["ok"] and found["layer"] == layer
    assert words in found["sentence"] + found["fix"]


def test_a_host_without_apparmor_passes_on_seccomp_alone_and_says_so():
    found = eis.diagnose(expected="seccomp+apparmor", actual="seccomp+apparmor", label=None,
                         result={"ok": True})
    assert found["ok"] and "no AppArmor" in found["sentence"]


def test_the_working_case_is_ok():
    found = eis.diagnose(expected="seccomp+apparmor", actual="seccomp+apparmor",
                         label="boss-engine (enforce)", result={"ok": True})
    assert found["ok"] and found["layer"] == ""


def test_doctor_prints_one_line_and_exits_by_verdict(monkeypatch, capsys):
    monkeypatch.setenv(eis.ENV, "seccomp")
    monkeypatch.setattr(eis, "apparmor_label", lambda *a: None)
    monkeypatch.setattr(eis, "probe", lambda *a, **k: {"ok": False, "output": BWRAP})
    assert eis.main(["--expect", "seccomp"]) == 1
    line = capsys.readouterr().out.strip()
    assert line.startswith("bad [kernel] ") and "Fix:" in line
    monkeypatch.setattr(eis, "probe", lambda *a, **k: {"ok": True, "output": ""})
    assert eis.main(["--expect", "seccomp"]) == 0
    assert capsys.readouterr().out.startswith("ok ")


# ── Setup's engine check ──────────────────────────────────────────────────────────────────────

class _Writer:
    def __init__(self):
        self.engine_policy = None

    def run_role(self, prompt, cwd, *, read_only=False, on_turn=None, **_):
        (Path(cwd) / ep.PROBE_FILE).write_text(ep.PROBE_TOKEN)
        on_turn({"direction": "tool_result", "tool_name": "Bash", "content": ep.PROBE_TOKEN,
                 "ok": True})
        return {"summary": ep.PROBE_TOKEN}


def test_off_by_default_the_engine_check_never_probes(monkeypatch):
    monkeypatch.delenv(eis.ENV, raising=False)
    monkeypatch.setattr(ep, "inner_sandbox_check",
                        lambda settings: pytest.fail("probed with the support off"))
    policy = ep.check_engine(get_settings(), "codex", agent_factory=_Writer)
    assert "inner_sandbox" not in policy
    assert "inner_sandbox" not in ep.summary(policy, "codex")


def test_when_on_setup_shows_the_failing_layer_even_if_the_writer_works(monkeypatch):
    monkeypatch.setenv(eis.ENV, "seccomp+apparmor")
    failing = eis.diagnose(expected="seccomp+apparmor", actual="seccomp+apparmor",
                           label="docker-default (enforce)", result={"ok": True})
    monkeypatch.setattr(ep, "inner_sandbox_check", lambda settings: failing)
    policy = ep.check_engine(get_settings(), "codex", agent_factory=_Writer)
    assert policy["can_write"] and policy["inner_sandbox"]["layer"] == "apparmor_profile"
    shown = ep.summary(policy, "codex")
    assert shown["state"] == "ok" and "load_apparmor.sh" in shown["tip"]
    assert shown["inner_sandbox"] == {"ok": False, "layer": "apparmor_profile", "mode": ""}


def test_a_managed_login_names_the_layer_in_its_cannot_write_sentence():
    policy = {"state": "cannot_write", "managed": True, "sandbox_proven": False,
              "inner_sandbox": eis.diagnose(expected="seccomp", actual="seccomp", label=None,
                                            result={"ok": False, "output": BWRAP})}
    sentence = ep.setup_sentence("codex", policy)
    assert "refuses unprivileged user namespaces" in sentence and "Fix:" in sentence


def test_checkup_runs_the_probe_in_the_worker_only_when_env_opts_in():
    checkup = (ROOT / "scripts/install/checkup.sh").read_text()
    assert "boss_engine.services.engine_inner_sandbox --expect" in checkup
    assert 'engine_mode="$(env_value BOSS_ENGINE_INNER_SANDBOX' in checkup
