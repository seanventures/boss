#!/usr/bin/env python3
"""Draw a blast-radius evaluation sample from a repository, independently of the knowledge graph.

    sample_symbols.py --repo-dir ../saleor --seed 20261022 --exclude results/blast_radius_saleor_truth.json

Symbols come straight from the parser (code_parse), not from an index, so the sample can be drawn
and its answer key written before the tool is run. Criteria (the same as the first sample):
non-test, non-migration definitions whose short name is defined once in the repository, at
least 8 characters, not private, appearing (`git grep -lw`) in 3-20 files. Quotas: 14 functions,
10 classes, 6 methods or properties. Prints the sample and writes per-symbol grep evidence files.
"""
from __future__ import annotations

import argparse
import collections
import json
import random
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "engine"))

from boss_engine.services.knowledge.code_index import list_files  # noqa: E402
from boss_engine.services.knowledge.code_parse import language_for, parse_file  # noqa: E402

QUOTA = {"function": 14, "class": 10, "method": 6}


def is_test(path: str) -> bool:
    name, slashed = Path(path).name, f"/{path}"
    return (any(d in slashed for d in ("/tests/", "/__tests__/", "/__mocks__/", "/__fixtures__/", "/migrations/",
                                       "/integration-tests/"))
            or name.startswith("test_") or ".spec." in name or ".test." in name)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--repo-dir", required=True)
    p.add_argument("--seed", type=int, required=True)
    p.add_argument("--exclude", nargs="*", default=[])
    p.add_argument("--evidence-dir")
    p.add_argument("--lang", default="python")
    p.add_argument("--quota", help="JSON quota override, e.g. '{\"function\": 8, \"class\": 5, \"method\": 2}'")
    args = p.parse_args()
    root = Path(args.repo_dir)
    quota = json.loads(args.quota) if args.quota else QUOTA
    files = [f for f in list_files(root)
             if (language_for(f) == args.lang or args.lang in ("typescript", "tsx")
                                     and language_for(f) in ("typescript", "tsx"))]
    defs, names = [], collections.Counter()
    for f in files:
        facts = parse_file(f, (root / f).read_bytes())
        for d in facts.defs:
            names[d.qualname.rsplit(".", 1)[-1]] += 1
            if not is_test(f):
                defs.append((f, d.qualname, "method" if d.kind == "property" else d.kind))
    excluded = set()
    for path in args.exclude:
        excluded |= set(json.loads(Path(path).read_text())["symbols"])
    rng = random.Random(args.seed)
    rng.shuffle(defs)
    picked = collections.defaultdict(list)
    for f, qual, kind in defs:
        short = qual.rsplit(".", 1)[-1]
        if f"{f}::{qual}" in excluded or names[short] != 1 or len(short) < 8 or short.startswith("_"):
            continue
        if kind not in quota or len(picked[kind]) >= quota[kind]:
            continue
        hits = subprocess.run(["git", "grep", "-lw", short], cwd=root, capture_output=True, text=True).stdout.splitlines()
        if not 3 <= len(hits) <= 20:
            continue
        picked[kind].append(f"{f}::{qual}")
        if args.evidence_dir:
            ev = subprocess.run(["git", "grep", "-nw", short], cwd=root, capture_output=True, text=True).stdout
            out = Path(args.evidence_dir) / (short + ".txt")
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(ev)
        if sum(len(v) for v in picked.values()) >= sum(quota.values()):
            break
    print(json.dumps({"seed": args.seed, "symbols": {k: v for k, v in picked.items()}}, indent=2))


if __name__ == "__main__":
    main()
