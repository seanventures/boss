"""Two guards for what builders write (dry run 3, change #19).

1. CONFIG-ONLY CHANGES ADD NO CONTENT-PINNING TESTS. A remediation (input_fidelity: "fix this
   given file") is checked by the repository's validator and contract tests, never by a new test
   that asserts the file's bytes (`lumenpay_remediated_test.go` pinned "the secret is gone",
   `contract_tests_yaml_test.go` pinned the contract file). Such a test freezes one answer into
   the suite: the next person's override then fails "old tests". ``pinning_tests`` names the
   test files a build added that read the remediated file; the build drops them.

2. LESSON IDS NEVER LAND IN CODE. A builder that read "Learned so far" wrote
   `// per kgn_…` into a Go test. A lesson id means nothing in the repository and leaks BOSS's
   memory into the customer's code. ``strip_lesson_ids`` removes ids from the comment part of
   lines a build added; ids already on the branch, and ids in code (a string literal in BOSS's
   own fixtures), are left alone.
"""
from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Optional

#: Said to every stage of a remediation build (input_fidelity.brief).
CONFIG_ONLY_TESTS_RULE = (
    "- This is a config-only change: it needs NO new test file. Never add or edit a test that "
    "reads, parses or asserts the contents of the file being fixed (or any config file): the "
    "repository's validator and contract tests are its checks, and BOSS removes such a test.")

LESSON_ID_RE = re.compile(r"[ \t]*[(\[]?\bkgn_[0-9a-f]{6,}\b[)\]]?[,;]?")
_COMMENT_RE = re.compile(r"//|#|/\*|^\s*\*|--|<!--")


def strip_lesson_ids(text: str, base: Optional[str] = None) -> str:
    """``text`` with lesson ids removed from the comment part of every line that is not
    already in ``base`` (the file as the branch holds it). Unchanged when there is nothing to
    strip, so the bytes of an untouched file never move."""
    if not isinstance(text, str) or "kgn_" not in text:
        return text
    kept = set((base or "").splitlines())
    out: list[str] = []
    changed = False
    for line in text.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        if "kgn_" in body and body not in kept:
            m = _COMMENT_RE.search(body)
            if m and LESSON_ID_RE.search(body, m.start()):
                head, tail = body[:m.start()], body[m.start():]
                tail = LESSON_ID_RE.sub("", tail).rstrip()
                new = (head + tail).rstrip() if tail.strip() else head.rstrip()
                line = new + line[len(body):]
                changed = True
        out.append(line)
    return "".join(out) if changed else text


def _names(target: str) -> list[str]:
    p = PurePosixPath(str(target or ""))
    names = [str(p)] if str(p) not in ("", ".") else []
    if p.name and p.name not in names:
        names.append(p.name)
    return names


def pins_target(test_text: Optional[str], target: str) -> bool:
    """Whether a test file reads the remediated file: it names its path or its file name."""
    if not test_text:
        return False
    return any(n and n in test_text for n in _names(target))


def pinning_tests(root: Any, test_files: Iterable[str], contract: Mapping[str, Any], *,
                  preexisting: Iterable[str] = ()) -> list[str]:
    """The test files THIS build added (not in ``preexisting``) that read the remediated file
    (its output, or its input when another path). Never raises."""
    root = Path(root)
    targets = [str(contract.get(k) or "") for k in ("output", "input")]
    before = {str(p) for p in preexisting or []}
    found: list[str] = []
    for rel in test_files or []:
        rel = str(rel)
        if rel in before:
            continue
        try:
            text = (root / rel).read_text(errors="ignore")
        except OSError:
            continue
        if any(t and pins_target(text, t) for t in targets):
            found.append(rel)
    return found
