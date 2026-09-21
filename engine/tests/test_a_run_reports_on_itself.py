"""The report an operator asks for at the end of a build — about the PRODUCT, not the spend.

    "What I want in the report is the details of what was done from research, meeting outcome,
     what feature was builded and tested, test pass results, even end user acceptance criteria,
     and how to use the app.. these are very useful for the user who interested to see after
     building the app, then what else we could build on top of the existing implementation -
     what can be improved, P1, P2 and other guidance for next steps"

Every one of those was already recorded. The panel's PRD carries the summary, the screens and
numbered acceptance criteria; the code task's state carries what was implemented, which test
files exist, what actually ran, three reviewer verdicts and the findings deferred rather than
fixed; the run started a preview. Nothing was missing except the joining — and the first
version of this report joined none of it, leading instead with four figures and a cost table.
"""

from __future__ import annotations

from boss_engine.services.run_retrospective import (
    _acceptance_criteria,
    _prd_sections,
    _prose,
    _screens,
    _trim,
    next_steps,
    workflow_findings,
    workflow_markdown,
)

PRD = """# PRD: Incident Timeline Builder

## 1. Summary

Merges an alert log and a chat transcript into one timeline.

### 5.1 User journey

```mermaid
flowchart LR
A[Import] --> B[Merge]
```

### 5.3 Screens

**Import** — paste or upload two sources side by side.

```
+------------------+
|  a box diagram   |
+------------------+
```

**Timeline editor** — the ordered spine, milestones pinned with citations.

## 9. Acceptance Criteria

1. **AC1 — Real log in, full timeline out.** All four milestones with citations.
2. **AC2 — Corrections work and recompute.** TTD/TTR update on a drag.
"""

#: Shaped like `assemble_workflow`, with this run's real numbers.
REPORT = {
    "ok": True,
    "workflow": "Standard Product Delivery",
    "status": "completed",
    "directive": "Build an incident timeline builder.\nWho it is for: an on-call engineer\n",
    "elapsed_minutes": 135.1,
    "working_minutes": 54.6,
    "total_usd": 10.32,
    "steps": [
        {"step_id": "build", "label": "Build", "status": "succeeded", "attempt": 1,
         "usd": 8.58, "seconds": 4560},
    ],
    "contributors": [
        {"role": "software_engineer", "calls": 1, "turns": 68, "usd": 4.14,
         "models": ["claude-sonnet-5"]},
    ],
    "host": {"state": "ok"},
    "findings": [],
    "product": {
        "name": "Incident Timeline Builder",
        "prd": {"summary": "Merges an alert log and a chat transcript into one timeline."},
        "screens": [{"name": "Import", "does": "paste or upload two sources."}],
        "acceptance": [{"id": "AC1", "text": "Real log in, full timeline out", "detail": ""}],
        "preview_url": "/v1/previews/prev_93f2/web/",
        "preview_status": "running",
        "build": {
            "summary": "853/854 tests pass across the whole repo.",
            "review_summary": "FINDING: BLOCKER incident_core/parse.py:76 — bare chat times "
                              "are anchored to 1900-01-01, corrupting the merge.",
            "files_changed": 36,
            "test_files": [f"tests/test_incident_{n}.py" for n in range(12)],
            "commands_run": [{"command": "npm install --no-audit", "ok": True,
                              "exit_code": 0}],
            "verdicts": {"review": "refuted", "security": "clean", "consistency": "drift"},
            "deferred_findings": [
                {"path": "incident_storage.py", "line": 82, "severity": "minor",
                 "text": "a malformed file takes down the whole list screen"},
            ],
            "has_e2e": False,
            "pull_request_url": "https://github.com/paramg/boss-sandbox/pull/211",
        },
    },
}


def _full() -> dict:
    report = {**REPORT}
    report["next_steps"] = next_steps(report["product"], report["findings"])
    return report


# --------------------------------------------------------------------------------------- #
# Reading the panel's PRD
# --------------------------------------------------------------------------------------- #

def test_sections_are_matched_on_words_not_section_numbers():
    """The panel numbers its sections and the numbering moves between runs. Anchoring on the
    number is how a section silently goes missing from every later report."""
    sections = _prd_sections(PRD)
    assert set(sections) == {"summary", "journey", "screens", "acceptance"}


def test_diagrams_are_stripped_rather_than_printed():
    """Panels draw. The first report rendered a raw ```mermaid block under "How to use it" and
    an ASCII box sketch under "Screens" — a wall of pipes where an explanation should be."""
    prose = _prose(_prd_sections(PRD)["screens"])
    assert "+---" not in prose
    assert "```" not in prose
    assert "paste or upload" in prose


def test_the_screens_become_a_walkthrough():
    """The journey section is frequently nothing but a flowchart, so it cannot answer "how do
    I use this". The screens can: one name, one line, in the order a person meets them."""
    screens = _screens(_prd_sections(PRD)["screens"])
    assert [s["name"] for s in screens] == ["Import", "Timeline editor"]
    assert screens[0]["does"].startswith("paste or upload")


def test_numbered_criteria_are_read_with_their_labels():
    criteria = _acceptance_criteria(_prd_sections(PRD)["acceptance"])
    assert [c["id"] for c in criteria] == ["AC1", "AC2"]
    assert criteria[0]["text"] == "Real log in, full timeline out"


def test_a_long_agent_summary_is_cut_at_a_sentence():
    """Agent summaries run to pages. A paragraph that stops mid-clause reads as truncated data
    rather than as a summary."""
    long = "First sentence here. " * 60
    assert _trim(long, 120).endswith("…")
    assert ". …" in _trim(long, 120)


# --------------------------------------------------------------------------------------- #
# What the document says
# --------------------------------------------------------------------------------------- #

def test_it_is_titled_with_the_thing_that_was_built():
    """Not the directive sliced at 110 characters, which names nothing and reads as a broken
    sentence."""
    assert workflow_markdown(_full()).splitlines()[0] == "# Incident Timeline Builder"


def test_it_says_what_the_thing_is_before_what_it_cost():
    doc = workflow_markdown(_full())
    assert doc.index("## What it is") < doc.index("## Run facts")


def test_it_offers_a_link_that_actually_opens():
    """The preview binds to loopback inside the API host: this run recorded three different
    ports for one preview and neither opened. Only the gateway path works."""
    doc = workflow_markdown(_full())
    assert "/v1/previews/prev_93f2/web/" in doc
    assert "running" in doc


def test_what_shipped_leads_with_facts_not_with_the_agents_excuse():
    """The implementing agent's prose opens wherever it chose — on this run, with a paragraph
    explaining why a failing test was not its fault. Presented as the summary it reads as what
    the build delivered."""
    doc = workflow_markdown(_full())
    shipped = doc[doc.index("## What shipped"):doc.index("## How to use it")]
    assert shipped.index("36 files changed") < shipped.index("Engineer")
    assert "**Engineer's note.**" in shipped


def test_the_reviewers_finding_is_shown_not_just_the_verdict_word():
    """`adversarial_verdict` alone says "refuted" and hides that what was refuted was a
    BLOCKER in the core merge feature."""
    doc = workflow_markdown(_full())
    assert "What the reviewer found" in doc
    assert "BLOCKER" in doc


def test_tests_written_are_stated_against_tests_actually_run():
    """The most useful line in the report: twelve test files written, and the only recorded
    verification was an install command."""
    doc = workflow_markdown(_full())
    assert "12 test file(s) written" in doc
    assert "npm install" in doc


def test_acceptance_criteria_are_listed_and_never_ticked_without_evidence():
    """A tick beside a criterion nobody checked is the most expensive kind of wrong: it is the
    exact claim a reader repeats to somebody else."""
    doc = workflow_markdown(_full())
    assert "**AC1** — Real log in, full timeline out" in doc
    assert "Not verified against the build" in doc


def test_a_run_with_no_prd_still_produces_a_document():
    """Not every workflow convenes a panel. The report must degrade to what it has rather than
    failing at the end of a successful build."""
    bare = {**REPORT, "product": {}, "next_steps": []}
    doc = workflow_markdown(bare)
    assert doc.startswith("# ")
    assert "## Run facts" in doc


def test_a_missing_run_renders_a_document_rather_than_raising():
    doc = workflow_markdown({"ok": False, "error": "no workflow run wfr_nope"})
    assert "no workflow run wfr_nope" in doc


def test_the_document_is_markdown_so_the_existing_pdf_route_renders_it():
    doc = workflow_markdown(_full())
    assert doc.startswith("# ")
    assert "| Check | Verdict |" in doc


# --------------------------------------------------------------------------------------- #
# What to do next
# --------------------------------------------------------------------------------------- #

def test_tests_written_but_never_run_is_a_p1():
    """The build is green on a command that installs dependencies. Nothing has been shown to
    work, including the parts the summary says work."""
    ranked = next_steps(REPORT["product"], [])
    first = ranked[0]
    assert first["priority"] == "P1"
    assert "never run" in first["item"]


def test_a_real_test_command_is_not_reported_as_missing():
    """The check must recognise a genuine test run, or it accuses every build that passed."""
    product = {**REPORT["product"]}
    product["build"] = {**product["build"],
                        "commands_run": [{"command": "python3 -m pytest -q", "ok": True}]}
    assert not any("never run" in n["item"] for n in next_steps(product, []))


def test_a_refuted_blocker_outranks_a_refuted_opinion():
    """The verdict field says "refuted" and nothing else. What was refuted here was a reviewer
    reporting a confirmed defect in the core feature."""
    ranked = next_steps(REPORT["product"], [])
    refuted = next(n for n in ranked if n["area"] == "review")
    assert refuted["priority"] == "P1"

    mild = {**REPORT["product"]}
    mild["build"] = {**mild["build"], "review_summary": "a naming quibble"}
    quiet = next(n for n in next_steps(mild, []) if n["area"] == "review")
    assert quiet["priority"] == "P2"


def test_a_deferred_finding_carries_where_it_is():
    ranked = next_steps(REPORT["product"], [])
    deferred = next(n for n in ranked if n["area"] == "deferred")
    assert "incident_storage.py:82" in deferred["item"]


def test_a_stalling_machine_is_reported_as_environment_not_as_the_pipelines_fault():
    ranked = next_steps(REPORT["product"], [{"area": "system", "severity": "warn",
                                             "finding": "stalled", "action": "fix it"}])
    assert any(n["area"] == "environment" for n in ranked)


def test_next_steps_are_ordered_worst_first_in_the_document():
    doc = workflow_markdown(_full())
    section = doc[doc.index("## What to do next"):]
    assert section.index("**P1**") < section.index("**P2**") < section.index("**P3**")


# --------------------------------------------------------------------------------------- #
# The run itself, kept as a footnote
# --------------------------------------------------------------------------------------- #

def test_system_and_functionality_problems_are_kept_apart():
    """Mixing them is how "BOSS is regressing" got said about a machine that was thrashing."""
    found = workflow_findings(REPORT["steps"], {"state": "degraded", "detail": "stalled"})
    assert {f["area"] for f in found} == {"system"}


def test_a_failure_with_no_reason_says_so_rather_than_inventing_one():
    steps = [{"step_id": "deploy", "label": "Deploy", "status": "failed", "attempt": 1,
              "usd": 0.0, "error": None}]
    assert "no reason recorded" in workflow_findings(steps, {"state": "ok"})[0]["finding"]


def test_an_automatic_approval_is_recorded_as_such_and_not_as_a_fault():
    steps = [{"step_id": "spec_review", "label": "Approve the spec", "status": "succeeded",
              "attempt": 1, "usd": 0.0, "approved_by": "auto_approval",
              "approved_automatically": True}]
    auto = workflow_findings(steps, {"state": "ok"})[0]
    assert auto["severity"] == "info"
    assert "not a fault" in auto["action"]


def test_cost_and_agents_are_present_but_last():
    """Real, and the footnote — not the headline the first version made them."""
    doc = workflow_markdown(_full())
    assert "$10.32" in doc
    assert "software_engineer" in doc
    assert doc.index("## Run facts") > doc.index("## What to do next")


# --------------------------------------------------------------------------------------- #
# The report must describe THIS run's product
# --------------------------------------------------------------------------------------- #

def test_the_prd_is_fetched_through_this_runs_own_meeting():
    """THE BUG THIS PINS. The lookup selected the most recent PRD in the whole PROJECT and used
    the meeting id only as a guard, so every run was titled after whatever was built last:

        "Incident Timeline Builder is showing for all workflows (this is bug)"

    Five different runs each reported themselves as that app, with its summary, screens and
    acceptance criteria attached to somebody else's build. A report that confidently describes
    the wrong product is worse than one that describes nothing — every fact in it is a claim
    about work that did not happen here.

    `meeting_runs.minutes_artifact_id` is the direct link and always was.
    """
    from pathlib import Path as _Path

    source = _Path(__file__).resolve().parents[1] / (
        "boss_engine/services/run_retrospective.py"
    )
    body = source.read_text(encoding="utf-8")
    lookup = body[body.index("meeting_id = next("):body.index("build_id = next(")]

    assert "m.minutes_artifact_id" in lookup, (
        "the PRD must be reached through this run's own meeting"
    )
    assert "ORDER BY created_at DESC" not in lookup, (
        "ordering by recency is what made every run show the newest app in the project"
    )
    assert ":m" in lookup and "m.id = :m" in lookup
