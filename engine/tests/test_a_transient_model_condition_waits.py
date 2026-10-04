"""A condition that clears by itself waits; only a verdict fails.

Seen on boss-1 (2026-10-04), three example workflows started together, all on the Claude Code
subscription:

1. One discovery stage out of many concurrent ones ran `claude` and got "Not logged in" — with
   successful calls on the same login seconds either side of it — and the run was failed as a
   REJECTED LOGIN. The CLI says "Not logged in" when it is given no credential at all (a token
   it rejects is a 401). It was given none because the lookup of the stored token treated every
   secret-store error as "nothing stored": one store hiccup under load, swallowed, became a CLI
   started without a login. And one such report was believed at once.

2. Another discovery failed with "Selected model source has no concurrent lease capacity" —
   the subscription's lease was held by the other two runs. A queue, read as a verdict.

These pin the classes, not the instances: absence vs a store that did not answer, one
classification of wait-vs-fail for every surface, and a login refusal confirmed before it is
believed.
"""
from __future__ import annotations

import subprocess

import pytest

from boss_engine.config import Settings
from boss_engine.services import cli_harness, model_failures, secret_backends
from boss_engine.services.secret_backends import (
    SecretNotFound,
    SecretStoreUnreachable,
    SecretUnavailable,
)

TOKEN = "sk-ant-oat01-" + "x" * 20


class _Store:
    """A secret store that answers from a script: a value, absent, or an error per call."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def get(self, key):
        self.calls += 1
        if key != "CLAUDE_CODE_OAUTH_TOKEN":    # the namespaced key: never stored here
            raise SecretNotFound(f"{key} absent")
        step = self.script.pop(0) if self.script else SecretNotFound(f"{key} absent")
        if isinstance(step, BaseException):
            raise step
        return step


@pytest.fixture
def store(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(cli_harness, "CREDENTIAL_LOOKUP_BACKOFF_SECONDS", (0, 0))
    held = {"store": _Store([]), "passed_over": []}
    monkeypatch.setattr(secret_backends, "pick_default_backend",
                        lambda settings: ("database", list(held["passed_over"])))
    monkeypatch.setattr(secret_backends, "backend_for", lambda kind, settings: held["store"])

    def use(script, passed_over=()):
        held["store"] = _Store(script)
        held["passed_over"] = list(passed_over)
        return held["store"]

    return use


# -- 1. absent is an answer; a store that did not answer is not ------------------------------

def test_a_store_that_blips_once_still_yields_the_stored_login(store):
    """The boss-1 call: the store failed to answer once. Before, that was "nothing stored"."""
    fake = store([SecretStoreUnreachable("infisical returned 429 for GET /api/v3/secrets/raw"),
                  TOKEN])

    assert cli_harness.stored_credential(Settings(), "CLAUDE_CODE_OAUTH_TOKEN") == TOKEN
    assert fake.calls >= 2


def test_a_preferred_store_passed_over_for_not_answering_is_not_read_as_absent(store):
    """`default_secret_backend` falls through to Postgres when Infisical does not answer the
    health probe — and Postgres does not hold what was written to Infisical."""
    store([], passed_over=["infisical: infisical unreachable: TimeoutError"])

    with pytest.raises(cli_harness.CredentialStoreUnavailable) as raised:
        cli_harness.stored_credential(Settings(), "CLAUDE_CODE_OAUTH_TOKEN", strict=True)
    assert raised.value.transient
    assert TOKEN not in str(raised.value)


def test_a_cleanly_absent_credential_is_answered_at_once_without_retrying(store):
    fake = store([])

    assert cli_harness.stored_credential(Settings(), "CLAUDE_CODE_OAUTH_TOKEN",
                                         strict=True) is None
    assert fake.calls == 2, "one pass over both keys, no repeat for an answer"


def test_the_claude_cli_is_never_started_without_a_login_the_store_did_not_answer_for(store):
    """The chain on boss-1: store blip -> None -> `claude` with no login -> "Not logged in"."""
    from boss_engine.services.claude_cli_adapter import ClaudeCliAdapter
    from boss_engine.services.code_agent import ClaudeCodeAgent

    for build_env in (lambda: ClaudeCodeAgent(Settings(CLAUDE_CODE_OAUTH_TOKEN=""))._child_env(),
                      lambda: ClaudeCliAdapter(Settings(CLAUDE_CODE_OAUTH_TOKEN=""))._child_env()):
        store([SecretStoreUnreachable("infisical unreachable: TimeoutError")] * 6)
        with pytest.raises(cli_harness.CredentialStoreUnavailable):
            build_env()


def test_the_cli_still_gets_the_login_after_a_blip(store):
    from boss_engine.services.code_agent import ClaudeCodeAgent

    store([SecretStoreUnreachable("infisical returned 503 for GET /x"), TOKEN])
    env = ClaudeCodeAgent(Settings(CLAUDE_CODE_OAUTH_TOKEN=""))._child_env()
    assert env.get("CLAUDE_CODE_OAUTH_TOKEN") == TOKEN


@pytest.mark.parametrize("code,kind,transient", [
    (404, SecretNotFound, False), (429, SecretStoreUnreachable, True),
    (503, SecretStoreUnreachable, True), (403, SecretUnavailable, False),
])
def test_a_stores_http_status_is_read_once_for_every_store(code, kind, transient):
    failure = secret_backends._http_failure("infisical", "GET", "/api/v3/secrets/raw/X", code)
    assert type(failure) is kind
    assert failure.transient is transient


# -- 2. one classification: waits wait, verdicts fail ----------------------------------------

def test_a_busy_model_source_is_a_wait_not_a_failure():
    said = model_failures.waiting_for("Selected model source has no concurrent lease capacity.",
                                      source_id="anthropic_claude_code")
    assert said is not None and said["kind"] == "capacity_wait" and said["retry"] is True
    assert "Claude Code subscription" in said["headline"]


def test_a_secret_store_that_did_not_answer_is_a_wait_and_not_a_rejected_login():
    error = cli_harness.CredentialStoreUnavailable(
        "CLAUDE_CODE_OAUTH_TOKEN", "infisical unreachable: TimeoutError", transient=True)
    said = model_failures.waiting_for(error)
    assert said is not None and said["kind"] == "credential_store_wait"
    assert not model_failures.is_login_rejected(error)


@pytest.mark.parametrize("verdict", [
    "claude code agent exited 1: Failed to authenticate. API Error: 401 Invalid bearer token",
    "Budget cap exceeded. Decision created.",
    "Paid gateway capacity requires a canonical SecretRef credential binding.",
])
def test_a_verdict_is_still_not_a_wait(verdict):
    assert model_failures.waiting_for(verdict) is None


def test_a_workflow_step_refused_a_busy_source_waits_with_its_kind():
    from boss_engine.services.workflow_capability_runner import (
        CapabilityExecutionError,
        CapabilityWaitError,
        _as_wait,
    )

    wait = _as_wait(CapabilityExecutionError(
        "Selected model source has no concurrent lease capacity."))
    assert isinstance(wait, CapabilityWaitError) and wait.kind == "capacity_wait"


# -- 3. one "Not logged in" is a report, not a verdict ---------------------------------------

def test_a_single_login_refusal_is_asked_again_before_it_is_believed():
    calls = []

    def call():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("claude code agent exited 1: success — Not logged in · "
                               "Please run /login")
        return "answered"

    assert model_failures.confirm_login_rejection(call, sleep=lambda s: None) == "answered"
    assert len(calls) == 2


def test_a_login_refused_twice_is_a_rejected_login():
    def call():
        raise RuntimeError("Not logged in · Please run /login")

    with pytest.raises(RuntimeError, match="Not logged in"):
        model_failures.confirm_login_rejection(call, sleep=lambda s: None)


def test_any_other_failure_is_not_asked_again():
    calls = []

    def call():
        calls.append(1)
        raise RuntimeError("claude CLI exited 2: bad flag")

    with pytest.raises(RuntimeError, match="bad flag"):
        model_failures.confirm_login_rejection(call, sleep=lambda s: None)
    assert len(calls) == 1


def test_the_chat_cli_confirms_a_login_refusal(monkeypatch):
    from boss_engine.services.claude_cli_adapter import ClaudeCliAdapter

    monkeypatch.setattr(model_failures, "LOGIN_RECHECK_SECONDS", 0)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", TOKEN)
    replies = [
        subprocess.CompletedProcess([], 1, '{"result": "Not logged in · Please run /login"}', ""),
        subprocess.CompletedProcess([], 0, '{"result": "hi", "usage": {}}', ""),
    ]
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: replies.pop(0))
    assert ClaudeCliAdapter(Settings())._invoke("hello")["result"] == "hi"
    assert replies == []


class _Child:
    """A `claude` process for ClaudeCodeAgent._invoke: exits with `code`, printing `out`."""

    def __init__(self, code, out):
        self.returncode, self._out = code, out
        self.pid = 4242
        self.stdin = None

    def communicate(self, *a, **k):
        return self._out, ""


def test_the_coding_and_research_agent_confirms_a_login_refusal(monkeypatch, tmp_path):
    """The exact boss-1 message, from the agent discovery's research runs on."""
    from boss_engine.services import code_agent

    monkeypatch.setattr(model_failures, "LOGIN_RECHECK_SECONDS", 0)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", TOKEN)
    children = [
        _Child(1, '{"subtype": "success", "is_error": true, '
                  '"result": "Not logged in · Please run /login"}'),
        _Child(0, '{"subtype": "success", "result": "found it", "usage": {}}'),
    ]
    monkeypatch.setattr(code_agent.subprocess, "Popen", lambda *a, **k: children.pop(0))
    monkeypatch.setattr(code_agent, "_drain", lambda child, **k: child.communicate())
    monkeypatch.setattr(code_agent, "capture_enabled", lambda: False)
    agent = code_agent.ClaudeCodeAgent(Settings())

    data = agent._invoke("research this", tmp_path)

    assert data["result"] == "found it"
    assert children == []
