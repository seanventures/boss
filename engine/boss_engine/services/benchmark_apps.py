"""Pinned full-stack application benchmarks for the governed coding pipeline.

The benchmarks are deliberately small enough to run in an isolated coding sandbox,
but each crosses planning, persistence, server behavior, browser behavior, and
verification.  They are task inputs, not pre-certified demos: only durable run
evidence may mark a benchmark as passed.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any


COMPLETION_CONTRACT = [
    "Written implementation plan is attached to the governed run.",
    "Working frontend and backend with database schema, migrations, and seed data.",
    "Client and server validation, API tests, and at least one browser-level end-to-end test.",
    "One-command local startup, health-check endpoint, lint, type-check, and concise README.",
    "Automated final verification report records actual command outcomes and no required TODOs remain.",
]


_APPS: tuple[dict[str, Any], ...] = (
    {
        "id": "bugbox",
        "name": "BugBox",
        "tagline": "Mini issue tracker",
        "summary": "CRUD, search/filtering, persistence, dashboard statistics, and responsive issue workflows.",
        "focus": [
            "full-stack CRUD",
            "database migrations",
            "search and filtering",
            "browser persistence",
        ],
        "acceptance_scenarios": [
            "Start the app with one command, create an issue, and confirm it appears on the dashboard.",
            "Change an issue to completed and confirm the dashboard totals update after refresh.",
            "Search for the issue, delete it, and confirm it disappears.",
        ],
        "prompt": """Build a full-stack application called BugBox, a lightweight issue tracker for one software project. Include a dashboard, issue list, issue creation/editing, issue detail pages, status and priority filters, search, assignees, tags, and a basic immutable activity history. Persist data in a local database with migrations and seed data. Add client and server validation, loading, empty, and useful error states. The issue model must include title, description, priority, status, assignee, and tags. Include API tests and a browser end-to-end test covering creation, editing/status transition, filtering, refresh persistence, and deletion.""",
    },
    {
        "id": "hookwatch",
        "name": "HookWatch",
        "tagline": "Webhook inspector and replay tool",
        "summary": "Dynamic request ingestion, arbitrary payload handling, durable request history, polling, and guarded outbound replay.",
        "focus": [
            "dynamic server routes",
            "request parsing",
            "outbound integration",
            "durable event log",
        ],
        "acceptance_scenarios": [
            "Create an inbox and POST JSON with a custom header to its generated endpoint.",
            "Confirm method, headers, query, body, timestamp, and content type appear and persist after refresh.",
            "Replay to the built-in mock destination and record the response; clear-history removes captured events.",
        ],
        "prompt": """Build a full-stack application called HookWatch. Users create webhook inboxes with unique HTTP endpoints. Any POST to an inbox endpoint is persisted and displayed with method, headers, query parameters, body, timestamp, and content type. Support JSON and plain-text payloads. Add a request detail view, copy-payload action, clear-history action, and a replay action to a built-in mock destination that records response status. Poll for new events unless real-time support already exists. Include seed data, API tests, and a browser end-to-end test that creates an inbox, sends a webhook, views it, refreshes it, and replays it. Treat replay as an auditable, allowlisted test-destination operation only; do not expose arbitrary production egress.""",
    },
    {
        "id": "approveit",
        "name": "ApproveIt",
        "tagline": "Expense approval workflow",
        "summary": "Role-scoped workflow rules, transactional state transitions, immutable audit records, and aggregate reporting.",
        "focus": [
            "authorization",
            "state machine",
            "audit trail",
            "negative-path tests",
        ],
        "acceptance_scenarios": [
            "As requester, submit a positive $125 software expense and see it pending.",
            "As manager, approve with a comment; requester sees status, comment, and immutable audit history.",
            "Server rejects editing or re-approving a terminal request, self-approval, and rejection without a comment.",
        ],
        "prompt": """Build a full-stack application called ApproveIt for submitting and approving expense requests. Seed one requester and one manager and provide a visible role switcher instead of OAuth. A requester can create, view, and withdraw pending requests. A manager can approve or reject pending requests with a decision comment; rejections require a comment. Requests contain amount, vendor, category, date, and explanation. Amounts must be positive. Only pending requests can transition; a requester cannot approve their own request; terminal requests are immutable. Every creation, withdrawal, approval, and rejection writes an immutable audit-log entry. Include dashboards for pending counts and monetary totals, server-side authorization and validation, migrations, seed data, API tests, and a browser end-to-end requester-to-manager workflow including negative paths.""",
    },
)


def list_benchmark_apps() -> list[dict[str, Any]]:
    """Return catalog data without leaking mutable module-level state."""
    return [
        {
            **deepcopy(app),
            "completion_contract": list(COMPLETION_CONTRACT),
            "verification_commands": [
                "npm run build",
                "npm run typecheck",
                "npm run lint",
                "npm run test:api",
                "npm run test:e2e",
                "npm run healthcheck",
            ],
        }
        for app in _APPS
    ]


def get_benchmark_app(benchmark_id: str) -> dict[str, Any] | None:
    target = (benchmark_id or "").strip().lower()
    return next((item for item in list_benchmark_apps() if item["id"] == target), None)


def benchmark_task_input(benchmark_id: str) -> dict[str, Any]:
    """Materialize the pinned task contract passed to the governed code-task runtime."""
    benchmark = get_benchmark_app(benchmark_id)
    if benchmark is None:
        raise ValueError(f"unknown QA benchmark app: {benchmark_id}")
    verification = benchmark["verification_commands"]
    instructions = (
        "This is a BOSS QA benchmark. Produce a real, repairable application, not a static mockup. "
        "Start by writing a concrete implementation plan before implementation. Use a local database "
        "and include schema migrations plus deterministic seed data. Do not leave required functionality "
        "as TODOs or pretend a check passed.\n\n"
        + benchmark["prompt"]
        + "\n\nSTANDARD COMPLETION CONTRACT:\n- "
        + "\n- ".join(COMPLETION_CONTRACT)
        + "\n\nREQUIRED PACKAGE SCRIPTS (each must be runnable and report its real result):\n- "
        + "\n- ".join(verification)
        + "\n\nThe README must state the one-command startup, migration/seed behavior, and the complete verification command. "
        "The health check must exercise a real running service rather than returning a hard-coded success response."
    )
    return {
        "benchmark": benchmark,
        "title": f"QA benchmark: {benchmark['name']} — {benchmark['tagline']}",
        "instructions": instructions,
        "acceptance_criteria": [
            *benchmark["acceptance_scenarios"],
            *COMPLETION_CONTRACT,
        ],
        # The existing code-task executor runs one authoritative command. `verify` must invoke
        # every required script and makes a single durable, replayable evidence record.
        "test_commands": ["npm run verify"],
        "finalize_ctx": {
            "qa_benchmark": {
                "id": benchmark["id"],
                "name": benchmark["name"],
                "verification_commands": verification,
                "completion_contract": list(COMPLETION_CONTRACT),
            }
        },
    }


def completion_report(run: dict[str, Any]) -> dict[str, Any]:
    """Render only observed evidence; an absent command is never treated as a pass."""
    spec = run.get("spec") or {}
    context = run.get("finalize_ctx") or {}
    benchmark = context.get("qa_benchmark") or spec.get("qa_benchmark") or {}
    tests = (run.get("state") or {}).get("tests") or []
    observed = tests if isinstance(tests, list) else []
    verify = next(
        (
            row
            for row in observed
            if isinstance(row, dict) and row.get("command") == "npm run verify"
        ),
        None,
    )
    verify_status = (
        "pass"
        if verify and verify.get("exit_code") == 0 and not verify.get("timed_out")
        else "fail"
        if verify
        else "not_recorded"
    )
    plan = (run.get("state") or {}).get("design")
    plan_status = (
        "pass"
        if isinstance(plan, dict) and str(plan.get("plan") or "").strip()
        else "not_recorded"
    )
    command_results = [
        {
            "label": "Implementation plan",
            "command": "governed design phase",
            "status": plan_status,
        },
        {
            "label": "Full benchmark verification",
            "command": "npm run verify",
            "status": verify_status,
            "exit_code": verify.get("exit_code") if isinstance(verify, dict) else None,
            "timed_out": verify.get("timed_out") if isinstance(verify, dict) else None,
        },
    ]
    is_complete = run.get("status") == "completed" and all(
        row["status"] == "pass" for row in command_results
    )
    return {
        "run_id": run.get("id"),
        "benchmark_id": benchmark.get("id"),
        "benchmark_name": benchmark.get("name"),
        "run_status": run.get("status"),
        "completion_contract": benchmark.get("completion_contract")
        or list(COMPLETION_CONTRACT),
        "required_verification_commands": benchmark.get("verification_commands") or [],
        "results": command_results,
        "passed": is_complete,
        "message": (
            "Benchmark acceptance evidence is complete."
            if is_complete
            else "Benchmark is not certified: missing, failed, or incomplete durable evidence remains visible."
        ),
    }
