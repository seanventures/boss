"""SCANNER OUTPUT -> NORMALISED FINDINGS (spec §4.2, §4.4; S4.4).

Each parser takes the tool's raw bytes, which are UNTRUSTED (the scanned code chose much of what is
in them): size-limited before parsing, every field read defensively, every string redacted, and a
shape the tool does not write is a ParseError — the tool is then recorded as failed, never as
"0 findings".

* Semgrep — SARIF 2.1.0 (`semgrep scan --sarif`). Rule ids are reported with the rules
  directory's path in front (opt.toolchains.security.semgrep-rules.javascript...); the pack-relative
  id (javascript.express...) is kept, so the id does not depend on where the image put the rules.
* OSV-Scanner — `--format json`. One finding per advisory GROUP (OSV groups aliases: GHSA-x and
  CVE-y for the same flaw are one finding), severity from the group's max CVSS score.
* ZAP — the `traditional-json` report. One finding per (alert, instance route+param): the same
  missing header on three pages of one route template is one finding.
"""
from __future__ import annotations

import json
import re
from pathlib import Path, PurePosixPath
from typing import Any, Optional

from . import fingerprints as fp
from . import redact as rd
from .findings import MAX_FINDINGS, MAX_RAW_BYTES, Finding, ParseError, clamp_severity, cvss_severity
from .rulepack import PACK_DIRS

_PACK_ROOTS = tuple(p.split("/", 1)[0] for p in PACK_DIRS)
_CWE = re.compile(r"\bCWE-(\d+)\b")
_OWASP = re.compile(r"\bA\d{2}:20\d\d\b")


def _load(raw: bytes | str, tool: str) -> Any:
    data = raw.encode("utf-8") if isinstance(raw, str) else raw
    if len(data) > MAX_RAW_BYTES:
        raise ParseError(f"{tool} output is {len(data)} bytes, over the {MAX_RAW_BYTES}-byte limit")
    try:
        return json.loads(data.decode("utf-8", errors="replace"))
    except ValueError as exc:
        raise ParseError(f"{tool} output is not JSON: {exc}") from None


def _s(value: Any, limit: int = 2000) -> str:
    return str(value if value is not None else "")[:limit]


def _first_sentence(text: str, limit: int = 200) -> str:
    text = " ".join(_s(text, 4000).split())
    cut = re.split(r"(?<=[.!?])\s", text, maxsplit=1)[0]
    return (cut[: limit - 1] + "…") if len(cut) > limit else cut


def pack_rule_id(rule_id: str) -> str:
    """opt.toolchains.security.semgrep-rules.javascript.x.y -> javascript.x.y"""
    parts = _s(rule_id, 500).split(".")
    for i, part in enumerate(parts):
        if part in _PACK_ROOTS and i > 0:
            return ".".join(parts[i:])
    return ".".join(parts)


def _rel(path: str, root: Optional[Path]) -> str:
    p = _s(path, 1000).replace("\\", "/")
    if p.startswith("file://"):
        p = p[len("file://"):]
    if root is not None:
        r = str(root).replace("\\", "/").rstrip("/") + "/"
        if p.startswith(r):
            p = p[len(r):]
    for prefix in ("/src/", "./"):
        if p.startswith(prefix):
            p = p[len(prefix):]
    p = str(PurePosixPath(p)).lstrip("/")
    return "" if p.startswith("..") else p


def _cap(findings: list[Finding]) -> list[Finding]:
    return findings[:MAX_FINDINGS]


def _finish(findings: list[Finding], root: Optional[Path]) -> list[Finding]:
    findings = _cap(findings)
    fp.annotate_functions(findings, root)
    return fp.assign(findings)


# ---- Semgrep (SARIF) ---------------------------------------------------------------------------

def parse_semgrep_sarif(raw: bytes | str, *, root: Optional[Path] = None) -> tuple[list[Finding], dict]:
    doc = _load(raw, "semgrep")
    if not isinstance(doc, dict) or not isinstance(doc.get("runs"), list) or not doc["runs"]:
        raise ParseError("semgrep output is not SARIF (no runs)")
    run = doc["runs"][0]
    driver = ((run.get("tool") or {}).get("driver") or {}) if isinstance(run, dict) else {}
    rules = {r.get("id"): r for r in driver.get("rules") or [] if isinstance(r, dict)}
    results = run.get("results")
    if not isinstance(results, list):
        raise ParseError("semgrep SARIF run has no results array")
    meta = {"version": _s(driver.get("semanticVersion") or driver.get("version"), 40),
            "truncated": len(results) > MAX_FINDINGS}
    out: list[Finding] = []
    for res in results[:MAX_FINDINGS]:
        if not isinstance(res, dict):
            continue
        raw_id = _s(res.get("ruleId"), 500)
        rule = rules.get(raw_id) or {}
        tags = [str(t) for t in ((rule.get("properties") or {}).get("tags") or [])]
        joined = " ".join(tags)
        cwe_m = _CWE.search(joined)
        owasp = sorted({m for m in _OWASP.findall(joined)}, reverse=True)
        confidence = next((t.split()[0].lower() for t in tags if t.endswith(" CONFIDENCE")), None)
        level = res.get("level") or (rule.get("defaultConfiguration") or {}).get("level") or "warning"
        locs = res.get("locations") or [{}]
        phys = (locs[0] or {}).get("physicalLocation") or {}
        region = phys.get("region") or {}
        file = _rel((phys.get("artifactLocation") or {}).get("uri") or "", root)
        rule_id = pack_rule_id(raw_id)
        cwe = f"CWE-{cwe_m.group(1)}" if cwe_m else None
        evidence = rd.redact_evidence(
            {"snippet": _s((region.get("snippet") or {}).get("text"), 1500),
             "message": _s((res.get("message") or {}).get("text"), 1500)},
            rule_id=rule_id, cwe=cwe)
        out.append(Finding(
            tool="semgrep", rule_id=rule_id,
            title=_first_sentence((res.get("message") or {}).get("text") or rule_id),
            severity=clamp_severity(level), confidence=confidence, cwe=cwe,
            owasp=owasp[0] if owasp else None,
            location={"file": file, "line": int(region.get("startLine") or 0)},
            evidence=evidence))
    return _finish(out, root), meta


# ---- OSV-Scanner (JSON) ------------------------------------------------------------------------

def parse_osv_json(raw: bytes | str, *, root: Optional[Path] = None) -> tuple[list[Finding], dict]:
    doc = _load(raw, "osv")
    if not isinstance(doc, dict) or not isinstance(doc.get("results", []), list):
        raise ParseError("osv output has no results array")
    out: list[Finding] = []
    for result in doc.get("results") or []:
        source = _rel(((result or {}).get("source") or {}).get("path") or "", root)
        for pkg in (result or {}).get("packages") or []:
            info = (pkg or {}).get("package") or {}
            vulns = {v.get("id"): v for v in (pkg.get("vulnerabilities") or []) if isinstance(v, dict)}
            groups = pkg.get("groups") or [{"ids": [i], "max_severity": ""} for i in vulns]
            for group in groups:
                ids = sorted(str(i) for i in (group.get("ids") or []) if i)
                if not ids:
                    continue
                primary = next((i for i in ids if i.startswith("GHSA-")), ids[0])
                vuln = vulns.get(primary) or vulns.get(ids[0]) or {}
                aliases = sorted({str(a) for a in (group.get("aliases") or [])} - {primary})
                cwe = next((c for c in ((vuln.get("database_specific") or {}).get("cwe_ids") or [])), None)
                fixed = sorted({str(e.get("fixed")) for a in (vuln.get("affected") or [])
                                for r in (a.get("ranges") or []) for e in (r.get("events") or [])
                                if isinstance(e, dict) and e.get("fixed")})
                sev = cvss_severity(group.get("max_severity")) if group.get("max_severity") \
                    else clamp_severity((vuln.get("database_specific") or {}).get("severity") or "medium")
                out.append(Finding(
                    tool="osv", rule_id=primary,
                    title=_first_sentence(vuln.get("summary") or f"{primary} in {info.get('name')}"),
                    severity=sev, confidence="high", cwe=_s(cwe, 20) or None,
                    location={"lockfile": source, "ecosystem": _s(info.get("ecosystem"), 40),
                              "package": _s(info.get("name"), 200),
                              "version": _s(info.get("version"), 80)},
                    evidence=rd.redact_evidence({
                        "ids": ids, "aliases": aliases[:20], "fixed_in": fixed[:10],
                        "max_cvss": _s(group.get("max_severity"), 10),
                        "advisory": f"https://osv.dev/vulnerability/{primary}"})))
    return _finish(out, root), {"truncated": len(out) > MAX_FINDINGS}


# ---- ZAP (traditional-json) --------------------------------------------------------------------

_ZAP_RISK = {"3": "high", "2": "medium", "1": "low", "0": "info"}
_ZAP_CONF = {"4": "confirmed", "3": "high", "2": "medium", "1": "low", "0": "false_positive"}


def parse_zap_json(raw: bytes | str) -> tuple[list[Finding], dict]:
    doc = _load(raw, "zap")
    if not isinstance(doc, dict) or not isinstance(doc.get("site"), list):
        raise ParseError("zap output has no site array (not a traditional-json report)")
    out: list[Finding] = []
    seen: set[tuple] = set()
    for site in doc["site"]:
        for alert in (site or {}).get("alerts") or []:
            if not isinstance(alert, dict):
                continue
            rule_id = _s(alert.get("alertRef") or alert.get("pluginid"), 40)
            risk = _ZAP_RISK.get(_s(alert.get("riskcode"), 2), "info")
            conf = _ZAP_CONF.get(_s(alert.get("confidence"), 2))
            if conf == "false_positive":
                continue
            cwe = _s(alert.get("cweid"), 10)
            for inst in alert.get("instances") or []:
                if not isinstance(inst, dict):
                    continue
                method = _s(inst.get("method") or "GET", 10).upper()
                path = fp.path_template(_s(inst.get("uri"), 2000))
                param = _s(inst.get("param"), 200)
                key = (rule_id, method, path, param)
                if key in seen:
                    continue
                seen.add(key)
                out.append(Finding(
                    tool="zap", rule_id=rule_id, title=_s(alert.get("name") or alert.get("alert"), 200),
                    severity=risk, confidence=conf,
                    cwe=f"CWE-{cwe}" if cwe and cwe.lstrip("-").isdigit() and int(cwe) > 0 else None,
                    location={"url": rd.redact_text(_s(inst.get("uri"), 2000)), "method": method,
                              "path": path, "param": param},
                    evidence=rd.redact_evidence({
                        "evidence": _s(inst.get("evidence"), 1000),
                        "attack": _s(inst.get("attack"), 500),
                        "otherinfo": _s(inst.get("otherinfo"), 1000),
                        "solution": _s(re.sub(r"<[^>]+>", " ", _s(alert.get("solution"), 1500)), 800)})))
                if len(out) >= MAX_FINDINGS:
                    return fp.assign(out), {"version": _s(doc.get("@version"), 20), "truncated": True}
    return fp.assign(out), {"version": _s(doc.get("@version"), 20), "truncated": False}
