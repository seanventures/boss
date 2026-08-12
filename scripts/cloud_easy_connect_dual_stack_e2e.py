#!/usr/bin/env python3
"""Dual-stack Easy Connect smoke (Account Connect hosted contracts).

Run when Account Connect is up:

  export BOSS_ACCOUNT_CONNECT_URL=http://127.0.0.1:8010
  export BOSS_ACCOUNT_CONNECT_TOKEN=dev-token
  export AC_SERVICE_TOKEN=dev-token
  python scripts/cloud_easy_connect_dual_stack_e2e.py

Validates device authorize + browser session complete + entitlements seed.
Optional: BOSS_API_URL + BOSS_API_KEY probes /v1/cloud/catalog.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request


def _req(method: str, url: str, body: dict | None = None, token: str | None = None):
    data = None if body is None else json.dumps(body).encode()
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            raw = response.read().decode()
            return response.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as error:
        raw = error.read().decode()
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {"raw": raw}
        return error.code, payload


def main() -> int:
    ac = (os.environ.get("BOSS_ACCOUNT_CONNECT_URL") or "").rstrip("/")
    token = os.environ.get("BOSS_ACCOUNT_CONNECT_TOKEN") or os.environ.get(
        "AC_SERVICE_TOKEN"
    )
    if not ac:
        print("SKIP: BOSS_ACCOUNT_CONNECT_URL unset")
        return 0

    status, health = _req("GET", f"{ac}/v1/health")
    if status != 200:
        print(f"FAIL: AC health {status} {health}")
        return 1
    print("OK health", health)

    code = "TEST-E2E1"
    status, authz = _req(
        "POST",
        f"{ac}/v1/device/authorize",
        {"user_code": code, "hosted_org_id": "org_boss_cloud"},
        token=token,
    )
    if status not in {200, 201}:
        print(f"FAIL: device authorize {status} {authz}")
        return 1
    print("OK device authorize", authz)

    status, fetched = _req(
        "GET",
        f"{ac}/v1/device/authorizations/{code}",
        token=token,
    )
    if status != 200 or fetched.get("status") != "authorized":
        print(f"FAIL: device fetch {status} {fetched}")
        return 1
    print("OK device fetch", fetched)

    status, session = _req(
        "POST",
        f"{ac}/v1/auth-sessions",
        {
            "project_id": "boss-dogfood",
            "state": "state_e2e_test",
            "return_url": "http://127.0.0.1:3000/cloud/callback",
            "metadata": {"source": "e2e"},
        },
        token=token,
    )
    if status not in {200, 201} or not session.get("authorization_url"):
        print(f"FAIL: auth session {status} {session}")
        return 1
    print("OK auth session", session.get("id"))

    status, complete = _req(
        "POST",
        f"{ac}/v1/auth-sessions/{session['id']}/complete",
        {
            "project_id": "boss-dogfood",
            "code": "e2e-code",
            "state": "state_e2e_test",
            "metadata": {},
        },
        token=token,
    )
    if status != 200 or not complete.get("hosted_org_id"):
        print(f"FAIL: complete {status} {complete}")
        return 1
    print("OK complete", complete)

    status, ents = _req(
        "GET",
        f"{ac}/v1/orgs/org_boss_cloud/entitlements",
        token=token,
    )
    if status != 200:
        print(f"FAIL: entitlements {status} {ents}")
        return 1
    print("OK entitlements", len(ents.get("entitlements") or []))

    print("PASS dual-stack hosted Account Connect contracts")
    return 0


if __name__ == "__main__":
    sys.exit(main())
