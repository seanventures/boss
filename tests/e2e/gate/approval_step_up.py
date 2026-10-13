"""e2e-gate plumbing: a person approves a workflow gate that needs step-up, and the run resumes.

rc10.3/rc10.4 known issue: "After a 6-digit step-up, an approval isn't re-sent; press Approve
again." The console half is a web fix; this is the API half, end to end and with no doubles in
identity: the REAL app (FastAPI, over HTTP semantics with cookies and CSRF), transport auth ON and
authz ENFORCED as an install runs, a person who claimed the install with a setup link and signs in
with password + TOTP, a published workflow `prd` (Approval gate) -> `ship` (Approval gate), and:

  1. the person starts the run; one worker tick parks it at `prd` with a decision;
  2. their second factor goes stale (the session's mfa_at is aged, as the identity suites do);
  3. Approve -> 401 {"error": "step_up_required"} and the decision is still pending;
  4. step-up with a TOTP code -> 200;
  5. the SAME approval is sent again -> 200, the decision is approved;
  6. one worker tick: `prd` succeeded and `ship` is waiting with its own decision (the run moved on).

No model is called (both steps are gates). Runs in a private schema of a test-named database
(BOSS_E2E_GATE_DATABASE_URL), dropped at exit. Never prints a password, TOTP secret, key or cookie.

    BOSS_E2E_GATE_DATABASE_URL=postgresql://…/boss_test_e2egate \
      PYTHONPATH=<boss>:<boss>/engine python3 tests/e2e/gate/approval_step_up.py

Last line: `RESULT PASS …` or `RESULT FAIL …`; exit 0 / 1.
"""
from __future__ import annotations

import base64
import os
import secrets
import sys
import time
import traceback
from uuid import uuid4

ORIGIN = "https://boss.example.test"
HEADERS = {"Origin": ORIGIN, "Sec-Fetch-Site": "same-origin"}
PROJECT = "boss-dogfood"
_STEPS: list[str] = []


class Fail(Exception):
    pass


def note(msg: str) -> None:
    _STEPS.append(msg)
    print(f"  · {msg}", flush=True)


def expect(cond: bool, what: str) -> None:
    if not cond:
        raise Fail(what)


def _short(res) -> str:
    """Status and the start of a body; bodies here never hold a credential (none are echoed)."""
    try:
        body = res.text[:200]
    except Exception:  # noqa: BLE001
        body = ""
    return f"{res.status_code} {body}"


def _environment() -> None:
    owner_key, worker_key = "e2egate_owner_" + secrets.token_hex(16), "e2egate_worker_" + secrets.token_hex(16)
    os.environ.update({
        "BOSS_EDITION": "community",
        # As an install runs (scripts/install/community-compose.yml, tests/e2e/harness/stack.py).
        "BOSS_REQUIRE_API_AUTH": "true",
        "BOSS_AUTHZ_MODE": "enforce",
        "BOSS_API_KEY": owner_key,
        "BOSS_API_KEYS": f"{owner_key},{worker_key}",
        "BOSS_API_KEY_PRINCIPALS": f"{owner_key}:ceo,{worker_key}:system",
        "BOSS_WORKER_PRINCIPAL": "system",
        "BOSS_WORKER_API_KEY": worker_key,
        "BOSS_DEFAULT_PROJECT_ID": PROJECT,
        "BOSS_PUBLIC_URL": ORIGIN,
        "BOSS_ENVIRONMENT": "test",
        "BOSS_TELEMETRY": "off",
        # A fixed, test-only identity server key (the API suite's): identity refuses to run without one.
        "BOSS_IDENTITY_SERVER_KEY": os.environ.get(
            "BOSS_IDENTITY_SERVER_KEY", "dGVzdC1vbmx5LWlkZW50aXR5LXNlcnZlci1rZXktLS0tLS0tLS0tLS0tLS0="),
        # A throwaway vault master key for this private schema only (32 random bytes, base64).
        "BOSS_MASTER_KEY": base64.b64encode(secrets.token_bytes(32)).decode(),
    })
    os.environ["BOSS_E2E_GATE_WORKER_KEY"] = worker_key


def main() -> int:
    base = os.environ.get("BOSS_E2E_GATE_DATABASE_URL", "").strip()
    if not base:
        raise Fail("BOSS_E2E_GATE_DATABASE_URL is not set")
    _environment()
    try:
        from apps.api.tests.db_guard import assert_safe_to_wipe
        from apps.api.tests.schema_confinement import confine_to_private_schema
    except ImportError as exc:
        raise Fail(f"this BOSS has no test-database guard to confine the check: {exc}") from None
    assert_safe_to_wipe(base)
    schema = confine_to_private_schema(base)
    expect(schema is not None, "could not create a private schema in the gate's database")

    t0 = time.time()
    from sqlalchemy import create_engine, text

    from apps.api.app.bootstrap import seed
    from apps.api.app.config import get_settings
    from boss_engine.db import ensure_schema

    engine = create_engine(os.environ["DATABASE_URL"], future=True)
    ensure_schema(engine)
    get_settings.cache_clear()
    # The API's boot order: the vault registers its master key's check value first (vault.boot),
    # then the install is seeded.
    from boss_engine.identity import vault

    with engine.begin() as db:
        vault.boot(db=db)
    seed()
    note(f"private schema ready and seeded ({time.time() - t0:.0f}s)")

    from fastapi.testclient import TestClient

    from apps.api.app.main import app
    from boss_engine.identity import authn, claim, totp

    def client() -> TestClient:
        return TestClient(app, base_url=ORIGIN, raise_server_exceptions=False)

    def csrf(c) -> dict:
        me = c.get("/v1/identity/me")
        expect(me.status_code == 200, f"/v1/identity/me answered {_short(me)}")
        return {**HEADERS, "X-Boss-CSRF": me.json()["csrf_token"]}

    def session_hash(c) -> str:
        return authn.hash_token(c.cookies.get(authn.SESSION_COOKIE) or "")

    # ---- the person: claimed with a setup link from the host shell, as an install is ---------------
    email = f"gate-owner-{uuid4().hex[:6]}@example.test"
    password = "gate-" + secrets.token_urlsafe(18)
    with engine.begin() as db:
        token = claim.issue_setup_token(db, force=True)
    expect(bool(token), "no setup link could be issued (is this install already claimed?)")
    setup = client()
    res = setup.post("/v1/auth/setup/claim", headers=HEADERS,
                     json={"name": "Gate Owner", "email": email, "password": password, "setup_token": token})
    expect(res.status_code == 200, f"the claim was refused: {_short(res)}")
    begin = setup.post("/v1/identity/me/mfa/totp", headers=csrf(setup))
    expect(begin.status_code == 200, f"TOTP enrolment did not begin: {_short(begin)}")
    secret = begin.json()["secret"]
    step0 = totp.current_step()
    confirm = setup.post("/v1/identity/me/mfa/totp/confirm", headers=csrf(setup),
                         json={"code": totp.code_at(secret, step0 - 1)})
    expect(confirm.status_code == 200, f"TOTP enrolment was not confirmed: {_short(confirm)}")
    note("owner claimed with a setup link and enrolled TOTP")

    # ---- sign in as that person: password, then the 6-digit code ----------------------------------
    me = client()
    res = me.post("/v1/auth/login", headers=HEADERS, json={"email": email, "password": password})
    expect(res.status_code == 200 and res.json().get("next") == "mfa",
           f"password sign-in did not ask for the second factor: {_short(res)}")
    res = me.post("/v1/auth/mfa/totp", headers=HEADERS, json={"code": totp.code_at(secret, step0)})
    expect(res.status_code == 200 and res.json().get("next") == "done", f"TOTP sign-in failed: {_short(res)}")
    note("signed in with password + TOTP")

    def wf(path: str, **body):
        return me.post(f"/v1/delivery-workflows/{path}", headers=csrf(me),
                       json={"project_id": PROJECT, "actor": "ceo", **body})

    worker = {"Authorization": f"Bearer {os.environ['BOSS_E2E_GATE_WORKER_KEY']}"}

    def tick() -> None:
        r = client().post("/v1/delivery-workflows/run-capabilities", headers=worker,
                          json={"project_id": PROJECT})
        expect(r.status_code == 200, f"the worker's tick was refused: {_short(r)}")

    def steps(run_id: str) -> dict:
        r = me.get("/v1/delivery-workflows/runs", params={"project_id": PROJECT, "limit": 100})
        expect(r.status_code == 200, f"the runs view answered {_short(r)}")
        found = [x for x in r.json()["runs"] if x["id"] == run_id]
        expect(bool(found), f"run {run_id} is not in the runs view")
        return {s["step_id"]: s for s in found[0]["steps"]}

    # ---- a tiny workflow: PRD approval, then a second gate ------------------------------------------
    spec = {"entry_step": "prd", "steps": [
        {"id": "prd", "kind": "decision", "captain": "human", "next": ["ship"],
         "question": f"Approve the PRD? ({uuid4().hex[:6]})"},
        {"id": "ship", "kind": "decision", "captain": "human", "next": [],
         "question": f"Ship it? ({uuid4().hex[:6]})"}]}
    pub = wf("publish", name=f"Gate step-up {uuid4().hex[:6]}", spec=spec)
    expect(pub.status_code == 200, f"publishing the workflow failed: {_short(pub)}")
    key = pub.json()["definition"]["workflow_key"]
    started = wf("start", workflow_key=key, input={}, start_anyway=["model"])
    expect(started.status_code == 200, f"starting the workflow failed: {_short(started)}")
    run_id = started.json()["run"]["id"]
    tick()
    prd = steps(run_id)["prd"]
    expect(prd["status"] == "waiting" and prd.get("decision_id"),
           f"the run did not stop at its PRD approval: prd is {prd['status']}")
    decision = prd["decision_id"]
    note(f"workflow started, waiting at its PRD approval ({decision})")

    # ---- the second factor goes stale; Approve asks for step-up --------------------------------------
    with engine.begin() as db:
        db.execute(text("UPDATE identity_sessions SET mfa_at = now() - interval '11 minutes' "
                        "WHERE token_hash = :h"), {"h": session_hash(me)})
    approval = {"actor": "ceo", "note": "PRD approved in the e2e gate"}
    first = me.post(f"/v1/decisions/{decision}/approve", headers=csrf(me), json=approval)
    expect(first.status_code == 401 and (first.json() or {}).get("error") == "step_up_required",
           f"a stale second factor was not asked to step up: {_short(first)}")
    card = me.get(f"/v1/decisions/{decision}")
    expect(card.status_code == 200 and card.json().get("status") == "pending",
           f"the refused approval changed the decision: {_short(card)}")
    note("stale approval -> 401 step_up_required, decision still pending")

    stepped = me.post("/v1/auth/step-up", headers=csrf(me),
                      json={"method": "totp", "code": totp.code_at(secret, step0 + 1)})
    expect(stepped.status_code == 200, f"step-up with a TOTP code failed: {_short(stepped)}")
    note("stepped up with TOTP")

    again = me.post(f"/v1/decisions/{decision}/approve", headers=csrf(me), json=approval)
    expect(again.status_code == 200, f"the re-sent approval after step-up was refused: {_short(again)}")
    card = me.get(f"/v1/decisions/{decision}").json()
    expect(card.get("status") == "approved", f"the decision is {card.get('status')!r} after the re-sent approval")
    note("same approval re-sent -> 200, decision approved")

    # ---- the run resumes ------------------------------------------------------------------------------
    tick()
    now = steps(run_id)
    expect(now["prd"]["status"] == "succeeded", f"the approved gate is {now['prd']['status']!r}, not succeeded")
    expect(now["ship"]["status"] == "waiting" and now["ship"].get("decision_id"),
           f"the run did not move on after the approval: ship is {now['ship']['status']!r}")
    note("worker tick: prd succeeded, ship waiting on its own approval")
    return 0


if __name__ == "__main__":
    started_at = time.time()
    try:
        code = main()
        print(f"RESULT PASS approval needing step-up was re-sent and the run resumed "
              f"({time.time() - started_at:.0f}s)")
    except Fail as exc:
        code = 1
        print(f"RESULT FAIL {exc}")
    except Exception as exc:  # noqa: BLE001 — words, not a traceback, on the last line
        code = 1
        tb = traceback.extract_tb(exc.__traceback__)
        where = f"{tb[-1].filename.rsplit('/', 1)[-1]}:{tb[-1].lineno}" if tb else "?"
        print(f"RESULT FAIL the check could not run: {type(exc).__name__} at {where}: {str(exc)[:200]}")
    sys.stdout.flush()
    os._exit(code) if os.environ.get("BOSS_E2E_GATE_HARD_EXIT") else sys.exit(code)
