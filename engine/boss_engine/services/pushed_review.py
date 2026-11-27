"""Identify the submitted files of an existing local change independently of the worktree diff."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from . import local_repo


@dataclass
class SubmittedChange:
    paths: list[str]
    documents_only: bool


def inspect(git_dir: Path, base: str, head: str) -> SubmittedChange:
    # Commit IDs come from the stored change. No rename detection: a rename out of code
    # still includes the removed source path. Inspect both tree modes, including deletions.
    raw = local_repo.git(["diff", "--raw", "--no-renames", "-z", f"{base}...{head}", "--"],
                         git_dir=git_dir, raw=True)
    fields = raw.split(b"\0")[:-1]
    paths = []
    documents_only = bool(fields)
    for header, name in zip(fields[::2], fields[1::2], strict=True):
        old, new = header[1:].split()[:2]
        path = name.decode("utf-8", "replace")
        paths.append(path)
        documents_only = documents_only and prose_file(path, old) and prose_file(path, new)
    if documents_only:
        # A repository with any code, executable, symlink, manifest or test keeps its
        # normal build and test gates, even when this particular edit touches a document.
        tree = local_repo.git(["ls-tree", "-r", "-z", head], git_dir=git_dir, raw=True)
        for entry in tree.split(b"\0")[:-1]:
            meta, name = entry.split(b"\t", 1)
            if not prose_file(name.decode("utf-8", "replace"), meta.split()[0]):
                documents_only = False
                break
    return SubmittedChange(paths, documents_only)


def prose_file(path: str, mode: bytes) -> bool:
    p = PurePosixPath(path)
    return (mode in {b"000000", b"100644"} and p.suffix.lower() in {".md", ".rst", ".txt"}
            and all(not component.startswith(".") for component in p.parts)
            and p.name.lower() != "cmakelists.txt"
            and not p.name.lower().startswith(("requirements", "constraints"))
            and not {"requirements", "constraints"}.intersection(part.lower() for part in p.parts))
