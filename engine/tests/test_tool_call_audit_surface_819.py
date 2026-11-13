"""BOSSAPP-819: the tool-call audit can be read — filtered, keyset-paged, exported — and its rows
say who.

* query_tool_calls filters by run, server, tool, decision, outcome, screen verdict and time, and
  pages by (recorded_at, id) with no gaps and no repeats; a cursor it did not hand out is refused.
* export_tool_calls streams the whole filter, bounded.
* `explanation` is an optional column a writer MAY fill; the table is still append-only.
* The receipt door and the kernel's sink name the real agent and the identity principal.
"""
from __future__ import annotations

import contextlib
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import ProgrammingError

from boss_engine.services import tool_call_audit as audit

URL = os.environ.get("BOSS_TEST_819_DATABASE_URL", "")
_needs_db = pytest.mark.skipif(not URL or "test" not in URL.lower(),
                               reason="requires BOSS_TEST_819_DATABASE_URL naming a test database")


@pytest.fixture(scope="module")
def engine():
    admin = create_engine(URL, future=True)
    with admin.begin() as c:
        c.execute(text("DROP SCHEMA IF EXISTS t819 CASCADE"))
        c.execute(text("CREATE SCHEMA t819"))
    eng = create_engine(URL, future=True, connect_args={"options": "-csearch_path=t819"})
    raw = eng.raw_connection()
    try:
        raw.cursor().execute(audit.SCHEMA_SQL)
        # SCHEMA_SQL's guard looks the trigger up by name across the database; this disposable
        # schema sits beside the suite's own tool_call_audit, so it is attached here explicitly.
        raw.cursor().execute(
            "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = "
            "'tool_call_audit_no_update' AND tgrelid = 't819.tool_call_audit'::regclass) THEN "
            "CREATE TRIGGER tool_call_audit_no_update BEFORE UPDATE OR DELETE ON "
            "t819.tool_call_audit FOR EACH ROW EXECUTE FUNCTION "
            "t819.tool_call_audit_append_only(); END IF; END $$;")
        raw.commit()
    finally:
        raw.close()
    yield eng
    eng.dispose()
    with admin.begin() as c:
        c.execute(text("DROP SCHEMA IF EXISTS t819 CASCADE"))
    admin.dispose()


def _row(c, project: str, *, tool: str = "fetch", run: str = "r1", server: str = "s1",
         decision: str = "allow", outcome: str = "succeeded", screen=None, explanation=None,
         args=None) -> str:
    return audit.record_tool_call(
        c, project_id=project, run_id=run, agent="atlas", principal="usr_1", server_id=server,
        tool=tool, arguments=args or {"url": "https://example.com"}, decision=decision,
        matched_policy_ids=[decision], decision_id=None, outcome=outcome, latency_ms=5,
        screen=screen, explanation=explanation)


@pytest.fixture(scope="module")
def seeded(engine):
    """Twelve rows in p819 (and one in another project), one transaction each so recorded_at
    differs (now() is the transaction's time)."""
    ids = []
    for i in range(12):
        with engine.begin() as c:
            ids.append(_row(
                c, "p819", tool="fetch" if i % 2 else "write", run="r1" if i < 6 else "r2",
                server="s1" if i % 3 else "s2",
                decision="ask" if i % 4 == 0 else "allow",
                outcome="refused" if i % 4 == 0 else "succeeded",
                screen=({"mode": "patterns", "verdict": "suspicious", "rules": ["ignore_previous"]}
                        if i == 5 else ({"mode": "patterns", "verdict": "clean", "rules": []}
                                        if i % 4 else None)),
                explanation="asks a person: tier 2" if i == 4 else None))
    with engine.begin() as c:
        _row(c, "p-other")
    return ids


@_needs_db
def test_one_project_newest_first_with_every_field(engine, seeded):
    with engine.begin() as c:
        page = audit.query_tool_calls(c, project_id="p819", limit=200)
    items = page["items"]
    assert len(items) == 12 and page["next"] is None
    assert {i["project_id"] for i in items} == {"p819"}
    stamps = [(i["recorded_at"], i["id"]) for i in items]
    assert stamps == sorted(stamps, reverse=True)
    first = items[0]
    assert set(first) >= {"tool", "args_sha256", "args_redacted", "decision", "matched_policy_ids",
                          "decision_id", "outcome", "latency_ms", "screen", "explanation",
                          "agent", "principal", "run_id", "server_id"}
    assert first["agent"] == "atlas" and first["principal"] == "usr_1"


@_needs_db
def test_every_filter_narrows(engine, seeded):
    with engine.begin() as c:
        def n(**f):
            return len(audit.query_tool_calls(c, project_id="p819", limit=200,
                                              **audit.tool_call_filters(**f))["items"])
        assert n(run_id="r1") == 6 and n(run_id="r2") == 6
        assert n(tool="fetch") == 6
        assert n(server_id="s2") == 4
        assert n(decision="ask") == 3 and n(outcome="refused") == 3
        assert n(screen="suspicious") == 1
        assert n(screen="clean") == 8
        assert n(screen="none") == 3
        assert n(run_id="r2", tool="fetch", decision="allow") == 3
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        assert n(since=future) == 0
        assert n(until=future.replace("+00:00", "Z")) == 12


@_needs_db
def test_the_explanation_is_kept_when_a_writer_has_one(engine, seeded):
    with engine.begin() as c:
        items = audit.query_tool_calls(c, project_id="p819", limit=200)["items"]
    said = [i["explanation"] for i in items if i["explanation"]]
    assert said == ["asks a person: tier 2"]


@_needs_db
def test_keyset_pages_have_no_gaps_and_no_repeats(engine, seeded):
    seen: list[str] = []
    cursor = None
    with engine.begin() as c:
        while True:
            page = audit.query_tool_calls(c, project_id="p819", limit=5, cursor=cursor)
            seen += [i["id"] for i in page["items"]]
            cursor = page["next"]
            if not cursor:
                break
        everything = [i["id"] for i in audit.query_tool_calls(c, project_id="p819", limit=200)["items"]]
        with pytest.raises(ValueError):
            audit.query_tool_calls(c, project_id="p819", cursor="not-a-cursor")
    assert seen == everything and len(seen) == len(set(seen)) == 12


def test_bad_filters_are_refused_in_words():
    with pytest.raises(ValueError, match="screen"):
        audit.tool_call_filters(screen="evil")
    with pytest.raises(ValueError, match="since"):
        audit.tool_call_filters(since="yesterday")
    with pytest.raises(ValueError, match="before"):
        audit.tool_call_filters(since="2026-11-12T10:00:00Z", until="2026-11-12T09:00:00Z")


@_needs_db
def test_the_export_streams_the_whole_filter_and_stops_at_the_cap(engine, seeded):
    with engine.begin() as c:
        rows = list(audit.export_tool_calls(c, project_id="p819"))
        capped = list(audit.export_tool_calls(c, project_id="p819", max_rows=7))
        refused = list(audit.export_tool_calls(c, project_id="p819",
                                               **audit.tool_call_filters(outcome="refused")))
    assert len(rows) == 12 and len(capped) == 7
    assert [r["id"] for r in capped] == [r["id"] for r in rows[:7]]
    assert {r["outcome"] for r in refused} == {"refused"} and len(refused) == 3


@_needs_db
def test_the_table_is_still_append_only_with_the_new_column(engine, seeded):
    for statement in ("UPDATE tool_call_audit SET explanation = 'rewritten' WHERE id = :id",
                      "DELETE FROM tool_call_audit WHERE id = :id"):
        with engine.connect() as c:
            tx = c.begin()
            with pytest.raises(ProgrammingError, match="append-only"):
                c.execute(text(statement), {"id": seeded[4]})
            tx.rollback()


def test_the_schema_appends_the_column_at_its_end():
    schema = (Path(__file__).resolve().parents[2] / "db" / "schema.sql").read_text()
    tail = schema[schema.rindex("-- BOSSAPP-816-819"):]
    assert "ALTER TABLE tool_call_audit ADD COLUMN IF NOT EXISTS explanation TEXT;" in tail
    assert "ADD COLUMN IF NOT EXISTS explanation TEXT" in audit.SCHEMA_SQL


# ── who: the receipt door and the kernel's sink ──────────────────────────────────────────────

def test_the_receipt_door_names_the_agent_and_the_acting_principal(monkeypatch):
    from boss_engine.identity import lineage
    from boss_engine.services import mcp_registry

    written: list[dict] = []
    monkeypatch.setattr(mcp_registry, "record_tool_call", lambda _c, **kw: written.append(kw))
    monkeypatch.setattr(mcp_registry, "needs_person", lambda _s: False)
    monkeypatch.setattr(mcp_registry, "tool_policy", lambda *_a: "off")
    server = {"id": "srv_1", "name": "github"}
    with lineage.acting(lineage.Acting(principal_id="usr_alice", org_id="org_1")):
        with pytest.raises(mcp_registry.McpToolOff):
            mcp_registry.call_tool_with_receipt(
                None, None, "p819", server, "create_issue", {"title": "t"},
                dedupe_key="k", tenant_scope=("o", "w"), agent="atlas")
    with pytest.raises(mcp_registry.McpToolOff):
        mcp_registry.call_tool_with_receipt(
            None, None, "p819", server, "create_issue", {"title": "t"},
            dedupe_key="k", tenant_scope=("o", "w"), principal="svc_bot")
    assert (written[0]["agent"], written[0]["principal"]) == ("atlas", "usr_alice")
    assert (written[1]["agent"], written[1]["principal"]) == ("mcp_registry", "svc_bot")


def test_the_kernel_sink_names_the_runs_actor_and_principal(monkeypatch):
    from types import SimpleNamespace

    from boss_engine.identity import lineage
    from boss_engine.kernel.ledger import DbSink, SinkMeta
    from boss_engine.kernel.types import Event

    written: list[dict] = []
    monkeypatch.setattr(audit, "record_tool_call", lambda _c, **kw: written.append(kw))

    class _Engine:
        @contextlib.contextmanager
        def begin(self):
            yield object()

    sink = DbSink(_Engine())
    state = SimpleNamespace(project_id="p819", run_id="krun", act_id=None)
    event = Event(kind="tool.result", run_id="krun", seq=1, step=1,
                  at=datetime.now(timezone.utc),
                  data={"tool": "read_logs", "args": {"run": "r1"}, "status": "ok"})
    with lineage.acting(lineage.Acting(principal_id="agt_atlas", org_id="org_1")):
        sink._tool_call(event, state, SinkMeta(engine="scripted", actor="engine_agent"))
    sink._tool_call(event, state, SinkMeta())
    assert (written[0]["agent"], written[0]["principal"]) == ("engine_agent", "agt_atlas")
    assert (written[1]["agent"], written[1]["principal"]) == ("kernel", None)
