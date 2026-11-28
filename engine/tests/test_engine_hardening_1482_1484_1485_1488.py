"""Regression tests for BOSSAPP-1482, BOSSAPP-1484, BOSSAPP-1485, and BOSSAPP-1488.

Covers:
- BOSSAPP-1482:
  * clear approved_resume_asked on 'retry' answer;
  * align P8 replica lease epoch on takeover across run_plane.fence, _LEASE_HELD, and heartbeat;
  * read rival_alive under the run-row lock and refuse takeover when claimer's own lease expired;
  * behavioural merge-card withdrawal test (in test_a_merge_card_speaks_for_the_latest_build.py).
- BOSSAPP-1484:
  * scripted-engine builds route _review_packet_markdown and model_routing.adapter_for("scripted")
    through ScriptedChatAdapter with ZERO provider calls and no OPENAI_API_KEY error.
- BOSSAPP-1485:
  * per-run request and token caps on subscription run tokens enforced at the model proxy with a
    plain HTTP 429 refusal when exhausted (never asking a person mid-build).
- BOSSAPP-1488:
  * provider-only "You've hit your weekly limit" line without a reset pauses routing for
    LIMIT_PROBE_S across capacity_after_failure, classify_pipeline_exception, and task_shepherd,
    while the four mixed app/test-output sentences stay not-limits.
"""
from __future__ import annotations

import contextlib
import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

URL = os.environ.get("BOSS_TEST_DATABASE_URL", "")
_NEEDS_DB = pytest.mark.skipif(
    not URL or "test" not in URL.lower(),
    reason="needs BOSS_TEST_DATABASE_URL naming a disposable database",
)


@pytest.fixture(scope="module")
def eng():
    from boss_engine.config import Settings
    from boss_engine.db import ensure_schema

    engine = create_engine(URL, future=True)
    ensure_schema(engine, Settings())
    yield engine
    engine.dispose()


def _mk_project(eng, prefix: str = "eh"):
    sfx = uuid4().hex[:8]
    org = f"org_{prefix}_{sfx}"
    ws = f"ws_{prefix}_{sfx}"
    proj = f"proj_{prefix}_{sfx}"
    with eng.begin() as c:
        c.execute(text("INSERT INTO orgs (id, name) VALUES (:o, :o)"), {"o": org})
        c.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES (:w, :o, :w)"),
                  {"w": ws, "o": org})
        c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES (:p, :w, :p, :p)"),
                  {"p": proj, "w": ws})
    return {"org": org, "ws": ws, "proj": proj, "sfx": sfx}


# ── BOSSAPP-1482 ──────────────────────────────────────────────────────────────────────────────


@_NEEDS_DB
def test_1482_expired_claimer_cannot_take_over_and_aligned_replica_survives_takeover(eng):
    from boss_engine.config import Settings
    from boss_engine.services import run_fencing, run_plane

    w = _mk_project(eng, "1482")
    sfx = w["sfx"]
    run_id = f"crun_1482_{sfx}"
    host_id = f"host_1482_{sfx}"
    dep_id = f"adep_1482_{sfx}"
    rep_id = f"arep_1482_{sfx}"
    job_expired = f"job_exp_{sfx}"
    job_live = f"job_live_{sfx}"
    tok_expired = f"tok_exp_{sfx}"
    tok_live = f"tok_live_{sfx}"
    hash_expired = run_fencing.hash_token(tok_expired)
    hash_live = run_fencing.hash_token(tok_live)

    with eng.begin() as c:
        c.execute(
            text(
                "INSERT INTO code_task_runs (id, project_id, title, status, phase, fence_epoch) "
                "VALUES (:r, :p, 't', 'running', 'implement', 5)"
            ),
            {"r": run_id, "p": w["proj"]},
        )
        # 1. Expired claimer with epoch=1 must be refused even when no rival is alive.
        c.execute(
            text(
                "INSERT INTO jobs (id, project_id, kind, status, payload, lease_owner, "
                "lease_token_hash, lease_expires_at, fence_epoch) "
                "VALUES (:j, :p, 'code.task', 'running', CAST(:pl AS JSONB), 'w1', :h, "
                "now() - interval '5 minutes', 1)"
            ),
            {"j": job_expired, "p": w["proj"], "pl": json.dumps({"run_id": run_id}),
             "h": hash_expired},
        )
        with pytest.raises(run_fencing.RunFenced):
            run_fencing.claim_run(c, run_id, hash_expired)

        # 2. Live replica holding job_live at epoch=2 takes over ghost run (epoch 5 -> 6).
        c.execute(
            text(
                "INSERT INTO agent_hosts (id, org_id, hostname) VALUES (:h, :o, :h)"
            ),
            {"h": host_id, "o": w["org"]},
        )
        c.execute(
            text(
                "INSERT INTO agent_deployments (id, project_id, agent_key, environment, created_by) "
                "VALUES (:d, :p, 'builder', 'dev', 'ceo')"
            ),
            {"d": dep_id, "p": w["proj"]},
        )
        c.execute(
            text(
                "INSERT INTO agent_replicas (id, org_id, project_id, deployment_id, host_id, "
                "vm_id, desired_state, observed_state, slots) "
                "VALUES (:rep, :o, :p, :d, :h, :vm, 'running', 'serving', 2)"
            ),
            {"rep": rep_id, "o": w["org"], "p": w["proj"], "d": dep_id, "h": host_id,
             "vm": f"vm_{sfx}"},
        )
        c.execute(
            text(
                "INSERT INTO jobs (id, project_id, kind, status, payload, lease_owner, "
                "lease_token_hash, lease_expires_at, fence_epoch) "
                "VALUES (:j, :p, 'code.task', 'running', CAST(:pl AS JSONB), :owner, :h, "
                "now() + interval '5 minutes', 2)"
            ),
            {"j": job_live, "p": w["proj"], "pl": json.dumps({"run_id": run_id}),
             "owner": run_plane.owner_of(rep_id), "h": hash_live},
        )
        assert run_fencing.claim_run(c, run_id, hash_live) is True

        # Job epoch moved 2 -> 6, and replica still holding LeaseRef(epoch=2) stays aligned!
        lease = run_plane.LeaseRef(replica_id=rep_id, job_id=job_live, token=tok_live, epoch=2)
        fenced_row = run_plane.fence(c, Settings(), host_id, run_id, lease)
        assert int(fenced_row["fence_epoch"]) == 6
        hb = run_plane.heartbeat(c, Settings(), host_id, run_id, lease)
        assert hb["fence_epoch"] == 6

        # A wrong/forged epoch (e.g. epoch=1, never the takeover_from_epoch) is STILL refused.
        bad_lease = run_plane.LeaseRef(replica_id=rep_id, job_id=job_live, token=tok_live, epoch=1)
        with pytest.raises(run_plane.RunPlaneError):
            run_plane.fence(c, Settings(), host_id, run_id, bad_lease)


# ── BOSSAPP-1484 ──────────────────────────────────────────────────────────────────────────────


def test_1484_scripted_review_packet_makes_zero_provider_calls(monkeypatch):
    from boss_engine.config import Settings
    from boss_engine.services import model_routing, scripted_engine
    from boss_engine.services.software_factory import FileChange, SoftwareFactoryService

    monkeypatch.setenv(scripted_engine.ENABLE_ENV, "1")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    settings = Settings(openai_api_key=None)
    sf = SoftwareFactoryService(settings)

    called_real = []

    def _boom(*_a, **_k):
        called_real.append(True)
        raise AssertionError("real provider adapter must NOT be called for scripted builds")

    monkeypatch.setattr(model_routing, "project_chat_adapter", _boom)

    emitted = []

    class _FakeEvents:
        def emit(self, _conn, _pid, ev_type, *_a, **_k):
            emitted.append(ev_type)

    sf.events = _FakeEvents()

    md = sf._review_packet_markdown(  # noqa: SLF001
        connection=None,
        project_id="proj_scripted",
        receipt_id="rcpt_1484",
        title="Add slug helper",
        summary="All gates green",
        files=[FileChange(path="src/slug.js", content="export const slug = s => s;\n")],
        module_spec=None,
        model_id=None,
        engine_name="scripted",
    )
    assert called_real == []
    assert "software_factory.review_packet_skipped" not in emitted
    assert "Scripted engine review packet" in md
    assert "OPENAI_API_KEY" not in md

    # model_routing.adapter_for("scripted") also returns ScriptedChatAdapter with 0 provider calls.
    adapter = model_routing.adapter_for(
        model_routing.ModelChoice("scripted", "scripted test"), None, settings, None
    )
    assert isinstance(adapter, scripted_engine.ScriptedChatAdapter)
    _ref, text_out, usage = adapter.create_text_response("Review this change")
    assert text_out and usage["cost_usd_micros"] == 0


# ── BOSSAPP-1485 ──────────────────────────────────────────────────────────────────────────────


def test_1485_subscription_run_token_enforces_request_and_token_caps_at_proxy():
    from boss_engine.services.model_proxy import ModelProxy

    upstream_calls = []

    @contextlib.contextmanager
    def _fake_upstream(method, url, headers, body):
        upstream_calls.append((method, url))
        payload = json.dumps({
            "id": "msg_1",
            "type": "message",
            "content": [{"type": "text", "text": "ok"}],
            "usage": {"input_tokens": 600, "output_tokens": 400},
        }).encode("utf-8")
        yield 200, {"content-type": "application/json"}, [payload]

    proxy = ModelProxy(upstream=_fake_upstream)
    proxy.start()
    try:
        # 1. Request cap = 2: third request refused plainly with HTTP 429, never reaches upstream.
        issued = proxy.issue(
            "claude_subscription", "real-sub-tok", ttl=300,
            run_id="crun_1485_req", project_id="p1", max_requests=2, max_tokens=100_000,
        )
        for _ in range(2):
            req = urllib.request.Request(
                f"{issued.base_url}/v1/messages",
                data=b'{"model":"claude-sonnet-4-6","messages":[]}',
                headers={"Authorization": f"Bearer {issued.token}",
                         "Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                assert resp.status == 200

        req3 = urllib.request.Request(
            f"{issued.base_url}/v1/messages",
            data=b'{"model":"claude-sonnet-4-6","messages":[]}',
            headers={"Authorization": f"Bearer {issued.token}",
                     "Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req3, timeout=5)
        assert exc_info.value.code == 429
        err_body = json.loads(exc_info.value.read().decode("utf-8"))
        assert err_body["error"]["code"] == "run_budget_exhausted"
        assert len(upstream_calls) == 2

        # 2. Token cap = 800: first call uses 1000 tokens -> second call refused plainly (429).
        issued2 = proxy.issue(
            "claude_subscription", "real-sub-tok", ttl=300,
            run_id="crun_1485_tok", project_id="p1", max_requests=50, max_tokens=800,
        )
        req_a = urllib.request.Request(
            f"{issued2.base_url}/v1/messages",
            data=b'{"model":"claude-sonnet-4-6","messages":[]}',
            headers={"Authorization": f"Bearer {issued2.token}",
                     "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req_a, timeout=5) as resp:
            assert resp.status == 200
        req_b = urllib.request.Request(
            f"{issued2.base_url}/v1/messages",
            data=b'{"model":"claude-sonnet-4-6","messages":[]}',
            headers={"Authorization": f"Bearer {issued2.token}",
                     "Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info2:
            urllib.request.urlopen(req_b, timeout=5)
        assert exc_info2.value.code == 429
        assert len(upstream_calls) == 3

        # 3. Default subscription grants are always bounded (never None/unbounded).
        default_issued = proxy.issue("claude_subscription", "real-sub-tok", ttl=300)
        g = proxy.grant_for(default_issued.token)
        assert g is not None and g.max_requests and g.max_tokens
    finally:
        proxy.stop()


# ── BOSSAPP-1488 ──────────────────────────────────────────────────────────────────────────────


def test_1488_provider_weekly_limit_without_reset_pauses_routing_while_app_output_stays_clear():
    from boss_engine.services import setup_checks, task_shepherd
    from boss_engine.services.code_task_service import classify_pipeline_exception
    from boss_engine.services.entitlements import CAPACITY_RESET_KEY
    from boss_engine.services.model_router.headroom import LIMIT_PROBE_S
    from boss_engine.services.typed_decision_spend import capacity_after_failure
    from tests.test_one_limit_detector import APP_OUTPUT

    provider_line = "You've hit your weekly limit"

    # 1. Direct provider error line (no reset named) -> timed denial (LIMIT_PROBE_S).
    before = datetime.now(timezone.utc)
    cap = capacity_after_failure(RuntimeError("cli failed"), provider_error=provider_line)
    assert cap["capacity_state"] == "unavailable"
    reset = setup_checks.parse_time(cap[CAPACITY_RESET_KEY])
    assert reset >= before + timedelta(seconds=LIMIT_PROBE_S - 5)

    # Also works when attached directly on the exception as `.provider_error`.
    exc = RuntimeError("build failed")
    exc.provider_error = provider_line  # type: ignore[attr-defined]
    cap2 = capacity_after_failure(exc)
    assert cap2["capacity_state"] == "unavailable" and cap2.get(CAPACITY_RESET_KEY)

    assert classify_pipeline_exception(
        "agent exited 1", provider_error=provider_line
    ) == ("needs_human", "model_rate_limit")
    assert task_shepherd.classify(
        "agent exited 1", provider_error=provider_line
    ) == task_shepherd.SPEND_CAP

    # 2. All 4 mixed app/test-output sentences stay strictly NOT limits!
    for msg in APP_OUTPUT:
        assert capacity_after_failure(RuntimeError(msg))["capacity_state"] == "available"
        assert classify_pipeline_exception(msg.lower()) != ("needs_human", "model_rate_limit")
        assert task_shepherd.classify(msg) != task_shepherd.SPEND_CAP
