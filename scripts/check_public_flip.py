#!/usr/bin/env python3
"""Mechanical public-flip readiness checks (Plane BOSS-293).

This script NEVER authorizes a public flip, NEVER changes LICENSE, NEVER
enables GitHub Actions, and NEVER stamps BOSS-293 / BOSS-300 Done.

It fails closed while the OSS licensing decision is still UNDECIDED
(BOSS-300 / docs/planning/oss-readiness/DECISION-oss-licensing-strategy.md).

Mechanical passes (SECURITY.md exists, claims freeze exists, no committed
.env) are printed for honesty. They are not approval to change visibility.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).resolve().parents[1]

LICENSE_DECISION = Path("docs/planning/oss-readiness/DECISION-oss-licensing-strategy.md")
SECURITY_MD = Path("engine/SECURITY.md")
CLAIMS_FREEZE = Path("docs/operations/COMMUNITY_GA_CLAIMS_FREEZE.md")
CHECKLIST = Path("docs/operations/PUBLIC_FLIP_CHECKLIST.md")

DECIDED = "DECIDED"
UNDECIDED = "UNDECIDED"

# Placeholder text currently in engine/SECURITY.md — a public-release blocker.
SECURITY_PLACEHOLDER_MARKERS = (
    "not yet configured",
    "public security reporting address or private reporting url is not yet",
    "must remain private",
)

# Tracked paths that are allowed to look like env files.
ENV_EXAMPLE_SUFFIXES = (".env.example", ".env.sample", ".env.tmpl", ".env.template")
ENV_BASENAMES = {".env", "app.env"}
CREDENTIAL_BASENAMES = {
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "credentials.json",
    "service-account.json",
    "plane_api_key",
}


@dataclass(frozen=True)
class CheckResult:
    id: str
    title: str
    ok: bool
    detail: str
    blocker: bool


def parse_license_status(text: str) -> str:
    """Return DECIDED only when the decision header is explicitly decided.

    Fail closed on OPEN / UNDECIDED / BLOCKED / missing / ambiguous status.
    A recommendation in the body is not a decision.
    """
    if not (text or "").strip():
        return UNDECIDED

    # Prefer the first Status: line (the decision header).
    header = None
    for line in text.splitlines():
        match = re.search(r"(?i)\*\*Status:\s*([^*]+)\*\*", line)
        if match:
            header = match.group(1).strip()
            break
        match = re.search(r"(?i)^>\s*\*\*Status:\s*([^*]+)\*\*", line)
        if match:
            header = match.group(1).strip()
            break
        match = re.search(r"(?i)^\s*Status:\s*(.+)$", line)
        if match:
            header = match.group(1).strip()
            break

    blob = header if header is not None else text
    upper = blob.upper()
    if re.search(r"\bUNDECIDED\b", upper) or re.search(r"\bOPEN\b", upper) or re.search(
        r"\bBLOCKED\b", upper
    ):
        return UNDECIDED
    if re.search(r"\bDECIDED\b", upper):
        return DECIDED
    return UNDECIDED


def committed_secret_paths(tracked: Sequence[str]) -> list[str]:
    """Return tracked paths that look like real env / credential files."""
    hits: list[str] = []
    for raw in tracked:
        path = raw.replace("\\", "/")
        if path.startswith("./"):
            path = path[2:]
        name = path.rsplit("/", 1)[-1]
        lower = name.lower()
        if any(path.endswith(suffix) or lower.endswith(suffix) for suffix in ENV_EXAMPLE_SUFFIXES):
            continue
        if lower in ENV_BASENAMES or lower.startswith(".env.") or lower == ".env":
            hits.append(path)
            continue
        if lower in CREDENTIAL_BASENAMES:
            hits.append(path)
            continue
        if lower.endswith((".pem", ".p12", ".pfx")) and "test" not in path.lower():
            hits.append(path)
    return sorted(set(hits))


def list_tracked_files(root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"],
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "git ls-files failed; cannot prove no .env is committed. "
            f"stderr={result.stderr.decode('utf-8', 'replace').strip() or 'empty'}"
        )
    return [p for p in result.stdout.decode("utf-8", "replace").split("\0") if p]


def _file_text(root: Path, relative: Path) -> str | None:
    path = root / relative
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8", errors="replace")


def security_reporting_configured(text: str) -> bool:
    """True only when a real channel is present and the placeholder is gone."""
    lowered = (text or "").lower()
    if any(marker in lowered for marker in SECURITY_PLACEHOLDER_MARKERS):
        return False
    has_mailbox = bool(re.search(r"[\w.+-]+@[\w.-]+\.[a-z]{2,}", text or "", re.I))
    has_advisory = "security/advisories" in lowered or "private vulnerability reporting" in lowered
    return has_mailbox or has_advisory


def evaluate(
    root: Path,
    tracked: Sequence[str] | None = None,
) -> dict[str, object]:
    checks: list[CheckResult] = []

    license_text = _file_text(root, LICENSE_DECISION)
    if license_text is None:
        license_status = UNDECIDED
        checks.append(
            CheckResult(
                id="license_decision",
                title="BOSS-300 license decision is DECIDED",
                ok=False,
                detail=f"missing {LICENSE_DECISION.as_posix()} — treat as UNDECIDED",
                blocker=True,
            )
        )
    else:
        license_status = parse_license_status(license_text)
        checks.append(
            CheckResult(
                id="license_decision",
                title="BOSS-300 license decision is DECIDED",
                ok=license_status == DECIDED,
                detail=(
                    f"{LICENSE_DECISION.as_posix()} status={license_status}. "
                    "Engine Apache-2.0 files are not a substitute. "
                    "Do not invent a license or close BOSS-300 from this script."
                ),
                blocker=True,
            )
        )

    security_text = _file_text(root, SECURITY_MD)
    checks.append(
        CheckResult(
            id="security_md_exists",
            title="engine/SECURITY.md exists",
            ok=security_text is not None,
            detail=(
                f"file present: {SECURITY_MD.as_posix()}"
                if security_text is not None
                else f"missing {SECURITY_MD.as_posix()}"
            ),
            blocker=True,
        )
    )
    reporting_ok = bool(security_text) and security_reporting_configured(security_text or "")
    checks.append(
        CheckResult(
            id="security_private_reporting",
            title="SECURITY.md names a private reporting channel",
            ok=reporting_ok,
            detail=(
                "private reporting channel configured"
                if reporting_ok
                else "placeholder / missing private reporting destination — public-release blocker"
            ),
            blocker=True,
        )
    )

    claims_text = _file_text(root, CLAIMS_FREEZE)
    claims_ok = bool(claims_text) and (
        "Allowed claims" in (claims_text or "") and "Forbidden" in (claims_text or "")
    )
    checks.append(
        CheckResult(
            id="claims_freeze",
            title="Community claims freeze doc exists",
            ok=claims_ok,
            detail=(
                f"file present: {CLAIMS_FREEZE.as_posix()}"
                if claims_ok
                else f"missing or incomplete {CLAIMS_FREEZE.as_posix()}"
            ),
            blocker=True,
        )
    )

    checklist_text = _file_text(root, CHECKLIST)
    checks.append(
        CheckResult(
            id="public_flip_checklist",
            title="PUBLIC_FLIP_CHECKLIST.md exists",
            ok=checklist_text is not None,
            detail=(
                f"file present: {CHECKLIST.as_posix()}"
                if checklist_text is not None
                else f"missing {CHECKLIST.as_posix()}"
            ),
            blocker=False,
        )
    )

    tracked_error = None
    tracked_list: list[str]
    if tracked is None:
        try:
            tracked_list = list_tracked_files(root)
        except RuntimeError as exc:
            tracked_list = []
            tracked_error = str(exc)
    else:
        tracked_list = list(tracked)

    secret_hits = committed_secret_paths(tracked_list) if tracked_error is None else []
    env_ok = tracked_error is None and not secret_hits
    if tracked_error:
        env_detail = tracked_error
    elif secret_hits:
        env_detail = "committed secret-like paths: " + ", ".join(secret_hits)
    else:
        env_detail = "no committed .env / credential files (examples allowed)"
    checks.append(
        CheckResult(
            id="no_committed_env",
            title="No committed .env / credential files",
            ok=env_ok,
            detail=env_detail,
            blocker=True,
        )
    )

    ready = all(check.ok for check in checks)
    return {
        "ready": ready,
        "verdict": "READY" if ready else "BLOCKED",
        "license_status": license_status,
        "plane": {"checklist": "BOSS-293", "license": "BOSS-300"},
        "note": (
            "Passing this script is necessary but not sufficient for a public flip. "
            "It does not run a full-history secret scan, does not flip visibility, "
            "does not enable Actions, and does not close Plane issues."
        ),
        "checks": [asdict(check) for check in checks],
    }


def render_report(report: dict[str, object]) -> str:
    lines = [
        f"PUBLIC FLIP: {report['verdict']}",
        f"license_status={report['license_status']}  plane={report['plane']}",
        "",
    ]
    for check in report["checks"]:  # type: ignore[union-attr]
        mark = "PASS" if check["ok"] else "FAIL"
        blocker = " blocker" if check["blocker"] and not check["ok"] else ""
        lines.append(f"  [{mark}] {check['id']:28} {check['title']}{blocker}")
        lines.append(f"         {check['detail']}")
    lines.append("")
    lines.append(str(report["note"]))
    if report["license_status"] != DECIDED:
        lines.append(
            "FAIL-CLOSED: OSS license is UNDECIDED (BOSS-300). "
            "Do not flip public. Do not stamp BOSS-293 or BOSS-300 Done."
        )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    report = evaluate(args.root.resolve())
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(render_report(report))
    return 0 if report["ready"] else 1


if __name__ == "__main__":
    sys.exit(main())
