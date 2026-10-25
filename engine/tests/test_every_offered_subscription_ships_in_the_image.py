"""Every subscription Add a model offers can actually be connected on this image (audit P1-8).

Cursor was offered in the console drawer, the setup catalogue and MODEL_CONNECTIONS.md, but
`cursor-agent` is not in Dockerfile.community and its harness spec is self-declared unverified,
so the tile could only ever read "Not on server" with Connect disabled. A subscription is now
offered only when its CLI ships in the runtime stage and BOSS has a harness spec for it; a new
tile without both fails here, naming what is missing.
"""
from __future__ import annotations

from boss_engine.services import setup_service, setup_view
from boss_engine.services.cli_harness import BUILTIN_CLI_HARNESSES

from .test_every_engine_binary_is_in_the_image import _runtime_stage, installed_by_image


def _offered() -> list[str]:
    return [e["provider"] for e in setup_view.CATALOGUE if e["kind"] == "subscription"]


def test_every_offered_subscription_has_a_harness_and_its_cli_in_the_image():
    have = installed_by_image(_runtime_stage())
    problems = []
    for provider in _offered():
        spec = BUILTIN_CLI_HARNESSES.get(provider)
        if spec is None:
            problems.append(f"{provider}: no harness spec in cli_harness.BUILTIN_CLI_HARNESSES")
            continue
        cli = [need for need in spec.runtime if need.binary == spec.binary]
        if not cli:
            problems.append(f"{provider}: the harness declares no runtime for `{spec.binary}`")
        for need in cli:
            if need.via == "operator" or need.package not in have.get(need.via, set()):
                problems.append(f"{provider}: `{need.binary}` ({need.via} {need.package!r}) is "
                                "not installed by Dockerfile.community's runtime stage")
    assert not problems, "\n".join(problems)


def test_cursor_is_not_offered_until_it_ships():
    assert "cursor" not in _offered()
    assert "cursor" not in setup_view.OFFERED_SUBSCRIPTIONS
    assert "cursor" not in setup_service.PROVIDERS
    # The harness itself stays (bring-your-own CLI), it is just not offered.
    assert "cursor" in BUILTIN_CLI_HARNESSES


def test_detection_lists_only_offered_subscriptions(monkeypatch):
    monkeypatch.setattr(setup_view, "_cli_detection", lambda _s: {})
    monkeypatch.setattr(setup_view, "local_found", lambda _p: None)
    subscriptions = [d["provider"] for d in setup_view._detected(object())
                     if d["kind"] == "subscription"]
    assert sorted(subscriptions) == sorted(setup_view.OFFERED_SUBSCRIPTIONS)


def test_the_docs_do_not_offer_cursor():
    from pathlib import Path

    doc = (Path(__file__).resolve().parents[2] / "docs/community/MODEL_CONNECTIONS.md").read_text()
    assert "Grok Build, Cursor" not in doc
    assert "Cursor is not offered" in doc
