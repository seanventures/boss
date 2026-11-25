"""AST ratchet & runtime ContextVar guard: no model, git-host, sandbox or CI waits inside
written database transactions (BOSSAPP-1232).
"""
from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text

from boss_engine import db as boss_db
from boss_engine.config import Settings
from boss_engine.db import (
    STRICT_TX_GUARD_ENV,
    WaitInWrittenTransactionError,
    assert_not_in_written_tx,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Call attribute/function names that indicate a slow wait, external network call, or
#: lifecycle/state-completion boundary to inspect inside `with begin/transaction/_tx`.
WAIT_CALL_NAMES = frozenset({
    "propose_pull_request",
    "merge_pull_request",
    "create_text_response",
    "create_chat_completion",
    "wait_for_verdict",
    "run_checks",
    "chat",
    "diff",
    "discover",
    "probe_repository",
    "complete",
    "run_agent_test",
    "list_grants",
    "revoke_grant",
})

#: Strictly bounded allowlist of benign deterministic DB/in-memory calls inside transactions.
#: Must ONLY shrink toward zero; any new entry or count increase fails this test.
ALLOWLIST_COUNTS: dict[tuple[str, str], int] = {
    ("apps/api/app/main.py", "idempotency.complete"): 1,
    ("apps/api/app/main.py", "break_glass.list_grants"): 1,
    ("apps/api/app/main.py", "break_glass.revoke_grant"): 1,
    ("apps/api/app/main.py", "complete"): 1,
    ("apps/api/app/main.py", "jobs.complete"): 1,
    ("apps/api/app/main.py", "agent_studio.run_agent_test"): 1,
    ("apps/api/app/routers/agent_kms.py", "kms.revoke_grant"): 1,
    ("apps/api/app/routers/agent_kms.py", "kms.list_grants"): 1,
    ("apps/api/app/routers/agent_specs.py", "versions.diff"): 1,
    ("apps/api/app/routers/mcp_credentials.py", "tool_access_requests.list_grants"): 1,
    ("apps/api/app/routers/mcp_credentials.py", "tool_access_requests.revoke_grant"): 1,
    ("apps/api/app/routes_mcp_oauth.py", "m.revoke_grant"): 1,
    ("engine/boss_engine/services/bots.py", "jobs.complete"): 1,
    ("engine/boss_engine/services/canaries.py", "jl.complete"): 1,
    ("engine/boss_engine/services/canaries.py", "lease_service.complete"): 1,
    ("engine/boss_engine/services/chaos.py", "jl.complete"): 1,
}

MAX_ALLOWLIST_TOTAL = 16


def _is_tx_ctx(expr: ast.AST) -> bool:
    if isinstance(expr, ast.Call):
        fn = expr.func
        name = fn.attr if isinstance(fn, ast.Attribute) else (fn.id if isinstance(fn, ast.Name) else "")
        if name in {"begin", "_transaction", "_tx"}:
            return True
    return False


def _call_name(node: ast.Call) -> tuple[str, str]:
    fn = node.func
    if isinstance(fn, ast.Attribute):
        recv = (
            fn.value.id
            if isinstance(fn.value, ast.Name)
            else (fn.value.attr if isinstance(fn.value, ast.Attribute) else "")
        )
        return recv, fn.attr
    if isinstance(fn, ast.Name):
        return "", fn.id
    return "", ""


def _scan_waits_in_transactions() -> list[tuple[str, int, int, str]]:
    hits: list[tuple[str, int, int, str]] = []
    for base in (
        REPO_ROOT / "apps" / "api",
        REPO_ROOT / "apps" / "worker",
        REPO_ROOT / "engine" / "boss_engine",
    ):
        for path in sorted(base.rglob("*.py")):
            posix = path.as_posix()
            if "/tests/" in posix or "\\tests\\" in posix:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            rel = path.relative_to(REPO_ROOT).as_posix()

            def walk_node(node: ast.AST, tx_lines: list[int]) -> None:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    for child in ast.iter_child_nodes(node):
                        walk_node(child, [])
                    return
                if isinstance(node, ast.With):
                    has_tx = any(_is_tx_ctx(item.context_expr) for item in node.items)
                    new_tx = tx_lines + ([node.lineno] if has_tx else [])
                    for item in node.items:
                        walk_node(item.context_expr, tx_lines)
                    for child in node.body:
                        walk_node(child, new_tx)
                    return
                if tx_lines and isinstance(node, ast.Call):
                    recv, attr = _call_name(node)
                    if attr in WAIT_CALL_NAMES:
                        label = f"{recv}.{attr}" if recv else attr
                        hits.append((rel, node.lineno, tx_lines[-1], label))
                for child in ast.iter_child_nodes(node):
                    walk_node(child, tx_lines)

            walk_node(tree, [])
    return hits


def test_no_new_waits_inside_transactions():
    assert sum(ALLOWLIST_COUNTS.values()) <= MAX_ALLOWLIST_TOTAL
    hits = _scan_waits_in_transactions()
    counts = Counter((rel, label) for rel, _ln, _txln, label in hits)
    unexpected = {
        key: count
        for key, count in counts.items()
        if count > ALLOWLIST_COUNTS.get(key, 0)
    }
    assert not unexpected, (
        "New wait/external call(s) detected inside a DB transaction: "
        + ", ".join(f"{rel}:{label} x{cnt}" for (rel, label), cnt in sorted(unexpected.items()))
    )


def _sqlite_written_engine():
    eng = create_engine("sqlite+pysqlite:///:memory:", future=True)
    with eng.begin() as c:
        c.execute(text("CREATE TABLE items (id TEXT PRIMARY KEY, val TEXT)"))
    return eng


def test_contextvar_guard_tracks_written_transactions_only(monkeypatch):
    monkeypatch.setenv(STRICT_TX_GUARD_ENV, "1")
    eng = _sqlite_written_engine()

    # Outside tx: ok
    assert_not_in_written_tx("test.outside")

    # Read-only begin(): ok
    with boss_db.begin(eng) as c:
        c.execute(text("SELECT count(*) FROM items")).scalar()
        assert boss_db.in_written_transaction() is False
        assert_not_in_written_tx("test.read_only")

    # Written begin(): raises under BOSS_STRICT_TX_GUARD=1
    with boss_db.begin(eng) as c:
        c.execute(text("INSERT INTO items (id, val) VALUES ('1', 'a')"))
        assert boss_db.in_written_transaction() is True
        with pytest.raises(WaitInWrittenTransactionError, match="test.written"):
            assert_not_in_written_tx("test.written")

    # After commit: cleared
    assert boss_db.in_written_transaction() is False
    assert_not_in_written_tx("test.after_commit")


def test_choke_points_raise_inside_written_transaction(monkeypatch, tmp_path):
    monkeypatch.setenv(STRICT_TX_GUARD_ENV, "1")
    eng = _sqlite_written_engine()
    settings = Settings()

    from boss_engine.services.github_adapter import GitHubAdapter
    from boss_engine.services.model_routing import GovernedChatAdapter, ModelChoice
    from boss_engine.services.pipeline_adapter import GitHubActionsPipeline
    from boss_engine.services.sandbox import EphemeralSandbox

    class _DummyModel:
        def create_text_response(self, *_a, **_k):
            return "ref", "ok", {}

    class _StubAdapter(GovernedChatAdapter):
        def _open(self):
            return ("res_1", "rcpt_1")

    with boss_db.begin(eng) as c:
        c.execute(text("INSERT INTO items (id, val) VALUES ('2', 'b')"))

        # 1. model_routing choke point
        model = _StubAdapter(
            _DummyModel(), c, settings, "prj_1", skill_id="test", ref="r",
            estimate_usd_micros=0, choice=ModelChoice("codex", "test"),
        )
        with pytest.raises(WaitInWrittenTransactionError, match="model_routing.create_text_response"):
            model.create_text_response("hi")

        # 2. github_adapter choke point
        with pytest.raises(WaitInWrittenTransactionError, match="github_adapter.GET"):
            GitHubAdapter._send("GET", "https://example.invalid", headers={})

        # 3. sandbox choke points
        sb = EphemeralSandbox(settings, task_id="tsk_guard")
        sb.path = tmp_path
        with pytest.raises(WaitInWrittenTransactionError, match="sandbox.run"):
            sb.run("echo hi")
        with pytest.raises(WaitInWrittenTransactionError, match="sandbox.diff"):
            sb.diff()

        # 4. pipeline_adapter choke point
        pipe = GitHubActionsPipeline(
            settings,
            github=SimpleNamespace(repository_full_name="acme/app"),  # type: ignore[arg-type]
        )
        with pytest.raises(WaitInWrittenTransactionError, match="pipeline_adapter.wait_for_verdict"):
            pipe.wait_for_verdict("abc123", timeout_seconds=1, poll_interval_seconds=1)
