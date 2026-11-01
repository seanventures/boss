"""A scripted Rulebook Analyst: plays the ``rulebook_analyst`` role with no model.

The conformance engine for the ``extract_rulebook_rules`` step (rulebook source G5): it answers
exactly the prompt the real role gets — the role file and a batch of ``[S<n>]`` sections — by
READING THE SECTIONS' TEXT, the way a careful model would: a section whose first line is a rule
card ("<id> v<n> — <title>") becomes one rule, every field taken from its labelled lines. It never
looks at ``data.py``; what it answers comes from the PDF's text through BOSS's own sectioning, so
measuring it against ``answer_key()`` measures the pipeline (reader, sections, prompt, parsing,
validation, graph writes), not a copy of the key.

Used by engine/tests/test_rulebook_extraction.py and, with ``--scripted``,
scripts/kg/measure_rulebook_extraction.py.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

MARK_OPEN = "=== JUDGEMENT ==="
MARK_CLOSE = "=== END JUDGEMENT ==="

_SECTION = re.compile(r"^\[S(?P<n>\d+)\] (?P<name>.*?)(?: \(p\. \d+\))?$", re.MULTILINE)
_CARD = re.compile(r"^(?P<id>[A-Z][A-Z0-9]*(?:-[A-Z0-9]+){2,})(?:\s+v(?P<v>\d+))?\s+[—–]\s+(?P<title>.+)$")
_LABELS = ("Statement", "Dimension", "CDE", "Applies to", "Check", "Warning when", "Error when",
           "Regulatory reach", "Legal reference", "Owner", "Handling")
_LABEL = re.compile(r"^(?P<label>" + "|".join(re.escape(x) for x in _LABELS) + r"):\s*(?P<rest>.*)$")
_RULE_ID = re.compile(r"\b[A-Z]{2,}-[A-Z]{2,}-[A-Z]{2}-\d{3}\b")
_CDE = re.compile(r"\bCDE-[A-Z]{2}-\d{3}\b")
_MDRM = re.compile(r"\(([A-Z]{4}[A-Z0-9]{4})\)")
_NUMBER = re.compile(r"(\d[\d,]*(?:\.\d+)?)")
_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
          "nine": 9, "ten": 10}


def sections_of(prompt: str) -> list[tuple[str, str]]:
    """(``S<n>``, the section's text) for every section the prompt carries."""
    marks = list(_SECTION.finditer(prompt))
    out = []
    for i, mark in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(prompt)
        body = prompt[mark.end():end].strip("\n")
        out.append((f"S{mark.group('n')}", body))
    return out


def _fields(lines: list[str]) -> tuple[dict[str, str], dict[str, str]]:
    """Labelled fields (wrapped lines joined with a space) and each field's first physical line."""
    fields: dict[str, str] = {}
    first: dict[str, str] = {}
    current: Optional[str] = None
    for line in lines:
        found = _LABEL.match(line.strip())
        if found:
            current = found.group("label")
            fields[current] = found.group("rest").strip()
            first[current] = line.strip()
        elif current:
            # A wrapped line continues the field; a word broken at a hyphen ("CDE-" / "TX-004")
            # joins without a space.
            glue = "" if fields[current].endswith("-") else " "
            fields[current] = f"{fields[current]}{glue}{line.strip()}".strip()
    return fields, first


def threshold(words: str) -> dict[str, Any]:
    """A Warning/Error condition's metric and the first number in it."""
    text = " ".join(words.split())
    low = text.lower()
    # "more than one active loan" is 1; "the two totals" is no threshold.
    spelled = re.sub(r"\bmore than (" + "|".join(_WORDS) + r")\b",
                     lambda m: f"more than {_WORDS[m.group(1)]}", low)
    number = _NUMBER.search(spelled)
    value: Optional[float] = float(number.group(1).replace(",", "")) if number else None
    if "%" in text:
        metric = "relative_diff" if "differ" in low else "rate"
        value = round(value / 100, 10) if value is not None else None
    elif "usd" in low:
        metric = "amount_usd"
    elif "deadline" in low or "minute" in low:
        metric = "minutes"
        value = value if ("minute" in low and value is not None) else 0.0
    else:
        metric = "count"
        value = 0.0 if low.startswith("any ") or value is None else value
    return {"text": text, "metric": metric, "value": value}


def rule_of(section: str, body: str) -> Optional[dict[str, Any]]:
    lines = [ln for ln in body.split("\n") if ln.strip()]
    if not lines:
        return None
    card = _CARD.match(lines[0].strip())
    if not card:
        return None
    fields, first = _fields(lines[1:])
    if "Check" not in fields:
        return None
    dimension = re.search(r"Dimension:\s*(\w+)", first.get("Dimension", ""))
    severity = re.search(r"Severity:\s*(\w+)", first.get("Dimension", ""))
    system = re.search(r"System:\s*(S-[A-Z]+)", first.get("Dimension", ""))
    feed = re.search(r"Feed:\s*(F-\d+)", first.get("Dimension", ""))
    reach = fields.get("Regulatory reach", "")
    lines_reached = [] if reach.lower().startswith("none") else [
        " ".join(part.split()).rstrip(".") for part in reach.split(";") if part.strip()]
    owner_text = fields.get("Owner", "")
    owner = owner_text.split(" (")[0].split(".")[0].strip()
    related_text = owner_text.split("Related:", 1)[1] if "Related:" in owner_text else ""
    columns = [c.strip().rstrip(".") for c in fields.get("Applies to", "").split(",") if c.strip()]
    legal = fields.get("Legal reference")
    return {
        "section": section, "rule_id": card.group("id"),
        "version": int(card.group("v")) if card.group("v") else None,
        "title": card.group("title").strip(), "statement": fields.get("Statement", ""),
        "dimension": dimension.group(1).lower() if dimension else None,
        "severity": severity.group(1) if severity else None,
        "system": system.group(1) if system else None, "feed": feed.group(1) if feed else None,
        "cdes": _CDE.findall(fields.get("CDE", "")), "columns": columns,
        "tables": sorted({c.rsplit(".", 1)[0] for c in columns if "." in c}),
        "check": fields.get("Check", ""),
        "warning": threshold(fields.get("Warning when", "")) if "Warning when" in fields else None,
        "error": threshold(fields.get("Error when", "")) if "Error when" in fields else None,
        "report_lines": lines_reached, "mdrm": _MDRM.findall(reach),
        "owner": owner or None, "related": _RULE_ID.findall(related_text),
        "legal_reference": legal.rstrip(".") if legal else None,
        # The Check line as printed: a passage of the section, word for word.
        "quote": first["Check"], "confidence": 0.95,
    }


def answer(prompt: str) -> str:
    """The judgement for one batch, between the marker lines."""
    rules, skipped = [], []
    for section, body in sections_of(prompt):
        rule = rule_of(section, body)
        if rule is None:
            skipped.append({"section": section, "why": "no rule card in this section"})
        else:
            rules.append(rule)
    return f"{MARK_OPEN}\n{json.dumps({'rules': rules, 'skipped': skipped})}\n{MARK_CLOSE}"


class ScriptedRulebookAnalyst:
    """``run_role`` like a governed agent; records every prompt it was given."""

    model_id = "scripted:rulebook_analyst"

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def run_role(self, prompt: str, cwd: Path, *, read_only: bool = False, max_turns: Any = None,
                 **_kw: Any) -> dict[str, Any]:
        self.prompts.append(prompt)
        return {"summary": answer(prompt), "usage": {"input_tokens": len(prompt) // 4,
                                                    "output_tokens": 400},
                "cost_usd_micros": 0, "model": self.model_id}
