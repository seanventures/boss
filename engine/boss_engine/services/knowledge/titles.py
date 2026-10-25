"""A RUN'S TITLE, CUT AT A WORD (tester round 6).

A run (a workflow run, a build, a conversation) is named by its title, at most ``RUN_TITLE_MAX``
characters. The cut was ``title[:89] + "…"``, which split a word ("…transaction processors (retry
with verif…"), and run nodes in the graph keep the name they were given. ``word_cut`` is the one
builder; ``repair_cut`` mends a stored name the old builder cut, on read.
"""
from __future__ import annotations

RUN_TITLE_MAX = 90
_TRAIL = " \t-–—_.,;:/(["


def word_cut(text: str, limit: int = RUN_TITLE_MAX) -> str:
    """``text`` whole when it fits; else cut at the last word that fits, with "…", in fewer than
    ``limit`` characters in all. A single word longer than most of the room is cut by character."""
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    # One short of the old builder's length, so a title cut here never looks like one it cut.
    room = max(1, limit - 2)
    head = text[:room]
    if text[room] != " ":
        at = head.rfind(" ")
        # Never down to a stub: a cut that would keep under 40% of the room cuts the word instead.
        if at >= room * 0.4:
            head = head[:at]
    return head.rstrip(_TRAIL) + "…"


def repair_cut(name: str, limit: int = RUN_TITLE_MAX) -> str:
    """A stored run name the old builder cut mid-word, mended: ``<title> · <when>`` whose title is
    exactly ``limit`` characters ending "<letter>…" loses its partial word. Anything else — a
    whole title, one ``word_cut`` made — is returned as it is."""
    name = str(name or "")
    title, sep, when = name.rpartition(" · ")
    if not sep:
        title, when = name, ""
    if len(title) != limit or not title.endswith("…") or not title[-2].isalnum():
        return name
    body = title[:-1]
    at = body.rfind(" ")
    if at < len(body) * 0.4:
        return name
    mended = body[:at].rstrip(_TRAIL) + "…"
    return f"{mended}{sep}{when}" if sep else mended
