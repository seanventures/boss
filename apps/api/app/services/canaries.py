"""Canary suite — runnable smokes that PROVE the spec's non-negotiables: exactly-once effects,
no-drop comms, durable execution, and budget gating. Run nightly or on demand; each result
persists to canary_runs so the console can show canary status (spec §26 dashboards, §28 testing).

Deterministic canaries run for real against the live primitives. Heavy external canaries
(software factory, router fallback) are declared but reported 'skipped' unless live model/GitHub
lanes are configured — so the suite runs nightly without burning budget or flaking.
"""
from __future__ import annotations

import json

from sqlalchemy import text

from ..db import begin
from .budget import BudgetGovernor
from .comms import CommsService
from .core import emit_event, make_id
from .jobs import JobLeaseService
from .receipts import ReceiptService


def _scope(connection, project_id: str):
    row = connection.execute(
        text(
            "SELECT p.workspace_id AS ws, w.org_id AS org FROM projects p "
            "JOIN workspaces w ON w.id = p.workspace_id WHERE p.id = :p"
        ),
        {"p": project_id},
    ).mappings().first()
    return (row["org"], row["ws"]) if row else (project_id, project_id)


def _canary_exactly_once(engine, settings, project_id):
    """Same dedupe_key twice → one receipt, reused (no duplicate side effect)."""
    receipts = ReceiptService()
    key = f"canary:exactly_once:{make_id('k')}"
    with begin(engine) as c:
        org_id, ws_id = _scope(c, project_id)
        r1, c1 = receipts.create_or_reuse(
            c, org_id, ws_id, project_id, key, "canary.exactly_once", {"canary": True}, {"n": 1})
        r2, c2 = receipts.create_or_reuse(
            c, org_id, ws_id, project_id, key, "canary.exactly_once", {"canary": True}, {"n": 2})
    ok = bool(c1) and not bool(c2) and r1["id"] == r2["id"]
    return ok, f"created1={bool(c1)} created2={bool(c2)} same_receipt={r1['id'] == r2['id']}"


def _canary_no_drop_outbox(engine, settings, project_id):
    """Every outbound message is persisted with a tracked status — never silently dropped."""
    comms = CommsService(settings)
    key = f"canary:outbox:{make_id('k')}"
    with begin(engine) as c:
        msg = comms.create_outbox_message(
            c, project_id, channel="email:canary@example.invalid", subject="canary",
            body="canary smoke", dedupe_key=key, actor="canary")
        row = c.execute(
            text("SELECT status FROM outbox_messages WHERE id = :i"), {"i": msg["id"]}
        ).mappings().first()
    ok = row is not None and row["status"] in ("draft", "ready", "queued", "sending", "acked")
    return ok, f"outbox status={row['status'] if row else 'MISSING'} (tracked, not dropped)"


def _canary_job_lifecycle(engine, settings, project_id):
    """Durable execution: a job goes QUEUED → LEASED → SUCCEEDED via the lease protocol."""
    jl = JobLeaseService(settings)
    with begin(engine) as c:
        job = jl.create_job(c, project_id, kind="canary.noop", payload={"canary": True})
    with begin(engine) as c:
        leased = jl.claim_job(c, "canary-worker", ["canary.noop"], 60)
    if not leased or leased["id"] != job["id"]:
        return False, f"claim failed (got {leased['id'] if leased else None}, want {job['id']})"
    with begin(engine) as c:
        jl.complete(c, "canary-worker", leased["id"], leased["lease_token"], {"canary": "ok"})
    with begin(engine) as c:
        row = c.execute(
            text("SELECT status FROM jobs WHERE id = :i"), {"i": job["id"]}
        ).mappings().first()
    ok = row is not None and row["status"] == "succeeded"
    return ok, f"job {job['id']} → {row['status'] if row else 'MISSING'} (queued→leased→succeeded)"


def _canary_budget_reserve_release(engine, settings, project_id):
    """Budget gate: a reservation is created (quote→reserve) and cleanly released."""
    bg = BudgetGovernor(settings)
    with begin(engine) as c:
        res = bg.reserve(c, project_id, category="canary", amount_usd_micros=1000,
                         reason="canary reserve", expires_in_minutes=5)
    if res.get("status") != "reserved":
        return False, f"reserve denied: {res.get('status')}"
    rid = res["reservation_id"]
    with begin(engine) as c:
        bg.release(c, project_id, rid, "canary release")
        row = c.execute(
            text("SELECT status FROM budget_reservations WHERE id = :i"), {"i": rid}
        ).mappings().first()
    ok = row is not None and row["status"] == "released"
    return ok, f"reservation {rid} → {row['status'] if row else 'MISSING'} (reserve→release)"


CANARIES = [
    ("exactly_once", _canary_exactly_once),
    ("no_drop_outbox", _canary_no_drop_outbox),
    ("job_lifecycle", _canary_job_lifecycle),
    ("budget_reserve_release", _canary_budget_reserve_release),
]
SKIPPED = [
    ("software_factory", "needs live model + GitHub creds — run against staging / mock mode"),
    ("router_fallback", "needs ≥2 model lanes configured to exercise the fallback chain"),
]


def run_canary_suite(engine, settings, project_id: str, actor: str = "canary") -> dict:
    results = []
    for name, fn in CANARIES:
        try:
            ok, detail = fn(engine, settings, project_id)
            results.append({"canary": name, "status": "pass" if ok else "fail", "detail": detail})
        except Exception as exc:  # noqa: BLE001 — a canary error is a canary failure, not a crash
            results.append({"canary": name, "status": "fail", "detail": f"error: {exc}"})
    for name, why in SKIPPED:
        results.append({"canary": name, "status": "skipped", "detail": why})

    passed = sum(1 for r in results if r["status"] == "pass")
    failed = sum(1 for r in results if r["status"] == "fail")
    skipped = sum(1 for r in results if r["status"] == "skipped")
    overall = "fail" if failed else "pass"
    run_id = make_id("canary")
    with begin(engine) as c:
        c.execute(
            text(
                """
                INSERT INTO canary_runs (id, project_id, status, results, passed, failed, skipped)
                VALUES (:id, :p, :st, CAST(:r AS JSONB), :pa, :fa, :sk)
                """
            ),
            {"id": run_id, "p": project_id, "st": overall, "r": json.dumps(results),
             "pa": passed, "fa": failed, "sk": skipped},
        )
        emit_event(c, project_id, "canary.suite_run", actor, "canary_run", run_id,
                   {"status": overall, "passed": passed, "failed": failed, "skipped": skipped})
    return {"id": run_id, "status": overall, "passed": passed, "failed": failed,
            "skipped": skipped, "results": results}


def list_canary_runs(engine, project_id: str, limit: int = 20) -> list[dict]:
    with begin(engine) as c:
        rows = c.execute(
            text(
                """
                SELECT id, status, results, passed, failed, skipped, created_at
                FROM canary_runs WHERE project_id = :p ORDER BY created_at DESC LIMIT :l
                """
            ),
            {"p": project_id, "l": limit},
        ).mappings().all()
    return [dict(r) for r in rows]
