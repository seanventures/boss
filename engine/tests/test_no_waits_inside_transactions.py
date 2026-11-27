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
    "stream_text_response",
    "create_embeddings",
    "run_checks",
    "run_checks_phased",
    "_verify_github_app",
    "wait_for_verdict",
    "chat",
    "chat_durably",
    "diff",
    "discover",
    "probe_repository",
    "check_repository",
    "check_model",
    "complete",
    "run_agent_test",
    "list_grants",
    "revoke_grant",
})

#: Qualified `receiver.method` calls that perform external network/process waits inside a tx.
WAIT_QUALIFIED_CALLS = frozenset({
    "github_setup.verify",
    "subscription_keepalive.sweep",
    "openrouter_catalog.refresh",
    "cat.refresh",
    "registry.refresh",
    "sandbox.run",
    "sb.run",
})

#: Strictly bounded allowlist of benign deterministic DB/in-memory calls inside transactions.
#: Must ONLY shrink toward zero; any new entry or count increase fails this test.
ALLOWLIST_COUNTS: dict[tuple[str, str], int] = {
    ("apps/api/app/main.py", "idempotency.complete"): 1,
    ("apps/api/app/main.py", "break_glass.list_grants"): 1,
    ("apps/api/app/main.py", "break_glass.revoke_grant"): 1,
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

MAX_ALLOWLIST_TOTAL = 15


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


def _scan_tree(tree: ast.AST, rel: str) -> list[tuple[str, int, int, str]]:
    hits: list[tuple[str, int, int, str]] = []

    def walk_node(node: ast.AST, tx_lines: list[int]) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            for child in ast.iter_child_nodes(node):
                walk_node(child, [])
            return
        if isinstance(node, (ast.With, ast.AsyncWith)):
            has_tx = any(_is_tx_ctx(item.context_expr) for item in node.items)
            new_tx = tx_lines + ([node.lineno] if has_tx else [])
            for item in node.items:
                walk_node(item.context_expr, tx_lines)
            for child in node.body:
                walk_node(child, new_tx)
            return
        if tx_lines and isinstance(node, ast.Call):
            recv, attr = _call_name(node)
            label = f"{recv}.{attr}" if recv else attr
            if attr in WAIT_CALL_NAMES or label in WAIT_QUALIFIED_CALLS:
                hits.append((rel, node.lineno, tx_lines[-1], label))
        for child in ast.iter_child_nodes(node):
            walk_node(child, tx_lines)

    walk_node(tree, [])
    return hits


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
            hits.extend(_scan_tree(tree, rel))
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

        # 5. github_app._call choke point
        from boss_engine.identity import github_app

        with pytest.raises(WaitInWrittenTransactionError, match="github_app.GET"):
            github_app._call("GET", "/app", token="tok")  # noqa: SLF001

        # 6. channel_oauth choke points
        from boss_engine.services import channel_oauth

        with pytest.raises(WaitInWrittenTransactionError, match="channel_oauth.refresh"):
            channel_oauth._exchange_refresh(settings, "ref", "cid", "csec")  # noqa: SLF001


def test_is_mutating_sql_and_raw_engine_begin_and_async_with(monkeypatch):
    monkeypatch.setenv(STRICT_TX_GUARD_ENV, "1")
    eng = _sqlite_written_engine()

    # Comment / literal non-mutating SQL must not mark transaction as written
    assert boss_db._is_mutating_sql("-- INSERT INTO items\nSELECT 1") is False  # noqa: SLF001
    assert boss_db._is_mutating_sql("SELECT 'INSERT INTO items' AS txt") is False  # noqa: SLF001
    assert boss_db._is_mutating_sql("SELECT 'FOR UPDATE' AS txt") is False  # noqa: SLF001

    # CTE writes and locking reads must be classified as mutating
    assert boss_db._is_mutating_sql(  # noqa: SLF001
        "/* note */ WITH ins AS (INSERT INTO events VALUES ('1') RETURNING id) SELECT id FROM ins"
    ) is True
    assert boss_db._is_mutating_sql("SELECT id FROM projects WHERE id = :p FOR UPDATE") is True  # noqa: SLF001
    assert boss_db._is_mutating_sql("SELECT id FROM projects FOR NO KEY UPDATE") is True  # noqa: SLF001

    # Raw engine.begin() (not boss_db.begin) is tracked by Engine hooks
    with eng.begin() as c:
        c.execute(text("SELECT 'INSERT INTO items'"))
        assert boss_db.in_written_transaction() is False
        c.execute(text("INSERT INTO items (id, val) VALUES ('raw1', 'v')"))
        assert boss_db.in_written_transaction() is True
        with pytest.raises(WaitInWrittenTransactionError, match="raw.engine.begin"):
            assert_not_in_written_tx("raw.engine.begin")
    assert boss_db.in_written_transaction() is False

    # async with begin(eng): is detected by AST scanner
    async_src = (
        "async def f(eng):\n"
        "    async with begin(eng) as conn:\n"
        "        await client.chat(conn)\n"
        "        github_setup.verify(conn)\n"
    )
    hits = _scan_tree(ast.parse(async_src), "synthetic.py")
    assert [label for _r, _ln, _txln, label in hits] == ["client.chat", "github_setup.verify"]


def test_postgres_cte_for_update_and_phased_setup_github_app(monkeypatch):
    import os
    import uuid
    from boss_engine.identity import github_app, github_setup, github_urls
    from boss_engine.services import setup_actions

    db_url = os.environ.get("DATABASE_URL") or ""
    if not db_url:
        pytest.skip("DATABASE_URL not configured")
    monkeypatch.setenv(STRICT_TX_GUARD_ENV, "1")
    pg_eng = create_engine(db_url, future=True)

    # 1. Live Postgres CTE WITH ... INSERT and SELECT ... FOR UPDATE trigger WaitInWrittenTransactionError
    with pytest.raises(WaitInWrittenTransactionError):
        with boss_db.begin(pg_eng) as c:
            c.execute(text(
                "WITH sel AS (SELECT id FROM projects LIMIT 1), "
                "ins AS (INSERT INTO events (id, project_id, event_type, actor, entity_type, entity_id, data) "
                "SELECT :eid, id, 'test.cte', 'test', 'project', id, '{}'::jsonb FROM sel RETURNING id) "
                "SELECT id FROM ins"
            ), {"eid": f"evt_{uuid.uuid4().hex[:10]}"})
            assert boss_db.in_written_transaction() is True
            try:
                github_app._call("GET", "/app", token="tok")  # noqa: SLF001
            finally:
                c.rollback()

    with pg_eng.begin() as c:
        c.execute(text("SELECT id FROM projects LIMIT 1 FOR UPDATE"))
        assert boss_db.in_written_transaction() is True
        with pytest.raises(WaitInWrittenTransactionError, match="github_app.GET"):
            github_app._call("GET", "/app", token="tok")  # noqa: SLF001
    assert boss_db.in_written_transaction() is False

    # 2. setup_actions.run_checks_phased(target="all") with a Setup-created GitHub App runs under STRICT_TX_GUARD
    org_id = f"org_{uuid.uuid4().hex[:8]}"
    ws_id = f"ws_{uuid.uuid4().hex[:8]}"
    proj_id = f"prj_{uuid.uuid4().hex[:8]}"
    with pg_eng.begin() as c:
        c.execute(text("INSERT INTO orgs (id, name) VALUES (:o, 'StrictOrg')"), {"o": org_id})
        c.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES (:w, :o, 'StrictWS')"),
                  {"w": ws_id, "o": org_id})
        c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES (:p, :w, 'StrictProj', :p)"),
                  {"p": proj_id, "w": ws_id})
        c.execute(text(
            "INSERT INTO identity_github_app (org_id, app_id, slug, html_url, client_id, installation_id, "
            "account_login, account_type, owner_login, owner_type) "
            "VALUES (:o, '777', 'boss-test', 'https://github.test/apps/boss-test', 'Iv1.test', '4242', "
            "'acme', 'Organization', 'acme', 'Organization')"
        ), {"o": org_id})

    calls: list[tuple[str, str]] = []

    def fake_transport(method, url, headers, body):
        calls.append((method, url))
        if url.endswith("/app"):
            return github_app.Response(200, b'{"id":777,"owner":{"login":"acme","type":"Organization"},"permissions":{"contents":"write"}}')
        if "/app/installations/4242/access_tokens" in url:
            return github_app.Response(201, b'{"token":"ghs_test"}')
        if "/app/installations/4242" in url:
            return github_app.Response(200, b'{"id":4242,"account":{"login":"acme","type":"Organization"},"permissions":{"contents":"write","pull_requests":"write","checks":"read","statuses":"read","metadata":"read","issues":"read","actions":"read"},"repository_selection":"all"}')
        if "/installation/repositories" in url:
            return github_app.Response(200, b'{"repositories":[{"full_name":"acme/web","private":true,"owner":{"login":"acme","type":"Organization"}}]}')
        return github_app.Response(404, b"{}")

    monkeypatch.setattr(github_app, "TRANSPORT", fake_transport)
    monkeypatch.setattr(github_app, "app_jwt", lambda db: github_urls.tag("ey.fake.jwt", github_app.api_base()))
    monkeypatch.setattr(setup_actions, "detect_local", lambda settings, http=None: None)
    monkeypatch.setattr(setup_actions, "_plan_checks", lambda c, settings, project_id, target: ("all", []))
    monkeypatch.setattr(github_setup, "app_config", lambda db, oid=None: {
        "source": "setup", "app_id": "777", "installation_id": "4242", "slug": "boss-test",
        "account_login": "acme", "account_type": "Organization",
        "owner_login": "acme", "owner_type": "Organization",
    })
    monkeypatch.setattr(github_setup, "app_ready", lambda db, oid=None: True)

    try:
        setup_actions.run_checks_phased(pg_eng, Settings(), proj_id, "all")
        assert any(u.endswith("/app") for _m, u in calls)
        assert any("/app/installations/4242" in u for _m, u in calls)
    finally:
        with pg_eng.begin() as c:
            c.execute(text("DELETE FROM identity_github_app WHERE org_id = :o"), {"o": org_id})
            c.execute(text("DELETE FROM projects WHERE id = :p"), {"p": proj_id})
            c.execute(text("DELETE FROM workspaces WHERE id = :w"), {"w": ws_id})
            c.execute(text("DELETE FROM orgs WHERE id = :o"), {"o": org_id})
