"""OSS code security vulnerability scanning for BOSS.

Market-validated scanner adapter shells:

1. **Semgrep** (SAST) — https://github.com/semgrep/semgrep
2. **Trivy** (vuln / container / FS) — https://github.com/aquasecurity/trivy
3. **OSV-Scanner** (deps) — https://github.com/google/osv-scanner
4. **Gitleaks** (secrets) — https://github.com/gitleaks/gitleaks
5. **Checkov** (IaC) — already used in ``iac_verify``; exposed here as an optional
   scanner kind for project-wide runs

BOSS orchestrates scans via subprocess when the binary is on PATH. Every path
supports ``simulated=True`` fixture findings so E2E and CI work without tools
installed.

Findings are normalized to a common schema (severity, rule_id, path, line,
message, scanner, optional cve / fix). Scan runs are stored in-memory and
mirrored as Receipts with a unique ``dedupe_key``. Critical findings can fail
the gate (go / no-go).

Snyk commercial API is **never** called; Snyk appears only as a catalog note
("bring your own" / planned optional).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .core import emit_event, make_id
from .receipts import ReceiptService

# Per-scanner stdout/stderr capture cap (~32KB head+tail).
_OUTPUT_CAP = 32_768
_DEFAULT_TIMEOUT_SECONDS = 300

SEVERITIES = ("critical", "high", "medium", "low", "info", "unknown")

# ---------------------------------------------------------------------------
# Catalog — market-validated OSS scanners + BYO note for Snyk
# ---------------------------------------------------------------------------

SCANNER_CATALOG: dict[str, dict[str, Any]] = {
    "semgrep": {
        "id": "semgrep",
        "name": "Semgrep",
        "kind": "sast",
        "binary": "semgrep",
        "website": "https://semgrep.dev",
        "repo": "https://github.com/semgrep/semgrep",
        "stars_note": "~10k+ GitHub stars; industry-standard OSS SAST",
        "description": "Static analysis for many languages; rule packs + custom rules.",
        "status": "supported",
        "argv": lambda path: [
            "semgrep",
            "scan",
            "--json",
            "--quiet",
            "--metrics=off",
            str(path),
        ],
    },
    "trivy": {
        "id": "trivy",
        "name": "Trivy",
        "kind": "vuln_fs",
        "binary": "trivy",
        "website": "https://trivy.dev",
        "repo": "https://github.com/aquasecurity/trivy",
        "stars_note": "~25k+ GitHub stars; container/FS/vuln scanner",
        "description": "Filesystem, container image, and dependency vulnerability scan.",
        "status": "supported",
        "argv": lambda path: [
            "trivy",
            "fs",
            "--format",
            "json",
            "--quiet",
            str(path),
        ],
    },
    "osv-scanner": {
        "id": "osv-scanner",
        "name": "OSV-Scanner",
        "kind": "deps",
        "binary": "osv-scanner",
        "website": "https://google.github.io/osv-scanner/",
        "repo": "https://github.com/google/osv-scanner",
        "stars_note": "~7k+ GitHub stars; Google OSV vulnerability DB client",
        "description": "Scans lockfiles and SBOMs against the OSV database.",
        "status": "supported",
        "argv": lambda path: [
            "osv-scanner",
            "--format",
            "json",
            "-r",
            str(path),
        ],
    },
    "gitleaks": {
        "id": "gitleaks",
        "name": "Gitleaks",
        "kind": "secrets",
        "binary": "gitleaks",
        "website": "https://gitleaks.io",
        "repo": "https://github.com/gitleaks/gitleaks",
        "stars_note": "~18k+ GitHub stars; secret detection",
        "description": "Detects hardcoded secrets, keys, and credentials in source.",
        "status": "supported",
        "argv": lambda path: [
            "gitleaks",
            "detect",
            "--source",
            str(path),
            "--report-format",
            "json",
            "--report-path",
            "/dev/stdout",
            "--no-git",
            "--exit-code",
            "0",
        ],
    },
    "checkov": {
        "id": "checkov",
        "name": "Checkov",
        "kind": "iac",
        "binary": "checkov",
        "website": "https://www.checkov.io",
        "repo": "https://github.com/bridgecrewio/checkov",
        "stars_note": "~7k+ GitHub stars; IaC policy-as-code (also used in iac_verify)",
        "description": (
            "Infrastructure-as-code security/compliance scanner. Optional scanner "
            "kind for project-wide runs; the governed IaC verify lane already "
            "invokes checkov/tfsec on Terraform changes."
        ),
        "status": "supported",
        "argv": lambda path: [
            "checkov",
            "-d",
            str(path),
            "--output",
            "json",
            "--quiet",
            "--compact",
        ],
    },
    # Catalog-only — never invoked. Bring-your-own commercial option planned.
    "snyk": {
        "id": "snyk",
        "name": "Snyk (BYO)",
        "kind": "commercial",
        "binary": None,
        "website": "https://snyk.io",
        "repo": None,
        "stars_note": "Commercial SaaS — not invoked by BOSS; bring-your-own planned",
        "description": (
            "Snyk commercial API is intentionally NOT called. Listed as a catalog "
            "note only: operators may wire a BYO Snyk integration later behind an "
            "explicit policy-guarded adapter. Use Semgrep/Trivy/OSV/Gitleaks/Checkov "
            "for OSS scanning today."
        ),
        "status": "planned_byo",
        "argv": None,
    },
}

# Runnable scanner ids (fail-closed for anything else, including snyk).
SUPPORTED_SCANNERS: frozenset[str] = frozenset(
    sid for sid, meta in SCANNER_CATALOG.items() if meta.get("status") == "supported"
)

DEFAULT_SCANNERS: tuple[str, ...] = (
    "semgrep",
    "trivy",
    "osv-scanner",
    "gitleaks",
)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class Finding:
    """Normalized security finding shared across scanner adapters."""

    severity: str
    rule_id: str
    path: str
    line: Optional[int]
    message: str
    scanner: str
    cve: Optional[str] = None
    fix: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # Drop None optionals for cleaner JSON.
        return {k: v for k, v in d.items() if v is not None}


@dataclass
class ScannerResult:
    scanner: str
    status: str  # ok | failed | tool_missing | error | simulated
    findings: list[Finding] = field(default_factory=list)
    duration_ms: int = 0
    exit_code: Optional[int] = None
    stdout_truncated: str = ""
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "scanner": self.scanner,
            "status": self.status,
            "findings": [f.to_dict() for f in self.findings],
            "finding_count": len(self.findings),
            "duration_ms": self.duration_ms,
            "exit_code": self.exit_code,
            "stdout_truncated": self.stdout_truncated,
            "error": self.error,
        }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _truncate(text: str, cap: int = _OUTPUT_CAP) -> str:
    if text is None:
        return ""
    if len(text) <= cap:
        return text
    head = cap // 2
    tail = cap - head - 32
    return text[:head] + "\n...[truncated]...\n" + text[-tail:]


def _normalize_severity(raw: Any) -> str:
    if raw is None:
        return "unknown"
    s = str(raw).strip().lower()
    aliases = {
        "crit": "critical",
        "error": "high",
        "warn": "medium",
        "warning": "medium",
        "note": "info",
        "informational": "info",
        "none": "info",
        "negligible": "low",
        "moderate": "medium",
        "important": "high",
        "fatal": "critical",
    }
    s = aliases.get(s, s)
    return s if s in SEVERITIES else "unknown"


def _binary_available(name: Optional[str]) -> bool:
    return bool(name) and shutil.which(name) is not None


# ---------------------------------------------------------------------------
# Simulated fixture findings (deterministic for E2E)
# ---------------------------------------------------------------------------


def _simulated_findings(scanner: str, path: str) -> list[Finding]:
    """Deterministic fixture findings so CI works without scanner binaries."""
    base = path.rstrip("/") or "."
    fixtures: dict[str, list[Finding]] = {
        "semgrep": [
            Finding(
                severity="high",
                rule_id="python.lang.security.audit.dangerous-system-call",
                path=f"{base}/app/utils.py",
                line=42,
                message="Simulated: potential dangerous system call via subprocess with shell=True",
                scanner="semgrep",
                fix="Use subprocess with a list argv and shell=False",
            ),
            Finding(
                severity="medium",
                rule_id="python.lang.security.audit.hardcoded-password",
                path=f"{base}/config.py",
                line=17,
                message="Simulated: possible hardcoded credential pattern",
                scanner="semgrep",
            ),
        ],
        "trivy": [
            Finding(
                severity="critical",
                rule_id="CVE-2024-0001",
                path=f"{base}/requirements.txt",
                line=3,
                message="Simulated: vulnerable dependency (example package)",
                scanner="trivy",
                cve="CVE-2024-0001",
                fix="Upgrade example-package to >=2.0.0",
            ),
            Finding(
                severity="high",
                rule_id="CVE-2023-9999",
                path=f"{base}/package-lock.json",
                line=1,
                message="Simulated: outdated transitive dependency",
                scanner="trivy",
                cve="CVE-2023-9999",
            ),
        ],
        "osv-scanner": [
            Finding(
                severity="high",
                rule_id="GHSA-xxxx-yyyy-zzzz",
                path=f"{base}/pyproject.toml",
                line=12,
                message="Simulated: known vulnerable package version in lockfile",
                scanner="osv-scanner",
                cve="CVE-2024-1111",
                fix="Bump dependency to patched release",
            ),
        ],
        "gitleaks": [
            Finding(
                severity="critical",
                rule_id="aws-access-key-id",
                path=f"{base}/.env.example",
                line=8,
                message="Simulated: AWS access key id pattern detected",
                scanner="gitleaks",
                fix="Remove secret; use a secret manager / BOSS SecretRef",
            ),
        ],
        "checkov": [
            Finding(
                severity="high",
                rule_id="CKV_AWS_20",
                path=f"{base}/infra/s3.tf",
                line=5,
                message="Simulated: S3 Bucket has an ACL defined which allows public access",
                scanner="checkov",
                fix="Set acl = 'private' and block public access",
            ),
            Finding(
                severity="medium",
                rule_id="CKV_AWS_144",
                path=f"{base}/infra/s3.tf",
                line=12,
                message="Simulated: S3 bucket has cross-region replication disabled",
                scanner="checkov",
            ),
        ],
    }
    return list(fixtures.get(scanner, []))


# ---------------------------------------------------------------------------
# Output parsers (best-effort; simulated path is the E2E guarantee)
# ---------------------------------------------------------------------------


def _parse_semgrep(raw: str, scanner: str) -> list[Finding]:
    findings: list[Finding] = []
    try:
        data = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return findings
    for item in data.get("results") or []:
        extra = item.get("extra") or {}
        meta = extra.get("metadata") or {}
        sev = extra.get("severity") or meta.get("severity") or "medium"
        start = item.get("start") or {}
        findings.append(
            Finding(
                severity=_normalize_severity(sev),
                rule_id=str(item.get("check_id") or "semgrep.unknown"),
                path=str(item.get("path") or ""),
                line=start.get("line"),
                message=str(extra.get("message") or item.get("check_id") or "semgrep finding"),
                scanner=scanner,
                fix=(extra.get("fix") or meta.get("fix")) if isinstance(extra.get("fix") or meta.get("fix"), str) else None,
            )
        )
    return findings


def _parse_trivy(raw: str, scanner: str) -> list[Finding]:
    findings: list[Finding] = []
    try:
        data = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return findings
    results = data.get("Results") or data.get("results") or []
    if isinstance(data, list):
        results = data
    for res in results:
        target = str(res.get("Target") or res.get("target") or "")
        for vuln in res.get("Vulnerabilities") or res.get("vulnerabilities") or []:
            cve = vuln.get("VulnerabilityID") or vuln.get("vulnerabilityID")
            findings.append(
                Finding(
                    severity=_normalize_severity(vuln.get("Severity") or vuln.get("severity")),
                    rule_id=str(cve or vuln.get("PkgName") or "trivy.unknown"),
                    path=target,
                    line=None,
                    message=str(
                        vuln.get("Title")
                        or vuln.get("Description")
                        or f"Vulnerable package {vuln.get('PkgName', '?')}"
                    )[:500],
                    scanner=scanner,
                    cve=str(cve) if cve else None,
                    fix=str(vuln.get("FixedVersion")) if vuln.get("FixedVersion") else None,
                )
            )
        for mis in res.get("Misconfigurations") or []:
            findings.append(
                Finding(
                    severity=_normalize_severity(mis.get("Severity")),
                    rule_id=str(mis.get("ID") or "trivy.misconfig"),
                    path=target,
                    line=None,
                    message=str(mis.get("Title") or mis.get("Message") or "misconfiguration")[:500],
                    scanner=scanner,
                    fix=str(mis.get("Resolution")) if mis.get("Resolution") else None,
                )
            )
    return findings


def _parse_osv(raw: str, scanner: str) -> list[Finding]:
    findings: list[Finding] = []
    try:
        data = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return findings
    for result in data.get("results") or []:
        source = (result.get("source") or {}).get("path") or ""
        for pkg in result.get("packages") or []:
            pkg_name = (pkg.get("package") or {}).get("name") or "unknown"
            for vuln in pkg.get("vulnerabilities") or []:
                aliases = vuln.get("aliases") or []
                cve = next((a for a in aliases if str(a).startswith("CVE-")), None)
                vid = vuln.get("id") or cve or "osv.unknown"
                summary = vuln.get("summary") or vuln.get("details") or f"OSV hit on {pkg_name}"
                findings.append(
                    Finding(
                        severity=_normalize_severity(
                            (vuln.get("database_specific") or {}).get("severity")
                            or "high"
                        ),
                        rule_id=str(vid),
                        path=str(source),
                        line=None,
                        message=str(summary)[:500],
                        scanner=scanner,
                        cve=str(cve) if cve else None,
                    )
                )
    return findings


def _parse_gitleaks(raw: str, scanner: str) -> list[Finding]:
    findings: list[Finding] = []
    text = (raw or "").strip()
    if not text:
        return findings
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Sometimes tools emit one JSON object per line.
        data = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                data.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    if isinstance(data, dict):
        data = data.get("findings") or data.get("leaks") or [data]
    if not isinstance(data, list):
        return findings
    for item in data:
        if not isinstance(item, dict):
            continue
        findings.append(
            Finding(
                severity="critical",
                rule_id=str(item.get("RuleID") or item.get("rule_id") or "gitleaks.secret"),
                path=str(item.get("File") or item.get("file") or ""),
                line=item.get("StartLine") or item.get("line"),
                message=str(
                    item.get("Description")
                    or item.get("description")
                    or "Secret detected"
                )[:500],
                scanner=scanner,
                fix="Rotate the credential and remove it from source control",
            )
        )
    return findings


def _parse_checkov(raw: str, scanner: str) -> list[Finding]:
    findings: list[Finding] = []
    try:
        data = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return findings
    # checkov may return a list of reports or a single object.
    reports = data if isinstance(data, list) else [data]
    for report in reports:
        if not isinstance(report, dict):
            continue
        results = report.get("results") or {}
        for item in results.get("failed_checks") or []:
            file_path = str(item.get("file_path") or item.get("repo_file_path") or "")
            line = None
            fr = item.get("file_line_range") or item.get("file_lines")
            if isinstance(fr, list) and fr:
                try:
                    line = int(fr[0])
                except (TypeError, ValueError):
                    line = None
            findings.append(
                Finding(
                    severity=_normalize_severity(item.get("severity") or "high"),
                    rule_id=str(item.get("check_id") or "checkov.unknown"),
                    path=file_path.lstrip("/"),
                    line=line,
                    message=str(item.get("check_name") or item.get("check_id") or "checkov finding")[:500],
                    scanner=scanner,
                    fix=str(item.get("guideline")) if item.get("guideline") else None,
                )
            )
    return findings


_PARSERS = {
    "semgrep": _parse_semgrep,
    "trivy": _parse_trivy,
    "osv-scanner": _parse_osv,
    "gitleaks": _parse_gitleaks,
    "checkov": _parse_checkov,
}


# ---------------------------------------------------------------------------
# Subprocess runner
# ---------------------------------------------------------------------------


def _run_subprocess(
    argv: list[str],
    cwd: Path,
    timeout_s: int = _DEFAULT_TIMEOUT_SECONDS,
) -> tuple[int, str]:
    """Run scanner; never raises. Returns (exit_code, truncated combined output)."""
    env = os.environ.copy()
    # Scrub cloud/provider credentials from scanner subprocesses (defense in depth).
    for key in list(env.keys()):
        if key.startswith(("AWS_", "ARM_", "GOOGLE_", "AZURE_", "SNYK_")):
            env.pop(key, None)
    try:
        proc = subprocess.run(  # noqa: S603 — fixed argv from catalog, no shell
            argv,
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        return proc.returncode, _truncate(out)
    except subprocess.TimeoutExpired:
        return -1, _truncate(f"$ {' '.join(argv)} timed out after {timeout_s}s")
    except OSError as exc:
        return -1, _truncate(f"$ {' '.join(argv)} failed to start: {exc}")


def run_scanner(
    scanner_id: str,
    path: str | Path,
    *,
    simulated: bool = False,
    timeout_s: int = _DEFAULT_TIMEOUT_SECONDS,
) -> ScannerResult:
    """Run one scanner (or produce simulated findings). Fail closed on unknown id."""
    if scanner_id not in SUPPORTED_SCANNERS:
        # Includes snyk and any typo — fail closed, never invent a run.
        raise ValueError(
            f"unknown or unsupported scanner '{scanner_id}'; "
            f"supported: {sorted(SUPPORTED_SCANNERS)}"
        )

    meta = SCANNER_CATALOG[scanner_id]
    path = Path(path).resolve()
    started = time.monotonic()

    if simulated:
        findings = _simulated_findings(scanner_id, str(path))
        return ScannerResult(
            scanner=scanner_id,
            status="simulated",
            findings=findings,
            duration_ms=int((time.monotonic() - started) * 1000),
            exit_code=0,
            stdout_truncated="[simulated] fixture findings",
        )

    binary = meta.get("binary")
    if not _binary_available(binary):
        return ScannerResult(
            scanner=scanner_id,
            status="tool_missing",
            findings=[],
            duration_ms=int((time.monotonic() - started) * 1000),
            error=f"{binary} not found on PATH; install the tool or re-run with simulated=true",
        )

    if not path.exists():
        return ScannerResult(
            scanner=scanner_id,
            status="error",
            findings=[],
            duration_ms=int((time.monotonic() - started) * 1000),
            error=f"scan path does not exist: {path}",
        )

    argv_fn = meta.get("argv")
    assert callable(argv_fn)
    argv = list(argv_fn(path))
    code, out = _run_subprocess(argv, cwd=path if path.is_dir() else path.parent, timeout_s=timeout_s)
    parser = _PARSERS.get(scanner_id)
    findings = parser(out, scanner_id) if parser else []
    # Scanners often exit non-zero when findings exist; that is not a tool failure.
    status = "ok"
    if code == -1:
        status = "error"
    elif code not in (0, 1, 2) and not findings:
        # Unusual exit with no parseable findings — surface as failed.
        status = "failed"
    return ScannerResult(
        scanner=scanner_id,
        status=status,
        findings=findings,
        duration_ms=int((time.monotonic() - started) * 1000),
        exit_code=code,
        stdout_truncated=out,
        error=None if status == "ok" else (f"exit_code={code}" if status != "ok" else None),
    )


# ---------------------------------------------------------------------------
# In-memory run store + orchestration
# ---------------------------------------------------------------------------

_lock = threading.RLock()
_RUNS: dict[str, dict[str, Any]] = {}
_RUNS_BY_PROJECT: dict[str, list[str]] = {}


def reset_store() -> None:
    """Test helper: clear in-memory runs."""
    with _lock:
        _RUNS.clear()
        _RUNS_BY_PROJECT.clear()


def list_scanners(*, include_planned: bool = True) -> list[dict[str, Any]]:
    """Return the scanner catalog for the API (no callable argv)."""
    items = []
    for sid, meta in SCANNER_CATALOG.items():
        if not include_planned and meta.get("status") != "supported":
            continue
        items.append(
            {
                "id": meta["id"],
                "name": meta["name"],
                "kind": meta["kind"],
                "binary": meta.get("binary"),
                "website": meta.get("website"),
                "repo": meta.get("repo"),
                "stars_note": meta.get("stars_note"),
                "description": meta.get("description"),
                "status": meta.get("status"),
                "available": _binary_available(meta.get("binary"))
                if meta.get("status") == "supported"
                else False,
            }
        )
    return items


def _summarize(findings: list[Finding]) -> dict[str, Any]:
    by_sev: dict[str, int] = {s: 0 for s in SEVERITIES}
    by_scanner: dict[str, int] = {}
    for f in findings:
        sev = _normalize_severity(f.severity)
        by_sev[sev] = by_sev.get(sev, 0) + 1
        by_scanner[f.scanner] = by_scanner.get(f.scanner, 0) + 1
    critical = by_sev.get("critical", 0)
    high = by_sev.get("high", 0)
    return {
        "total": len(findings),
        "by_severity": by_sev,
        "by_scanner": by_scanner,
        "critical": critical,
        "high": high,
        "has_critical": critical > 0,
    }


def _evaluate_gate(
    summary: dict[str, Any],
    *,
    fail_on: frozenset[str] | set[str] | None = None,
) -> dict[str, Any]:
    """Go / no-go evaluation. Default: any critical finding → no-go."""
    fail_on = fail_on or frozenset({"critical"})
    by_sev = summary.get("by_severity") or {}
    blocking = {sev: int(by_sev.get(sev, 0)) for sev in fail_on if int(by_sev.get(sev, 0)) > 0}
    go = len(blocking) == 0
    return {
        "decision": "go" if go else "no-go",
        "go": go,
        "fail_on": sorted(fail_on),
        "blocking_counts": blocking,
        "reason": (
            "no blocking findings"
            if go
            else f"blocking findings present: {blocking}"
        ),
    }


def _project_scope(connection, project_id: str) -> tuple[str, str]:
    from sqlalchemy import text

    row = connection.execute(
        text(
            "SELECT p.workspace_id AS ws, w.org_id AS org FROM projects p "
            "JOIN workspaces w ON w.id = p.workspace_id WHERE p.id = :p"
        ),
        {"p": project_id},
    ).mappings().first()
    if row:
        return str(row["org"]), str(row["ws"])
    return project_id, project_id


def _record_receipt(
    engine,
    *,
    project_id: str,
    run_id: str,
    actor: str,
    payload: dict[str, Any],
    status: str,
) -> Optional[str]:
    """Best-effort receipt with unique dedupe_key. Returns receipt id or None."""
    if engine is None:
        return None
    try:
        from ..db import begin
    except Exception:  # noqa: BLE001
        return None
    dedupe_key = f"security_scan:{project_id}:{run_id}"
    receipts = ReceiptService()
    try:
        with begin(engine) as connection:
            org_id, workspace_id = _project_scope(connection, project_id)
            receipt, _created = receipts.create_or_reuse(
                connection,
                org_id,
                workspace_id,
                project_id,
                dedupe_key,
                "security_scan.run",
                {
                    "project_id": project_id,
                    "run_id": run_id,
                    "scanners": payload.get("scanners"),
                    "path": payload.get("path"),
                    "simulated": payload.get("simulated"),
                },
                {
                    "run_id": run_id,
                    "summary": payload.get("summary"),
                    "status": status,
                },
            )
            if status in ("succeeded", "failed", "completed"):
                if status == "failed" or (
                    payload.get("summary") or {}
                ).get("has_critical"):
                    # Gate failure still records a succeeded *effect* (scan ran);
                    # the scan status itself is separate. Mark receipt succeeded
                    # with result carrying the gate outcome.
                    receipts.mark_succeeded(
                        connection,
                        receipt["id"],
                        provider_ref=run_id,
                        result={
                            "run_id": run_id,
                            "scan_status": status,
                            "summary": payload.get("summary"),
                            "gate": payload.get("gate"),
                        },
                    )
                else:
                    receipts.mark_succeeded(
                        connection,
                        receipt["id"],
                        provider_ref=run_id,
                        result={
                            "run_id": run_id,
                            "scan_status": status,
                            "summary": payload.get("summary"),
                            "gate": payload.get("gate"),
                        },
                    )
            emit_event(
                connection,
                project_id,
                "security_scan.completed",
                actor,
                "security_scan_run",
                run_id,
                {
                    "status": status,
                    "dedupe_key": dedupe_key,
                    "receipt_id": receipt["id"],
                    "summary": payload.get("summary"),
                    "simulated": payload.get("simulated"),
                },
            )
            return receipt["id"]
    except Exception:  # noqa: BLE001 — receipt is audit; never block the scan path
        return None


def run_scan(
    *,
    project_id: str,
    actor: str = "system",
    path: Optional[str] = None,
    scanners: Optional[list[str]] = None,
    simulated: bool = False,
    timeout_s: int = _DEFAULT_TIMEOUT_SECONDS,
    engine=None,
    fail_on_critical: bool = True,
) -> dict[str, Any]:
    """Orchestrate one or more scanners and persist the run in-memory (+ receipt)."""
    project_id = (project_id or "").strip()
    if not project_id:
        raise ValueError("project_id is required")

    scan_path = path or os.getcwd()
    requested = list(scanners) if scanners else list(DEFAULT_SCANNERS)
    if not requested:
        raise ValueError("at least one scanner is required")

    # Fail closed on any unknown / planned-only scanner before running anything.
    unknown = [s for s in requested if s not in SUPPORTED_SCANNERS]
    if unknown:
        raise ValueError(
            f"unknown or unsupported scanner(s): {unknown}; "
            f"supported: {sorted(SUPPORTED_SCANNERS)}. "
            "Snyk is catalog-only (planned BYO) and is never invoked."
        )

    run_id = make_id("secscan")
    started = time.monotonic()
    scanner_results: list[ScannerResult] = []
    all_findings: list[Finding] = []

    for sid in requested:
        result = run_scanner(sid, scan_path, simulated=simulated, timeout_s=timeout_s)
        scanner_results.append(result)
        all_findings.extend(result.findings)

    summary = _summarize(all_findings)
    gate = _evaluate_gate(
        summary,
        fail_on=frozenset({"critical"}) if fail_on_critical else frozenset(),
    )

    # Scan status: failed when any critical findings (if gating) or hard tool errors
    # with zero usable results. tool_missing in non-sim mode is recorded but does
    # not alone fail the whole run unless every scanner is missing.
    hard_errors = [r for r in scanner_results if r.status in ("error", "failed")]
    all_missing = scanner_results and all(r.status == "tool_missing" for r in scanner_results)
    if fail_on_critical and summary.get("has_critical"):
        scan_status = "failed"
    elif all_missing and not simulated:
        scan_status = "failed"
    elif hard_errors and not all_findings:
        scan_status = "failed"
    else:
        scan_status = "completed"

    duration_ms = int((time.monotonic() - started) * 1000)
    record: dict[str, Any] = {
        "id": run_id,
        "project_id": project_id,
        "actor": actor,
        "path": str(Path(scan_path).resolve()),
        "scanners": requested,
        "simulated": bool(simulated),
        "status": scan_status,
        "summary": summary,
        "gate": gate,
        "findings": [f.to_dict() for f in all_findings],
        "scanner_results": [r.to_dict() for r in scanner_results],
        "duration_ms": duration_ms,
        "receipt_id": None,
        "dedupe_key": f"security_scan:{project_id}:{run_id}",
        "created_at": _now_iso(),
    }

    receipt_id = _record_receipt(
        engine,
        project_id=project_id,
        run_id=run_id,
        actor=actor,
        payload=record,
        status=scan_status,
    )
    record["receipt_id"] = receipt_id

    with _lock:
        _RUNS[run_id] = record
        _RUNS_BY_PROJECT.setdefault(project_id, []).insert(0, run_id)

    return record


def list_runs(project_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
    project_id = (project_id or "").strip()
    limit = max(1, min(int(limit), 500))
    with _lock:
        ids = list(_RUNS_BY_PROJECT.get(project_id, []))[:limit]
        return [_public_run(_RUNS[i]) for i in ids if i in _RUNS]


def get_run(run_id: str) -> Optional[dict[str, Any]]:
    with _lock:
        rec = _RUNS.get(run_id)
        return _public_run(rec) if rec else None


def evaluate_gate(
    run_id: str,
    *,
    fail_on: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Re-evaluate go/no-go for a stored run (default: critical blocks)."""
    with _lock:
        rec = _RUNS.get(run_id)
        if not rec:
            raise KeyError(f"scan run not found: {run_id}")
        summary = rec.get("summary") or {}
        sevs = fail_on if fail_on is not None else ["critical"]
        normalized = {_normalize_severity(s) for s in sevs}
        gate = _evaluate_gate(summary, fail_on=normalized)
        # Persist latest gate evaluation on the run.
        rec = dict(rec)
        rec["gate"] = gate
        # Update status if critical gate fails.
        if not gate["go"] and rec.get("status") == "completed":
            rec["status"] = "failed"
        elif gate["go"] and rec.get("status") == "failed" and not summary.get("has_critical"):
            # Only flip back if original failure was gate-driven without criticals under new policy.
            pass
        _RUNS[run_id] = rec
        return {
            "run_id": run_id,
            "project_id": rec["project_id"],
            "status": rec["status"],
            "summary": summary,
            "gate": gate,
        }


def _public_run(rec: dict[str, Any]) -> dict[str, Any]:
    """Return a JSON-safe copy of a run record."""
    return dict(rec)


def last_scan_summary(project_id: str) -> Optional[dict[str, Any]]:
    """Convenience: summary of the most recent run for a project."""
    runs = list_runs(project_id, limit=1)
    if not runs:
        return None
    r = runs[0]
    return {
        "run_id": r["id"],
        "status": r["status"],
        "summary": r.get("summary"),
        "gate": r.get("gate"),
        "created_at": r.get("created_at"),
        "simulated": r.get("simulated"),
    }


def latest_run(project_id: str) -> dict[str, Any]:
    """Honest latest-run view: empty is not_configured, never a fake score."""
    project_id = (project_id or "").strip()
    runs = list_runs(project_id, limit=1) if project_id else []
    if not runs:
        return {
            "project_id": project_id,
            "status": "not_configured",
            "run": None,
            "summary": None,
            "note": (
                "No scan has been recorded in this process. "
                "POST /v1/security-scan/run with simulated=true for a fixture."
            ),
        }
    rec = runs[0]
    return {
        "project_id": project_id,
        "status": rec.get("status"),
        "run": rec,
        "summary": rec.get("summary"),
        "gate": rec.get("gate"),
    }
