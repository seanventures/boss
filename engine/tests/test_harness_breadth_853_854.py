"""P2-10 harness breadth (BOSSAPP-853 + 854), held by scripted fakes only — no vendor is called.

853  * an ACP agent: session lifecycle, tool-call events mapped onto the engine contract's
       canonical names, permission requests answered by BOSS's existing mid-run approval path
       (the MCP gateway when on, the pre-814 "awaiting approval" stop when off);
     * Gemini CLI and OpenCode registered as ACP-backed, UNVERIFIED harnesses;
     * the `process` and `http` harness kinds, each end to end against a conformance fake;
     * installable adapters: a manifest behind the trust/checksum gate, a probe, no plugin code.
854  * every harness's MCP servers and skills: a delivery matrix per engine, and a conformance run
       in which each engine's scripted fake reads its config the way that engine does.
"""
from __future__ import annotations

import dataclasses
import json
import os
import stat
import sys
import textwrap
import threading
import tomllib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from boss_engine.config import get_settings
from boss_engine.services import acp_protocol as acp
from boss_engine.services import harness_delivery as hd
from boss_engine.services import llm_telemetry
from boss_engine.services.cli_harness import BUILTIN_CLI_HARNESSES, CliHarnessUnavailable
from boss_engine.services.code_task_mcp import HarnessMcp, skill_markdown, write_skills_bundle

FAKE_ACP = Path(__file__).parent / "fakes" / "fake_acp_agent.py"


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.setattr(llm_telemetry, "record_llm_call", lambda **kw: None)
    monkeypatch.setattr(llm_telemetry, "record_agent_turns", lambda *a, **kw: 0)
    monkeypatch.setenv("BOSS_CODE_AGENT_IDLE_TIMEOUT_SECONDS", "0")
    monkeypatch.delenv("BOSS_MCP_GATEWAY", raising=False)


def _exe(path: Path, body: str) -> Path:
    path.write_text(f"#!{sys.executable}\n{body}")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def _mcp(tmp_path: Path, *, disallowed=(), gateway_token: str = "", skills: bool = False
         ) -> HarnessMcp:
    """What code_task_mcp.prepare hands a harness: an mcp.json with two servers."""
    directory = tmp_path / "mcp"
    directory.mkdir(exist_ok=True)
    path = directory / "mcp.json"
    path.write_text(json.dumps({"mcpServers": {
        "library_docs": {"type": "http", "url": "https://docs.example/mcp",
                         "headers": {"Authorization": "Bearer up-stream"}},
        "tickets": {"type": "http", "url": "https://tickets.example/mcp"},
    }}))
    mcp = HarnessMcp(path=str(path), servers=["library_docs", "tickets"],
                     disallowed=list(disallowed), gateway_token=gateway_token)
    if skills:
        mcp.skills_dir = write_skills_bundle([
            {"skill_key": "release_notes", "label": "Release notes",
             "summary": "Write release notes", "knowledge": "Group by area."}])
    return mcp


# ══ 853 · ACP ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("kind,name", [("read", "Read"), ("edit", "Edit"), ("delete", "Edit"),
                                       ("move", "Edit"), ("search", "Grep"),
                                       ("execute", "Bash"), ("fetch", "WebFetch"),
                                       ("think", "TodoWrite")])
def test_every_acp_tool_kind_maps_onto_a_canonical_name(kind, name):
    assert acp.canonical_tool({"kind": kind, "title": "whatever"}) == name


def test_an_other_tool_falls_back_to_its_title_and_keeps_an_mcp_name():
    assert acp.canonical_tool({"kind": "other", "title": "read_file"}) == "Read"
    assert acp.canonical_tool({"kind": "other", "title": "Read src/a.py"}) == "Read"
    assert acp.canonical_tool({"kind": "other", "title": "mcp__docs__search"}) == \
        "mcp__docs__search"


def test_tool_input_carries_the_profiles_keys():
    args = acp.tool_input({"kind": "edit", "locations": [{"path": "/w/a.py"}],
                           "rawInput": {"content": "x"}})
    assert args["file_path"] == "/w/a.py"
    assert acp.tool_input({"kind": "execute", "rawInput": {"cmd": ["ls", "-l"]}})["command"] \
        == "ls -l"


def test_boss_never_answers_allow_always():
    options = [{"optionId": "a", "kind": "allow_always"}, {"optionId": "o", "kind": "allow_once"},
               {"optionId": "r", "kind": "reject_once"}]
    assert acp.pick_option(options, True) == "o"
    assert acp.pick_option(options, False) == "r"
    assert acp.permission_outcome(None) == {"outcome": {"outcome": "cancelled"}}


@pytest.fixture
def acp_agent(tmp_path, monkeypatch):
    from boss_engine.services.acp_code_agent import AcpCodeAgent

    bindir = tmp_path / "bin"
    bindir.mkdir()
    _exe(bindir / "fake-acp", f"import runpy; runpy.run_path({str(FAKE_ACP)!r}, "
                              "run_name='__main__')\n")
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")
    record = tmp_path / "acp.jsonl"
    monkeypatch.setenv("FAKE_ACP_RECORD", str(record))
    spec = dataclasses.replace(BUILTIN_CLI_HARNESSES["opencode"], name="fake_acp",
                               binary="fake-acp", args=(), timeout=60, review_timeout=60,
                               env_passthrough=("FAKE_ACP_RECORD", "FAKE_ACP_SCENARIO",
                                                "FAKE_ACP_VERSION", "FAKE_ACP_NO_HTTP"))
    agent = AcpCodeAgent(get_settings(), spec)
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "README.md").write_text("readme")

    def records(kind=None):
        rows = [json.loads(x) for x in record.read_text().splitlines()] if record.exists() \
            else []
        return [r for r in rows if kind is None or r["kind"] == kind]

    return agent, tree, records


def test_an_acp_session_runs_end_to_end_and_its_events_land_canonically(acp_agent):
    agent, tree, records = acp_agent
    turns: list[dict] = []
    out = agent.run_role("Write hello.txt", tree, on_turn=turns.append)
    assert out["stop_reason"] == "done"
    assert out["summary"] == "Done."
    assert out["session_id"] == "sess-1"
    assert (tree / "hello.txt").read_text() == "hello"       # through fs/write_text_file
    assert records("read")[0]["params"]["result"] == {"content": "hello"}
    calls = [t["tool_name"] for t in turns if t["direction"] == "tool_call"]
    assert calls == ["Read", "Edit"]
    results = [(t["tool_name"], t["ok"]) for t in turns if t["direction"] == "tool_result"]
    assert results == [("Read", True), ("Edit", True)]
    edit = next(t for t in turns if t["tool_name"] == "Edit" and t["direction"] == "tool_call")
    assert edit["content"]["file_path"].endswith("hello.txt")
    init = records("initialize")[0]["params"]
    assert init["protocolVersion"] == acp.PROTOCOL_VERSION
    assert init["clientCapabilities"] == {"fs": {"readTextFile": True, "writeTextFile": True},
                                          "terminal": False}
    assert records("session/new")[0]["params"]["cwd"] == str(tree)
    assert out["permissions"] == [{"tool": "Edit", "allow": True, "reason": "allow",
                                   "held": False, "decision_id": None}]


def test_a_follow_up_turn_loads_the_session_and_does_not_replay_it(acp_agent):
    agent, tree, records = acp_agent
    out = agent.run_role("again", tree, resume_session_id="sess-1")
    assert records("session/load")[0]["params"]["sessionId"] == "sess-1"
    assert not records("session/new")
    assert "REPLAYED" not in out["summary"]
    assert out["session_id"] == "sess-1"


def test_an_ask_tool_without_the_gateway_ends_the_turn_awaiting_approval(acp_agent):
    """Pre-814: the native loop's behaviour — the call does not run, the turn stops waiting."""
    agent, tree, records = acp_agent
    agent.tool_policy = {"Edit": "ask"}
    out = agent.run_role("Write hello.txt", tree)
    assert out["stop_reason"] == "awaiting_approval"
    assert out["awaiting_approval"] is True
    assert out["pending_tool"] == "Edit"
    assert out["pending_args_sha256"]
    assert not (tree / "hello.txt").exists()
    assert records("permission")[0]["params"]["outcome"]["optionId"] == "no"


def test_an_off_tool_is_rejected_and_the_session_carries_on(acp_agent):
    agent, tree, _ = acp_agent
    agent.tool_policy = {"Edit": "off"}
    out = agent.run_role("Write hello.txt", tree)
    assert out["stop_reason"] == "done"
    assert out["summary"] == "The change was refused."
    assert not (tree / "hello.txt").exists()


def test_a_read_only_session_refuses_writes(acp_agent):
    agent, tree, records = acp_agent
    out = agent.run_role("Review", tree, read_only=True, _contract_read_only_copy=True)
    assert not (tree / "hello.txt").exists()
    assert out["permissions"][0]["reason"] == "read_only"
    caps = records("initialize")[0]["params"]["clientCapabilities"]
    assert caps["fs"]["writeTextFile"] is False


class _FakeGrant:
    def live(self):
        return True


class _FakeGateway:
    def __init__(self, status):
        self.status = status
        self.raised: list[dict] = []

    def grant_for(self, token):
        return _FakeGrant() if token == "tok" else None

    def raise_decision(self, grant, **kw):
        self.raised.append(kw)
        return "dec-1"

    def park(self, grant, decision_id, sink=None, **kw):
        return {"status": self.status, "note": None}


@pytest.mark.parametrize("status,written", [("approved", True), ("rejected", False)])
def test_with_the_gateway_an_ask_parks_on_a_decision_a_person_answers(acp_agent, monkeypatch,
                                                                      tmp_path, status, written):
    from boss_engine.services import mcp_gateway

    agent, tree, _ = acp_agent
    gateway = _FakeGateway(status)
    monkeypatch.setenv("BOSS_MCP_GATEWAY", "on")
    monkeypatch.setattr(mcp_gateway, "get", lambda: gateway)
    agent.tool_policy = {"Edit": "ask"}
    agent.mcp_harness = HarnessMcp(path="", servers=[], gateway_token="tok")
    out = agent.run_role("Write hello.txt", tree)
    assert gateway.raised and gateway.raised[0]["tool"] == "Edit"
    assert gateway.raised[0]["kind"] == mcp_gateway.ACTION_REQUEST_APPROVAL
    assert out["stop_reason"] == "done"                     # parked, then answered — no hold
    assert out["permissions"][0]["decision_id"] == "dec-1"
    assert (tree / "hello.txt").exists() is written


def test_an_execute_call_is_decided_as_bash(acp_agent, monkeypatch):
    agent, tree, _ = acp_agent
    monkeypatch.setenv("FAKE_ACP_SCENARIO", "execute")
    agent.tool_policy = {"Bash": "ask"}
    out = agent.run_role("Run the tests", tree)
    assert out["pending_tool"] == "Bash"


def test_a_write_outside_the_tree_is_refused(acp_agent, monkeypatch):
    agent, tree, records = acp_agent
    monkeypatch.setenv("FAKE_ACP_SCENARIO", "escape")
    agent.run_role("Escape", tree)
    assert "error" in records("write")[0]["params"]
    assert not Path("/etc/boss-escape.txt").exists()


def test_an_unsigned_agent_is_a_clear_sign_in_error(acp_agent, monkeypatch):
    agent, tree, _ = acp_agent
    monkeypatch.setenv("FAKE_ACP_SCENARIO", "auth")
    with pytest.raises(CliHarnessUnavailable, match="not signed in"):
        agent.run_role("x", tree)


def test_an_agent_on_another_protocol_version_is_refused(acp_agent, monkeypatch):
    agent, tree, _ = acp_agent
    monkeypatch.setenv("FAKE_ACP_VERSION", "2")
    with pytest.raises(CliHarnessUnavailable, match="ACP version 2"):
        agent.run_role("x", tree)


def test_the_agents_mcp_servers_go_in_session_new_when_it_takes_http(acp_agent, tmp_path):
    agent, tree, records = acp_agent
    agent.mcp_harness = _mcp(tmp_path)
    out = agent.run_role("x", tree)
    sent = records("session/new")[0]["params"]["mcpServers"]
    assert [s["name"] for s in sent] == ["library_docs", "tickets"]
    assert sent[0] == {"type": "http", "name": "library_docs", "url": "https://docs.example/mcp",
                       "headers": [{"name": "Authorization", "value": "Bearer up-stream"}]}
    assert out["kit"]["servers"] == ["library_docs", "tickets"]


def test_an_agent_without_http_mcp_gets_none_and_the_run_says_so(acp_agent, tmp_path,
                                                                 monkeypatch):
    agent, tree, records = acp_agent
    monkeypatch.setenv("FAKE_ACP_NO_HTTP", "1")
    agent.mcp_harness = _mcp(tmp_path)
    out = agent.run_role("x", tree)
    assert records("session/new")[0]["params"]["mcpServers"] == []
    assert out["kit"]["withheld"] == {"library_docs": "no_http_capability",
                                      "tickets": "no_http_capability"}


def test_gemini_cli_and_opencode_are_acp_backed_and_unverified():
    from boss_engine.services.acp_code_agent import AcpCodeAgent
    from boss_engine.services.harness_registry import available_harnesses, get_harness

    listed = {h["name"]: h for h in available_harnesses()}
    for name in ("gemini_cli", "opencode"):
        assert BUILTIN_CLI_HARNESSES[name].protocol == "acp"
        assert listed[name]["kind"] == "acp"
        assert listed[name]["verified"] is False
        assert isinstance(get_harness(name).build(get_settings()), AcpCodeAgent)


# ══ 853 · process and http kinds ═════════════════════════════════════════════════════════════

FAKE_PROCESS = textwrap.dedent('''\
    import json, os, sys
    prompt = sys.stdin.read() if os.environ.get("VIA") != "argv" else sys.argv[-1]
    work = os.environ["BOSS_WORKSPACE"]
    assert os.getcwd() == os.path.realpath(work) or os.getcwd() == work
    seen = []
    if os.environ.get("BOSS_MCP_CONFIG"):
        seen = sorted(json.load(open(os.environ["BOSS_MCP_CONFIG"]))["mcpServers"])
    skills = sorted(os.listdir(os.environ["BOSS_SKILLS_DIR"])) if os.environ.get(
        "BOSS_SKILLS_DIR") else []
    if os.environ.get("FAIL"):
        print("cannot", file=sys.stderr); sys.exit(3)
    if os.environ["BOSS_MODE"] == "write":
        open(os.path.join(work, "out.txt"), "w").write(prompt)
    json.dump({"summary": "wrote out.txt", "status": "done",
               "usage": {"prompt_tokens": 5, "completion_tokens": 2},
               "tool_calls": [{"name": "write_file", "input": {"path": "out.txt"}, "ok": True,
                               "output": "ok"}],
               "session_id": "p-1", "mcp": seen, "skills": skills,
               "argv": sys.argv[1:]}, open(os.environ["BOSS_RESULT_FILE"], "w"))
''')


@pytest.fixture
def process_agent(tmp_path):
    from boss_engine.services.custom_harness import ProcessCodeAgent, ProcessHarnessConfig

    script = _exe(tmp_path / "agent.py", FAKE_PROCESS)

    def make(**kw):
        config = ProcessHarnessConfig(name="inhouse", argv=kw.pop(
            "argv", (str(script), "--mode", "{mode}", "--model", "{model}")), timeout=60, **kw)
        return ProcessCodeAgent(get_settings(), config)

    tree = tmp_path / "tree"
    tree.mkdir()
    return make, tree


def test_a_process_agent_runs_end_to_end(process_agent):
    make, tree = process_agent
    agent = make()
    out = agent.run_role("make out.txt", tree, model="m-1")
    assert (tree / "out.txt").read_text() == "make out.txt"
    assert out["summary"] == "wrote out.txt"
    assert out["stop_reason"] == "done"
    assert out["session_id"] == "p-1"
    assert out["tool_calls"] == 1
    assert out["usage"]["prompt_tokens"] == 5


def test_a_process_agent_substitutes_its_argv_template(process_agent, monkeypatch):
    make, tree = process_agent
    monkeypatch.setenv("VIA", "argv")
    agent = make(argv=(str(tree.parent / "agent.py"), "--mode", "{mode}", "{prompt}"),
                 prompt_via="argv", env_passthrough=("VIA",))
    agent.run_role("from argv", tree, read_only=False)
    assert (tree / "out.txt").read_text() == "from argv"


def test_a_failing_process_agent_fails_the_stage_with_its_words(process_agent, monkeypatch):
    from boss_engine.services.custom_harness import CustomHarnessError

    make, tree = process_agent
    monkeypatch.setenv("FAIL", "1")
    agent = make(env_passthrough=("FAIL",))
    with pytest.raises(CustomHarnessError, match="exited 3"):
        agent.run_role("x", tree)


def test_a_process_agent_gets_its_kit_by_env(process_agent, tmp_path, monkeypatch):
    from boss_engine.services import custom_harness

    make, tree = process_agent
    agent = make()
    agent.mcp_harness = _mcp(tmp_path, skills=True)
    captured = {}
    real = custom_harness.normalise_report

    def spy(report, **kw):
        captured.update(report or {})
        return real(report, **kw)

    monkeypatch.setattr(custom_harness, "normalise_report", spy)
    out = agent.run_role("x", tree)
    assert captured["mcp"] == ["library_docs", "tickets"]
    assert captured["skills"] == ["release_notes"]
    assert out["kit"]["mcp_delivery"] == "env_manifest"


@pytest.mark.parametrize("kw,match", [
    ({"argv": ()}, "argv is required"),
    ({"argv": ("x", "{secret}")}, "unknown placeholder"),
    ({"argv": ("x",), "prompt_via": "argv"}, "needs {prompt}"),
    ({"argv": ("x",), "prompt_via": "pipe"}, "prompt_via"),
])
def test_a_bad_process_contract_is_refused(kw, match):
    from boss_engine.services.custom_harness import ProcessHarnessConfig

    with pytest.raises(ValueError, match=match):
        ProcessHarnessConfig(name="bad", **kw)


class _FakeHttpAgent:
    """The boss.harness.http/v1 conformance fake: a service that edits the shared workspace."""

    def __init__(self, *, polls_running: int = 1, never_done: bool = False):
        self.runs: dict[str, dict] = {}
        self.requests: list[tuple[str, str, dict, dict]] = []
        self.polls_running = polls_running
        self.never_done = never_done
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _answer(self, code, body):
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.requests.append(("POST", self.path, body, dict(self.headers)))
                run_id = f"r{len(fake.runs) + 1}"
                fake.runs[run_id] = {"body": body, "polls": 0}
                if body["workspace"]["access"] == "read_write":
                    Path(body["workspace"]["path"], "svc.txt").write_text(body["prompt"])
                self._answer(202, {"run_id": run_id, "poll_after_ms": 10})

            def do_GET(self):  # noqa: N802
                fake.requests.append(("GET", self.path, {}, dict(self.headers)))
                run = fake.runs[self.path.rsplit("/", 1)[-1]]
                run["polls"] += 1
                if fake.never_done or run["polls"] <= fake.polls_running:
                    return self._answer(200, {"status": "running"})
                self._answer(200, {"status": "done", "summary": "service did it",
                                   "usage": {"completion_tokens": 3, "cost_usd_micros": 42},
                                   "tool_calls": [{"name": "edit", "input": {"path": "svc.txt"}}],
                                   "session_id": "h-1"})

            def do_DELETE(self):  # noqa: N802
                fake.requests.append(("DELETE", self.path, {}, dict(self.headers)))
                self._answer(200, {"cancelled": True})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()


@pytest.fixture
def http_fake():
    fakes: list[_FakeHttpAgent] = []

    def make(**kw):
        fake = _FakeHttpAgent(**kw)
        fakes.append(fake)
        return fake

    yield make
    for fake in fakes:
        fake.close()


def test_an_http_agent_runs_end_to_end(http_fake, tmp_path, monkeypatch):
    from boss_engine.services.custom_harness import HttpCodeAgent, HttpHarnessConfig

    fake = http_fake(polls_running=2)
    monkeypatch.setenv("SVC_TOKEN", "t0ken")
    agent = HttpCodeAgent(get_settings(), HttpHarnessConfig(
        name="svc", base_url=fake.url, token_env="SVC_TOKEN", poll_seconds=0.01, timeout=30))
    agent.mcp_harness = _mcp(tmp_path)
    tree = tmp_path / "tree"
    tree.mkdir()
    out = agent.run_role("from http", tree, model="m")
    assert (tree / "svc.txt").read_text() == "from http"
    assert out["summary"] == "service did it" and out["stop_reason"] == "done"
    assert out["cost_usd_micros"] == 42 and out["cost_unreported"] is False
    assert out["session_id"] == "h-1"
    method, path, body, headers = fake.requests[0]
    assert (method, path) == ("POST", "/v1/runs")
    assert body["contract"] == "boss.harness.http/v1"
    assert body["workspace"] == {"kind": "path", "path": str(tree.resolve()),
                                 "access": "read_write"}
    assert sorted(body["kit"]["mcpServers"]) == ["library_docs", "tickets"]
    assert headers["Authorization"] == "Bearer t0ken"
    assert [r[0] for r in fake.requests].count("GET") == 3


def test_an_http_agent_past_its_deadline_is_cancelled(http_fake, tmp_path, monkeypatch):
    from boss_engine.services import custom_harness
    from boss_engine.services.custom_harness import (
        CustomHarnessError, HttpCodeAgent, HttpHarnessConfig,
    )

    fake = http_fake(never_done=True)
    monkeypatch.setattr("boss_engine.services.code_agent._cap_to_deadline",
                        lambda t, read_only: 0)
    agent = HttpCodeAgent(get_settings(), HttpHarnessConfig(
        name="svc", base_url=fake.url, poll_seconds=0.01, timeout=30))
    tree = tmp_path / "tree"
    tree.mkdir()
    with pytest.raises(CustomHarnessError, match="did not finish"):
        agent.run_role("x", tree)
    assert fake.requests[-1][0] == "DELETE"
    del custom_harness


# ══ 853 · installable adapters ══════════════════════════════════════════════════════════════

MANIFEST = textwrap.dedent('''\
    apiVersion: boss.harness/v1
    kind: Harness
    metadata:
      id: acme.inhouse
      name: {name}
      version: 1.0.0
      description: Acme's in-house agent.
    spec:
      kind: process
      capabilities: [mcp, skills]
      requires: [ACME_TOKEN]
      process:
        argv: [{argv}]
        promptVia: stdin
      testEnvironment:
        argv: [{probe}]
        expect: "agent ok"
''')


@pytest.fixture
def plugin_dir(tmp_path, monkeypatch):
    from boss_engine.services import harness_registry

    root = tmp_path / "harness-plugins"
    plugin = root / "inhouse"
    plugin.mkdir(parents=True)
    script = _exe(tmp_path / "agent.py", FAKE_PROCESS)
    probe = _exe(tmp_path / "probe.py", "print('agent ok')\n")
    (plugin / "boss-harness.yaml").write_text(MANIFEST.format(
        name="acme_inhouse", argv=json.dumps(str(script)), probe=json.dumps(str(probe))))
    # Code shipped beside the manifest must never be imported or run by loading it.
    marker = tmp_path / "imported"
    (plugin / "__init__.py").write_text(f"open({str(marker)!r}, 'w').write('x')\n")
    monkeypatch.setenv("BOSS_HARNESS_PLUGIN_PATHS", str(root))
    monkeypatch.delenv("BOSS_HARNESS_PLUGINS_APPROVED", raising=False)
    monkeypatch.delenv("BOSS_HARNESS_LOCK", raising=False)
    harness_registry._ensure_builtins()
    yield plugin, marker
    harness_registry._REGISTRY.pop("acme_inhouse", None)


def _plugin():
    from boss_engine.services import harness_plugins

    (found,) = harness_plugins.discover()
    return found


def test_an_unapproved_community_harness_is_refused(plugin_dir):
    from boss_engine.services import harness_plugins
    from boss_engine.services.harness_registry import _REGISTRY

    plugin = _plugin()
    assert plugin.source == "community" and not plugin.usable
    assert "not approved" in plugin.problems[0] and plugin.checksum in plugin.problems[0]
    harness_plugins.load_harness_plugins()
    assert "acme_inhouse" not in _REGISTRY


def test_an_approved_harness_registers_unverified_on_boss_own_adapter(plugin_dir, monkeypatch,
                                                                     tmp_path):
    from boss_engine.services import harness_plugins
    from boss_engine.services.custom_harness import ProcessCodeAgent
    from boss_engine.services.harness_registry import available_harnesses, get_harness

    plugin_root, marker = plugin_dir
    monkeypatch.setenv("BOSS_HARNESS_PLUGINS_APPROVED", f"acme.inhouse@{_plugin().checksum}")
    loaded = harness_plugins.load_harness_plugins()
    assert loaded[0].usable, loaded[0].problems
    spec = get_harness("acme_inhouse")
    assert spec.verified is False and spec.kind == "process"
    assert spec.source == "plugin:acme.inhouse"
    listed = {h["name"]: h for h in available_harnesses()}["acme_inhouse"]
    assert listed["source"] == "plugin:acme.inhouse" and listed["requires"] == ["ACME_TOKEN"]
    agent = spec.build(get_settings())
    assert isinstance(agent, ProcessCodeAgent)
    tree = tmp_path / "tree"
    tree.mkdir()
    assert agent.run_role("plugin run", tree)["summary"] == "wrote out.txt"
    assert not marker.exists()                      # nothing from the plugin was imported
    # Loading twice is idempotent (the same plugin re-registers itself, nothing else).
    harness_plugins.load_harness_plugins()
    assert get_harness("acme_inhouse").source == "plugin:acme.inhouse"


def test_a_harness_whose_files_change_after_approval_is_refused(plugin_dir, monkeypatch):
    plugin_root, _ = plugin_dir
    monkeypatch.setenv("BOSS_HARNESS_PLUGINS_APPROVED", f"acme.inhouse@{_plugin().checksum}")
    assert _plugin().usable
    (plugin_root / "notes.txt").write_text("changed")
    assert "changed after it was approved" in _plugin().problems[0]


def test_a_pinned_harness_is_verified_source_and_must_match_its_pin(plugin_dir, monkeypatch,
                                                                   tmp_path):
    plugin_root, _ = plugin_dir
    lock = tmp_path / "harness.lock.yaml"
    lock.write_text(json.dumps({"harnesses": [{"id": "acme.inhouse", "version": "1.0.0",
                                               "checksum": _plugin().checksum}]}))
    monkeypatch.setenv("BOSS_HARNESS_LOCK", str(lock))
    assert _plugin().source == "verified" and _plugin().usable
    (plugin_root / "notes.txt").write_text("changed")
    assert "does not match its pinned checksum" in _plugin().problems[0]


def test_a_harness_may_not_take_a_builtin_name_or_the_official_namespace(plugin_dir,
                                                                         monkeypatch):
    from boss_engine.services import harness_plugins
    from boss_engine.services.harness_registry import get_harness

    plugin_root, _ = plugin_dir
    text = (plugin_root / "boss-harness.yaml").read_text()
    (plugin_root / "boss-harness.yaml").write_text(text.replace("name: acme_inhouse",
                                                                "name: codex"))
    monkeypatch.setenv("BOSS_HARNESS_PLUGINS_APPROVED", f"acme.inhouse@{_plugin().checksum}")
    (loaded,) = harness_plugins.load_harness_plugins()
    assert "already registered" in loaded.problems[-1]
    assert get_harness("codex").source == "builtin"
    (plugin_root / "boss-harness.yaml").write_text(text.replace("id: acme.inhouse",
                                                                "id: official.inhouse"))
    assert "reserved" in " ".join(_plugin().problems)


@pytest.mark.parametrize("patch,reason", [
    (("kind: process", "kind: shell"), "spec.kind"),
    (("  process:\n", "  http:\n"), "spec.http"),
    (("capabilities: [mcp, skills]", "capabilities: [telepathy]"), "unknown capabilities"),
])
def test_an_invalid_manifest_says_why(plugin_dir, patch, reason):
    plugin_root, _ = plugin_dir
    path = plugin_root / "boss-harness.yaml"
    path.write_text(path.read_text().replace(*patch))
    assert reason in " ".join(_plugin().problems)


def test_the_probe_runs_only_when_asked_and_reads_its_expectation(plugin_dir, tmp_path):
    from boss_engine.services import harness_plugins

    plugin_root, _ = plugin_dir
    assert harness_plugins.probe(_plugin()) ["ok"] is True
    path = plugin_root / "boss-harness.yaml"
    path.write_text(path.read_text().replace('expect: "agent ok"', 'expect: "never"'))
    assert harness_plugins.probe(_plugin())["ok"] is False


def test_an_http_probe_reads_the_status(tmp_path):
    from boss_engine.services import harness_plugins

    manifest = harness_plugins.validate_manifest({
        "apiVersion": "boss.harness/v1", "kind": "Harness",
        "metadata": {"id": "acme.svc", "name": "acme_svc", "version": "1.0.0",
                     "description": "svc"},
        "spec": {"kind": "http", "http": {"baseUrl": "http://svc.local"},
                 "testEnvironment": {"http": "/healthz"}}})
    plugin = harness_plugins.HarnessPlugin(directory=tmp_path, origin="path", checksum="x",
                                           manifest=manifest)

    class Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    urls = []
    result = harness_plugins.probe(plugin, opener=lambda url, timeout: urls.append(url) or Resp())
    assert result["ok"] is True and urls == ["http://svc.local/healthz"]


def test_an_entry_point_is_resolved_to_its_files_without_importing(tmp_path, monkeypatch):
    from boss_engine.services import harness_plugins

    pkg = tmp_path / "site" / "acme_pkg" / "harness"
    pkg.mkdir(parents=True)
    (pkg / "boss-harness.yaml").write_text("x: 1")

    class Dist:
        name = "acme-pkg"

        def locate_file(self, rel):
            return tmp_path / "site" / rel

    class EP:
        value = "acme_pkg.harness:manifest"
        dist = Dist()

    import importlib.metadata as md

    monkeypatch.setattr(md, "entry_points", lambda group: [EP()] if group ==
                        harness_plugins.ENTRY_POINT_GROUP else [])
    assert list(harness_plugins._entry_point_candidates()) == [("entry_point:acme-pkg", pkg)]


# ══ 854 · the kit on every harness ═════════════════════════════════════════════════════════

def test_every_cli_harness_declares_how_its_kit_reaches_it():
    for name, spec in BUILTIN_CLI_HARNESSES.items():
        assert spec.mcp_delivery in hd.MCP_DELIVERY, f"{name} declares no MCP delivery"
        assert spec.skills_dir == "" or spec.skills_dir.startswith(("{workspace}", "{home}",
                                                                    "{env}")), name


#: The delivery matrix: engine -> (mcp_delivery, skills land under ...).
MATRIX = {
    "claude_code": ("claude_flag", "{workspace}/.claude/skills"),
    "codex": ("codex_home", "{home}/skills"),
    "cursor": ("cursor_workspace", ""),
    "grok": ("none", ""),
    "aider": ("none", ""),
    "gemini_cli": ("acp", ""),
    "opencode": ("acp", "{workspace}/.opencode/skill"),
}


def test_the_delivery_matrix_is_what_the_docs_say():
    assert {n: (s.mcp_delivery, s.skills_dir) for n, s in BUILTIN_CLI_HARNESSES.items()} == MATRIX
    doc = (Path(__file__).resolve().parents[2] / "docs" / "engineering" /
           "HARNESS_ADAPTERS.md").read_text()
    for name, (mode, _) in MATRIX.items():
        assert f"| {name} | {mode} |" in doc, name


def _fake_engine_sees(mode: str, kit: hd.KitDelivery, tree: Path, tmp_path: Path, mcp) -> list:
    """Each engine's scripted fake reads its MCP config the way that engine does."""
    reader = _exe(tmp_path / f"read-{mode}", textwrap.dedent('''\
        import json, os, sys, tomllib
        mode = sys.argv[1]
        if mode == "claude_flag":
            path = sys.argv[sys.argv.index("--mcp-config") + 1]
            names = sorted(json.load(open(path))["mcpServers"])
        elif mode == "codex_home":
            names = sorted(tomllib.load(open(os.path.join(os.environ["CODEX_HOME"],
                                                          "config.toml"), "rb"))["mcp_servers"])
        elif mode == "cursor_workspace":
            names = sorted(json.load(open(os.path.join(".cursor", "mcp.json")))["mcpServers"])
        elif mode == "env_manifest":
            names = sorted(json.load(open(os.environ["BOSS_MCP_CONFIG"]))["mcpServers"])
        else:
            names = []
        print(json.dumps(names))
    '''))
    import subprocess

    argv = [str(reader), mode]
    if mode == "claude_flag":
        argv += mcp.argv()
    env = {**os.environ, **kit.env_for(dict(os.environ), default_home=str(tmp_path / "home"))}
    done = subprocess.run(argv, cwd=tree, env=env, capture_output=True, text=True, check=True)
    return json.loads(done.stdout)


@pytest.mark.parametrize("name", sorted(MATRIX) + ["process", "http"])
def test_conformance_every_harness_sees_the_agents_servers(name, tmp_path, monkeypatch):
    """VERIFIED harnesses: the engine's fake sees exactly the agent's servers. Unverified ones:
    the delivery files are written (and, where cheap, read back the same way)."""
    from boss_engine.services.custom_harness import HttpHarnessConfig, ProcessHarnessConfig

    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    (tmp_path / "home" / "auth.json").write_text("{}")
    if name in BUILTIN_CLI_HARNESSES:
        spec = BUILTIN_CLI_HARNESSES[name]
        mode, skills_dir, verified = spec.mcp_delivery, spec.skills_dir, spec.verified
    else:
        config = (ProcessHarnessConfig(name="p", argv=("x",)) if name == "process"
                  else HttpHarnessConfig(name="h", base_url="http://x"))
        mode, skills_dir, verified = config.mcp_delivery, config.skills_dir, False
    tree = tmp_path / "tree"
    tree.mkdir()
    mcp = _mcp(tmp_path, gateway_token="")
    monkeypatch.setattr(mcp, "bind_session", lambda: None)
    kit = hd.deliver(engine=name, mode=mode, skills_dir=skills_dir, mcp=mcp, workspace=tree)
    try:
        if mode == "none":
            assert kit.servers == [] and set(kit.withheld) == {"library_docs", "tickets"}
            return
        if mode == "acp":
            assert [s["name"] for s in kit.acp_servers_for(http=True)] == ["library_docs",
                                                                           "tickets"]
            return
        seen = _fake_engine_sees(mode, kit, tree, tmp_path, mcp)
        if verified:
            assert seen == ["library_docs", "tickets"], name
        else:
            assert seen == kit.servers == ["library_docs", "tickets"], name
            if mode == "cursor_workspace":
                assert (tree / ".cursor" / "mcp.json").is_file()
            if mode == "env_manifest":
                assert os.path.isfile(kit.env["BOSS_MCP_CONFIG"])
    finally:
        kit.cleanup()
    assert not (tree / ".cursor").exists()


def test_codex_gets_a_per_run_home_with_its_login_and_only_the_agents_servers(tmp_path):
    home = tmp_path / "codex"
    (home / "sessions").mkdir(parents=True)
    (home / "auth.json").write_text('{"tokens": 1}')
    (home / "config.toml").write_text('model = "gpt-6"\n[mcp_servers.personal]\n'
                                      'url = "https://mine"\n[profiles.fast]\nmodel = "x"\n')
    mcp = _mcp(tmp_path, disallowed=["mcp__tickets__delete"], skills=True)
    kit = hd.deliver(engine="codex", mode="codex_home", skills_dir="{home}/skills", mcp=mcp,
                     workspace=tmp_path)
    run_home = Path(kit.env_for({"CODEX_HOME": str(home)})["CODEX_HOME"])
    config = tomllib.loads((run_home / "config.toml").read_text())
    assert config["model"] == "gpt-6" and config["profiles"]["fast"]["model"] == "x"
    assert sorted(config["mcp_servers"]) == ["library_docs", "tickets"]   # not "personal"
    assert config["mcp_servers"]["library_docs"]["http_headers"] == {
        "Authorization": "Bearer up-stream"}
    assert config["mcp_servers"]["tickets"]["disabled_tools"] == ["delete"]
    assert (run_home / "auth.json").is_symlink() and (run_home / "sessions").is_symlink()
    assert (run_home / "skills" / "release_notes" / "SKILL.md").is_file()
    # A login Codex rotated during the run (written over the link) goes back to the real home.
    (run_home / "auth.json").unlink()
    (run_home / "auth.json").write_text('{"tokens": 2}')
    kit.cleanup()
    assert not run_home.exists()
    assert (home / "auth.json").read_text() == '{"tokens": 2}'


def test_the_codex_driver_runs_on_the_per_run_home(tmp_path, monkeypatch):
    from boss_engine.services.cli_harness import load_specs
    from boss_engine.services.codex_code_agent import CodexCodeAgent

    fake = _exe(tmp_path / "codex", textwrap.dedent(f'''\
        import json, os, sys
        sys.stdin.read()
        home = os.environ["CODEX_HOME"]
        open({str(tmp_path / "seen")!r}, "w").write(open(os.path.join(home, "config.toml")).read())
        final = sys.argv[sys.argv.index("--output-last-message") + 1]
        print(json.dumps({{"type": "thread.started", "thread_id": "t"}}), flush=True)
        print(json.dumps({{"type": "turn.completed", "usage": {{}}}}), flush=True)
        open(final, "w").write("done")
    '''))
    del fake
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    agent = CodexCodeAgent(get_settings(), load_specs(get_settings())["codex"])
    agent._auth_checked = True
    monkeypatch.setattr(agent, "remember_rotated_credential", lambda: False)
    agent.mcp_harness = _mcp(tmp_path)
    monkeypatch.setattr(agent.mcp_harness, "bind_session", lambda: None)
    tree = tmp_path / "tree"
    tree.mkdir()
    agent.run_role("x", tree)
    assert sorted(tomllib.loads((tmp_path / "seen").read_text())["mcp_servers"]) == [
        "library_docs", "tickets"]


def test_cursor_gets_its_workspace_file_and_the_repos_own_comes_back(tmp_path, monkeypatch):
    from boss_engine.services.cli_harness import GenericCliCodeAgent

    _exe(tmp_path / "cursor-agent", textwrap.dedent(f'''\
        import json, os
        open({str(tmp_path / "seen")!r}, "w").write(open(".cursor/mcp.json").read())
        print("edited")
    '''))
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}")
    agent = GenericCliCodeAgent(get_settings(), BUILTIN_CLI_HARNESSES["cursor"])
    agent.mcp_harness = _mcp(tmp_path)
    monkeypatch.setattr(agent.mcp_harness, "bind_session", lambda: None)
    tree = tmp_path / "tree"
    (tree / ".cursor").mkdir(parents=True)
    (tree / ".cursor" / "mcp.json").write_text('{"mcpServers": {"repo": {}}}')
    out = agent.run_role("x", tree)
    assert out["summary"] == "edited"
    assert sorted(json.loads((tmp_path / "seen").read_text())["mcpServers"]) == [
        "library_docs", "tickets"]
    assert json.loads((tree / ".cursor" / "mcp.json").read_text()) == {"mcpServers": {"repo": {}}}


def test_an_engine_that_cannot_refuse_a_tool_is_not_handed_that_server_without_the_gateway(
        tmp_path):
    mcp = _mcp(tmp_path, disallowed=["mcp__tickets__delete"])
    kit = hd.deliver(engine="cursor", mode="cursor_workspace", mcp=mcp, workspace=tmp_path,
                     gateway=False)
    try:
        assert kit.servers == ["library_docs"]
        assert kit.withheld == {"tickets": "cannot_disallow"}
    finally:
        kit.cleanup()
    kit = hd.deliver(engine="cursor", mode="cursor_workspace", mcp=mcp, workspace=tmp_path,
                     gateway=True)
    try:
        assert kit.servers == ["library_docs", "tickets"]   # the gateway refuses Off calls
    finally:
        kit.cleanup()


def test_skills_reach_a_workspace_reader_and_leave_the_repos_own(tmp_path):
    tree = tmp_path / "tree"
    (tree / ".claude" / "skills" / "repo_skill").mkdir(parents=True)
    mcp = _mcp(tmp_path, skills=True)
    kit = hd.deliver(engine="claude_code", mode="claude_flag",
                     skills_dir="{workspace}/.claude/skills", mcp=mcp, workspace=tree)
    synced = tree / ".claude" / "skills" / "boss-release_notes" / "SKILL.md"
    assert synced.read_text().startswith("---\nname: release_notes\n")
    kit.cleanup()
    assert not synced.exists()
    assert (tree / ".claude" / "skills" / "repo_skill").is_dir()
    # A tree with no .claude at all is left with none.
    bare = tmp_path / "bare"
    bare.mkdir()
    kit = hd.deliver(engine="opencode", mode="acp", skills_dir="{workspace}/.opencode/skill",
                     mcp=mcp, workspace=bare)
    assert (bare / ".opencode" / "skill" / "boss-release_notes" / "SKILL.md").is_file()
    kit.cleanup()
    assert not (bare / ".opencode").exists()


def test_an_engine_with_no_skills_route_records_it(tmp_path):
    kit = hd.deliver(engine="cursor", mode="cursor_workspace", skills_dir="",
                     mcp=_mcp(tmp_path, skills=True), workspace=tmp_path)
    try:
        assert kit.skills_skipped == "no_skills_route"
    finally:
        kit.cleanup()


def test_the_claude_driver_syncs_skills_for_the_session_only(tmp_path, monkeypatch):
    """ClaudeCodeAgent declares its profile; the decorator on `_invoke` delivers the skills."""
    from boss_engine.services.code_agent import ClaudeCodeAgent

    assert ClaudeCodeAgent.KIT_PROFILE == ("claude_flag", "{workspace}/.claude/skills")
    agent = ClaudeCodeAgent(get_settings())
    agent.mcp_harness = _mcp(tmp_path, skills=True)
    tree = tmp_path / "tree"
    tree.mkdir()
    seen = {}

    @hd.delivers_kit
    def session(self, prompt, cwd):
        seen["skills"] = sorted(os.listdir(Path(cwd) / ".claude" / "skills"))
        return {}

    session(agent, "x", tree)
    assert seen["skills"] == ["boss-release_notes"]
    assert not (tree / ".claude").exists()


def test_no_kit_changes_nothing(tmp_path):
    kit = hd.deliver(engine="codex", mode="codex_home", skills_dir="{home}/skills",
                     mcp=HarnessMcp(path="", servers=[]), workspace=tmp_path)
    assert kit.env_for({"CODEX_HOME": str(tmp_path)}) == {}
    assert kit.written == []


def test_a_skill_becomes_a_skill_md():
    text = skill_markdown({"skill_key": "k", "label": "K", "summary": "does\nthings",
                           "knowledge": "Body."})
    assert text == '---\nname: k\ndescription: "does things"\n---\n\n# K\n\nBody.\n'
    assert write_skills_bundle([{"skill_key": "empty", "knowledge": ""}]) == ""


def test_the_toml_writer_round_trips_a_codex_config():
    data = {"model": "gpt-6", "approval": False, "n": 3, "list": ["a", "b"],
            "mcp_servers": {"a b": {"url": "u", "http_headers": {"X-Y": "z"}}},
            "profiles": [{"name": "p"}]}
    assert tomllib.loads(hd.toml_dumps(data)) == data
