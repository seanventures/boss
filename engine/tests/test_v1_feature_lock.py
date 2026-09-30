"""v1 feature lock stays evidence-only: Cloud/Ent never Community shipped."""

from pathlib import Path

from boss_engine.services.v1_feature_lock import (
    apply_v1_lock,
    default_v1_feature_lock_path,
    load_v1_feature_lock,
    lock_rows_are_honest,
    match_v1_lock_row,
    parse_v1_feature_lock,
    summarize_v1_feature_lock,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
LOCK_PATH = default_v1_feature_lock_path(REPO_ROOT)


def test_lock_file_parses_and_keeps_cloud_enterprise_unshipped():
    assert LOCK_PATH.is_file(), "docs/product/V1_FEATURE_LOCK.md is the v1 lock"
    rows = load_v1_feature_lock(LOCK_PATH)
    assert rows, "lock file must contain parseable rows"
    assert lock_rows_are_honest(rows) == []

    buckets = {row["lock"] for row in rows}
    assert buckets == {"in", "out", "cloud_enterprise"}

    ce_rows = [row for row in rows if row["lock"] == "cloud_enterprise"]
    assert ce_rows
    assert all(row["features"] in {"✦", "◐"} for row in ce_rows)
    assert all(row["features"] != "✅" for row in ce_rows)

    titles = {row["title"] for row in ce_rows}
    assert "Managed multi-tenant model gateway" in titles
    assert "Self-improvement automation routes" in titles
    assert "Hosted Account Connect & seats" in titles


def test_apply_v1_lock_does_not_mark_commercial_as_community_shipped():
    markdown = """
| id | feature | edition | lock | features | match | notes |
| V1-IN-001 | Durable tasks, plans, jobs (Postgres) | community | in | ✅ | durable tasks | |
| V1-CE-001 | Managed multi-tenant model gateway | cloud | cloud_enterprise | ✦ | model gateway | |
"""
    rows = parse_v1_feature_lock(markdown)
    community = match_v1_lock_row(
        title="Durable tasks, plans, jobs (Postgres)",
        label="V1-IN-001",
        rows=rows,
    )
    commercial = match_v1_lock_row(
        title="Managed multi-tenant model gateway",
        label="V1-CE-001",
        rows=rows,
    )
    assert community is not None
    assert commercial is not None

    col, meta = apply_v1_lock(
        column="built", catalog_status="Implemented", lock_row=community
    )
    assert col == "built"
    assert meta["community_shipped"] is True

    col, meta = apply_v1_lock(
        column="built", catalog_status="Implemented", lock_row=commercial
    )
    assert col != "built"
    assert meta["community_shipped"] is False
    assert meta["v1_lock"] == "cloud_enterprise"
    assert meta["v1_features_mark"] == "✦"


def test_partial_community_row_is_not_shipped():
    row = {
        "id": "V1-IN-046",
        "title": "Metatron captain (persistent session / goals / watch)",
        "edition": "community",
        "lock": "in",
        "features": "◐",
        "match": [],
    }
    col, meta = apply_v1_lock(
        column="built", catalog_status="Implemented", lock_row=row
    )
    assert col == "building"
    assert meta["community_shipped"] is False


def test_summarize_lock_is_in_out_cloud_counts_only():
    rows = load_v1_feature_lock(LOCK_PATH)
    summary = summarize_v1_feature_lock(rows)
    assert set(summary) == {"in", "out", "cloud"}
    assert summary["in"] == sum(1 for row in rows if row["lock"] == "in")
    assert summary["out"] == sum(1 for row in rows if row["lock"] == "out")
    assert summary["cloud"] == sum(
        1 for row in rows if row["lock"] == "cloud_enterprise"
    )
    assert summary["in"] >= 65
    assert summary["out"] >= 14
    assert summary["cloud"] == 12
    assert all(isinstance(value, int) and value >= 0 for value in summary.values())


def test_load_v1_feature_lock_on_packaged_image_path_is_non_empty(tmp_path):
    """Production images root Settings.repo_root at /app.

    The lock is copied to /app/docs/product/V1_FEATURE_LOCK.md. A missing file
    loads as [] and GET /v1/v1-lock returns {in:0, out:0, cloud:0} — honest
    zeros, not invented counts. The packaged path must parse non-empty.
    """
    packaged_root = tmp_path / "app"
    dest = default_v1_feature_lock_path(packaged_root)
    dest.parent.mkdir(parents=True)
    dest.write_bytes(LOCK_PATH.read_bytes())

    rows = load_v1_feature_lock(dest)
    assert rows, "packaged lock path must parse to non-empty rows"
    assert lock_rows_are_honest(rows) == []

    summary = summarize_v1_feature_lock(rows)
    assert set(summary) == {"in", "out", "cloud"}
    assert summary["in"] > 0
    assert summary["out"] > 0
    assert summary["cloud"] > 0
    assert all(row["lock"] != "ga" for row in rows)
    assert all(
        row["features"] != "✅" for row in rows if row["lock"] == "cloud_enterprise"
    )


def test_missing_packaged_lock_is_empty_not_invented():
    missing = default_v1_feature_lock_path("/app")
    if missing.is_file():
        return
    rows = load_v1_feature_lock(missing)
    assert rows == []
    assert summarize_v1_feature_lock(rows) == {"in": 0, "out": 0, "cloud": 0}
