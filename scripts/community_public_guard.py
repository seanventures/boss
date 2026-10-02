#!/usr/bin/env python3
"""Refuse a public Community sync that would drop work only the public repository has.

The export (`community_export.py`) builds the public tree from the private source and the sync
replaces public `main` with it. Anything pushed to public `main` after the previous sync, and not
yet brought into the private source, is erased by that replacement. That happened once: 39
collaborator commits after `v1.0-rc2` were replaced by the rc3 sync and had to be restored.

The rule this script enforces:

* Every public sync commit carries a trailer naming the private commit it was exported from:
  ``Private-source: <40-hex sha>``. (Release commits made before this rule say
  ``Private source <sha>.`` in their body; that form is still recognised.)
* Every non-merge public commit after the newest sync commit must be imported into the private
  ref being exported. A private commit marks an import with the trailer
  ``Imported-from: seanventures/boss@<40-hex public sha>``.
* The newest sync's private source must be an ancestor of the private ref, so a sync never rolls
  public back to an older private source.

A deliberate exception (a public commit that is meant to be discarded) is named one commit at a
time with ``--acknowledge <sha> --reason "..."``; the reason is printed with the result so it can
be copied into the sync commit. There is no flag that skips the check as a whole.

Exit status: 0 when the sync may proceed, 1 when it must not, 2 for a usage or git error.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_REPO = "seanventures/boss"
PRIVATE_SOURCE_TRAILER = re.compile(r"(?mi)^Private-source:\s*([0-9a-f]{7,40})\s*$")
LEGACY_PRIVATE_SOURCE = re.compile(r"(?m)\bPrivate source ([0-9a-f]{7,40})\.")
IMPORTED_FROM = re.compile(r"(?mi)^Imported-from:\s*([\w.-]+/[\w.-]+)@([0-9a-f]{40})\s*$")


class GuardError(Exception):
    """A git or usage problem: the check could not be made, which is not a pass."""


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if result.returncode != 0:
        raise GuardError(f"git {' '.join(args)} failed in {repo}: {result.stderr.strip()}")
    return result.stdout


@dataclass
class PublicCommit:
    sha: str
    subject: str
    body: str
    parents: list[str]


@dataclass
class Verdict:
    ok: bool
    sync_commit: str | None = None
    private_source: str | None = None
    pending: list[PublicCommit] = field(default_factory=list)
    imported: list[PublicCommit] = field(default_factory=list)
    acknowledged: list[tuple[PublicCommit, str]] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def public_commits(public: Path, *revs: str) -> list[PublicCommit]:
    """Public commits for a `git log` revision selection, newest first."""
    raw = git(public, "log", "--format=%H%x1f%P%x1f%s%x1f%B%x1e", *revs)
    commits = []
    for record in raw.split("\x1e"):
        record = record.strip("\n")
        if not record:
            continue
        sha, parents, subject, body = record.split("\x1f", 3)
        commits.append(PublicCommit(sha, subject, body, parents.split()))
    return commits


def sync_source(commit: PublicCommit) -> str | None:
    """The private sha a sync commit names, or None when the commit is not a sync."""
    match = PRIVATE_SOURCE_TRAILER.search(commit.body) or LEGACY_PRIVATE_SOURCE.search(commit.body)
    return match.group(1) if match else None


def imported_shas(private: Path, ref: str, public_repo: str) -> set[str]:
    """Full public shas that the private ref's history says it imported."""
    raw = git(private, "log", "--format=%B%x1e", f"--grep=Imported-from:", "-i", ref)
    found = set()
    for body in raw.split("\x1e"):
        for repo, sha in IMPORTED_FROM.findall(body):
            if repo.lower() == public_repo.lower():
                found.add(sha.lower())
    return found


def check(public: Path, public_ref: str, private: Path, private_ref: str, *,
          public_repo: str = PUBLIC_REPO, acknowledge: dict[str, str] | None = None) -> Verdict:
    acknowledge = {sha.lower(): reason for sha, reason in (acknowledge or {}).items()}
    verdict = Verdict(ok=False)
    # The newest sync along public main's own line (a sync is a commit on main, not on a
    # contributor's merged branch); then every commit reachable from main but not from it.
    for commit in public_commits(public, "--first-parent", public_ref):
        source = sync_source(commit)
        if source:
            verdict.sync_commit = commit.sha
            verdict.private_source = source
            break
    after_sync = (public_commits(public, f"{verdict.sync_commit}..{public_ref}")
                  if verdict.sync_commit else [])
    if verdict.sync_commit is None:
        verdict.problems.append(
            f"No sync commit on public {public_ref}: no commit carries a 'Private-source: <sha>' "
            "trailer, so BOSS cannot tell which public commits the private source already has.")
        return verdict

    try:
        source_full = git(private, "rev-parse", "--verify", "--quiet",
                          f"{verdict.private_source}^{{commit}}").strip()
    except GuardError:
        source_full = ""
    if not source_full:
        verdict.problems.append(
            f"The last sync ({verdict.sync_commit[:12]}) names private source "
            f"{verdict.private_source}, which this private repository does not have.")
    else:
        verdict.private_source = source_full
        ancestor = subprocess.run(
            ["git", "-C", str(private), "merge-base", "--is-ancestor", source_full, private_ref],
            capture_output=True, text=True)
        if ancestor.returncode == 1:
            verdict.problems.append(
                f"The last sync was exported from private {source_full[:12]}, which is not an "
                f"ancestor of {private_ref}: syncing would roll public back to older work.")
        elif ancestor.returncode != 0:
            raise GuardError(ancestor.stderr.strip() or "git merge-base failed")

    imported = imported_shas(private, private_ref, public_repo)
    unknown_acks = set(acknowledge)
    for commit in reversed(after_sync):
        if len(commit.parents) > 1:
            continue  # a merge carries no change of its own; its parents are listed themselves
        sha = commit.sha.lower()
        if sha in imported:
            verdict.imported.append(commit)
            continue
        matched = next((key for key in acknowledge if sha.startswith(key)), None)
        if matched is not None:
            unknown_acks.discard(matched)
            verdict.acknowledged.append((commit, acknowledge[matched]))
            continue
        verdict.pending.append(commit)
    for key in sorted(unknown_acks):
        verdict.problems.append(
            f"--acknowledge {key} names no public commit after the last sync.")
    verdict.ok = not verdict.pending and not verdict.problems
    return verdict


def report(verdict: Verdict, public_ref: str, private_ref: str, public_repo: str) -> str:
    lines = []
    if verdict.sync_commit:
        lines.append(f"Last public sync: {verdict.sync_commit[:12]} "
                     f"(private source {str(verdict.private_source)[:12]}).")
    for commit in verdict.imported:
        lines.append(f"  imported      {commit.sha[:12]} {commit.subject}")
    for commit, reason in verdict.acknowledged:
        lines.append(f"  acknowledged  {commit.sha[:12]} {commit.subject} -- {reason}")
    for commit in verdict.pending:
        lines.append(f"  NOT IMPORTED  {commit.sha[:12]} {commit.subject}")
    lines.extend(f"Problem: {problem}" for problem in verdict.problems)
    if verdict.ok:
        lines.append(f"OK: {private_ref} contains every public change since the last sync. "
                     f"Name it in the sync commit as 'Private-source: <sha of {private_ref}>'.")
        for commit, reason in verdict.acknowledged:
            lines.append(f"Record in the sync commit: Discards-public: {commit.sha} ({reason})")
    else:
        lines.append(
            f"REFUSED: syncing {private_ref} to public {public_ref} would drop the work above. "
            f"Import each commit into the private source first, keeping its author and adding "
            f"'Imported-from: {public_repo}@<sha>' (see docs/operations/COMMUNITY_EXPORT_TOOL.md), "
            "or, only for a commit that is meant to be discarded, pass "
            "--acknowledge <sha> --reason \"why\".")
    return "\n".join(lines)


def open_public(public: str, ref: str) -> tuple[Path, str, tempfile.TemporaryDirectory | None]:
    path = Path(public)
    if path.exists():
        return path, ref, None
    holder = tempfile.TemporaryDirectory(prefix="boss-public-guard-")
    clone = Path(holder.name) / "public.git"
    result = subprocess.run(
        ["git", "clone", "--quiet", "--bare", "--filter=blob:none", public, str(clone)],
        capture_output=True, text=True)
    if result.returncode != 0:
        holder.cleanup()
        raise GuardError(f"could not clone {public}: {result.stderr.strip()}")
    return clone, ref, holder


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--public", default=f"https://github.com/{PUBLIC_REPO}.git",
                        help="public repository: a local clone or a clone URL")
    parser.add_argument("--public-ref", default="main")
    parser.add_argument("--private", default=str(ROOT), help="private repository path")
    parser.add_argument("--private-ref", default="HEAD", help="the private ref being exported")
    parser.add_argument("--public-repo-name", default=PUBLIC_REPO,
                        help="the name Imported-from trailers use for the public repository")
    parser.add_argument("--acknowledge", action="append", default=[], metavar="SHA",
                        help="a public commit deliberately discarded by this sync (repeatable)")
    parser.add_argument("--reason", help="why the acknowledged commits are discarded (required "
                                         "with --acknowledge)")
    args = parser.parse_args(argv)
    if args.acknowledge and not (args.reason or "").strip():
        print("--acknowledge needs --reason: say why that public work is discarded.",
              file=sys.stderr)
        return 2
    for sha in args.acknowledge:
        if not re.fullmatch(r"[0-9a-fA-F]{7,40}", sha):
            print(f"--acknowledge {sha!r} is not a commit sha.", file=sys.stderr)
            return 2
    holder = None
    try:
        public, ref, holder = open_public(args.public, args.public_ref)
        verdict = check(public, ref, Path(args.private), args.private_ref,
                        public_repo=args.public_repo_name,
                        acknowledge={sha: args.reason.strip() for sha in args.acknowledge})
    except GuardError as error:
        print(f"Public sync guard could not run: {error}", file=sys.stderr)
        return 2
    finally:
        if holder is not None:
            holder.cleanup()
    print(report(verdict, args.public_ref, args.private_ref, args.public_repo_name))
    return 0 if verdict.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
