"""THE ONE MAPPING FROM BOSS'S OWN WORDS TO A NEWCOMER'S (e2e recipe-box walk, 2026-11-18).

Internal words leaked onto the build page, the merge card and the work list: a title ending
"· retry 2", a headline quoting the raw repair prompt ("Autonomous review-repair: the quality
gate blocked on code_review_passed, acceptance_met…"), a skipped check reading "not convened:
profile 'lean' does not include this seat", gate keys in brackets "(code_review_passed)".

Everything a surface shows passes through here once; the records keep their own words (a test, a
retry's fingerprint, the shepherd all read those) and the keys stay in a details fold only.
`leaks` is what the guard tests use: a string a surface renders must return [].
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Optional

#: "· retry 2" describes the attempt, not the work (code_task_service._RETRY_SUFFIX).
_RETRY_TAIL = re.compile(r"\s*·\s*retry\s*\d+\b", re.I)
#: The prefix the review-repair loop writes on its own retry note (work_provenance).
_REPAIR_PREFIX = "Autonomous review-repair"
_REPAIR_ROUND = re.compile(r"^\s*Auto review-repair round (\d+)", re.I)
#: seat_policy.skip_reason's record sentence.
_NOT_CONVENED = re.compile(r"not convened:\s*profile\s*'([^']*)'\s*does not include this seat", re.I)


def gate_words(key: str) -> str:
    """The label of a gate key (factory_view.gate_label, the one table)."""
    from .factory_view import gate_label

    return gate_label(key)


def _gate_keys() -> tuple[str, ...]:
    from .factory_view import GATE_LABELS

    # Longest first, so a key that contains another is replaced whole.
    return tuple(sorted(GATE_LABELS, key=len, reverse=True))


#: A FAILED GATE SAID AS WHAT WENT WRONG. "Review passed ✗" beside "Review: Changes asked" read
#: as a contradiction; a failed gate names the failure. THE SAME TABLE as apps/web/lib/plain-words.ts.
FAILED_WORDS: dict[str, str] = {
    "code_written": "No code written",
    "written_tests_pass": "New tests fail",
    "existing_tests_intact": "Old tests broke",
    "tests_written_first": "Tests not written first",
    "coder_did_not_edit_tests": "Coder edited the tests",
    "brief_tests_written": "Named tests missing",
    "tests_strong_enough": "Tests too weak",
    "code_review_passed": "Review asked for changes",
    "security_clean": "Security findings open",
    "consistent": "Not consistent with the code",
    "acceptance_met": "Must-haves not all met",
    "input_fidelity": "Not derived from input",
    "nothing_broke": "Something broke",
    "app_serves": "App does not start",
    "web_package_honest": "Package not honest",
    "template_tests": "Template tests fail",
    "no_committed_secrets": "A secret was committed",
}


def gate_failed_words(key: str) -> str:
    """A failed gate as what went wrong ("Review asked for changes"), never its pass label."""
    k = str(key or "")
    return FAILED_WORDS.get(k) or f"{gate_words(k)}: failed"


def plain_title(title: Any) -> str:
    """A build's name without the attempt counter: "Recipe box · retry 2" → "Recipe box"."""
    return _RETRY_TAIL.sub("", str(title or "")).strip()


def gates_in(text: Any) -> list[str]:
    """The gate keys a sentence names, in order (for "BOSS is fixing <labels>")."""
    s = str(text or "")
    found: list[tuple[int, str]] = []
    for key in (k for k in _gate_keys() if "_" in k):
        for m in re.finditer(rf"(?<![A-Za-z0-9_]){re.escape(key)}(?![A-Za-z0-9_])", s):
            if not any(a <= m.start() < a + len(k) for a, k in found):
                found.append((m.start(), key))
    return [k for _, k in sorted(found)]


def repair_words(gates: Iterable[str]) -> str:
    labels = [gate_failed_words(g) for g in gates]
    if not labels:
        return "BOSS is fixing what its checks found"
    return "BOSS is fixing what its checks found: " + ", ".join(labels)


def plain_note(note: Any) -> Optional[str]:
    """A retry note as a person reads it. BOSS's own repair prompt becomes one plain sentence;
    a person's note is kept as they wrote it."""
    text = str(note or "").strip()
    if not text:
        return None
    if text.startswith(_REPAIR_PREFIX) or _REPAIR_ROUND.match(text):
        # Only the lead names the failing gates; the rest is instructions for the coder.
        lead = text.split(". ", 1)[0]
        return repair_words(gates_in(lead))
    return text


def plain_skip(reason: Any) -> str:
    """Why a check did not run, in words: the profile is the project's quality level."""
    text = str(reason or "").strip()
    m = _NOT_CONVENED.search(text)
    if m:
        level = (m.group(1) or "").strip()
        if not level or level.lower() == "none":
            return "Not part of this project's quality level"
        return f"Not part of the {level.capitalize()} quality level"
    return text


def plain_sentence(text: Any) -> str:
    """Any sentence a surface shows: its gate keys become labels, a "· retry N" goes, a repair
    prompt becomes one sentence, a not-convened seat reads as a skipped check."""
    s = str(text or "")
    if not s:
        return s
    note = plain_note(s)
    if note is not None and note != s.strip():
        return note
    s = _NOT_CONVENED.sub(lambda m: plain_skip(m.group(0)), s)
    s = _RETRY_TAIL.sub("", s)
    # A sentence about failing names the failure ("Review asked for changes"), never the pass
    # label ("failed its checks (Review passed)" read as a contradiction).
    words = gate_failed_words if re.search(r"fail|block", s, re.I) else gate_words
    for key in (k for k in _gate_keys() if "_" in k):   # "consistent" is also a plain word
        s = re.sub(rf"\s*\(\s*{re.escape(key)}\s*\)(?=\s*:)", "", s)
        s = re.sub(rf"(?<![A-Za-z0-9_]){re.escape(key)}(?![A-Za-z0-9_])", words(key), s)
    # "['Review passed', 'Must-haves met']" from a Python list printed into a sentence.
    s = re.sub(r"\[\s*'([^\]]*)'\s*\]", lambda m: m.group(1).replace("', '", ", "), s)
    return s


def leaks(text: Any) -> list[str]:
    """The internal words a rendered string still carries ([] when it is plain)."""
    s = str(text or "")
    found: list[str] = []
    if _RETRY_TAIL.search(s):
        found.append("retry N")
    if _REPAIR_PREFIX.lower() in s.lower() or _REPAIR_ROUND.search(s):
        found.append("repair prompt")
    if _NOT_CONVENED.search(s):
        found.append("not convened")
    for key in _gate_keys():
        if "_" in key and re.search(rf"(?<![A-Za-z0-9_]){re.escape(key)}(?![A-Za-z0-9_])", s):
            found.append(key)
    return found
