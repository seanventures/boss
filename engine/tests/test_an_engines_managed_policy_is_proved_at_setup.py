"""An engine login's ORGANISATION POLICY is detected, proved at Setup and obeyed — per login.

An enterprise install (2026-11): the operator's Codex login belonged to a ChatGPT Enterprise
workspace whose requirements forbid `danger-full-access`. Codex said so once and ran read-only;
the test author wrote nothing and BOSS reported "no test files". The contributor's fix switched
every Codex writer to `workspace-write` — which, measured on a stock install (codex-cli 0.158,
Docker's default seccomp profile), makes every Codex command die in bubblewrap and undoes
BOSSAPP-1350's review seats. So:

  * every downgrade wording in the Codex binary (0.154, 0.158) is a `policy_denied` verdict for
    a WRITER (a reviewer held read-only asked for nothing more) — never a refused login;
  * Setup runs one writer session per engine login that must write a file and run a command;
  * workspace-write is used ONLY for a managed login whose own sandbox Setup PROVED here, with
    network access and the toolchain caches writable; everything else keeps today's mode;
  * Claude Code's managed settings are read (by key, never a value) where they are on disk.

The binary is a fake `codex`; no model is called.
"""
from __future__ import annotations

import json
import os
import stat
import sys
import textwrap
from pathlib import Path

import pytest

from boss_engine.config import get_settings
from boss_engine.services import engine_contract as ec
from boss_engine.services import engine_policy as ep
from boss_engine.services import harness_failures as hf
from boss_engine.services import llm_telemetry

#: The downgrade wordings, as they are in the codex-cli binaries (strings of 0.154 and 0.158).
DOWNGRADES = [
    "Configured value for `permission_profile` is disallowed by requirements; falling back from "
    "`:danger-full-access` to `:read-only`",
    '`approval_policy = "never"` cannot be used because requirements do not allow '
    '`sandbox_mode = "danger-full-access"`; Codex would fall back to read-only',
    "configured value is disallowed by requirements; falling back to required value for "
    "`sandbox_mode`",
    "default sandbox policy is disallowed by requirements; falling back to required default",
    "derived permission profile cannot be represented as a legacy sandbox policy; falling back "
    "to read-only",
]

FAKE_CODEX = textwrap.dedent('''\
    #!{python}
    import json, os, sys
    argv = sys.argv[1:]
    prompt = sys.stdin.read()
    with open({log!r}, "a") as log:
        log.write(json.dumps({{"argv": argv}}) + "\\n")
    behaviour = open({behaviour!r}).read().strip()
    def out(o): print(json.dumps(o), flush=True)
    out({{"type": "thread.started", "thread_id": "t1"}})
    if behaviour == "downgrade_item":
        out({{"type": "item.completed", "item": {{"type": "error", "message": {downgrade!r}}}}})
    if behaviour == "downgrade_stderr":
        print("WARN codex_core::config: " + {downgrade!r}, file=sys.stderr, flush=True)
    if behaviour == "mcp_401":
        out({{"type": "item.completed", "item": {{"type": "error",
            "message": "MCP client for `boss` failed to start: 401 Unauthorized"}}}})
    final = argv[argv.index("--output-last-message") + 1]
    out({{"type": "item.completed", "item": {{"type": "agent_message", "text": "done"}}}})
    out({{"type": "turn.completed", "usage": {{"input_tokens": 1, "output_tokens": 1}}}})
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
                                      behaviour=str(behaviour), downgrade=DOWNGRADES[0]))
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("BOSS_CODE_AGENT_IDLE_TIMEOUT_SECONDS", "0")
    monkeypatch.setattr(llm_telemetry, "record_llm_call", lambda **kw: None)
    monkeypatch.setattr(llm_telemetry, "record_agent_turns", lambda *a, **kw: 0)
    inner = CodexCodeAgent(get_settings(), load_specs(get_settings())["codex"])
    inner._auth_checked = True
    inner.engine_policy = {}                 # today's mode unless a test records one
    monkeypatch.setattr(inner, "remember_rotated_credential", lambda: False)
    tree = tmp_path / "tree"
    tree.mkdir()

    def calls() -> list[dict]:
        path = tmp_path / "fake.log"
        return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []

    return inner, tree, behaviour, calls


# ── The words ────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("said", DOWNGRADES)
def test_every_codex_downgrade_wording_stops_a_writer_not_a_reviewer(said):
    verdict = hf.classify(said, engine="codex", where="stream", writer=True)
    assert verdict is not None and verdict.kind == "policy_denied"
    assert "ENGINE_SANDBOX.md" in verdict.fix
    assert hf.classify(said, engine="codex", where="stream", writer=False) is None
    assert ep.codex_downgrades(said)


def test_a_policy_words_inside_a_web_search_note_are_not_a_downgrade():
    said = "review web_search_mode is disallowed by requirements; keeping constrained value"
    assert hf.classify(said, engine="codex", where="stream", writer=True) is None


# ── The Codex driver ─────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("behaviour", ["downgrade_item", "downgrade_stderr"])
def test_a_managed_downgrade_stops_a_codex_writer_with_the_evidence(codex, behaviour):
    agent, tree, mode, _ = codex
    mode.write_text(behaviour)
    with hf.run_scope("crun_managed_codex"):
        with pytest.raises(hf.HarnessFailure) as stopped:
            ec.ContractedAgent(agent).run_role("Write the tests.", tree)
        assert hf.tripped("crun_managed_codex").kind == "policy_denied"
    assert "disallowed by requirements" in stopped.value.verdict.message


def test_a_managed_downgrade_on_a_review_seat_is_reported_not_fatal(codex):
    agent, tree, mode, _ = codex
    mode.write_text("downgrade_item")
    out = ec.ContractedAgent(agent).run_role("Review it.", tree, read_only=True)
    assert any("disallowed by requirements" in d for d in out["diagnostics"])


def test_an_mcp_401_diagnostic_neither_stops_the_writer_nor_refuses_the_login(codex,
                                                                              monkeypatch):
    from boss_engine.services import harness_refusal

    noted = []
    monkeypatch.setattr(harness_refusal, "note", lambda *a, **kw: noted.append(a))
    agent, tree, mode, _ = codex
    mode.write_text("mcp_401")
    out = ec.ContractedAgent(agent).run_role("Write the tests.", tree)
    assert out["summary"] == "done" and not noted


def test_today_s_mode_is_kept_without_a_proved_managed_policy(codex):
    agent, tree, _, calls = codex
    for policy in ({}, {"managed": True, "writer_mode": "unavailable"},
                   {"managed": True, "writer_mode": "workspace-write", "sandbox_proven": True,
                    "network": False}):
        agent.engine_policy = policy
        agent.run_role("Implement it.", tree)
        argv = calls()[-1]["argv"]
        assert argv[argv.index("--sandbox") + 1] == "danger-full-access"


def test_a_managed_login_with_a_proved_sandbox_writes_in_workspace_write(codex, tmp_path):
    agent, tree, _, calls = codex
    proved = {"managed": True, "writer_mode": "workspace-write", "sandbox_proven": True,
              "network": True}
    agent.engine_policy = proved                       # proved, but NOT where builds run
    agent.run_role("Implement it.", tree)
    assert "danger-full-access" in calls()[-1]["argv"]
    agent.engine_policy = {**proved, "proved_in": {"job": True, "isolated": True}}
    agent.run_role("Implement it.", tree)
    argv = calls()[-1]["argv"]
    assert argv[argv.index("--sandbox") + 1] == "workspace-write"
    assert "sandbox_workspace_write.network_access=true" in argv
    roots = next(a for a in argv if a.startswith("sandbox_workspace_write.writable_roots="))
    home = str(tmp_path / "home")
    for cache in (".npm", ".cache", "go", ".m2"):
        assert json.dumps(os.path.join(home, cache)) in roots
        assert (Path(home) / cache).is_dir()            # Codex needs a root to exist
    # A review seat on BOSS's copy runs in the same proved sandbox; a resumed session too.
    ec.ContractedAgent(agent).run_role("Review it.", tree, read_only=True)
    assert "workspace-write" in calls()[-1]["argv"]
    resumed = agent._codex_argv(read_only=False, model=None, resume="t1", final_path="/f",
                                env={"HOME": home})
    assert 'sandbox_mode="workspace-write"' in resumed


# ── Setup's engine check ─────────────────────────────────────────────────────────────────────

class _Writer:
    """A fake engine session for writer_check: does what `act` says in the probe directory."""

    def __init__(self, act):
        self.act = act
        self.engine_policy = None
        self.modes = []

    def run_role(self, prompt, cwd, *, read_only=False, on_turn=None, **_):
        self.modes.append(dict(self.engine_policy or {}))
        return self.act(Path(cwd), on_turn, self.engine_policy or {})


def _wrote(cwd, on_turn, policy):
    (cwd / ep.PROBE_FILE).write_text(ep.PROBE_TOKEN)
    on_turn({"direction": "tool_result", "tool_name": "Bash", "content": ep.PROBE_TOKEN,
             "ok": True})
    return {"summary": ep.PROBE_TOKEN}


def _read_only_fs(cwd, on_turn, policy):
    on_turn({"direction": "tool_result", "tool_name": "Bash", "ok": False,
             "content": "sh: 1: cannot create .boss-probe: Read-only file system"})
    return {"summary": "I could not write the file."}


def _managed_codex(cwd, on_turn, policy):
    if policy.get("writer_mode") == "workspace-write":
        return _wrote(cwd, on_turn, policy)
    raise hf.HarnessFailure(hf.classify(DOWNGRADES[0], engine="codex", where="stream",
                                        writer=True))


def test_a_login_that_writes_passes_and_keeps_todays_mode():
    policy = ep.check_engine(get_settings(), "codex", agent_factory=lambda: _Writer(_wrote))
    assert policy["can_write"] and policy["writer_mode"] == "default"
    assert not policy["managed"] and ep.setup_sentence("codex", policy) is None


def test_a_session_that_did_not_try_is_unproven_not_a_failed_setup():
    policy = ep.check_engine(get_settings(), "gemini_cli",
                             agent_factory=lambda: _Writer(lambda c, t, p: {"summary": "hi"}))
    assert policy["state"] == "unproven" and ep.setup_sentence("gemini_cli", policy) is None


def test_a_refused_write_fails_setup_with_one_sentence():
    policy = ep.check_engine(get_settings(), "opencode",
                             agent_factory=lambda: _Writer(_read_only_fs))
    assert policy["state"] == "cannot_write"
    assert "Read-only file system" in ep.setup_sentence("opencode", policy)


def test_a_managed_codex_with_a_proved_sandbox_is_recorded_workspace_write():
    writers = []

    def factory():
        writers.append(_Writer(_managed_codex))
        return writers[-1]

    policy = ep.check_engine(get_settings(), "codex", agent_factory=factory,
                             sandbox_prover=lambda: {"sandbox_proven": True, "network": True,
                                                     "writable_roots": ["/h/.npm"],
                                                     "missing_roots": []},
                             proved_in={"job": True, "isolated": True, "uid": 61001})
    assert policy["managed"] and policy["writer_mode"] == "workspace-write"
    assert policy["can_write"] and ep.uses_workspace_write(policy)
    assert policy["downgrades"] and "disallowed by requirements" in policy["downgrades"][0]
    assert writers[0].modes == [{}]                               # first: today's mode
    assert writers[1].modes[0]["writer_mode"] == "workspace-write"


@pytest.mark.parametrize("proof, says", [
    ({"sandbox_proven": False, "network": False, "error": "bwrap: No permissions to create a "
      "new namespace"}, "cannot start in this container"),
    ({"sandbox_proven": True, "network": False}, "cannot reach the package registry"),
    ({"sandbox_proven": True, "network": True, "missing_roots": ["/h/.m2"]}, "cannot start"),
])
def test_a_managed_codex_whose_sandbox_is_not_proved_fails_setup_and_keeps_its_mode(proof, says):
    policy = ep.check_engine(get_settings(), "codex",
                             agent_factory=lambda: _Writer(_managed_codex),
                             sandbox_prover=lambda: proof)
    assert policy["writer_mode"] == "unavailable" and not ep.uses_workspace_write(policy)
    sentence = ep.setup_sentence("codex", policy)
    assert says in sentence and ep.DOC in sentence
    assert ep.codex_writer_mode(policy)["mode"] == "danger-full-access"


def test_a_policy_proved_outside_the_engine_check_job_never_selects_workspace_write():
    writers = []

    def factory():
        writers.append(_Writer(_managed_codex))
        return writers[-1]

    policy = ep.check_engine(get_settings(), "codex", agent_factory=factory,
                             sandbox_prover=lambda: {"sandbox_proven": True, "network": True,
                                                     "missing_roots": []})
    assert policy["proved_in"] == {"job": False}
    assert not ep.uses_workspace_write(policy)
    assert ep.codex_writer_mode(policy)["mode"] == "danger-full-access"


def test_diagnostics_are_redacted_before_anything_keeps_them():
    token = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"

    def said(cwd, on_turn, policy):
        return {"summary": "x", "diagnostics": [f"MCP server https://x:{token}@h.example.com 401"]}

    check = ep.writer_check(get_settings(), "codex", agent=_Writer(said))
    assert token not in json.dumps(check)


# ── Connect queues the check; the check runs where builds run ────────────────────────────────

@pytest.fixture
def connecting(monkeypatch):
    """connect_coding_agent with the CLI, the governed proof and the broker faked."""
    from types import SimpleNamespace

    from boss_engine.services import cli_harness, setup_service

    monkeypatch.setattr(cli_harness, "detect", lambda *_a, **_kw: {
        "installed": True, "authenticated": True, "version": "grok 1.0.40",
        "auth_detail": "Available models", "ready": True})
    monkeypatch.setattr(setup_service, "governed_proof",
                        lambda *a, **kw: {"ok": True, "exit_code": 0, "replied": "BOSS_OK"})
    monkeypatch.setattr("boss_engine.services.entitlements.EntitlementBrokerService",
                        lambda _settings: SimpleNamespace(upsert_source=lambda *a, **k: None))
    started, ran = [], []
    monkeypatch.setattr(ep, "start_check", lambda conn, project, provider, source, actor: (
        started.append((project, provider, source, actor)) or {"state": "checking",
                                                               "job_id": "job_1"}))
    monkeypatch.setattr(ep, "check_engine", lambda *a, **kw: ran.append(a) or {})

    class _Conn:
        engine = object()

        def begin_nested(self):
            import contextlib

            return contextlib.nullcontext()

    def connect():
        return setup_service.connect_coding_agent(
            _Conn(), SimpleNamespace(boss_edition="enterprise"), "project", "grok",
            actor="member")

    return connect, started, ran


def test_connect_queues_the_check_and_returns_at_once(connecting):
    connect, started, ran = connecting
    out = connect()
    assert out["ok"] and out["engine_check"] == {"state": "checking", "job_id": "job_1"}
    assert started == [("project", "grok", "xai_grok_build", "member")]
    assert not ran                                   # nothing ran inside the request


class _Ident:
    uid = 61007

    def __init__(self, root):
        self.run_dir = root

    def workspace(self, prefix):
        path = self.run_dir / f"{prefix}x"
        path.mkdir()
        return path


@pytest.fixture
def job(monkeypatch, tmp_path):
    """run_check with the sandbox identity, the governed proof and the record faked."""
    import contextlib

    from boss_engine.services import owned_process, sandbox_identity

    seen = {"owners": [], "governed": [], "recorded": []}
    ident = _Ident(tmp_path)

    @contextlib.contextmanager
    def bind(owner_id, project_id=None):
        seen["owners"].append((owned_process.current_owner().kind, owner_id, project_id))
        yield ident

    monkeypatch.setattr(sandbox_identity, "bind", bind)
    monkeypatch.setattr(sandbox_identity, "network_fenced", lambda: True)
    monkeypatch.setattr(ep, "record", lambda conn, source, policy: seen["recorded"].append(
        (source, policy)))

    def governed(engine, settings, project_id, provider, actor, prove=None, amount=None, **kw):
        seen["governed"].append(amount)
        return prove()

    class _Engine:
        def begin(self):
            return contextlib.nullcontext(object())

    def run(check, **kw):
        return ep.run_check(_Engine(), get_settings(), job_id="j1", project_id="p",
                            provider=kw.get("provider", "codex"), source_id="openai_codex_chatgpt",
                            actor="ceo", check=check, governed=governed)

    return run, seen, ident


def test_the_check_runs_under_a_builds_sandbox_identity_and_records_per_login(job):
    run, seen, ident = job
    got = {}

    def check(settings, provider, *, workdir, proved_in):
        got["dir"], got["where"] = workdir(), proved_in
        return {"engine": provider, "state": "ok", "writer_mode": "default"}

    out = run(check)
    assert out["state"] == "ok"
    assert seen["owners"] == [("engine_check", "engchk_j1", "p")]
    assert got["where"] == {"job": True, "isolated": True, "uid": 61007, "network_fenced": True}
    assert got["dir"].parent == ident.run_dir         # inside the run's own directory
    assert seen["recorded"][0][0] == "openai_codex_chatgpt"
    assert seen["governed"] == [0]                    # a subscription reserves $0


def test_a_metered_login_reserves_the_checks_spend(job, monkeypatch):
    run, seen, _ = job
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
    run(lambda settings, provider, **kw: {"state": "ok"})
    assert seen["governed"] == [ep.CHECK_ESTIMATE_USD_MICROS]


def test_a_check_that_could_not_run_is_recorded_as_not_checked(job):
    run, seen, _ = job

    def broken(settings, provider, **kw):
        raise RuntimeError("engine not installed in this image")

    assert run(broken)["state"] == "not_checked"
    policy = seen["recorded"][0][1]
    assert policy["state"] == "not_checked" and "not installed" in policy["error"]
    assert ep.summary(policy)["label"] == "Not checked"


def test_setup_shows_checking_then_the_result():
    assert ep.summary({"state": "checking"})["label"] == "Checking…"
    refused = ep.summary({"engine": "opencode", "state": "cannot_write",
                          "writer_check": {"refusal": "Read-only file system"}}, "opencode")
    assert refused["tone"] == "red" and "cannot write here" in refused["tip"]
    assert ep.summary({}) is None


def test_the_engine_check_job_is_wired_from_worker_to_api():
    from boss_engine.identity.job_permissions import JOB_KIND_PERMISSIONS

    runner = (Path(__file__).resolve().parents[2] / "apps/worker/boss_worker/runner.py").read_text()
    assert '"engine.check"' in runner and "client.run_engine_check(" in runner
    assert ep.JOB_KIND in JOB_KIND_PERMISSIONS
    setup_router = (Path(__file__).resolve().parents[2] / "apps/api/app/routers/setup.py").read_text()
    assert '"/v1/setup/engine-check/execute"' in setup_router


def test_the_codex_sandbox_proof_reads_write_caches_and_network(tmp_path):
    env = {"HOME": str(tmp_path), "PATH": os.environ.get("PATH", "")}
    roots = ep.cache_roots(env)

    def ran(argv, **kw):
        assert argv[:2] == ["codex", "sandbox"] and 'sandbox_mode="workspace-write"' in argv
        assert "sandbox_workspace_write.network_access=true" in argv
        out = "BOSS_WRITE_OK\n" + "".join(f"BOSS_ROOT_OK {r}\n" for r in roots) + "BOSS_NET_OK\n"
        return type("R", (), {"stdout": out, "stderr": ""})()

    proof = ep.codex_sandbox_proof("codex", env=env, run=ran)
    assert proof["sandbox_proven"] and proof["network"] and not proof["missing_roots"]

    def blocked(argv, **kw):
        return type("R", (), {"stdout": "", "stderr": "bwrap: No permissions to create a new "
                                                     "namespace"})()

    proof = ep.codex_sandbox_proof("codex", env=env, run=blocked)
    assert not proof["sandbox_proven"] and "namespace" in proof["error"]


def test_claude_managed_settings_are_read_by_key_never_by_value(tmp_path):
    managed = tmp_path / "managed-settings.json"
    managed.write_text(json.dumps({
        "sandbox": {"enabled": True, "allowUnsandboxedCommands": False},
        "permissions": {"deny": ["Bash(curl:*)"], "disableBypassPermissionsMode": "disable"},
        "env": {"SOME_TOKEN": "s3cr3t-value"}, "apiKeyHelper": "/bin/secret"}))
    found = ep.claude_managed_settings((str(managed), str(tmp_path / "absent.json")))
    assert found["managed"] and found["sandbox_forced"] and found["bypass_disabled"]
    assert found["unsandboxed_commands_allowed"] is False and found["denies"] == ["Bash(curl:*)"]
    assert "s3cr3t" not in json.dumps(found) and "/bin/secret" not in json.dumps(found)
    assert ep.claude_managed_settings((str(tmp_path / "absent.json"),)) == {
        "managed": False, "files": []}


def test_claude_policy_words_stop_a_writer_only():
    said = "Bash is blocked by enterprise managed policy"
    assert hf.classify(said, engine="claude_code", where="tool_result", ok=False,
                       writer=True).kind == "policy_denied"
    assert hf.classify(said, engine="claude_code", where="tool_result", ok=False,
                       writer=False) is None
    # A deny rule BOSS set itself is not the organisation's policy.
    assert hf.classify("Permission to use Write has been denied.", engine="claude_code",
                       where="tool_result", ok=False, writer=True) is None
