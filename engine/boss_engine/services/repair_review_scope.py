"""What a repair attempt's reviewers re-read: what the repair changed, not the whole change again.

Why
---
HelpDesk AI, "Add SLA timers to tickets" (2026-11-06). Attempt 1 (crun_c8e2a5f8) was reviewed
in full and failed on a missing DST test, an untested route and a test file the gate never ran.
Its repair (crun_b32b4a26) was SEEDED from attempt 1's workspace and changed four files: the
root package.json script and three new test files. Its first panel then re-read the WHOLE
21-file change from scratch: review_code and consistency_check each spent ~7 min (round 0) to
confirm a handful of fixes, before the in-run round re-reviewed again. The operator asked
"why these are taking so long, 58m … can we cut wait times".

The in-run fix round already re-reviews only what the fix changed (``_fix_round_change``).
This module gives the SAME scoping to a repair attempt's first panel, against the change the
parent's seat last reviewed:

* the files that differ from that reviewed change (a unified diff where BOSS holds the reviewed
  text, the whole file where it does not),
* the seat's own findings from that review, to confirm fixed or raise again,
* the instruction to look for regressions the repair introduced.

QUALITY GUARDRAILS. A scoped re-review is a shortcut, so it is taken only when it is safe; any
doubt runs the FULL review exactly as before (same seat, same model routing, security_check
always full):

* NO PRIOR REVIEW: the seat never completed a review on the parent (hollow, not run), or the
  parent predates the reviewed-change snapshot this module records — nothing to scope against.
* TOO BIG: more than :data:`MAX_SCOPED_FILES` files or :data:`MAX_SCOPED_LINES` changed lines.
  A repair that size is a rework, not a fix; the scoped prompt would save little and lose the
  whole-change view the seat needs to judge how the pieces fit.
* UNRELATED FILES: the repair touched a non-test file no prior finding, failing test output or
  operator note names, or a test file that tests none of them. The seat's findings are then no
  longer a map of what to re-check.

The decision is model-agnostic: it reads recorded findings, file text and hashes, never an
engine's behaviour. Every decision is recorded (``code_task.repair_review_scope``) so the mode
that ran is visible on the run.
"""
from __future__ import annotations

import difflib
import hashlib
import re
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

from . import review_input

#: A repair over this many files runs the full review. In-run fix rounds recorded on the
#: HelpDesk and BOSS lineages touch 1-4 files; the SLA change they repaired was 21 files, so 8
#: is already ~40% of a medium feature — past that the "repair" is a second implementation.
MAX_SCOPED_FILES = 8
#: A repair over this many changed (+/-) lines runs the full review. The reviewer is handed up
#: to review_input.MAX_TOTAL (48,000 chars, ~1,200 lines) of change in a full review; 300 lines
#: is a quarter of that — above it the saving shrinks while the risk of judging new code out of
#: context grows. The real repair (crun_b32b4a26) was well under it.
MAX_SCOPED_LINES = 300

#: The seats a repair attempt may scope. security_check is never scoped: it is fast (0.46 min
#: on crun_b32b4a26) and a security regression can live anywhere the change reaches.
SCOPABLE_SEATS = ("review_code", "consistency_check")

SCOPED = "scoped"
FULL = "full"


def _hash(text: Optional[str]) -> str:
    return hashlib.sha256((text or "").encode("utf-8", "replace")).hexdigest()[:20]


def snapshot(change: review_input.ChangeUnderReview) -> dict[str, str]:
    """The change a seat judged, as {path: content hash}. Recorded with every review round so a
    later repair attempt knows exactly what was already reviewed. Lockfiles are never inlined
    for review, so they are not part of what was judged."""
    return {p: _hash(change.files.get(p)) if p in change.files else ""
            for p in change.paths if not review_input.is_lockfile(p)}


@dataclass
class ScopePlan:
    """One seat's decision: review the repair's delta (SCOPED) or the whole change (FULL)."""

    seat: str
    mode: str
    reason: str
    changed: dict[str, Optional[str]] = field(default_factory=dict)  # path -> text (None = removed)
    diffs: dict[str, str] = field(default_factory=dict)              # path -> unified diff
    changed_lines: int = 0
    prior_summary: str = ""
    baseline_run: str = ""
    baseline_round: Optional[int] = None

    @property
    def scoped(self) -> bool:
        return self.mode == SCOPED

    def record(self) -> dict:
        return {"mode": self.mode, "reason": self.reason, "files": sorted(self.changed),
                "changed_lines": self.changed_lines, "baseline_run": self.baseline_run,
                "baseline_round": self.baseline_round}


def last_completed_review(rounds: Iterable[dict], seat: str) -> Optional[dict]:
    """The latest recorded round in which `seat` really reviewed (not hollow, not not_run) and
    whose reviewed change was recorded. None when there is nothing to scope against."""
    best = None
    for entry in rounds or ():
        if not isinstance(entry, dict):
            continue
        s = ((entry.get("seats") or {}).get(seat) or {})
        if not s or s.get("hollow") or str(s.get("verdict") or "not_run") in ("not_run", "hollow"):
            continue
        if not str(s.get("summary") or "").strip() or not isinstance(entry.get("reviewed"), dict):
            continue
        if best is None or int(entry.get("round") or 0) >= int(best.get("round") or 0):
            best = entry
    return best


_SKIP_LINE = re.compile(r"(?im)^\s*REVIEWED:.*$")


def _named(path: str, text: str) -> bool:
    """Does `text` name `path`? The full path anywhere, or its basename as a whole word (a
    finding often says `package.json` or `sla.mjs` without the folder). A root-level file is
    not named by another folder's file of the same name (`web/package.json`)."""
    p = path.lower()
    low = text.lower()
    if re.search(r"(?<![\w./-])" + re.escape(p) + r"(?![\w-])", low):
        return True
    base = p.rsplit("/", 1)[-1]
    if "/" in p and len(base) >= 4:
        return bool(re.search(r"(?<![\w.-])" + re.escape(base) + r"(?![\w-])", low))
    return False


_TEST_MARKS = re.compile(r"(^test_|^test\.|_test$|_tests$|\.test$|\.spec$|_spec$)")


def _stem(path: str) -> str:
    base = path.replace("\\", "/").rsplit("/", 1)[-1].lower()
    stem = base.split(".", 1)[0] if not base.startswith(".") else base
    # foo.test.mjs -> "foo"; test_foo.py -> "foo"; foo_test.go -> "foo"
    return _TEST_MARKS.sub("", stem)


def _tests_a_named_file(path: str, named_stems: set[str]) -> bool:
    stem = _stem(path)
    tokens = set(re.split(r"[_.\-]", stem))
    for s in named_stems:
        if len(s) < 3:
            continue
        if stem == s or s in tokens or stem.startswith(s + "_") or stem.startswith(s + "-") \
                or stem.endswith("_" + s) or stem.endswith("-" + s):
            return True
    return False


def _diff(old: Optional[str], new: Optional[str], path: str) -> tuple[str, int]:
    a = (old or "").splitlines(keepends=True)
    b = (new or "").splitlines(keepends=True)
    lines = list(difflib.unified_diff(a, b, f"reviewed/{path}", f"repair/{path}", n=3))
    n = sum(1 for ln in lines if ln[:1] in "+-" and not ln.startswith(("+++", "---")))
    return "".join(lines), n


def plan(seat: str, *, parent_run_id: str, parent_rounds: Iterable[dict],
         current: review_input.ChangeUnderReview, baseline: Optional[dict] = None,
         named_text: str = "", is_test: Callable[[str], bool] = lambda _p: False,
         max_files: int = MAX_SCOPED_FILES, max_lines: int = MAX_SCOPED_LINES) -> ScopePlan:
    """Decide how `seat` reviews a repair attempt's first panel.

    `parent_rounds` is the parent's ``finalize_ctx.review_rounds``; `current` the whole change
    as it stands now; `baseline` the parent's workspace text ({path: text}) — used to show a
    diff when it is exactly the text that was reviewed; `named_text` the other evidence that
    names files the repair may legitimately touch (the parent's other seats' findings, failing
    test output, the operator's fix note)."""
    if seat not in SCOPABLE_SEATS:
        return ScopePlan(seat, FULL, "seat_always_full", baseline_run=parent_run_id)
    prior = last_completed_review(parent_rounds, seat) if parent_run_id else None
    if prior is None:
        return ScopePlan(seat, FULL, "no_prior_review", baseline_run=parent_run_id)
    reviewed: dict = prior.get("reviewed") or {}
    summary = str((prior["seats"][seat] or {}).get("summary") or "")
    now = snapshot(current)
    baseline = baseline or {}
    changed: dict[str, Optional[str]] = {}
    diffs: dict[str, str] = {}
    lines = 0
    for path in sorted(set(now) | set(reviewed)):
        if now.get(path) == reviewed.get(path) and path in now and path in reviewed:
            continue
        new = current.files.get(path) if path in now else None
        changed[path] = new
        old = baseline.get(path)
        if path in reviewed and old is not None and _hash(old) == reviewed[path]:
            text, n = _diff(old, new, path)
            diffs[path] = text
            lines += n
        else:
            # The reviewed text is not held: the whole file counts as changed.
            lines += max(1, (new or "").count("\n") + 1) if new is not None else 1
    out = ScopePlan(seat, FULL, "", changed=changed, diffs=diffs, changed_lines=lines,
                    prior_summary=summary, baseline_run=parent_run_id,
                    baseline_round=int(prior.get("round") or 0))
    if not changed:
        out.reason = "nothing_changed_since_review"
        out.mode = SCOPED
        return out
    if len(changed) > max_files:
        out.reason = f"too_many_files:{len(changed)}>{max_files}"
        return out
    if lines > max_lines:
        out.reason = f"too_many_lines:{lines}>{max_lines}"
        return out
    evidence = _SKIP_LINE.sub("", summary + "\n" + (named_text or ""))
    named = {p for p in set(now) | set(reviewed) | set(changed) if _named(p, evidence)}
    named_stems = {_stem(p) for p in named if not is_test(p)}
    outside = [p for p in changed if p not in named
               and not (is_test(p) and _tests_a_named_file(p, named_stems))]
    if outside:
        out.reason = "touches_unnamed_files:" + ",".join(outside[:5])
        return out
    out.mode = SCOPED
    out.reason = "small_repair_of_named_files"
    return out


def render(p: ScopePlan, *, max_total: int = review_input.MAX_TOTAL,
           max_per_file: int = review_input.MAX_PER_FILE) -> str:
    """The change block a SCOPED seat judges. Wrapped by the seat's own prompt exactly as the
    full change block is, so every seat's rules (FINDING format, REVIEWED line) still apply."""
    head = ("REPAIR RE-REVIEW. This is a repair attempt of a change you (this seat) already "
            f"reviewed in full (run {p.baseline_run}, round {p.baseline_round}). Your findings "
            "from that review are below. Since that review the repair changed ONLY the files "
            "listed after them. (1) For each blocking finding, confirm in the code that it is "
            "resolved; raise it again, in the same FINDING format, if it is not. (2) Check these "
            "changes for a NEW blocking defect, INCLUDING regressions the repair introduced in "
            "code that calls, or is called by, the changed lines — open any file in the "
            "read-only tree for that context. Do not re-review unchanged code: it was judged "
            "already.\n\n"
            "YOUR PREVIOUS FINDINGS:\n" + (p.prior_summary or "(none recorded)")[:6000] + "\n\n")
    if not p.changed:
        return head + ("WHAT THE REPAIR CHANGED: nothing since your review — the code you "
                       "reviewed is the code under review. Re-check each finding against it.")
    listed = ["WHAT THE REPAIR CHANGED (" + str(len(p.changed)) + " file(s), "
              + str(p.changed_lines) + " changed line(s), against the version you reviewed):"]
    listed += [f"  - {path}" + ("  (removed)" if p.changed[path] is None else
                               "  (diff below)" if path in p.diffs else
                               "  (whole file below: reviewed text not held)")
               for path in sorted(p.changed)]
    parts, total = ["\n".join(listed)], 0
    for path in sorted(p.changed):
        if path in p.diffs:
            body, tag = p.diffs[path], "DIFF"
        elif p.changed[path] is not None and not review_input.is_lockfile(path):
            body, tag = p.changed[path] or "", "FILE"
        else:
            continue
        if len(body) > max_per_file:
            body = body[:max_per_file] + f"\n… [truncated {len(body) - max_per_file} chars]"
        block = f"\n=== {tag}: {path} ===\n{body}\n"
        if total + len(block) > max_total:
            parts.append(f"\n… [further text omitted to fit context, starting at {path}; open "
                         "the rest in the tree]")
            break
        parts.append(block)
        total += len(block)
    return head + "".join(parts).strip()
