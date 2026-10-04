"""A Stop pressed before the PR exists wins (factory walk D1).

The PR step is one long transaction with no phase change inside it, so a Stop recorded while
it ran was first seen after the PR was open. Every place a build opens its PR now checks for a
pending stop right before the first GitHub write. (A stop that arrives after the PR exists reads
`Too late · it had finished`: apps/api/tests/test_build_actions_have_http_routes.py.)
"""
from __future__ import annotations

import re
from pathlib import Path

SERVICES = Path(__file__).resolve().parents[1] / "boss_engine" / "services"


def _calls_with_preamble(source: str, call: str, lines_before: int = 8) -> list[str]:
    out = []
    lines = source.splitlines()
    for i, line in enumerate(lines):
        if call in line:
            out.append("\n".join(lines[max(0, i - lines_before):i + 1]))
    return out


def test_the_governed_build_checks_for_a_stop_before_each_pr_write():
    source = (SERVICES / "governed_code_task_service.py").read_text()
    calls = _calls_with_preamble(source, "sf.propose_pull_request(")
    # The partial-PR rescue runs only for an executor that is ending anyway; it is not a
    # decision to ship. Every other PR write is preceded by the stop check.
    shipping = [c for c in calls if "[partial]" not in source[source.find(c):source.find(c) + 2000]
                .split("summary=")[0]]
    assert len(shipping) >= 2
    for preamble in shipping:
        assert re.search(r"_abort_if_cancelled\(\"before (opening|retrying) the PR\"\)", preamble), \
            preamble


def test_the_simple_build_honours_a_stop_before_its_pr():
    source = (SERVICES / "code_task_service.py").read_text()
    run = source[source.index("def run_code_task("):]
    preamble = _calls_with_preamble(run, "sf.propose_pull_request(", 10)[0]
    assert "pending_stop(c, run_id)" in preamble and "honour_stop_request(c, run_id)" in preamble
