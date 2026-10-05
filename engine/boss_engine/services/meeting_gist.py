"""A panel turn's gist: a headline and up to three key points, plain text.

A seat's reply is raw markdown — headings, bold, tables, numbered lists, a page or more. The
meeting room shows each turn as a card: the seat's face, ONE headline, three points and "Read
all". Cutting that card out of the markdown in the browser gave every card a different cut (a
table row as the headline, "**Priority** | **Why**" as a point), so the gist is made where the
turn is made and stored beside it.

Two sources, one shape (`{"headline": str, "points": [str, ...]}`):

1. The seat writes it. `GIST_INSTRUCTION` asks for one closing `GIST: {...}` line after the
   reply; `split_reply_gist` takes it off the reply and validates it.
2. When there is none — a meeting held before gists existed, a seat that ignored the ask, a
   simulated fixture — `extract_gist` cuts one deterministically: the first heading (else the
   first meaningful line) and the meaningful lines after it, with markdown and tables stripped.
"""
from __future__ import annotations

import json
import re
from typing import Optional

HEADLINE_MAX = 120
POINT_MAX = 140
MAX_POINTS = 3

#: Appended to each seat's prompt in rounds 1 and 2.
GIST_INSTRUCTION = (
    "\n\nAfter your full reply, end with ONE final line, exactly in this form and nothing after "
    'it:\nGIST: {"headline": "<your position in one plain sentence, under 100 characters>", '
    '"points": ["<key point, under 120 characters>", "<key point>", "<key point>"]}\n'
    "Plain text inside the GIST — no markdown, at most three points."
)

_GIST_LINE = re.compile(r"^[ \t>*_`]*GIST[ \t*_`]*:[ \t]*(\{.*\})[ \t`*_]*$")
_FENCE = re.compile(r"```.*?```", re.S)
_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+")
_BOLD_LINE = re.compile(r"^\s*(\*\*|__)(.+?)(\*\*|__)\s*:?\s*$")
_BULLET = re.compile(r"^\s*(?:[-*+•▪◦]|\d+[.)]|[0-9]️?⃣)\s+")
_KEYCAP = re.compile(r"[0-9#*]️?⃣")
_MARKS = re.compile(r"[*_`>#~]")
_ONLY_PUNCT = re.compile(r"^[\s\-—–:|=.·•]*$")


def _clip(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= limit:
        return text
    cut = text.rfind(" ", 0, limit - 1)
    return text[: cut if cut > limit // 2 else limit - 1].rstrip(" ,;:-–—") + "…"


def _plain(line: str) -> str:
    """One markdown line as plain text: no heading marks, bullets, emphasis, links or emoji keys."""
    line = _LINK.sub(r"\1", line)
    line = _HEADING.sub("", line)
    line = _BULLET.sub("", line)
    line = _KEYCAP.sub("", line)
    line = _MARKS.sub("", line)
    line = line.replace(" ", " ").replace(" ", " ")
    return re.sub(r"\s+", " ", line).strip(" \t-–—:•")


def _is_table(line: str) -> bool:
    s = line.strip()
    return s.startswith("|") or s.count("|") >= 2


def _clean(gist: object) -> Optional[dict]:
    """A gist as stored: a non-empty plain headline and 0-3 plain points, or None."""
    if not isinstance(gist, dict):
        return None
    headline = _clip(_plain(str(gist.get("headline") or "")), HEADLINE_MAX)
    if not headline:
        return None
    raw = gist.get("points")
    points = []
    for p in raw if isinstance(raw, list) else []:
        text = _clip(_plain(str(p or "")), POINT_MAX)
        if text and text != headline:
            points.append(text)
        if len(points) == MAX_POINTS:
            break
    return {"headline": headline, "points": points}


def split_reply_gist(reply: Optional[str]) -> tuple[str, Optional[dict]]:
    """The reply without its closing GIST line, and the gist it carried (None when it had none
    or it did not parse). Only the LAST non-blank line is read — a reply that mentions "GIST:"
    earlier keeps it. Never raises: a malformed line stays in the reply untouched."""
    text = reply or ""
    lines = text.splitlines()
    end = len(lines)
    # A model that fenced its closing line: step over the fence and blank lines.
    while end and (not lines[end - 1].strip() or lines[end - 1].strip() == "```"):
        end -= 1
    if not end:
        return text, None
    m = _GIST_LINE.match(lines[end - 1].strip())
    if not m:
        return text, None
    try:
        gist = _clean(json.loads(m.group(1)))
    except (ValueError, TypeError):
        return text, None
    if gist is None:
        return text, None
    body = lines[: end - 1]
    while body and body[-1].strip() in ("", "---"):
        body.pop()
    # The fence the model opened around its GIST line — not one that closes a real code block.
    fences = sum(1 for line in body if line.strip().startswith("```"))
    if body and body[-1].strip() == "```" and fences % 2 == 1:
        body.pop()
    return "\n".join(body).rstrip(), gist


def extract_gist(markdown: Optional[str], points: int = MAX_POINTS) -> dict:
    """The deterministic gist: first heading (else first meaningful line) as the headline, then
    the next meaningful lines as points. Code blocks, tables and rules are skipped."""
    text = _FENCE.sub("\n", markdown or "")
    lines: list[tuple[str, bool]] = []  # (plain text, was a heading)
    for raw in text.splitlines():
        if not raw.strip() or _is_table(raw):
            continue
        heading = bool(_HEADING.match(raw) or _BOLD_LINE.match(_BULLET.sub("", raw)))
        plain = _plain(raw)
        if len(plain) <= 3 or _ONLY_PUNCT.match(plain):
            continue
        lines.append((plain, heading))
    if not lines:
        return {"headline": "", "points": []}
    head_at = next((i for i, (_t, h) in enumerate(lines) if h), 0)
    # A heading buried deep in the reply is not its headline; the opening line is.
    if head_at > 2:
        head_at = 0
    headline = _clip(lines[head_at][0], HEADLINE_MAX)
    # Points are what the seat SAID, so body lines come first; its sub-headings ("Core
    # priorities", "Where I disagree") only fill in when the body runs short.
    rest = lines[head_at + 1:]
    body = [t for t, h in rest if not h and len(t) >= 20]
    heads = [t for t, h in rest if h or len(t) < 20]
    out: list[str] = []
    for plain in body + heads:
        if len(plain) < 8:
            continue
        clipped = _clip(plain, POINT_MAX)
        if clipped != headline and clipped not in out:
            out.append(clipped)
        if len(out) >= points:
            break
    return {"headline": headline, "points": out}


def gists_for(turns: Optional[dict], stored: Optional[dict] = None) -> dict:
    """Every seat's gist for one round: the stored one when the seat wrote it, else extracted."""
    out: dict[str, dict] = {}
    stored = stored if isinstance(stored, dict) else {}
    for seat, reply in (turns or {}).items():
        gist = _clean(stored.get(seat))
        out[seat] = gist or extract_gist(str(reply or ""))
    return out


def with_gists(turns: dict) -> tuple[dict, dict]:
    """A round as the model returned it → (replies without their GIST lines, each seat's gist)."""
    replies: dict[str, str] = {}
    gists: dict[str, dict] = {}
    for seat, reply in (turns or {}).items():
        body, gist = split_reply_gist(reply)
        replies[seat] = body
        gists[seat] = gist or extract_gist(body)
    return replies, gists


def room_gists(round1: Optional[dict], round2: Optional[dict], synthesis: Optional[str],
               stored1: Optional[dict] = None, stored2: Optional[dict] = None) -> dict:
    """The gist fields a meeting-room record carries: every seat's, both rounds, and the
    facilitator's synthesis (five points — it reconciles the whole panel)."""
    return {
        "round1_gists": gists_for(round1, stored1),
        "round2_gists": gists_for(round2, stored2),
        "synthesis_gist": extract_gist(synthesis, points=5) if synthesis else None,
    }
