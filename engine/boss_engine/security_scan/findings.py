"""The normalised finding every scanner's output becomes (spec §4.4), and the limits (S4.4)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

TOOLS = ("semgrep", "osv", "zap")
SEVERITIES = ("critical", "high", "medium", "low", "info")
SEVERITY_RANK = {s: i for i, s in enumerate(SEVERITIES)}
STATUSES = ("open", "fixed", "accepted", "false_positive")
TRIAGE_STATUSES = ("accepted", "false_positive")

#: S4.4: a tool's raw output, the findings one tool may contribute, one finding's evidence.
MAX_RAW_BYTES = 50 * 1024 * 1024
MAX_FINDINGS = 10_000
MAX_EVIDENCE_BYTES = 4096


class ParseError(ValueError):
    """The tool's output is not what the tool writes (or is over a limit): the TOOL failed."""


@dataclass
class Finding:
    tool: str
    rule_id: str
    title: str
    severity: str
    #: {file, line, function} for code, {lockfile, ecosystem, package, version} for a dependency,
    #: {url, method, path, param} for DAST. `snippet_key` (added by fingerprints) finds a "moved".
    location: dict[str, Any]
    evidence: dict[str, Any] = field(default_factory=dict)
    confidence: Optional[str] = None
    cwe: Optional[str] = None
    owasp: Optional[str] = None
    fingerprint: str = ""

    def as_row(self) -> dict[str, Any]:
        return {"tool": self.tool, "rule_id": self.rule_id, "title": self.title,
                "severity": self.severity, "confidence": self.confidence, "cwe": self.cwe,
                "owasp": self.owasp, "location": self.location, "evidence": self.evidence,
                "fingerprint": self.fingerprint}


def clamp_severity(value: Any) -> str:
    v = str(value or "").strip().lower()
    aliases = {"error": "high", "warning": "medium", "note": "low", "moderate": "medium",
               "informational": "info", "information": "info", "none": "info"}
    v = aliases.get(v, v)
    return v if v in SEVERITY_RANK else "info"


def cvss_severity(score: Any) -> str:
    """A CVSS base score (OSV's max_severity) as a severity word."""
    try:
        s = float(score)
    except (TypeError, ValueError):
        return "medium"          # an advisory with no score is still an advisory
    if s >= 9.0:
        return "critical"
    if s >= 7.0:
        return "high"
    if s >= 4.0:
        return "medium"
    return "low" if s > 0 else "info"


def counts(findings: list[Finding] | list[dict[str, Any]]) -> dict[str, Any]:
    """{total, by_severity: {...}, by_tool: {tool: {severity: n}}}."""
    by_sev = {s: 0 for s in SEVERITIES}
    by_tool: dict[str, dict[str, int]] = {}
    for f in findings:
        tool = f.tool if isinstance(f, Finding) else f["tool"]
        sev = f.severity if isinstance(f, Finding) else f["severity"]
        by_sev[sev] += 1
        by_tool.setdefault(tool, {s: 0 for s in SEVERITIES})[sev] += 1
    return {"total": sum(by_sev.values()), "by_severity": by_sev, "by_tool": by_tool}
