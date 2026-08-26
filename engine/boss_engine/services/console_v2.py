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
from typing import Any, Optional

from sqlalchemy import text as sql

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
    r"captain:|quality improve pass|goldeneye profile check|follow-up:\s*deferred review"
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
_MARKDOWN_HEADING = re.compile(r"^#{1,6}\s*(.+)$")

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
    heading = _MARKDOWN_HEADING.match(body.strip())
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
            LIMIT 400
            """
        ),
        {"p": project_id},
    ).mappings().all()]

    # Group into apps (lineages), newest first by most recent activity.
    apps: dict[str, list[dict]] = {}
    for row in rows:
        if _is_internal(str(row.get("title") or "")):
            continue
        apps.setdefault(_lineage_key(row), []).append(row)

    projects = []
    for key, runs in apps.items():
        runs.sort(key=lambda r: r["created_at"], reverse=True)
        title = ""
        for r in runs:                       # first run whose title survives cleaning
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
    projects = projects[:limit]

    gates = int(connection.execute(
        sql("SELECT count(*) FROM decisions WHERE project_id = :p AND status = 'pending'"),
        {"p": project_id}).scalar() or 0)

    spend_micros = int(connection.execute(
        sql("SELECT COALESCE(sum(cost_usd_micros), 0) FROM llm_calls "
            "WHERE project_id = :p AND created_at > date_trunc('month', now())"),
        {"p": project_id}).scalar() or 0)

    return {
        "company": {"id": project_id, "name": project_id},
        "burn": {"spent_usd": round(spend_micros / 1_000_000, 2), "cap_usd": None},
        "gates": gates,
        "projects": projects,
        # Autonomy is a real setting in the artifact, not decoration. Reported honestly as the
        # only mode BOSS actually implements today rather than faking a switch that does
        # nothing — see CONSOLE_V2_MIGRATION.md §5.
        "autonomy": {"mode": "Supervised",
                     "detail": "Spend & deploys pause for you.",
                     "modes_available": ["Supervised"]},
    }
