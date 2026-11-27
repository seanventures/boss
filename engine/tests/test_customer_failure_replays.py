"""Deterministic replay tests for scrubbed customer failure records (BOSSAPP-1434)."""
from __future__ import annotations

from pathlib import Path

import pytest

from boss_engine.services import customer_failure_replay as cfr
from boss_engine.services import private_infra

FIXTURES_ROOT = Path(__file__).resolve().parent / "fixtures" / "customer_replays"


def test_at_least_three_customer_failure_records_exist():
    records = cfr.discover_records(FIXTURES_ROOT)
    assert len(records) >= 3
    ids = {rec["id"] for _, rec in records}
    assert {
        "northgate_breaker_quoted_db",
        "helpdesk_shared_repo_needs_java",
        "helpdesk_deploy_res_db_eai_again",
    } <= ids


@pytest.mark.parametrize(
    "record_path",
    sorted(FIXTURES_ROOT.glob("*/record.yaml")),
    ids=lambda p: p.parent.name,
)
def test_customer_failure_record_is_scrubbed_valid_and_replays_deterministically(
    record_path: Path, tmp_path: Path
):
    raw_text = record_path.read_text(encoding="utf-8")
    assert private_infra.findings(raw_text) == []

    record = cfr.load_record(record_path)
    assert cfr.validate_record(record) == []

    result = cfr.replay_record(record, work_dir=tmp_path / record["id"])
    assert isinstance(result, dict)


def test_validate_record_rejects_unredacted_secrets_unmasked_hosts_and_missing_fields():
    base = cfr.load_record(FIXTURES_ROOT / "northgate_breaker_quoted_db" / "record.yaml")

    bad_secret = {**base, "raw_evidence": "token=ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4"}
    errs = cfr.validate_record(bad_secret)
    assert any("unredacted secret" in e for e in errs), errs

    bad_openai = {**base, "symptom": "failed with sk-" + "proj1234567890abcdef1234567890"}
    errs = cfr.validate_record(bad_openai)
    assert any("unredacted secret" in e for e in errs), errs

    bad_ip = {**base, "raw_evidence": "failed connecting to 203.0.113.42:8000"}
    errs = cfr.validate_record(bad_ip)
    assert any("unmasked host or IP" in e for e in errs), errs

    bad_unscrubbed = {**base, "scrubbed": False}
    errs = cfr.validate_record(bad_unscrubbed)
    assert any("scrubbed must be boolean true" in e for e in errs), errs

    missing = {"schema": cfr.SCHEMA_VERSION, "id": "only_id"}
    errs = cfr.validate_record(missing)
    assert any("missing required field" in e for e in errs), errs
