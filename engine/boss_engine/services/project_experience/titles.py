"""THE SHORT HUMAN TITLE of a piece of work: one helper for every surface (Recent work, the
Running tile, Work rows, Roadmap, Needs you, This week).

A run's `title` was the first 80 characters of its brief ("Finish the visual refresh the last
change promised but did not deliver. The tick"), and the page cut it again to six words
("Finish the visual refresh the last"): a sentence chopped mid-thought, everywhere. A title is
a NAME, so it is made once, by rule, from the brief's lead clause:

  1. the work's own name first: stacked "Follow-up: … —" leads and "· retry N" tails off
     (work.clean_title, the Work tab's grouping reads the same);
  1b. a KIND LABEL lead is not the name: "Bug: weekend tickets breach SLA…", "[Feature] …",
     "Ticket - …" name the work after what it says, never "Bug" (a one-word lead, or a lead made
     only of kind words such as bug, feature, ticket, fix, chore, minor feature, P1);
  2. the first sentence, then the head of it before a colon, a dash, a semicolon or a bracket
     ("Add SLA timers to tickets: each priority gets…" → "Add SLA timers to tickets");
  3. still long: cut at the first conjunction that starts a second thought (" and let ",
     " but ", " so ", " because ", " which ", " that ", " and ") and then at a second
     article ("Finish the visual refresh | the last change promised");
  4. at most `limit` words, never ending on a joining word ("to", "the", "and", …).

A stored short title (`input.short_title`, or a title that is not a cut of the brief) wins:
`short_title(title, brief)` keeps a real title and only shortens a brief. Deterministic and
cheap (no model call), so every surface draws the same words for the same work.
"""
from __future__ import annotations

import re
from typing import Any, Optional

#: words a title never ends on.
_JOINERS = frozenset("a an the to of and or for with in on at by from as into via but so that "
                     "which who is are be it its this these those our your their my".split())
_ARTICLES = frozenset({"the", "a", "an"})
_HEADS = re.compile(r"\s*(?::\s|\s[—–-]\s|;|\(|\s\d\)\s)")
_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9])|\n+")
#: words that only say what KIND of work it is (a label lead, never a title on its own).
_KIND_WORDS = frozenset("bug bugs bugfix fix hotfix feature feat features ticket tickets issue task "
                        "chore story epic request idea enhancement improvement todo note minor major "
                        "new small quick report urgent high low medium priority p0 p1 p2 p3 "
                        "refactor docs doc question".split())
_LABEL_LEAD = re.compile(r"^\s*(?:\[([^\]]{1,30})\]|\(([^)]{1,30})\)|([A-Za-z0-9][\w /&+-]{0,30}?)"
                         r"\s*(?::|\s[—–-])\s)\s*")
_CLAUSES = (" and let ", " but ", " so that ", " so ", " because ", " which ", " that ",
            " and then ", " then ", " and ")


def _clean(title: Any) -> str:
    try:
        from .work import clean_title
    except ImportError:  # pragma: no cover — the Work tab's helper is always there
        return " ".join(str(title or "").split())
    return " ".join(clean_title(title).split())


def strip_label(text: str) -> str:
    """Drop a kind-label lead ("Bug:", "[Feature]", "Minor feature -", "P1:") so the title comes
    from what the brief SAYS. A one-word lead before a colon is a label too ("Frontend: …").
    Kept when nothing meaningful follows it."""
    t = text
    for _ in range(3):
        m = _LABEL_LEAD.match(t)
        if not m:
            break
        lead = (m.group(1) or m.group(2) or m.group(3) or "").strip()
        words = [w.lower() for w in re.split(r"[\s/&+-]+", lead) if w]
        rest = t[m.end():].strip()
        bracketed = bool(m.group(1) or m.group(2))
        is_label = bool(words) and (all(w in _KIND_WORDS for w in words)
                                    or (len(words) == 1 and not bracketed and ":" in t[:m.end()])
                                    or (bracketed and len(words) <= 2))
        if not is_label or len(rest.split()) < 2:
            break
        t = rest
    return t


def _trim(words: list[str], limit: int) -> list[str]:
    out = words[:limit]
    while len(out) > 1 and out[-1].lower().strip(",.;:") in _JOINERS:
        out = out[:-1]
    return out


def _tidy(text: str) -> str:
    text = text.strip().strip(",.;:!?-—–\"' ")
    return text[:1].upper() + text[1:] if text else text


def shorten(text: Any, limit: int = 6) -> str:
    """A short title from a brief or a long title (rules 2–4)."""
    raw = strip_label(_clean(text))
    if not raw:
        return ""
    first = _SENTENCE.split(raw, maxsplit=1)[0].strip()
    head = _HEADS.split(first, maxsplit=1)[0].strip() or first
    head = head.rstrip(".!?")
    if len(head.split()) <= limit:
        return _tidy(head)
    lower = head.lower()
    for clause in _CLAUSES:
        at = lower.find(clause)
        if at > 0 and len(head[:at].split()) >= 2:
            head, lower = head[:at], head[:at].lower()
            if len(head.split()) <= limit:
                return _tidy(head)
    words = head.split()
    seen_article = False
    for i, w in enumerate(words):
        if w.lower() in _ARTICLES:
            if seen_article and i >= 3:
                words = words[:i]
                break
            seen_article = True
    if "," in " ".join(words) and len(words) > limit:
        before = " ".join(words).split(",", 1)[0].split()
        if len(before) >= 2:
            words = before
    return _tidy(" ".join(_trim(words, limit)))


def _is_cut_of(title: str, brief: str) -> bool:
    """True when `title` is just the start of `brief` (the old 80-character cut)."""
    t = " ".join(title.split()).rstrip(" .…")
    b = " ".join(brief.split())
    return bool(t) and len(b) > len(t) and b.startswith(t)


def short_title(title: Any, brief: Any = None, *, stored: Any = None, limit: int = 6) -> str:
    """THE helper: a stored short title wins; a real title is kept (cleaned) when it is short;
    a title that is a cut of the brief, or a long one, is shortened by rule."""
    kept = _clean(stored)
    if kept:
        return _tidy(kept) if len(kept.split()) <= limit + 2 else shorten(kept, limit)
    t = strip_label(_clean(title))
    b = _clean(brief)
    if t and not (b and _is_cut_of(t, b)) and len(t.split()) <= limit and not re.search(
            r"[.:;]\s", t):
        if not (b and t.lower().strip(" :") in _KIND_WORDS):
            return _tidy(t)
    # A title that is only the start of the brief (or only a kind word, "Bug") is named from
    # the brief itself.
    if b and (not t or _is_cut_of(t, b) or t.lower().strip(" :") in _KIND_WORDS):
        return shorten(b, limit)
    return shorten(t or b, limit)


def title_fields(title: Any, brief: Any = None, *, stored: Any = None) -> dict[str, Optional[str]]:
    """{label, title}: the short name every surface shows, and the full text for the drawer."""
    full = _clean(title) or _clean(brief) or None
    return {"label": short_title(title, brief, stored=stored) or full, "title": full}


__all__ = ["shorten", "short_title", "strip_label", "title_fields"]
