"""Parse the Community v1 feature lock (in / out / Cloud-Enterprise).

Used by release-planning ``sync-from-features`` as a small honesty hook.
Does not rewrite the feature catalog.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

LOCK_VALUES = frozenset({"in", "out", "cloud_enterprise"})
FEATURES_MARKS = frozenset({"✅", "◐", "○", "✦"})
DEFAULT_LOCK_RELPATH = Path("docs") / "product" / "V1_FEATURE_LOCK.md"


def _first_mark(value: str) -> str:
    token = (value or "").strip().split()[0] if (value or "").strip() else ""
    return token if token in FEATURES_MARKS else ""


def parse_v1_feature_lock(markdown: str) -> list[dict[str, Any]]:
    """Parse lock tables. Unknown tables are ignored."""
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line in markdown.splitlines():
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 5:
            continue
        lock_id, feature, edition, lock, features_cell = cells[:5]
        if not lock_id or lock_id.lower() in {"id", "lock_id"}:
            continue
        if set(lock_id) <= {"-", ":"}:
            continue
        if lock not in LOCK_VALUES:
            continue
        mark = _first_mark(features_cell)
        if not mark:
            continue
        if lock_id in seen:
            continue
        seen.add(lock_id)
        match_tokens = []
        if len(cells) >= 6:
            match_tokens = [
                token.strip().lower()
                for token in cells[5].split(",")
                if token.strip()
            ]
        rows.append(
            {
                "id": lock_id,
                "title": feature,
                "edition": edition,
                "lock": lock,
                "features": mark,
                "match": match_tokens,
            }
        )
    return rows


def load_v1_feature_lock(path: Path | str | None) -> list[dict[str, Any]]:
    if path is None:
        return []
    lock_path = Path(path)
    if not lock_path.is_file():
        return []
    return parse_v1_feature_lock(lock_path.read_text(encoding="utf-8"))


def default_v1_feature_lock_path(repo_root: Path | str) -> Path:
    return Path(repo_root) / DEFAULT_LOCK_RELPATH


def match_v1_lock_row(
    *,
    title: str | None,
    label: str | None,
    rows: list[dict[str, Any]],
) -> Optional[dict[str, Any]]:
    title_l = (title or "").strip().lower()
    label_l = (label or "").strip().lower()
    for row in rows:
        if title_l and title_l == str(row.get("title") or "").strip().lower():
            return row
        if label_l and label_l == str(row.get("id") or "").strip().lower():
            return row
    if title_l:
        for row in rows:
            for token in row.get("match") or []:
                if token and token in title_l:
                    return row
    return None


def apply_v1_lock(
    *,
    column: str,
    catalog_status: str | None,
    lock_row: Optional[dict[str, Any]],
) -> tuple[str, dict[str, Any]]:
    """Return (column, metadata). Never marks Cloud/Enterprise as Community shipped."""
    if not lock_row:
        return column, {"community_shipped": False, "v1_lock": None}

    lock = lock_row.get("lock")
    mark = lock_row.get("features") or ""
    meta: dict[str, Any] = {
        "v1_lock": lock,
        "v1_lock_id": lock_row.get("id"),
        "v1_edition": lock_row.get("edition"),
        "v1_features_mark": mark,
        "community_shipped": False,
    }
    status = (catalog_status or "").strip()

    if lock == "cloud_enterprise":
        if column == "built":
            column = "building"
        meta["community_shipped"] = False
        return column, meta

    if lock == "out":
        if column == "built":
            column = "planned"
        meta["community_shipped"] = False
        return column, meta

    if lock == "in":
        shipped = mark == "✅" and status == "Implemented" and column == "built"
        meta["community_shipped"] = shipped
        if not shipped and column == "built" and mark != "✅":
            column = "building"
        return column, meta

    return column, meta


def summarize_v1_feature_lock(rows: list[dict[str, Any]]) -> dict[str, int]:
    """Small lock summary: in / out / cloud counts only. Not a shipment claim."""
    summary = {"in": 0, "out": 0, "cloud": 0}
    for row in rows:
        lock = row.get("lock")
        if lock == "in":
            summary["in"] += 1
        elif lock == "out":
            summary["out"] += 1
        elif lock == "cloud_enterprise":
            summary["cloud"] += 1
    return summary


def lock_rows_are_honest(rows: list[dict[str, Any]]) -> list[str]:
    """Return honesty violations (empty list means the lock file is clean)."""
    violations: list[str] = []
    for row in rows:
        if row.get("lock") == "cloud_enterprise" and row.get("features") == "✅":
            violations.append(
                f"{row.get('id')}: Cloud/Enterprise row must stay ✦/◐, not ✅"
            )
        if row.get("lock") not in LOCK_VALUES:
            violations.append(f"{row.get('id')}: unknown lock {row.get('lock')!r}")
        if row.get("features") not in FEATURES_MARKS:
            violations.append(
                f"{row.get('id')}: features mark {row.get('features')!r} is not evidence-only"
            )
    return violations
