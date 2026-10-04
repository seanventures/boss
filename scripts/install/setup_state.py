#!/usr/bin/env python3
"""What Repo and models says about this install, for the installer card and `boss open`.

Reads GET /v1/setup (which never touches the network itself). The API key arrives in the
environment (BOSS_API_KEY), never argv, and is never printed.

    setup_state.py ready        -> prints "ready" or "not_ready" (exit 1 if the API did not answer)
    setup_state.py repository   -> prints "owner/app ✓", "owner/app ✗ <pill>" or "not connected"
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request

PROJECT = os.environ.get("BOSS_PROJECT_ID", "boss-dogfood")


def view() -> dict:
    api = os.environ.get("BOSS_API_URL", "http://127.0.0.1:8000").rstrip("/")
    request = urllib.request.Request(
        f"{api}/v1/setup?project_id={PROJECT}",
        headers={"Authorization": f"Bearer {os.environ.get('BOSS_API_KEY', '')}"})
    with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310 — local API
        return json.loads(response.read().decode())


def main(argv: list[str]) -> int:
    what = argv[1] if len(argv) > 1 else "ready"
    try:
        found = view()
    except Exception:  # noqa: BLE001 — the caller says "could not ask"; nothing to print
        return 1
    if what == "ready":
        print("ready" if found.get("ready") else "not_ready")
        return 0
    repos = (found.get("repository") or {}).get("repos") or []
    if not repos:
        print("not connected")
        return 0
    repo = next((r for r in repos if r.get("default")), repos[0])
    pill = repo.get("pill") or {}
    good = pill.get("tone") in ("green", "amber", "busy")
    print(f"{repo.get('full_name')} {'✓' if good else '✗ ' + str(pill.get('label') or '')}".rstrip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
