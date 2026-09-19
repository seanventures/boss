#!/usr/bin/env python3
"""Move work between BOSS's fork and the repository it was forked from.

BOSS writes to a FORK — `paramg/boss` — and cannot write to `seanheiney/boss` at all. That is
not a configuration gap to close: a fine-grained token's resource owner is the person who
issued it, so it cannot be scoped to a repository owned by another user account, and the
upstream repository is private, so "tokens can always read public repositories" does not apply
either. Measured: the connected token answers 404 for seanheiney/boss.

What CAN reach upstream is the operator's own credential — `gh`, holding a classic token with
`repo` scope, with `push: true` on the upstream repository. So promotion is a thing a person
runs, which is also the right shape: a pull request BOSS opened on a fork becomes a pull
request a human opens upstream, reviewed once more on the way.

Two directions, because a fork drifts in both:

    python3 scripts/promote_fork.py sync
        Bring the fork's main up to date with upstream, so the next thing BOSS writes is
        branched from current code rather than from the day the fork was made.

    python3 scripts/promote_fork.py promote 12
        Take pull request 12 on the fork, push its branch to upstream, and open the matching
        pull request there. Opens it as a DRAFT unless --ready is passed: what BOSS wrote has
        been reviewed on the fork, and a second pair of eyes upstream is the point of the
        exercise.

Nothing here merges anything. `gh pr merge` remains a deliberate act by whoever owns the
decision.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys

FORK = "paramg/boss"
UPSTREAM = "seanheiney/boss"


def gh(*args: str, check: bool = True) -> str:
    """Run `gh` and return its stdout. The operator's credential, never BOSS's."""
    try:
        out = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=120)
    except FileNotFoundError:
        raise SystemExit(
            "gh is not installed. It is what holds the credential that can reach the upstream "
            "repository — BOSS's own token cannot, by design.") from None
    if check and out.returncode != 0:
        raise SystemExit(f"gh {' '.join(args)} failed:\n{(out.stderr or out.stdout).strip()}")
    return (out.stdout or "").strip()


def require_push_upstream() -> None:
    """Fail early and say which credential is short, rather than at the push."""
    raw = gh("api", f"repos/{UPSTREAM}", "--jq", ".permissions.push")
    if raw.strip().lower() != "true":
        raise SystemExit(
            f"this gh account cannot push to {UPSTREAM}. Ask for write access, or open the "
            f"pull request from the GitHub UI on {FORK} with {UPSTREAM} as the base.")


def cmd_sync(_args: argparse.Namespace) -> int:
    """Fast-forward the fork's default branch to upstream's."""
    before = gh("api", f"repos/{FORK}/commits/main", "--jq", ".sha")[:7]
    gh("repo", "sync", FORK, "--source", UPSTREAM)
    after = gh("api", f"repos/{FORK}/commits/main", "--jq", ".sha")[:7]
    print(f"{FORK} main: {before} -> {after}"
          + ("  (already current)" if before == after else ""))
    return 0


def cmd_promote(args: argparse.Namespace) -> int:
    require_push_upstream()
    raw = gh("pr", "view", str(args.pr), "--repo", FORK,
             "--json", "number,title,body,headRefName,state,url")
    pr = json.loads(raw or "{}")
    if pr.get("state") != "OPEN" and not args.force:
        raise SystemExit(
            f"pull request {args.pr} on {FORK} is {pr.get('state','unknown').lower()}. "
            f"Pass --force to promote it anyway.")
    branch = pr["headRefName"]

    # The branch has to exist upstream before a pull request can point at it. Pushed from the
    # fork's remote rather than from a local checkout, so this works on a clean machine.
    print(f"pushing {branch} to {UPSTREAM}…")
    subprocess.run(["git", "fetch", f"https://github.com/{FORK}.git", branch],
                   check=True, timeout=300)
    subprocess.run(["git", "push", f"https://github.com/{UPSTREAM}.git",
                    f"FETCH_HEAD:refs/heads/{branch}"], check=True, timeout=300)

    body = (pr.get("body") or "").strip()
    trailer = f"\n\n---\nPromoted from {pr['url']} — written by BOSS on the fork."
    create = ["pr", "create", "--repo", UPSTREAM, "--base", "main", "--head", branch,
              "--title", pr["title"], "--body", body + trailer]
    if not args.ready:
        create.append("--draft")
    print(gh(*create))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("sync", help=f"bring {FORK} main up to date with {UPSTREAM}") \
       .set_defaults(func=cmd_sync)

    promote = sub.add_parser("promote", help=f"open {FORK} pull request N upstream")
    promote.add_argument("pr", type=int, help=f"pull request number on {FORK}")
    promote.add_argument("--ready", action="store_true",
                         help="open it ready for review instead of as a draft")
    promote.add_argument("--force", action="store_true",
                         help="promote a pull request that is no longer open")
    promote.set_defaults(func=cmd_promote)

    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
