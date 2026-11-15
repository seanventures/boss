"""A merge left a bare `=======` line in db/schema.sql (2026-11-15): every fresh install would fail
to apply the schema. Guard every tracked SQL/Python/TS/Go/YAML file against conflict markers."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MARKER = re.compile(r"^(<{7}( |$)|={7}$|>{7}( |$))")
SUFFIXES = (".sql", ".py", ".ts", ".tsx", ".go", ".yaml", ".yml", ".sh")


def _tracked() -> list[Path]:
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True)
    return [ROOT / p for p in out.stdout.splitlines() if p.endswith(SUFFIXES)]


def test_no_tracked_file_carries_a_merge_conflict_marker():
    offenders = []
    for path in _tracked():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError):
            continue
        for n, line in enumerate(text.splitlines(), 1):
            if MARKER.match(line):
                offenders.append(f"{path.relative_to(ROOT)}:{n}")
    assert not offenders, "merge conflict markers left in: " + ", ".join(offenders[:20])
