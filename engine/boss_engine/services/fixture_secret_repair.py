"""Repairing a secret-looking literal the TEST AUTHOR committed (the no_committed_secrets gate).

THE DEAD END THIS CLOSES. In a governed build the independent QA stage writes the tests and the
implementer may not edit them (gate coder_did_not_edit_tests). The template gate
no_committed_secrets scans tests too. So when QA typed a password into a fixture, every repair
round handed the finding to the one agent forbidden to touch it, and QA was never asked again:
crun_b4613dcf (8 findings, all in tests) and crun_ce7e8841 (auth.test.ts:38) both died there.

THE GATE IS NOT SOFTENED. The scan, its rules and its verdict are untouched; this only gives the
finding to the role that owns the file. Findings are split by owner: a test file the QA stage
authored goes to a narrow QA "fixture repair" turn; everything else goes to the coder as before.

THE TURN IS NARROW, AND THAT IS ENFORCED, NOT ASKED FOR. The test author is told to change only
the flagged lines, replacing each literal with a value generated at run time or read from the
test harness's environment. Afterwards BOSS diffs the tree: a change to any line that was not
flagged (other than an added import), to any other file, or a removed line, and the whole turn is
reverted. If the suite passed before the turn and fails after it, the turn is reverted too. The
gates then re-run on whatever stands.
"""
from __future__ import annotations

import difflib
import hashlib
import re
from pathlib import Path
from typing import Callable, Iterable, Mapping, Optional

from . import owned_process

#: An added line the turn may introduce beside the flagged ones: an import the generated value
#: needs (`import secrets`, `import { randomUUID } from "node:crypto"`, `const x = require(...)`).
_IMPORT_LINE = re.compile(
    r"^\s*(?:import\s|from\s+[\w.]+\s+import\s|(?:const|let|var)\s+[\w{}\s,]+=\s*require\(|"
    r"using\s|require\s)")


def split_secret_findings(findings: Iterable[Mapping], *,
                          qa_owns: Callable[[str], bool]) -> tuple[list[dict], list[dict]]:
    """(findings in files the QA stage authored, findings for the coder)."""
    qa, coder = [], []
    for f in findings or ():
        if not isinstance(f, Mapping) or not f.get("path"):
            continue
        (qa if qa_owns(str(f["path"])) else coder).append(dict(f))
    return qa, coder


def parent_failed_on_test_secrets(fctx: Mapping) -> bool:
    """Did the attempt this run retries fail the secret gate? (Recorded on the evidence so a
    retry of such a build says it came to repair QA's fixture, not to rewrite the code.)"""
    gates = [str(g) for g in (fctx.get("prior_failing_gates") or [])]
    return "no_committed_secrets" in gates


def flagged_lines(findings: Iterable[Mapping]) -> dict[str, set[int]]:
    out: dict[str, set[int]] = {}
    for f in findings:
        try:
            out.setdefault(str(f["path"]), set()).add(int(f["line"]))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def fixture_repair_prompt(findings: Iterable[Mapping], test_cmd: str = "") -> str:
    """The QA turn's whole instruction. Names file, line and rule, never the matched text."""
    listing = "\n".join(f"  - {f['path']}:{f['line']}  (rule: {f.get('rule')})" for f in findings)
    return (
        "You are the TEST AUTHOR (QA). You wrote the tests in this tree. BOSS's secret scan "
        "(no_committed_secrets) found a credential-shaped literal on these lines of YOUR test "
        "files:\n" + listing + "\n\n"
        "FIXTURE REPAIR, AND NOTHING ELSE:\n"
        "- Change ONLY the lines listed above. Do not touch any other line or file.\n"
        "- Replace each literal with a value generated at run time (crypto.randomUUID(), "
        "secrets.token_urlsafe(16)) or read from an environment variable the test harness sets "
        "for itself with a generated fallback (process.env.TEST_PASSWORD ?? crypto.randomUUID()).\n"
        "- If the same value must match elsewhere in the test, keep the matching on that line "
        "(e.g. assign it to the same variable name) so every assertion still means what it "
        "meant. You may add ONE import line if the generator needs it; nothing else.\n"
        "- The tests must stay semantically identical: same cases, same assertions, same "
        "expected outcomes. Do not rename, delete, skip or weaken anything.\n"
        "- Do not write any secret-looking literal, placeholder text included.\n"
        + (f"- Run `{test_cmd}` afterwards; it must pass exactly as before.\n" if test_cmd else "")
        + "BOSS checks the diff: any change outside the listed lines reverts your whole turn."
    )


def check_line_edits(before: str, after: str, allowed: set[int]) -> list[str]:
    """What the edit did beyond replacing the allowed lines (1-based). Empty means within bounds."""
    a, b = before.splitlines(), after.splitlines()
    problems: list[str] = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op == "equal":
            continue
        if op == "insert":
            extra = [ln for ln in b[j1:j2] if not _IMPORT_LINE.match(ln)]
            if extra:
                problems.append(f"added {len(extra)} line(s) near line {i1 + 1}")
            continue
        touched = set(range(i1 + 1, i2 + 1))
        if op == "delete":
            problems.append(f"removed line(s) {i1 + 1}-{i2}")
        elif not touched <= allowed:
            problems.append(f"changed unflagged line(s) {sorted(touched - allowed)[:5]}")
        elif (j2 - j1) != (i2 - i1):
            extra = (j2 - j1) - (i2 - i1)
            inserted = b[j1:j2]
            imports = sum(1 for ln in inserted if _IMPORT_LINE.match(ln))
            if extra < 0 or imports < extra:
                problems.append(f"replaced lines {i1 + 1}-{i2} with {j2 - j1} line(s)")
    return problems


def _git(root: Path, *args: str) -> str:
    # Owned (owned_process): its own group, ended with the whole tree on timeout or cancel.
    return owned_process.run(["git", *args], cwd=str(root), capture_output=True, text=True,
                             timeout=60, check=False).stdout


def _dirty(root: Path) -> dict[str, Optional[bytes]]:
    """{path: content or None if deleted} for every path git sees as changed or untracked."""
    out: dict[str, Optional[bytes]] = {}
    for raw in _git(root, "status", "--porcelain=v1", "-uall", "--no-renames", "-z").split("\0"):
        if len(raw) < 4:
            continue
        p = raw[3:]
        fp = root / p
        out[p] = fp.read_bytes() if fp.is_file() else None
    return out


def _digest(v: Optional[bytes]) -> str:
    return "-" if v is None else hashlib.sha256(v).hexdigest()


def repair_fixture_secrets(*, root: Path, findings: list[dict],
                           run_qa: Callable[[str], Mapping],
                           rerun_tests: Optional[Callable[[], bool]] = None,
                           tests_passed_before: bool = False,
                           test_cmd: str = "",
                           rescan: Optional[Callable[[dict[str, str]], list]] = None) -> dict:
    """Run one QA fixture-repair turn over `findings` (all in QA-authored test files) in `root`.

    `run_qa(prompt)` invokes the test author's role in `root` and returns its result dict.
    Returns the evidence record. The caller re-runs the gates afterwards."""
    root = Path(root)
    allowed = flagged_lines(findings)
    record: dict = {"attempted": True, "role": "write_tests",
                    "findings": [{k: f.get(k) for k in ("path", "line", "rule")} for f in findings],
                    "files": sorted(allowed)}
    before_dirty = _dirty(root)
    before_text = {p: (root / p).read_text(errors="replace") for p in allowed if (root / p).is_file()}

    try:
        res = run_qa(fixture_repair_prompt(findings, test_cmd)) or {}
    except Exception as exc:  # noqa: BLE001 — a dead turn leaves the gate failed, never passes it
        res = {"error": f"{type(exc).__name__}: {exc}"[:300]}
    record["cost_usd_micros"] = int(res.get("cost_usd_micros") or 0)
    if res.get("error"):
        record["agent_error"] = str(res["error"])[:300]

    after_dirty = _dirty(root)
    changed = sorted(p for p in set(before_dirty) | set(after_dirty)
                     if _digest(before_dirty.get(p, _HEAD)) != _digest(after_dirty.get(p, _HEAD)))
    problems = [f"{p}: not a flagged file" for p in changed if p not in allowed]
    for p, old in before_text.items():
        fp = root / p
        new = fp.read_text(errors="replace") if fp.is_file() else ""
        problems += [f"{p}: {x}" for x in check_line_edits(old, new, allowed[p])]
    record["changed_files"] = changed

    if not problems and rerun_tests is not None and tests_passed_before:
        if not rerun_tests():
            problems.append("the suite passed before the fixture repair and fails after it")

    if problems:
        _revert(root, before_dirty, after_dirty)
        record.update(repaired=False, reverted=True, reasons=problems[:10])
        return record

    record["reverted"] = False
    if rescan is not None:
        remaining = rescan({p: (root / p).read_text(errors="replace")
                            for p in allowed if (root / p).is_file()})
        record["remaining"] = [x.as_dict() if hasattr(x, "as_dict") else x for x in remaining][:20]
        record["repaired"] = not remaining
    else:
        record["repaired"] = bool(changed)
    return record


#: Sentinel: "this path was clean before" (its content is HEAD's).
_HEAD = b"\0<HEAD>"


def _revert(root: Path, before: dict, after: dict) -> None:
    """Put every path the turn touched back exactly as it was before the turn."""
    for p in set(before) | set(after):
        old = before.get(p, _HEAD)
        if _digest(old) == _digest(after.get(p, _HEAD)):
            continue
        fp = root / p
        if old is _HEAD:
            if _git(root, "ls-files", "--", p).strip():
                owned_process.run(["git", "checkout", "--", p], cwd=str(root), capture_output=True,
                                  timeout=60, check=False)
            else:
                fp.unlink(missing_ok=True)
        elif old is None:
            fp.unlink(missing_ok=True)
        else:
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_bytes(old)
