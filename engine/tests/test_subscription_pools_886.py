"""BOSSAPP-886: several subscription accounts per provider, used as one pool.

Members' logins live in the secret store under their own names in the subscription namespace ->
a run gets the member with the most headroom and keeps it for its task -> a member at its limit is
skipped; the pool is exhausted only when every member is -> the run's rate windows land on its
member -> Claude runs on the member's token, Codex on the member's own CODEX_HOME, and a rotated
codex login is synced back to the MEMBER's store key through the one sync seam -> no pool, no
change.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text

from boss_engine.config import Settings
from boss_engine.services import subscription_pools as sp
from boss_engine.services.llm_telemetry import clear_llm_context, set_llm_context

TEST_DB = os.environ.get(
    "BOSS_TEST_DATABASE_URL",
    os.environ.get("DATABASE_URL", "postgresql+psycopg://boss:boss@localhost:5432/boss_test"),
)
TOKEN_A = "sk-ant-oat01-" + "a" * 48         # shaped like logins; not real ones
TOKEN_B = "sk-ant-oat01-" + "b" * 48


class MemoryStore:
    kind = "memory"

    def __init__(self) -> None:
        self.held: dict[str, str] = {}

    def get(self, key):
        from boss_engine.services.secret_backends import SecretNotFound

        if key not in self.held:
            raise SecretNotFound(key)
        return self.held[key]

    def put(self, key, value):
        self.held[key] = value

    def delete(self, key):
        self.held.pop(key, None)


@pytest.fixture()
def store(monkeypatch):
    """ONE store for every reader and writer: the pool's writes, stored_credential's reads and
    sync_rotated_credential's write-back all go through secret_backends."""
    held = MemoryStore()
    from boss_engine.services import secret_backends

    monkeypatch.setattr(secret_backends, "backend_for", lambda kind, settings=None: held)
    monkeypatch.setattr(secret_backends, "pick_default_backend", lambda settings: ("memory", []))
    monkeypatch.setattr(secret_backends, "default_secret_backend", lambda settings: "memory")
    for name in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "CODEX_AUTH_JSON"):
        monkeypatch.delenv(name, raising=False)
    from boss_engine.services.model_router import accounts

    accounts.reset_caches()
    yield held
    accounts.reset_caches()
    clear_llm_context()


@pytest.fixture(scope="module")
def eng():
    engine = create_engine(TEST_DB, future=True)
    try:
        with engine.begin() as c:
            c.execute(text("SELECT 1 FROM subscription_pool_members LIMIT 1"))
    except Exception as error:  # pragma: no cover
        pytest.skip(f"test database unavailable or not migrated: {error}")
    return engine


@pytest.fixture()
def project(eng):
    pid = f"p_{uuid.uuid4().hex[:10]}"
    with eng.begin() as c:
        wid = c.execute(text("SELECT id FROM workspaces LIMIT 1")).scalar()
        if wid is None:
            pytest.skip("test database has no workspace")
        c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) "
                       "VALUES (:i, :w, 'w4o1 886', :s)"), {"i": pid, "w": wid, "s": pid})
    yield pid
    with eng.begin() as c:
        c.execute(text("DELETE FROM subscription_accounts WHERE account_key IN (SELECT "
                       "m.account_key FROM subscription_pool_members m JOIN subscription_pools s "
                       "ON s.id = m.pool_id WHERE s.project_id = :p)"), {"p": pid})
        c.execute(text("DELETE FROM subscription_pools WHERE project_id = :p"), {"p": pid})
        c.execute(text("DELETE FROM events WHERE project_id = :p"), {"p": pid})
        c.execute(text("DELETE FROM projects WHERE id = :p"), {"p": pid})


def _windows(c, member, *, used=None, rejected_until=None):
    body = {}
    if used is not None:
        body["seven_day"] = {"used": used, "status": "allowed",
                             "resets_at": (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()}
    if rejected_until is not None:
        body["seven_day"] = {"used": 1.0, "status": "rejected", "resets_at": rejected_until.isoformat()}
    c.execute(text("INSERT INTO subscription_accounts (account_key, engine, windows) VALUES "
                   "(:k, 'claude_code', CAST(:w AS JSONB)) ON CONFLICT (account_key) DO UPDATE "
                   "SET windows = EXCLUDED.windows"), {"k": member["account_key"],
                                                       "w": json.dumps(body)})
    from boss_engine.services.model_router import accounts

    accounts.reset_caches()


def _pool(c, pid, engine="claude_code", logins=(TOKEN_A, TOKEN_B)):
    pool = sp.create_pool(c, pid, engine=engine, name="seats", actor="operator")
    members = [sp.add_member(c, Settings(), pid, pool["id"], label=f"seat {i}", credential=v,
                             actor="operator") for i, v in enumerate(logins)]
    return pool, members


def test_logins_go_to_the_subscription_store_never_a_table(eng, project, store):
    with eng.begin() as c:
        pool, members = _pool(c, project)
        listed = sp.describe(c, project)
        rows = c.execute(text("SELECT * FROM subscription_pool_members WHERE pool_id = :p"),
                         {"p": pool["id"]}).mappings().all()
    keys = sorted(store.held)
    assert all(k.startswith("BOSS_SUBSCRIPTION_CLAUDE_CODE_OAUTH_TOKEN_POOL_") for k in keys)
    assert sorted(store.held.values()) == sorted([TOKEN_A, TOKEN_B])
    assert TOKEN_A not in json.dumps(listed, default=str) and "secret_name" not in json.dumps(listed)
    assert all(TOKEN_A not in json.dumps(dict(r), default=str) for r in rows)
    assert [m["account_key"] for m in members] == [f"claude_code:pool:{m['id']}" for m in members]


def test_most_headroom_wins_the_pin_holds_and_exhausted_is_every_member(eng, project, store):
    with eng.begin() as c:
        _pool_, (a, b) = _pool(c, project)
        _windows(c, a, used=0.9)                         # seat 0 nearly spent
        first = sp.assign(c, project, "claude_code", "wfr_1")
        assert first.member_id == b["id"] and not first.pinned
        _windows(c, b, used=0.95)                         # now seat 1 has less left...
        again = sp.assign(c, project, "claude_code", "wfr_1")
        assert again.member_id == b["id"] and again.pinned, "the task stays on its account"
        other = sp.assign(c, project, "claude_code", "wfr_2")
        assert other.member_id == a["id"], "a new task gets the most headroom"
        soon = datetime.now(timezone.utc) + timedelta(hours=2)
        _windows(c, b, rejected_until=soon)               # the pinned seat hits its limit
        moved = sp.assign(c, project, "claude_code", "wfr_1")
        assert moved.member_id == a["id"], "a pin never holds a run on a spent account"
        _windows(c, a, rejected_until=soon + timedelta(hours=1))
        with pytest.raises(sp.PoolExhausted) as full:
            sp.assign(c, project, "claude_code", "wfr_3")
        assert full.value.retry_at is not None and abs(
            (full.value.retry_at - soon).total_seconds()) < 2


def test_a_run_spends_on_its_member(eng, project, store):
    from boss_engine.services.model_router import accounts

    with eng.begin() as c:
        _pool(c, project)
    set_llm_context(project_id=project, run_id="crun_9", db_engine=eng)
    chosen = sp.assignment_for("claude_code", Settings())
    assert chosen is not None
    assert accounts.account_key(None, Settings(), project, "claude_code") == chosen.account_key
    assert accounts.account_key(None, Settings(), project, "codex") != chosen.account_key


def test_claude_runs_on_the_members_token(eng, project, store, monkeypatch):
    from boss_engine.services import code_agent as ca

    with eng.begin() as c:
        _pool_, (a, b) = _pool(c, project)
        _windows(c, a, used=0.9)
    set_llm_context(project_id=project, run_id="crun_c", db_engine=eng)
    env = ca.ClaudeCodeAgent(Settings().model_copy(
        update={"claude_code_oauth_token": "", "anthropic_api_key": None}))._child_env()
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN_B
    assert "ANTHROPIC_API_KEY" not in env


def test_codex_runs_on_the_members_home_and_syncs_back_to_the_member(eng, project, store,
                                                                     monkeypatch, tmp_path):
    from boss_engine.services.cli_harness import GenericCliCodeAgent, load_specs

    base = tmp_path / "codex"
    base.mkdir()
    (base / "auth.json").write_text(json.dumps({"tokens": {"x": "main"},
                                                "last_refresh": "2026-11-01T00:00:00Z"}))
    (base / "config.toml").write_text("model = 'x'\n")
    monkeypatch.setenv("CODEX_HOME", str(base))
    seat = json.dumps({"tokens": {"x": "seat"}, "last_refresh": "2026-11-10T00:00:00Z"})
    with eng.begin() as c:
        _pool_, (member,) = _pool(c, project, engine="codex", logins=(seat,))
    set_llm_context(project_id=project, run_id="crun_x", db_engine=eng)
    settings = Settings()
    agent = GenericCliCodeAgent(settings, load_specs(settings)["codex"])
    agent._preflight(read_only=False)
    home = agent.cli_home()
    assert home == f"{base}-pool-{member['id']}"
    assert json.loads(open(os.path.join(home, "auth.json")).read())["tokens"]["x"] == "seat"
    assert json.loads((base / "auth.json").read_text())["tokens"]["x"] == "main", \
        "the single login's home is never written"
    env = agent._env()
    assert env["CODEX_HOME"] == home and "OPENAI_API_KEY" not in env
    # The CLI rotated the member's login during the run: it goes back to the MEMBER's key.
    rotated = json.dumps({"tokens": {"x": "seat-2"}, "last_refresh": "2026-11-12T00:00:00Z"})
    with open(os.path.join(home, "auth.json"), "w") as fh:
        fh.write(rotated)
    assert agent.remember_rotated_credential() is True
    member_key = sp._store_key(sp.secret_name_for("codex", member["id"]))
    assert store.held[member_key] == rotated
    assert "BOSS_SUBSCRIPTION_CODEX_AUTH_JSON" not in store.held, "the single login is untouched"


def test_no_pool_changes_nothing(eng, project, store):
    assert sp.assignment_for("claude_code", Settings()) is None        # no project in context
    set_llm_context(project_id=project, run_id="crun_n", db_engine=eng)
    assert sp.assignment_for("claude_code", Settings()) is None        # a project with no pool
    with eng.begin() as c:
        pool, _members = _pool(c, project)
        sp.set_pool_status(c, project, pool["id"], status="disabled", actor="operator")
    assert sp.assignment_for("claude_code", Settings()) is None        # a pool turned off


def test_a_refused_member_leaves_rotation(eng, project, store):
    with eng.begin() as c:
        _pool_, (a, b) = _pool(c, project)
    sp.mark_needs_reauth(Settings().model_copy(update={"database_url": TEST_DB}), a["id"])
    with eng.begin() as c:
        for task in ("t1", "t2", "t3"):
            assert sp.assign(c, project, "claude_code", task).member_id == b["id"]
        fresh = sp.update_member(c, Settings(), project, a["id"], actor="operator",
                                 credential=TOKEN_A)
        assert fresh["status"] == "active"
