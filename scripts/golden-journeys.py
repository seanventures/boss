#!/usr/bin/env python3
"""The golden journeys (BOSSAPP-1284) and the freeze counter (BOSSAPP-1285), from a terminal.

    scripts/golden-journeys.py list
    scripts/golden-journeys.py run --api http://localhost:8000 [--key-env BOSS_JOURNEY_KEY]
                                   [--var owner=acme --var repo=sandbox ...] [--vars-file F]
                                   [--only J1,J5] [--record [--host laptop]] [--out results.json]
    scripts/golden-journeys.py report --api URL [--key-env NAME]
    scripts/golden-journeys.py schedule --api URL --project P --at 06:00 [--tz Europe/London]

`run` plays the journeys against that install's API (real engine), judges each with the
Acceptance Agent's verdict module and prints one row per journey; `--record` stores the rows on
the install (POST /v1/golden-journeys/results), where Govern › Journeys and the counter read
them. `schedule` arms the install's own daily run (a schedule_work wake that calls the
run_golden_journeys tool). `report` prints today's results and the consecutive green days.

The API key is a service account's, read from the environment variable --key-env names; it is
held in memory only and never printed or written. Pending journeys (waiting on an open PR) are
listed and count as not green.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "engine"))

from boss_engine.services.golden_journeys import definitions, fakes, runner  # noqa: E402


def _client(args: argparse.Namespace) -> runner.HttpClient:
    key = os.environ.get(args.key_env or "BOSS_JOURNEY_KEY") or None
    if args.key_env and not key:
        sys.exit(f"golden-journeys: {args.key_env} is not set")
    return runner.HttpClient(args.api, key)


def _variables(args: argparse.Namespace) -> dict:
    out: dict = {}
    if args.vars_file:
        out.update(json.loads(Path(args.vars_file).read_text()))
    for pair in args.var or []:
        name, _, value = pair.partition("=")
        if not name or not _:
            sys.exit(f"golden-journeys: --var takes name=value, got {pair!r}")
        try:
            out[name] = json.loads(value)
        except ValueError:
            out[name] = value
    return out


def cmd_list(_: argparse.Namespace) -> int:
    for j in definitions.load_all():
        state = f"pending: {j.pending['waits_for']}" if j.is_pending else ", ".join(j.modes)
        print(f"{j.id:<4} {j.title:<70} {state}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    client = _client(args)
    only = {s.strip() for s in (args.only or "").split(",") if s.strip()} or None
    rows = runner.run_all(definitions.load_all(), client, variables=_variables(args),
                          fake_factory=fakes.start, only=only)
    for r in rows:
        print(f"{r['journey_id']:<4} {r['status']:<8} {r['seconds']:>7}s  {r['title'][:60]:<60} "
              f"{(r.get('why') or '')[:160]}")
    if args.out:
        Path(args.out).write_text(json.dumps(rows, indent=1, default=str))
    if args.record:
        status, body = client.call("POST", "/v1/golden-journeys/results",
                                   {"results": rows, **({"host": args.host} if args.host else {})})
        if status >= 300:
            print(f"golden-journeys: the install refused the results: HTTP {status} {body}",
                  file=sys.stderr)
            return 2
        print(f"recorded {len(body.get('stored') or [])} rows")
    return 0 if all(r["status"] == runner.GREEN for r in rows) else 1


def cmd_report(args: argparse.Namespace) -> int:
    status, body = _client(args).call("GET", "/v1/golden-journeys")
    if status != 200:
        print(f"HTTP {status}: {body}", file=sys.stderr)
        return 2
    print(f"{body['today']}: {body['green_days']} of {body['needed']} green days"
          f"{' (freeze lifted)' if body['freeze_lifted'] else ''}; hosts {', '.join(body['hosts'])}")
    for j in body["journeys"]:
        cells = "  ".join(f"{h}={v['status']}" for h, v in j["today"].items())
        print(f"{j['id']:<4} {j['title'][:60]:<60} {cells}")
    return 0


def cmd_schedule(args: argparse.Namespace) -> int:
    status, body = _client(args).call("POST", "/v1/golden-journeys/schedule", {
        "project_id": args.project, "at_time": args.at, "timezone": args.tz})
    print(json.dumps(body, indent=1, default=str) if status < 300 else f"HTTP {status}: {body}")
    return 0 if status < 300 else 2


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    for name in ("run", "report", "schedule"):
        p = sub.add_parser(name)
        p.add_argument("--api", required=True)
        p.add_argument("--key-env", help="the environment variable holding a service-account "
                                          "API key (default BOSS_JOURNEY_KEY, when set)")
        if name == "run":
            p.add_argument("--var", action="append")
            p.add_argument("--vars-file")
            p.add_argument("--only")
            p.add_argument("--record", action="store_true")
            p.add_argument("--host")
            p.add_argument("--out")
        if name == "schedule":
            p.add_argument("--project", required=True)
            p.add_argument("--at", required=True)
            p.add_argument("--tz", default="UTC")
    args = ap.parse_args(argv)
    return {"list": cmd_list, "run": cmd_run, "report": cmd_report,
            "schedule": cmd_schedule}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
