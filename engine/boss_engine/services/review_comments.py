"""Review findings anchored to a file and a line, the way a person reviews a pull request.

Why
---
The reviewer's contract was ``FINDING: <SEVERITY> — <prose>``. Severity graded the gate
correctly, but the prose went back to the implementer with no idea WHERE. A reviewer that can
only say "error handling is weak" hands the implementer a search problem on top of a fix, and on
a thirty-hunk change the implementer guesses — which is how a repair round is spent re-reading
the diff instead of changing it.

A human reviewing that same change points at line 47. That is the whole difference, and it is
the difference between a review that improves the code and one that merely blocks it.

So a finding may now carry ``path:line``:

    FINDING: MAJOR inventory/pricing/rules.py:14 — an unknown SKU returns 1.0 instead of raising

The anchor is OPTIONAL and the old shape still parses, because a model that forgets the anchor
must degrade to the previous behaviour rather than have its finding dropped. A dropped finding
is a silent quality regression; an unanchored one is merely a worse comment.

What this module is not
-----------------------
This is not product feedback about BOSS (``send_feedback``), and it is not the approve/reject
Decision. It is the review conversation about one change: who said what, about which line, and
whether it was dealt with.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from pathlib import Path
import re
from typing import Iterable, Optional

#: Index = how blocking. Kept identical to the pipeline's ladder on purpose: two severity
#: scales that drift apart would make the gate and the comments disagree about the same finding.
SEVERITY_ORDER = ["nit", "minor", "major", "blocker"]

#: Who wrote a comment. The pipeline's whole premise is that the author of something does not
#: approve it, so the author of a comment is evidence, not decoration.
AUTHOR_AGENT = "agent"
AUTHOR_HUMAN = "human"

STATUS_OPEN = "open"
STATUS_ADDRESSED = "addressed"
STATUS_DISMISSED = "dismissed"
STATUSES = (STATUS_OPEN, STATUS_ADDRESSED, STATUS_DISMISSED)

#: `FINDING: MAJOR path/to/file.py:42 — text`, with the anchor optional.
#:
#: WRITTEN FOR WHAT MODELS ACTUALLY EMIT, not for the contract as stated. Measured against a
#: real reviewer (deepseek-chat, brownfield fixture): it produced correct, well-anchored
#: findings as
#:
#:     1. **FINDING: MINOR inventory/pricing/rules.py:8** — the tiers are ordered smallest-first
#:
#: and the original expression matched NONE of them, because it required the line to begin with
#: `FINDING:`. Every finding was dropped, the gate fell back to the binary verdict, and the
#: verdict was SURVIVED — so a graded review became a silent pass. A parser that only accepts
#: the contract as written is a parser that rejects the reviewers it exists to read.
#:
#: The path is deliberately permissive (anything non-space containing a dot or a slash) so a
#: reviewer naming `src/a.ts:12` and one naming `a.ts:12` both parse. Resolution to a real file
#: happens later, where the tree is available and a miss can be reported.
_EMPH = r"(?:\*\*|__|\*|`)*"
_FINDING_RE = re.compile(
    r"(?im)^"
    r"[\s>]*"                          # quoting / indentation
    r"(?:[-*+]\s*)*"                   # bullet
    r"(?:\d+[.)]\s*)?"                # "1." / "2)" numbered list
    + _EMPH +                          # **FINDING
    r"\s*FINDING\s*:\s*" + _EMPH +
    r"\s*(?P<sev>BLOCKER|MAJOR|MINOR|NIT)\b" + _EMPH +
    r"(?:[\s:]+" + _EMPH +
    r"(?P<path>[^\s:*`][^\s*`]*?[./][^\s:*`]*?):(?P<line>\d+)(?:-(?P<end>\d+))?"
    + _EMPH + r")?"
    + _EMPH +
    r"[\s—–:-]*"
    r"(?P<text>.*?)$"
)


@dataclass
class ReviewComment:
    """One remark about one place in a change."""

    severity: str
    body: str
    path: Optional[str] = None
    line: Optional[int] = None
    end_line: Optional[int] = None
    author: str = "adversarial_reviewer"
    author_kind: str = AUTHOR_AGENT
    status: str = STATUS_OPEN
    #: Set when the comment came from a review stage, so a repair can say which pass raised it.
    stage: Optional[str] = None

    @property
    def anchored(self) -> bool:
        return bool(self.path) and self.line is not None

    @property
    def blocking_rank(self) -> int:
        try:
            return SEVERITY_ORDER.index(self.severity)
        except ValueError:
            return SEVERITY_ORDER.index("major")

    def location(self) -> str:
        if not self.anchored:
            return "(no location given)"
        if self.end_line and self.end_line != self.line:
            return f"{self.path}:{self.line}-{self.end_line}"
        return f"{self.path}:{self.line}"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict) -> "ReviewComment":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in (raw or {}).items() if k in known})


def _clean(text: Optional[str]) -> str:
    """Drop the markdown a model wraps its prose in, keeping the prose.

    Emphasis markers are stripped; BACKTICKS ARE NOT. A leading backtick is almost always
    inline code that opens the sentence — "`rate` is wrong" — and eating it turns a precise
    comment into a broken one on the pull request where people read it.
    """
    out = (text or "").strip()
    out = re.sub(r"^(?:\*\*|__|\*)+", "", out)
    out = re.sub(r"(?:\*\*|__)+$", "", out)
    return out.strip(" —–:-").strip()


def parse_findings(
    summary: str, *, author: str = "adversarial_reviewer", stage: Optional[str] = None,
) -> list[ReviewComment]:
    """Read a reviewer's FINDING lines into comments.

    An empty list means the reviewer emitted NO structured signal. It never means "clean" —
    callers fall back to the binary verdict, which is the fail-closed path.
    """
    out: list[ReviewComment] = []
    for m in _FINDING_RE.finditer(summary or ""):
        line = int(m.group("line")) if m.group("line") else None
        end = int(m.group("end")) if m.group("end") else None
        # A backwards range is a typo, not an instruction to invert the span.
        if line and end and end < line:
            end = None
        out.append(ReviewComment(
            severity=m.group("sev").lower(),
            body=_clean(m.group("text"))[:500],
            path=(m.group("path") or None),
            line=line,
            end_line=end,
            author=author,
            author_kind=AUTHOR_AGENT,
            stage=stage,
        ))
    return out


def resolve_path(root, path: Optional[str]) -> Optional[Path]:
    """Find the file a reviewer named, tolerating a shortened path.

    Reviewers name `rules.py` when the file is `inventory/pricing/rules.py`. Refusing that
    anchor would punish the reviewer for being readable; matching it by suffix keeps the comment
    useful. An ambiguous suffix resolves to nothing rather than to a guess — pointing the
    implementer at the WRONG line is worse than giving it no line at all.
    """
    if not path or root is None:
        return None
    base = Path(root)
    direct = base / path
    if direct.is_file():
        return direct
    want = path.strip("/")
    matches = [
        p for p in base.rglob("*")
        if p.is_file() and p.as_posix().endswith("/" + want)
        and not any(part in {".git", "node_modules", "__pycache__", ".venv"} for part in p.parts)
    ]
    return matches[0] if len(matches) == 1 else None


def quote_source(root, comment: ReviewComment, context: int = 3) -> str:
    """The code the comment is about, with line numbers and a marker on the line itself.

    Quoting is the point. The implementer gets the reviewer's words AND the text they were
    written about, so it does not have to re-derive which line was meant — and if the file has
    moved on since the review, the mismatch is visible instead of silently misapplied.
    """
    target = resolve_path(root, comment.path)
    if target is None or comment.line is None:
        return ""
    try:
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    if not lines:
        return ""
    first = max(1, comment.line - context)
    last = min(len(lines), (comment.end_line or comment.line) + context)
    if comment.line > len(lines):
        return (f"    (this file now has {len(lines)} lines; the review pointed at line "
                f"{comment.line}, so the code has moved — re-read it before changing anything)")
    span = range(comment.line, (comment.end_line or comment.line) + 1)
    return "\n".join(
        f"    {'>' if n in span else ' '} {n:>5} | {lines[n - 1]}"
        for n in range(first, last + 1)
    )


def render_for_implementer(comments: Iterable[ReviewComment], root=None) -> str:
    """The review, as instructions an implementer can act on without hunting.

    Ordered most blocking first: a repair round is bounded, so what it spends its turns on
    should be decided here rather than by whatever the model reads first.
    """
    items = [c for c in comments if c.status == STATUS_OPEN]
    if not items:
        return ""
    items.sort(key=lambda c: (-c.blocking_rank, c.path or "", c.line or 0))
    blocks = ["REVIEW COMMENTS — each is a specific place in the change. Address every one, or "
              "say why it is wrong.\n"]
    for i, c in enumerate(items, start=1):
        who = "a reviewer" if c.author_kind == AUTHOR_AGENT else "the operator"
        head = f"{i}. [{c.severity.upper()}] {c.location()} — {c.body}"
        blocks.append(head + f"\n    (raised by {who}"
                      + (f", {c.stage}" if c.stage else "") + ")")
        quoted = quote_source(root, c, context=3)
        if quoted:
            blocks.append(quoted)
        blocks.append("")
    return "\n".join(blocks)


def to_github_comments(comments: Iterable[ReviewComment]) -> list[dict]:
    """Anchored comments as GitHub review-comment payloads.

    Only anchored ones: GitHub rejects a review comment with no path+line, and silently
    dropping the rest would hide findings, so callers put the unanchored ones in the review body.
    """
    out = []
    for c in comments:
        if not c.anchored:
            continue
        item = {
            "path": c.path,
            "line": c.end_line or c.line,
            "body": f"**{c.severity.upper()}** — {c.body}",
        }
        if c.end_line and c.end_line != c.line:
            item["start_line"] = c.line
        out.append(item)
    return out


def unanchored_body(comments: Iterable[ReviewComment]) -> str:
    """The findings that could not be pinned to a line, for the review's summary body."""
    loose = [c for c in comments if not c.anchored]
    if not loose:
        return ""
    return "\n".join(f"- **{c.severity.upper()}** — {c.body}" for c in loose)
