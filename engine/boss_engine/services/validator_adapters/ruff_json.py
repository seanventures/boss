"""`ruff check --output-format json`: [{code, message, filename, location: {row, column},
fix: {applicability, message, edits} | null, url}]. ruff has no severity: every finding is
"error"; a `safe` fix is mechanically fixable."""
from __future__ import annotations

from typing import Optional

from . import Raw, load_json, raw


def parse(stdout: str, file: str) -> Optional[list[Raw]]:
    data = load_json(stdout)
    if not isinstance(data, list) or not all(isinstance(r, dict) and "code" in r for r in data):
        return None
    out: list[Raw] = []
    for d in data:
        fix = d.get("fix") if isinstance(d.get("fix"), dict) else None
        out.append(raw(d.get("code"), "error", d.get("filename") or file,
                       (d.get("location") or {}).get("row"), d.get("message"),
                       fix=(fix or {}).get("message"),
                       fixable="mechanical" if (fix or {}).get("applicability") == "safe"
                       else "never"))
    return out
