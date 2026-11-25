"""`eslint -f json`: [{filePath, messages: [{ruleId, severity: 1|2, message, line, fix?}]}].
Severity 2 is "error", 1 is "warn"; a message with a `fix` is mechanically fixable."""
from __future__ import annotations

from typing import Optional

from . import Raw, load_json, raw

_LEVEL = {2: "error", 1: "warn", 0: "off"}


def parse(stdout: str, file: str) -> Optional[list[Raw]]:
    data = load_json(stdout)
    if not isinstance(data, list) or not all(isinstance(r, dict) and "messages" in r
                                             for r in data):
        return None
    out: list[Raw] = []
    for result in data:
        for m in result.get("messages") or []:
            if not isinstance(m, dict):
                continue
            out.append(raw(m.get("ruleId") or "parse-error", _LEVEL.get(m.get("severity"), ""),
                           result.get("filePath") or file, m.get("line"), m.get("message"),
                           fixable="mechanical" if m.get("fix") else "never"))
    return out
