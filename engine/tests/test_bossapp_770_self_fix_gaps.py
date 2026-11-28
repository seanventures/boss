"""Tests for BOSSAPP-770: self-fix defaults ($50/fix, $500/month), SF-11 cross-project gap lookup,
SF-16 close_platform_gap tool, and ENG-1 budget_limit across NativeCodeAgent & CodexCodeAgent."""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text

from boss_engine.config import Settings
from boss_engine.services import boss_recommendations as recs
from boss_engine.services import self_fix as sf
from boss_engine.services.code_agent import NativeCodeAgent
from boss_engine.services.codex_code_agent import CodexCodeAgent
from boss_engine.services.engine_contract import (
    BUDGET_PARTIAL_MARKER,
    ContractedAgent,
    capabilities_of,
)
from boss_engine.tools.builtin import _register_read_tools
from boss_engine.tools.registry import ToolRegistry


def _db_url() -> str:
    return os.environ.get("BOSS_TEST_DATABASE_URL") or os.environ.get("DATABASE_URL") or ""


@pytest.fixture()
def pg_engine():
    url = _db_url()
    if not url:
        pytest.skip("requires Postgres (DATABASE_URL / BOSS_TEST_DATABASE_URL)")
    eng = create_engine(url, future=True)
    yield eng
    eng.dispose()


def _seed_org_and_project(conn, *, org_id: str, project_id: str) -> None:
    ws_id = f"ws_{project_id}"
    conn.execute(
        text("INSERT INTO orgs (id, name) VALUES (:o, :n) ON CONFLICT (id) DO NOTHING"),
        {"o": org_id, "n": f"Org {org_id}"},
    )
    conn.execute(
        text("INSERT INTO workspaces (id, org_id, name) VALUES (:w, :o, :n) ON CONFLICT (id) DO NOTHING"),
        {"w": ws_id, "o": org_id, "n": f"WS {project_id}"},
    )
    conn.execute(
        text(
            "INSERT INTO projects (id, workspace_id, name, slug) "
            "VALUES (:p, :w, :n, :s) ON CONFLICT (id) DO NOTHING"
        ),
        {"p": project_id, "w": ws_id, "n": f"Proj {project_id}", "s": project_id},
    )


def test_self_fix_defaults_pinned_to_50_per_fix_and_500_per_month(monkeypatch):
    monkeypatch.delenv("BOSS_SELF_FIX_BUDGET_CENTS", raising=False)
    monkeypatch.delenv("BOSS_SELF_FIX_MONTH_CENTS", raising=False)
    assert sf.DEFAULTS["self_fix_budget_cents"] == 5000
    assert sf.DEFAULTS["self_fix_month_cents"] == 50000
    s = Settings(_env_file=None)
    assert s.self_fix_budget_cents == 5000
    assert s.self_fix_month_cents == 50000


def test_sf11_cross_project_lookup_and_sf16_close_platform_gap_with_org_isolation(pg_engine, tmp_path):
    suffix = uuid.uuid4().hex[:8]
    org_a = f"org_sf11a_{suffix}"
    org_b = f"org_sf11b_{suffix}"
    proj_a1 = f"p_sf11a1_{suffix}"
    proj_a2 = f"p_sf11a2_{suffix}"
    proj_b1 = f"p_sf11b1_{suffix}"

    settings = Settings(_env_file=None, artifacts_dir=str(tmp_path / "artifacts"))
    with pg_engine.begin() as c:
        _seed_org_and_project(c, org_id=org_a, project_id=proj_a1)
        _seed_org_and_project(c, org_id=org_a, project_id=proj_a2)
        _seed_org_and_project(c, org_id=org_b, project_id=proj_b1)
        rec_a2 = recs.record(
            c, settings, project_id=proj_a2, signature=f"sig:a2:{suffix}",
            title="Sibling project gap", symptom="fails on staging",
            root_cause="missing retry", proposed_change="add backoff",
            acceptance_tests=["retries twice"],
        )
        rec_b1 = recs.record(
            c, settings, project_id=proj_b1, signature=f"sig:b1:{suffix}",
            title="Other org secret gap", symptom="other org symptom",
            root_cause="other org", proposed_change="other org",
            acceptance_tests=["isolated"],
        )

    reg = ToolRegistry()
    _register_read_tools(reg)
    ctx = SimpleNamespace(begin=pg_engine.begin, settings=settings)

    list_fn = reg.get("list_platform_gaps").handler
    close_fn = reg.get("close_platform_gap").handler

    # SF-11: Lookup by id from proj_a1 finds sibling proj_a2 gap in the SAME org
    found = list_fn(ctx, project_id=proj_a1, actor="shepherd", args={"id": rec_a2["id"]})
    assert found["ok"] is True and found["found"] is True
    assert found["cross_project"] is True
    assert found["gap"]["id"] == rec_a2["id"]
    assert found["gap"]["project_id"] == proj_a2

    # SF-11: all_projects=True lists org_a gaps and NEVER leaks org_b gaps
    org_list = list_fn(ctx, project_id=proj_a1, actor="shepherd", args={"all_projects": True})
    ids = {g["id"] for g in org_list["gaps"]}
    assert rec_a2["id"] in ids
    assert rec_b1["id"] not in ids

    # Tenant isolation: proj_a1 MUST NOT see or close org_b's gap
    denied_read = list_fn(ctx, project_id=proj_a1, actor="shepherd", args={"id": rec_b1["id"]})
    assert denied_read["ok"] is False and denied_read["found"] is False
    denied_close = close_fn(
        ctx, project_id=proj_a1, actor="shepherd",
        args={"id": rec_b1["id"], "status": "done", "reason": "attempt cross-org close"},
    )
    assert denied_close["ok"] is False and denied_close["found"] is False

    # SF-16: Validation rejects blank reason and invalid status
    bad_reason = close_fn(
        ctx, project_id=proj_a1, actor="shepherd",
        args={"id": rec_a2["id"], "status": "done", "reason": "   "},
    )
    assert bad_reason["ok"] is False

    # SF-16: Closing sibling gap succeeds, updates DB status, emits audited event
    closed = close_fn(
        ctx, project_id=proj_a1, actor="shepherd",
        args={
            "id": rec_a2["id"],
            "status": "done",
            "reason": "Fixed in commit abc1234",
            "commit_ref": "abc1234",
            "pr_url": "https://github.com/seanventures/boss/pull/999",
        },
    )
    assert closed["ok"] is True
    assert closed["status"] == "done"
    assert closed["project_id"] == proj_a2
    assert closed["cross_project"] is True

    with pg_engine.begin() as c:
        row = c.execute(
            text("SELECT status FROM boss_recommendations WHERE id = :i"),
            {"i": rec_a2["id"]},
        ).scalar()
        assert row == "done"
        ev = c.execute(
            text("SELECT data FROM events WHERE project_id = :p AND event_type = 'boss.recommendation_closed' AND entity_id = :e"),
            {"p": proj_a2, "e": rec_a2["id"]},
        ).mappings().first()
        assert ev is not None
        payload = ev["data"] if isinstance(ev["data"], dict) else json.loads(ev["data"])
        assert payload["status"] == "done"
        assert payload["commit_ref"] == "abc1234"


def test_eng1_native_code_agent_honors_budget_limit(tmp_path: Path):
    calls = {"n": 0}

    class _ExpensiveLLM:
        def create_chat_completion(self, messages, *, tools=None, model_override=None, redact=False):
            calls["n"] += 1
            # Each turn requests a tool call and costs ~$0.08 (gpt-4o pricing).
            msg = {
                "content": f"Turn {calls['n']} working...",
                "tool_calls": [
                    {"id": f"tc_{calls['n']}", "name": "list_dir", "arguments": json.dumps({"path": "."})}
                ],
            }
            usage = {
                "model": "gpt-4o",
                "prompt_tokens": 10_000,
                "completion_tokens": 6_000,
                "total_tokens": 16_000,
            }
            return msg, usage

    settings = Settings(_env_file=None)
    raw = NativeCodeAgent(settings)
    raw._llm = _ExpensiveLLM()
    assert capabilities_of(raw).budget_limit is True

    contracted = ContractedAgent(raw)
    contracted.budget_ceiling = lambda: 0.05
    res = contracted.run_role("inspect the repo", tmp_path, read_only=True, max_turns=10)
    assert res["stop_reason"] == "budget_cap"
    assert res["budget_ceiling_usd"] == pytest.approx(0.05)
    assert BUDGET_PARTIAL_MARKER in res["summary"]
    assert calls["n"] == 1


def test_eng1_codex_code_agent_honors_budget_limit(tmp_path: Path, monkeypatch):
    from boss_engine.services import cli_harness, owned_process

    assert capabilities_of(CodexCodeAgent).budget_limit is True

    settings = Settings(_env_file=None)
    agent = CodexCodeAgent(settings, cli_harness.load_specs(settings)["codex"])
    monkeypatch.setattr(agent, "_preflight", lambda **kw: None)
    monkeypatch.setattr(agent, "_env", lambda: {**os.environ, "OPENAI_API_KEY": "sk-test-proxy"})
    monkeypatch.setattr(agent, "_codex_argv", lambda **kw: ["codex", "exec"])

    terminated = {"called": False}

    class _FakeChild:
        def __init__(self):
            self.pid = 424242
            self.returncode = -15
            self.stdin = SimpleNamespace(write=lambda _s: None, close=lambda: None)
            self._alive = True

        def poll(self):
            return None if self._alive else self.returncode

        def terminate(self):
            terminated["called"] = True
            self._alive = False

    monkeypatch.setattr(owned_process, "popen", lambda *a, **kw: _FakeChild())

    def _fake_drain(child, *, timeout, on_line, on_err_line):
        # Emit a metered turn.completed event with high token usage so cost exceeds $0.05.
        line = json.dumps({
            "type": "turn.completed",
            "usage": {"input_tokens": 50_000, "output_tokens": 20_000, "cached_input_tokens": 0},
        })
        on_line(line)
        return line + "\n", ""

    from boss_engine.services import code_agent as ca
    monkeypatch.setattr(ca, "_drain", _fake_drain)
    monkeypatch.setattr(ca, "register_seat", lambda *a, **kw: None)
    monkeypatch.setattr(ca, "release_seat", lambda *a, **kw: None)

    contracted = ContractedAgent(agent)
    contracted.budget_ceiling = lambda: 0.05
    res = contracted.run_role("do work", tmp_path, model="gpt-5.4")
    assert terminated["called"] is True
    assert res["stop_reason"] == "budget_cap"
    assert res["budget_ceiling_usd"] == pytest.approx(0.05)
    assert BUDGET_PARTIAL_MARKER in res["summary"]
