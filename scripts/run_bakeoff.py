#!/usr/bin/env python3
"""VAG bake-off driver (spec §8) — run the eval suite through the governed pipeline once
per model config and report $/verified-outcome, pass-rate, retries, and false-pass.

Thin by design: task/config content lives in evals/bakeoff/*.json, request building and
aggregation live in apps.api.app.services.bakeoff (unit-tested), and execution reuses the
real /v1/code-tasks API — so a bake-off run exercises exactly the production pipeline.

Launch + poll (a worker executes the queued jobs):
    python3 scripts/run_bakeoff.py --api https://boss-api.local.sean.network \
        --api-key $BOSS_API_KEY --project boss-dogfood --n 3

No-worker local mode (drives each run synchronously via /v1/code-tasks/step):
    python3 scripts/run_bakeoff.py --api http://localhost:8000 --project boss-dogfood --drive

Re-aggregate an earlier batch without launching anything:
    DATABASE_URL=... python3 scripts/run_bakeoff.py --report <batch_id>

The deployment executing the jobs must run the GOVERNED pipeline
(BOSS_GOVERNED_CODING_PIPELINE=true) for gate metrics to exist, and cheap-model configs
need OPENAI_BASE_URL/OPENAI_API_KEY pointed at the chosen provider.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from apps.api.app.services.bakeoff import (  # noqa: E402
    aggregate_batch,
    build_run_request,
    render_markdown,
)

TERMINAL = {"completed", "failed", "blocked"}


def _http(method: str, url: str, api_key: str, body: dict | None = None) -> dict:
    import urllib.request

    req = urllib.request.Request(url, method=method)
    req.add_header("Content-Type", "application/json")
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")
    data = json.dumps(body).encode() if body is not None else None
    with urllib.request.urlopen(req, data=data, timeout=120) as resp:
        return json.loads(resp.read().decode() or "{}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--api", default="http://localhost:8000")
    ap.add_argument("--api-key", default=os.environ.get("BOSS_API_KEY", ""))
    ap.add_argument("--project", default="boss-dogfood")
    ap.add_argument("--tasks", default=str(REPO_ROOT / "evals/bakeoff/tasks.json"))
    ap.add_argument("--configs", default=str(REPO_ROOT / "evals/bakeoff/configs.json"))
    ap.add_argument("--n", type=int, default=1, help="iterations per task per config")
    ap.add_argument("--only", default="", help="comma-separated config names to run")
    ap.add_argument("--owner", default="", help="target repo owner (else deployment default)")
    ap.add_argument("--repo", default="", help="target repo name (else deployment default)")
    ap.add_argument("--open-pr", action="store_true", help="open real PRs (default: suppressed)")
    ap.add_argument("--include-known-bad", action="store_true",
                    help="run disabled expect=fail probes too (false-pass audit)")
    ap.add_argument("--drive", action="store_true",
                    help="no-worker mode: drive each run synchronously via /v1/code-tasks/step")
    ap.add_argument("--poll-seconds", type=int, default=20)
    ap.add_argument("--timeout-minutes", type=int, default=45, help="per-run poll timeout")
    ap.add_argument("--out", default=str(REPO_ROOT / "evals/bakeoff/results"))
    ap.add_argument("--report", default="", metavar="BATCH_ID",
                    help="re-aggregate an existing batch (needs DATABASE_URL); launches nothing")
    args = ap.parse_args()

    if args.report:
        return _report(args, args.report)

    tasks_doc = json.loads(Path(args.tasks).read_text())
    configs_doc = json.loads(Path(args.configs).read_text())
    defaults = dict(tasks_doc.get("defaults") or {})
    defaults.update({"project_id": args.project, "open_pr": args.open_pr})
    if args.owner:
        defaults["owner"] = args.owner
    if args.repo:
        defaults["repo"] = args.repo

    tasks = [t for t in tasks_doc["tasks"]
             if t.get("enabled", True) or (args.include_known_bad and t.get("expect") == "fail")]
    configs = configs_doc["configs"]
    if args.only:
        wanted = {c.strip() for c in args.only.split(",") if c.strip()}
        configs = [c for c in configs if c["name"] in wanted]
        missing = wanted - {c["name"] for c in configs}
        if missing:
            print(f"unknown configs: {sorted(missing)}", file=sys.stderr)
            return 2

    batch_id = f"bake_{uuid.uuid4().hex[:10]}"
    total = len(configs) * len(tasks) * args.n
    print(f"batch {batch_id}: {len(configs)} configs × {len(tasks)} tasks × {args.n} = "
          f"{total} governed runs against {args.api} (PRs "
          f"{'OPEN' if args.open_pr else 'suppressed'}). Each run reserves the deployment's "
          f"code-task budget — make sure the project budget covers {total} reservations.")

    launched: list[dict] = []
    for config in configs:
        for task in tasks:
            for iteration in range(1, args.n + 1):
                payload = build_run_request(task, config, batch_id, iteration, defaults)
                resp = _http("POST", f"{args.api}/v1/code-tasks", args.api_key, payload)
                run_id = resp.get("run_id")
                print(f"  queued {config['name']} / {task['key']} #{iteration}: {run_id}")
                launched.append({"run_id": run_id, "config": config["name"], "task": task["key"]})
                if args.drive:
                    _drive(args, run_id)

    if not args.drive:
        _poll(args, launched)
    return _report(args, batch_id)


def _drive(args, run_id: str) -> None:
    """Synchronously advance one run to a terminal state (no worker)."""
    while True:
        step = _http("POST", f"{args.api}/v1/code-tasks/step", args.api_key, {"run_id": run_id})
        if step.get("done") or step.get("status") in TERMINAL:
            print(f"    -> {step.get('status')}")
            return


def _poll(args, launched: list[dict]) -> None:
    deadline = time.time() + args.timeout_minutes * 60 * max(1, len(launched))
    pending = {r["run_id"]: r for r in launched if r["run_id"]}
    while pending and time.time() < deadline:
        time.sleep(args.poll_seconds)
        for run_id in list(pending):
            run = _http("GET", f"{args.api}/v1/code-tasks/{run_id}", args.api_key)
            status = (run.get("run") or run).get("status")
            if status in TERMINAL:
                r = pending.pop(run_id)
                print(f"  done  {r['config']} / {r['task']}: {status} ({len(pending)} left)")
    if pending:
        print(f"WARNING: {len(pending)} runs still not terminal after the poll timeout; "
              f"the report will show them as incomplete.", file=sys.stderr)


def _report(args, batch_id: str) -> int:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("DATABASE_URL is required to aggregate (the report reads llm_calls/quality_gates).",
              file=sys.stderr)
        return 2
    from sqlalchemy import create_engine

    engine = create_engine(database_url, future=True)
    with engine.begin() as connection:
        report = aggregate_batch(connection, batch_id)
    out_dir = Path(args.out) / batch_id
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.json").write_text(json.dumps(report, indent=2, default=str))
    markdown = render_markdown(report)
    (out_dir / "report.md").write_text(markdown)
    print(f"\n{markdown}\nwritten to {out_dir}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
