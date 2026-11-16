"""THE BRIEF RECORDS THE STACK EACH COMPONENT ASKS FOR (BOSSAPP-1207).

Northgate (2026-11-15) asked for a Go gateway and a Go gwlint. The brief Helm confirmed had no
structured required stack, so later steps could only re-scan the words — and the spec's
deploy-machine sentence and allowed-command inventory were read as Ruby and PHP. shape_brief and
the picks keep an editable required_stack, one entry per component, from the words. The same
backstop that reads "(Go)", "(Go, …)", "idiomatic Go", go vet|test|build|run|mod and go 1.x
fills it when the model leaves it out. "go to" and "go live" are not Go. A line the person
pinned replaces the reading.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from boss_engine.services.project_experience import add_project

NORTHGATE = Path(__file__).resolve().parent / "fixtures" / "northgate_go_brief.txt"


def _languages(entries: list[dict]) -> set[str]:
    return {str(e["language"]) for e in entries}


def _by_component(entries: list[dict]) -> dict[str, str]:
    return {str(e["component"]): str(e["language"]) for e in entries}


def test_the_northgate_brief_records_go_per_component_and_not_ruby_or_php():
    words = NORTHGATE.read_text()
    brief = add_project.shape_brief(
        {"title": "Northgate API Gateway + gwlint",
         "must_haves": ["northgate-gateway: reverse-proxy API gateway (Go)",
                        "gwlint CLI"],
         "done_when": "go vet and go test ./... are clean"},
        words=words)
    picks = add_project._picks(  # noqa: SLF001 — the picks are the other half of the record
        {}, catalogue=(), stacks=(), workflows=(), words=words)
    for record in (brief["required_stack"], picks["required_stack"]):
        got = _by_component(record)
        assert got["northgate-gateway"] == "go"
        assert got["gwlint"] == "go"
        assert _languages(record) == {"go"}


@pytest.mark.parametrize("words", [
    "API gateway (Go)",
    "### 1. `gateway` (Go, medium complexity)",
    "Clean, idiomatic Go",
    "`go vet` must be clean",
    "`go test ./...` must pass",
    "go build ./cmd/gateway must pass",
    "go run ./cmd/gateway must pass",
    "go mod tidy is required",
    "The module requires go 1.22",
])
def test_the_backstop_reads_the_phrasings_a_brief_uses_for_go(words):
    brief = add_project.shape_brief({"title": "Gateway", "must_haves": [words]}, words=words)
    assert _languages(brief["required_stack"]) == {"go"}


@pytest.mark.parametrize("words", [
    "Users log in, go to the dashboard and go back",
    "We go live on Monday",
    "the go-to-market plan",
])
def test_go_to_and_go_live_are_not_a_required_stack(words):
    brief = add_project.shape_brief({"title": "Launch", "must_haves": [words]}, words=words)
    assert _languages(brief["required_stack"]) == set()


def test_a_pinned_required_stack_replaces_the_reading():
    words = "northgate-gateway: reverse-proxy API gateway (Go)"
    pinned = {"required_stack": [{"component": "api", "language": "node"}]}
    brief = add_project.shape_brief(
        {"title": "Gateway", "must_haves": [words],
         "required_stack": [{"component": "northgate-gateway", "language": "go"}]},
        words=words, pinned=pinned)
    picks = add_project._picks(  # noqa: SLF001
        {"required_stack": [{"component": "northgate-gateway", "language": "go"}]},
        catalogue=(), stacks=(), workflows=(), words=words, pinned=pinned)
    assert brief["required_stack"] == [{"component": "api", "language": "node"}]
    assert brief["pinned"] == ["required_stack"]
    assert picks["required_stack"] == [{"component": "api", "language": "node"}]


def test_a_model_list_keeps_its_entries_and_the_backstop_fills_a_missed_component():
    brief = add_project.shape_brief(
        {"title": "Gateway",
         "must_haves": ["northgate-gateway: reverse-proxy API gateway (Go)"],
         "required_stack": [{"component": "web", "language": "node"}]},
        words="gwlint CLI (Go, same repo)")
    got = _by_component(brief["required_stack"])
    assert got["web"] == "node"
    assert got["northgate-gateway"] == "go"
    assert got["gwlint"] == "go"
