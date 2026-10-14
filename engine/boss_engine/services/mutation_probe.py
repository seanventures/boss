"""What the mutation probe may mutate, whether its verdict can cost a round, and its mutant.

The probe (`tests_strong_enough`) plants one bug in the implementation and expects the suite to
fail. Three rules, all here so the first probe and a fix round's re-probe obey the same ones:

1. PASS 1 DOES NOT OWE TEST STRENGTH. Its test author is told "no P2 (edge cases, error paths,
   contract detail)" (_test_scope_clause), so a boundary-flip mutant surviving it is the scope
   working, not a weak suite. On pass 1 the probe's verdict is recorded as advisory information
   and buys no round; the More tests pass (pass 2) owns test strength. Before this, a surviving
   mutant bought up to two rounds that changed nothing and shipped anyway on advisory profiles.
2. A FIX ROUND RE-PROBES THE SAME MUTANT. A new model-planted bug each round measured a
   different thing every time; the strengthened suite must catch the bug it was strengthened
   against (`capture` / `reapply`).
3. ONLY FILES THE ACCEPTANCE CRITERIA COVER, NEVER A CONNECTOR'S. A connector is pre-tested in its
   own tier and its tests are not this build's; config, docs and manifests carry no behaviour a
   criterion names (`targets`).
"""
from __future__ import annotations

import difflib
import re
from pathlib import PurePosixPath
from typing import Callable, Iterable, Optional

#: Source a behaviour lives in. Everything else (config, docs, manifests, styles) is not a target.
_SOURCE_SUFFIXES = (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".py", ".go", ".java", ".kt",
                    ".rb", ".rs", ".cs", ".php", ".scala", ".swift", ".vue", ".svelte")
_CONFIG_NAME = re.compile(
    r"(?i)(?:^|[./_-])(?:config|conf|setup|settings|eslint|prettier|vite|vitest|jest|playwright|"
    r"tailwind|postcss|webpack|rollup|babel|tsconfig|next|nuxt|svelte\.config|seed|migrat\w*)"
    r"(?:[./_-]|$)")
#: Names too common to say a file is the connector's (`index.ts` is in every app).
_GENERIC_NAMES = frozenset({"index", "main", "app", "server", "routes", "router", "config",
                            "utils", "types", "db", "api"})
_STOP = frozenset({"with", "that", "this", "from", "have", "when", "into", "each", "their", "they",
                   "them", "then", "must", "should", "will", "can", "able", "user", "users",
                   "page", "shows", "show", "list", "data", "only", "every", "after", "before",
                   "which", "where", "there", "what", "your", "about", "also", "more", "than"})


def strength_is_advisory(*, more_tests_pass: bool) -> bool:
    """True on pass 1: the probe's verdict is information, never a failed gate or a round."""
    return not more_tests_pass


def _is_connector_file(path: str, connector_files: Iterable[str]) -> bool:
    p = str(path).replace("\\", "/").lstrip("/")
    base = PurePosixPath(p).name
    stem = base.split(".", 1)[0].lower()
    for raw in connector_files or ():
        tp = str(raw).replace("\\", "/").lstrip("/")
        if not tp:
            continue
        if p == tp or p.endswith("/" + tp):
            return True
        if PurePosixPath(tp).name == base and stem not in _GENERIC_NAMES:
            return True
    return False


def _criteria_words(criteria: Iterable[str]) -> set[str]:
    words: set[str] = set()
    for c in criteria or ():
        for w in re.findall(r"[a-zA-Z][a-zA-Z0-9]{3,}", str(c)):
            w = w.lower()
            if w not in _STOP:
                words.add(w.rstrip("s") if len(w) > 4 else w)
    return words


def targets(impl_files: Iterable[str], criteria: Iterable[str], *,
            connector_files: Iterable[str] = (),
            read: Optional[Callable[[str], Optional[str]]] = None) -> list[str]:
    """The implementation files the probe may mutate.

    Source files only, never a connector's, never config/seed/migration; of those, the files a
    criterion's words appear in (path or text). When no file mentions one, every remaining
    source file (the criteria may be phrased in words the code does not use)."""
    connector_files = list(connector_files or ())
    source = []
    for p in impl_files or ():
        p = str(p)
        base = PurePosixPath(p.replace("\\", "/")).name
        if not base.lower().endswith(_SOURCE_SUFFIXES) or _CONFIG_NAME.search(base):
            continue
        if _is_connector_file(p, connector_files):
            continue
        source.append(p)
    words = _criteria_words(criteria)
    if not words:
        return source
    covered = []
    for p in source:
        hay = p.lower()
        if read is not None:
            try:
                hay += "\n" + (read(p) or "").lower()
            except Exception:  # noqa: BLE001 — an unreadable file is judged by its path
                pass
        if any(w in hay for w in words):
            covered.append(p)
    return covered or source


#: One file's mutant: (text before, text after) of each changed block, enough to plant it again.
Mutant = dict[str, list[tuple[str, str]]]


def capture(before: dict[str, str], after: dict[str, str]) -> Mutant:
    """The edits the mutator made, per file, as (old block, new block) pairs."""
    out: Mutant = {}
    for path, old in before.items():
        new = after.get(path)
        if new is None or new == old:
            continue
        a, b = old.splitlines(keepends=True), new.splitlines(keepends=True)
        blocks = []
        for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_opcodes():
            if tag == "equal":
                continue
            # Anchor a pure insertion on the line before it, so it has text to find.
            lo = i1 - 1 if (i1 == i2 and i1 > 0) else i1
            jlo = j1 - (i1 - lo)
            blocks.append(("".join(a[lo:i2]), "".join(b[jlo:j2])))
        if blocks:
            out[path] = blocks
    return out


def reapply(current: str, blocks: list[tuple[str, str]]) -> Optional[str]:
    """`current` with the same mutant planted again, or None when it cannot be placed exactly
    (an old block missing or ambiguous: the code moved under it)."""
    text = current
    for old, new in blocks:
        if not old or text.count(old) != 1:
            return None
        text = text.replace(old, new, 1)
    return text if text != current else None
