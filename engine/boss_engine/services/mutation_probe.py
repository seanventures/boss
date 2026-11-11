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
4. THE PLANT IS CODE, NOT A MODEL (BOSSAPP-1004, audit 997 §7.7). The probe spent a
   write-capable model session (87 calls, $13.93/month) to flip one operator, while the
   re-probe already replanted without a model (`reapply`). `plant` / `plant_first` now flip
   the first boundary-shaped operator deterministically — same input, same mutant, no model,
   and no write-capable session left in the panel's attack surface.
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
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


#: The operator flips the deterministic plant knows, tried in order at each position; the
#: longest match at the earliest position wins. Each one changes a boundary or a branch —
#: exactly the one-line, spec-violating bug the probe's prompt used to ask a model for.
_FLIPS: tuple[tuple[str, re.Pattern], ...] = (
    ("===", re.compile(r"===")),
    ("!==", re.compile(r"!==")),
    ("==", re.compile(r"(?<![=!<>+\-*/%&|^])==(?!=)")),
    ("!=", re.compile(r"(?<![=!<>])!=(?!=)")),
    ("<=", re.compile(r"(?<![<=])<=(?!=)")),
    (">=", re.compile(r"(?<![>=])>=(?!=)")),
    ("<", re.compile(r"(?<= )<(?= )")),
    (">", re.compile(r"(?<= )>(?= )")),
    ("&&", re.compile(r"&&")),
    ("||", re.compile(r"\|\|")),
    ("and", re.compile(r"(?<= )and(?= )")),
    ("or", re.compile(r"(?<= )or(?= )")),
    ("True", re.compile(r"\bTrue\b")),
    ("true", re.compile(r"\btrue\b(?!['\"-])")),
    ("+", re.compile(r"(?<= )\+(?= )")),
)
_FLIP_TO = {"===": "!==", "!==": "===", "==": "!=", "!=": "==", "<=": ">", ">=": "<",
            "<": ">=", ">": "<=", "&&": "||", "||": "&&", "and": "or", "or": "and",
            "True": "False", "true": "false", "+": "-"}

#: Lines the plant never touches: comments, imports, and lines whose flip would be invisible
#: to a test that imports the module (`__main__` guards) or would change tooling, not behaviour.
_SKIP_LINE = re.compile(
    r"^\s*(?:#|//|/\*|\*|<!--|--\s|'''|\"\"\"|import\s|from\s[\w.]+\s+import\b|package\s)"
    r"|__name__|__main__")


def _in_string(line: str, pos: int) -> bool:
    """Approximately: is `pos` inside a quoted string on this line? Counts unescaped quotes
    before it; an odd count of either kind means inside. Approximate on purpose — a flip
    inside a string is a survivable false target, not a crash."""
    head = line[:pos]
    for quote in ("'", '"', "`"):
        if len(re.findall(rf"(?<!\\){re.escape(quote)}", head)) % 2:
            return True
    return False


@dataclass(frozen=True)
class Planted:
    """One deterministic mutant: where it is, what it changed, and the mutated file text."""

    path: str
    mutated: str
    line: int          # 1-based
    before: str        # the line as written
    after: str         # the line as planted
    operator: str      # which flip ("==" -> "!=", …)

    @property
    def description(self) -> str:
        return (f"Mutation probe (deterministic plant): {self.path}:{self.line} changed "
                f"`{self.before.strip()}` to `{self.after.strip()}` "
                f"(`{self.operator}` -> `{_FLIP_TO[self.operator]}`).")


def plant(path: str, text: str) -> Optional[Planted]:
    """The first boundary-shaped operator in `text`, flipped — or None when the file holds
    nothing the plant knows how to flip. Deterministic: the same text plants the same bug."""
    lines = (text or "").splitlines(keepends=True)
    for i, raw in enumerate(lines):
        if _SKIP_LINE.search(raw):
            continue
        best: Optional[tuple[int, int, str, re.Match]] = None  # (pos, -len, op, match)
        for op, pattern in _FLIPS:
            for m in pattern.finditer(raw):
                if _in_string(raw, m.start()):
                    continue
                key = (m.start(), -len(op))
                if best is None or key < (best[0], best[1]):
                    best = (m.start(), -len(op), op, m)
                break  # only the first non-string match of this operator matters
        if best is None:
            continue
        _pos, _neg, op, m = best
        flipped = raw[:m.start()] + _FLIP_TO[op] + raw[m.end():]
        if flipped == raw:
            continue
        mutated = "".join(lines[:i]) + flipped + "".join(lines[i + 1:])
        if mutated == text:
            continue
        return Planted(path=path, mutated=mutated, line=i + 1,
                       before=raw.rstrip("\n"), after=flipped.rstrip("\n"), operator=op)
    return None


def plant_first(targets: Iterable[str], read: Callable[[str], Optional[str]]) -> Optional[Planted]:
    """One mutant across `targets` (the first file, in sorted order, that yields one).
    `read(path)` returns the file's text or None. None when no target can be mutated —
    recorded as a probe that did not run, never a model fallback."""
    for p in sorted({str(t) for t in targets or ()}):
        try:
            text = read(p)
        except Exception:  # noqa: BLE001 — an unreadable file is simply not a target
            continue
        if not isinstance(text, str) or not text:
            continue
        planted = plant(p, text)
        if planted is not None:
            return planted
    return None


def apply_command(planted: Planted) -> str:
    """A shell command that writes the mutated file — how the plant reaches a micro-VM guest,
    where the suite runs: the echo engine clones the work branch and runs test commands, so the
    first command plants the bug and the rest run the suite against it. Base64 keeps the file
    text out of shell quoting."""
    import base64
    import shlex

    payload = base64.b64encode((planted.mutated or "").encode("utf-8")).decode("ascii")
    return f"printf %s {payload} | base64 -d > {shlex.quote(planted.path)}"


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
