"""Loads the checked-in dependency snapshot."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class DependencyRecord:
    name: str
    current_version: str
    latest_version: str | None = None


def load_snapshot(path: Path) -> list[DependencyRecord]:
    data = json.loads(Path(path).read_text())
    return [
        DependencyRecord(
            name=entry["name"],
            current_version=entry["current_version"],
            latest_version=entry.get("latest_version"),
        )
        for entry in data
    ]
