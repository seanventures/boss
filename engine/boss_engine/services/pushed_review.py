"""Identify the submitted files of an existing local change independently of the worktree diff."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from . import local_repo


@dataclass
class SubmittedChange:
    paths: list[str]
    documents_only: bool


def inspect(git_dir: Path, base: str, head: str) -> SubmittedChange:
    # Commit IDs come from the stored change. No rename detection: a rename out of code
    # still includes the removed source path. Inspect both tree modes, including deletions.
    raw = local_repo.git(["diff", "--raw", "--no-renames", "-z", f"{base}...{head}", "--"],
                         git_dir=git_dir, raw=True)
    fields = raw.split(b"\0")[:-1]
    paths = []
    documents_only = bool(fields)
    for header, name in zip(fields[::2], fields[1::2], strict=True):
        old, new = header[1:].split()[:2]
        path = name.decode("utf-8", "replace")
        paths.append(path)
        documents_only = documents_only and prose_file(path, old) and prose_file(path, new)
    if documents_only:
        # A repository with any code, executable, symlink, manifest or test keeps its
        # normal build and test gates, even when this particular edit touches a document.
        tree = local_repo.git(["ls-tree", "-r", "-z", head], git_dir=git_dir, raw=True)
        for entry in tree.split(b"\0")[:-1]:
            meta, name = entry.split(b"\t", 1)
            mode = meta.split()[0]
            # The local-repo seed always includes a regular .gitignore. A change to this
            # file already failed the changed-path check above.
            if name == b".gitignore" and mode == b"100644":
                continue
            if not prose_file(name.decode("utf-8", "replace"), mode):
                documents_only = False
                break
    return SubmittedChange(paths, documents_only)


def prose_file(path: str, mode: bytes) -> bool:
    p = PurePosixPath(path)
    return (mode in {b"000000", b"100644"} and p.suffix.lower() in {".md", ".rst", ".txt"}
            and all(not component.startswith(".") for component in p.parts)
            and p.name.lower() != "cmakelists.txt"
            and not p.name.lower().startswith(("requirements", "constraints"))
            and not {"requirements", "constraints"}.intersection(part.lower() for part in p.parts))


REVIEW_CRITERIA = (
    "Check the submitted change. Build and run the existing suite when applicable. "
    "In a documentation-only repository report the document checks and the absence of an application suite.",
    "The review panel reviewed the change.",
)


def documentation_acceptance(items, *, stack, source, dropped, head, paths, check, verdict, votes, suspect):
    """Judge BOSS's own two review steps from this revision's Git and panel receipts."""
    from .acceptance.verdict import MET, MISSING, UNJUDGED, summarize_judgments
    from .review_input import hollow_reason

    if tuple(item["text"] for item in items) != REVIEW_CRITERIA or not paths or dropped:
        return None
    git_passed = check.exit_code == 0 and not check.timed_out
    git_evidence = (f"git diff --check on {head} exited {check.exit_code}. "
                    "The submitted tree contains documentation and has no application suite.")
    panel_status = UNJUDGED
    panel_evidence = "No verified review of the submitted files."
    if not suspect and verdict in ("survived", "refuted"):
        for result, vote in votes:
            summary = (result or {}).get("summary") or ""
            if vote == verdict and not hollow_reason(summary, paths):
                panel_status = MET if verdict == "survived" else MISSING
                panel_evidence = f"Panel verdict {verdict} on {head}. {summary[:700]}"
                break
    rows = [{**items[0], "status": MET if git_passed else MISSING, "evidence": git_evidence},
            {**items[1], "status": panel_status, "evidence": panel_evidence}]
    summary = "MET" if git_passed and panel_status == MET else "NOT MET"
    return summarize_judgments(items, rows, summary, stack, source)
