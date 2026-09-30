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


# --------------------------------------------------------------------------- #
# App identity: stamped at creation, never inferred from lineage afterwards
# --------------------------------------------------------------------------- #
def test_the_same_app_in_one_repo_has_one_identity():
    from boss_engine.services.naming import stable_app_id
    base = stable_app_id("boss-dogfood", "boss-sandbox", "Shorty — self-hosted URL shortener")
    for variant in ("Shorty — self-hosted URL shortener · retry 2",
                    "Web app — Shorty — self-hosted URL shortener",
                    "  Shorty — self-hosted URL shortener  "):
        assert stable_app_id("boss-dogfood", "boss-sandbox", variant) == base, variant


def test_different_apps_do_not_collide():
    from boss_engine.services.naming import stable_app_id
    a = stable_app_id("p", "r", "Shorty — self-hosted URL shortener")
    b = stable_app_id("p", "r", "Ascend — coaching for new managers")
    assert a != b


def test_the_same_name_in_a_different_repo_is_a_different_app():
    from boss_engine.services.naming import stable_app_id
    assert stable_app_id("p", "repo-a", "Shorty") != stable_app_id("p", "repo-b", "Shorty")


def test_an_unnamed_run_gets_no_identity_rather_than_a_shared_one():
    """Stamping "Untitled app" would herd every unnamed run onto a single card — worse than
    the fragmentation it was meant to fix."""
    from boss_engine.services.naming import stable_app_id
    for junk in ("PRD", "Design", "", "   "):
        assert stable_app_id("p", "r", junk) == ""


def test_start_code_task_stamps_it_not_only_the_retry_paths():
    """The whole defect: app_id existed and was written on retry ONLY, so five runs of one app
    made four cards and a building app rendered as BLOCKED. A run started fresh is precisely
    the case lineage cannot cover — it is a root, not a child."""
    import inspect
    from boss_engine.services.code_task_service import start_code_task
    src = inspect.getsource(start_code_task)
    assert "stable_app_id" in src
    assert 'fctx.get("app_id")' in src, "must not overwrite an identity the caller already set"


def test_the_console_groups_on_the_same_definition_the_api_blocks_on():
    """active_same_app already trusts project_name to REFUSE to start a duplicate. A definition
    good enough to block work is good enough to group a card — and using two different ones is
    how the board and the guard disagreed."""
    import inspect
    from boss_engine.services.code_task_service import active_same_app
    from boss_engine.services.naming import stable_app_id
    assert "project_name" in inspect.getsource(active_same_app)
    assert "project_name" in inspect.getsource(stable_app_id)
