#!/usr/bin/env python3
"""Fail-on-high/critical audit of *runtime* Python pins.

Local gate only. Does not enable GitHub Actions. Dev-only toolchain packages
(pytest, ruff, playwright) and npm/pnpm advisories are out of scope — those
stay waived on ``make deps-audit``.

Usage:
    python scripts/deps_audit_strict.py
    python scripts/deps_audit_strict.py --root /path/to/repo
    python scripts/deps_audit_strict.py --json-in findings.json   # test/offline
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Iterable, Sequence
from urllib.error import URLError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]

RUNTIME_REQUIREMENT_FILES = (
    "engine/requirements.txt",
    "apps/api/requirements.txt",
    "apps/worker/requirements.txt",
)

# Installed in app manifests for tests/lint/screenshots; not production runtime.
DEV_ONLY_PACKAGES = frozenset(
    {
        "pytest",
        "ruff",
        "playwright",
    }
)

BLOCKING_SEVERITIES = frozenset({"high", "critical", "unknown"})
NON_BLOCKING_SEVERITIES = frozenset({"low", "moderate", "medium", "informational", "none"})


@dataclass(frozen=True)
class Pin:
    name: str
    spec: str
    source: str


@dataclass(frozen=True)
class Finding:
    package: str
    version: str
    vuln_id: str
    severity: str
    aliases: tuple[str, ...]
    description: str


def parse_requirement_line(line: str) -> tuple[str, str] | None:
    """Return (canonical_name, original_spec) or None for comments/blank/flags."""
    cleaned = line.strip()
    if not cleaned or cleaned.startswith("#") or cleaned.startswith("-"):
        return None
    # Drop inline comments.
    if " #" in cleaned:
        cleaned = cleaned.split(" #", 1)[0].strip()
    if not cleaned:
        return None
    name = cleaned
    for separator in ("==", ">=", "<=", "~=", "!=", ">", "<"):
        if separator in cleaned:
            name = cleaned.split(separator, 1)[0]
            break
    name = name.split("[", 1)[0].strip()
    if not name:
        return None
    return name.lower().replace("_", "-"), cleaned


def load_runtime_pins(root: Path, files: Sequence[str] = RUNTIME_REQUIREMENT_FILES) -> list[Pin]:
    pins: list[Pin] = []
    seen: set[str] = set()
    for rel in files:
        path = root / rel
        if not path.is_file():
            raise FileNotFoundError(f"missing runtime requirements file: {rel}")
        for raw in path.read_text(encoding="utf-8").splitlines():
            parsed = parse_requirement_line(raw)
            if parsed is None:
                continue
            name, spec = parsed
            if name in DEV_ONLY_PACKAGES:
                continue
            if name in seen:
                continue
            seen.add(name)
            pins.append(Pin(name=name, spec=spec, source=rel))
    return pins


_CVSS_AV = {"N": 0.85, "A": 0.62, "L": 0.55, "P": 0.2}
_CVSS_AC = {"L": 0.77, "H": 0.44}
_CVSS_PR_UNCHANGED = {"N": 0.85, "L": 0.62, "H": 0.27}
_CVSS_PR_CHANGED = {"N": 0.85, "L": 0.68, "H": 0.50}
_CVSS_UI = {"N": 0.85, "R": 0.62}
_CVSS_IMPACT = {"H": 0.56, "L": 0.22, "N": 0.0}


def parse_cvss_v3_metrics(vector: str) -> dict[str, str] | None:
    text = str(vector).strip()
    if not text.upper().startswith("CVSS:3"):
        return None
    metrics: dict[str, str] = {}
    for part in text.split("/"):
        if ":" not in part:
            continue
        key, _, value = part.partition(":")
        key = key.strip().upper()
        if key.startswith("CVSS"):
            continue
        metrics[key] = value.strip().upper()
    required = {"AV", "AC", "PR", "UI", "S", "C", "I", "A"}
    if not required.issubset(metrics):
        return None
    return metrics


def cvss_v3_base_score(vector: str) -> float | None:
    metrics = parse_cvss_v3_metrics(vector)
    if metrics is None:
        return None
    try:
        av = _CVSS_AV[metrics["AV"]]
        ac = _CVSS_AC[metrics["AC"]]
        ui = _CVSS_UI[metrics["UI"]]
        confidentiality = _CVSS_IMPACT[metrics["C"]]
        integrity = _CVSS_IMPACT[metrics["I"]]
        availability = _CVSS_IMPACT[metrics["A"]]
        scope_changed = metrics["S"] == "C"
        pr = (_CVSS_PR_CHANGED if scope_changed else _CVSS_PR_UNCHANGED)[metrics["PR"]]
    except KeyError:
        return None
    iss = 1.0 - ((1.0 - confidentiality) * (1.0 - integrity) * (1.0 - availability))
    if iss <= 0.0:
        return 0.0
    if scope_changed:
        impact = 7.52 * (iss - 0.029) - 3.25 * (iss - 0.02) ** 15
    else:
        impact = 6.42 * iss
    exploitability = 8.22 * av * ac * pr * ui
    if scope_changed:
        score = min(1.08 * (impact + exploitability), 10.0)
    else:
        score = min(impact + exploitability, 10.0)
    return _cvss_roundup(score)


def _cvss_roundup(value: float) -> float:
    # CVSS v3.1 Roundup: smallest one-decimal value >= input, after 5-dp compare.
    scaled = round(value * 100000)
    if scaled % 10000 == 0:
        return scaled / 100000.0
    return (int(scaled / 10000) + 1) / 10.0


def severity_from_score(score: float) -> str:
    if score >= 9.0:
        return "critical"
    if score >= 7.0:
        return "high"
    if score >= 4.0:
        return "moderate"
    if score > 0.0:
        return "low"
    return "none"


def classify_severity(vuln: dict) -> str:
    score = _cvss_score(vuln)
    if score is not None:
        return severity_from_score(score)
    raw = vuln.get("severity")
    if raw is None and isinstance(vuln.get("database_specific"), dict):
        raw = vuln["database_specific"].get("severity")
    if isinstance(raw, dict):
        raw = raw.get("type") or raw.get("level") or raw.get("severity")
    if raw is None or isinstance(raw, (list, dict)):
        return "unknown"
    value = str(raw).strip().lower()
    if value in BLOCKING_SEVERITIES or value in NON_BLOCKING_SEVERITIES:
        return "moderate" if value == "medium" else value
    parsed = cvss_v3_base_score(str(raw))
    if parsed is not None:
        return severity_from_score(parsed)
    return "unknown"


def _cvss_score(vuln: dict) -> float | None:
    scores: list[float] = []
    severity = vuln.get("severity")
    candidates: list[object] = []
    if isinstance(severity, list):
        candidates.extend(severity)
    elif severity is not None:
        candidates.append(severity)
    for item in candidates:
        if isinstance(item, dict):
            raw = item.get("score") or item.get("vector")
        else:
            raw = item
        if raw is None:
            continue
        text = str(raw).strip()
        parsed = cvss_v3_base_score(text)
        if parsed is not None:
            scores.append(parsed)
            continue
        try:
            scores.append(float(text.split()[0]))
        except (TypeError, ValueError):
            continue
    return max(scores) if scores else None


def fetch_osv_record(vuln_id: str, timeout: float = 5.0) -> dict | None:
    url = f"https://api.osv.dev/v1/vulns/{vuln_id}"
    try:
        with urlopen(url, timeout=timeout) as response:
            payload = json.load(response)
    except (URLError, TimeoutError, OSError, json.JSONDecodeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def enrich_finding_severity(
    finding: Finding,
    fetch: Callable[[str], dict | None] = fetch_osv_record,
) -> Finding:
    if finding.severity != "unknown":
        return finding
    ids = (finding.vuln_id, *finding.aliases)
    # Prefer GHSA records — they carry database_specific.severity.
    ordered = sorted(ids, key=lambda item: (not str(item).startswith("GHSA-"), str(item)))
    for vuln_id in ordered:
        if not vuln_id:
            continue
        record = fetch(vuln_id)
        if not record:
            continue
        classified = classify_severity(record)
        if classified != "unknown":
            return replace(finding, severity=classified)
    return finding


def dedupe_findings(findings: Iterable[Finding]) -> list[Finding]:
    seen: set[tuple[str, str]] = set()
    unique: list[Finding] = []
    for finding in findings:
        key = (finding.package.lower(), finding.vuln_id)
        if key in seen:
            continue
        seen.add(key)
        unique.append(finding)
    return unique


def findings_from_pip_audit_json(payload: dict | list) -> list[Finding]:
    if isinstance(payload, list):
        dependencies = payload
    else:
        dependencies = payload.get("dependencies") or payload.get("deps") or []
    findings: list[Finding] = []
    for dep in dependencies:
        if not isinstance(dep, dict):
            continue
        package = str(dep.get("name") or "").strip()
        version = str(dep.get("version") or "").strip()
        for vuln in dep.get("vulns") or dep.get("vulnerabilities") or []:
            if not isinstance(vuln, dict):
                continue
            vuln_id = str(vuln.get("id") or vuln.get("vuln_id") or "unknown")
            aliases = tuple(
                str(alias)
                for alias in (vuln.get("aliases") or [])
                if alias
            )
            findings.append(
                Finding(
                    package=package,
                    version=version,
                    vuln_id=vuln_id,
                    severity=classify_severity(vuln),
                    aliases=aliases,
                    description=str(vuln.get("description") or "").strip(),
                )
            )
    return findings


def blocking_findings(findings: Iterable[Finding]) -> list[Finding]:
    return [finding for finding in findings if finding.severity in BLOCKING_SEVERITIES]


def write_runtime_requirements(pins: Sequence[Pin], dest: Path) -> Path:
    dest.write_text(
        "\n".join(pin.spec for pin in pins) + "\n",
        encoding="utf-8",
    )
    return dest


def run_pip_audit(python: str, requirements_file: Path) -> dict:
    try:
        completed = subprocess.run(
            [
                python,
                "-m",
                "pip_audit",
                "-r",
                str(requirements_file),
                "--format",
                "json",
                "--progress-spinner",
                "off",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as error:
        raise SystemExit(
            "pip-audit is required for make deps-audit-strict. "
            "Install it in the venv: .venv/bin/pip install 'pip-audit>=2.7,<3'"
        ) from error
    stdout = (completed.stdout or "").strip()
    if not stdout:
        stderr = (completed.stderr or "").strip()
        if completed.returncode != 0 and "No module named pip_audit" in stderr:
            raise SystemExit(
                "pip-audit is required for make deps-audit-strict. "
                "Install it in the venv: .venv/bin/pip install 'pip-audit>=2.7,<3'"
            )
        if completed.returncode != 0:
            raise SystemExit(
                f"pip-audit failed (exit {completed.returncode}): {stderr or 'no output'}"
            )
        return {"dependencies": []}
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as error:
        raise SystemExit(
            f"pip-audit produced non-JSON output: {error}\n{stdout[:500]}"
        ) from error
    if not isinstance(payload, (dict, list)):
        raise SystemExit("pip-audit JSON was not an object or list")
    return payload


def render_report(
    pins: Sequence[Pin],
    findings: Sequence[Finding],
    blocked: Sequence[Finding],
) -> str:
    lines = [
        "=== Runtime Python dependency audit (fail on high/critical) ===",
        f"pins: {len(pins)} from {', '.join(RUNTIME_REQUIREMENT_FILES)}",
        f"dev-only waived: {', '.join(sorted(DEV_ONLY_PACKAGES))}",
        f"findings: {len(findings)}  blocking: {len(blocked)}",
    ]
    if blocked:
        lines.append("BLOCKING:")
        for finding in blocked:
            lines.append(
                f"  - {finding.package}=={finding.version} {finding.vuln_id} "
                f"severity={finding.severity}"
            )
    elif findings:
        lines.append("non-blocking findings (waived below high):")
        for finding in findings:
            lines.append(
                f"  - {finding.package}=={finding.version} {finding.vuln_id} "
                f"severity={finding.severity}"
            )
    else:
        lines.append("no published advisories on current runtime pins")
    return "\n".join(lines)


def evaluate(
    *,
    root: Path,
    python: str,
    json_in: Path | None = None,
    enrich: bool | None = None,
    fetch: Callable[[str], dict | None] = fetch_osv_record,
) -> tuple[int, str]:
    pins = load_runtime_pins(root)
    if json_in is not None:
        payload = json.loads(json_in.read_text(encoding="utf-8"))
        should_enrich = False if enrich is None else enrich
    else:
        import tempfile

        with tempfile.TemporaryDirectory(prefix="boss-deps-audit-") as tmp:
            req = write_runtime_requirements(pins, Path(tmp) / "runtime-requirements.txt")
            payload = run_pip_audit(python, req)
        should_enrich = True if enrich is None else enrich
    findings = dedupe_findings(findings_from_pip_audit_json(payload))
    if should_enrich:
        findings = [enrich_finding_severity(finding, fetch=fetch) for finding in findings]
    blocked = blocking_findings(findings)
    report = render_report(pins, findings, blocked)
    return (1 if blocked else 0, report)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument(
        "--json-in",
        type=Path,
        default=None,
        help="Offline pip-audit JSON fixture (used by tests; skips the live scanner).",
    )
    args = parser.parse_args(argv)
    code, report = evaluate(root=args.root.resolve(), python=args.python, json_in=args.json_in)
    print(report)
    if code != 0:
        print(
            "deps-audit-strict FAILED: high/critical (or unknown-severity) "
            "advisories on runtime Python pins.",
            file=sys.stderr,
        )
    return code


if __name__ == "__main__":
    raise SystemExit(main())
