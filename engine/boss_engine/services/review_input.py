"""What a reviewer is given to judge, and how BOSS tells a review that never saw it.

Why
---
crun_86902c82 opened pull request #223: 77 files, 13,830 lines. Its reviewer answered

    No changes exist on disk or in git relative to HEAD — the working tree is clean and there
    is no diff to review. Since the task statement itself says "(no file changes detected)" …

and the run recorded ``adversarial_verdict: survived``, ``review_confidence: 1.0`` and every
review gate PASSED. The review was handed an empty change (the attempt was seeded from a saved
workspace and its agent had committed, so the live diff against HEAD was empty), went looking
with ``git diff`` against HEAD, found nothing, and its "nothing to review" was read as a pass.

Two rules, both enforced here so every reviewer seat obeys the same ones:

1. A reviewer judges THE CHANGE: every path this run's lineage changed against the branch it
   started from (seeded files and agent commits included), never the working tree against HEAD.
   :func:`build` is the one place that input is made, for the in-run panel (code review,
   adversarial, security, consistency) and for "Review this PR again" alike.
2. A review that did not see the change is not a pass. :func:`hollow_reason` says when a
   non-empty change got a review that reports nothing to review, or names none of the files.
   The seat is asked again once; a second hollow answer fails the gate as
   :data:`HOLLOW_GATE_REASON`, never ``survived``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Callable, Iterable, Optional

from . import review_comments

#: The words a failed review gate carries when the reviewer never saw the change.
HOLLOW_GATE_REASON = "review did not see the change"

#: Inlined text is capped so a large change cannot blow the reviewer's context; every changed
#: path is still LISTED, so the reviewer knows what it has not been shown and can open it.
MAX_TOTAL = 48000
MAX_PER_FILE = 12000


@dataclass
class ChangeUnderReview:
    """The change a reviewer judges: every changed path, and the text of those BOSS can read."""

    paths: list[str] = field(default_factory=list)
    files: dict[str, str] = field(default_factory=dict)
    #: Where the change was measured from, said to the reviewer in words.
    measured: str = "the branch this run started from"

    @property
    def empty(self) -> bool:
        return not self.paths

    def block(self) -> str:
        return render(self)


def build(live_files: Optional[dict] = None, changed_paths: Iterable[str] = (), *,
          read: Optional[Callable[[str], Optional[str]]] = None,
          keep: Optional[Callable[[str], bool]] = None,
          measured: str = "the branch this run started from") -> ChangeUnderReview:
    """THE ONE BUILDER of review input.

    ``live_files`` is what the live diff could read ({path: text}); ``changed_paths`` is every
    path the lineage changed (the seeded workspace, checkpointed files, the live diff). A path in
    ``changed_paths`` the live diff did not carry is read with ``read`` (from the working tree),
    so a seeded file the agent did not touch again is still in front of the reviewer. ``keep``
    drops dependencies and build output, which are not the work.
    """
    keep = keep or (lambda _p: True)
    files: dict[str, str] = {}
    for path, content in dict(live_files or {}).items():
        if keep(path) and isinstance(content, str):
            files[str(path)] = content
    paths = set(files)
    for path in changed_paths or ():
        path = str(path or "").strip()
        if not path or not keep(path):
            continue
        paths.add(path)
        if path in files or read is None:
            continue
        try:
            text = read(path)
        except Exception:  # noqa: BLE001 — an unreadable file is listed, never fatal
            text = None
        if isinstance(text, str):
            files[path] = text
    return ChangeUnderReview(paths=sorted(paths), files=files, measured=measured)


def render(change: ChangeUnderReview, *, max_total: int = MAX_TOTAL,
           max_per_file: int = MAX_PER_FILE) -> str:
    """The change as the reviewer reads it: the full list of changed paths, then their text."""
    if change.empty:
        return "(no file changes detected)"
    head = [f"{len(change.paths)} file(s) changed, measured against {change.measured}. "
            "This is the whole change, whatever `git diff HEAD` shows in the tree: the work may "
            "already be committed or carried over from an earlier attempt, so an empty "
            "`git diff` does NOT mean there is nothing to review.",
            "CHANGED FILES:"]
    head += [f"  - {p}" + ("" if p in change.files else "  (not inlined: open it in the tree)")
             for p in change.paths]
    parts, total = ["\n".join(head)], 0
    for path in change.paths:
        if path not in change.files:
            continue
        content = change.files[path] or ""
        body = content
        if len(body) > max_per_file:
            body = body[:max_per_file] + f"\n… [truncated {len(content) - max_per_file} chars]"
        block = f"\n=== FILE: {path} ===\n{body}\n"
        if total + len(block) > max_total:
            parts.append(f"\n… [further file text omitted to fit context, starting at {path}; "
                         "every changed file is listed above — open the rest in the tree]")
            break
        parts.append(block)
        total += len(block)
    return "".join(parts).strip()


def reviewed_clause() -> str:
    """The line every reviewer is asked to start with, so a review shows what it looked at."""
    return ("Start your answer with one line `REVIEWED: <the changed files you examined>`. A "
            "review that names none of the changed files is treated as one that did not see "
            "the change, and is asked again.\n\n")


#: A reviewer saying, in any of the ways models say it, that it found nothing to look at.
_NOTHING_TO_REVIEW = re.compile(
    r"(?i)("
    r"no (?:file )?changes? (?:exist|exists|detected|to review|found|were made|on disk|relative)"
    r"|nothing to review"
    r"|no diff(?: to review)?\b"
    r"|working tree is clean"
    r"|there is no (?:implementation|diff|change|code) to (?:review|evaluate|judge)"
    r"|\(no file changes detected\)"
    r"|no implementation (?:exists|to evaluate|was found)"
    r")")


def _mentions_a_changed_file(summary: str, paths: Iterable[str]) -> bool:
    text = summary or ""
    lowered = text.lower()
    for path in paths:
        p = str(path or "").strip().lower()
        if not p:
            continue
        if p in lowered:
            return True
        base = p.rsplit("/", 1)[-1]
        if len(base) >= 4 and re.search(r"(?<![\w.-])" + re.escape(base) + r"(?![\w-])", lowered):
            return True
    return False


def hollow_reason(summary: str, changed_paths: Iterable[str]) -> Optional[str]:
    """Why this review did not see the change, or None when it did.

    Only asked of a NON-EMPTY change. Hollow when the reviewer returned nothing, said there was
    nothing to review, or named none of the changed files and raised no finding. A finding is a
    sign the reviewer read something, so a review with findings is never hollow.
    """
    paths = [p for p in (changed_paths or ()) if str(p or "").strip()]
    if not paths:
        return None
    text = (summary or "").strip()
    n = len(paths)
    if not text:
        return f"the reviewer returned nothing for a change of {n} file(s)"
    if review_comments.parse_findings(text):
        return None
    if _NOTHING_TO_REVIEW.search(text):
        return (f"the reviewer said there was nothing to review, but this change has {n} "
                f"file(s)")
    if not _mentions_a_changed_file(text, paths):
        return f"the review names none of the {n} changed file(s)"
    return None


def retry_preface(reason: str, change: ChangeUnderReview) -> str:
    """Put in front of the prompt when a seat is asked again after a hollow answer."""
    listed = "\n".join(f"  - {p}" for p in change.paths[:60])
    more = f"\n  … and {len(change.paths) - 60} more" if len(change.paths) > 60 else ""
    return (f"YOUR PREVIOUS REVIEW DID NOT SEE THE CHANGE ({reason}). This change is NOT empty: "
            f"{len(change.paths)} file(s) were added or modified against {change.measured}:\n"
            f"{listed}{more}\n"
            "Review THESE files. Their text is inlined below; open any of them in the tree. "
            "Do not use `git diff HEAD` to decide what changed.\n\n")
