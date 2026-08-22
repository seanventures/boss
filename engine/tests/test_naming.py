"""A title has one job: let a person tell one piece of work from another.

Titles were built by truncating whatever prose started the brief —
``("Web app — " + directive[:70])`` — so a card read

    "Web app — # Product Requirements Document (PRD)   **Project:** Lightweight Self-"

for a project the PRD names on its very next line: "Shorty". Four different benchmark apps all
displayed as "QA benchmark", because the label came first and the name was cut off.

Every case here is a real title or directive taken from the live system.
"""

import pytest

from boss_engine.services.naming import project_name

PRD = ('# Product Requirements Document (PRD)  \n'
       '**Project:** Lightweight Self‑Hosted URL Shortener – “Shorty”'
       ' (working title)  \n**Scope:** first slice')


def test_it_reads_the_name_the_document_gives_itself():
    name, blurb, _ = project_name(PRD)
    assert name == "Shorty"
    assert "URL Shortener" in blurb


def test_the_old_title_was_unreadable_and_the_new_one_is_short():
    old = ("Web app — " + PRD[:70]).strip()
    name, _, _ = project_name(PRD)
    assert "Product Requirements Document" in old  # what an operator used to see
    assert len(name) < 20


@pytest.mark.parametrize("text", [
    "Web app — # Product Requirements Document (PRD)   **Project:** Something Real",
    "Web app — # PRD: BugBox — Single-Project Issue Tracker **Status:** Ready",
    "# Design\n**Project:** Real Name Here",
])
def test_document_boilerplate_is_never_the_name(text):
    name, _, _ = project_name(text)
    assert not name.lower().startswith(("product requirements", "prd", "web app", "design"))


def test_the_run_kind_becomes_a_label_not_the_name():
    name, blurb, label = project_name(
        "QA benchmark: HookWatch — Webhook inspector and replay tool · retry 1")
    assert name == "HookWatch"
    assert label == "benchmark"
    assert "Webhook inspector" in blurb


def test_three_benchmarks_do_not_collapse_into_one_name():
    names = {project_name(f"QA benchmark: {n} — something")[0]
             for n in ("HookWatch", "ApproveIt", "BugBox")}
    assert len(names) == 3


def test_a_quoted_product_name_wins_over_its_description():
    name, _, _ = project_name(
        "Web app — Build 'Living Colors' — a VIBRANT, COLORFUL portfolio · retry 1")
    assert name == "Living Colors"


def test_retry_suffix_is_not_part_of_a_name():
    name, _, _ = project_name("Build a simple todo list web app · retry 2")
    assert "retry" not in name.lower()
    assert name == "Simple Todo List"


def test_nested_labels_are_all_stripped():
    name, _, label = project_name(
        "Follow-up: deferred review findings — e2e smoke — /healthz endpoint")
    assert label == "follow-up"
    assert name.lower() != "follow-up"


@pytest.mark.parametrize("junk", ["", "   ", "· retry 3", "Web app — ", "###"])
def test_a_name_is_always_produced(junk):
    name, _, _ = project_name(junk)
    assert name and name.strip()
