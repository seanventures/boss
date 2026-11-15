"""ST-8 (BOSSAPP-1153): telemetry retention and monthly partitions.

The live database kept every event, tick receipt, anomaly signal and scrape for ever (laptop,
2026-11-14: events 501 MB, receipts 400 MB, anomaly_signals 298 MB after six weeks). These pin:

* the per-table policy and its floor (30 days for anything audit-relevant, whatever is asked);
* the dry run equals the real run, row for row and partition for partition;
* a batched delete never touches a row newer than its window, an audit event, a non-tick
  receipt, an unsettled receipt, a receipt any foreign key points at, or a signal of an open
  incident;
* `events` converts online into monthly partitions, twice in a row without harm, keeps its
  rows, indexes and trigger, and the schema still applies on top of it;
* an expired month drops whole, its audit events archived first;
* one process sweeps at a time (the advisory lock).

The database tests run on a fresh `boss_scratch_*` database of BOSS_TEST_DATABASE_URL's server.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text

from boss_engine.services import telemetry_retention as tr

NOW = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)


# ── the policy (pure) ────────────────────────────────────────────────────────────────────────

def test_every_audit_policy_has_a_thirty_day_floor_whatever_is_asked():
    for p in tr.POLICIES:
        if p.audit:
            assert p.floor_days >= 30, p.key
        assert tr.effective_days(p, {p.key: 1}) == p.floor_days
        assert tr.effective_days(p, {p.key: "nonsense"}) == max(p.floor_days, p.default_days)
        assert tr.effective_days(p, {}) == max(p.floor_days, p.default_days)
        assert tr.effective_days(p, {p.key: 400}) == 400
    assert tr.effective_days(tr.POLICY_BY_KEY["receipts"], {"receipts": 3}) == 30
    assert tr.effective_days(tr.POLICY_BY_KEY["events"], {"events": 0}) == 30


def test_only_tick_receipts_are_ever_candidates():
    kinds = set(tr.TICK_RECEIPT_KINDS)
    for evidence in ("decision.approve", "github.pull_request.merge", "github.pull_request.create",
                     "ssh_host.stack.deploy", "budget.llm_spend.settle", "infra.provision",
                     "model.invoke", "storage.cleanup", tr.RECEIPT_KIND):
        assert evidence not in kinds
    assert "status IN ('succeeded', 'failed')" in tr.POLICY_BY_KEY["receipts"].scope_sql


def test_the_cutover_month_is_always_at_least_two_days_away():
    assert tr._cutover(datetime(2026, 11, 14, tzinfo=timezone.utc)) == datetime(2026, 12, 1, tzinfo=timezone.utc)
    assert tr._cutover(datetime(2026, 11, 30, 12, tzinfo=timezone.utc)) == datetime(2027, 1, 1, tzinfo=timezone.utc)
    assert tr._cutover(datetime(2026, 12, 29, 23, tzinfo=timezone.utc)) == datetime(2027, 1, 1, tzinfo=timezone.utc)


def test_a_parent_index_keeps_the_declared_name_and_shape():
    got = tr._parent_index_sql("idx_events_type", "CREATE INDEX idx_events_type ON public.events USING btree "
                                                  "(project_id, event_type, created_at DESC)")
    assert got == ('CREATE INDEX IF NOT EXISTS "idx_events_type" ON events USING btree '
                   "(project_id, event_type, created_at DESC)")


# ── the database ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def db():
    from boss_engine.db import ensure_schema
    from boss_engine.dev.test_databases import scratch_database

    url = os.environ.get("BOSS_TEST_DATABASE_URL", "")
    if not url or "test" not in url.rsplit("/", 1)[-1]:
        pytest.skip("BOSS_TEST_DATABASE_URL must name a disposable boss_test* database")
    scratch = scratch_database(url, "st8")
    try:
        scratch_url = scratch.__enter__()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"cannot create the scratch database: {type(exc).__name__}")
    engine = create_engine(scratch_url, future=True)
    ensure_schema(engine)
    with engine.begin() as c:
        c.execute(text("INSERT INTO orgs (id, name) VALUES ('org_st8', 'st8') ON CONFLICT DO NOTHING"))
        c.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES ('ws_st8', 'org_st8', 'st8') "
                       "ON CONFLICT DO NOTHING"))
        c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES "
                       "('p_st8', 'ws_st8', 'st8', 'st8') ON CONFLICT DO NOTHING"))
        # A foreign key this module has never heard of: honoured, read from the catalogue.
        c.execute(text("CREATE TABLE st8_cost_lines (id TEXT PRIMARY KEY, "
                       "receipt_id TEXT REFERENCES receipts(id))"))
    try:
        yield engine
    finally:
        engine.dispose()
        scratch.__exit__(None, None, None)


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _event(c, event_type: str, at: datetime) -> str:
    i = _id("evt")
    c.execute(text("INSERT INTO events (id, project_id, event_type, actor, entity_type, entity_id, "
                   "data, created_at) VALUES (:i, 'p_st8', :t, 'test', 'x', 'y', '{}', :at)"),
              {"i": i, "t": event_type, "at": at})
    return i


def _receipt(c, kind: str, at: datetime, status: str = "succeeded") -> str:
    i = _id("rcpt")
    c.execute(text("INSERT INTO receipts (id, org_id, workspace_id, project_id, dedupe_key, "
                   "effect_kind, payload_hash, status, created_at, updated_at) VALUES "
                   "(:i, 'org_st8', 'ws_st8', 'p_st8', :i, :k, 'h', :s, :at, :at)"),
              {"i": i, "k": kind, "s": status, "at": at})
    return i


def _signal(c, at: datetime, incident: str | None = None) -> str:
    i = _id("sig")
    c.execute(text("INSERT INTO anomaly_signals (id, project_id, incident_id, kind, signature, ts) "
                   "VALUES (:i, 'p_st8', :inc, 'error_surge', 's', :at)"), {"i": i, "inc": incident, "at": at})
    return i


def _ids(c, table: str) -> set[str]:
    return {str(r[0]) for r in c.execute(text(f"SELECT id FROM {table}")).all()}


def _counts(report) -> dict[str, tuple[int, int, tuple[str, ...], int]]:
    return {t["policy"]: (t["rows"], t["bytes"] if not t["partitions"] else -1,
                          tuple(t["partitions"]), t["archived"]) for t in report["tables"]}


def test_dry_run_equals_real_and_nothing_protected_or_new_is_touched(db):
    old, mid, new = NOW - timedelta(days=120), NOW - timedelta(days=45), NOW - timedelta(days=2)
    keep: dict[str, set[str]] = {"events": set(), "receipts": set(), "anomaly_signals": set()}
    gone: dict[str, set[str]] = {"events": set(), "receipts": set(), "anomaly_signals": set()}
    with db.begin() as c:
        for _ in range(7):
            gone["events"].add(_event(c, "job.created", old))           # past 90 d
            gone["events"].add(_event(c, "receipt.created", mid))       # noise past 30 d
            keep["events"].add(_event(c, "job.created", mid))           # inside 90 d
            keep["events"].add(_event(c, "decision.approved", old))     # audit: never row-deleted
            keep["events"].add(_event(c, "receipt.created", new))
        for _ in range(5):
            gone["receipts"].add(_receipt(c, "metatron.watch", mid))
            keep["receipts"].add(_receipt(c, "metatron.watch", new))
            keep["receipts"].add(_receipt(c, "decision.approve", old))  # evidence
            keep["receipts"].add(_receipt(c, "github.pull_request.merge", old))
            keep["receipts"].add(_receipt(c, "ai_sre.overwatch", old, status="in_progress"))
            referenced = _receipt(c, "process_manager.tick", old)
            keep["receipts"].add(referenced)
            c.execute(text("INSERT INTO st8_cost_lines (id, receipt_id) VALUES (:i, :r)"),
                      {"i": _id("cl"), "r": referenced})
        c.execute(text("INSERT INTO incidents (id, project_id, dedupe_key, title, status) VALUES "
                       "('inc_open', 'p_st8', 'k1', 'open one', 'open'), "
                       "('inc_done', 'p_st8', 'k2', 'done one', 'resolved')"))
        for _ in range(4):
            gone["anomaly_signals"].add(_signal(c, mid))
            gone["anomaly_signals"].add(_signal(c, mid, "inc_done"))
            keep["anomaly_signals"].add(_signal(c, mid, "inc_open"))
            keep["anomaly_signals"].add(_signal(c, new))

    dry = tr.sweep(db, now=NOW, dry_run=True)
    with db.connect() as c:
        before = {t: _ids(c, t) for t in keep}
    assert before == {t: keep[t] | gone[t] for t in keep}, "a dry run deleted something"
    # A tiny batch: the loop runs many statements, each bounded.
    real = tr.sweep(db, now=NOW, batch=2, receipt=False)
    assert _counts(real) == _counts(dry)
    assert real["rows"] == dry["rows"] == sum(len(v) for v in gone.values())
    assert real["bytes"] == dry["bytes"] > 0
    with db.connect() as c:
        for t in keep:
            assert _ids(c, t) == keep[t], t
    by = {t["policy"]: t for t in real["tables"]}
    assert by["events"]["rows"] == 7 and by["events_noise"]["rows"] == 7
    assert by["receipts"]["rows"] == 5 and by["anomaly_signals"]["rows"] == 8
    # Nothing left to do: a second pass is a no-op.
    assert tr.sweep(db, now=NOW, dry_run=True)["rows"] == 0


def test_a_real_sweep_leaves_a_receipt_with_rows_and_bytes(db):
    with db.begin() as c:
        for _ in range(3):
            _receipt(c, "metatron.overwatch", NOW - timedelta(days=60))
    report = tr.sweep(db, now=NOW)
    assert report["rows"] == 3 and report["receipt_id"]
    with db.connect() as c:
        row = c.execute(text("SELECT effect_kind, status, result FROM receipts WHERE id = :i"),
                        {"i": report["receipt_id"]}).mappings().one()
    assert row["effect_kind"] == tr.RECEIPT_KIND and row["status"] == "succeeded"
    result = row["result"] if isinstance(row["result"], dict) else json.loads(row["result"])
    assert result["rows"] == 3 and result["bytes"] > 0
    assert {t["policy"] for t in result["tables"]} >= {"events", "receipts"}


def test_events_convert_online_twice_and_keep_everything(db):
    with db.begin() as c:
        ids = _ids(c, "events")
        _event(c, "job.created", NOW - timedelta(days=20))
        ids = _ids(c, "events")
    first = tr.ensure_partitioned(db, now=NOW)
    assert first.get("converted") is True, first
    second = tr.ensure_partitioned(db, now=NOW)
    assert second.get("converted") is False and not second.get("error"), second
    with db.connect() as c:
        assert tr.is_partitioned(c, "events")
        assert _ids(c, "events") == ids, "the conversion lost or added rows"
        parts = {p["name"]: p for p in tr.partitions(c, "events")}
        assert parts["events_legacy"]["lo"] is None
        assert parts["events_legacy"]["hi"] == datetime(2026, 12, 1, tzinfo=timezone.utc)
        assert "events_default" in parts and "events_2026_12" in parts
        names = {r[0] for r in c.execute(text(
            "SELECT indexname FROM pg_indexes WHERE tablename = 'events'")).all()}
        assert {"events_pkey", "idx_events_project_created", "idx_events_type", "idx_events_entity",
                "idx_events_fts", "idx_events_created_id"} <= names
        assert c.execute(text("SELECT count(*) FROM pg_trigger WHERE tgrelid = 'events'::regclass "
                              "AND tgname = 'events_notify_boss_events'")).scalar() == 1
        assert c.execute(text("SELECT count(*) FROM pg_constraint WHERE contype = 'f' "
                              "AND confrelid = 'events_legacy'::regclass")).scalar() == 0
        assert c.execute(text("SELECT count(*) FROM pg_constraint WHERE conname = 'events_legacy_bound'")).scalar() == 0
    # Writes keep working: now (the legacy range) and next month (its own partition).
    with db.begin() as c:
        a = _event(c, "job.created", datetime.now(timezone.utc))
        b = _event(c, "job.created", datetime(2026, 12, 5, tzinfo=timezone.utc))
        where = dict(c.execute(text("SELECT id, tableoid::regclass::text FROM events WHERE id IN (:a, :b)"),
                               {"a": a, "b": b}).all())
    assert where[b] == "events_2026_12"
    # The schema applies on top of the partitioned table without a new failure.
    from boss_engine.db import ensure_schema

    with db.connect() as c:
        before = c.execute(text("SELECT failed FROM schema_apply_runs ORDER BY id DESC LIMIT 1")).scalar()
    ensure_schema(db)
    with db.connect() as c:
        row = c.execute(text("SELECT failed, failures FROM schema_apply_runs ORDER BY id DESC LIMIT 1")).first()
        assert row[0] <= before, row[1]
        assert tr.is_partitioned(c, "events")


def test_an_expired_month_drops_whole_and_its_audit_events_are_archived(db):
    assert tr.ensure_partitioned(db, now=NOW).get("converted") is False
    later = datetime(2027, 3, 15, tzinfo=timezone.utc)   # legacy (to 2026-12-01) is past 90 days
    with db.connect() as c:
        audit = {str(r[0]) for r in c.execute(text(
            "SELECT id FROM events_legacy WHERE split_part(event_type, '.', 1) = 'decision'")).all()}
    assert audit
    dry = tr.sweep(db, now=later, dry_run=True)
    assert "events_legacy" in dry["partitions"]
    assert "partition:events_legacy" in dry["items"]
    real = tr.sweep(db, now=later, receipt=False)
    assert _counts(real) == _counts(dry)
    assert real["archived"] == dry["archived"] == len(audit)
    with db.connect() as c:
        assert c.execute(text("SELECT to_regclass('events_legacy')")).scalar() is None
        assert {str(r[0]) for r in c.execute(text("SELECT id FROM events_archive")).all()} >= audit
        assert tr.is_partitioned(c, "events")


def test_only_the_dry_runs_items_are_touched_on_a_real_run(db):
    with db.begin() as c:
        r = _receipt(c, "knowledge.embed", NOW - timedelta(days=90))
        s = _signal(c, NOW - timedelta(days=90))
    dry = tr.sweep(db, now=NOW, dry_run=True)
    assert {"rows:receipts", "rows:anomaly_signals"} <= set(dry["items"])
    tr.sweep(db, now=NOW, only=["rows:receipts"], receipt=False)
    with db.connect() as c:
        assert r not in _ids(c, "receipts")
        assert s in _ids(c, "anomaly_signals"), "a table outside the approved items was touched"


def test_one_process_sweeps_at_a_time(db):
    holder = db.connect()
    try:
        assert holder.execute(text("SELECT pg_try_advisory_lock(hashtext(:k))"), {"k": tr.LOCK_NAME}).scalar()
        assert tr.run_once(db, now=NOW) == {"skipped": "busy"}
        holder.execute(text("SELECT pg_advisory_unlock(hashtext(:k))"), {"k": tr.LOCK_NAME})
        holder.commit()
    finally:
        holder.close()
    got = tr.run_once(db, now=NOW)
    assert "sweep" in got and not got["sweep"]["errors"], got
    with db.connect() as c:
        valid = c.execute(text("SELECT indisvalid FROM pg_index WHERE indexrelid = "
                               "to_regclass('idx_receipts_created')")).scalar()
        assert valid is True
        share = tr.share(c)
    assert share["bytes"] > 0 and any(t["table"] == "events" and t["partitioned"] for t in share["tables"])
