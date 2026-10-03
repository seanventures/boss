"""A refused reservation is read from what the governor SAYS, never from its status.

`reserve()` returns status 'denied' for every refusal; what it means — whether it clears by
itself (`transient`, `retry_after_seconds`) and the sentence for it (`message`) — is in the
other fields. Callers that formatted the status produced "Budget reservation denied: denied",
and the code-task paths blocked that as needing a person even when it cleared within a minute
(review of f59c9676). This fails if any source formats a reservation's status into text again.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCES = [ROOT / "engine" / "boss_engine", ROOT / "apps" / "api" / "app",
           ROOT / "apps" / "worker" / "boss_worker"]
# `{reservation.get('status')}`, `{res["status"]}`, `{renewed.get("status") or ...}` inside an
# f-string — the status of a reservation formatted into a sentence.
FORMATTED_STATUS = re.compile(
    r"\{\s*(?:res|reserv\w*|renew\w*)\s*(?:\.get\(\s*|\[\s*)['\"]status['\"]")


def test_no_source_formats_a_reservations_status_into_a_sentence():
    offenders = []
    for base in SOURCES:
        for path in base.rglob("*.py"):
            if "tests" in path.parts:
                continue
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if FORMATTED_STATUS.search(line):
                    offenders.append(f"{path.relative_to(ROOT)}:{number}: {line.strip()}")
    assert not offenders, (
        "a budget refusal is read from reserve()'s `message` and `transient`, not its status:\n"
        + "\n".join(offenders))


def test_the_pattern_catches_the_shape_it_exists_for():
    assert FORMATTED_STATUS.search('f"Budget reservation denied: {reservation.get(\'status\')}"')
    assert FORMATTED_STATUS.search('f"reserve denied: {res.get(\'status\')}"')
    assert not FORMATTED_STATUS.search('f"{result.get(\'status\')}"')
