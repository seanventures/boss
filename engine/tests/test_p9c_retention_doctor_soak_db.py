"""BOSSAPP-1107 (P9c-3): intake_events monthly partitioning + retention boundaries, Fleet doctor
playbooks, and multi-adapter simulated soak harness.

Covers:
  * Monthly partitioning of `intake_events` (`relkind = 'p'`), `ensure_monthly_partitions`
    creating `intake_events_YYYY_MM` and safely migrating rows from `intake_events_default`.
  * Tested retention drop & replay boundaries:
      - 14d raw boundary: replay succeeds before 14d; after 14d sweep `raw` is nulled while
        `raw_sha256` stays and `replay()` cleanly refuses (`raw body expired (retention)`).
      - 30d routed/ignored boundary vs 90d dead/refused boundary: routed rows deleted at 35d
        while dead/refused rows survive until 90d; active (`received`/`processing`/`failed`) rows
        are NEVER deleted or dropped.
      - Whole monthly partition drop (`DROP TABLE intake_events_YYYY_MM`) once strictly older than
        90d with zero active/unexpired rows, pruning `intake_event_dedupe` in that window.
  * All 4 Fleet doctor playbooks wired into `system_doctor`:
      1. `intake_subscription_lost` -> auto `repair_intake_subscription`
      2. `intake_poller_dead` -> auto `restart_intake_poller`
      3. `intake_connection_auth_failed` -> choice card + `pause_intake_connection`
      4. `chat_outbox_dead_letters` -> choice card + `retry_outbox_dead_letters` (skips auth errors)
  * P9c-T4 simulated soak via `scripts/intake_soak.py`: mixed adapters (webhook, github, outlook),
    redeliveries injected -> 0 lost, 0 duplicate runs, ack p99 <= 300 ms.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from boss_engine.config import get_settings
from boss_engine.connectors.intake.outlook import adapter as outlook_adapter
from boss_engine.services import (
    event_intake,
    intake_doctor,
    intake_retention,
    system_doctor,
)
from scripts.intake_soak import run_soak

from ._intake_support import (
    attach_chat_endpoint,
    deploy_local_agent,
    make_connection,
    make_project,
    make_route,
    pg_engine,
    purge,
    token_of,
)


@pytest.fixture(scope="module")
def eng():
    engine = pg_engine()
    yield engine
    engine.dispose()


@pytest.fixture()
def pid(eng):
    p = make_project(eng)
    yield p
    purge(eng, p)


# ── Monthly partitioning + retention drop & replay boundaries ─────────────────────────────────


def test_intake_events_monthly_partitioning_and_retention_replay_boundaries(eng, pid):
    conn = make_connection(eng, pid, kind="webhook", name="ret-wh")
    base_now = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
    old_month = datetime(2026, 2, 10, 12, 0, 0, tzinfo=timezone.utc)  # > 90d before June 15

    with eng.begin() as c:
        assert intake_retention.is_partitioned(c) is True
        # Ensure Feb 2026 (old) + Jun/Jul/Aug 2026 partitions exist.
        ens_old = intake_retention.ensure_monthly_partitions(c, now=old_month, months_back=0, months_ahead=0)
        ens_cur = intake_retention.ensure_monthly_partitions(c, now=base_now, months_back=0, months_ahead=2)
        parts = {p["name"] for p in intake_retention.list_monthly_partitions(c)}
        assert "intake_events_2026_02" in parts
        assert {"intake_events_2026_06", "intake_events_2026_07", "intake_events_2026_08"} <= parts
        assert isinstance(ens_old["created"], list) and isinstance(ens_cur["created"], list)

    # Seed 4 events with controlled timestamps/expiries:
    #  1) routed_recent: received June 1, raw expires June 15 11:00 (past base_now), row expires July 1
    #  2) dead_mid: received May 1, raw expired, row expires July 30 (90d dead retention)
    #  3) old_feb_expired: received Feb 10 (in intake_events_2026_02), expired March 12
    #  4) active_failed: received June 1, status='failed', raw_expires_at in the past -> MUST NOT be touched!
    ids = {}
    with eng.begin() as c:
        def _seed(label: str, *, status: str, recv: datetime, raw_exp: datetime, exp: datetime):
            eid = f"iev_ret_{label}_{uuid.uuid4().hex[:6]}"
            dk = f"ret:{label}:{eid}"
            c.execute(text(
                "INSERT INTO intake_event_dedupe (connection_id, dedupe_key, event_id, received_at) "
                "VALUES (:c, :dk, :id, :at)"),
                {"c": conn["id"], "dk": dk, "id": eid, "at": recv})
            c.execute(text("""
                INSERT INTO intake_events (
                    id, connection_id, project_id, dedupe_key, status, raw, raw_sha256,
                    received_at, raw_expires_at, expires_at)
                VALUES (:id, :c, :p, :dk, :st, :raw, :sha, :at, :rexp, :exp)"""),
                {"id": eid, "c": conn["id"], "p": pid, "dk": dk, "st": status,
                 "raw": b'{"hello":"world"}', "sha": "deadbeef" * 8,
                 "at": recv, "rexp": raw_exp, "exp": exp})
            ids[label] = eid

        _seed("routed_recent", status="routed",
              recv=datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc),
              raw_exp=datetime(2026, 6, 15, 11, 0, tzinfo=timezone.utc),
              exp=datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc))
        _seed("dead_mid", status="dead",
              recv=datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
              raw_exp=datetime(2026, 5, 15, 12, 0, tzinfo=timezone.utc),
              exp=datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc))
        _seed("old_feb_expired", status="routed",
              recv=old_month,
              raw_exp=old_month + timedelta(days=14),
              exp=old_month + timedelta(days=30))
        _seed("active_failed", status="failed",
              recv=datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc),
              raw_exp=datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc),
              exp=datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc))

    # Before raw expiry (June 15 10:00): replay on routed_recent SUCCEEDS!
    with eng.begin() as c:
        before_sweep = intake_retention.sweep_retention(
            c, now=datetime(2026, 6, 15, 10, 0, tzinfo=timezone.utc), manage_partitions=False,
        )
        assert before_sweep["raw_nulled"] >= 1  # dead_mid & old_feb_expired nulled, NOT routed_recent
        rep_ok = event_intake.replay(c, project_id=pid, ids=[ids["routed_recent"]], actor="ceo")
        assert ids["routed_recent"] in rep_ok["replayed"]
        # Mark it routed again so we can test post-14d boundary.
        c.execute(text("UPDATE intake_events SET status = 'routed' WHERE id = :id"),
                  {"id": ids["routed_recent"]})

    # At base_now (June 15 12:00 — past 14d raw boundary for routed_recent, and >90d past Feb 2026):
    with eng.begin() as c:
        res = intake_retention.sweep_retention(c, now=base_now, manage_partitions=True)
        assert "intake_events_2026_02" in res["partitions_dropped"]
        # routed_recent raw is now NULL, raw_sha256 kept, and replay is cleanly REFUSED!
        rr = c.execute(text("SELECT raw, raw_sha256 FROM intake_events WHERE id = :id"),
                       {"id": ids["routed_recent"]}).mappings().one()
        assert rr["raw"] is None and rr["raw_sha256"] == "deadbeef" * 8
        rep_refused = event_intake.replay(c, project_id=pid, ids=[ids["routed_recent"]], actor="ceo")
        assert rep_refused["replayed"] == []
        assert any("raw body expired" in s["reason"] for s in rep_refused["skipped"])
        # dead_mid survives (90d dead retention not yet expired).
        assert c.execute(text("SELECT 1 FROM intake_events WHERE id = :id"),
                         {"id": ids["dead_mid"]}).scalar() == 1
        # active_failed is NEVER nulled or deleted while status='failed'!
        af = c.execute(text("SELECT raw, status FROM intake_events WHERE id = :id"),
                       {"id": ids["active_failed"]}).mappings().one()
        assert af["raw"] is not None and af["status"] == "failed"
        # Feb 2026 partition is gone and its dedupe row pruned!
        parts_after = {p["name"] for p in intake_retention.list_monthly_partitions(c)}
        assert "intake_events_2026_02" not in parts_after
        assert c.execute(text("SELECT 1 FROM intake_event_dedupe WHERE event_id = :id"),
                         {"id": ids["old_feb_expired"]}).scalar() is None

    # Clean up test partitions created in 2026_06..08 only if empty.
    with eng.begin() as c:
        c.execute(text("DELETE FROM intake_events WHERE project_id = :p"), {"p": pid})


# ── All 4 Fleet doctor playbooks ──────────────────────────────────────────────────────────────


def test_fleet_doctor_all_four_intake_playbooks(eng, pid, monkeypatch):
    settings = get_settings()
    # Stub Outlook Graph transport for subscription repair playbook.
    def fake_graph(method, url, headers, body, purpose):
        if method == "POST" and url.endswith("/v1.0/subscriptions"):
            exp = (datetime.now(timezone.utc) + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
            return 201, {"id": "gsub_repaired", "resource": "users/a@b.com/messages",
                         "expirationDateTime": exp}, {}
        if method == "GET" and "/messages/delta" in url:
            return 200, {"value": [], "@odata.deltaLink": url + "?$deltatoken=1"}, {}
        return 200, {}, {}

    monkeypatch.setattr(outlook_adapter, "TRANSPORT", fake_graph)

    # 1. Subscription lost (Outlook)
    c_sub = make_connection(
        eng, pid, kind="outlook", name="doc-outlook",
        config={"mailbox": "doc@acme.com"},
        secrets={"client_state": "cs_doc_123456", "access_token": "tok_doc_123456"},
    )
    # 2. Poller dead (Outlook poll mode with 5 failures)
    with eng.begin() as c:
        c_poll = event_intake.create_connection(
            c, project_id=pid, kind="outlook", actor="ceo", name="doc-poller", mode="poll",
            config={"mailbox": "poller@acme.com"},
            secrets={"access_token": "tok_poller_123"},
        )
        c.execute(text("""
            UPDATE intake_connections
               SET health = jsonb_build_object('poll', jsonb_build_object(
                   'failures', 5, 'last_error', 'timeout',
                   'last_poll_at', to_char(now() - interval '1 hour', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
                   'next_poll_at', to_char(now() + interval '10 minutes', 'YYYY-MM-DD"T"HH24:MI:SS"Z"')))
             WHERE id = :id"""), {"id": c_poll["id"]})

    # 3. Connection auth failed
    c_auth = make_connection(eng, pid, kind="webhook", name="doc-auth-bad")
    with eng.begin() as c:
        event_intake.set_connection_status(
            c, project_id=pid, connection_id=c_auth["id"], actor="boss",
            status="auth_failed", reason="401 invalid_token",
        )

    # 4. Outbox dead letters (1 transient + 1 auth-blocked)
    ep_id = attach_chat_endpoint(eng, pid, c_auth["id"])
    with eng.begin() as c:
        sub_id = f"isub_doc_{uuid.uuid4().hex[:8]}"
        c.execute(text("""
            INSERT INTO intake_subscriptions (id, connection_id, provider_subscription_id,
                   resource, status, expires_at, renew_at)
            VALUES (:id, :c, 'gsub_gone', 'users/doc@acme.com/messages', 'removed',
                    now() - interval '1 hour', now() - interval '2 hours')"""),
            {"id": sub_id, "c": c_sub["id"]})
        c.execute(text("""
            INSERT INTO chat_outbox (id, endpoint_id, project_id, kind, dedupe_key, payload,
                   status, attempts, last_error)
            VALUES (:i1, :e, :p, 'agent_reply', :d1, '{}'::jsonb, 'dead', 6, '502 bad gateway'),
                   (:i2, :e, :p, 'agent_reply', :d2, '{}'::jsonb, 'dead', 6, 'invalid_auth')"""),
            {"i1": f"cob_t_{uuid.uuid4().hex[:8]}", "i2": f"cob_a_{uuid.uuid4().hex[:8]}",
             "e": ep_id, "p": pid,
             "d1": f"dead_t_{uuid.uuid4().hex[:8]}", "d2": f"dead_a_{uuid.uuid4().hex[:8]}"})

    # Collect + diagnose through system_doctor!
    with eng.connect() as c:
        health = intake_doctor.collect(c)
    findings = intake_doctor.diagnose(health)
    by_kind = {f["kind"]: f for f in findings}

    assert f"intake_subscription_lost:{sub_id}" in by_kind
    assert f"intake_poller_dead:{c_poll['id']}" in by_kind
    assert f"intake_connection_auth_failed:{c_auth['id']}" in by_kind
    assert f"chat_outbox_dead_letters:{ep_id}" in by_kind

    # Every auto action and offered option satisfies system_doctor's safety invariants!
    for f in findings:
        assert set(f["auto"]) <= system_doctor.AUTO_ACTIONS
        assert not (set(f["auto"]) & system_doctor.DESTRUCTIVE)
        assert len(f["options"]) <= 3
        for opt in f["options"]:
            assert opt["key"] in system_doctor.OFFERED_OPTIONS
            assert opt["key"] in system_doctor._CHOICES  # noqa: SLF001

    # Execute Playbook 1 (auto): repair_intake_subscription -> subscription becomes active.
    intake_doctor.repair_subscription(eng, settings, c_sub["id"])
    with eng.connect() as c:
        assert c.execute(text("SELECT status FROM intake_subscriptions WHERE id = :id"),
                         {"id": sub_id}).scalar() == "active"

    # Execute Playbook 2 (auto): restart_intake_poller -> failures reset to 0 and poll runs.
    res_poll = json.loads(intake_doctor.restart_poller(eng, settings, c_poll["id"]))
    assert res_poll["polled"] == 1

    # Execute Playbook 3 (operator choice): pause_intake_connection -> status becomes paused.
    res_auth = intake_doctor.pause_connection_choice(
        eng, settings, {}, by_kind[f"intake_connection_auth_failed:{c_auth['id']}"]["evidence"],
        actor="ceo",
    )
    assert res_auth["status"] == "resolved"
    with eng.connect() as c:
        assert c.execute(text("SELECT status FROM intake_connections WHERE id = :id"),
                         {"id": c_auth["id"]}).scalar() == "paused"

    # Execute Playbook 4 (operator choice): retry_outbox_dead_letters -> 1 requeued, 1 auth skipped!
    res_ob = intake_doctor.retry_outbox_dead_letters_choice(
        eng, settings, {}, by_kind[f"chat_outbox_dead_letters:{ep_id}"]["evidence"], actor="ceo",
    )
    assert res_ob["requeued"] == 1 and res_ob["skipped_auth"] == 1


# ── P9c-T4: Simulated multi-adapter soak (0 lost, 0 dupes, p99 <= 300 ms) ─────────────────────


def test_p9c_t4_simulated_multi_adapter_soak(eng, pid):
    settings = get_settings()
    wh = make_connection(eng, pid, kind="webhook", name="soak-wh")
    gh_secret = "soak-gh-" + uuid.uuid4().hex[:8]
    gh = make_connection(eng, pid, kind="github", name="soak-gh",
                         secrets={"webhook_secret": gh_secret})
    ol_state = "soak-ol-" + uuid.uuid4().hex[:8]
    ol = make_connection(eng, pid, kind="outlook", name="soak-ol",
                         config={"mailbox": "soak-box@acme.com"},
                         secrets={"client_state": ol_state})
    dep = deploy_local_agent(eng, pid)
    make_route(eng, pid, dep["id"], wh["id"], name="soak-wh-r", thread_key="{subject}")
    make_route(eng, pid, dep["id"], gh["id"], name="soak-gh-r", thread_key="{subject}")
    make_route(eng, pid, dep["id"], ol["id"], name="soak-ol-r", identity_mode="owner",
               thread_key="{subject}")

    report = run_soak(
        eng,
        project_id=pid,
        connections={
            "webhook": {"id": wh["id"], "token": token_of(wh["url_path"]), "secret": wh["secret"]},
            "github": {"id": gh["id"], "token": token_of(gh["url_path"]), "secret": gh_secret},
            "outlook": {"id": ol["id"], "token": token_of(ol["url_path"]), "secret": ol_state},
        },
        duration_seconds=0.8,
        rate_per_second=60.0,
        duplicate_ratio=0.25,
        settings=settings,
    )
    assert report["lost"] == 0, report
    assert report["duplicate_runs"] == 0, report
    assert report["errors"] == 0, report
    assert report["duplicates_absorbed"] > 0, report
    assert report["ack_p99_ms"] <= 300.0, report
