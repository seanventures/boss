#!/usr/bin/env python3
"""BOSSAPP-940 (Kiro gap 7) — Guided first minute & time-to-first-useful-answer measurement.

Tracks the three first-minute milestones after install and measures elapsed time from
install completion to the first useful answer (target: <= 60s on prebuilt images):
  1. Claim / readiness check (API healthy)
  2. Workspace & model check (project + model ready)
  3. First useful answer (Ask BOSS or Quick Fix response received)
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional


@dataclass
class FirstMinuteReport:
    install_mode: str
    install_seconds: float
    time_to_first_answer_seconds: float
    under_60s: bool
    api_healthy: bool
    steps_completed: list[str]
    first_answer_preview: str
    recorded_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _http_json(
    url: str,
    *,
    method: str = "GET",
    payload: Optional[dict[str, Any]] = None,
    headers: Optional[dict[str, str]] = None,
    timeout: float = 10.0,
) -> tuple[int, dict[str, Any]]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req_headers = {"Accept": "application/json", **(headers or {})}
    if data is not None:
        req_headers.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return resp.status, json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as err:
        raw = err.read().decode("utf-8", errors="replace")
        try:
            body = json.loads(raw) if raw.strip() else {}
        except ValueError:
            body = {"detail": raw[:300]}
        return err.code, body


def run_guided_first_minute(
    *,
    api_base: str,
    install_mode: str = "prebuilt",
    install_seconds: float = 0.0,
    prompt: str = "Summarize the current project health and top next step.",
    requester: Optional[Callable[..., tuple[int, dict[str, Any]]]] = None,
    clock: Callable[[], float] = time.monotonic,
) -> FirstMinuteReport:
    """Execute the guided first-minute walk and measure time to first useful answer."""
    call = requester or _http_json
    base = api_base.rstrip("/")
    started = clock()
    steps: list[str] = []

    # Step 1: Health check
    status, _ = call(f"{base}/health")
    api_healthy = status == 200
    if api_healthy:
        steps.append("health_verified")

    # Step 2: Check projects/models readiness
    p_status, p_body = call(f"{base}/v1/projects")
    project_id = None
    if p_status == 200:
        steps.append("workspace_checked")
        projects = p_body.get("projects") if isinstance(p_body, dict) else None
        if isinstance(projects, list) and projects:
            project_id = projects[0].get("id")

    # Step 3: Ask for first useful answer
    answer_preview = ""
    if api_healthy:
        a_status, a_body = call(
            f"{base}/v1/assistant/ask",
            method="POST",
            payload={"project_id": project_id or "default", "message": prompt},
        )
        if a_status in (200, 201, 202):
            steps.append("first_useful_answer")
            answer_preview = str(
                a_body.get("answer")
                or a_body.get("reply")
                or a_body.get("summary")
                or json.dumps(a_body)
            )[:240]

    elapsed = round(max(0.0, clock() - started), 3)
    return FirstMinuteReport(
        install_mode=install_mode,
        install_seconds=round(max(0.0, float(install_seconds)), 3),
        time_to_first_answer_seconds=elapsed,
        under_60s=(elapsed <= 60.0 and "first_useful_answer" in steps),
        api_healthy=api_healthy,
        steps_completed=steps,
        first_answer_preview=answer_preview,
        recorded_at=datetime.now(timezone.utc).isoformat(),
    )


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="BOSS guided first-minute walk & timer")
    parser.add_argument("--api-base", default="http://127.0.0.1:8000")
    parser.add_argument("--install-mode", default="prebuilt", choices=("prebuilt", "source"))
    parser.add_argument("--install-seconds", type=float, default=0.0)
    parser.add_argument("--prompt", default="Summarize the current project health and top next step.")
    parser.add_argument("--output", default="", help="Optional JSON file path to save the report")
    args = parser.parse_args(argv)

    report = run_guided_first_minute(
        api_base=args.api_base,
        install_mode=args.install_mode,
        install_seconds=args.install_seconds,
        prompt=args.prompt,
    )
    rendered = json.dumps(report.to_dict(), indent=2)
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if report.api_healthy else 1


if __name__ == "__main__":
    sys.exit(main())
