"""The data behind Console v2, shaped the way the screens actually read it.

WHY THIS IS ONE MODULE AND NOT EIGHTY-TWO FEEDS
-----------------------------------------------
The v1 console fetches 82 endpoints per render and shows nothing until the slowest answers.
Measured in production: TTFB 24s on the overview area, and 13.2s of it was a single audit
export that only one area displayed. Rendering all fifty areas cost 1ms — the waiting was
everything.

So v2 does the opposite: one screen, one call, returning exactly what that screen renders.
The budget is TTFB <= 2s and it is enforced by a test, not by intention.

THE TRANSLATION
---------------
The artifact talks about a company employing agents on projects. BOSS's nouns are different,
so the mapping is written down here once rather than being re-derived in each screen:

    artifact          BOSS
    ---------------   ---------------------------------------------------
    company           org / project (the console's current scope)
    project           an APP — the lineage of code-task runs that build one thing
    stage             DISCOVERY -> BUILD -> REVIEW -> SHIPPED
    lead              the agent that owns the app
    steps             turns recorded across the lineage
    spend             sum of the lineage's llm cost
    gate              a pending decision

An app is a LINEAGE, not a run. That distinction has already caused real damage once: asked
to deploy an app, BOSS judged deployability from a single cancelled run and refused, while a
completed run sat in the same lineage. Everything here groups by lineage for that reason.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import text as sql

from . import decision_actions

def _meeting_title(title, directive=None) -> str:
    """meeting_service.meeting_title, imported on first use: that module pulls in the model
    adapters, and this one is read on every console page."""
    from .meeting_service import meeting_title

    return meeting_title(title, directive)


# Stage vocabulary, in order. Progress is derived from position, so the order is meaningful.
STAGES = ("DISCOVERY", "BUILD", "REVIEW", "SHIPPED")
STAGE_PCT = {"DISCOVERY": 22, "BUILD": 64, "REVIEW": 41, "SHIPPED": 100}

# Status vocabulary the v2 screens use. Deliberately NOT BOSS's raw run statuses: the artifact
# speaks in terms an operator recognises, and mapping once here keeps that consistent.
RUNNING, GATED, DONE, STUCK, IDLE = "RUNNING", "GATED", "DONE", "STUCK", "IDLE"

_DONE_STATES = ("completed", "succeeded", "passed")
_FAILED_STATES = ("failed", "error")

# BOSS's own housekeeping, which is not a product an operator has a view on.
#
# v1 filters these out of Factory and v2 must too — without it the first thing on the new
# Projects screen was "Captain: QUALITY IMPROVE PASS", "Follow-up: deferred review findings"
# and "List the exact names of the tools you can call", three of which are BOSS talking to
# itself. A console whose headline list is dominated by internal chatter teaches the operator
# to ignore the list.
#
# Kept in step with INTERNAL_RUN in apps/web/app/code-tasks-ui.tsx.
#
# The live screen listed 40 rows; 22 of them were BOSS benchmarking itself — five bakeoff
# harnesses times five exercises, plus the QA benchmark suite. The pattern set below was
# extended from what was actually on screen, not from what seemed likely.
_INTERNAL_TITLE = re.compile(
    r"^\s*[\[(]?\s*(?:"
    r"captain:|quality improve pass|follow-up:\s*deferred review"
    r"|list the exact names|ceo command:|boss-\d+"
    # BOSS's own model bakeoffs and end-to-end exercises: "[bakeoff:qwen_all#1] URL slug
    # generator", "[boss:coding e2e test] Add a greet() helper", "QA benchmark: BugBox".
    r"|bakeoff:|boss:coding|qa benchmark:|smoke test:|regression:"
    # Notices ABOUT a run rather than a thing to build.
    r"|the deployment for run\b|this is the discovery task\b"
    # More of BOSS's own scaffolding, read off the same screen: harness fixtures, canaries,
    # self-repair notices, and substrate validation runs.
    r"|ga-slice|sealed-e2e|e2e smoke|auto-repair:|hybrid substrate validation"
    r"|\w[\w-]*\s+test\)"
    # A host's profile check, under whatever name the host has ("reference host profile check").
    r"|[\w-]+\s+host\s+profile\s+check\b"
    r")",
    re.I,
)

#: A title that is a question, or a message addressed to BOSS, is not a product. These reached
#: the Projects screen because Ask BOSS records the operator's words as the run title.
_NOT_A_PRODUCT = re.compile(
    r"^\s*(?:what|who|when|where|why|how|can you|could you|do you|does|is there|are there)\b"
    r"|^\s*\w+\s+test\)"          # "workspace test) what's the status of this task?"
    r"|\?\s*$",                   # anything ending in a question mark
    re.I,
)


def _is_internal(title: str) -> bool:
    t = title or ""
    return bool(_INTERNAL_TITLE.match(t) or _NOT_A_PRODUCT.search(t))


def _lineage_key(row: dict) -> str:
    """Which app a run belongs to.

    finalize_ctx.app_id is the explicit marker and root_run_id is the older one. Both are
    authoritative when present.

    When neither is set the run falls back to its cleaned NAME, and the distinction matters:
    matching on the raw title would merge unrelated apps that happen to share a prefix (which
    is how one app's deployment once appeared on another's card), but "Living Colors" and
    "Shorty" each appeared TWICE on the live screen as separate apps, because their raw titles
    differed by a retry suffix and an artifact type. An exact match on the extracted name is
    narrow enough to be safe and wide enough to fix that.

    A run with no usable name stays its own app — guessing would be worse than a duplicate.
    """
    ctx = row.get("finalize_ctx") or {}
    explicit = ctx.get("app_id") or ctx.get("root_run_id")
    if explicit:
        return str(explicit)
    name = _clean_title(str(row.get("title") or ""))
    return f"name:{name.casefold()}" if name else str(row["id"])


#: Names of artefacts, which are never names of products.
_ARTIFACT = r"PRD|Product Requirements Document|Spec|Specification|Design|Overview"

#: Bookkeeping about a particular RUN. "retry 2" and "[1/6]" describe an attempt, not the thing
#: being built, and leaving them in is what split one app into several rows on the screen.
_RUN_BOOKKEEPING = (
    (re.compile(r"\s*[·•]\s*retry\s*\d+\s*$", re.I), ""),
    # "Re-build of `example-user/boss-sandbox` Simple T — List": where the code goes, not what the
    # product is called.
    (re.compile(r"^\s*re-?build\s+of\s+`?[\w.-]+/[\w.-]+`?\s*", re.I), ""),
    (re.compile(r"^\s*\[?\s*\d+\s*/\s*\d+\s*\]\s*"), ""),
    (re.compile(r"\s*\(attempt\s*\d+\)\s*$", re.I), ""),
)

#: "Build 'Living Colors' — a VIBRANT..." — the operator's directive with the name quoted
#: inside it. This is the shape Ask BOSS produces, and it is where the real name lives.
_QUOTED_NAME = re.compile(
    r"\b(?:build|create|make|re-?build)\s+[\"'\u2018\u201c]([^\"'\u2019\u201d]{2,60})"
    r"[\"'\u2019\u201d]",
    re.I,
)

#: What KIND of thing it is, which is never what it is CALLED. Eight rows on the live screen
#: were all named "Web app", because the title was "Web app - <the entire PRD pasted in>" and
#: the head of the dash was taken as a name.
_CATEGORY = re.compile(
    r"^\s*(?:web\s*app|webapp|website|web\s*site|app|application|service|api|site|tool|"
    r"landing\s*page|mobile\s*app|script)\s*$",
    re.I,
)

#: The run title often carries an entire PRD after the category. The product name is the
#: markdown H1 -- "# BugBox - Product Requirements Document (PRD)" -- and NOT the document type
#: that follows it. That distinction is the operator's complaint in one line: the PRD heading
#: names the artifact; the name inside it names the product.
#:
#: MULTILINE matters: a run title arrives flattened to one line, but a spec brief is real
#: markdown with newlines, and without it `$` demands end-of-string and matches neither.
_MARKDOWN_HEADING = re.compile(r"^#{1,6}[ \t]*(.+)$", re.M)

#: When the heading names only the document, the PRD states the product separately:
#: "# Product Requirements Document (PRD)  **Project:** Lightweight Self-hosted Bookmarks".
_PROJECT_FIELD = re.compile(
    r"\*\*\s*(?:project|product|app|name)\s*:?\s*\*\*\s*:?\s*([^\n*#]{2,60})",
    re.I,
)


def _strip_artifact(text: str) -> str:
    """Remove the document type from around a name.

    "BugBox - Product Requirements Document (PRD)" is one string naming two things: a product
    and the kind of document describing it. Only the first is a title.
    """
    t = text.strip()
    t = re.sub(rf"^(?:{_ARTIFACT})\s*[:\-\u2013\u2014]\s*", "", t, flags=re.I)
    t = re.sub(rf"\s*[\u2013\u2014-]\s*(?:re-?build\s+)?(?:{_ARTIFACT})\b.*$", "", t, flags=re.I)
    t = re.sub(rf"\s*\(\s*(?:{_ARTIFACT})\s*\)\s*$", "", t, flags=re.I)
    return t.strip(" -\u2013\u2014:\u00b7")


def _name_from_document(body: str) -> str:
    """The product name out of a PRD that was pasted into the run title.

    30 runs in production were titled "Web app - <the entire PRD>", so the name was sitting in
    the markdown H1 all along:

        # BugBox - Product Requirements Document (PRD)  *Version 1.0*   -> BugBox
        # ApproveIt Canary - PRD (BOSS #1c23722, PR #90)                -> ApproveIt Canary
        # Product Requirements Document (PRD)  **Project:** Bookmarks   -> Bookmarks

    The third case is the interesting one: when the heading names only the DOCUMENT, the
    product is named by a field inside it. Taking the heading there would put "Product
    Requirements Document" on the card, which is the exact complaint this is answering.
    """
    body = (body or "").strip()
    if not body:
        return ""
    heading = _MARKDOWN_HEADING.match(body)
    if heading:
        # The pasted markdown is flattened to one line, so the heading runs on into the body.
        # Cut at the first bold field or sentence marker.
        line = re.split(r"\s{2,}|\*\*|\s\*", heading.group(1), maxsplit=1)[0]
        name = _strip_artifact(line)
        # A heading is often "Name - what it does" ("ApproveIt - Expense Approval Workflow").
        # Same short-head rule as below: a name, not a sentence.
        parts = re.split(r"\s*[–—]\s*", name, maxsplit=1)
        if len(parts) == 2 and parts[0].strip() and len(parts[0].split()) <= 5:
            name = parts[0].strip()
        if name and not re.fullmatch(rf"(?:{_ARTIFACT})[\s()]*", name, re.I):
            return name

    field = _PROJECT_FIELD.search(body)
    if field:
        return _strip_artifact(field.group(1))
    return ""


def _clean_title(raw: str) -> str:
    """A product NAME - not a document type, not a category, not a run's bookkeeping.

    Four things kept ending up on cards that are none of them:

      "Product Requirements Document"   what the artifact IS, not what the product is called;
      "Web app"                         what KIND of thing it is (eight rows shared this one);
      "Web app - Build 'Living Colors'" an instruction;
      "Shorty - ... - retry 2"          an attempt number.

    The last matters beyond tidiness: two decorations of one name are two rows on the Projects
    screen, so "Shorty - Re-build PRD" and "Shorty - self-hosted URL shortener" showed up as
    separate apps. Cleaning them to the same string is what lets them merge.

    Returns "" when nothing survives, so the caller can say it does not know rather than
    labelling every app with the name of a document.
    """
    t = (raw or "").strip().split("\n")[0].strip()
    t = re.sub(r"^[\s\W_]*(?:\ud83d[\udcc4-\udcdd]|\U0001F4C4|\U0001F4DD)?[\s\W_]*", "", t)

    for pattern, repl in _RUN_BOOKKEEPING:
        t = pattern.sub(repl, t)

    # A quoted name inside a directive wins outright - it is the one place the operator said
    # what the thing is CALLED rather than what it should do.
    quoted = _QUOTED_NAME.search(t)
    if quoted:
        return quoted.group(1).strip()[:80]

    # "<category> - <rest>": the head says nothing, so the name must come from the rest.
    parts = re.split(r"\s+[\u2013\u2014]\s+", t, maxsplit=1)
    if len(parts) == 2 and _CATEGORY.match(parts[0]):
        from_doc = _name_from_document(parts[1])
        if from_doc:
            return from_doc[:80]
        # No name anywhere in it. Better to show the directive than eight identical rows
        # reading "Web app".
        t = parts[1].strip()

    t = _strip_artifact(t)

    # "Name - description". Only when the head looks like a NAME: short, not a sentence, and
    # not a category. "Add multi-currency support: let users pick..." must survive whole,
    # because there the head IS the work.
    dashed = re.split(r"\s+[\u2013\u2014]\s+", t, maxsplit=1)
    if len(dashed) == 2:
        head = dashed[0].strip()
        if head and len(head) <= 40 and len(head.split()) <= 5 and not _CATEGORY.match(head):
            t = head

    t = t.strip(" -\u2013\u2014:\u00b7")
    if _CATEGORY.match(t):
        return ""
    if re.fullmatch(rf"(?:{_ARTIFACT})[\s:\u2013\u2014()-]*", t, flags=re.I):
        return ""
    return _shorten(t)


def _shorten(t: str, limit: int = 80) -> str:
    """Cut at a word, and show that a cut happened.

    A hard slice produced "...and convert amount" and "...queryable by focu", which read as the
    name rather than as a truncation of it.
    """
    if len(t) <= limit:
        return t
    cut = t[: limit - 1]
    space = cut.rfind(" ")
    if space > limit // 2:
        cut = cut[:space]
    return cut.rstrip(" ,;:-\u2013\u2014") + "\u2026"


def _stage_of(runs: list[dict]) -> tuple[str, str]:
    """(stage, status) for an app, from what EXISTS rather than a status column.

    Derived from artefacts because a status field can be stale while the work has moved on —
    a deployment run means it shipped, a PR means it is in review, a code run means it is
    building. The furthest-along signal wins.
    """
    any_pr = any(r.get("pull_request_url") for r in runs)
    any_running = any(str(r.get("status")) == "running" for r in runs)
    any_done = any(str(r.get("status")) in _DONE_STATES for r in runs)
    all_failed = runs and all(str(r.get("status")) in _FAILED_STATES for r in runs)

    if any_done and any_pr:
        return "SHIPPED", DONE
    if any_pr:
        return "REVIEW", GATED if not any_running else RUNNING
    if any_running:
        return "BUILD", RUNNING
    if all_failed:
        return "BUILD", STUCK
    if any_done:
        return "BUILD", DONE
    return "DISCOVERY", IDLE


def company_overview(connection, project_id: str, *, limit: int = 40) -> dict[str, Any]:
    """Everything the v2 shell and Projects screen render, in one query set."""
    rows = [dict(r) for r in connection.execute(
        sql(
            """
            SELECT id, title, status, phase, spec, state, usage, finalize_ctx,
                   pull_request_url, pull_request_number, created_at, updated_at
            FROM code_task_runs
            WHERE project_id = :p
              AND created_at > now() - interval '90 days'
            ORDER BY created_at DESC
            LIMIT 2000
            """
        ),
        {"p": project_id},
    ).mappings().all()]

    # Group into apps (lineages), newest first by most recent activity.
    apps: dict[str, list[dict]] = {}
    for row in rows:
        apps.setdefault(_lineage_key(row), []).append(row)

    # A run whose title is internal ("Follow-up: deferred review findings") is housekeeping, not
    # a product -- but it still BELONGS to the app it ran against, and its cost is part of that
    # app's cost. Dropping such rows before grouping, which is what this did, meant an app whose
    # only named run cleaned to nothing had no name left to find and was labelled "Untitled app"
    # while its own workspace page showed the real one. Whole groups of nothing but housekeeping
    # are still discarded: those are not apps.
    apps = {
        key: runs for key, runs in apps.items()
        if not all(_is_internal(str(r.get("title") or "")) for r in runs)
    }

    projects = []
    for key, runs in apps.items():
        runs.sort(key=lambda r: r["created_at"], reverse=True)
        title = ""
        for r in runs:                       # newest run that names a PRODUCT
            if _is_internal(str(r.get("title") or "")):
                continue
            title = _clean_title(str(r.get("title") or ""))
            if title:
                break
        if not title:
            # A title that is only an artifact type ("📄 Product Requirements Document") says
            # nothing, but its BRIEF is usually the PRD itself, whose heading names the
            # product. Looking there beats labelling the card "Untitled app".
            for r in runs:
                title = _name_from_document(str((r.get("spec") or {}).get("instructions") or ""))
                if title:
                    break
        if not title:
            # Last resort, and the same one the workspace uses: a housekeeping title is a real
            # name and beats "Untitled app". Both screens must land on it, or they disagree
            # about what you just clicked.
            for r in runs:
                title = _clean_title(str(r.get("title") or ""))
                if title:
                    break
        stage, status = _stage_of(runs)
        micros = sum(int((r.get("usage") or {}).get("cost_usd_micros") or 0) for r in runs)
        steps = sum(int((r.get("state") or {}).get("turns") or 0) for r in runs) or len(runs)
        spec = runs[0].get("spec") or {}
        projects.append({
            "id": key,
            "title": title or "Untitled app",
            "description": str(spec.get("instructions") or "")[:160],
            "status": status,
            "stage": stage,
            "pct": STAGE_PCT.get(stage, 0),
            "runs": len(runs),
            "steps": steps,
            "spend_usd": round(micros / 1_000_000, 2),
            "repo": f"{spec.get('owner') or '?'}/{spec.get('repo') or '?'}",
            "latest_run_id": runs[0]["id"],
            "pull_request_number": next(
                (r.get("pull_request_number") for r in runs if r.get("pull_request_number")), None),
            "updated_at": runs[0]["updated_at"].isoformat() if runs[0].get("updated_at") else None,
        })

    projects.sort(key=lambda p: p["updated_at"] or "", reverse=True)
    # WHAT WAS TRIMMED, SAID OUT LOUD.
    #
    # Two caps stack here and neither used to be visible. The row window above decides which
    # apps EXIST at all — at 400 rows, the private host's 474 runs meant whole apps never entered the
    # grouping, and no `limit` the caller passed could bring them back. That one is now wide
    # enough to hold the project rather than a page of it.
    #
    # This one only decides how many are RETURNED, which is a legitimate page — but a page that
    # does not say it is a page reads as the whole list, and the strip has been showing 40 of
    # 59 as though 40 were all of them.
    total_projects = len(projects)
    projects = projects[:limit]

    # The decisions waiting on the operator, by the Approvals inbox's rule. `status = 'pending'`
    # left out delegated decisions and stuck pull requests, so this strip disagreed with the
    # v2 queue a click away (review round 3).
    gates = decision_actions.count_waiting_on_operator(connection, project_id)

    spend_micros = int(connection.execute(
        sql("SELECT COALESCE(sum(cost_usd_micros), 0) FROM llm_calls "
            "WHERE project_id = :p AND created_at > date_trunc('month', now())"),
        {"p": project_id}).scalar() or 0)

    return {
        "company": {"id": project_id, "name": project_id},
        "burn": {"spent_usd": round(spend_micros / 1_000_000, 2), "cap_usd": None},
        "gates": gates,
        "projects": projects,
        "total_projects": total_projects,
        "truncated": total_projects > len(projects),
        # Autonomy is a real setting in the artifact, not decoration. Reported honestly as the
        # only mode BOSS actually implements today rather than faking a switch that does
        # nothing — see CONSOLE_V2_MIGRATION.md §5.
        "autonomy": {"mode": "Supervised",
                     "detail": "Spend & deploys pause for you.",
                     "modes_available": ["Supervised"]},
    }


#: The quality checks the governed pipeline records, in the order v1 showed them. Each is
#: (label, how to read the verdict). Kept as data so the screen cannot drift from the pipeline.
_GATE_SPECS = (
    ("Code built / tests pass", "tests"),
    ("Code review (adversarial)", "adversarial"),
    ("Security clean", "security"),
    ("No regressions", "regressions"),
    ("Consistency", "consistency"),
    ("Pull request opened", "pr"),
)

#: Verdict strings that mean "this did not run", as opposed to "this ran and failed". The
#: difference matters more than it looks: a skipped check is an unknown, and colouring it red
#: claims evidence of a failure nobody gathered.
_NOT_RUN = {"", "skipped", "not_run", "none"}

#: Verdicts that count as a pass for the consistency check.
_CONSISTENT = {"consistent", "pass", "survived", "ok"}


def _gate_rows(state: dict, run: dict) -> list[dict[str, Any]]:
    """The quality checks, each as pass / fail / not-run with the evidence for it.

    Three states, never two. v1 drew a tick, a cross, or a dash, and the dash is the important
    one -- "we did not check" is not "it is fine", and a screen that renders it as either is
    inventing a result.
    """
    def row(label: str, ok: Optional[bool], skipped: bool, note: Optional[str]) -> dict:
        return {"label": label, "ok": None if skipped else ok,
                "skipped": skipped, "note": (note or None)}

    tests_passed = state.get("tests_passed")
    exit_code = state.get("test_exit_code")
    adversarial = str(state.get("adversarial_verdict") or "")
    security = str(state.get("security_verdict") or "")
    regressions = state.get("regressions_ok")
    consistency = str(state.get("consistency_verdict") or "")

    return [
        row("Code built / tests pass", bool(tests_passed),
            tests_passed is None and exit_code is None,
            f"exit {exit_code}" if exit_code is not None else None),
        row("Code review (adversarial)", adversarial == "survived",
            adversarial in _NOT_RUN, adversarial or None),
        row("Security clean", security == "clean", security in _NOT_RUN, security or None),
        row("No regressions", bool(regressions), regressions is None,
            "mutation caught" if state.get("mutation_caught") else None),
        row("Consistency", consistency in _CONSISTENT, consistency in _NOT_RUN,
            consistency or None),
        row("Pull request opened", bool(run.get("pull_request_url")),
            not run.get("pull_request_url") and str(run.get("status")) != "completed",
            f"PR #{run['pull_request_number']}" if run.get("pull_request_number") else None),
    ]


def _provenance(state: dict) -> dict[str, Any]:
    """Who built it and who reviewed it -- and whether those were the same model.

    A model grading its own work is the single fact that most changes how much a green review is
    worth, so it is computed here rather than left for the reader to notice.
    """
    build = state.get("build_engine")
    review = state.get("review_engine") or build
    return {
        "build_engine": build,
        "build_model": state.get("model"),
        "review_engine": review,
        "review_model": state.get("review_model"),
        "cross_model": bool(build and review and review != build),
        "reviewer_independent": bool(state.get("reviewer_independent")),
        "test_author_independent": bool(state.get("test_author_independent")),
        "profile": state.get("profile"),
        "risk_tier": state.get("risk_tier"),
    }


def _evidence(state: dict) -> list[dict[str, str]]:
    """The reviewers' own words, in the order v1 showed them.

    Returned as a list rather than a dict so the screen renders what exists in a fixed order and
    silently omits what does not, instead of drawing empty panels titled "Security review".
    """
    out = []
    for key, title in (
        ("review_summary", "Adversarial reviewer findings"),
        ("security_summary", "Security review"),
        ("consistency_summary", "Consistency review"),
        ("qa_summary", "QA / test-author summary"),
        ("impl_summary", "Implementation summary"),
        ("summary", "Agent summary"),
    ):
        text = state.get(key)
        if isinstance(text, str) and text.strip():
            clipped, cut = _clip(text)
            out.append({"key": key, "title": title, "text": clipped, "truncated": cut})
    return out


#: What a stopped attempt is waiting on, in a sentence. Keyed on the phase the run halted in.
#:
#: "blocked" on its own reads as a failure, and the operator read it that way. The commonest
#: case here is not a failure at all -- the pipeline finished planning and is waiting for a
#: human to approve the plan before it writes any code.
_WHY_STOPPED = {
    "awaiting_plan_approval":
        "It finished planning and stopped for a human to approve the plan before writing code. "
        "Nothing failed — it is waiting on you.",
    "queued": "It has not started yet; it is waiting for a free slot in the pipeline.",
    "provisioning": "It stopped while cloning the repository.",
    "design": "It stopped while designing the plan.",
    "implement": "It stopped part-way through writing the code.",
    "run_tests": "It stopped while running the tests.",
    "review_panel": "It stopped during adversarial review.",
    "check_tests": "It stopped during the mutation check.",
}

#: Human names for the pipeline phases, matching v1's vocabulary so the two consoles agree.
PHASE_LABELS = {
    "queued": "Queued", "provisioning": "Cloning repo", "design": "Designing plan",
    "awaiting_plan_approval": "Awaiting plan approval", "write_tests": "QA writing tests",
    "implement": "Engineer implementing", "run_tests": "Running tests",
    "review_panel": "Adversarial review", "check_tests": "Mutation check", "done": "Done",
    "coding": "Coding", "validating": "Testing", "opening_pr": "Opening PR",
}


def _narrative(run: dict, gates: list[dict], steps: list[dict]) -> dict[str, Any]:
    """What this attempt set out to do, and what became of it -- in sentences.

    Every clause is derived from a recorded field. The screen showed "blocked" and a list of
    stage names, which tells someone who already knows the pipeline what happened and tells
    everyone else nothing.
    """
    status = str(run.get("status") or "")
    phase = str(run.get("phase") or "")
    done = [s["stage"] for s in steps]
    # From finalize_ctx, because this is handed the raw row -- the computed `advisory` key is
    # added further down and reading it here silently got None, so every waived check was
    # described as unwaived.
    advisory = bool((run.get("finalize_ctx") or {}).get("advisory_failing_gates"))

    if status in _DONE_STATES:
        passed = [g for g in gates if g["ok"] is True]
        failed = [g for g in gates if g["ok"] is False]
        outcome = (
            f"Completed. It ran {len(done)} step{'' if len(done) == 1 else 's'}, "
            f"passed {len(passed)} of {len(passed) + len(failed)} quality checks"
        )
        if run.get("pull_request_number"):
            outcome += f", and opened PR #{run['pull_request_number']}"
        outcome += "."
        if failed:
            names = ", ".join(g["label"].lower() for g in failed)
            outcome += (
                f" {len(failed)} check did not pass ({names})"
                if len(failed) == 1 else
                f" {len(failed)} checks did not pass ({names})"
            )
            outcome += (
                " — graded advisory, so it did not block the PR."
                if advisory else
                " and it was not waived."
            )
        return {"outcome": outcome, "waiting": False,
                "phase_label": PHASE_LABELS.get(phase, phase)}

    if status in _FAILED_STATES:
        why = str(run.get("error") or "").strip()
        return {
            "outcome": (f"Failed during {PHASE_LABELS.get(phase, phase) or 'the run'}."
                        + (f" {why}" if why else "")),
            "waiting": False,
            "phase_label": PHASE_LABELS.get(phase, phase),
        }

    # Stopped, but not failed. This is the case the screen was describing worst.
    why = _WHY_STOPPED.get(phase)
    label = PHASE_LABELS.get(phase, phase)
    outcome = f"Stopped at “{label}”." if label else "Stopped."
    if done:
        outcome += f" It got through {', '.join(done)} first."
    return {
        "outcome": outcome,
        "why": why,
        # A gate is a decision waiting on a person, not a fault. Saying which it is decides
        # whether the reader goes looking for a bug or goes and approves something.
        "waiting": phase in _WAITING_PHASES,
        "phase_label": label,
    }


#: Phases where the pipeline is deliberately holding for a human.
_WAITING_PHASES = {"awaiting_plan_approval", "queued"}


def _step_agents(connection, project_id: str) -> dict[str, dict[str, Any]]:
    """Which agent owns each pipeline stage, and how that agent's work has actually gone.

    The mapping is STAGE_ROLES from llm_telemetry -- the SAME map BOSS uses at write time to
    attribute spend. Reusing it means the console and the cost ledger cannot disagree about who
    did what; inventing a second mapping here would guarantee they eventually would.

    NOT a confidence score. BOSS records no per-step confidence, so what is returned instead is
    an observation: how many of that role's calls succeeded, over how many, in the window. A
    role with three calls and a role with three hundred are different kinds of evidence, so the
    denominator travels with the number.
    """
    from .llm_telemetry import STAGE_ROLES, canonical_role

    stats: dict[str, dict] = {}
    for row in connection.execute(
        sql(
            """
            SELECT stage,
                   count(*) AS calls,
                   count(*) FILTER (WHERE ok) AS ok_calls
            FROM llm_calls
            WHERE project_id = :p
              AND created_at > now() - interval '30 days'
            GROUP BY stage
            """
        ),
        {"p": project_id},
    ).mappings().all():
        role = STAGE_ROLES.get(str(row["stage"] or "").strip().lower())
        if not role:
            continue
        agg = stats.setdefault(role, {"calls": 0, "ok": 0})
        agg["calls"] += int(row["calls"] or 0)
        agg["ok"] += int(row["ok_calls"] or 0)

    # `roles` is the registry -- the same table the Workforce screen reads. There is no
    # `agent_registry`; inventing one here would have failed closed and quietly marked every
    # role unregistered, which reads as a much bigger problem than the real one.
    registered = {
        str(r["role_key"]) for r in connection.execute(
            sql("SELECT role_key FROM roles WHERE project_id = :p"),
            {"p": project_id},
        ).mappings().all()
    }

    out: dict[str, dict[str, Any]] = {}
    for stage, raw_role in STAGE_ROLES.items():
        role = canonical_role(raw_role)
        agg = stats.get(role) or {"calls": 0, "ok": 0}
        out[stage] = {
            "role": role,
            # Whether the org actually has this agent. Two of these do NOT resolve on the live
            # deployment -- STAGE_ROLES attributes work to "architect" and "reviewer" while the
            # registry holds "chief_architect" and "adversarial_reviewer" -- so that spend can
            # never be matched to an agent. Reported rather than hidden.
            "registered": role in registered,
            "calls": agg["calls"],
            "ok_calls": agg["ok"],
            # None, not 100%, when nothing has run. A rate over zero calls is not a good score.
            "success_rate": (round(agg["ok"] / agg["calls"], 3) if agg["calls"] else None),
        }
    return out


def _deployment(connection, project_id: str, title: str) -> Optional[dict[str, Any]]:
    """Where this app is actually running, and how the last deploy went.

    The operator asked why the console could not load this when the app was plainly deployed --
    "is the state missing?". It was not missing. deployment_targets.domains held the address and
    deployment_runs held the outcome; v2 selected neither, so a running app looked unbound.

    Matched on app_name, which is what deployment_targets records. Returns None rather than a
    half-filled block when there is no target: "not deployed" and "deployed somewhere I cannot
    find" must not render alike.
    """
    if not title:
        return None
    target = connection.execute(
        sql(
            """
            SELECT id, app_name, environment, target_kind, target_ref, domains, status
            FROM deployment_targets
            WHERE project_id = :p AND app_name = :n
            ORDER BY updated_at DESC
            LIMIT 1
            """
        ),
        {"p": project_id, "n": title},
    ).mappings().first()
    if not target:
        return None

    last = connection.execute(
        sql(
            """
            SELECT status, commit_ref, started_at, finished_at, failure_reason
            FROM deployment_runs
            WHERE project_id = :p AND deployment_target_id = :t
            ORDER BY created_at DESC
            LIMIT 1
            """
        ),
        {"p": project_id, "t": target["id"]},
    ).mappings().first()

    domains = [str(d) for d in (target.get("domains") or []) if d]
    # The health check records a scheme; without one an address is not a link.
    url = f"http://{domains[0]}" if domains else None
    return {
        "target_id": target["id"],
        "environment": str(target.get("environment") or ""),
        "target_ref": str(target.get("target_ref") or ""),
        "status": str(target.get("status") or ""),
        "url": url,
        "domains": domains,
        "last_deploy": (
            {
                "status": str(last.get("status") or ""),
                "commit": (str(last["commit_ref"])[:10] if last.get("commit_ref") else None),
                "finished_at": (last["finished_at"].isoformat()
                                if last.get("finished_at") else None),
                "failure_reason": (str(last.get("failure_reason"))[:200]
                                   if last.get("failure_reason") else None),
            }
            if last else None
        ),
    }


def project_workspace(connection, project_id: str, app_id: str) -> dict[str, Any]:
    """One app: every run in its lineage, where it stands, and what can be done to it.

    ONE call, like company_overview. The v1 equivalent of this screen was assembled from the
    whole 82-feed page render, so opening one app cost the same as opening everything.

    The lineage is the unit, not the run. Judging an app by its newest run is what made BOSS
    refuse to deploy something whose completed build was sitting two rows down in the same
    lineage, so `deployable` is answered from the lineage and the reason is returned with it —
    a disabled button with no reason is what sends people to the API to find out why.
    """
    rows = [dict(r) for r in connection.execute(
        sql(
            """
            SELECT id, title, status, phase, spec, state, usage, finalize_ctx,
                   pull_request_url, pull_request_number, error, created_at, updated_at
            FROM code_task_runs
            WHERE project_id = :p
              AND created_at > now() - interval '180 days'
            ORDER BY created_at DESC
            LIMIT 400
            """
        ),
        {"p": project_id},
    ).mappings().all()]

    lineage = [r for r in rows if _lineage_key(r) == app_id]
    if not lineage:
        # NOT an empty app. An id that matches nothing and an app with no runs are different
        # facts, and rendering them alike sends the operator looking for work that was never
        # there.
        return {"found": False, "app_id": app_id, "project_id": project_id}

    lineage.sort(key=lambda r: r["created_at"], reverse=True)
    # Name the app the way the projects list names it, which means skipping internal runs.
    #
    # A follow-up task carries its own title ("Follow-up: deferred review findings"), and taking
    # simply the newest title let that RENAME the app it ran against: three different apps all
    # opened under one heading, and clicking "Invoice-Chaser" landed on a page called something
    # else. An app is not renamed by the last piece of housekeeping done to it.
    title = ""
    for r in lineage:
        if _is_internal(str(r.get("title") or "")):
            continue
        title = _clean_title(str(r.get("title") or ""))
        if title:
            break
    if not title:
        # Every run in this lineage is internal. Its own title beats "Untitled app".
        for r in lineage:
            title = _clean_title(str(r.get("title") or ""))
            if title:
                break

    stage, status = _stage_of(lineage)
    micros = sum(int((r.get("usage") or {}).get("cost_usd_micros") or 0) for r in lineage)
    spec = lineage[0].get("spec") or {}

    # Per-run detail: the steps, who ran them, and what each cost.
    #
    # A card that says "attempt 3 — failed" and nothing else is a dead end: the only move left
    # is to go and read logs. The operator asked for what v1 showed — latency, the steps, and
    # which agent made the change — and BOSS records all of it.
    run_ids = [r["id"] for r in lineage]
    agents = _step_agents(connection, project_id)
    calls = {}
    steps_by_run: dict[str, list] = {}
    if run_ids:
        for row in connection.execute(
            sql(
                """
                SELECT code_task_run_id AS run_id, stage, model, engine,
                       total_tokens, cost_usd_micros, latency_ms, turns, retries, ok, error
                FROM llm_calls
                WHERE code_task_run_id = ANY(:ids)
                ORDER BY code_task_run_id, created_at
                """
            ),
            {"ids": run_ids},
        ).mappings().all():
            steps_by_run.setdefault(row["run_id"], []).append({
                # The stage IS the step: spec, plan, implement, review_code, check_tests.
                "stage": str(row.get("stage") or "(unnamed)"),
                "model": row.get("model"),
                "engine": row.get("engine"),
                "tokens": int(row.get("total_tokens") or 0),
                "spend_usd": round(int(row.get("cost_usd_micros") or 0) / 1_000_000, 4),
                # Latency, which is what the operator asked for and what tells a slow model
                # apart from a slow pipeline.
                "latency_ms": int(row.get("latency_ms") or 0),
                # Per CALL, not summed per stage. An earlier version grouped by stage and so
                # discarded turns, retries and the error text -- and the error is the whole
                # reason to open a failed attempt. A stage that ran twice is also two facts.
                "turns": int(row.get("turns") or 0),
                "retries": int(row.get("retries") or 0),
                "ok": bool(row.get("ok")),
                "error": (str(row.get("error"))[:200] or None) if row.get("error") else None,
                # Which agent this step belongs to, so the work maps onto the org chart.
                "agent": agents.get(str(row.get("stage") or "").strip().lower()),
            })

    runs = [
        {
            "id": r["id"],
            "status": str(r.get("status") or ""),
            "phase": str(r.get("phase") or ""),
            "attempt": int((r.get("finalize_ctx") or {}).get("attempt") or 1),
            "turns": int((r.get("state") or {}).get("turns") or 0),
            "spend_usd": round(int((r.get("usage") or {}).get("cost_usd_micros") or 0) / 1e6, 4),
            "pull_request_number": r.get("pull_request_number"),
            "pull_request_url": r.get("pull_request_url"),
            # The failure text, because "failed" without it is a dead end on the card.
            "error": str(r.get("error") or "")[:400] or None,
            "created_at": r["created_at"].isoformat() if r.get("created_at") else None,
            # The steps this attempt actually ran, in order, each with its model and latency.
            "steps": steps_by_run.get(r["id"], []),
            "latency_ms": sum(s["latency_ms"] for s in steps_by_run.get(r["id"], [])),
            # The governed pipeline's own record of this attempt: what it checked, who checked
            # it, and what they wrote. v1 showed all of it; v2 was selecting the columns and
            # discarding them, so the screen said "attempt 1 - completed" and stopped.
            "gates": _gate_rows(r.get("state") or {}, r),
            "narrative": _narrative(
                r, _gate_rows(r.get("state") or {}, r), steps_by_run.get(r["id"], [])),
            "provenance": _provenance(r.get("state") or {}),
            "evidence": _evidence(r.get("state") or {}),
            "impl_files": list((r.get("state") or {}).get("impl_files") or [])[:60],
            "test_files": list((r.get("state") or {}).get("test_files") or [])[:60],
            # Phase durations, recorded by the pipeline as it goes.
            "phase_timings": [
                {"phase": str(t.get("phase") or ""), "ms": int(t.get("ms") or 0)}
                for t in ((r.get("finalize_ctx") or {}).get("phase_timings") or [])
                if isinstance(t, dict)
            ],
            # A first-pass build ships with failing checks marked advisory. Saying so is the
            # difference between "this passed" and "we let this through on purpose".
            "advisory": bool((r.get("finalize_ctx") or {}).get("advisory_failing_gates")),
        }
        for r in lineage
    ]

    shipped = [r for r in lineage if str(r.get("status")) in _DONE_STATES]
    reviewed = [r for r in shipped if r.get("pull_request_url")]

    return {
        "found": True,
        "app_id": app_id,
        "project_id": project_id,
        "title": title or "Untitled app",
        "description": str(spec.get("instructions") or "")[:400],
        "repo": f"{spec.get('owner') or '?'}/{spec.get('repo') or '?'}",
        "stage": stage,
        "status": status,
        "pct": STAGE_PCT.get(stage, 0),
        "spend_usd": round(micros / 1_000_000, 2),
        # Where it is running, if it is. Read from the same rows the v1 CI/CD screen reads.
        "deployment": _deployment(connection, project_id, title),
        # The contract this work was accepted against. Without it the quality checks are marks
        # with no exam paper -- you can see that something passed, not what it passed.
        "criteria": [str(c) for c in (spec.get("acceptance_criteria") or []) if c][:40],
        # `test_commands` is the key the pipeline actually writes. An earlier version read a
        # plausible-looking `verification` that no spec has ever contained, so the section would
        # have stayed empty forever and read as a task nobody verified.
        "verification": [str(c) for c in (spec.get("test_commands") or []) if c][:40],
        "runs": runs,
        # Say whether it can ship AND why not, together. A disabled Deploy with no reason is
        # what sends people to the API to find out.
        "deployable": bool(reviewed),
        "deployable_reason": (
            "" if reviewed
            else "nothing reviewed yet — a run must complete and open a PR before it can ship"
        ),
        "latest_run_id": lineage[0]["id"],
        "phases": [
            {"id": name, "reached": STAGES.index(name) <= STAGES.index(stage), "label": name.title()}
            for name in STAGES
        ],
    }


#: Where a decision that is still the operator's to make can be. Which of them really are is
#: decision_actions.attention, the rule the Approvals inbox and the Command Center use: this
#: list alone counted a revision_requested the proposer was working on as waiting on you.
_OPEN_DECISION = decision_actions.NEEDS_ACTION_CANDIDATE_STATUSES


def decision_queue(connection, project_id: str, *, limit: int = 60,
                   now: Optional[datetime] = None) -> dict[str, Any]:
    """Every gate waiting on a human, and what happens if nobody answers.

    THE DEADLINE IS THE POINT
    -------------------------
    A gate has a `deadline_at` and a `default_action`, which means an unanswered decision does
    NOT simply wait — it resolves itself, one way or the other, and the work either proceeds
    unreviewed or is dropped. A queue that shows the question but not that consequence is how a
    gate silently kills work: the operator reads a list of things they could get to later, not
    a list of things that are about to be decided for them.

    So every row says what will happen and when, and the ones with no default say THAT, because
    "waits for you indefinitely" and "proceeds in two hours" are opposite facts.
    """
    rows = [dict(r) for r in connection.execute(
        sql(
            """
            SELECT id, task_id, question, recommendation, options, risk_tier,
                   cost_summary, deadline_at, default_action, status,
                   delegated_to, created_at
            FROM decisions
            WHERE project_id = :p
              AND status = ANY(:open)
            ORDER BY
              -- Soonest deadline first, and a gate with no deadline never outranks one that
              -- is about to fire on its own.
              deadline_at ASC NULLS LAST,
              risk_tier DESC,
              created_at ASC
            """
        ),
        # The limit is applied after classifying, so rows that are not the operator's cannot
        # use up the page.
        {"p": project_id, "open": list(_OPEN_DECISION)},
    ).mappings().all()]
    rows = [r for r in rows if decision_actions.attention(r, now) == "needs_action"][:int(limit)]

    items = []
    for r in rows:
        described = decision_actions.describe(r, now)
        default = str(r.get("default_action") or "").strip()
        deadline = r.get("deadline_at")
        options = r.get("options") or []
        items.append({
            "id": r["id"],
            "task_id": r.get("task_id"),
            "question": str(r.get("question") or "")[:400],
            "recommendation": (r.get("recommendation") or {}).get("summary")
            or (r.get("recommendation") or {}).get("text") or "",
            "options": [
                str(o.get("label") or o.get("id") or o) if isinstance(o, dict) else str(o)
                for o in options
            ][:6],
            "risk_tier": int(r.get("risk_tier") or 1),
            "status": str(r.get("status") or ""),
            "delegated_to": r.get("delegated_to"),
            "deadline_at": deadline.isoformat() if deadline else None,
            "default_action": default or None,
            # Said in words, because a bare timestamp beside a bare verb does not read as a
            # consequence. This is the sentence the operator actually needs.
            "if_ignored": (
                f"{default} automatically" if default and deadline
                else f"{default} when it expires" if default
                else "waits for you — nothing happens on its own"
            ),
            "created_at": r["created_at"].isoformat() if r.get("created_at") else None,
            # The same contract the Approvals inbox draws from, so a refusal notice reads as a
            # notice here too and the row names the answers that work, not the raw options.
            "kind": described["kind"],
            "allowed_actions": described["allowed_actions"],
            "guidance": described["guidance"],
        })

    expiring = [i for i in items if i["deadline_at"] and i["default_action"]]
    return {
        "project_id": project_id,
        "items": items,
        "total": len(items),
        # Counted separately because it is a different kind of urgent: these are decisions that
        # will be made without you.
        "auto_resolving": len(expiring),
    }


#: A receipt whose effect never completed. These are the interesting ones: an effect recorded
#: as started and never resolved is either still going or was lost when something restarted.
_RECEIPT_OPEN = ("in_progress", "pending")
_RECEIPT_FAILED = ("failed", "error", "rejected")


def receipt_feed(connection, project_id: str, *, limit: int = 60,
                 hours: int = 24) -> dict[str, Any]:
    """What BOSS actually did, with the evidence attached.

    THE COUNT IS A REAL COUNT
    -------------------------
    A tile on the old console read "Verified today: 100" — which was the page size. It fetched
    the hundred most recent receipts and counted them, so it would have said 100 for a project
    with a million and for a project with exactly a hundred. Here the window totals come from
    aggregates over the whole window, computed separately from the page, so a number is either
    true or absent.

    AN UNFINISHED EFFECT IS NOT A SUCCESS
    -------------------------------------
    A receipt sits at `in_progress` until its effect resolves. If the API restarts mid-effect
    nothing moves it, so it stays there forever — which is exactly what "the API restarted
    while this deployment was running" leaves behind. Counting those as done would report a
    clean day for a day that lost work, so they are counted and shown separately.
    """
    rows = [dict(r) for r in connection.execute(
        sql(
            """
            SELECT id, effect_kind, effect_scope, status, provider_ref, result, error,
                   data_boundary, created_at, updated_at
            FROM receipts
            WHERE project_id = :p
              AND created_at > now() - make_interval(hours => :h)
            ORDER BY created_at DESC
            LIMIT :lim
            """
        ),
        {"p": project_id, "h": int(hours), "lim": int(limit)},
    ).mappings().all()]

    # Separate aggregate over the WHOLE window. `len(rows)` is the page, and reporting a page
    # size as a total is the defect this is written to avoid.
    totals = dict(connection.execute(
        sql(
            """
            SELECT
              count(*) AS total,
              count(*) FILTER (WHERE status = ANY(:open)) AS unfinished,
              count(*) FILTER (WHERE status = ANY(:bad)) AS failed
            FROM receipts
            WHERE project_id = :p
              AND created_at > now() - make_interval(hours => :h)
            """
        ),
        {"p": project_id, "h": int(hours),
         "open": list(_RECEIPT_OPEN), "bad": list(_RECEIPT_FAILED)},
    ).mappings().first() or {})

    items = []
    for r in rows:
        status = str(r.get("status") or "")
        err = r.get("error") or {}
        effect = str(r.get("effect_kind") or "")
        scope_raw = r.get("effect_scope") or {}
        family, routine = _effect_family(effect)
        items.append({
            "id": r["id"],
            "effect": effect,
            "scope": _scope_label(scope_raw),
            # What kind of thing this is, so the screen can colour it and the reader can filter.
            "family": family,
            # BOSS's own heartbeat. Kept in the ledger, but marked so it can be collapsed:
            # newest-first with no distinction buries the work under the ticks.
            "routine": routine,
            "summary": _effect_sentence(effect, scope_raw, status),
            # The run this belongs to, when the scope names one -- the link back to the work.
            "run_id": (str(scope_raw.get("code_task_run_id") or scope_raw.get("run_id"))
                       if (scope_raw.get("code_task_run_id") or scope_raw.get("run_id"))
                       else None),
            "status": status,
            "unfinished": status in _RECEIPT_OPEN,
            # The provider's own reference, which is what makes this evidence rather than a
            # claim: it can be checked against the system that did the work.
            "provider_ref": r.get("provider_ref"),
            "error": (
                str(err.get("message") or err.get("detail") or err)[:300]
                if err else None
            ),
            "data_boundary": str(r.get("data_boundary") or "internal"),
            "created_at": r["created_at"].isoformat() if r.get("created_at") else None,
        })

    return {
        "project_id": project_id,
        "window_hours": int(hours),
        "items": items,
        # Named so they cannot be mistaken for the page.
        "window_total": int(totals.get("total") or 0),
        "window_unfinished": int(totals.get("unfinished") or 0),
        "window_failed": int(totals.get("failed") or 0),
        "shown": len(items),
        # How much of the page is BOSS keeping itself alive. Stated, so a reader who sees
        # mostly ticks knows that is what this project's day looked like rather than assuming
        # the screen is broken.
        "shown_routine": sum(1 for i in items if i["routine"]),
    }


#: Scope keys that name the THING an effect acted on, best first.
_SUBJECT_KEYS = ("app_name", "name", "repo", "host", "target", "run_id", "code_task_run_id",
                 "deployment_target_id", "meeting_id", "decision_id", "id")

#: Scope keys that are true of every row on the screen and so tell the reader nothing.
_SCOPE_NOISE = {"project_id", "org_id", "workspace_id"}


def _scope_label(scope: dict) -> str:
    """What the effect acted on, in one short phrase.

    The earlier version fell back to the KEYS of the scope, so most rows read "project_id,
    window" or "action, minute, steward_id" -- the names of the fields rather than anything
    about what happened. On a screen whose whole job is letting an operator correlate an effect
    with the work that caused it, that is the field label where the value should be.
    """
    for key in _SUBJECT_KEYS:
        value = scope.get(key)
        if value:
            return str(value)[:60]
    # No named subject: show key=value for what is left, dropping the keys that are true of
    # every row anyway. Values, not field names.
    pairs = [f"{k}={scope[k]}" for k in sorted(scope)
             if k not in _SCOPE_NOISE and scope.get(k) not in (None, "", [], {})]
    return ", ".join(pairs)[:70] if pairs else ""


#: Which family an effect belongs to. Matched on the leading segment of the dotted name, then
#: on the whole name, so `code_task.spend_cap_failover` is CODE and `budget.llm_spend.settle`
#: is SPEND without either needing its own entry.
_EFFECT_FAMILIES = (
    ("code_task", "CODE"), ("github", "CODE"), ("pr", "CODE"),
    ("agent_process", "PROCESS"),
    ("deployment", "DEPLOY"), ("deploy", "DEPLOY"), ("preview", "DEPLOY"),
    ("budget", "SPEND"), ("spend", "SPEND"), ("entitlement", "SPEND"),
    ("decision", "GATE"), ("gate", "GATE"), ("approval", "GATE"),
    ("meeting", "PANEL"), ("panel", "PANEL"),
    ("discovery", "RESEARCH"), ("research", "RESEARCH"),
)

#: Effects BOSS runs on a timer to keep itself alive. They are real effects and belong in the
#: ledger, but they are not work anybody asked for.
_ROUTINE_PREFIXES = ("process_manager", "metatron", "ai_sre", "watchdog", "janitor")
_ROUTINE_SUFFIXES = ("tick", "watch", "overwatch", "sweep", "heartbeat", "poll")


def _effect_family(effect: str) -> tuple[str, bool]:
    """(family, is_routine) for an effect kind.

    Routine matters more than it sounds. Over 24h this project recorded 1,901 receipts, and of
    the 60 most recent, 46 were BOSS's own heartbeat -- ticks, watches and overwatches. Sorted
    newest-first with no distinction, the four receipts that recorded actual work were buried
    under them, which is precisely why the operator could not correlate anything on this screen.
    """
    name = (effect or "").strip().lower()
    head = name.split(".")[0]
    tail = name.split(".")[-1]
    routine = head in _ROUTINE_PREFIXES or tail in _ROUTINE_SUFFIXES
    for prefix, family in _EFFECT_FAMILIES:
        if head == prefix:
            return family, routine
    return ("SYSTEM" if routine else "OTHER"), routine


def _effect_sentence(effect: str, scope: dict, status: str) -> str:
    """One line saying what happened, in words rather than in a dotted identifier.

    `code_task.spend_cap_failover` is precise and unreadable; both halves matter, so the screen
    keeps the identifier AND says what it means.
    """
    name = (effect or "").strip().lower()
    subject = _scope_label(scope)
    verb = {
        "succeeded": "", "failed": " — and failed", "in_progress": " — still running",
    }.get(status, "")
    words = name.replace("_", " ").replace(".", " · ")
    return (f"{words}{f' on {subject}' if subject else ''}{verb}")[:160]


#: Keys a transcript turn may use. Checked in order; the first that holds text wins.
_TURN_SPEAKER = ("speaker", "role", "name", "agent")
_TURN_TEXT = ("message", "text", "content", "summary")


def _transcript_turns(raw: Any) -> list[dict[str, Any]]:
    """Per-speaker turns out of a transcript artifact.

    Returns [] rather than a placeholder when there is nothing to show. An invented turn would
    be indistinguishable from a real one, which on a screen about deliberation is the worst
    possible thing to get wrong.
    """
    if isinstance(raw, str):
        import json as _json

        try:
            raw = _json.loads(raw)
        except (TypeError, ValueError):
            return []
    if not isinstance(raw, dict):
        return []

    turns = raw.get("transcript") or raw.get("turns") or []
    if not isinstance(turns, list):
        return []

    out = []
    for turn in turns:
        if not isinstance(turn, dict):
            continue
        speaker = next((str(turn[k]) for k in _TURN_SPEAKER if turn.get(k)), "")
        text = next((str(turn[k]) for k in _TURN_TEXT if turn.get(k)), "")
        if not text:
            continue
        clipped, cut = _clip(text)
        out.append({
            "speaker": speaker or "(unattributed)",
            "phase": str(turn.get("phase") or ""),
            "text": clipped,
            "truncated": cut,
            # BOSS does not record a stance per turn. None, never a guess: inventing FOR or
            # AGAINST would put words in an agent's mouth on the record.
            "stance": turn.get("stance") if turn.get("stance") in ("FOR", "AGAINST") else None,
        })
    return out[:40]


#: The rounds a meeting's minutes record, in order. Each is {panelist: what they argued}.
_ROUNDS = (("round1_perspectives", "round 1"), ("round2_debate", "round 2"))

#: How much of one panelist's argument travels to the console. Long enough for a real argument;
#: bounded so twenty panels stay one response.
_TURN_LIMIT = 4000

#: Markdown markers that come in pairs. Longest first, so ``` is settled before `.
_PAIRED = ("```", "**", "`")


def _balance_markup(text: str) -> str:
    """Drop a markdown marker the cut left unclosed.

    Truncation counts characters; markdown pairs them. Cutting mid-emphasis leaves an opening **
    with no partner, and the renderer -- correctly -- shows it as two literal asterisks against a
    fragment of a word. Cutting back to the orphan removes the fragment as well, which is right:
    half of an unfinished bold phrase is noise, not information.
    """
    for marker in _PAIRED:
        if text.count(marker) % 2:
            text = text[: text.rfind(marker)]
    return text


def _clip(text: str, limit: int = _TURN_LIMIT) -> tuple[str, bool]:
    """Shorten to `limit`, on a boundary, without breaking the markup. Says whether it cut.

    The flag is the point. An argument silently truncated reads as an argument that finished,
    so the reader draws conclusions about reasoning that was never shown to them.
    """
    text = text.strip()
    if len(text) <= limit:
        return text, False
    head = text[:limit]
    # Prefer the largest structural boundary that does not throw away most of the budget.
    for sep in ("\n\n", "\n", ". ", " "):
        idx = head.rfind(sep)
        if idx >= limit // 2:
            head = head[:idx]
            break
    return _balance_markup(head).rstrip(), True


def _as_content(raw: Any) -> dict:
    if isinstance(raw, str):
        import json as _json

        try:
            raw = _json.loads(raw)
        except (TypeError, ValueError):
            return {}
    return raw if isinstance(raw, dict) else {}


def _minutes_turns(raw: Any) -> list[dict[str, Any]]:
    """Per-panelist argument, from the MINUTES.

    This is where a real BOSS meeting keeps its reasoning: two rounds, each a mapping of
    panelist to what they argued. Richer than the reference's single round, and it was sitting
    unread while the screen said "no transcript recorded".
    """
    content = _as_content(raw)
    out: list[dict[str, Any]] = []
    for key, phase in _ROUNDS:
        block = content.get(key)
        if not isinstance(block, dict):
            continue
        for speaker, text in block.items():
            if not isinstance(text, str) or not text.strip():
                continue
            clipped, cut = _clip(text)
            out.append({
                "speaker": str(speaker),
                "phase": phase,
                "text": clipped,
                "truncated": cut,
                # BOSS does not record a position per panelist. None, never inferred from the
                # words: a guessed vote on the record is worse than no vote.
                "stance": None,
            })
    return out[:40]


def _minutes_synthesis(raw: Any) -> Optional[str]:
    """What the panel agreed, in its own words."""
    content = _as_content(raw)
    for key in ("synthesis", "output_text", "summary"):
        value = content.get(key)
        if isinstance(value, str) and value.strip():
            return _clip(value)[0]
    return None


def panel_sessions(connection, project_id: str, *, limit: int = 20) -> dict[str, Any]:
    """The panels debating something right now, and what they concluded.

    THE ARTIFACT'S LIVE DELIBERATION, HONESTLY
    ------------------------------------------
    The reference streams a scripted debate — agents speaking in turn, votes tallying, a verdict
    the operator ratifies or vetoes. BOSS's panels are real and produce real minutes, but they
    are not streamed: a meeting runs, and its transcript and decisions land when it completes.

    So this shows what BOSS actually has — who is on the panel, whether it is still running,
    what it decided, and any recorded dissent — rather than animating a debate that is not
    being watched. A fake live feed would be the most convincing lie in the console.

    DISSENT IS SHOWN
    ----------------
    A panel that agreed and a panel that overruled an objection produce the same decision, and
    they are not the same event. BOSS records dissent_records and v1 never displayed them.
    """
    rows = [dict(r) for r in connection.execute(
        sql(
            """
            SELECT m.id, m.title, m.purpose, m.status, m.participants, m.decisions,
                   m.dissent_records, m.minutes_artifact_id, m.transcript_artifact_id,
                   m.blocked_state, m.completed_at, m.created_at,
                   t.content AS transcript,
                   mi.content AS minutes
            FROM meetings m
            -- The deliberation lives in artifacts.content, so it joins rather than needing a
            -- second call per session. One query, whatever the session count.
            --
            -- BOTH artifacts, because they hold different things and only one of them exists.
            -- Real meetings write MINUTES, which carry round1_perspectives, round2_debate and a
            -- synthesis; the transcript path is only taken by simulated meetings. Reading the
            -- transcript alone found nothing on twenty real sessions and reported "no
            -- transcript recorded" while the reasoning sat in the other artifact.
            LEFT JOIN artifacts t ON t.id = m.transcript_artifact_id
            LEFT JOIN artifacts mi ON mi.id = m.minutes_artifact_id
            WHERE m.project_id = :p
            ORDER BY m.created_at DESC
            LIMIT :lim
            """
        ),
        {"p": project_id, "lim": int(limit)},
    ).mappings().all()]

    sessions = []
    for row in rows:
        status = str(row.get("status") or "")
        decisions = row.get("decisions") or []
        dissent = row.get("dissent_records") or []
        blocked = row.get("blocked_state") or None
        sessions.append({
            "id": row["id"],
            # The panel framing comes off first (meeting_title): _clean_title alone is the
            # product-name cleaner and left the framing as the name of older framed rows. Then
            # the product-name decorations ("— Re-build PRD"), as on every other v2 card.
            "title": (lambda t: _clean_title(t) or t)(
                _meeting_title(row.get("title"), row.get("purpose"))),
            "purpose": str(row.get("purpose") or "")[:300],
            "status": status,
            "deliberating": status in ("scheduled", "running", "in_progress"),
            "panelists": [
                str(p.get("role") or p.get("name") or p) if isinstance(p, dict) else str(p)
                for p in (row.get("participants") or [])
            ][:12],
            "decisions": [
                str(d.get("summary") or d.get("text") or d) if isinstance(d, dict) else str(d)
                for d in decisions
            ][:8],
            # Never rolled into the decision count. A panel that agreed and a panel that
            # overruled an objection are different events with the same output.
            "dissent": [
                str(d.get("note") or d.get("summary") or d) if isinstance(d, dict) else str(d)
                for d in dissent
            ][:8],
            # A blocked panel is neither running nor concluded, and showing it as either loses
            # the fact that it needs someone.
            "blocked": bool(blocked),
            "blocked_reason": (
                str(blocked.get("reason") or blocked)[:200] if isinstance(blocked, dict)
                else (str(blocked)[:200] if blocked else None)
            ),
            "has_minutes": bool(row.get("minutes_artifact_id")),
            # WHAT WAS ACTUALLY SAID. The reference shows each panelist's argument; BOSS records
            # the same thing as {speaker, message, phase} turns, and the console has never
            # displayed them. A panel whose reasoning is invisible is indistinguishable from a
            # coin toss with minutes attached.
            "turns": (
                _transcript_turns(row.get("transcript"))
                or _minutes_turns(row.get("minutes"))
            ),
            # The agreed outcome, as the panel wrote it. Distinct from `decisions`, which is a
            # column that these meetings leave empty.
            "synthesis": _minutes_synthesis(row.get("minutes")),
            "completed_at": row["completed_at"].isoformat() if row.get("completed_at") else None,
            "created_at": row["created_at"].isoformat() if row.get("created_at") else None,
        })

    return {
        "project_id": project_id,
        "sessions": sessions,
        "deliberating": sum(1 for s in sessions if s["deliberating"]),
        "blocked": sum(1 for s in sessions if s["blocked"]),
        # Stated so the screen never animates a debate nobody is watching.
        "streamed": False,
        "stream_note": (
            "BOSS panels are not streamed. A meeting runs and its minutes land when it "
            "completes; this shows where each one has got to."
        ),
    }
