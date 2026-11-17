"""The open items a follow-up brief lists, and whether each was ADDRESSED (BOSSAPP-1355).

A re-check's "Fix these" follow-up carried 26 open items. The test writer wrote tests for the one
that was unit-testable, the implementer made the frozen tests pass and reported "No further
changes needed", and the other 25 (README examples, the compose mount, a Makefile target, a
Dockerfile flag) were never touched: the frozen tests are a floor, not the definition of done.

So a brief that lists items is held to ALL of them:
  * `parse` reads the numbered checklist out of the brief's findings section;
  * the implementer is told to report each item `ITEM <key>: FIXED <file:line>` or
    `ITEM <key>: NOT-FIXED <reason>` (`checklist_note`), and `unaddressed` names the items its
    report skipped, so the pipeline can send it back once with them;
  * the test writer reports each item `COVERAGE <key>: <TestName>` or
    `COVERAGE <key>: not unit-testable — verified by acceptance/review` (`coverage_note`), and
    `uncovered` names the items it skipped or whose named test is not in the suite.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Optional

#: The heading the follow-up builder puts its findings under (governed._spawn_deferred_findings_task).
_SECTION = re.compile(
    r"(?m)^(?:FAILING CHECKS AND OPEN FINDINGS|DEFERRED FINDINGS)[^\n]*:\s*\n")
_BULLET = re.compile(r"^\s*-\s+\[([A-Za-z]+)\]\s+(.+)$")
_KEY = re.compile(r"^(F-\d+)\b")
#: A gate's own failure is the consequence of the other items, never an item to fix on its own.
_GATE_LINE = re.compile(r"^(?:F-\d+[^:]*:\s*)?Failing gate\b", re.I)

#: The most items the checklist enforces; the rest still ride the brief.
MAX_ITEMS = 80


@dataclass(frozen=True)
class Item:
    key: str        # `F-12` (the findings ledger's key) or the item's number in the list
    text: str
    severity: str = "major"

    def line(self) -> str:
        return f"{self.key}: {self.text[:300]}"


def parse(instructions: Optional[str]) -> list[Item]:
    """The items of a follow-up brief, in order, without repeats; [] when the brief lists none
    (a plain build keeps today's behaviour)."""
    text = str(instructions or "")
    m = _SECTION.search(text)
    if not m:
        return []
    out: list[Item] = []
    seen: set[str] = set()
    for ln in text[m.end():].splitlines():
        if not ln.strip():
            break
        b = _BULLET.match(ln)
        if not b:
            continue
        body = " ".join(b.group(2).split())
        if _GATE_LINE.match(body):
            continue
        k = _KEY.match(body)
        key = k.group(1) if k else str(len(out) + 1)
        if key in seen:
            continue
        seen.add(key)
        out.append(Item(key=key, text=body, severity=b.group(1).lower()))
        if len(out) >= MAX_ITEMS:
            break
    return out


def checklist_note(items: Iterable[Item]) -> str:
    """The implementer's checklist: done means every item addressed, whatever the frozen tests
    cover."""
    items = list(items or ())
    if not items:
        return ""
    return ("\n\nTHE BRIEF'S OPEN ITEMS. The frozen tests are a FLOOR, not the definition of "
            "done: you are done when EVERY item below is addressed, including the ones no test "
            "covers (docs, config, build files). Fix each one in the tree.\n"
            + "\n".join(f"  {i}. {it.line()}" for i, it in enumerate(items, 1))
            + "\n\nEND your reply with one line per item, using the item's key exactly:\n"
              "  ITEM <key>: FIXED <file:line>\n"
              "  ITEM <key>: NOT-FIXED <the reason it cannot or should not be fixed>\n"
              "An item with no line is treated as skipped and you will be sent back for it.\n")


def sendback_note(missing: Iterable[Item]) -> str:
    """What a send-back tells the implementer: the items its report left out."""
    missing = list(missing or ())
    return ("Your report left these brief items unaddressed. Fix each one now (docs and config "
            "files count), then END with an `ITEM <key>: FIXED <file:line>` or "
            "`ITEM <key>: NOT-FIXED <reason>` line for each:\n"
            + "\n".join(f"  - {it.line()}" for it in missing))


def _reports(summary: str, key: str) -> list[str]:
    rx = re.compile(r"(?im)^[\s>*\-`#]*(?:(?:ITEM|COVERAGE)\s+)?" + re.escape(key)
                    + r"(?![\w-])[\s*`:.)\]-]*(.*)$")
    return [m.group(1).strip() for m in rx.finditer(str(summary or ""))]


_FIXED = re.compile(r"(?i)^\**\s*fixed\b[\s*:\-—–]*(.*)$")
_NOT_FIXED = re.compile(r"(?i)^\**\s*not[\s_-]*fixed\b[\s*:\-—–]*(.*)$")
_PATHISH = re.compile(r"\S+\.\w+|\S+:\d+|`[^`]+`")


def unaddressed(items: Iterable[Item], summary: Optional[str]) -> list[Item]:
    """The items the implementer's report does not answer: no line for the key, a FIXED with no
    place, or a NOT-FIXED with no reason."""
    out = []
    for it in items or ():
        ok = False
        for rest in _reports(summary or "", it.key):
            f, n = _FIXED.match(rest), _NOT_FIXED.match(rest)
            if f and _PATHISH.search(f.group(1) or ""):
                ok = True
            elif n and len((n.group(1) or "").strip()) >= 3:
                ok = True
        if not ok:
            out.append(it)
    return out


UNTESTABLE = "not unit-testable"


def coverage_note(items: Iterable[Item]) -> str:
    """The test writer's list: a test for each testable item, or the stated reason it has none."""
    items = list(items or ())
    if not items:
        return ""
    return ("\n\nTHE BRIEF'S OPEN ITEMS. Write a test for EACH item a unit test can pin (a "
            "behaviour, an output, a config value the code reads). An item no test can judge "
            "(prose in a README, a deploy file, a build flag) gets no test.\n"
            + "\n".join(f"  {i}. {it.line()}" for i, it in enumerate(items, 1))
            + "\n\nEND your reply with one line per item, using the item's key exactly:\n"
              "  COVERAGE <key>: <TestName>[, <TestName>]\n"
              f"  COVERAGE <key>: {UNTESTABLE} — verified by acceptance/review\n"
              "An item with no line is treated as skipped.\n")


def uncovered(items: Iterable[Item], summary: Optional[str],
              defined: Iterable[str]) -> list[Item]:
    """The items the test writer skipped, or covered by a test the suite does not define.
    `defined` is the set of test names the tree's test files define (brief_tests.defined)."""
    have = set(defined or ())
    out = []
    for it in items or ():
        ok = False
        for rest in _reports(summary or "", it.key):
            if UNTESTABLE in rest.lower():
                ok = True
                continue
            names = re.findall(r"[A-Za-z_][\w./]*", rest)
            # A JS/TS test is titled by a sentence: its whole title, quoted or not, counts.
            if (any(n in have or n.split("/")[-1] in have for n in names)
                    or any(len(h) >= 8 and h in rest for h in have)):
                ok = True
        if not ok:
            out.append(it)
    return out


__all__ = ["Item", "MAX_ITEMS", "UNTESTABLE", "checklist_note", "coverage_note",
           "parse", "sendback_note", "unaddressed", "uncovered"]
