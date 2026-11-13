"""BOSSAPP-816: an approval is bound to the exact arguments, signed, and fails closed.

A hold made now is stamped binding_version 2 and signed with the identity server key over the
tool, the digest and the preview the person saw. A v2 decision without a valid signature never
releases a call; an unstamped (v1) decision keeps the lenient rule so in-flight approvals stand.
The native coder's stopped session carries the same binding, and a resumed session runs the
approved call once and refuses a changed one in McpArgumentsChanged's words. Credential-shaped
VALUES are redacted in the preview, not only secret-named keys.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from boss_engine.identity import authn_support
from boss_engine.services import argument_binding as ab
from boss_engine.services.code_agent import (
    NativeCodeAgent,
    approved_call_for_decision,
    approved_call_from,
)

_ARGS = {"path": "a.py", "content": "x = 1\n"}


# ── the signature ─────────────────────────────────────────────────────────────────────────────

def test_a_new_hold_is_stamped_v2_and_signed_and_the_signature_round_trips():
    bound = ab.bind_held_call("write_file", dict(_ARGS))
    assert bound["binding_version"] == 2
    assert bound["args_sig"].startswith("hs1:")
    assert bound["args_sha256"] == ab.args_sha256("write_file", _ARGS)
    assert ab.signature_valid("write_file", bound["args_sha256"], bound["args_preview"],
                              bound["args_sig"])
    assert ab.binding_holds("write_file", dict(_ARGS), bound) is True


@pytest.mark.parametrize("tamper", [
    lambda b: b.update(args_preview={"path": "harmless.txt", "content": "x = 1\n"}),
    lambda b: b.update(args_sig="hs1:" + "0" * 64),
    lambda b: b.update(args_sig="hs0:" + b["args_sig"].split(":", 1)[1]),
    lambda b: b.update(args_sig=b["args_sig"][:-1] + ("0" if b["args_sig"][-1] != "0" else "1")),
])
def test_a_tampered_binding_is_refused(tamper):
    bound = ab.bind_held_call("write_file", dict(_ARGS))
    tamper(bound)
    assert ab.binding_holds("write_file", dict(_ARGS), bound) is False


def test_a_digest_rewritten_to_other_arguments_is_refused():
    other = {"path": "b.py", "content": "boom"}
    bound = ab.bind_held_call("write_file", dict(_ARGS))
    bound["args_sha256"] = ab.args_sha256("write_file", other)
    assert ab.binding_holds("write_file", other, bound) is False


def test_another_servers_key_does_not_verify(monkeypatch):
    bound = ab.bind_held_call("write_file", dict(_ARGS))
    monkeypatch.setenv("BOSS_IDENTITY_SERVER_KEY",
                       "b3RoZXItc2VydmVyLWtleS1mb3ItdGhlLTgxNi10ZXN0LS0tLS0tLS0tLS0=")
    from boss_engine.config import get_settings

    get_settings.cache_clear() if hasattr(get_settings, "cache_clear") else None
    authn_support.reset_server_key_for_tests()
    try:
        assert ab.binding_holds("write_file", dict(_ARGS), bound) is False
    finally:
        monkeypatch.undo()
        get_settings.cache_clear() if hasattr(get_settings, "cache_clear") else None
        authn_support.reset_server_key_for_tests()
    assert ab.binding_holds("write_file", dict(_ARGS), bound) is True


# ── v2 fails closed, v1 stays lenient ─────────────────────────────────────────────────────────

def test_a_v2_decision_without_a_signature_or_digest_never_matches():
    bound = ab.bind_held_call("write_file", dict(_ARGS))
    unsigned = {k: v for k, v in bound.items() if k != "args_sig"}
    assert ab.binding_holds("write_file", dict(_ARGS), unsigned) is False
    no_digest = {k: v for k, v in bound.items() if k != "args_sha256"}
    assert ab.binding_holds("write_file", dict(_ARGS), no_digest) is False
    assert ab.binding_holds("write_file", "not-a-dict", bound) is False
    assert ab.binding_holds("write_file", dict(_ARGS), {"binding_version": 2}) is False


def test_stripping_the_stamp_does_not_downgrade_a_signed_decision():
    bound = ab.bind_held_call("write_file", dict(_ARGS))
    stripped = {k: v for k, v in bound.items() if k != "binding_version"}
    assert ab.binding_version(stripped) == 2
    stripped["args_preview"] = {"path": "elsewhere"}
    assert ab.binding_holds("write_file", dict(_ARGS), stripped) is False


def test_without_a_server_key_a_hold_opens_unsigned_and_can_never_be_released(monkeypatch):
    def no_key(*_a, **_k):
        raise authn_support.ServerKeyMissing("no key")

    monkeypatch.setattr(authn_support, "keyed_hash", no_key)
    bound = ab.bind_held_call("write_file", dict(_ARGS))
    assert bound["binding_version"] == 2 and "args_sig" not in bound
    assert ab.binding_holds("write_file", dict(_ARGS), bound) is False


def test_an_unstamped_v1_decision_keeps_the_lenient_rule():
    other = {"path": "b.py"}
    assert ab.binding_holds("write_file", other, {}) is True
    assert ab.binding_holds("write_file", other, {"run_id": "r1"}) is True
    assert ab.binding_holds("write_file", other, None) is True
    digest = ab.args_sha256("write_file", _ARGS)
    assert ab.binding_holds("write_file", dict(_ARGS), {"args_sha256": digest}) is True
    assert ab.binding_holds("write_file", other, {"args_sha256": digest}) is False


# ── credential-shaped values ──────────────────────────────────────────────────────────────────

def test_credential_shaped_values_are_redacted_whatever_the_key_is_called():
    preview = ab.redacted_preview({
        "header": "Bearer abcdefghijklmnopqrstuvwxyz123456",
        "gh": "ghp_" + "A" * 36,
        "aws": "AKIA" + "B" * 16,
        "dsn": "postgresql://boss:hunter2hunter2@db:5432/boss",
        "cmd": "curl -H 'x' --data api_key=s3cr3tvalue123 https://example.com",
        "nested": [{"value": "sk-ant-" + "c" * 30}],
        "plain": "hello world, nothing secret here",
        "short": "abc",
        "count": 3,
    })
    assert preview["header"] == ab.REDACTED
    assert preview["gh"] == ab.REDACTED
    assert preview["aws"] == ab.REDACTED
    assert preview["nested"][0]["value"] == ab.REDACTED
    assert "hunter2hunter2" not in preview["dsn"] and preview["dsn"].startswith("postgresql://")
    assert "s3cr3tvalue123" not in preview["cmd"] and "curl" in preview["cmd"]
    assert preview["plain"] == "hello world, nothing secret here"
    assert preview["short"] == "abc"
    assert preview["count"] == 3


def test_the_digest_still_covers_the_raw_value_not_the_redacted_one():
    a = {"token_field": "ghp_" + "A" * 36}
    b = {"token_field": "ghp_" + "Z" * 36}
    assert ab.redacted_preview(a) == ab.redacted_preview(b)
    assert ab.args_sha256("t", a) != ab.args_sha256("t", b)


# ── the native coder's resume consumes the hash ──────────────────────────────────────────────

class _ScriptedLLM:
    def __init__(self, turns: list[dict]) -> None:
        self._turns = list(turns)

    def create_chat_completion(self, messages, tools=None, tool_choice="auto",
                               model_override=None, redact=False):
        if not self._turns:
            return {"content": "done", "tool_calls": None, "raw": None}, {"model": "test"}
        return self._turns.pop(0), {"model": "test"}


def _tool_turn(name: str, args: dict) -> dict:
    return {"content": None, "raw": None,
            "tool_calls": [{"id": "c1", "name": name, "arguments": json.dumps(args)}]}


def _agent(llm, approved=None) -> NativeCodeAgent:
    from boss_engine.config import Settings

    agent = NativeCodeAgent.__new__(NativeCodeAgent)
    agent.settings = Settings(BOSS_CODE_AGENT_CONTEXT_FILES=0)
    agent._llm = llm
    agent.tool_policy = {"write_file": "ask"}
    if approved is not None:
        agent.approved_call = approved
    return agent


@pytest.fixture()
def quiet(monkeypatch):
    monkeypatch.setattr("boss_engine.services.code_agent.record_agent_turns", lambda *a, **k: None)
    monkeypatch.setattr("boss_engine.services.code_agent.record_llm_call", lambda *a, **k: None)


def _held(tmp_path: Path) -> dict:
    held = {"path": "ran.py", "content": "ran = True\n"}
    out = _agent(_ScriptedLLM([_tool_turn("write_file", held)])).run_role("create ran.py", tmp_path)
    assert out["stop_reason"] == "awaiting_approval"
    return out


def test_the_stopped_session_carries_a_signed_v2_binding(tmp_path, quiet):
    out = _held(tmp_path)
    binding = out["pending_binding"]
    assert binding["binding_version"] == 2 and binding["args_sig"].startswith("hs1:")
    assert out["pending_args_sha256"] == binding["args_sha256"]
    call = approved_call_from({**out, "decision_id": "dec_1"})
    assert call["tool"] == "write_file" and call["decision_id"] == "dec_1"
    assert ab.binding_holds("write_file", {"path": "ran.py", "content": "ran = True\n"}, call)


def test_a_resumed_session_runs_the_approved_call_once(tmp_path, quiet):
    out = _held(tmp_path)
    held = {"path": "ran.py", "content": "ran = True\n"}
    llm = _ScriptedLLM([_tool_turn("write_file", held),
                        {"content": "wrote it", "tool_calls": None, "raw": None}])
    resumed = _agent(llm, {**out, "decision_id": "dec_1"}).run_role("create ran.py", tmp_path)
    assert (tmp_path / "ran.py").read_text() == "ran = True\n"
    assert resumed["stop_reason"] != "awaiting_approval"
    assert "refused" not in resumed


def test_a_resumed_session_refuses_a_changed_argument(tmp_path, quiet):
    out = _held(tmp_path)
    changed = {"path": "ran.py", "content": "import os; os.system('rm -rf /')\n"}
    llm = _ScriptedLLM([_tool_turn("write_file", changed)])
    resumed = _agent(llm, {**out, "decision_id": "dec_1"}).run_role("create ran.py", tmp_path)
    assert not (tmp_path / "ran.py").exists()
    assert resumed["stop_reason"] == "awaiting_approval"
    assert resumed["refused"] == {"error": "McpArgumentsChanged", "reason": ab.CHANGED,
                                  "tool": "write_file", "decision_id": "dec_1"}
    assert resumed["summary"].startswith(f"Did not call write_file: {ab.CHANGED}.")
    # The changed call now waits for its own approval, bound to ITS arguments.
    assert resumed["pending_args_sha256"] == ab.args_sha256("write_file", changed)


def test_a_resumed_session_refuses_a_forged_v2_approval(tmp_path, quiet):
    out = _held(tmp_path)
    forged = {**out, "decision_id": "dec_1",
              "pending_binding": {**out["pending_binding"], "args_sig": "hs1:" + "f" * 64}}
    held = {"path": "ran.py", "content": "ran = True\n"}
    llm = _ScriptedLLM([_tool_turn("write_file", held)])
    resumed = _agent(llm, forged).run_role("create ran.py", tmp_path)
    assert not (tmp_path / "ran.py").exists()
    assert resumed["refused"]["error"] == "McpArgumentsChanged"


# ── the kernel's resume ───────────────────────────────────────────────────────────────────────

def test_the_kernel_resume_applies_the_same_v2_rule():
    from types import SimpleNamespace

    from boss_engine.kernel.loop import _approval_refusal

    state = SimpleNamespace(pending={"tool": "write_file", "args": dict(_ARGS),
                                     "decision_id": "dec_1"})
    bound = ab.bind_held_call("write_file", dict(_ARGS))
    ok = {"approved": True, "decision_id": "dec_1", **bound}
    assert _approval_refusal(state, ok) == ""
    unsigned = {k: v for k, v in ok.items() if k != "args_sig"}
    assert _approval_refusal(state, unsigned) == ab.CHANGED
    state.pending["args"] = {"path": "b.py"}
    assert _approval_refusal(state, ok) == ab.CHANGED
    # v1: an approval with no binding still releases the held call.
    assert _approval_refusal(state, {"approved": True, "decision_id": "dec_1"}) == ""


# ── the receipt door and the decision loader (Postgres) ──────────────────────────────────────

URL = os.environ.get("BOSS_TEST_819_DATABASE_URL", "")
_needs_db = pytest.mark.skipif(not URL or "test" not in URL.lower(),
                               reason="requires BOSS_TEST_819_DATABASE_URL naming a test database")


@pytest.fixture()
def conn():
    engine = create_engine(URL, future=True)
    with engine.begin() as c:
        c.execute(text("DROP SCHEMA IF EXISTS t816 CASCADE"))
        c.execute(text("CREATE SCHEMA t816"))
    with engine.connect() as c:
        c.execute(text("SET search_path TO t816"))
        c.execute(text("CREATE TABLE decisions (id TEXT PRIMARY KEY, project_id TEXT, "
                       "status TEXT, recommendation JSONB)"))
        c.commit()
        yield c
    with engine.begin() as c:
        c.execute(text("DROP SCHEMA IF EXISTS t816 CASCADE"))
    engine.dispose()


def _decision(c, did: str, status: str, rec: dict) -> None:
    c.execute(text("INSERT INTO decisions VALUES (:i, 'p1', :s, CAST(:r AS jsonb))"),
              {"i": did, "s": status, "r": json.dumps(rec)})


@_needs_db
def test_the_receipt_door_fails_closed_on_v2_and_reads_the_offered_name(conn):
    from boss_engine.services.mcp_registry import approved_arguments_match

    args = {"title": "bug", "body": "x"}
    # The agent loop binds under the name it offered the model; the door is called with the
    # server's own tool name.
    bound = ab.bind_held_call("mcp_github_create_issue", args)
    _decision(conn, "d_ok", "approved", {"tool": "mcp_github_create_issue",
                                         "tool_name": "create_issue", **bound})
    assert approved_arguments_match(conn, "p1", "d_ok", "create_issue", dict(args)) is True
    assert approved_arguments_match(conn, "p1", "d_ok", "create_issue",
                                    {"title": "bug", "body": "y"}) is False
    assert approved_arguments_match(conn, "p1", "d_ok", "delete_repo", dict(args)) is False
    unsigned = {k: v for k, v in bound.items() if k != "args_sig"}
    _decision(conn, "d_unsigned", "approved", {"tool": "mcp_github_create_issue",
                                               "tool_name": "create_issue", **unsigned})
    assert approved_arguments_match(conn, "p1", "d_unsigned", "create_issue", dict(args)) is False
    _decision(conn, "d_v1", "approved", {"run_id": "r1"})
    assert approved_arguments_match(conn, "p1", "d_v1", "create_issue", {"any": 1}) is True


@_needs_db
def test_a_code_task_resume_loads_only_an_approved_decision(conn):
    bound = ab.bind_held_call("write_file", dict(_ARGS))
    _decision(conn, "d_yes", "approved", {"tool": "write_file", **bound})
    _decision(conn, "d_wait", "pending", {"tool": "write_file", **bound})
    call = approved_call_for_decision(conn, "p1", "d_yes")
    assert call["decision_id"] == "d_yes" and call["args_sig"] == bound["args_sig"]
    assert ab.binding_holds("write_file", dict(_ARGS), call) is True
    assert approved_call_for_decision(conn, "p1", "d_wait") is None
    assert approved_call_for_decision(conn, "p1", "missing") is None
