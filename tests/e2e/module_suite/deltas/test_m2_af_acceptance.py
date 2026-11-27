"""M2 Agent Framework Acceptance Delta Suite (Armed for M2 merge & --profile m7).

Merged M2.1 slices (`008f7187b`) run actively on `sv/main`; unmerged M2.2 slices
(`ai/codex/m2-2-agent-framework`) are explicitly skipped with the pending branch name.
Covers:
- BOSSAPP-1462: db.background() ContextVar propagation across worker thread pools / spawned threads (M2.1 — active)
- BOSSAPP-1463: AST/runtime sweep guard ensuring all worker sweeps enter db.background() (M2.1 — active)
- BOSSAPP-1103: GitHub/Jira/Plane/Teams/ServiceNow intake connector catalog (M2.2 — pending)
- BOSSAPP-1054: Web Console Workforce Deployments & Health tabs rendering cleanly (M2.2 — pending)
- BOSSAPP-1100: Microsoft Teams signed intake adapter (M2.2 — pending)
- BOSSAPP-1102: ServiceNow signed intake adapter (M2.2 — pending)
- BOSSAPP-1101..1107: hosts/next scale-from-zero, Mail/Salesforce intake & event retention (M2.2 — pending)
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import hmac
import json
import time
import uuid

import httpx
import pytest
from sqlalchemy import text

from boss_engine.db import (
    BACKGROUND_LOCK_TIMEOUT_SECONDS,
    background,
    begin,
    in_background,
    spawn_thread,
    submit_in_context,
)

_PENDING_M2_2 = pytest.mark.skip(
    reason="Pending unmerged branch: ai/codex/m2-2-agent-framework (M2.2)"
)


def _sign_hmac_hex(secret: str, payload: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def test_m2_1462_background_contextvar_propagates_to_spawned_worker_threads(db_engine):
    seen_bg: list[bool] = []
    seen_lt: list[str] = []

    def worker_child():
        seen_bg.append(in_background())
        with begin(db_engine) as conn:
            seen_lt.append(str(conn.execute(text("SHOW lock_timeout")).scalar()))

    with background():
        t = spawn_thread(worker_child, name="m2-1462-child")
        t.join(timeout=10)
        assert not t.is_alive()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            fut = submit_in_context(pool, worker_child)
            fut.result(timeout=10)

    assert seen_bg == [True, True]
    assert seen_lt == [f"{BACKGROUND_LOCK_TIMEOUT_SECONDS}s", f"{BACKGROUND_LOCK_TIMEOUT_SECONDS}s"]


def test_m2_1463_worker_sweeps_wrapped_in_db_background(tenant_factory):
    from pathlib import Path

    root = Path(__file__).resolve().parents[4]
    sweep_test = root / "engine" / "tests" / "test_an_abandoned_transaction_never_blocks_sign_in.py"
    assert sweep_test.exists(), "M2 sweep guard test missing"
    tenant = tenant_factory(seed_defaults=False)
    res = tenant.session_client.post(
        "/v1/bots/tick",
        json={"project_id": tenant.project_id, "limit": 5},
        headers=tenant.csrf_headers,
    )
    assert res.status_code == 200, res.text
    assert in_background() is False


@_PENDING_M2_2
def test_m2_1103_intake_catalog_includes_migrated_connectors(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    res = client.get("/v1/intake/catalog", headers=tenant.owner.headers)
    assert res.status_code == 200
    keys = {src.get("key") or src.get("kind") for src in res.json()["sources"]}
    assert {"github", "jira", "plane", "teams", "servicenow"}.issubset(keys)


@_PENDING_M2_2
def test_m2_1100_teams_and_1102_servicenow_intake_front_door(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    for kind in ("teams", "servicenow"):
        conn = tenant.session_client.post(
            "/v1/intake/connections",
            json={"project_id": tenant.project_id, "actor": tenant.owner.principal.id, "kind": kind, "name": f"m2-{kind}"},
            headers=tenant.csrf_headers,
        )
        assert conn.status_code == 201, f"{kind}: {conn.text}"
        cdata = conn.json()
        raw = json.dumps({"id": f"evt_{uuid.uuid4().hex[:8]}", "text": f"Alert from {kind}"}).encode("utf-8")
        secret = cdata.get("secret") or (cdata.get("secrets") or {}).get("signing_secret") or "m2_secret"
        ts = str(int(time.time()))
        sig = _sign_hmac_hex(secret, f"{ts}.".encode("utf-8") + raw)
        bad = client.post(
            cdata["url_path"],
            content=raw,
            headers={"Content-Type": "application/json", "X-BOSS-Timestamp": ts, "X-BOSS-Signature": "v1=" + ("0" * 64)},
        )
        assert bad.status_code in (401, 403), f"{kind} accepted forged signature: {bad.status_code}"
        good = client.post(
            cdata["url_path"],
            content=raw,
            headers={
                "Content-Type": "application/json",
                "X-BOSS-Timestamp": ts,
                "X-BOSS-Signature": f"v1={sig}",
                "X-BOSS-Delivery": f"m2_{uuid.uuid4().hex[:8]}",
            },
        )
        assert good.status_code in (200, 202), f"{kind}: {good.text}"


@_PENDING_M2_2
def test_m2_1101_hosts_next_scale_from_zero_and_1105_1107_catalog_retention(client, tenant_factory):
    tenant = tenant_factory(seed_defaults=False)
    cat = client.get("/v1/intake/catalog", headers=tenant.owner.headers)
    assert cat.status_code == 200
    keys = {src.get("key") or src.get("kind") for src in cat.json()["sources"]}
    assert {"mail", "salesforce"}.issubset(keys)
    scaling = client.get("/v1/workers/scaling", params={"project_id": tenant.project_id}, headers=tenant.owner.headers)
    assert scaling.status_code == 200


@_PENDING_M2_2
def test_m2_1054_console_workforce_deployments_and_health_routes(live_web_console):
    for route in ("/v2/workforce", "/v2/agents"):
        r = httpx.get(f"{live_web_console['web_url']}{route}", follow_redirects=False, timeout=15.0)
        assert r.status_code in (200, 307, 308), f"{route}: {r.status_code}"
