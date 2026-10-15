"""The Codex driver (codex_code_agent) keeps every safeguard a generic CLI session has.

`make_code_agent` builds `CodexCodeAgent` for engine "codex"; it overrides `run_role` whole, so
each safeguard GenericCliCodeAgent.run_role carries has to be carried here too, or Codex quietly
runs without it. The driver was first written before two of them landed, and missed both:

  * the model-key proxy scope (model_proxy.scoped): the run tokens a session is issued die with
    the session; outside it they live until their TTL;
  * the harness verdict (harness_failures): a Codex whose sandbox cannot start stops the stage
    at once, and every later session of that run refuses — instead of handing on blind work;

and two more it got subtly wrong:

  * billing: "metered" read the environment a SECOND `_env()` call built (a second proxy token),
    not the one the child ran with;
  * a lost session (Codex no longer has it) was continued with a cold prompt, though BOSS knows
    what that session was asked and reported (engine_contract.seeded_prompt).

The binary is a fake `codex` that speaks `codex exec --json`; no model is called.
"""
from __future__ import annotations

import contextlib
import json
import os
import stat
import sys
import textwrap

import pytest

from boss_engine.config import get_settings
from boss_engine.services import engine_contract as ec
from boss_engine.services import harness_failures as hf
from boss_engine.services import llm_telemetry

FAKE_CODEX = textwrap.dedent('''\
    #!{python}
    import json, os, sys, uuid
    argv = sys.argv[1:]
    prompt = sys.stdin.read()
    with open({log!r}, "a") as log:
        log.write(json.dumps({{"argv": argv, "prompt": prompt,
                               "key": os.environ.get("OPENAI_API_KEY", "")}}) + "\\n")
    behaviour = open({behaviour!r}).read().strip()
    resume = len(argv) > 1 and argv[1] == "resume"
    if resume and behaviour == "lost_session":
        print("Error: no rollout found for thread id", file=sys.stderr)
        sys.exit(1)
    def out(o): print(json.dumps(o), flush=True)
    tid = argv[-2] if resume else str(uuid.uuid4())
    out({{"type": "thread.started", "thread_id": tid}})
    if behaviour == "sandbox_broken":
        print("Error: Landlock ruleset is not supported by this kernel", flush=True)
        sys.exit(1)
    final = argv[argv.index("--output-last-message") + 1]
    out({{"type": "item.completed", "item": {{"type": "agent_message", "text": "done"}}}})
    out({{"type": "turn.completed", "usage": {{"input_tokens": 10, "output_tokens": 2}}}})
    open(final, "w").write("done")
''')


@pytest.fixture
def codex(tmp_path, monkeypatch):
    from boss_engine.services.cli_harness import load_specs
    from boss_engine.services.codex_code_agent import CodexCodeAgent

    behaviour = tmp_path / "behaviour"
    behaviour.write_text("ok")
    fake = tmp_path / "codex"
    fake.write_text(FAKE_CODEX.format(python=sys.executable, log=str(tmp_path / "fake.log"),
                                      behaviour=str(behaviour)))
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    monkeypatch.setenv("BOSS_CODE_AGENT_IDLE_TIMEOUT_SECONDS", "0")
    monkeypatch.setattr(llm_telemetry, "record_llm_call", lambda **kw: None)
    monkeypatch.setattr(llm_telemetry, "record_agent_turns", lambda *a, **kw: 0)
    agent = CodexCodeAgent(get_settings(), load_specs(get_settings())["codex"])
    agent._auth_checked = True              # the credential route has its own tests
    monkeypatch.setattr(agent, "remember_rotated_credential", lambda: False)
    tree = tmp_path / "tree"
    tree.mkdir()

    def calls() -> list[dict]:
        path = tmp_path / "fake.log"
        return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []

    return agent, tree, behaviour, calls


def test_make_code_agent_builds_the_codex_driver_for_codex():
    from boss_engine.services.code_agent import make_code_agent
    from boss_engine.services.codex_code_agent import CodexCodeAgent

    agent = make_code_agent(get_settings(), engine="codex")
    assert isinstance(ec.adapter_of(agent), CodexCodeAgent)
    assert isinstance(agent, ec.ContractedAgent) or isinstance(
        getattr(agent, "_inner", None), ec.ContractedAgent)


def test_a_codex_session_runs_inside_the_model_proxy_scope(codex, monkeypatch):
    from boss_engine.services import model_proxy

    agent, tree, _, _ = codex
    scopes: list[str] = []
    real = model_proxy.session_scope

    @contextlib.contextmanager
    def watched():
        scopes.append("open")
        with real() as usage:
            yield usage
        scopes.append("closed")

    monkeypatch.setattr(model_proxy, "session_scope", watched)
    agent.run_role("Implement it.", tree)
    assert scopes == ["open", "closed"]


def test_a_codex_session_builds_its_environment_once(codex, monkeypatch):
    agent, tree, _, _ = codex
    built: list[int] = []
    real = agent._env

    def counted():
        built.append(1)
        return real()

    monkeypatch.setattr(agent, "_env", counted)
    agent.run_role("Implement it.", tree)
    assert len(built) == 1        # each call issues a proxy run token; one session, one token


def test_codex_on_a_chatgpt_login_is_a_subscription_and_on_a_key_is_metered(codex, monkeypatch):
    from boss_engine.services.included_spend import BILLING_METERED, BILLING_SUBSCRIPTION

    agent, tree, _, _ = codex
    monkeypatch.setattr(agent, "_env", lambda: {"PATH": os.environ["PATH"], "HOME": "/tmp"})
    assert agent.run_role("Implement it.", tree)["usage"]["billing"] == BILLING_SUBSCRIPTION
    monkeypatch.setattr(agent, "_env", lambda: {"PATH": os.environ["PATH"], "HOME": "/tmp",
                                                "OPENAI_API_KEY": "boss-run-token"})
    assert agent.run_role("Implement it.", tree)["usage"]["billing"] == BILLING_METERED


def test_a_codex_whose_sandbox_cannot_start_stops_the_run(codex):
    agent, tree, behaviour, calls = codex
    behaviour.write_text("sandbox_broken")
    with hf.run_scope("crun_codex_driver"):
        with pytest.raises(hf.HarnessFailure) as stopped:
            agent.run_role("Implement it.", tree)
        assert stopped.value.verdict.engine == "codex"
        assert stopped.value.verdict.kind == "sandbox_unavailable"
        # The run's latch: the next session refuses before it starts (no second process).
        with pytest.raises(hf.HarnessFailure):
            agent.run_role("Repair it.", tree)
    hf.reset("crun_codex_driver")
    assert len(calls()) == 1


def test_a_lost_codex_session_is_continued_with_what_it_held(codex):
    agent, tree, behaviour, calls = codex
    held = ec.contracted(agent)
    first = held.run_role("TASK: build the slug service (marker 51c2)", tree)
    behaviour.write_text("lost_session")
    held.run_role("Now fix what the reviewer found.", tree, resume_session_id=first["session_id"])
    resumed, fresh = calls()[-2], calls()[-1]
    assert resumed["argv"][1] == "resume"          # the native resume was tried first
    assert fresh["argv"][1] != "resume"            # then a new session ...
    assert "marker 51c2" in fresh["prompt"]        # ... carrying what the lost one was asked
    assert "Now fix what the reviewer found." in fresh["prompt"]


def test_a_reviewer_on_codex_cannot_resume_a_writing_session(codex):
    agent, tree, _, calls = codex
    agent.run_role("Review it.", tree, read_only=True, resume_session_id="thread-1")
    argv = calls()[-1]["argv"]
    assert argv[1] != "resume" and "read-only" in argv
