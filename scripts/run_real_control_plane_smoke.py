#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
import sys

from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from apps.api.app.bootstrap import seed
from apps.api.app.config import get_settings
from apps.api.app.db import database_is_reachable, get_engine
from apps.api.app.main import app


def quote_budget(
    client: TestClient, project_id: str, category: str, amount: int, reason: str
) -> None:
    response = client.post(
        "/v1/budget/quote",
        json={
            "project_id": project_id,
            "category": category,
            "amount_usd_micros": amount,
            "reason": reason,
        },
    )
    response.raise_for_status()
    payload = response.json()
    if not payload["allowed"]:
        raise SystemExit(json.dumps(payload, indent=2))


def reserve_budget(client: TestClient, project_id: str, category: str, amount: int, reason: str) -> str:
    quote_budget(client, project_id, category, amount, reason)
    response = client.post(
        "/v1/budget/reserve",
        json={
            "project_id": project_id,
            "category": category,
            "amount_usd_micros": amount,
            "reason": reason,
        },
    )
    response.raise_for_status()
    payload = response.json()
    if payload["status"] != "reserved":
        raise SystemExit(json.dumps(payload, indent=2))
    return payload["reservation_id"]


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Run the real BOSS control-plane smoke flow against local Postgres, "
            "OpenAI, and optionally the sandbox GitHub repository."
        )
    )
    parser.add_argument("--project-id", default=None)
    parser.add_argument(
        "--with-pr",
        action="store_true",
        help="Create a real draft PR in the configured sandbox repository.",
    )
    parser.add_argument(
        "--plan-budget-usd-micros",
        type=int,
        default=250_000,
        help="Budget reservation amount for the OpenAI planning call.",
    )
    parser.add_argument(
        "--pr-budget-usd-micros",
        type=int,
        default=500_000,
        help="Budget reservation amount for the sandbox PR proposal.",
    )
    args = parser.parse_args()

    get_settings.cache_clear()
    settings = get_settings()
    engine = get_engine(settings)
    if not database_is_reachable(engine):
        raise SystemExit(
            "Postgres is not reachable. Start Docker and run `make seed` first."
        )

    seed()
    project_id = args.project_id or settings.default_project_id
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    with TestClient(app) as client:
        reservation_id = reserve_budget(
            client,
            project_id,
            "model",
            args.plan_budget_usd_micros,
            "real control-plane smoke: plan draft",
        )
        plan_response = client.post(
            "/v1/openai/plans",
            json={
                "project_id": project_id,
                "reservation_id": reservation_id,
                "dedupe_key": f"real-smoke-plan-{timestamp}",
                "prompt": (
                    "Create a short technical execution plan for validating the BOSS "
                    "real-only sandbox workflow. Keep it concise and actionable."
                ),
                "system_prompt": (
                    "Return markdown with objectives, steps, risks, and a clear done-when."
                ),
            },
        )
        plan_response.raise_for_status()
        plan_payload = plan_response.json()
        print("Plan result:")
        print(json.dumps(plan_payload, indent=2))

        if not args.with_pr:
            return

        pr_reservation_id = reserve_budget(
            client,
            project_id,
            "tool",
            args.pr_budget_usd_micros,
            "real control-plane smoke: sandbox PR proposal",
        )
        pr_response = client.post(
            "/v1/software-factory/proposals",
            json={
                "project_id": project_id,
                "reservation_id": pr_reservation_id,
                "dedupe_key": f"real-smoke-pr-{timestamp}",
                "title": f"BOSS smoke proposal {timestamp}",
                "branch_name": f"boss-smoke/{timestamp.lower()}",
                "summary": (
                    "Create a real draft PR in the sandbox repository to validate "
                    "receipt-gated GitHub proposal flow."
                ),
                "files": [
                    {
                        "path": f"notes/boss-smoke-{timestamp}.md",
                        "content": (
                            "# BOSS Smoke Proposal\n\n"
                            f"- Generated at: {timestamp}\n"
                            "- Purpose: verify real receipt-gated PR creation.\n"
                            "- Merge policy: manual review only.\n"
                        ),
                    }
                ],
            },
        )
        pr_response.raise_for_status()
        print("PR result:")
        print(json.dumps(pr_response.json(), indent=2))


if __name__ == "__main__":
    main()
