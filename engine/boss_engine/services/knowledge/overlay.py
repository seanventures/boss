"""The per-run branch overlay (KNOWLEDGE_GRAPH_SPEC.md §3.5; operator decision 2026-10-21).

The stored code graph describes the default branch only. During a build, queries see the
default branch **plus that build's own diff**: this overlay, derived from the run's working tree,
held in memory for that run only and discarded when the run ends. It is never written into the
stored graph.

What the overlay does to a query:
- the build's changed files and the symbols they now define become **seeds**, so their
  neighbourhood and facts are found even when the plan does not name them;
- the module map marks a symbol "[changed in this build]", and lists one the build added
  "[new in this build]";
- a fact about a symbol the build deleted is marked `stale_in_this_build`.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional  # noqa: F401

from sqlalchemy import text

_LOCK = threading.Lock()
_OVERLAYS: dict[str, "Overlay"] = {}
MAX_OVERLAYS = 256


@dataclass
class Overlay:
    run_id: str
    project_id: str
    changed_paths: set[str] = field(default_factory=set)
    defined: set[str] = field(default_factory=set)    # symbol names the changed files define now
    base_defined: set[str] = field(default_factory=set)  # ...and defined on the default branch

    def seed_names(self) -> list[str]:
        """Changed paths and the symbols they define; the query resolves them under the reader's filter."""
        return sorted(self.changed_paths | (self.defined & self.base_defined))

    def changes(self, name: str, path: Optional[str]) -> bool:
        return (path in self.changed_paths) or (name in self.defined and name in self.base_defined)

    def added_names(self) -> list[str]:
        return sorted(self.defined - self.base_defined)

    def deleted_names(self) -> set[str]:
        return self.base_defined - self.defined

    def annotate(self, facts: list[dict]) -> list[dict]:
        gone = {n.lower() for n in self.deleted_names()}
        return [{**f, "stale_in_this_build": True} if str(f.get("subject", "")).lower() in gone else f for f in facts]


def build(c, project_id: str, run_id: str, files: dict[str, Optional[bytes]]) -> Overlay:
    """The overlay for one run from its diff: {path: new content, or None for a deleted file}."""
    from .code_parse import language_for, parse_file

    ov = Overlay(run_id=run_id, project_id=project_id, changed_paths=set(files))
    for path, source in files.items():
        if source is None or language_for(path) is None:
            continue
        facts = parse_file(path, source)
        for d in facts.defs:
            ov.defined.add(d.qualname)   # code_index names a symbol by its qualname
    rows = c.execute(text(
        "SELECT id, name, path FROM kg_nodes WHERE project_id = :p AND valid_to IS NULL AND kind = 'symbol' "
        "AND path = ANY(:paths)"), {"p": project_id, "paths": sorted(files)}).all()
    for r in rows:
        ov.base_defined.add(r.name)
    # A deleted file's symbols are gone; a changed file's base symbols no longer defined are gone too.
    return ov


def from_worktree(c, project_id: str, run_id: str, root: Path, changed: Iterable[str]) -> Overlay:
    files = {}
    for rel in changed:
        p = Path(root) / rel
        files[rel] = p.read_bytes() if p.is_file() else None
    return build(c, project_id, run_id, files)


def hold(ov: Overlay) -> Overlay:
    with _LOCK:
        if len(_OVERLAYS) >= MAX_OVERLAYS:
            _OVERLAYS.pop(next(iter(_OVERLAYS)), None)
        _OVERLAYS[ov.run_id] = ov
    return ov


def for_run(run_id: Optional[str]) -> Optional[Overlay]:
    if not run_id:
        return None
    with _LOCK:
        return _OVERLAYS.get(run_id)


def discard(run_id: str) -> None:
    with _LOCK:
        _OVERLAYS.pop(run_id, None)
