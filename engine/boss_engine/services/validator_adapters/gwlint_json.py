"""`gwlint validate <file> --json`: {"path", "violations": [{rule_id, severity: error|warn,
title, why, fix, line, fixable: never|mechanical}]}."""
from __future__ import annotations

from typing import Optional

from . import Raw, load_json, raw


def parse(stdout: str, file: str) -> Optional[list[Raw]]:
    data = load_json(stdout)
    if not isinstance(data, dict) or not isinstance(data.get("violations"), list):
        return None
    return [raw(v.get("rule_id"), v.get("severity"), file, v.get("line"), v.get("title"),
                why=v.get("why"), fix=v.get("fix"), fixable=v.get("fixable"))
            for v in data["violations"] if isinstance(v, dict)]
