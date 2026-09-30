"""What the v2 Projects screen calls things.

Every string here was read off the live console. The screen listed 40 "projects"; more than
half were BOSS's own benchmark harness, several apps appeared twice under different decorations
of the same name, and the titles that remained were directives rather than product names:

    [bakeoff:qwen_all#1] URL slug generator
    QA benchmark: BugBox — Mini issue tracker
    Web app — Build 'Living Colors' — a VIBRANT, COLORFUL...
    Shorty — self-hosted URL shortener · retry 2
    Shorty — Re-build PRD
    The deployment for run crun_a57b42adc0884dc4bfda failed...

The operator's complaint was precise: "PRD is not a title, it is a document... you even don't
know the difference of artifact name vs actual project title."
"""

import pytest

from boss_engine.services.console_v2 import _clean_title, _is_internal


# --------------------------------------------------------------------------- #
# What is not a product
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("title", [
    "[bakeoff:glm_all#1] Interval merge with containment queries",
    "[bakeoff:kimi_all#1] Markdown table-of-contents generator",
    "[bakeoff:qwen_all#1] CSV sales aggregation",
    "[bakeoff:deepseek_all#1] Token-bucket rate limiter",
    "[boss:coding e2e test] Add a greet() helper",
    "QA benchmark: HookWatch — Webhook inspector and replayer",
    "QA benchmark: BugBox — Mini issue tracker",
    "Captain: QUALITY IMPROVE PASS",
    "[boss-379] activity log",
])
def test_boss_testing_itself_is_not_a_project(title):
    """22 of the 40 rows were benchmark runs. A headline list dominated by internal chatter
    teaches the operator to ignore the list, which is worse than an empty one."""
    assert _is_internal(title), title


@pytest.mark.parametrize("title", [
    "The deployment for run crun_a57b42adc0884dc4bfda failed at the health gate",
    "What can you help me build, and how does the governed pipeline work?",
    "workspace test) what's the status of this task?",
    "This is the Discovery task for the project - Personalized coaching to ...",
])
def test_a_question_or_a_status_note_is_not_a_project(title):
    """These are things said TO BOSS, or notices from it. Listing them as products means the
    screen is showing a chat log with progress bars attached."""
    assert _is_internal(title), title


@pytest.mark.parametrize("title", [
    "Shorty — self-hosted URL shortener",
    "Living Colors",
    "BugBox — Mini issue tracker",
    "Invoice-Chaser",
    "Add multi-currency support: let users pick a display currency",
])
def test_real_work_is_not_filtered(title):
    """The filter must stay narrow. Swallowing real apps would be a silent, invisible failure —
    the screen would look tidy and be wrong."""
    assert not _is_internal(title), title


# --------------------------------------------------------------------------- #
# A product name, not a directive
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("raw,want", [
    # The name is quoted inside a directive. This is the common shape from Ask BOSS.
    ("Web app — Build 'Living Colors' — a VIBRANT, COLORFUL site", "Living Colors"),
    ('Build "HookWatch" — a webhook inspector', "HookWatch"),
    # Name — description: the name is the part before the dash.
    ("Shorty — self-hosted URL shortener", "Shorty"),
    ("BugBox — Mini issue tracker", "BugBox"),
    # Artifact type is not a name. This is the operator's original complaint.
    ("Shorty — Re-build PRD", "Shorty"),
    ("PRD: Invoice-Chaser", "Invoice-Chaser"),
    ("Product Requirements Document — Shorty", "Shorty"),
])
def test_the_product_name_is_extracted(raw, want):
    assert _clean_title(raw) == want


@pytest.mark.parametrize("raw,want", [
    ("Shorty — self-hosted URL shortener · retry 2", "Shorty"),
    ("Living Colors · retry 3", "Living Colors"),
    ("[1/6] Define domain types and static content data · retry 2",
     "Define domain types and static content data"),
    ("Coaching HTTP service (F1+F2) — hardened: /coaching-moment + /health",
     "Coaching HTTP service (F1+F2)"),
])
def test_run_bookkeeping_is_not_part_of_the_name(raw, want):
    """"retry 2" and "[1/6]" describe THIS RUN, not the thing being built. Leaving them in is
    what split one app into several rows on the screen."""
    assert _clean_title(raw) == want


def test_a_document_type_alone_is_not_a_name():
    """A card once read "Product Requirements Document" — what the artifact IS, not what the
    product is called. Better to admit we do not know."""
    for raw in ("Product Requirements Document", "PRD", "Spec", "  design  "):
        assert _clean_title(raw) == "", raw


def test_a_name_survives_unchanged():
    """The cleaning must not mangle something already correct."""
    for raw in ("Living Colors", "Invoice-Chaser", "HookWatch"):
        assert _clean_title(raw) == raw


def test_two_decorations_of_one_name_collapse_to_the_same_thing():
    """"Shorty — Re-build PRD" and "Shorty — self-hosted URL shortener · retry 2" were two rows
    on the screen for one app. After cleaning they are one name, which is what lets the lineage
    grouping merge them."""
    assert _clean_title("Shorty — Re-build PRD") == \
           _clean_title("Shorty — self-hosted URL shortener · retry 2")


def test_a_title_is_bounded():
    assert len(_clean_title("x" * 500)) <= 80


# --------------------------------------------------------------------------- #
# One app, one row
# --------------------------------------------------------------------------- #
from boss_engine.services.console_v2 import _lineage_key  # noqa: E402


def _run(run_id, title, **ctx):
    return {"id": run_id, "title": title, "finalize_ctx": ctx or None}


def test_an_explicit_app_id_wins():
    """When BOSS recorded the lineage, that is the answer — never second-guess it with a name."""
    a = _run("crun_1", "Shorty", app_id="app_7")
    b = _run("crun_2", "Something else entirely", app_id="app_7")
    assert _lineage_key(a) == _lineage_key(b)


def test_root_run_id_still_works():
    a = _run("crun_1", "Shorty", root_run_id="crun_0")
    b = _run("crun_2", "Shorty rebuilt", root_run_id="crun_0")
    assert _lineage_key(a) == _lineage_key(b)


def test_two_rows_for_one_app_become_one():
    """Live screen: "Shorty — Re-build PRD" and "Shorty — self-hosted URL shortener · retry 2"
    were two separate projects. Same app, two decorations of one name."""
    a = _run("crun_1", "Shorty — Re-build PRD")
    b = _run("crun_2", "Shorty — self-hosted URL shortener · retry 2")
    assert _lineage_key(a) == _lineage_key(b)


def test_living_colors_stops_being_two_apps():
    a = _run("crun_1", "Web app — Build 'Living Colors' — a VIBRANT, COLORFUL site")
    b = _run("crun_2", "Living Colors · retry 3")
    assert _lineage_key(a) == _lineage_key(b)


def test_different_apps_stay_apart():
    """The reason the old code refused to look at titles at all. An exact match on the
    EXTRACTED name is narrow enough; a prefix match would not be."""
    a = _run("crun_1", "Shorty — self-hosted URL shortener")
    b = _run("crun_2", "Shorty Pro — the paid one")
    assert _lineage_key(a) != _lineage_key(b)


def test_case_does_not_split_an_app():
    assert _lineage_key(_run("c1", "living colors")) == _lineage_key(_run("c2", "Living Colors"))


def test_a_run_with_no_usable_name_is_its_own_app():
    """Guessing would merge unrelated work under one card. A duplicate row is the lesser
    failure — it is visible, and it is honest."""
    a = _run("crun_1", "PRD")
    b = _run("crun_2", "Spec")
    assert _lineage_key(a) != _lineage_key(b)
    assert _lineage_key(a) == "crun_1"


def test_an_explicit_id_is_never_overridden_by_a_matching_name():
    """Two apps deliberately given different lineages must not be merged just because someone
    called them the same thing."""
    a = _run("crun_1", "Shorty", app_id="app_1")
    b = _run("crun_2", "Shorty", app_id="app_2")
    assert _lineage_key(a) != _lineage_key(b)


# --------------------------------------------------------------------------- #
# The name is inside the document, not the document's name
#
# The real shape on the private host: the run title is a generic category, an em-dash, and then the
# ENTIRE PRD markdown pasted in. 30 runs looked like this, and taking the head of the dash gave
# eight rows all called "Web app".
#
#   Web app — # BugBox – Product Requirements Document (PRD)  *Version 1.0 – 10 Aug...
#   Web app — # ApproveIt Canary – PRD (BOSS #1c23722, PR #90)   **Document version...
#   Web app — # Product Requirements Document (PRD)   **Project:** Lightweight Self-...
#
# "you even don't know the difference of artifact name vs actual project title" — the PRD
# heading is the artifact; the name inside it is the product.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("raw,want", [
    ("Web app — # BugBox – Product Requirements Document (PRD)  *Version 1.0 – 10 Aug*", "BugBox"),
    ("Web app — # HookWatch – Product Requirements Document (PRD)  **Version:** 1.0.0", "HookWatch"),
    ("Web app — # Official BugBox – Product Requirements Document (PRD)   **Version:**",
     "Official BugBox"),
    ("Web app — # ApproveIt Canary – PRD (BOSS #1c23722, PR #90)    **Document version",
     "ApproveIt Canary"),
    ("Web app — # BugBox v1.0 – Product Requirements Document (PRD)   **Version 1.0", "BugBox v1.0"),
    ("Web app — # ApproveIt – Expense Approval Workflow   **Product Requirements Doc",
     "ApproveIt"),
])
def test_the_name_is_taken_from_the_prd_heading(raw, want):
    assert _clean_title(raw) == want


def test_a_heading_that_is_only_the_artifact_falls_through_to_the_project_field():
    """"# Product Requirements Document (PRD)  **Project:** Lightweight Self-hosted..."
    The heading names the document, so the product name is wherever the document says it."""
    raw = ("Web app — # Product Requirements Document (PRD)   "
           "**Project:** Lightweight Self-hosted Bookmarks")
    assert _clean_title(raw) == "Lightweight Self-hosted Bookmarks"


@pytest.mark.parametrize("raw", [
    "Web app — Build a professional, polished single-page marketing website · retry 2",
    "Web app",
    "Website — something or other",
])
def test_a_category_is_never_used_as_a_name(raw):
    """"Web app" is what KIND of thing it is. Eight rows on the live screen were all called
    that, which is the same failure as "Product Requirements Document" wearing a card."""
    assert _clean_title(raw) != "Web app"
    assert _clean_title(raw) != "Website"


def test_the_quoted_name_still_wins_inside_a_category_prefix():
    assert _clean_title(
        "Web app — Build 'Living Colors' — a VIBRANT, COLORFUL portfolio · retry 1"
    ) == "Living Colors"


# --------------------------------------------------------------------------- #
# More of BOSS's own scaffolding, read off the same screen
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("title", [
    "actor-share test) do the thing",
    "[ga-slice] simulated fixture",
    "e2e smoke",
    "sealed-e2e-canary",
    "Auto-repair: turn cap exhausted",
    "Hybrid substrate validation: word-frequency counter",
    "[boss:coding e2e test] Add a greet() helper",
])
def test_test_scaffolding_is_not_a_project(title):
    assert _is_internal(title), title


@pytest.mark.parametrize("title", [
    "Leads Capture Tool v1",
    "Pricing page (monthly/annual toggle)",
    "Waitlist signup (landing + DB)",
    "Official BugBox",
])
def test_real_apps_survive_the_wider_filter(title):
    """Every widening of this filter risks swallowing real work, which fails silently — the
    screen just looks tidier than the truth."""
    assert not _is_internal(title), title


# --------------------------------------------------------------------------- #
# Two rough edges found by running the cleaner over all 353 real titles
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("title", [
    "(actor-share test) · retry 2",
    "(actor-share test)",
    "(workspace test) what's the status of this task?",
])
def test_scaffolding_in_parentheses_is_still_scaffolding(title):
    """The anchor allowed a leading "[" but not a leading "(", so three of these reached the
    Projects screen as apps called "actor-share test)"."""
    assert _is_internal(title), title


def test_a_long_name_is_cut_at_a_word_and_marked():
    """Cutting mid-word produced "...convert amount" and "...queryable by focu", which read as
    the actual name rather than as a truncation. An ellipsis says a name was shortened; a
    severed word says the name is broken."""
    raw = ("Add multi-currency support: let users pick a display currency and convert amounts "
           "(USD/EUR/INR/GBP) with a rate lookup")
    got = _clean_title(raw)
    assert len(got) <= 80
    assert got.endswith("…")
    assert not got[:-1].endswith(" ")
    # The last kept word must be whole.
    assert raw.startswith(got[:-1].rstrip("… "))


def test_a_name_that_fits_gains_no_ellipsis():
    assert _clean_title("Leads Capture Tool v1") == "Leads Capture Tool v1"


# --------------------------------------------------------------------------- #
# The last two on the live screen
# --------------------------------------------------------------------------- #
def test_a_rebuild_directive_names_the_app_not_the_repo():
    """Live: "Re-build of `example-user/boss-sandbox` Simple T — List". The repo is where the code
    goes, not what the product is called."""
    got = _clean_title("Re-build of `example-user/boss-sandbox` Simple T — List")
    assert "boss-sandbox" not in got
    assert "Re-build" not in got
    assert got.startswith("Simple T")


def test_a_rebuild_of_a_named_app_keeps_the_name():
    assert _clean_title("Re-build of `example-user/boss-sandbox` Shorty") == "Shorty"


def test_the_brief_is_the_last_resort_for_a_name():
    """Live: a run titled "📄 Product Requirements Document" — the artifact type and nothing
    else. Its BRIEF holds the actual PRD, whose H1 names the product. Looking there beats
    labelling the card "Untitled app"."""
    from boss_engine.services.console_v2 import _name_from_document
    brief = "# Bookmarks — Product Requirements Document (PRD)\n\n**Version:** 1.0"
    assert _name_from_document(brief) == "Bookmarks"


def test_a_brief_with_no_name_in_it_yields_nothing():
    """Better to admit we do not know than to put the first line of a brief on a card as if it
    were a product name."""
    from boss_engine.services.console_v2 import _name_from_document
    assert _name_from_document("Please build something nice for the team.") == ""
