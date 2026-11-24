"""TELEMETRY RETENTION AND MONTHLY PARTITIONS (ST-8, BOSSAPP-1153; storage hygiene plan §2).

The live database kept every event, tick receipt, anomaly signal, metrics scrape and health
sample for ever. Measured on the operator's laptop (2026-11-14, six weeks of use): events 501 MB,
receipts 400 MB, anomaly_signals 298 MB, agent_turns 128 MB, app_metrics_scrapes 103 MB,
metatron_watch_ticks 70 MB, process_manager_ticks 40 MB, deployment_health_samples 26 MB. Only
the transcript (activity_retention) and the tick tables (resource_reaper) had a rule.

WHAT THIS DOES, per table (`POLICIES`; the days are settings — the `telemetry` retention rule's
`tables` map — under a hard floor):

    policy               rows it may delete                          default  floor
    events               known chatter kinds only (CHATTER)           90 d     30 d (audit)
    events_noise         loop chatter (receipt.created, *.completed)  30 d     14 d
    events_ticks         *.ticked, job.heartbeat (was resource_reaper) 14 d      7 d
    receipts             tick receipts only (allow-list), settled,   30 d     30 d (audit)
                         and referenced by NO foreign key
    anomaly_signals      signals not tied to an open incident        30 d      7 d
    app_metrics_scrapes  every scrape                                 7 d      7 d
    deployment_health_samples  every sample                          30 d      7 d

AUDIT EVIDENCE IS NEVER DELETED.
* Receipts: only the kinds in `TICK_RECEIPT_KINDS` (the overwatch/watch/tick loops — 97 % of the
  rows) are candidates at all. A decision, merge, deploy, PR, budget settlement, infra or model
  receipt is out of scope by construction, and a tick receipt that ANY foreign key points at (a
  cost line, a ledger row, a rollout, a delivery…) is kept: the referencing columns are read from
  pg_constraint on every sweep, so a new FK is honoured without touching this file.
* Events: a DELETE-list, not a keep-list. Only kinds known to be loop chatter (`*.ticked`,
  `job.heartbeat`, `receipt.created`, the overwatch/drive completions …) are ever deleted row by
  row; every other kind — including kinds nobody listed yet (workflow.merge_landed,
  project.created, incident.resolved, …) — is kept. When a whole month partition expires, every
  non-chatter row is first copied into `events_archive` (kept; checked before the drop) and only
  then is the partition dropped.

HOW IT DELETES.
* `events` is range-partitioned by month on created_at (`ensure_partitioned`), so an expired
  month is DROPPED in one catalogue operation. The existing table is converted online: a unique
  index built CONCURRENTLY, a NOT VALID check validated without blocking writers, then one short
  transaction (lock_timeout bounded) that renames the old table to `events_legacy`, creates the
  partitioned `events` with the same columns, indexes and trigger, and attaches the legacy table as
  the partition for everything before the cutover month — no row is copied or rewritten. Measured
  on a 1 GB seeded table in tests/bench (see the PR). The legacy partition drops like any other
  once its upper bound passes the window; until then its old rows go by batched deletes.
* Every other table: batched deletes (BATCH rows a statement, statement_timeout and lock_timeout
  per batch, a time budget per table per sweep), driven by an index on the timestamp — built
  CONCURRENTLY by `ensure_indexes` when missing, never by the boot schema apply (a plain CREATE
  INDEX blocks writers for the length of the build).
* Why not partition receipts / anomaly_signals too: ~20 foreign keys reference receipts(id), and a
  partitioned table cannot be referenced by `id` alone; anomaly signals need row-level protection
  (open incidents) a partition drop cannot give. Batched deletes keep them bounded.

THE SWEEP: a dry run lists exactly what the real run removes (same cut-off, same predicates, same
order), so `dry_run(now) == sweep(now)` row for row. A real sweep writes one receipt
(`storage.telemetry_retention`) with rows and bytes per table, and the last sweep is kept in the
`system_services` row the Storage card reads. It runs hourly as the `telemetry_retention` system
service under a session advisory lock (exactly one process sweeps), and the Infra Agent's storage
executor calls it early under storage pressure (tier 2 `storage.drop_partitions`, rule `telemetry`).

Deleted rows free space INSIDE Postgres (autovacuum reuses it, the table stops growing); only a
dropped partition hands the bytes back to the disk at once.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Optional, Sequence

from sqlalchemy import text

_log = logging.getLogger("boss.telemetry_retention")

AUDIT_FLOOR_DAYS = 30
DIAGNOSTIC_FLOOR_DAYS = 7
BATCH = 5_000
STATEMENT_TIMEOUT = "30s"
LOCK_TIMEOUT = "2s"
#: A swap waits at most this long for the ACCESS EXCLUSIVE lock on `events`.
SWAP_LOCK_TIMEOUT = "3s"
#: A sweep spends at most this long on one table; the rest waits for the next pass.
TABLE_BUDGET_SECONDS = 120.0
MONTHS_AHEAD = 2
INTERVAL_SECONDS = 3600.0
FIRST_PASS_DELAY = 300.0
LOCK_NAME = "boss.telemetry_retention"
#: Held for the whole events conversion, from before the legacy bound exists until the swap (or
#: the failure path) has dropped it. schema.sql's boot drop and `clear_stale_bound` take the same
#: key and leave the bound alone while a conversion holds it. hashtext, so both sides agree.
CONVERSION_LOCK = "boss.events_conversion"
RECEIPT_KIND = "storage.telemetry_retention"
SERVICE_KEY = "telemetry_retention"
#: The cutover month must be at least this far away when the legacy bound is set: the NOT VALID
#: check is enforced on inserts at once, and a swap that keeps failing must never reach it.
CUTOVER_MARGIN = timedelta(days=2)

#: Loop chatter: the record of the same thing lives elsewhere (receipt.created repeats the
#: receipt; the overwatch/drive completions repeat their tick rows). THE DELETE-LIST: an event
#: kind not named here (or matching TICK_EVENT_SQL) is never deleted row by row, and is archived
#: before its month partition drops — an unknown kind is treated as evidence.
NOISE_EVENT_TYPES: tuple[str, ...] = (
    "receipt.created", "metatron.drive.completed", "metatron.overwatch.completed",
    "metatron.command.staff", "metatron.command.loop", "captain.coordinated",
)
#: Ticks and heartbeats (resource_reaper pruned these at 14 days before ST-8).
TICK_EVENT_SQL = "(t.event_type LIKE '%.ticked' OR t.event_type = 'job.heartbeat')"
#: The ONLY receipt kinds retention may delete: the always-on loops' per-tick receipts.
TICK_RECEIPT_KINDS: tuple[str, ...] = (
    "ai_sre.overwatch", "metatron.watch", "metatron.overwatch", "process_manager.tick",
    "process_manager.advance_runs", "metatron.command.loop_playbook",
    "metatron.command.staff_captain", "knowledge.repository.index", "knowledge.embed",
)
#: Telemetry tables another sweep owns: counted in the share, never deleted here.
OWNED_ELSEWHERE: tuple[str, ...] = ("agent_turns", "metatron_watch_ticks", "process_manager_ticks")

_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")


def _lits(values: Sequence[str]) -> str:
    """A constant SQL text[] literal. Values are this module's own constants (checked)."""
    for v in values:
        if not re.fullmatch(r"[a-z0-9_.]+", v):
            raise ValueError(f"unsafe literal {v!r}")
    return "ARRAY[" + ", ".join(f"'{v}'" for v in values) + "]::text[]"


#: Protects EVERY row: what a protection that cannot be built safely falls back to.
FAIL_CLOSED = "true"


def _receipt_refs_protect(connection: Any) -> str:
    """`t` (a receipt) is referenced by any foreign key: read from the catalogue every sweep.

    FAILS CLOSED: a catalogue read that errors, a multi-column key, or a referencing table or
    column this function cannot quote safely makes the predicate `true` — every receipt is
    protected and nothing is deleted until the shape is understood."""
    try:
        rows = connection.execute(text("""
            SELECT n.nspname AS sch, r.relname AS tbl, c.conkey, c.confkey,
                   (SELECT a.attname FROM pg_attribute a
                     WHERE a.attrelid = c.conrelid AND a.attnum = c.conkey[1]) AS col
              FROM pg_constraint c
              JOIN pg_class r ON r.oid = c.conrelid
              JOIN pg_namespace n ON n.oid = r.relnamespace
             WHERE c.contype = 'f' AND c.confrelid = to_regclass('receipts')""")).all()
    except Exception:  # noqa: BLE001
        _log.warning("receipt references unreadable: every receipt kept", exc_info=True)
        return FAIL_CLOSED
    parts = []
    for sch, tbl, conkey, confkey, col in rows:
        if len(list(conkey or [])) != 1 or len(list(confkey or [])) != 1:
            _log.warning("receipt reference %s.%s has several columns: every receipt kept", sch, tbl)
            return FAIL_CLOSED
        if not (sch and tbl and col and _IDENT.match(str(sch)) and _IDENT.match(str(tbl))
                and _IDENT.match(str(col))):
            _log.warning("receipt reference %s.%s.%s cannot be quoted: every receipt kept", sch, tbl, col)
            return FAIL_CLOSED
        parts.append(f'EXISTS (SELECT 1 FROM "{sch}"."{tbl}" x WHERE x."{col}" = t.id)')
    return " OR ".join(parts) if parts else "false"


@dataclass(frozen=True)
class Policy:
    key: str
    table: str
    ts: str
    default_days: int
    floor_days: int
    audit: bool
    label: str
    #: Which rows this policy may delete at all (alias t); empty = every row.
    scope_sql: str = ""
    #: Rows never deleted (alias t): a constant predicate, or one built per sweep.
    protect_sql: str | Callable[[Any], str] = ""
    #: Monthly partitions: expired months are dropped, `archive_sql` rows copied first.
    partitioned: bool = False
    #: Rows of an expiring partition kept in `<table>_archive` before the drop (alias t).
    archive_sql: str = ""
    #: Rows per delete statement (smaller where every delete fires many FK checks).
    batch: int = 0

    def protect(self, connection: Any) -> str:
        p = self.protect_sql(connection) if callable(self.protect_sql) else self.protect_sql
        return p or "false"


_NOISE_EVENT = f"t.event_type = ANY({_lits(NOISE_EVENT_TYPES)})"
#: Every kind retention may delete from `events`. Anything else is kept / archived.
CHATTER_SQL = f"({_NOISE_EVENT} OR {TICK_EVENT_SQL})"

POLICIES: tuple[Policy, ...] = (
    Policy("events", "events", "created_at", 90, AUDIT_FLOOR_DAYS, True, "Events",
           scope_sql=CHATTER_SQL, partitioned=True, archive_sql=f"NOT {CHATTER_SQL}"),
    Policy("events_noise", "events", "created_at", 30, 14, False, "Loop events",
           scope_sql=_NOISE_EVENT),
    Policy("events_ticks", "events", "created_at", 14, DIAGNOSTIC_FLOOR_DAYS, False, "Tick events",
           scope_sql=TICK_EVENT_SQL),
    # Every receipt delete fires one RI check per referencing FK (19 on the laptop, none of
    # the referencing columns indexed; the largest, cost_receipts, ~4k rows). Measured on a
    # scratch DB with 50k tick receipts and a 4k-row unindexed referencer: 500 rows 1.3 s,
    # 1000 rows 1.5 s, 5000 rows 2.5 s per statement. 1000 keeps each statement well inside
    # STATEMENT_TIMEOUT with room for referencers ten times that size.
    Policy("receipts", "receipts", "created_at", 30, AUDIT_FLOOR_DAYS, True, "Tick receipts",
           scope_sql=(f"t.effect_kind = ANY({_lits(TICK_RECEIPT_KINDS)}) "
                      "AND t.status IN ('succeeded', 'failed')"),
           protect_sql=_receipt_refs_protect, batch=1_000),
    Policy("anomaly_signals", "anomaly_signals", "ts", 30, DIAGNOSTIC_FLOOR_DAYS, False,
           "Anomaly signals",
           protect_sql=("t.incident_id IS NOT NULL AND EXISTS (SELECT 1 FROM incidents i "
                        "WHERE i.id = t.incident_id AND i.status NOT IN ('resolved', 'dismissed'))")),
    Policy("app_metrics_scrapes", "app_metrics_scrapes", "scraped_at", 7, DIAGNOSTIC_FLOOR_DAYS,
           False, "Metrics scrapes"),
    Policy("deployment_health_samples", "deployment_health_samples", "checked_at", 30,
           DIAGNOSTIC_FLOOR_DAYS, False, "Health samples"),
    # BOSSAPP-1459: the append-only tables that had no rule at all (RETENTION_OWNERS below is
    # the census; test_a_running_install_stays_in_its_budget keeps it complete).
    Policy("focused_agent_ticks", "focused_agent_ticks", "created_at", 14, DIAGNOSTIC_FLOOR_DAYS,
           False, "Focused agent ticks"),
    Policy("worker_autoscaling_history", "worker_autoscaling_history", "created_at", 30,
           DIAGNOSTIC_FLOOR_DAYS, False, "Autoscaling history"),
    Policy("trace_export_attempts", "trace_export_attempts", "created_at", 30,
           DIAGNOSTIC_FLOOR_DAYS, False, "Trace exports"),
    Policy("a2a_transport_attempts", "a2a_transport_attempts", "created_at", 30,
           DIAGNOSTIC_FLOOR_DAYS, False, "Agent bus attempts"),
    Policy("policy_eval_log", "policy_eval_log", "created_at", 90, AUDIT_FLOOR_DAYS, True,
           "Policy checks"),
    # A sandbox's egress credential is a hash that stops working at expires_at; a week after
    # that it proves nothing.
    Policy("identity_egress_credentials", "identity_egress_credentials", "expires_at", 7,
           DIAGNOSTIC_FLOOR_DAYS, False, "Expired egress credentials"),
)
POLICY_BY_KEY = {p.key: p for p in POLICIES}

#: The timestamp index each batched table needs (name, table, column). `events` has
#: idx_events_created_id (created_at, id) from schema.sql.
TS_INDEXES: tuple[tuple[str, str, str], ...] = (
    ("idx_receipts_created", "receipts", "created_at"),
    ("idx_anomaly_signals_ts", "anomaly_signals", "ts"),
    ("idx_app_metrics_scrapes_at", "app_metrics_scrapes", "scraped_at"),
    ("idx_health_samples_checked", "deployment_health_samples", "checked_at"),
    ("idx_focused_agent_ticks_created", "focused_agent_ticks", "created_at"),
    ("idx_worker_autoscaling_history_created", "worker_autoscaling_history", "created_at"),
    ("idx_trace_export_attempts_created", "trace_export_attempts", "created_at"),
    ("idx_a2a_transport_attempts_created", "a2a_transport_attempts", "created_at"),
    ("idx_policy_eval_log_created", "policy_eval_log", "created_at"),
    ("idx_identity_egress_credentials_expires", "identity_egress_credentials", "expires_at"),
)

#: EVERY APPEND-ONLY TABLE HAS AN OWNER (BOSSAPP-1459). A table that only grows — an event log, a
#: tick, a sample, a signal, an attempt, an audit — is listed here with what bounds it: a policy
#: of this module (`POLICIES`), another module's retention, or the reason it is kept. The census
#: test reads db/schema.sql and fails on an append-only-shaped table missing from this map, so a
#: new one cannot ship without a decision.
RETENTION_OWNERS: dict[str, str] = {
    **{p.table: "telemetry_retention" for p in POLICIES},
    "events_archive": "kept: the non-chatter events of dropped months (audit evidence)",
    "agent_turns": "activity_retention",
    "app_logs": "activity_retention",
    "metatron_watch_ticks": "resource_reaper.prune_tables",
    "process_manager_ticks": "resource_reaper.prune_tables",
    "llm_calls": "ledger_retention",
    "tool_call_audit": "ledger_retention",
    "run_events": "ledger_retention (monthly partitions)",
    "trace_spans": "trace_payloads.sweep",
    "storage_samples": "storage_watch.prune",
    "identity_egress_log": "identity.hygiene (monthly partitions)",
    "intake_events": "event_intake (expires_at)",
    "identity_security_events": "kept: hash-chained security audit; a deleted row breaks the chain",
    "secret_access_events": "kept: who read which secret (audit)",
    "ip_allow_audit": "kept: allow-list decisions (audit)",
    "kg_audit": "kept: knowledge changes (audit)",
    "guest_calls": "kept: sandbox guest calls (audit)",
    "host_events": "kept: operator actions on hosts (audit)",
    "cost_events": "kept: spend (finance records)",
    "provider_usage_events": "kept: provider usage (finance records)",
    "routing_logs": "kept: referenced by route decisions (foreign key)",
    "delivery_presence_events": "kept: the dedupe ledger; a deleted row lets a notice post again",
    "github_reconciliation_signals": "kept: the reconciliation ledger of provider events",
    "regression_signals": "kept: incident evidence",
    "agent_chat_turns": "kept: people's conversations",
    "agent_conversation_turns": "kept: people's conversations",
    "builder_turns": "kept: people's conversations",
    "agent_deployment_history": "kept: which agent version ran when (records)",
    "shepherd_reasoning": "kept: the learning record of recoveries",
    "tm_heartbeats": "kept: one row per install per day (the collector's own data)",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(v: Any) -> Any:
    return v.isoformat() if isinstance(v, (datetime, date)) else v


# ── policy (settings + floor) ───────────────────────────────────────────────────────────────

def effective_days(policy: Policy, overrides: Optional[Mapping[str, Any]] = None) -> int:
    """The configured days for one policy, never below its floor."""
    raw = (overrides or {}).get(policy.key)
    try:
        asked = int(raw) if raw not in (None, "") else policy.default_days
    except (TypeError, ValueError):
        asked = policy.default_days
    return max(policy.floor_days, asked)


#: The single `keep_days` the telemetry rule shipped with before ST-8.
LEGACY_KEEP_DAYS_DEFAULT = 30
_WARNED_KEEP_DAYS: set[int] = set()


def overrides_from_rule(rule: Mapping[str, Any]) -> dict[str, Any]:
    """The per-policy overrides a `telemetry` rule asks for. `tables` wins; an old single
    `keep_days` an operator CHANGED from the shipped 30 still counts — for every policy without
    its own entry (each under its floor) — and says once that it should move to `tables`."""
    tables = dict(rule.get("tables") or {})
    raw = rule.get("keep_days")
    try:
        legacy = int(raw) if raw not in (None, "") else None
    except (TypeError, ValueError):
        legacy = None
    if legacy is not None and legacy != LEGACY_KEEP_DAYS_DEFAULT:
        if legacy not in _WARNED_KEEP_DAYS:
            _WARNED_KEEP_DAYS.add(legacy)
            _log.warning("telemetry rule keep_days=%s applies to every table without its own entry; "
                         "set per-table days in the rule's `tables` map instead", legacy)
        for p in POLICIES:
            tables.setdefault(p.key, legacy)
    return tables


def configured_days(connection: Any) -> dict[str, int]:
    """Per-policy days from the `telemetry` retention rule (install-wide), under the floors."""
    overrides: dict[str, Any] = {}
    try:
        row = connection.execute(text(
            "SELECT rule, enabled FROM retention_rules WHERE kind = 'telemetry' "
            "ORDER BY host_id NULLS FIRST LIMIT 1")).first()
        if row is not None and row[1]:
            rule = row[0] if isinstance(row[0], dict) else json.loads(row[0] or "{}")
            overrides = overrides_from_rule(rule)
    except Exception:  # noqa: BLE001 — no rules table: the defaults
        pass
    return {p.key: effective_days(p, overrides) for p in POLICIES}


# ── catalogue helpers ───────────────────────────────────────────────────────────────────────

def _exists(connection: Any, name: str) -> bool:
    return connection.execute(text("SELECT to_regclass(:t) IS NOT NULL"), {"t": name}).scalar() is True


def is_partitioned(connection: Any, table: str) -> bool:
    return connection.execute(text(
        "SELECT relkind = 'p' FROM pg_class WHERE oid = to_regclass(:t)"), {"t": table}).scalar() is True


_BOUND = re.compile(r"FOR VALUES FROM \((?P<lo>[^)]*)\) TO \((?P<hi>[^)]*)\)")


def _ts_literal(raw: str) -> Optional[datetime]:
    raw = raw.strip().strip("'")
    if raw.upper() in ("MINVALUE", "MAXVALUE"):
        return None
    try:
        v = datetime.fromisoformat(raw.replace(" ", "T"))
    except ValueError:
        return None
    return v if v.tzinfo else v.replace(tzinfo=timezone.utc)


def partitions(connection: Any, table: str) -> list[dict[str, Any]]:
    """[{name, lo, hi, default}] for a partitioned table, oldest first."""
    rows = connection.execute(text("""
        SELECT c.relname, pg_get_expr(c.relpartbound, c.oid) AS bound
          FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid
         WHERE i.inhparent = to_regclass(:t)"""), {"t": table}).all()
    out = []
    for name, bound in rows:
        b = str(bound or "")
        if b.strip().upper() == "DEFAULT":
            out.append({"name": str(name), "lo": None, "hi": None, "default": True})
            continue
        m = _BOUND.search(b)
        if not m:
            continue
        out.append({"name": str(name), "lo": _ts_literal(m.group("lo")),
                    "hi": _ts_literal(m.group("hi")), "default": False})
    far = datetime.max.replace(tzinfo=timezone.utc)
    return sorted(out, key=lambda p: (p["default"], p["hi"] or far))


def _month_start(d: datetime) -> datetime:
    return datetime(d.year, d.month, 1, tzinfo=timezone.utc)


def _add_months(d: datetime, n: int) -> datetime:
    y, m = d.year + (d.month - 1 + n) // 12, (d.month - 1 + n) % 12 + 1
    return datetime(y, m, 1, tzinfo=timezone.utc)


def _autocommit(engine: Any):
    return engine.connect().execution_options(isolation_level="AUTOCOMMIT")


# ── indexes (CONCURRENTLY, never at boot) ───────────────────────────────────────────────────

#: After a build that failed (a lock wait, a timeout), wait this long before the next try —
#: never a CONCURRENTLY rebuild of the same index every hour.
INDEX_RETRY_AFTER = timedelta(hours=24)
_INDEX_BACKOFF: dict[str, datetime] = {}


def ensure_indexes(engine: Any, *, now: Optional[datetime] = None) -> list[str]:
    """Build each missing timestamp index CONCURRENTLY (writers keep writing). An INVALID one a
    crashed build left behind is dropped and built again. A build that fails backs off for
    INDEX_RETRY_AFTER. Returns the indexes built."""
    now = now or _now()
    made: list[str] = []
    with _autocommit(engine) as c:
        c.execute(text(f"SET lock_timeout = '{LOCK_TIMEOUT}'"))
        for name, table, column in TS_INDEXES:
            if _INDEX_BACKOFF.get(name) and _INDEX_BACKOFF[name] > now:
                continue
            try:
                if not _exists(c, table):
                    continue
                valid = c.execute(text(
                    "SELECT i.indisvalid FROM pg_index i WHERE i.indexrelid = to_regclass(:n)"),
                    {"n": name}).scalar()
                if valid is True:
                    continue
                if valid is False:
                    c.execute(text(f'DROP INDEX CONCURRENTLY IF EXISTS "{name}"'))
                c.execute(text(f'CREATE INDEX CONCURRENTLY IF NOT EXISTS "{name}" ON "{table}" ("{column}")'))
                made.append(name)
                _INDEX_BACKOFF.pop(name, None)
            except Exception:  # noqa: BLE001 — the sweep still runs; retried after the back-off
                _INDEX_BACKOFF[name] = now + INDEX_RETRY_AFTER
                _log.info("telemetry index %s not built; retried after %s", name, INDEX_RETRY_AFTER,
                          exc_info=True)
        c.execute(text("RESET lock_timeout"))
    return made


# ── the events conversion (online, resumable, idempotent) ───────────────────────────────────

LEGACY = "events_legacy"
DEFAULT_PART = "events_default"
_LEGACY_CHECK = "events_legacy_bound"
_HELPER_INDEX = "events_id_created_key"


def _cutover(now: datetime) -> datetime:
    nxt = _add_months(_month_start(now), 1)
    return nxt if nxt - now >= CUTOVER_MARGIN else _add_months(nxt, 1)


class _ConversionGuard:
    """Session advisory lock held across the conversion's several transactions.

    `pg_try_advisory_lock` so a boot or a second pass that cannot get it leaves the bound alone
    instead of waiting out the index build. The connection stays checked out: the lock is
    session-level and dies with it."""

    def __init__(self, engine: Any):
        self.engine, self.conn, self.held = engine, None, False

    def __enter__(self) -> bool:
        self.conn = self.engine.connect()
        try:
            self.held = bool(self.conn.execute(
                text("SELECT pg_try_advisory_lock(hashtext(:k))"), {"k": CONVERSION_LOCK}).scalar())
            self.conn.commit()
        except Exception:
            self.conn.close()
            self.conn = None
            raise
        return self.held

    def __exit__(self, *exc: Any) -> None:
        try:
            if self.held and self.conn is not None:
                self.conn.execute(text("SELECT pg_advisory_unlock(hashtext(:k))"),
                                  {"k": CONVERSION_LOCK})
                self.conn.commit()
        finally:
            if self.conn is not None:
                self.conn.close()


def _drop_stale_bound_if_plain(engine: Any) -> bool:
    """Drop `events_legacy_bound` from a plain `events`. Caller holds CONVERSION_LOCK, or has
    decided no conversion is in flight. True when one was there. Never raises (logged)."""
    try:
        with engine.connect() as c:
            plain = c.execute(text(
                "SELECT relkind = 'r' FROM pg_class WHERE oid = to_regclass('events')")).scalar() is True
            there = plain and c.execute(text(
                "SELECT count(*) FROM pg_constraint WHERE conname = :n "
                "AND conrelid = to_regclass('events')"), {"n": _LEGACY_CHECK}).scalar()
        if not there:
            return False
        _log.warning("events carried a stale %s from an interrupted conversion; dropping it", _LEGACY_CHECK)
        return _drop_legacy_check(engine)
    except Exception:  # noqa: BLE001
        _log.warning("stale events bound not checked", exc_info=True)
        return False


def clear_stale_bound(engine: Any) -> bool:
    """Drop `events_legacy_bound` from a plain `events` when no conversion holds it.

    A conversion in flight holds CONVERSION_LOCK the whole time the check exists. This refuses
    while that lock is held (the swap still needs the check). True when one was dropped. Never
    raises (logged)."""
    try:
        with _ConversionGuard(engine) as held:
            if not held:
                return False
            return _drop_stale_bound_if_plain(engine)
    except Exception:  # noqa: BLE001
        _log.warning("stale events bound not checked", exc_info=True)
        return False


def _drop_legacy_check(engine: Any) -> bool:
    try:
        with engine.begin() as c:
            c.execute(text(f"SET LOCAL lock_timeout = '{SWAP_LOCK_TIMEOUT}'"))
            c.execute(text(f'ALTER TABLE events DROP CONSTRAINT IF EXISTS "{_LEGACY_CHECK}"'))
        return True
    except Exception:  # noqa: BLE001
        _log.warning("events legacy check not dropped; the next pass retries", exc_info=True)
        return False


def ensure_partitioned(engine: Any, *, now: Optional[datetime] = None) -> dict[str, Any]:
    """Convert a plain `events` into a monthly-partitioned one without copying a row; make the
    month partitions ahead. Safe to call every pass: an already-partitioned table only gets its
    next months. Never raises; a step that cannot get its lock in time is retried next pass."""
    now = now or _now()
    out: dict[str, Any] = {"converted": False, "made": [], "seconds": {}}
    # The lock covers the stale-bound drop and the conversion. Boot (schema.sql) and
    # clear_stale_bound take the same key and will not drop a check this pass still needs.
    with _ConversionGuard(engine) as held:
        if not held:
            out["skipped"] = "conversion busy"
            try:
                with engine.connect() as c:
                    if _exists(c, "events") and is_partitioned(c, "events"):
                        out["made"] = ensure_months(engine, now=now)
            except Exception as exc:  # noqa: BLE001
                _log.warning("events partitioning not done", exc_info=True)
                out["error"] = f"{type(exc).__name__}: {exc}"[:300]
            return out
        # FIRST, before anything that can fail: a bound a crashed pass left on the PLAIN table
        # goes. Left there, every insert after its cutover would fail. (schema.sql drops it at
        # boot too, unless a conversion holds the lock.)
        out["stale_bound_dropped"] = _drop_stale_bound_if_plain(engine)
        try:
            with engine.connect() as c:
                if not _exists(c, "events"):
                    return out
                already = is_partitioned(c, "events")
            if not already:
                out.update(_convert_events(engine, now=now))
            with engine.connect() as c:
                if not is_partitioned(c, "events"):
                    return out
            out["made"] = ensure_months(engine, now=now)
        except Exception as exc:  # noqa: BLE001
            _log.warning("events partitioning not done", exc_info=True)
            out["error"] = f"{type(exc).__name__}: {exc}"[:300]
        return out


def _convert_events(engine: Any, *, now: datetime) -> dict[str, Any]:
    t: dict[str, float] = {}
    # 1. The unique (id, created_at) index the partitioned primary key needs, built without
    #    blocking writers. An INVALID leftover is rebuilt.
    t0 = time.monotonic()
    with _autocommit(engine) as c:
        c.execute(text(f"SET lock_timeout = '{SWAP_LOCK_TIMEOUT}'"))
        valid = c.execute(text("SELECT indisvalid FROM pg_index WHERE indexrelid = to_regclass(:n)"),
                          {"n": _HELPER_INDEX}).scalar()
        if valid is False:
            c.execute(text(f'DROP INDEX CONCURRENTLY IF EXISTS "{_HELPER_INDEX}"'))
        if valid is not True:
            c.execute(text(f'CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS "{_HELPER_INDEX}" '
                           "ON events (id, created_at)"))
    t["unique_index"] = round(time.monotonic() - t0, 2)

    # 2. The bound the legacy partition will cover: everything before the cutover month (at least
    #    CUTOVER_MARGIN away). Added NOT VALID (no scan, a brief lock) and validated (a scan that
    #    blocks no writer). Always fresh: ensure_partitioned dropped any older one first.
    cutover = _cutover(now)
    t0 = time.monotonic()
    try:
        with engine.begin() as c:
            c.execute(text(f"SET LOCAL lock_timeout = '{SWAP_LOCK_TIMEOUT}'"))
            c.execute(text(f'ALTER TABLE events DROP CONSTRAINT IF EXISTS "{_LEGACY_CHECK}"'))
            c.execute(text(f'ALTER TABLE events ADD CONSTRAINT "{_LEGACY_CHECK}" '
                           f"CHECK (created_at < '{cutover.isoformat()}') NOT VALID"))
        with engine.begin() as c:
            c.execute(text("SET LOCAL statement_timeout = 0"))
            c.execute(text(f"SET LOCAL lock_timeout = '{SWAP_LOCK_TIMEOUT}'"))
            c.execute(text(f'ALTER TABLE events VALIDATE CONSTRAINT "{_LEGACY_CHECK}"'))
        t["validate"] = round(time.monotonic() - t0, 2)

        # 3. The swap: one short transaction, catalogue work only.
        t0 = time.monotonic()
        with engine.begin() as c:
            _swap(c, cutover=cutover, now=now)
        t["swap"] = round(time.monotonic() - t0, 2)
    except Exception:
        # Never leave the bound behind on a plain table: inserts past it would fail.
        _drop_legacy_check(engine)
        raise
    _log.info("events converted to monthly partitions (cutover %s): %s", cutover.date(), t)
    return {"converted": True, "cutover": cutover.isoformat(), "seconds": t}


def _swap(c: Any, *, cutover: datetime, now: datetime) -> None:
    c.execute(text(f"SET LOCAL lock_timeout = '{SWAP_LOCK_TIMEOUT}'"))
    c.execute(text("SET LOCAL statement_timeout = '60s'"))
    c.execute(text("LOCK TABLE events IN ACCESS EXCLUSIVE MODE"))
    if is_partitioned(c, "events"):
        return  # another process won the race
    # A CREATE INDEX CONCURRENTLY that crashed leaves indisvalid false. Attaching that index
    # makes the parent's index invalid too, and schema.sql's CREATE INDEX IF NOT EXISTS never
    # rebuilds a name that already exists. Drop it here (we already hold ACCESS EXCLUSIVE) so
    # the name is free for a later valid build. Constraint indexes are not this list.
    invalid = c.execute(text("""
        SELECT ic.relname
          FROM pg_index i JOIN pg_class ic ON ic.oid = i.indexrelid
         WHERE i.indrelid = 'events'::regclass
           AND NOT i.indisvalid
           AND NOT EXISTS (SELECT 1 FROM pg_constraint k WHERE k.conindid = i.indexrelid)
           AND ic.relname <> :helper"""), {"helper": _HELPER_INDEX}).all()
    for (name,) in invalid:
        c.execute(text(f'DROP INDEX "{name}"'))
    # What the new parent must carry: every plain index (constraint indexes aside), every
    # user trigger, and the foreign keys that point at events(id) — dropped, see below.
    indexes = c.execute(text("""
        SELECT ic.relname, pg_get_indexdef(i.indexrelid)
          FROM pg_index i JOIN pg_class ic ON ic.oid = i.indexrelid
         WHERE i.indrelid = 'events'::regclass
           AND i.indisvalid
           AND NOT EXISTS (SELECT 1 FROM pg_constraint k WHERE k.conindid = i.indexrelid)
           AND ic.relname <> :helper"""), {"helper": _HELPER_INDEX}).all()
    triggers = c.execute(text(
        "SELECT tgname, pg_get_triggerdef(oid) FROM pg_trigger "
        "WHERE tgrelid = 'events'::regclass AND NOT tgisinternal")).all()
    fks = c.execute(text(
        "SELECT conrelid::regclass::text, conname FROM pg_constraint "
        "WHERE contype = 'f' AND confrelid = 'events'::regclass")).all()
    # The keys events itself holds (events.project_id → projects): re-declared on the parent so
    # every new month keeps them. The legacy table's identical key is ATTACHED, not re-checked.
    own_fks = c.execute(text(
        "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE contype = 'f' AND conrelid = 'events'::regclass")).all()
    # A partitioned table can only be referenced through its whole key (id, created_at). The two
    # references (webhook_deliveries/inbound_webhooks.event_id, ON DELETE SET NULL) become soft
    # references: the column stays, the readers already LEFT JOIN.
    for tbl, name in fks:
        c.execute(text(f'ALTER TABLE {tbl} DROP CONSTRAINT IF EXISTS "{name}"'))
    for name, _def in triggers:
        c.execute(text(f'DROP TRIGGER IF EXISTS "{name}" ON events'))
    c.execute(text(f'ALTER TABLE events RENAME TO "{LEGACY}"'))
    # The partition's primary key must match the parent's (id, created_at): the old key on `id`
    # gives way to the unique index built CONCURRENTLY in step 1 — catalogue work, no scan (both
    # columns are NOT NULL already).
    pk = c.execute(text(
        "SELECT conname FROM pg_constraint WHERE conrelid = to_regclass(:l) AND contype = 'p'"),
        {"l": LEGACY}).scalar()
    if pk:
        c.execute(text(f'ALTER TABLE "{LEGACY}" DROP CONSTRAINT "{pk}"'))
    c.execute(text(f'ALTER TABLE "{LEGACY}" ADD CONSTRAINT "{LEGACY}_pkey" '
                   f'PRIMARY KEY USING INDEX "{_HELPER_INDEX}"'))
    for name, _def in indexes:
        c.execute(text(f'ALTER INDEX "{name}" RENAME TO "{_legacy_index_name(name)}"'))
    c.execute(text(f'CREATE TABLE events (LIKE "{LEGACY}" INCLUDING DEFAULTS) '
                   "PARTITION BY RANGE (created_at)"))
    c.execute(text("ALTER TABLE events ADD CONSTRAINT events_pkey PRIMARY KEY (id, created_at)"))
    # The validated check proves the bound: no scan. The (id, created_at) unique index is
    # attached as the partition's piece of the primary key: no build.
    c.execute(text(f'ALTER TABLE events ATTACH PARTITION "{LEGACY}" '
                   f"FOR VALUES FROM (MINVALUE) TO ('{cutover.isoformat()}')"))
    # Same names, same definitions on the parent, made ON ONLY the parent (no recursion, no
    # build) and the legacy index ATTACHED to each. A plain CREATE INDEX on the parent rebuilt the
    # GIN full-text index under the lock (5.9 s on a 1 GB table); an index that does not match
    # now fails the swap (rolled back, retried next pass) instead of building silently.
    for name, idef in indexes:
        c.execute(text(_parent_index_sql(str(name), str(idef))))
        c.execute(text(f'ALTER INDEX "{name}" ATTACH PARTITION "{_legacy_index_name(str(name))}"'))
    for name, cdef in own_fks:
        c.execute(text(f'ALTER TABLE events ADD CONSTRAINT "{name}" {cdef}'))
    for _name, tdef in triggers:
        c.execute(text(re.sub(r"\bON (public\.)?events_legacy\b|\bON (public\.)?events\b", "ON events",
                              str(tdef), count=1)))
    c.execute(text(f'CREATE TABLE IF NOT EXISTS "{DEFAULT_PART}" PARTITION OF events DEFAULT'))
    _make_months(c, start=cutover, until=_add_months(_month_start(now), MONTHS_AHEAD + 1))
    c.execute(text(f'ALTER TABLE "{LEGACY}" DROP CONSTRAINT IF EXISTS "{_LEGACY_CHECK}"'))


def _legacy_index_name(name: str) -> str:
    base = name.replace("events", "events_legacy", 1) if "events" in name else f"{name}_legacy"
    return base[:63]


def _parent_index_sql(name: str, indexdef: str) -> str:
    """`CREATE INDEX <legacy name> ON public.events_legacy USING …` (as read before the
    rename, so it still names `events`) → the same index, original name, on the parent."""
    # Never normalise whitespace: it sits inside string literals too (translate(…, '._-', '   ')
    # collapsed to ' ' built a DIFFERENT index, which could not be attached).
    m = re.match(r"^CREATE (UNIQUE )?INDEX \S+ ON (?:ONLY )?\S+ (USING .*)$", indexdef.strip(), re.S)
    if not m:
        raise ValueError(f"unexpected index definition: {indexdef[:120]}")
    return f'CREATE {m.group(1) or ""}INDEX IF NOT EXISTS "{name}" ON ONLY events {m.group(2)}'


def _make_months(c: Any, *, start: datetime, until: datetime) -> list[str]:
    made = []
    month = _month_start(start)
    if month < start:
        month = _add_months(month, 1)
    while month < until:
        nxt = _add_months(month, 1)
        name = f"events_{month:%Y_%m}"
        if not _exists(c, name):
            c.execute(text(f'CREATE TABLE "{name}" PARTITION OF events FOR VALUES '
                           f"FROM ('{month.isoformat()}') TO ('{nxt.isoformat()}')"))
            made.append(name)
        month = nxt
    return made


def ensure_months(engine: Any, *, now: Optional[datetime] = None) -> list[str]:
    """This month and the next MONTHS_AHEAD, after the newest bound that exists. A month whose
    rows already sit in the default partition stays there (said in the log, never an error)."""
    now = now or _now()
    made: list[str] = []
    with engine.connect() as c:
        parts = [p for p in partitions(c, "events") if not p["default"]]
    newest = max((p["hi"] for p in parts if p["hi"] is not None), default=None)
    start = max(newest, _month_start(now)) if newest else _month_start(now)
    until = _add_months(_month_start(now), MONTHS_AHEAD + 1)
    month = start
    while month < until:
        try:
            with engine.begin() as c:
                c.execute(text(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'"))
                if not _exists(c, DEFAULT_PART):
                    c.execute(text(f'CREATE TABLE "{DEFAULT_PART}" PARTITION OF events DEFAULT'))
                made += _make_months(c, start=month, until=_add_months(month, 1))
        except Exception:  # noqa: BLE001 — default holds rows of that month, or a lock wait
            _log.info("events partition for %s not made", month.date(), exc_info=True)
        month = _add_months(month, 1)
    return made


# ── the sweep ───────────────────────────────────────────────────────────────────────────────

@dataclass
class TableResult:
    policy: str
    table: str
    days: int
    cut: str
    rows: int = 0
    bytes: int = 0
    partitions: list[str] = field(default_factory=list)
    archived: int = 0
    #: Of `rows`, those removed by batched deletes (the rest went with dropped partitions).
    batched: int = 0
    done: bool = True
    error: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        return {"policy": self.policy, "table": self.table, "days": self.days, "cut": self.cut,
                "rows": self.rows, "bytes": self.bytes, "partitions": list(self.partitions),
                "archived": self.archived, "batched": self.batched, "done": self.done, "error": self.error}


def _expired_partitions(connection: Any, policy: Policy, cut: datetime) -> list[dict[str, Any]]:
    if not policy.partitioned or not is_partitioned(connection, policy.table):
        return []
    return [p for p in partitions(connection, policy.table)
            if not p["default"] and p["hi"] is not None and p["hi"] <= cut]


def _archive_columns(connection: Any) -> list[str]:
    rows = connection.execute(text("""
        SELECT a.column_name FROM information_schema.columns a
          JOIN information_schema.columns b ON b.column_name = a.column_name
               AND b.table_schema = a.table_schema AND b.table_name = 'events_archive'
         WHERE a.table_schema = current_schema() AND a.table_name = 'events'
         ORDER BY a.ordinal_position""")).all()
    return [str(r[0]) for r in rows if _IDENT.match(str(r[0]))]


def _predicates(connection: Any, policies: Sequence[Policy], days: Mapping[str, int],
                now: datetime) -> dict[str, tuple[str, datetime]]:
    """policy key → (the delete predicate on alias t, its cut-off). A policy never re-counts rows
    an EARLIER policy on the same table deletes, so a dry run adds up exactly like the real run."""
    out: dict[str, tuple[str, datetime]] = {}
    earlier: dict[str, list[str]] = {}
    for p in policies:
        cut = now - timedelta(days=days[p.key])
        own = [f't."{p.ts}" < :cut_{p.key}']
        if p.scope_sql:
            own.append(f"({p.scope_sql})")
        own.append(f"NOT ({p.protect(connection)})")
        pred = " AND ".join(own)
        full = " AND ".join([pred] + [f"NOT ({e})" for e in earlier.get(p.table, [])])
        out[p.key] = (full, cut)
        earlier.setdefault(p.table, []).append(pred)
    return out


def _params(preds: Mapping[str, tuple[str, datetime]]) -> dict[str, Any]:
    return {f"cut_{k}": v[1] for k, v in preds.items()}


def plan(connection: Any, *, now: Optional[datetime] = None,
         days: Optional[Mapping[str, int]] = None,
         only: Optional[Sequence[str]] = None) -> list[TableResult]:
    """THE DRY RUN: exactly what `sweep` removes at `now` — partitions first, then rows (rows
    in a partition that drops are not counted twice). Read-only."""
    now = now or _now()
    days = dict(days or configured_days(connection))
    live = [p for p in POLICIES if _exists(connection, p.table)]
    preds = _predicates(connection, live, days, now)
    params = _params(preds)
    results: list[TableResult] = []
    dropping: dict[str, list[str]] = {}
    for p in live:
        pred, cut = preds[p.key]
        r = TableResult(p.key, p.table, days[p.key], cut.isoformat())
        for part in _expired_partitions(connection, p, cut):
            if only is not None and f"partition:{part['name']}" not in only:
                continue
            r.partitions.append(part["name"])
            r.bytes += int(connection.execute(text("SELECT pg_total_relation_size(:t)"),
                                              {"t": part["name"]}).scalar() or 0)
            keep = p.archive_sql or "false"
            got = connection.execute(text(
                f'SELECT count(*) FILTER (WHERE NOT ({keep})), '
                f'count(*) FILTER (WHERE {keep}) FROM "{part["name"]}" t')).first()
            r.rows += int(got[0] or 0)
            r.archived += int(got[1] or 0)
        dropping.setdefault(p.table, []).extend(r.partitions)
        if only is None or f"rows:{p.key}" in only:
            skip = dropping.get(p.table) or []
            extra = " AND t.tableoid::regclass::text <> ALL(:skip)" if skip else ""
            got = connection.execute(text(
                f'SELECT count(*), COALESCE(sum(pg_column_size(ROW(t.*))), 0) FROM "{p.table}" t '
                f"WHERE {pred}{extra}"), {**params, "skip": skip}).first()
            r.rows += int(got[0] or 0)
            r.batched += int(got[0] or 0)
            r.bytes += int(got[1] or 0)
        results.append(r)
    return results


def items_of(results: Sequence[TableResult]) -> list[str]:
    """The stable item list the storage executor's plan_hash binds (no counts: those move)."""
    out: list[str] = []
    for r in results:
        out += [f"partition:{n}" for n in r.partitions]
        if r.batched:
            out.append(f"rows:{r.policy}")
    return sorted(set(out))


def sweep(engine: Any, *, now: Optional[datetime] = None, dry_run: bool = False,
          days: Optional[Mapping[str, int]] = None, only: Optional[Sequence[str]] = None,
          budget_seconds: float = TABLE_BUDGET_SECONDS, batch: Optional[int] = None,
          receipt: bool = True) -> dict[str, Any]:
    """One pass. `only` (the executor's dry-run items) limits a real run to exactly those.
    Returns {at, dry_run, tables: [...], rows, bytes, partitions, receipt_id}."""
    now = now or _now()
    with engine.connect() as c:
        days = dict(days or configured_days(c))
        if dry_run:
            results = plan(c, now=now, days=days, only=only)
            return _report(results, now=now, dry_run=True)
        live = [p for p in POLICIES if _exists(c, p.table)]
        preds = _predicates(c, live, days, now)
        archive_cols = _archive_columns(c) if _exists(c, "events_archive") else []
        expired = {p.key: _expired_partitions(c, p, preds[p.key][1]) for p in live}
    params = _params(preds)
    results: list[TableResult] = []
    for p in live:
        pred, cut = preds[p.key]
        r = TableResult(p.key, p.table, days[p.key], cut.isoformat())
        try:
            for part in expired.get(p.key) or []:
                if only is not None and f"partition:{part['name']}" not in only:
                    continue
                _drop_partition(engine, p, part["name"], p.archive_sql or "false", archive_cols, r)
            if only is None or f"rows:{p.key}" in only:
                _delete_rows(engine, p, pred, params, r, budget_seconds=budget_seconds,
                             batch=int(batch or p.batch or BATCH))
        except Exception as exc:  # noqa: BLE001 — one table never stops the others
            _log.warning("telemetry retention: %s not finished", p.key, exc_info=True)
            r.error = f"{type(exc).__name__}: {exc}"[:300]
            r.done = False
        results.append(r)
    report = _report(results, now=now, dry_run=False)
    if receipt and (report["rows"] or report["partitions"] or report["errors"]):
        report["receipt_id"] = _write_receipt(engine, report)
    return report


def _drop_partition(engine: Any, p: Policy, name: str, protect: str, cols: Sequence[str],
                    r: TableResult) -> None:
    with engine.begin() as c:
        c.execute(text(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'"))
        c.execute(text("SET LOCAL statement_timeout = 0"))
        size = int(c.execute(text("SELECT pg_total_relation_size(:t)"), {"t": name}).scalar() or 0)
        total = int(c.execute(text(f'SELECT count(*) FROM "{name}"')).scalar() or 0)
        archived = 0
        if protect != "false":
            if not cols:
                raise RuntimeError("events_archive is missing: kept rows would be lost")
            collist = ", ".join(f'"{x}"' for x in cols)
            selected = ", ".join(f't."{x}"' for x in cols)
            archived = int(c.execute(text(
                f"INSERT INTO events_archive ({collist}) SELECT {selected} "
                f'FROM "{name}" t WHERE {protect} ON CONFLICT DO NOTHING')).rowcount or 0)
            # Every protected row must be in the archive before the month goes.
            missing = int(c.execute(text(
                f'SELECT count(*) FROM "{name}" t WHERE {protect} AND NOT EXISTS '
                f"(SELECT 1 FROM events_archive a WHERE a.id = t.id)")).scalar() or 0)
            if missing:
                raise RuntimeError(f"{missing} kept rows of {name} not archived; not dropped")
            protected = int(c.execute(text(f'SELECT count(*) FROM "{name}" t WHERE {protect}')).scalar() or 0)
        else:
            protected = 0
        c.execute(text(f'DROP TABLE "{name}"'))
    r.partitions.append(name)
    r.rows += total - protected
    r.archived += protected
    r.bytes += size
    _log.info("telemetry retention: dropped %s (%d rows, %d archived, %d bytes)", name, total,
              archived, size)


def _delete_rows(engine: Any, p: Policy, pred: str, params: Mapping[str, Any], r: TableResult, *,
                 budget_seconds: float, batch: int) -> None:
    started = time.monotonic()
    # Lock candidates, then delete in a SECOND statement. One statement shares a snapshot, so the
    # re-check could not see a receipt reference that committed after the snapshot and before the
    # row lock (FOR UPDATE does not re-run that subquery when the receipt row itself is unchanged).
    # The delete's statement takes a new snapshot; the row lock is held across it, so a reference
    # that landed in between is kept and one that is still inserting waits on the lock.
    lock_sql = text(f"""
        INSERT INTO _boss_tr_batch (id, ts, sz)
        SELECT t.id, t."{p.ts}", pg_column_size(ROW(t.*))
          FROM "{p.table}" t WHERE {pred} LIMIT :batch FOR UPDATE OF t SKIP LOCKED""")
    delete_sql = text(f"""
        WITH gone AS (
            DELETE FROM "{p.table}" t
              USING _boss_tr_batch v
             WHERE t.id = v.id AND t."{p.ts}" = v.ts AND {pred}
            RETURNING v.sz)
        SELECT count(*), COALESCE(sum(gone.sz), 0) FROM gone""")
    while True:
        with engine.begin() as c:
            c.execute(text(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'"))
            c.execute(text(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'"))
            c.execute(text("CREATE TEMP TABLE _boss_tr_batch "
                           "(id text, ts timestamptz, sz bigint) ON COMMIT DROP"))
            c.execute(lock_sql, {**params, "batch": int(batch)})
            got = c.execute(delete_sql, params).first()
        n, b = int(got[0] or 0), int(got[1] or 0)
        r.rows += n
        r.batched += n
        r.bytes += b
        if n < batch:
            return
        if time.monotonic() - started > budget_seconds:
            r.done = False   # the rest next pass
            return


def _report(results: Sequence[TableResult], *, now: datetime, dry_run: bool) -> dict[str, Any]:
    tables = [x.as_dict() for x in results]
    return {"at": now.isoformat(), "dry_run": dry_run, "tables": tables,
            "rows": sum(x.rows for x in results), "bytes": sum(x.bytes for x in results),
            "partitions": sorted(n for x in results for n in x.partitions),
            "archived": sum(x.archived for x in results),
            "errors": [f"{x.policy}: {x.error}" for x in results if x.error],
            "items": items_of(results), "receipt_id": None}


def _write_receipt(engine: Any, report: Mapping[str, Any]) -> Optional[str]:
    try:
        from .receipts import ReceiptService
        from .core import make_id

        with engine.begin() as c:
            row = c.execute(text(
                "SELECT p.id, p.workspace_id, w.org_id FROM projects p "
                "JOIN workspaces w ON w.id = p.workspace_id ORDER BY p.created_at LIMIT 1")).first()
            if row is None:
                return None
            payload = {k: report[k] for k in ("at", "rows", "bytes", "partitions", "archived",
                                              "errors")}
            payload["tables"] = [{k: t[k] for k in ("policy", "rows", "bytes", "partitions",
                                                    "archived", "days", "done")}
                                 for t in report["tables"]]
            rec, _ = ReceiptService().record_completed(
                c, str(row[2]), str(row[1]), str(row[0]), f"telemetry_retention:{make_id('sweep')}",
                RECEIPT_KIND, {"tool": "telemetry_retention"}, payload)
            return str(rec["id"])
    except Exception:  # noqa: BLE001 — a missing receipt never undoes a sweep
        _log.info("telemetry retention receipt not written", exc_info=True)
        return None


# ── the share the Storage card shows ────────────────────────────────────────────────────────

TELEMETRY_TABLES: tuple[str, ...] = tuple(dict.fromkeys(
    [p.table for p in POLICIES] + list(OWNED_ELSEWHERE) + ["events_archive"]))


def share(connection: Any) -> dict[str, Any]:
    """Telemetry bytes per table (partitions summed), their share of this database, and the last
    sweep. Catalogue reads only."""
    rows = []
    total = 0
    days = configured_days(connection)
    for t in TELEMETRY_TABLES:
        try:
            if not _exists(connection, t):
                continue
            b = int(connection.execute(text(
                "SELECT COALESCE(sum(pg_total_relation_size(relid)), 0) FROM pg_partition_tree(:t)"),
                {"t": t}).scalar() or 0)
        except Exception:  # noqa: BLE001
            continue
        total += b
        keep = next((days[p.key] for p in POLICIES if p.table == t and p.key == t), None)
        rows.append({"table": t, "bytes": b, "gb": round(b / 1e9, 3), "keep_days": keep,
                     "partitioned": is_partitioned(connection, t)})
    try:
        db = int(connection.execute(text("SELECT pg_database_size(current_database())")).scalar() or 0)
    except Exception:  # noqa: BLE001
        db = 0
    last = None
    try:
        got = connection.execute(text(
            "SELECT details -> 'last_sweep' FROM system_services WHERE key = :k"), {"k": SERVICE_KEY}).scalar()
        last = got if isinstance(got, dict) else (json.loads(got) if got else None)
    except Exception:  # noqa: BLE001
        last = None
    return {"bytes": total, "gb": round(total / 1e9, 3), "db_bytes": db,
            "share_pct": round(100.0 * total / db, 1) if db else None,
            "tables": sorted(rows, key=lambda r: -r["bytes"]), "last_sweep": last}


# ── compaction: a table whose file stayed big after its rows went (BOSSAPP-1459) ─────────────

#: Deleted rows free space INSIDE a table; the file keeps its size. A table that once churned
#: (kg_vec_1536 rewrote every vector on every embedding write before that was fixed) can hold
#: almost nothing and still fill the disk: 924 MB for 145 rows on the operator's laptop. Such a
#: table is rewritten (VACUUM FULL) when ALL hold — small live data, so the rewrite takes seconds
#: and the lock it needs is short; a big file; and the live data a small share of it:
COMPACT_MIN_BYTES = 256 * 1024 * 1024
COMPACT_MAX_LIVE_ROWS = 50_000
COMPACT_MAX_LIVE_SHARE = 0.10
COMPACT_STATEMENT_TIMEOUT = "120s"
#: The rewrite copies the live data and rebuilds every index (an HNSW one included): above this
#: much live data it could hold its lock for the whole timeout, so it is not attempted.
COMPACT_MAX_LIVE_BYTES = 64 * 1024 * 1024
#: A table whose rewrite failed, or did not halve it, is left alone this long.
COMPACT_RETRY_AFTER = timedelta(hours=24)
_COMPACT_BACKOFF: dict[str, datetime] = {}


def compaction_candidates(connection: Any) -> list[dict[str, Any]]:
    """Ordinary public tables (never a partition or a partitioned parent) that pass every
    COMPACT_* bound. Statistics only pre-select (they can be stale: kg_vec_1536 was never analysed
    and said 145 rows for 1,599); the live figure is counted, at most COMPACT_MAX_LIVE_ROWS + 1
    rows read."""
    rows = connection.execute(text("""
        SELECT c.relname, pg_total_relation_size(c.oid) AS bytes
          FROM pg_class c
          JOIN pg_namespace n ON n.oid = c.relnamespace
          JOIN pg_stat_user_tables s ON s.relid = c.oid
         WHERE n.nspname = 'public' AND c.relkind = 'r' AND NOT c.relispartition
           AND pg_total_relation_size(c.oid) >= :min
           AND s.n_live_tup <= :max_rows
         ORDER BY 2 DESC"""), {"min": COMPACT_MIN_BYTES, "max_rows": COMPACT_MAX_LIVE_ROWS}).all()
    out = []
    for name, size in rows:
        if not _IDENT.match(str(name)):
            continue
        got = connection.execute(text(
            f'SELECT count(*), COALESCE(sum(pg_column_size(t.*)), 0) FROM '
            f'(SELECT * FROM "{name}" LIMIT :n) t'), {"n": COMPACT_MAX_LIVE_ROWS + 1}).first()
        live_rows, live_bytes = int(got[0] or 0), int(got[1] or 0)
        if live_rows <= COMPACT_MAX_LIVE_ROWS and live_bytes <= COMPACT_MAX_LIVE_BYTES \
                and live_bytes <= COMPACT_MAX_LIVE_SHARE * int(size):
            out.append({"table": str(name), "bytes": int(size), "live_rows": live_rows,
                        "live_bytes": live_bytes})
    return out


def compact(engine: Any, *, dry_run: bool = False) -> list[dict[str, Any]]:
    """Rewrite each candidate (VACUUM FULL, its indexes rebuilt), a lock wait bounded by
    LOCK_TIMEOUT and the rewrite by COMPACT_STATEMENT_TIMEOUT. A table that cannot get its lock is
    left for the next pass. Returns [{table, bytes, after, error}]."""
    with engine.connect() as c:
        found = compaction_candidates(c)
    now = _now()
    found = [x for x in found if not (_COMPACT_BACKOFF.get(x["table"]) and _COMPACT_BACKOFF[x["table"]] > now)]
    if dry_run:
        return found
    out = []
    for cand in found:
        name = cand["table"]
        try:
            with _autocommit(engine) as c:
                try:
                    c.execute(text(f"SET lock_timeout = '{LOCK_TIMEOUT}'"))
                    c.execute(text(f"SET statement_timeout = '{COMPACT_STATEMENT_TIMEOUT}'"))
                    c.execute(text(f'VACUUM (FULL, ANALYZE) "{name}"'))
                    after = int(c.execute(text("SELECT pg_total_relation_size(to_regclass(:t))"),
                                          {"t": name}).scalar() or 0)
                finally:
                    # A pooled connection never keeps these limits, whatever happened above.
                    c.execute(text("RESET statement_timeout"))
                    c.execute(text("RESET lock_timeout"))
            out.append({**cand, "after": after, "error": None})
            if after * 2 > cand["bytes"]:
                _COMPACT_BACKOFF[name] = now + COMPACT_RETRY_AFTER   # it did not shrink: stop trying
            _log.info("compacted %s: %d → %d bytes", name, cand["bytes"], after)
        except Exception as exc:  # noqa: BLE001 — a busy table waits; a failed one backs off
            _COMPACT_BACKOFF[name] = now + COMPACT_RETRY_AFTER
            out.append({**cand, "after": None, "error": f"{type(exc).__name__}"})
            _log.info("table %s not compacted this pass", name, exc_info=True)
    return out


# ── the service: hourly, one process at a time ──────────────────────────────────────────────

_LAST: dict[str, Any] = {}
_THREAD: Optional[threading.Thread] = None


class _LeaderLock:
    """The session advisory lock every real sweep runs under (hourly pass, Infra Agent tier 2)."""

    def __init__(self, engine: Any):
        self.engine, self.conn, self.held = engine, None, False

    def __enter__(self) -> bool:
        self.conn = self.engine.connect()
        self.held = bool(self.conn.execute(text("SELECT pg_try_advisory_lock(hashtext(:k))"),
                                           {"k": LOCK_NAME}).scalar())
        self.conn.commit()
        return self.held

    def __exit__(self, *exc: Any) -> None:
        try:
            if self.held:
                self.conn.execute(text("SELECT pg_advisory_unlock(hashtext(:k))"), {"k": LOCK_NAME})
                self.conn.commit()
        finally:
            self.conn.close()


def leader_lock(engine: Any) -> _LeaderLock:
    return _LeaderLock(engine)


def sweep_locked(engine: Any, **kwargs: Any) -> Optional[dict[str, Any]]:
    """A REAL sweep under the leader lock; None when another process holds it (it is sweeping
    now). A dry run takes no lock."""
    if kwargs.get("dry_run"):
        return sweep(engine, **kwargs)
    with leader_lock(engine) as held:
        if not held:
            return None
        return sweep(engine, **kwargs)


def run_once(engine: Any, *, now: Optional[datetime] = None, convert: bool = True) -> dict[str, Any]:
    """Indexes → partitions → sweep, under the leader lock (a session advisory lock on its own
    connection). A second process gets {"skipped": "busy"} at once."""
    from . import system_services

    with leader_lock(engine) as held:
        if not held:
            return {"skipped": "busy"}
        report: dict[str, Any] = {"indexes": ensure_indexes(engine, now=now)}
        if convert:
            report["partitioning"] = ensure_partitioned(engine, now=now)
        report["sweep"] = sweep(engine, now=now)
        try:
            from ..db import background

            with background():
                report["compacted"] = compact(engine)
        except Exception:  # noqa: BLE001 — compaction never fails the sweep
            _log.info("compaction pass skipped", exc_info=True)
            report["compacted"] = []
        last = {k: report["sweep"][k] for k in ("at", "rows", "bytes", "partitions", "archived",
                                                 "errors", "receipt_id")}
        last["tables"] = [{k: t[k] for k in ("policy", "rows", "bytes", "days")}
                          for t in report["sweep"]["tables"]]
        last["compacted"] = [{k: t.get(k) for k in ("table", "bytes", "after")}
                             for t in report["compacted"] if not t.get("error")]
        ok = not report["sweep"]["errors"] and not report.get("partitioning", {}).get("error")
        system_services.beat(engine, SERVICE_KEY, ok=ok,
                             error=None if ok else "; ".join(report["sweep"]["errors"])[:400]
                             or report.get("partitioning", {}).get("error"),
                             details={"last_sweep": last})
    _LAST.clear()
    _LAST.update(report)
    return report


def last_report() -> dict[str, Any]:
    return dict(_LAST)


def start(engine: Any, settings: Any = None, *, interval: float = INTERVAL_SECONDS,
          first_delay: float = FIRST_PASS_DELAY) -> threading.Thread:
    global _THREAD
    if _THREAD is not None and _THREAD.is_alive():
        return _THREAD

    def _loop() -> None:
        time.sleep(first_delay)
        while True:
            try:
                run_once(engine)
            except Exception:  # noqa: BLE001 — the service keeps its clock
                _log.warning("telemetry retention pass failed", exc_info=True)
            time.sleep(interval)

    _THREAD = threading.Thread(target=_loop, name="boss-telemetry-retention", daemon=True)
    _THREAD.start()
    return _THREAD


__all__ = ["AUDIT_FLOOR_DAYS", "CHATTER_SQL", "RETENTION_OWNERS", "compact", "compaction_candidates", "CONVERSION_LOCK", "NOISE_EVENT_TYPES", "POLICIES",
           "POLICY_BY_KEY", "Policy", "TICK_RECEIPT_KINDS", "clear_stale_bound", "configured_days",
           "effective_days",
           "ensure_indexes", "ensure_months", "ensure_partitioned", "is_partitioned", "items_of",
           "leader_lock", "overrides_from_rule", "partitions", "plan", "run_once", "share",
           "start", "sweep", "sweep_locked"]
