"""Time a Saleor code index and score a hand-checked symbol list.

Python files are parsed with the stdlib AST, which is the indexer backend
for Python. Prints one JSON object. Does not spend model credit.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "engine"))

from boss_engine.services.knowledge.code_index import parse_source  # noqa: E402


def _python_files(root: Path) -> list[Path]:
    skip = {".git", "__pycache__", ".venv", "venv", "node_modules"}
    found = []
    for path in root.rglob("*.py"):
        if any(part in skip for part in path.parts):
            continue
        found.append(path)
    return sorted(found)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--handcheck", required=True)
    args = parser.parse_args()
    root = Path(args.root).resolve()
    hand = json.loads(Path(args.handcheck).read_text())
    files = _python_files(root)
    started = time.perf_counter()
    symbols = 0
    errors = 0
    parsed_bytes = 0
    for path in files:
        source = path.read_text(encoding="utf-8", errors="replace")
        parsed_bytes += len(source.encode("utf-8"))
        try:
            symbols += len(parse_source(str(path.relative_to(root)), "python", source).symbols)
        except SyntaxError:
            errors += 1
    full_seconds = time.perf_counter() - started
    largest = max(files, key=lambda path: path.stat().st_size)
    sample_source = largest.read_text(encoding="utf-8", errors="replace") + "\n"
    incremental_started = time.perf_counter()
    parse_source(str(largest.relative_to(root)), "python", sample_source)
    incremental_seconds = time.perf_counter() - incremental_started
    checked = []
    missed = []
    for item in hand["symbols"]:
        path = root / item["path"]
        source = path.read_text(encoding="utf-8")
        found = parse_source(item["path"], "python", source).symbols
        if item["name"] in found:
            checked.append(item["name"])
        else:
            missed.append(item)
    ratio = len(checked) / len(hand["symbols"]) if hand["symbols"] else 0
    sha = subprocess.check_output(
        ["git", "-C", str(root), "rev-parse", "HEAD"], text=True
    ).strip()
    print(
        json.dumps(
            {
                "root": str(root),
                "sha": sha,
                "sha_expected": hand.get("sha"),
                "sha_matches": sha == hand.get("sha"),
                "python_files": len(files),
                "bytes": parsed_bytes,
                "symbols": symbols,
                "syntax_errors": errors,
                "full_index_seconds": round(full_seconds, 3),
                "incremental_one_file_seconds": round(incremental_seconds, 6),
                "incremental_file": str(largest.relative_to(root)),
                "incremental_bytes": largest.stat().st_size,
                "handcheck_count": len(hand["symbols"]),
                "handcheck_found": len(checked),
                "handcheck_ratio": ratio,
                "handcheck_missed": missed,
                "parser": "python_ast",
                "source_sha256": hashlib.sha256(
                    Path(args.handcheck).read_bytes()
                ).hexdigest(),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
