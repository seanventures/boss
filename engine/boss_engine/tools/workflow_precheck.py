"""`python -m boss_engine.tools.workflow_precheck` — the workflow pre-check from a shell.

    python -m boss_engine.tools.workflow_precheck --workflow 'HelpDesk Delivery (D1)' \\
        --project boss-dogfood [--app-dir <path>] [--checkpoint pre_run|pre_deploy] \\
        [--build-run crun_...] [--run wfr_...] [--offline] [--json]

Prints one row per check, per step: the verdict, what it saw, and the fix. Read-only: the
pre-check runs in a transaction it rolls back (services.workflow_precheck).
"""
from __future__ import annotations

import argparse
import json
import sys
import textwrap
from typing import Any, Optional, Sequence

MARK = {"ready": "ok", "fixable": "FIX", "will_fail": "FAIL", "unknown": "?"}


def render(report: dict[str, Any], *, width: int = 120) -> str:
    lines = [f"{report.get('workflow_name') or report.get('workflow_key')} · "
             f"{report['checkpoint']} · project {report['project_id']}"
             + (f" · app {report['app_dir']}" if report.get("app_dir") else "")
             + (f" · build {report['build_run_id']}" if report.get("build_run_id") else ""), ""]
    head = f"{'STEP':<16} {'CHECK':<24} {'VERDICT':<7} EVIDENCE / FIX"
    lines += [head, "-" * min(width, 140)]
    wrap = max(40, width - 50)
    for step in report["steps"]:
        for check in step["checks"]:
            evidence = check["evidence"]
            if check.get("also_needed_by"):
                evidence += f" [also for: {', '.join(check['also_needed_by'])}]"
            body = textwrap.wrap(evidence, wrap) or [""]
            action = (check.get("fix") or {}).get("action")
            if action and check["verdict"] != "ready":
                body += textwrap.wrap(("FIX (auto): " if check["fix"].get("auto") else "FIX: ")
                                      + action, wrap)
            if not check.get("skip_allowed", True) and check["verdict"] == "will_fail" \
                    and check.get("skip_refused"):
                body += textwrap.wrap(f"cannot skip: {check['skip_refused']}", wrap)
            lines.append(f"{step['step_id']:<16} {check['id']:<24} "
                         f"{MARK.get(check['verdict'], check['verdict']):<7} {body[0]}")
            lines += [" " * 49 + b for b in body[1:]]
    s = report["summary"]
    lines += ["", "COUNTS: " + ", ".join(f"{v} {s.get(v, 0)}" for v in MARK),
              f"SUMMARY: {s['sentence']} ({s.get('seconds', '?')}s)"]
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="workflow_precheck", description=__doc__.split("\n")[0])
    parser.add_argument("--workflow", help="workflow name, key or version id")
    parser.add_argument("--run", help="an existing workflow run (wfr_…)")
    parser.add_argument("--project", required=True)
    parser.add_argument("--checkpoint", default="pre_run", choices=("pre_run", "pre_deploy"))
    parser.add_argument("--app-dir")
    parser.add_argument("--repo-root")
    parser.add_argument("--build-run", help="the pass-1 build (crun_…) for pre_deploy")
    parser.add_argument("--input", help="run input as JSON")
    parser.add_argument("--input-from-run", help="start as this earlier run (wfr_…) was: its "
                        "input (the brief, the template, the app)")
    parser.add_argument("--offline", action="store_true", help="no network, no subprocesses")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if not (args.workflow or args.run):
        parser.error("--workflow or --run is required")

    from ..config import get_settings
    from ..db import get_engine
    from ..services import workflow_precheck as wp

    settings = get_settings()
    engine = get_engine(settings)
    run_input = json.loads(args.input) if args.input else {}
    if args.input_from_run:
        from sqlalchemy import text

        with engine.connect() as c:
            stored = c.execute(text("SELECT input FROM workflow_runs WHERE id = :r AND "
                                    "project_id = :p"),
                               {"r": args.input_from_run, "p": args.project}).scalar()
        if stored is None:
            print(f"precheck: no run {args.input_from_run}", file=sys.stderr)
            return 2
        stored = stored if isinstance(stored, dict) else json.loads(stored)
        run_input = {**{k: v for k, v in stored.items() if not k.startswith(("adopt_", "_"))},
                     **run_input}
    try:
        report = wp.precheck(
            engine, settings, project_id=args.project, workflow=args.workflow,
            run_id=args.run, run_input=run_input or None,
            checkpoint=args.checkpoint, app_dir=args.app_dir, repo_root=args.repo_root,
            build_run_id=args.build_run, offline=args.offline)
    except wp.PrecheckError as error:
        print(f"precheck: {error}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2, default=str) if args.json else render(report))
    return 1 if report["summary"]["blocking"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
