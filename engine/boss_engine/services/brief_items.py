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
    """The lines of a report that answer `key`. A bare number key (a brief with no ledger keys)
    must carry the ITEM/COVERAGE prefix, so "1. fixed the cache" prose answers nothing."""
    prefix = r"(?:ITEM|COVERAGE)\s+" if key.isdigit() else r"(?:(?:ITEM|COVERAGE)\s+)?"
    rx = re.compile(r"(?im)^[\s>*\-`#]*" + prefix + re.escape(key)
                    + r"(?![\w-])[\s*`:.)\]-]*(.*)$")
    return [m.group(1).strip() for m in rx.finditer(str(summary or ""))]


def clip_summary(text: Optional[str], limit: int = 8000, head: int = 3000) -> str:
    """A harness's summary for the pipeline, keeping its HEAD and its TAIL: the ITEM/COVERAGE
    report comes last, and cutting at the first `limit` characters threw it away."""
    text = str(text or "")
    if len(text) <= limit:
        return text
    tail = max(0, limit - head - 24)
    return text[:head] + "\n[... middle cut ...]\n" + text[-tail:]


_FIXED = re.compile(r"(?i)^\**\s*fixed\b[\s*:\-—–]*(.*)$")
_NOT_FIXED = re.compile(r"(?i)^\**\s*not[\s_-]*fixed\b[\s*:\-—–]*(.*)$")
_PATH = re.compile(r"`?([\w./-]*[\w-]\.[A-Za-z0-9]+|[\w.-]+/[\w./-]+|Dockerfile|Makefile)(?::\d+)?`?")
_CODE_EXT = frozenset({"go", "py", "js", "jsx", "mjs", "ts", "tsx", "java", "kt", "rs", "rb", "cs",
                       "c", "cc", "cpp", "h", "hpp", "php", "swift", "scala"})


def _paths(text: str) -> list[str]:
    return [m.group(1) for m in _PATH.finditer(str(text or ""))]


def is_code_item(item: Item) -> bool:
    """A finding anchored to a source file (not docs, config or build files): only a test can
    stand for it."""
    return any(p.rsplit(".", 1)[-1].lower() in _CODE_EXT for p in _paths(item.text.split(": ", 1)[0]))


def _same_file(a: str, b: str) -> bool:
    a, b = a.strip("./"), b.strip("./")
    return a == b or a.endswith("/" + b) or b.endswith("/" + a)


@dataclass
class Assessment:
    """What an implementer's report amounts to, checked against the files it changed."""
    fixed: list[Item]                 # FIXED, and a file it names was changed
    missing: list[Item]               # no line at all
    unbacked: list[Item]              # FIXED, but no file it names was changed
    not_fixed: list[tuple[Item, str]]  # NOT-FIXED, with its reason

    @property
    def send_back(self) -> list[Item]:
        return self.missing + self.unbacked

    @property
    def not_done(self) -> list[Item]:
        """Every item the coder did not fix: judged by review and acceptance."""
        return self.missing + self.unbacked + [i for i, _ in self.not_fixed]

    def record(self) -> dict:
        return {"fixed": [i.key for i in self.fixed], "missing": [i.key for i in self.missing],
                "unbacked": [i.key for i in self.unbacked],
                "not_fixed": [{"key": i.key, "reason": r[:200]} for i, r in self.not_fixed]}


def assess(items: Iterable[Item], summary: Optional[str],
           changed: Iterable[str]) -> Assessment:
    """A FIXED claim counts only when a file it names is among the files actually changed; a
    NOT-FIXED counts as answered (with its reason) but is not a fix; no line is no answer."""
    changed = [str(c) for c in changed or ()]
    out = Assessment([], [], [], [])
    for it in items or ():
        verdict = None
        reason = ""
        for rest in _reports(summary or "", it.key):
            f, n = _FIXED.match(rest), _NOT_FIXED.match(rest)
            if f and any(_same_file(p, c) for p in _paths(f.group(1) or "") for c in changed):
                verdict = "fixed"
                break
            if f and _paths(f.group(1) or ""):
                verdict = verdict or "unbacked"
            elif n and len((n.group(1) or "").strip()) >= 3:
                verdict, reason = "not_fixed", n.group(1).strip()
        if verdict == "fixed":
            out.fixed.append(it)
        elif verdict == "unbacked":
            out.unbacked.append(it)
        elif verdict == "not_fixed":
            out.not_fixed.append((it, reason))
        else:
            out.missing.append(it)
    return out


def unaddressed(items: Iterable[Item], summary: Optional[str],
                changed: Iterable[str] = ()) -> list[Item]:
    """The items to send the implementer back for (no line, or a FIXED no changed file backs)."""
    return assess(items, summary, changed).send_back


def judge_block(not_done: Iterable[Item], excused: Iterable[Item] = ()) -> str:
    """The list review and acceptance are handed: items the coder did not fix, and items no
    test covers, each to be judged against the tree."""
    nd, ex = list(not_done or ()), list(excused or ())
    out = ""
    if nd:
        out += ("\n\nITEMS THE CODER DID NOT FIX (it reported them not fixed, skipped them, or "
                "named no file it changed). JUDGE EACH against the tree; an unfixed one is a "
                "finding:\n" + "\n".join(f"  - {i.line()}" for i in nd))
    if ex:
        out += ("\n\nITEMS NO UNIT TEST COVERS (the test writer said they are not "
                "unit-testable). VERIFY EACH in the tree:\n"
                + "\n".join(f"  - {i.line()}" for i in ex))
    return out + ("\n" if out else "")


def pr_lines(record: Optional[dict]) -> list[str]:
    """The PR body's lines for what the brief's items came to."""
    r = record or {}
    if not r.get("items"):
        return []
    left = ([f"{k} (no report)" for k in r.get("missing") or []]
            + [f"{k} (claimed fixed, no such file changed)" for k in r.get("unbacked") or []]
            + [f"{x['key']} (not fixed: {x['reason']})" for x in r.get("not_fixed") or []])
    out = [f"- Brief items: {len(r.get('fixed') or [])} of {r['items']} fixed by the coder"
           + (", reworked after a send-back" if r.get("reworked") else "")
           + (", rework reverted (it broke the tests)" if r.get("reverted") else "")]
    out += [f"  - ⚠ not fixed: {x}" for x in left]
    out += [f"  - to verify (no unit test): {k}" for k in r.get("excused") or []]
    return out


UNTESTABLE = "not unit-testable"


def coverage_note(items: Iterable[Item]) -> str:
    """The test writer's list: a test for each testable item, or the stated reason it has none."""
    items = list(items or ())
    if not items:
        return ""
    return ("\n\nTHE BRIEF'S OPEN ITEMS. Write a test for EACH item a unit test can pin (a "
            "behaviour, an output, a config value the code reads). A finding in SOURCE CODE "
            "needs a test; only an item no test can judge (prose in a README, a deploy file, a "
            "build flag) may go without one.\n"
            + "\n".join(f"  {i}. {it.line()}" for i, it in enumerate(items, 1))
            + "\n\nEND your reply with one line per item, using the item's key exactly:\n"
              "  COVERAGE <key>: <TestName>[, <TestName>]\n"
              f"  COVERAGE <key>: {UNTESTABLE} — verified by acceptance/review\n"
              "An item with no line is treated as skipped.\n")


def _coverage(items, summary, defined):
    """(uncovered, excused): an item is covered by a test its line names EXACTLY and the suite
    defines; excused when the line says it is not unit-testable and it is not a code finding."""
    have = set(defined or ())
    uncovered, excused = [], []
    for it in items or ():
        ok = excuse = False
        for rest in _reports(summary or "", it.key):
            if UNTESTABLE in rest.lower():
                excuse = True
                continue
            parts = [p.strip(" `.*") for p in rest.split(",")] + [rest.strip(" `.*")]
            if any(p in have for p in parts if p):
                ok = True
        if ok:
            continue
        if excuse and not is_code_item(it):
            excused.append(it)
        else:
            uncovered.append(it)
    return uncovered, excused


def uncovered(items: Iterable[Item], summary: Optional[str],
              defined: Iterable[str]) -> list[Item]:
    """Items with no test and no allowed excuse. An EMPTY report covers nothing."""
    return _coverage(items, summary, defined)[0]


def excused(items: Iterable[Item], summary: Optional[str],
            defined: Iterable[str]) -> list[Item]:
    """Items the test writer excused (and may be excused): verified by review, not a test."""
    return _coverage(items, summary, defined)[1]


__all__ = ["Assessment", "Item", "MAX_ITEMS", "UNTESTABLE", "assess", "checklist_note",
           "clip_summary", "coverage_note", "excused", "is_code_item", "judge_block", "parse",
           "pr_lines", "sendback_note", "unaddressed", "uncovered"]
