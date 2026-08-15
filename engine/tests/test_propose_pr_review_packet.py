"""PR open must not dump full diffs into the review-packet LLM call.

Live official BugBox/HookWatch runs died as
``PR not created after 3 attempts: blocked`` because propose_pull_request
sent every file body to gpt-4o (context_length_exceeded) and then released
the reservation so retries could only report ``Missing active reservation``.
The PR is the deliverable; the review packet is optional.
"""
from __future__ import annotations

import json

from boss_engine.schemas import FileChange
from boss_engine.services.software_factory import (
    fallback_review_packet,
    review_packet_file_summaries,
    review_packet_prompt,
)


def _huge_files(n: int = 60, body_size: int = 20_000) -> list[FileChange]:
    return [
        FileChange(path=f"app/file_{i:02d}.py", content=("UNIQUEBODY" + "x" * body_size))
        for i in range(n)
    ]


def test_review_packet_summaries_omit_full_file_bodies():
    files = _huge_files()
    summaries = review_packet_file_summaries(files)
    blob = json.dumps(summaries)
    assert "UNIQUEBODY" + "x" * 20_000 not in blob
    assert len(blob) < 20_000
    assert summaries[0]["path"] == "app/file_00.py"
    assert summaries[0]["bytes"] > 20_000
    assert "UNIQUEBODY" in summaries[0]["preview"]
    omitted = next(item for item in summaries if "omitted_files" in item)
    assert omitted["omitted_files"] == 20


def test_review_packet_prompt_stays_bounded():
    prompt = review_packet_prompt(
        title="QA benchmark: BugBox",
        summary="Governed pipeline survived.",
        files=_huge_files(),
    )
    assert "UNIQUEBODY" + "x" * 20_000 not in prompt
    assert len(prompt) < 16_000
    assert "QA benchmark: BugBox" in prompt


def test_fallback_review_packet_does_not_invent_scores():
    text = fallback_review_packet(
        title="QA benchmark: HookWatch",
        summary="QualityGate qg_1 PASS (advisory).",
        files=_huge_files(3, 100),
        error="context_length_exceeded",
    )
    assert "HookWatch" in text
    assert "context_length_exceeded" in text
    assert "a11y_violations=0" not in text
    assert "invented" not in text.lower()
    lowered = text.lower()
    assert "5/5" not in lowered
    assert "jtbd pass" not in lowered