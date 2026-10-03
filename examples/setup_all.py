#!/usr/bin/env python3
"""Install every example workflow on a BOSS install, in one command.

RUNS THE INDIVIDUAL SCRIPTS, rather than reimplementing them. Each example stays independently
runnable and this stays a convenience — so there is no second copy of the setup logic to drift
from the first, and what this does is exactly what you would do by hand.

ONE FAILING EXAMPLE DOES NOT STOP THE OTHERS. They have different prerequisites: meeting to
tickets needs a work tracker, three of them need email. Somebody with a tracker and no SMTP
should end up with the examples they can actually run plus a clear list of what was skipped
and why — not four working examples they never saw because the second one stopped the script.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from email.utils import parseaddr
from pathlib import Path

HERE = Path(__file__).resolve().parent

#: In the order somebody meets them: the simplest prerequisites first, so a run that does hit
#: trouble has already installed the ones that were going to work.
EXAMPLES = [
    ("video-workflow", "edit a video and publish it", "Google Drive + YouTube, for the "
     "first and last step; the video agents are added from the media pack if missing"),
    ("trend-to-content", "a trend becomes a script, or an honest no", "email"),
    ("sales-signal", "a company name becomes a researched approach", "email"),
    ("weekly-newsletter", "research, written up, in your inbox", "email"),
    ("meeting-to-tickets", "a transcript becomes tracked work", "a work tracker — Plane, "
     "Jira, Linear, GitHub Issues or Azure DevOps"),
]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Install every example workflow.",
        epilog="Also reads BOSS_API, BOSS_API_KEY, BOSS_PROJECT, BOSS_ACTOR. "
               "Every example is safe to re-run, so this is too.")
    parser.add_argument("--api", default=os.environ.get("BOSS_API", "http://127.0.0.1:8000"))
    parser.add_argument("--key", default=os.environ.get("BOSS_API_KEY", ""))
    parser.add_argument("--project", default=os.environ.get("BOSS_PROJECT", "boss-dogfood"))
    parser.add_argument("--actor", default=os.environ.get("BOSS_ACTOR", "ceo"))
    parser.add_argument("--no-agents", action="store_true",
                        help="leave steps unassigned, so each runs as the workflow itself")
    parser.add_argument("--dry-run", action="store_true",
                        help="say what would be done and change nothing")
    parser.add_argument("--only", metavar="NAMES",
                        help="comma-separated subset, e.g. --only video-workflow,sales-signal")
    parser.add_argument("--schedule", metavar="TZ", nargs="?", const="UTC",
                        help="also schedule the newsletter for Monday 08:00 in this IANA zone")
    parser.add_argument("--newsletter-sectors", default="", metavar="TOPICS",
                        help="topics for every scheduled newsletter run")
    parser.add_argument("--newsletter-to", default="", metavar="EMAIL",
                        help="recipient for approved scheduled newsletters")
    args = parser.parse_args()

    if not args.key:
        # The same explanation the individual scripts give — imported rather than repeated, so
        # the two cannot drift into telling somebody different things.
        sys.path.insert(0, str(HERE))
        from _common import HOW_TO_FIND_THE_KEY

        print(f"\n  ERROR  {HOW_TO_FIND_THE_KEY}\n", file=sys.stderr)
        raise SystemExit(1)

    wanted = [w.strip() for w in (args.only or "").split(",") if w.strip()]
    unknown = [w for w in wanted if w not in {name for name, _, _ in EXAMPLES}]
    if unknown:
        print(f"\n  ERROR  no example called {', '.join(unknown)}. There is: "
              f"{', '.join(name for name, _, _ in EXAMPLES)}\n", file=sys.stderr)
        raise SystemExit(1)
    chosen = [e for e in EXAMPLES if not wanted or e[0] in wanted]
    if args.schedule and any(name == "weekly-newsletter" for name, _, _ in chosen):
        if not args.newsletter_sectors.strip() or not args.newsletter_to.strip():
            print("\n  ERROR  --schedule needs --newsletter-sectors and --newsletter-to. "
                  "The recurring run stores these inputs when it is created.\n",
                  file=sys.stderr)
            raise SystemExit(1)
        display_name, address = parseaddr(args.newsletter_to.strip())
        if display_name or address != args.newsletter_to.strip() or "@" not in address:
            print("\n  ERROR  --newsletter-to must be one email address, such as "
                  "reader@example.com.\n", file=sys.stderr)
            raise SystemExit(1)

    action = "Checking" if args.dry_run else "Installing"
    print(f"\n  {action} {len(chosen)} example{'' if len(chosen) == 1 else 's'} "
          f"into {args.project} on {args.api}")
    if args.dry_run:
        print("  DRY RUN — nothing will be changed.")
    print()

    installed: list[str] = []
    failed: list[tuple[str, str, str]] = []
    #: Installed, and BOSS says they cannot run here yet — with what is missing. Reported apart
    #: from failures: the workflow is there and Start will show the same list until it is fixed.
    not_ready: list[tuple[str, list[str]]] = []

    for name, what, needs in chosen:
        print(f"  ── {name} — {what}")
        command = [sys.executable, str(HERE / name / "setup.py"),
                   "--api", args.api,
                   "--project", args.project, "--actor", args.actor]
        if args.no_agents:
            command.append("--no-agents")
        if args.dry_run:
            command.append("--dry-run")
        if args.schedule and name == "weekly-newsletter":
            command += ["--schedule", args.schedule,
                        "--sectors", args.newsletter_sectors.strip(),
                        "--to", args.newsletter_to.strip()]

        # A setup key belongs in the child environment, never in argv where a process
        # listing on a shared machine can expose it while a slow API call runs.
        child_env = os.environ.copy()
        child_env["BOSS_API_KEY"] = args.key
        result = subprocess.run(command, capture_output=True, text=True, env=child_env)
        if result.returncode == 0:
            missing: list[str] = []
            for line in result.stdout.splitlines():
                stripped = line.strip()
                # The lines worth seeing from a run of five: what was published, and which
                # agent got each step. The rest is per-example detail its own script prints.
                if stripped.startswith(("published version", "unchanged", "created wfd",
                                        "every ", "next run",
                                        # A dry run says what it WOULD do, and a dry run that
                                        # printed nothing per example answered no question.
                                        "would ",
                                        # What will not work yet, and that it was checked.
                                        "not ready yet:", "fix:", "ready to run",
                                        "added (from")) or " added (from the " in stripped:
                    print(f"       {stripped}")
                if stripped.startswith("not ready yet:"):
                    missing.append(stripped[len("not ready yet:"):].strip())
            installed.append(name)
            if missing:
                not_ready.append((name, missing))
        else:
            # The example's OWN message, not "it failed". "this project has no active work
            # tracker configured" is the whole of what somebody needs to fix it.
            why = (result.stderr.strip() or result.stdout.strip() or "no output").splitlines()
            reason = next((line.replace("ERROR", "").strip() for line in why
                           if "ERROR" in line or "refused" in line), why[-1].strip())
            print(f"       SKIPPED — {reason}")
            failed.append((name, reason, needs))
        print()

    outcome = "checked" if args.dry_run else "installed"
    print(f"  {len(installed)} {outcome}" + (f", {len(failed)} skipped" if failed else ""))
    if installed and not args.dry_run:
        print("  Workflows → Library, in the console.")
    if not_ready:
        # INSTALLED IS NOT THE SAME AS RUNNABLE. The email examples installed cleanly on an
        # install with no mail server and failed at their last, approved step; this is the
        # list of what has to exist first, said at install time.
        print("\n  Installed, but will not run here yet:")
        for name, missing in not_ready:
            print(f"    {name}")
            for item in missing:
                print(f"      needs {item}")
        print("  Start shows the same list, with the fix, until each is in place.")
    if failed:
        # THE ACTUAL REASON LEADS. Listing prerequisites alone implied the prerequisite was the
        # problem — but a wrong --actor skips all five, and "needs email" is then a confident
        # answer to a question nobody asked. What it needs is shown as context underneath.
        print("\n  Skipped:")
        for name, reason, needs in failed:
            print(f"    {name}")
            print(f"      {reason}")
            print(f"      (this example also needs: {needs})")
        if args.dry_run:
            print("\n  Fix those and check again before installing.")
        else:
            print("\n  Fix those and run this again — anything already installed is reused,")
            print("  not duplicated.")
    print()
    # A partial install is a real outcome and not an error: it is what somebody with a tracker
    # and no SMTP should get. Only a total failure is worth a non-zero exit.
    raise SystemExit(0 if installed else 1)


if __name__ == "__main__":
    main()
