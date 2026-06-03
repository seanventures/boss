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


def drain_worker_once(client: TestClient) -> dict | None:
    poll = client.post(
        "/v1/worker/poll",
        json={
            "worker_id": "smoke-worker",
            "capabilities": [
                "plan.command",
                "plan.generate",
                "openai.plan",
                "software_factory.propose_pr",
                "github.pull_request.merge",
                "github.deployment.create",
            ],
            "lease_ttl_seconds": 60,
        },
    )
    poll.raise_for_status()
    leased = poll.json()
    if leased["status"] == "idle" or leased.get("job") is None:
        return None

    job = leased["job"]
    heartbeat = client.post(
        "/v1/worker/heartbeat",
        json={
            "worker_id": "smoke-worker",
            "job_id": job["id"],
            "lease_token": job["lease_token"],
            "lease_ttl_seconds": 60,
        },
    )
    heartbeat.raise_for_status()

    if job["kind"] == "openai.plan":
        result = client.post(
            "/v1/openai/plans",
            json={
                "project_id": job["project_id"],
                "reservation_id": job["payload"]["reservation_id"],
                "dedupe_key": job["payload"]["dedupe_key"],
                "prompt": job["payload"]["prompt"],
                "system_prompt": job["payload"].get("system_prompt"),
                "created_by_job_id": job["id"],
            },
        )
    elif job["kind"] == "software_factory.propose_pr":
        result = client.post(
            "/v1/software-factory/proposals",
            json={
                "project_id": job["project_id"],
                "reservation_id": job["payload"]["reservation_id"],
                "dedupe_key": job["payload"]["dedupe_key"],
                "title": job["payload"]["title"],
                "branch_name": job["payload"]["branch_name"],
                "summary": job["payload"]["summary"],
                "files": job["payload"]["files"],
                "created_by_job_id": job["id"],
            },
        )
    elif job["kind"] == "github.pull_request.merge":
        result = client.post(
            "/v1/software-factory/merges",
            json={
                "project_id": job["project_id"],
                "decision_id": job["payload"]["decision_id"],
                "dedupe_key": job["payload"]["dedupe_key"],
                "pull_request_number": job["payload"]["pull_request_number"],
                "pull_request_url": job["payload"]["pull_request_url"],
                "title": job["payload"]["title"],
                "branch_name": job["payload"].get("branch_name"),
                "created_by_job_id": job["id"],
            },
        )
    elif job["kind"] == "github.deployment.create":
        result = client.post(
            "/v1/software-factory/deployments",
            json={
                "project_id": job["project_id"],
                "decision_id": job["payload"]["decision_id"],
                "dedupe_key": job["payload"]["dedupe_key"],
                "merged_sha": job["payload"]["merged_sha"],
                "pull_request_url": job["payload"]["pull_request_url"],
                "title": job["payload"]["title"],
                "branch_name": job["payload"].get("branch_name"),
                "environment": job["payload"]["environment"],
                "created_by_job_id": job["id"],
            },
        )
    else:
        raise SystemExit(f"Unsupported smoke worker job kind: {job['kind']}")
    result.raise_for_status()
    result_payload = result.json()

    if result_payload.get("status") == "failed":
        failure = client.post(
            "/v1/worker/fail",
            json={
                "worker_id": "smoke-worker",
                "job_id": job["id"],
                "lease_token": job["lease_token"],
                "error": {"message": result_payload.get("message", "worker execution failed")},
                "retryable": False,
                "retry_delay_seconds": 0,
            },
        )
        failure.raise_for_status()
        return failure.json()

    completion = client.post(
        "/v1/worker/complete",
        json={
            "worker_id": "smoke-worker",
            "job_id": job["id"],
            "lease_token": job["lease_token"],
            "result": result_payload,
        },
    )
    completion.raise_for_status()
    return completion.json()


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
    parser.add_argument(
        "--with-merge",
        action="store_true",
        help="Approve the PR review decision and execute the merge job if merge policy is enabled.",
    )
    parser.add_argument(
        "--with-deploy",
        action="store_true",
        help="Approve the deployment review decision and execute the deployment job if deploy policy is enabled.",
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
            "/v1/software-factory/proposals/queue",
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
        print("Queued PR job:")
        print(json.dumps(pr_response.json(), indent=2))
        completion_payload = drain_worker_once(client)
        print("Worker completion:")
        print(json.dumps(completion_payload, indent=2))

        if args.with_deploy and not args.with_merge:
            raise SystemExit("--with-deploy requires --with-merge.")

        if not args.with_merge:
            return
        if not settings.enable_sandbox_merge:
            raise SystemExit(
                "BOSS_ENABLE_SANDBOX_MERGE is false. Enable it before running --with-merge."
            )
        decision_id = completion_payload.get("decision_id") if completion_payload else None
        if not decision_id:
            raise SystemExit("No PR review decision_id was returned from the proposal run.")
        approve = client.post(
            f"/v1/decisions/{decision_id}/approve",
            json={"actor": "ceo", "note": "Approve sandbox merge for smoke validation."},
        )
        approve.raise_for_status()
        print("Approved PR review decision:")
        print(json.dumps(approve.json(), indent=2))
        merge_completion = drain_worker_once(client)
        print("Merge worker completion:")
        print(json.dumps(merge_completion, indent=2))

        if not args.with_deploy:
            return
        if not settings.enable_sandbox_deploy:
            raise SystemExit(
                "BOSS_ENABLE_SANDBOX_DEPLOY is false. Enable it before running --with-deploy."
            )
        deploy_decision_id = (
            merge_completion.get("deployment_decision_id") if merge_completion else None
        )
        if not deploy_decision_id:
            raise SystemExit(
                "No deployment decision_id was returned from the merge execution."
            )
        deploy_approve = client.post(
            f"/v1/decisions/{deploy_decision_id}/approve",
            json={
                "actor": "ceo",
                "note": "Approve sandbox deployment for smoke validation.",
            },
        )
        deploy_approve.raise_for_status()
        print("Approved deployment review decision:")
        print(json.dumps(deploy_approve.json(), indent=2))
        deploy_completion = drain_worker_once(client)
        print("Deployment worker completion:")
        print(json.dumps(deploy_completion, indent=2))


if __name__ == "__main__":
    main()
