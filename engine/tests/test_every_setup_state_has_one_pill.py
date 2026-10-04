"""Every state Repo and models can show has exactly one way of being said.

The page had five verdicts for one question and they disagreed. Now the server says every state
through one catalogue (setup_pills), and this is the class-level guard: a state the checks can
produce with no pill, a label longer than three words or a tip longer than fourteen fails here
before it reaches anyone's screen.
"""
from __future__ import annotations

import ast
import inspect
import re

import pytest

from boss_engine.services import setup_actions, setup_checks, setup_pills, setup_view

BANNED = ("environment variable", " env ", "harness", "entitlement", "binding", "governed",
          "verification state", "lane", "inherited", "deployment default")


def _words(text: str) -> int:
    filled = re.sub(r"\{[a-z_]+\}", "x", text)
    return len([w for w in re.split(r"\s+", filled.replace("·", " ").strip()) if w])


@pytest.mark.parametrize("kind", sorted(setup_pills.CATALOGUES))
def test_every_label_is_three_words_and_every_tip_fourteen(kind):
    for state, entry in setup_pills.CATALOGUES[kind].items():
        assert entry["tone"] in setup_pills.TONES, (kind, state)
        assert _words(entry["label"]) <= 3, (kind, state, entry["label"])
        if not entry["tip"].startswith("{"):
            assert _words(entry["tip"]) <= 14, (kind, state, entry["tip"])
        assert entry.get("action") in (None, *setup_pills.ACTIONS), (kind, state)
        text = f" {entry['label']} {entry['tip']} ".lower()
        assert not any(word in text for word in BANNED), (kind, state)


def test_login_fixes_are_short_too():
    for fix in [*setup_pills.LOGIN_FIX.values(), setup_pills.DEFAULT_LOGIN_FIX]:
        assert _words(fix) <= 14, fix


def _states_named_in(module) -> set[str]:
    """Every string literal a check module hands back as a `state`."""
    found: set[str] = set()
    tree = ast.parse(inspect.getsource(module))
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and key.value == "state" \
                        and isinstance(value, ast.Constant) and isinstance(value.value, str):
                    found.add(value.value)
        if isinstance(node, ast.keyword) and node.arg == "refused" \
                and isinstance(node.value, ast.Constant):
            found.add(node.value.value)
        # `return "expired"` in the classifiers (repository_state, classify_model_failure).
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Constant) \
                and isinstance(node.value.value, str) and node.value.value.isidentifier():
            found.add(node.value.value)
        # `state = "rate_limited"` and friends.
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) \
                and any(isinstance(t, ast.Name) and t.id == "state" for t in node.targets) \
                and isinstance(node.value.value, str):
            found.add(node.value.value)
    return found


def test_every_state_the_checks_produce_has_a_pill():
    repository = set(setup_pills.REPOSITORY)
    model = set(setup_pills.MODEL)
    produced = _states_named_in(setup_checks) | _states_named_in(setup_actions)
    produced -= {"not_run", "working"}               # a check part, and the shared green
    unknown = {s for s in produced if s not in repository | model
               and s not in ("needs_consent", "in_use", "not_connected")}
    assert not unknown, f"states with no pill: {sorted(unknown)}"
    for state in ("token_rejected", "cant_write", "cant_open_prs", "no_access", "expired",
                  "expiring", "awaiting_org_approval", "rate_limited", "unreachable",
                  "checking", "not_connected", "working"):
        assert state in repository, state
    for state in ("working", "checking", "not_checked", "key_rejected", "login_expired",
                  "no_credit", "rate_limited", "model_not_found", "unreachable",
                  "paused_budget", "not_on_server", "key_unreadable"):
        assert state in model, state


def test_an_unknown_state_cannot_be_rendered():
    with pytest.raises(KeyError):
        setup_pills.pill("repository", "something_new")


def test_slots_are_filled_or_left_empty_never_shown_raw():
    shown = setup_pills.pill("repository", "rate_limited", time="14:20")
    assert shown["label"] == "GitHub busy · 14:20"
    blank = setup_pills.pill("model", "model_not_found")
    assert "{" not in blank["tip"]


def test_the_classifiers_only_say_what_the_catalogue_knows():
    for status, detail in ((0, ""), (401, ""), (402, ""), (404, ""), (429, ""), (500, "x"),
                           (400, "insufficient quota"), (400, "model_not_found")):
        assert setup_checks.classify_model_failure(status, detail) in setup_pills.MODEL
        assert setup_checks.classify_model_failure(status, detail, subscription=True) \
            in setup_pills.MODEL


def test_the_view_renders_only_through_the_catalogue():
    """The console never computes a tone: every pill the view hands out comes from `pill()`."""
    source = inspect.getsource(setup_view)
    assert '"tone": "' not in source.replace('"tone": entry["tone"]', "")


def test_a_time_in_a_pill_travels_as_an_instant_for_the_viewers_clock():
    shown = setup_pills.pill("repository", "rate_limited", time="14:20",
                             at="2026-10-04T14:20:00+00:00")
    assert shown["at"] == "2026-10-04T14:20:00+00:00"
    assert setup_pills.pill("repository", "rate_limited")["label"] == "GitHub busy"
    assert "at" not in setup_pills.pill("repository", "working", at="2026-10-04T14:20:00+00:00")


@pytest.mark.parametrize(("kind", "state"), [
    (kind, state) for kind, catalogue in setup_pills.CATALOGUES.items()
    for state, entry in catalogue.items() if "{time}" in entry["tip"]])
def test_a_tip_with_no_known_time_drops_the_phrase_not_the_time(kind, state):
    """"Auto uses another model until ." reached the page when a limit named no reset time."""
    tip = setup_pills.pill(kind, state)["tip"]
    assert not re.search(r"\b(until|at)\s*\.", tip), tip
    assert tip.endswith(".") and "{" not in tip


def test_a_subscription_limit_carries_the_time_the_cli_named(monkeypatch):
    from boss_engine.services import setup_actions, setup_service

    monkeypatch.setattr(setup_service, "governed_proof", lambda *a, **k: {
        "ok": False, "error": "Claude AI usage limit reached. Try again at 2026-10-04T16:20:00"})
    monkeypatch.setattr(setup_actions, "_engine", lambda c: None)
    proof = setup_actions._subscription_proof(None, None, "p", "claude_code", "ceo")
    assert proof["state"] == "rate_limited"
    assert proof["retry_at"].startswith("2026-10-04T16:20:00")


def test_a_model_row_carries_where_it_points_but_never_a_password_in_the_url():
    entry = setup_view._model_entry(
        source_id="setup-custom", provider="custom", kind="metered", model="m", state="working",
        checked_at=None, retry_at=None, source="connected", now=setup_checks.now_utc(),
        host="", latency_ms=None, base_url="https://user:pa55@llm.example:8443/v1")
    assert entry["base_url"] == "https://llm.example:8443/v1"
