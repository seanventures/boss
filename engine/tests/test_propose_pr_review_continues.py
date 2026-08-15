"""A review-packet model failure must not sink a finished official run's PR."""
from __future__ import annotations

from unittest.mock import MagicMock

from boss_engine.config import Settings
from boss_engine.schemas import FileChange
from boss_engine.services.software_factory import SoftwareFactoryService


class _ContextOverflow(Exception):
    pass


def test_review_packet_overflow_falls_back_and_keeps_reservation():
    settings = Settings(
        BOSS_TARGET_GITHUB_OWNER="paramg",
        BOSS_TARGET_GITHUB_REPO="boss-sandbox",
        BOSS_GITHUB_TOKEN="legacy-token",
    )
    sf = SoftwareFactoryService(settings, owner="paramg", repo="boss-sandbox")
    emitted: list[tuple] = []

    class Events:
        def emit(self, *args, **_k):
            emitted.append(args)

    class OpenAI:
        def create_text_response(self, *_a, **_k):
            raise _ContextOverflow(
                "Error code: 400 - context_length_exceeded Please reduce the length"
            )

    sf.events = Events()
    sf.openai = OpenAI()
    sf._merge_system_prompts = lambda *a, **k: "sys"
    sf._module_prompt = lambda *a, **k: ""

    files = [FileChange(path="app/main.py", content="print('hi')\n" * 5000)]
    text = sf._review_packet_markdown(
        MagicMock(),
        project_id="boss-dogfood",
        receipt_id="rcpt_038ecbe99167499dbdb9",
        title="QA benchmark: BugBox",
        summary="Governed pipeline survived.",
        files=files,
        module_spec={},
        model_id="gpt-4o",
    )
    assert "Review packet unavailable" in text
    assert "context_length_exceeded" in text
    assert "a11y_violations=0" not in text
    assert any(
        args[2] == "software_factory.review_packet_skipped" for args in emitted
    )
