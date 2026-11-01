"""Measure a rulebook extraction against the NorthPeak Bank answer key (rulebook source G5).

``measure(proposals)`` takes what an extraction proposed — ``rulebook_extract.proposals_of``
gives it from a project — and compares it with ``data.answer_key()``: rule precision and recall,
retired rules proposed (must be 0), threshold metric and value accuracy, and per-field accuracy.
Text fields are compared after collapsing whitespace and case (a PDF wraps lines); ids and sets
are compared exactly.
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Mapping, Optional

from . import data as D

#: Fields compared per rule: proposal field → answer-key field, and how.
TEXT_FIELDS = ("title", "statement", "check")
EXACT_FIELDS = ("version", "dimension", "severity", "system", "feed", "owner", "legal_reference")
SET_FIELDS = ("cdes", "columns", "tables", "mdrm", "related")


def _norm(value: Any) -> str:
    text = " ".join(str(value or "").replace(" ", " ").split()).lower()
    return re.sub(r"-\s+", "-", text).rstrip(".")


def _expected(rule_id: str, key: Mapping[str, Any], ds: Any) -> dict[str, Any]:
    owner = ds.owner(key["owner_id"]).name if key.get("owner_id") else None
    return {
        "version": key.get("version"), "title": key.get("title"), "statement": key.get("statement"),
        "check": key.get("check"), "dimension": key.get("dimension"), "severity": key.get("severity"),
        "system": key.get("system_id"), "feed": key.get("feed_id"), "owner": owner,
        "legal_reference": key.get("legal_reference"),
        "cdes": key.get("cde_ids") or [], "columns": key.get("columns") or [],
        "tables": key.get("tables") or [], "mdrm": key.get("mdrm") or [],
        "related": key.get("related") or [],
        "warning": {"text": key.get("warning"), "metric": key.get("metric"), "value": key.get("warning_value")},
        "error": {"text": key.get("error"), "metric": key.get("metric"), "value": key.get("error_value")},
    }


def _same(field: str, got: Any, want: Any) -> bool:
    if field in SET_FIELDS:
        return sorted({str(x) for x in got or []}) == sorted({str(x) for x in want or []})
    if field == "legal_reference":
        return _norm(got) == _norm(want) or (bool(got) and bool(want) and _norm(want) in _norm(got)) \
            or (not got and not want)
    if field in ("dimension", "owner", "severity", "system", "feed"):
        return _norm(got) == _norm(want)
    if field == "version":
        return (got is None and want is None) or (got is not None and want is not None and int(got) == int(want))
    return _norm(got) == _norm(want)


def _value_same(got: Any, want: Any) -> bool:
    if got is None or want is None:
        return got is None and want is None
    return abs(float(got) - float(want)) <= 1e-9 * max(1.0, abs(float(want)))


def measure(proposals: Iterable[Mapping[str, Any]], key: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
    key = key or D.answer_key()
    ds = D.generate()
    truth: Mapping[str, Mapping[str, Any]] = key["rules"]
    retired = set(key.get("retired_rules") or [])
    got: dict[str, Mapping[str, Any]] = {}
    for p in proposals:
        rid = str(p.get("rule_id") or "")
        if rid and rid not in got:
            got[rid] = p
    found = [r for r in got if r in truth]
    precision = len(found) / len(got) if got else 0.0
    recall = len(found) / len(truth) if truth else 0.0
    field_hits: dict[str, int] = {f: 0 for f in (*TEXT_FIELDS, *EXACT_FIELDS, *SET_FIELDS)}
    thresholds = {"pairs": 0, "metric": 0, "value": 0, "text": 0}
    misses: list[dict[str, Any]] = []
    for rid in found:
        want = _expected(rid, truth[rid], ds)
        have = got[rid]
        for field in field_hits:
            if _same(field, have.get(field), want.get(field)):
                field_hits[field] += 1
            elif len(misses) < 40:
                misses.append({"rule_id": rid, "field": field, "got": have.get(field), "want": want.get(field)})
        for level in ("warning", "error"):
            thresholds["pairs"] += 1
            mine = have.get(level) or {}
            if (mine.get("metric") or "") == want[level]["metric"]:
                thresholds["metric"] += 1
            if _value_same(mine.get("value"), want[level]["value"]):
                thresholds["value"] += 1
            elif len(misses) < 40:
                misses.append({"rule_id": rid, "field": f"{level}.value", "got": mine.get("value"),
                               "want": want[level]["value"]})
            if _norm(mine.get("text")) == _norm(want[level]["text"]):
                thresholds["text"] += 1
    n = len(found) or 1
    pairs = thresholds["pairs"] or 1
    return {
        "proposed": len(got), "expected": len(truth), "matched": len(found),
        "precision": round(precision, 4), "recall": round(recall, 4),
        "retired_proposed": sorted(r for r in got if r in retired),
        "unknown_proposed": sorted(r for r in got if r not in truth and r not in retired),
        "missed": sorted(r for r in truth if r not in got),
        "field_accuracy": {f: round(c / n, 4) for f, c in field_hits.items()},
        "threshold_accuracy": {"metric": round(thresholds["metric"] / pairs, 4),
                               "value": round(thresholds["value"] / pairs, 4),
                               "text": round(thresholds["text"] / pairs, 4)},
        "mismatches": misses,
    }


def report(result: Mapping[str, Any]) -> str:
    lines = [
        f"Rules: {result['matched']} of {result['expected']} found; {result['proposed']} proposed "
        f"(precision {result['precision']:.1%}, recall {result['recall']:.1%}).",
        f"Retired rules proposed: {len(result['retired_proposed'])}"
        + (f" ({', '.join(result['retired_proposed'])})" if result["retired_proposed"] else "") + ".",
        f"Thresholds: metric {result['threshold_accuracy']['metric']:.1%}, value "
        f"{result['threshold_accuracy']['value']:.1%}, text {result['threshold_accuracy']['text']:.1%}.",
        "Field accuracy: " + ", ".join(f"{k} {v:.0%}" for k, v in result["field_accuracy"].items()) + ".",
    ]
    if result["missed"]:
        lines.append(f"Missed: {', '.join(result['missed'][:20])}{' …' if len(result['missed']) > 20 else ''}")
    if result["unknown_proposed"]:
        lines.append(f"Not in the rulebook: {', '.join(result['unknown_proposed'][:20])}")
    return "\n".join(lines)
