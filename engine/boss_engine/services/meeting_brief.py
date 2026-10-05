"""What a person asked a panel, apart from everything BOSS added to the prompt.

A panel's directive IS its prompt, so BOSS wraps the operator's ask in scaffolding every seat
must read: the panel-purpose section (the workflow's objective), the research section
(meeting_service's markers) followed by "## What was researched" and a JSON dump of the dossier, the
dossier-promotion template ("Produce a PRD for this evidence-backed opportunity …  OUTCOME TO
BUILD: …"), and [boss:…] markers. On goldeneye 4 of 22 panels showed that scaffolding in the
console's hero line, cards and drawer.

So the two are kept apart:

* new panels store the operator's own words as ``constraints.brief`` when they are convened;
* every reader goes through ``split_brief``, which returns that stored brief — or, for rows
  written before it existed, the directive with every known scaffold taken off
  deterministically — plus the injected context as readable markdown (JSON rendered as lists).

``panel_title`` does the same for titles: a dossier-promoted panel is named for its outcome
("Batch triage prototype — lead triage inbox"), never "Produce a PRD for this …".
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

#: A scaffold section header: three (or two) dashes, an upper-case name, the dashes again.
_HEADER = re.compile(r"^\s*-{2,}\s*([A-Z][A-Z0-9 '’/&,-]{3,}?)\s*-{2,}\s*$", re.M)
_ASK_HEADER = "WHAT YOU ARE BEING ASKED ABOUT"
_PURPOSE_HEADER = "WHAT THIS PANEL IS FOR"
_BOSS_MARK = re.compile(r"[ \t]*\[boss:[^\]]*\]", re.I)
_RESEARCH_PREAMBLE = re.compile(
    r"^BOSS researched this before convening you\.[^\n]*\n?", re.M)
_PROMOTE_LEAD = "Produce a PRD for this evidence-backed opportunity"
_PROMOTE_FIELD = re.compile(r"^(OUTCOME TO BUILD|WEDGE|TARGET PILOT|FIRST PROTOTYPE SLICE|VALIDATION PLAN|"
                            r"BIGGEST RISK[^:]*|KEY MARKET SIGNALS):\s*(.*)$", re.M)


def _tidy(text: str) -> str:
    text = _BOSS_MARK.sub("", text or "")
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _json_as_markdown(value: Any, depth: int = 0) -> str:
    pad = "  " * depth
    if isinstance(value, dict):
        lines = []
        for k, v in value.items():
            label = str(k).replace("_", " ").strip().capitalize()
            if isinstance(v, (dict, list)):
                lines.append(f"{pad}- **{label}**")
                lines.append(_json_as_markdown(v, depth + 1))
            elif v not in (None, ""):
                lines.append(f"{pad}- **{label}:** {v}")
        return "\n".join(x for x in lines if x)
    if isinstance(value, list):
        return "\n".join(
            _json_as_markdown(v, depth) if isinstance(v, (dict, list)) else f"{pad}- {v}"
            for v in value if v not in (None, ""))
    return f"{pad}{value}"


def readable(text: str) -> str:
    """Context as a person reads it: every JSON object or list in it rendered as a list."""
    out, i, decoder = [], 0, json.JSONDecoder()
    src = text or ""
    for m in re.finditer(r"(?m)^[ \t]*[\[{]", src):
        if m.start() < i:
            continue
        start = src.index(m.group(0).strip()[0], m.start())
        try:
            value, end = decoder.raw_decode(src, start)
            if not isinstance(value, (dict, list)):
                continue
            rendered = _json_as_markdown(value)
        except ValueError:
            # Cut short (research is clipped to fit the prompt): read it line by line.
            stop = re.search(r"(?m)^\s*(?:#{1,6}\s|-{2,}\s*[A-Z])", src[start:])
            end = start + (stop.start() if stop else len(src) - start)
            rendered = _loose_json(src[start:end])
        out.append(src[i:m.start()])
        out.append(rendered + "\n\n")
        i = end
    out.append(src[i:])
    return _tidy(_INLINE_OBJ.sub(_inline_obj, "".join(out)))


_INLINE_OBJ = re.compile(r"(?m)^([ \t]*-[ \t]*)(\{.*)$")


def _inline_obj(m: "re.Match[str]") -> str:
    """"- {"angle": "demand", "finding": "…", "citation_url": "…"}" → "- Demand: … (source)"."""
    raw = m.group(2)
    try:
        obj = json.loads(raw)
    except ValueError:
        obj = dict(re.findall(r'"([^"]+)"\s*:\s*"([^"]*)', raw))
    if not isinstance(obj, dict) or not obj:
        return m.group(1) + raw.strip("{} ")
    main = next((str(obj[k]) for k in ("finding", "claim", "outcome", "title", "text") if obj.get(k)), "")
    tag = str(obj.get("angle") or obj.get("verdict") or "").strip()
    url = str(obj.get("citation_url") or obj.get("url") or "").strip()
    if not main:
        main = " · ".join(f"{k.replace('_', ' ')}: {v}" for k, v in obj.items() if isinstance(v, (str, int, float)))
    return f"{m.group(1)}{tag.capitalize() + ': ' if tag else ''}{main}{f' ({url})' if url else ''}"


def _loose_json(block: str) -> str:
    """JSON that does not parse (clipped mid-string) as a list: keys bold, strings bulleted."""
    lines = []
    for raw in block.splitlines():
        line = raw.strip().rstrip(",")
        if not line or line in "{}[]":
            continue
        key = re.match(r'^"([^"]+)"\s*:\s*(.*)$', line)
        if key:
            label = key.group(1).replace("_", " ").strip().capitalize()
            rest = key.group(2).strip().rstrip(",").strip()
            if rest in ("[", "{", ""):
                lines.append(f"- **{label}**")
            else:
                lines.append(f"- **{label}:** {rest.strip(chr(34))}")
            continue
        lines.append(f"  - {line.strip(chr(34)).strip()}")
    return "\n".join(lines)


def _promote_parts(directive: str) -> Optional[tuple[str, str]]:
    """A dossier-promotion directive → (the outcome as a brief, the evidence as context)."""
    if not directive.lstrip().startswith(_PROMOTE_LEAD):
        return None
    fields = {k.split(" (")[0].upper(): v.strip() for k, v in _PROMOTE_FIELD.findall(directive)}
    outcome = fields.get("OUTCOME TO BUILD", "")
    pilot = fields.get("TARGET PILOT", "")
    brief = outcome + (f"\nWho it is for: {pilot}" if pilot and pilot != "(n/a)" else "")
    start = directive.find("OUTCOME TO BUILD:")
    context = directive[start:] if start >= 0 else directive
    return brief, context


def split_brief(directive: Optional[str], stored: Optional[str] = None) -> dict:
    """{"brief": the operator's own words, "context": what BOSS added, as readable markdown}."""
    raw = str(directive or "")
    promoted = _promote_parts(raw)
    if promoted:
        brief, context = promoted
    else:
        headers = list(_HEADER.finditer(raw))
        if not headers:
            brief, context = raw, ""
        else:
            ask = next((h for h in headers if _ASK_HEADER in h.group(1).upper()), None)
            # The ask runs from its header (or the start, when there is none) to the next header.
            begin = ask.end() if ask else 0
            nxt = next((h for h in headers if h.start() >= begin and h is not ask), None)
            if not ask and headers[0].start() == 0 and _PURPOSE_HEADER in headers[0].group(1).upper():
                begin, nxt = len(raw), None  # all purpose, no ask
            brief = raw[begin: nxt.start() if nxt else len(raw)]
            ctx_parts = []
            for h_i, h in enumerate(headers):
                if h is ask:
                    continue
                stop = headers[h_i + 1].start() if h_i + 1 < len(headers) else len(raw)
                body = _RESEARCH_PREAMBLE.sub("", raw[h.end():stop])
                title = h.group(1).strip().capitalize()
                ctx_parts.append(f"### {title}\n\n{body.strip()}")
            context = "\n\n".join(ctx_parts)
    out_brief = _tidy(stored) if str(stored or "").strip() else _tidy(brief)
    return {"brief": out_brief, "context": readable(context) if context.strip() else ""}


def outcome_short_name(outcome: Optional[str], limit: int = 60) -> str:
    """"A narrow-scope 'batch triage' prototype: user pastes …" → "Narrow-scope batch triage prototype"."""
    text = re.split(r"\s*[:;(]|\s+[—–-]\s+|,\s", str(outcome or "").strip(), maxsplit=1)[0]
    text = re.sub(r"^(?:a|an|the)\s+", "", text, flags=re.I)
    text = re.sub(r"['\"‘’“”`]", "", text).strip(" .")
    if len(text) > limit:
        cut = text.rfind(" ", 0, limit)
        text = text[: cut if cut > limit // 2 else limit].rstrip()
    return (text[:1].upper() + text[1:]) if text else ""


def promoted_title(outcome: Optional[str], dossier_title: Optional[str]) -> str:
    """A dossier-promoted panel's name: its outcome, then the idea it came from."""
    name = outcome_short_name(outcome) or "Outcome"
    idea = re.split(r"[.\n]", str(dossier_title or "").strip())[0].strip()
    idea = re.sub(r"^(?:build|make|create)\s+(?:a|an|the)\s+", "", idea, flags=re.I)
    if idea:
        idea = idea[:1].lower() + idea[1:] if not idea[:2].isupper() else idea
        return f"{name} — {idea}"
    return name


def panel_title(title: Optional[str], directive: Optional[str]) -> Optional[str]:
    """A title fit to show: an old dossier-promotion boilerplate title is renamed for its outcome.
    Returns None when the stored title is already fine."""
    t = str(title or "")
    if _PROMOTE_LEAD.lower()[:30] in t.lower() or t.lower().startswith(("build: ", "build outcome from")):
        promoted = _promote_parts(str(directive or ""))
        if promoted:
            seed = re.search(r'on the seed: "([^"]*)"', str(directive or ""))
            idea = re.split(r"[.\n]", seed.group(1) if seed else "")[0]
            return promoted_title(promoted[0].split("\n")[0], idea)
        if t.lower().startswith("build: "):
            return outcome_short_name(t[7:]) or None
    return None
