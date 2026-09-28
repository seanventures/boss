"""Unit tests for scripts/check_public_flip.py (no network, no git rewrite)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "scripts" / "check_public_flip.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("check_public_flip", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


flip = _load_module()


def test_parse_license_status_open_is_undecided():
    text = (
        "# DECISION\n\n"
        "> **Status: OPEN — needs a product + legal call before the OSS engine split.**\n"
    )
    assert flip.parse_license_status(text) == flip.UNDECIDED


def test_parse_license_status_undecided_token():
    assert flip.parse_license_status("Status: UNDECIDED") == flip.UNDECIDED


def test_parse_license_status_decided_header():
    text = (
        "# DECISION\n\n"
        "> **Status: DECIDED — Option C on 2099-01-01 by Legal.**\n\n"
        "Recommendation remains historical.\n"
    )
    assert flip.parse_license_status(text) == flip.DECIDED


def test_parse_license_status_ambiguous_fails_closed():
    assert flip.parse_license_status("we should pick option C") == flip.UNDECIDED
    assert flip.parse_license_status("") == flip.UNDECIDED


def test_committed_secret_paths_allows_examples_only():
    tracked = [
        ".env.example",
        "apps/api/.env.example",
        "app.env.tmpl",
        "docs/readme.md",
    ]
    assert flip.committed_secret_paths(tracked) == []


def test_committed_secret_paths_flags_real_env():
    tracked = [".env", ".env.bak-edition", "apps/api/.env.local", "secrets/id_rsa"]
    hits = flip.committed_secret_paths(tracked)
    assert ".env" in hits
    assert ".env.bak-edition" in hits
    assert "apps/api/.env.local" in hits
    assert "secrets/id_rsa" in hits


def test_security_reporting_rejects_placeholder():
    placeholder = (
        "The public security reporting address or private reporting URL is not yet\n"
        "configured. Until an approved destination is recorded here, the repository\n"
        "must remain private.\n"
    )
    assert flip.security_reporting_configured(placeholder) is False


def test_security_reporting_accepts_mailbox_without_placeholder():
    text = (
        "Report vulnerabilities privately to security@example.com\n"
        "or via GitHub private vulnerability reporting.\n"
    )
    assert flip.security_reporting_configured(text) is True


def _write_kit(tmp_path: Path, *, status: str, reporting: str) -> Path:
    (tmp_path / "docs/operations").mkdir(parents=True)
    (tmp_path / "docs/operations/OSS_LICENSING_DECISION.md").write_text(
        f"> **Status: {status} — test fixture.**\n",
        encoding="utf-8",
    )
    (tmp_path / "SECURITY.md").write_text(reporting, encoding="utf-8")
    (tmp_path / "docs/operations/COMMUNITY_GA_CLAIMS_FREEZE.md").write_text(
        "## Allowed claims\n\n- x\n\n## Forbidden claims\n\n- y\n",
        encoding="utf-8",
    )
    (tmp_path / "docs/operations/PUBLIC_FLIP_CHECKLIST.md").write_text(
        "# Public-flip checklist fixture\n",
        encoding="utf-8",
    )
    return tmp_path


def test_evaluate_fails_when_license_undecided_even_if_mechanical_pass(tmp_path: Path):
    _write_kit(
        tmp_path,
        status="OPEN",
        reporting="Report to security@example.com via private vulnerability reporting.\n",
    )
    report = flip.evaluate(tmp_path, tracked=[".env.example", "README.md"])
    assert report["license_status"] == flip.UNDECIDED
    assert report["mechanical_pass"] is False
    assert report["verdict"] == "BLOCKED"
    by_id = {c["id"]: c for c in report["checks"]}
    assert by_id["license_decision"]["ok"] is False
    assert by_id["no_committed_env"]["ok"] is True
    assert by_id["security_md_exists"]["ok"] is True
    assert by_id["claims_freeze"]["ok"] is True


def test_evaluate_fails_on_committed_env_even_if_license_decided(tmp_path: Path):
    _write_kit(
        tmp_path,
        status="DECIDED",
        reporting="Report to security@example.com via private vulnerability reporting.\n",
    )
    report = flip.evaluate(tmp_path, tracked=[".env", "README.md"])
    assert report["license_status"] == flip.DECIDED
    assert report["mechanical_pass"] is False
    by_id = {c["id"]: c for c in report["checks"]}
    assert by_id["no_committed_env"]["ok"] is False


def test_evaluate_mechanical_pass_does_not_claim_public_readiness(tmp_path: Path):
    _write_kit(
        tmp_path,
        status="DECIDED",
        reporting="Report to security@example.com via private vulnerability reporting.\n",
    )
    report = flip.evaluate(tmp_path, tracked=[".env.example"])
    assert report["license_status"] == flip.DECIDED
    assert report["mechanical_pass"] is True
    assert report["verdict"] == "MECHANICAL_PASS"
    assert "ready" not in report
    assert "does not authorize publication" in report["note"]


def test_repo_tree_has_recorded_license_decision_only():
    """The local mechanical subset passes; BOSS-293 release gates remain open."""
    report = flip.evaluate(ROOT)
    assert report["license_status"] == flip.DECIDED
    assert report["mechanical_pass"] is True
    by_id = {c["id"]: c for c in report["checks"]}
    assert by_id["license_decision"]["ok"] is True
    assert by_id["security_md_exists"]["ok"] is True
    assert by_id["claims_freeze"]["ok"] is True
    assert by_id["public_flip_checklist"]["ok"] is True
    assert by_id["no_committed_env"]["ok"] is True
    assert by_id["security_private_reporting"]["ok"] is True
