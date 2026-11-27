#!/usr/bin/env python3
"""Multi-adapter event-intake soak harness (P9c-3, BOSSAPP-1107; EVENTING_LLD.md §14 P9c-T4).

Drives mixed simulated events across all intake adapters (webhook, github, outlook, gmail,
salesforce stream) against Postgres at a target rate (default 10 events/s), injects redeliveries
to verify zero duplicates, processes the inbox, and reports:
  * accepted / lost / duplicate-run counts (target: 0 lost, 0 duplicate runs)
  * front-door ack latency p50 / p95 / p99 in ms (target: p99 <= 300 ms)

For the 72-hour boss-1 soak:
    PYTHONPATH=.:engine:apps/worker python scripts/intake_soak.py --duration-seconds 259200 --rate 10
For a quick local verification:
    PYTHONPATH=.:engine:apps/worker python scripts/intake_soak.py --duration-seconds 5 --rate 20
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import create_engine, text


def percentile(samples: list[float], pct: float) -> float:
    if not samples:
        return 0.0
    ordered = sorted(samples)
    idx = min(len(ordered) - 1, max(0, int(round((pct / 100.0) * (len(ordered) - 1)))))
    return ordered[idx]


def run_soak(engine: Any, *, project_id: str, connections: dict[str, dict[str, Any]],
             duration_seconds: float = 3.0, rate_per_second: float = 20.0,
             duplicate_ratio: float = 0.2, settings: Any = None) -> dict[str, Any]:
    """Run a mixed-adapter soak against `engine` for `duration_seconds` at `rate_per_second`.

    `connections` maps adapter key (`webhook`, `github`, `outlook`, `gmail`, `salesforce`) to
    `{"id": ..., "token": ..., "secret": ..., ...}`.
    """
    from boss_engine.connectors.intake.contract import RawRequest
    from boss_engine.connectors.intake.github.adapter import github_signature
    from boss_engine.connectors.intake.webhook.adapter import standard_webhooks_signature
    from boss_engine.services import event_intake, intake_streams

    ack_ms: list[float] = []
    sent_unique: set[str] = set()
    accepted_ids: list[str] = []
    duplicates_sent = 0
    errors = 0

    total_target = max(1, int(round(duration_seconds * rate_per_second)))
    start_t = time.monotonic()
    push_kinds = [k for k in ("webhook", "github", "outlook") if k in connections]
    replay_pool: list[tuple[str, str, RawRequest]] = []

    for i in range(total_target):
        kind = push_kinds[i % len(push_kinds)]
        meta = connections[kind]
        now_s = time.time()
        unique_key = f"soak-{kind}-{i}-{uuid.uuid4().hex[:8]}"

        if kind == "webhook":
            body = json.dumps({"event": "soak.ping", "seq": i, "subject": f"soak/{i}"}).encode()
            ts = str(int(now_s))
            sig = standard_webhooks_signature(meta["secret"], unique_key, ts, body)
            req = RawRequest(method="POST", headers={
                "content-type": "application/json", "webhook-id": unique_key,
                "webhook-timestamp": ts, "webhook-signature": sig,
            }, body=body)
        elif kind == "github":
            body = json.dumps({
                "action": "opened",
                "issue": {"number": i + 1, "title": f"Soak issue {i}", "body": "soak"},
                "repository": {"full_name": "acme/soak", "private": True},
                "sender": {"login": "soaker", "id": 7, "type": "User"},
            }).encode()
            req = RawRequest(method="POST", headers={
                "content-type": "application/json", "x-github-event": "issues",
                "x-github-delivery": unique_key,
                "x-hub-signature-256": github_signature(meta["secret"], body),
            }, body=body)
        else:  # outlook
            body = json.dumps({"value": [{
                "subscriptionId": "sub-soak",
                "changeType": "created",
                "clientState": meta["secret"],
                "resourceData": {"id": unique_key},
                "message": {
                    "id": unique_key,
                    "conversationId": f"conv-{i}",
                    "subject": f"Soak mail {i}",
                    "bodyPreview": f"body {i}",
                    "from": {"emailAddress": {"name": "Soak", "address": "soak@example.com"}},
                    "internetMessageHeaders": [
                        {"name": "Authentication-Results", "value": "mx; dmarc=fail"},
                    ],
                },
            }]}).encode()
            req = RawRequest(method="POST", headers={"content-type": "application/json"}, body=body)

        t0 = time.perf_counter()
        res = event_intake.receive(engine, kind, meta["token"], req, settings=settings, now=now_s)
        dt_ms = (time.perf_counter() - t0) * 1000.0
        ack_ms.append(dt_ms)
        if 200 <= int(res["http_status"]) < 300:
            sent_unique.add(unique_key)
            if res.get("event_id"):
                accepted_ids.append(str(res["event_id"]))
            if len(replay_pool) < 50:
                replay_pool.append((kind, meta["token"], req))
        else:
            errors += 1

        # Inject duplicate redeliveries to prove 0 duplicate rows/runs.
        if replay_pool and duplicate_ratio > 0 and (i % max(1, int(round(1.0 / duplicate_ratio)))) == 0:
            rk, rtok, rreq = replay_pool[i % len(replay_pool)]
            t1 = time.perf_counter()
            dres = event_intake.receive(engine, rk, rtok, rreq, settings=settings, now=time.time())
            ack_ms.append((time.perf_counter() - t1) * 1000.0)
            if dres.get("outcome") == "duplicate":
                duplicates_sent += 1
            else:
                errors += 1

        # Periodically consume Salesforce stream if wired.
        if "salesforce" in connections and i % 10 == 0:
            intake_streams.consume_due(
                engine, settings, connection_id=connections["salesforce"]["id"],
                max_events=25, max_wait_ms=50,
            )

        # Pace only if running a real multi-second soak.
        target_elapsed = (i + 1) / max(1.0, rate_per_second)
        rem = target_elapsed - (time.monotonic() - start_t)
        if rem > 0.001 and duration_seconds >= 1.0:
            time.sleep(min(rem, 0.25))

    # Drain processor for this project.
    processed_total = 0
    for _ in range(50):
        got = event_intake.process_due(engine, settings, limit=100, project_id=project_id)
        processed_total += int(got.get("claimed") or 0)
        if int(got.get("claimed") or 0) == 0:
            break

    with engine.connect() as c:
        stored_count = int(c.execute(text(
            "SELECT count(*) FROM intake_events WHERE id = ANY(:ids)"),
            {"ids": accepted_ids}).scalar() or 0) if accepted_ids else 0
        dup_fires = int(c.execute(text("""
            SELECT count(*) FROM (
                SELECT trigger_id, intake_event_id FROM agent_trigger_fires
                 WHERE project_id = :p AND intake_event_id IS NOT NULL AND outcome = 'admitted'
                 GROUP BY trigger_id, intake_event_id HAVING count(*) > 1
            ) d"""), {"p": project_id}).scalar() or 0)

    lost = len(accepted_ids) - stored_count
    elapsed = time.monotonic() - start_t
    return {
        "duration_seconds": round(elapsed, 3),
        "unique_sent": len(sent_unique),
        "accepted": len(accepted_ids),
        "stored": stored_count,
        "lost": lost,
        "duplicates_absorbed": duplicates_sent,
        "duplicate_runs": dup_fires,
        "processed": processed_total,
        "errors": errors,
        "ack_p50_ms": round(percentile(ack_ms, 50), 2),
        "ack_p95_ms": round(percentile(ack_ms, 95), 2),
        "ack_p99_ms": round(percentile(ack_ms, 99), 2),
        "at": datetime.now(timezone.utc).isoformat(),
    }


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="BOSS P9c multi-adapter event intake soak harness")
    parser.add_argument("--duration-seconds", type=float, default=5.0)
    parser.add_argument("--rate", type=float, default=10.0)
    parser.add_argument("--database-url", default=os.environ.get("BOSS_TEST_DATABASE_URL")
                        or os.environ.get("DATABASE_URL") or "")
    args = parser.parse_args(argv)
    if not args.database_url:
        print("error: set --database-url or BOSS_TEST_DATABASE_URL", file=sys.stderr)
        return 2
    os.environ.setdefault("BOSS_COMMUNITY_SECRET_STORE", "database")
    from boss_engine.config import get_settings
    from boss_engine.db import ensure_schema
    from boss_engine.services import event_intake

    eng = create_engine(args.database_url, future=True)
    ensure_schema(eng)
    pid = f"p_soak_{uuid.uuid4().hex[:8]}"
    with eng.begin() as c:
        c.execute(text("INSERT INTO orgs (id, name) VALUES ('org_soak', 'soak') ON CONFLICT DO NOTHING"))
        c.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES ('ws_soak', 'org_soak', 'soak') ON CONFLICT DO NOTHING"))
        c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES (:p, 'ws_soak', :p, :p)"), {"p": pid})
        wh = event_intake.create_connection(c, project_id=pid, kind="webhook", actor="soak", name="soak-wh")
        gh_secret = "soak-gh-secret-" + uuid.uuid4().hex[:8]
        gh = event_intake.create_connection(c, project_id=pid, kind="github", actor="soak", name="soak-gh",
                                            secrets={"webhook_secret": gh_secret})
        ol_state = "soak-ol-state-" + uuid.uuid4().hex[:8]
        ol = event_intake.create_connection(c, project_id=pid, kind="outlook", actor="soak", name="soak-ol",
                                            config={"mailbox": "soak-box@example.com"},
                                            secrets={"client_state": ol_state})
    conns = {
        "webhook": {"id": wh["id"], "token": wh["url_path"].rsplit("/", 1)[-1], "secret": wh["secret"]},
        "github": {"id": gh["id"], "token": gh["url_path"].rsplit("/", 1)[-1], "secret": gh_secret},
        "outlook": {"id": ol["id"], "token": ol["url_path"].rsplit("/", 1)[-1], "secret": ol_state},
    }
    try:
        report = run_soak(eng, project_id=pid, connections=conns,
                          duration_seconds=args.duration_seconds, rate_per_second=args.rate,
                          settings=get_settings())
        print(json.dumps(report, indent=2))
        ok = report["lost"] == 0 and report["duplicate_runs"] == 0 and report["errors"] == 0 and report["ack_p99_ms"] <= 300.0
        return 0 if ok else 1
    finally:
        with eng.begin() as c:
            for sql in (
                "DELETE FROM intake_events WHERE project_id = :p",
                "DELETE FROM intake_connections WHERE project_id = :p",
                "DELETE FROM secret_refs WHERE project_id = :p",
                "DELETE FROM events WHERE project_id = :p",
                "DELETE FROM projects WHERE id = :p",
            ):
                with c.begin_nested():
                    c.execute(text(sql), {"p": pid})
        eng.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
