#!/usr/bin/env python3
"""Launch a pinned BOSS QA benchmark and export its durable evidence report.

This command talks only to the BOSS control-plane API.  It never calls a model,
provider, GitHub, or a sandbox directly; the normal budgeted code-task workflow owns
those effects.  A developer model can use the emitted JSON as a reproducible tuning
input or supply a separately authored PR/config diff for the proposal endpoint.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


TERMINAL_RUN_STATES = {"completed", "failed", "cancelled"}


def request_json(
    api_url: str,
    method: str,
    path: str,
    *,
    api_key: str | None = None,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Perform one BOSS API request while keeping credentials out of output."""
    encoded_body = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Accept": "application/json"}
    if encoded_body is not None:
        headers["Content-Type"] = "application/json"
    if api_key:
        headers["X-Boss-Api-Key"] = api_key
    request = Request(
        f"{api_url.rstrip('/')}{path}",
        data=encoded_body,
        headers=headers,
        method=method,
    )
    try:
        with urlopen(request, timeout=30) as response:  # noqa: S310 - caller supplies BOSS API URL
            payload = response.read().decode("utf-8")
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")[:1000]
        raise RuntimeError(f"BOSS API {method} {path} failed ({error.code}): {detail}") from error
    except URLError as error:
        raise RuntimeError(f"BOSS API {method} {path} could not be reached: {error.reason}") from error
    try:
        decoded = json.loads(payload)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"BOSS API {method} {path} returned invalid JSON.") from error
    if not isinstance(decoded, dict):
        raise RuntimeError(f"BOSS API {method} {path} returned an unexpected JSON payload.")
    return decoded


def wait_for_report(
    api_url: str,
    run_id: str,
    *,
    api_key: str | None,
    poll_seconds: float,
    timeout_seconds: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    path = f"/v1/qa/benchmark-apps/runs/{run_id}/report"
    while True:
        report = request_json(api_url, "GET", path, api_key=api_key)
        if report.get("run_status") in TERMINAL_RUN_STATES:
            return report
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"Timed out after {timeout_seconds:g}s waiting for benchmark run {run_id}. "
                "The run is still durable; retrieve its report later with the same run ID."
            )
        time.sleep(poll_seconds)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Launch a governed QA benchmark and export its evidence-backed scorecard."
    )
    parser.add_argument(
        "--api-url",
        default=os.environ.get("BOSS_API_URL", "http://localhost:8000"),
        help="BOSS control-plane API URL (default: BOSS_API_URL or http://localhost:8000).",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("BOSS_API_KEY"),
        help="BOSS API key (default: BOSS_API_KEY; never written to the report).",
    )
    parser.add_argument(
        "--project-id",
        default=os.environ.get("BOSS_PROJECT_ID"),
        required=os.environ.get("BOSS_PROJECT_ID") is None,
    )
    parser.add_argument("--benchmark", required=True, help="Pinned benchmark ID, e.g. bugbox.")
    parser.add_argument("--actor", default="executive_console")
    parser.add_argument("--owner")
    parser.add_argument("--repo")
    parser.add_argument("--base-branch", default="main")
    parser.add_argument("--profile", default="strict")
    parser.add_argument("--poll-seconds", type=float, default=5.0)
    parser.add_argument("--timeout-seconds", type=float, default=1800.0)
    parser.add_argument(
        "--no-wait",
        action="store_true",
        help="Queue the durable run and print its ID without waiting for completion.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Write the returned queue record or terminal evidence report to this JSON file.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.poll_seconds <= 0 or args.timeout_seconds <= 0:
        raise SystemExit("--poll-seconds and --timeout-seconds must be positive.")

    catalog = request_json(args.api_url, "GET", "/v1/qa/benchmark-apps", api_key=args.api_key)
    benchmark_ids = {
        item.get("id")
        for item in catalog.get("benchmark_apps", [])
        if isinstance(item, dict)
    }
    if args.benchmark not in benchmark_ids:
        known = ", ".join(sorted(item for item in benchmark_ids if isinstance(item, str)))
        raise SystemExit(f"Unknown benchmark {args.benchmark!r}. Available: {known}.")

    run_payload = {
        "project_id": args.project_id,
        "actor": args.actor,
        "base_branch": args.base_branch,
        "profile": args.profile,
    }
    if args.owner:
        run_payload["owner"] = args.owner
    if args.repo:
        run_payload["repo"] = args.repo
    queued = request_json(
        args.api_url,
        "POST",
        f"/v1/qa/benchmark-apps/{args.benchmark}/runs",
        api_key=args.api_key,
        body=run_payload,
    )
    result = queued if args.no_wait else wait_for_report(
        args.api_url,
        str(queued["run_id"]),
        api_key=args.api_key,
        poll_seconds=args.poll_seconds,
        timeout_seconds=args.timeout_seconds,
    )
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as error:
        print(f"qa benchmark runner: {error}", file=sys.stderr)
        raise SystemExit(1) from error
