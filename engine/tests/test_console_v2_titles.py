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
