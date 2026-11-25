"""SARIF 2.1.0, through the security scanner's parser (security_scan/parsers.py): one reader of
SARIF in BOSS. Its severity words (high/medium/low/info) are the native severity."""
from __future__ import annotations

from typing import Optional

from . import Raw, raw


def parse(stdout: str, file: str) -> Optional[list[Raw]]:
    from ...security_scan.findings import ParseError
    from ...security_scan.parsers import parse_semgrep_sarif

    try:
        found, _meta = parse_semgrep_sarif(stdout)
    except ParseError:
        return None
    return [raw(f.rule_id, f.severity, (f.location or {}).get("file") or file,
                (f.location or {}).get("line"), f.title, fixable=None) for f in found]
