"""The Semgrep rule packs BOSS scans with: which rules, and their digest (spec §4.2, §4.7 S4.6).

Stdlib only: Dockerfile.community runs this file on its own while building the `security`
toolchain stage (`python rulepack.py select <semgrep-rules checkout> <dest>`), and the scan
records `digest(<dest>)` on every scan so a before/after report can tell when the rules differ.

The packs are the language directories of the pinned semgrep-rules commit (the source of the
registry's p/javascript, p/typescript, p/python, p/java packs, whose security rules are what
p/owasp-top-ten curates) plus generic/secrets (p/secrets). Selected at image build, never fetched
at scan time: Semgrep runs with `--config <dest>` and `--metrics=off`.

The digest is over CONTENT, not the downloaded archive: sha256 of the sorted
"<relative path>\\0<sha256 of the file>\\n" lines. An archive GitHub re-gzips still verifies.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sys
from pathlib import Path

#: Directories of semgrep-rules that become BOSS's packs.
PACK_DIRS = ("javascript", "typescript", "python", "java", "generic/secrets")


def is_rule_file(rel: str) -> bool:
    name = rel.rsplit("/", 1)[-1]
    return (name.endswith(".yaml") or name.endswith(".yml")) and ".test." not in name


def rule_files(root: Path) -> list[str]:
    out = []
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            rel = str((Path(dirpath) / name).relative_to(root)).replace(os.sep, "/")
            if is_rule_file(rel):
                out.append(rel)
    return sorted(out)


def digest(root: Path | str) -> str:
    base = Path(root)
    h = hashlib.sha256()
    for rel in rule_files(base):
        h.update(rel.encode() + b"\0" + hashlib.sha256((base / rel).read_bytes()).hexdigest().encode() + b"\n")
    return "sha256:" + h.hexdigest()


def select(src: Path | str, dest: Path | str) -> str:
    src, dest = Path(src), Path(dest)
    for pack in PACK_DIRS:
        root = src / pack
        if not root.is_dir():
            raise SystemExit(f"rule pack {pack} is missing from {src}")
        for rel in rule_files(root):
            target = dest / pack / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(root / rel, target)
    return digest(dest)


if __name__ == "__main__":  # pragma: no cover - the image build
    if len(sys.argv) == 4 and sys.argv[1] == "select":
        print(select(sys.argv[2], sys.argv[3]))
    elif len(sys.argv) == 3 and sys.argv[1] == "digest":
        print(digest(sys.argv[2]))
    else:
        raise SystemExit("usage: rulepack.py select <src> <dest> | digest <dir>")
