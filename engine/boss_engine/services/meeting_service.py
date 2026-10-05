"""Multi-agent PRD meeting — BOSS convenes a panel of role-agents that debate,
then synthesize, then draft, before the deliverable goes through the human gate.

Each "agent" is the same brain (whatever make_agent_chat_adapter returns — e.g. Claude on
the Max plan) wearing a different persona/system-prompt. BOSS orchestrates the
rounds, shares one research pass across the panel, records the full minutes, and
gates the final publish. This is BOSS running a company of AI departments.

Flow:
  A. Research   — one shared web-intel crawl of the target (all agents see the same facts)
  B. Round 1    — each role-agent contributes its perspective
  C. Round 2    — each role-agent critiques the others and refines
  D. Synthesis  — a facilitator reconciles into agreed requirements + open questions
  E. Draft      — the PRD is written from the synthesis
  F. Publish    — publish_document, deferred to a human Decision (the gate)
"""
from __future__ import annotations

import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import Settings
from ..db import begin
from .jobs import JOB_PRIORITY_INTERACTIVE
from .core import stable_dedupe_key, create_artifact, create_blocking_decision, emit_event, make_id
from .mcp_registry import (
    McpServerUnreviewed,
    call_tool_unattended,
    list_mcp_servers,
)
from .openai_adapter import estimate_cost_usd_micros
from .meeting_gist import GIST_INSTRUCTION, with_gists

# ---- what a meeting is ABOUT, for titles ------------------------------------
#: The panel brief `request_meeting` puts in front of the operator's request, and the research
#: block it appends after it. Defined here so the tool that writes them and the titles that
#: read past them cannot drift apart.
PANEL_BRIEF_MARKER = "--- WHAT THIS PANEL IS FOR ---"
REQUEST_MARKER = "--- WHAT YOU ARE BEING ASKED ABOUT ---"
RESEARCH_MARKER = "--- RESEARCH ALREADY DONE FOR THIS ---"


def directive_headline(directive: str, limit: int = 80) -> str:
    """The operator's request, cut for a title.

    A panel's directive is its whole prompt: the workflow brief comes first and research is
    appended after the request. Titles cut from the start of it read "PRD: --- WHAT THIS PANEL
    IS FOR --- You are the panel inside BOSS's..." on every catalogue run. The title is the
    request itself: the text between the request marker and the research block, first line,
    whitespace collapsed. A directive with no markers is its own request.
    """
    text_ = str(directive or "")
    if REQUEST_MARKER in text_:
        text_ = text_.split(REQUEST_MARKER, 1)[1]
    elif PANEL_BRIEF_MARKER in text_:
        # A brief with no request section is all brief; there is no request to title.
        text_ = ""
    text_ = text_.split(RESEARCH_MARKER, 1)[0]
    lines = [" ".join(line.split()) for line in text_.splitlines()]
    headline = next((line for line in lines if line), "")
    if not headline:
        return "Untitled request"
    if len(headline) <= limit:
        return headline
    return headline[:limit].rstrip() + "…"


# ---- the role-agent template catalog -----------------------------------------
# Default AI role agents a meeting can be staffed with. A meeting picks any subset
# (the facilitator is always added). This is the "template" of available roles.
ROLE_CATALOG: dict[str, dict] = {
    "product_manager": {
        "key": "product_manager", "title": "Product Manager",
        "lens": (
            "user value and outcomes. Frame the real problem, the target users and their top "
            "jobs-to-be-done, scope vs non-goals, prioritization, and measurable success metrics. "
            "Push back on anything vague or unmeasurable."
        ),
    },
    "engineer": {
        "key": "engineer", "title": "Engineer / Architect",
        "lens": (
            "technical feasibility and architecture. Call out the target's real tech constraints, "
            "integration approach, data/RAG needs, performance, operating cost, and maintainability "
            "for the team that will own it. Flag anything technically risky or vague."
        ),
    },
    "ux_designer": {
        "key": "ux_designer", "title": "UX Designer",
        "lens": (
            "the actual users and their experience. User flows, information architecture, content "
            "and tone, and accessibility (WCAG) and inclusive design for the real audience. Ground "
            "every point in who the users actually are."
        ),
    },
    "privacy_safety": {
        "key": "privacy_safety", "title": "Privacy & Safety",
        "lens": (
            "data privacy, safety and compliance. PII and minors (COPPA), consent, retention, abuse "
            "and misuse, required refusals and guardrails, and ethical risks. Be the conscience of "
            "the room — name the things that could harm users or the org."
        ),
    },
    "finance": {
        "key": "finance", "title": "Finance / FinOps",
        "lens": (
            "cost, budget and ROI. Operating and unit economics, the spend cap, build-vs-buy, and "
            "whether the value justifies the investment. Be the steward of the money."
        ),
    },
    "growth": {
        "key": "growth", "title": "Growth / Marketing",
        "lens": (
            "adoption, positioning and conversion. Who will actually use this, how they discover it, "
            "the key conversion actions, and how success is measured in reach/engagement."
        ),
    },
    "qa": {
        "key": "qa", "title": "QA / Test",
        "lens": (
            "quality, edge cases and acceptance criteria. How this will be verified, what can break, "
            "failure modes, and the concrete tests that must pass before launch."
        ),
    },
    "customer_advocate": {
        "key": "customer_advocate", "title": "Customer Advocate",
        "lens": (
            "the end customer's voice. Real frustrations, support burden, trust, and whether this "
            "genuinely helps the people it's for — not just the business."
        ),
    },
    "security": {
        "key": "security", "title": "Security",
        "lens": (
            "threat modeling and abuse. Authentication, secrets, injection/abuse vectors, data "
            "exposure, and the controls required before this can be trusted in production."
        ),
    },
    "operations": {
        "key": "operations", "title": "Operations / Deployment Manager",
        "lens": (
            "running this thing at 3am. Operability is designed here, not bolted on after "
            "launch: for EVERY feature in the spec, name the signal that shows it working and "
            "the signal that shows it broken — a feature with no signal is not finished. "
            "Cover: the metrics and their names, what a dashboard should show at a glance, "
            "health and readiness checks, the deployment strategy and how a bad release is "
            "rolled back, what the first diagnostic step is when it misbehaves, and who is "
            "paged. Be concrete: 'verifications_completed_total{result}' beats 'add "
            "monitoring'. Say plainly which failures would be INVISIBLE with what is proposed "
            "so far — those are the gaps that cost a night's sleep later."
        ),
    },
    "data": {
        "key": "data", "title": "Data / Analytics",
        "lens": (
            "measurement and evidence. What to instrument, the metrics that prove success, data "
            "quality, and how decisions will be validated post-launch."
        ),
    },
}
# OPERATIONS SITS IN THE DEFAULT PANEL.
#
# Operability was a thing the platform demanded of the BUILD — expose /metrics, these series —
# and never a thing the DESIGN discussed. So the spec described features and the operational
# requirements arrived, if at all, as a paragraph the builder was told to obey. The result is
# on a host right now: three shipped apps, no metrics, and dashboards for data nobody had
# thought about at design time.
#
# Companies usually hold the operations conversation after the features work. Holding it during
# design is the point: the metric that proves a feature works is a property OF that feature,
# and it is far cheaper to name while the feature is still being argued about.
DEFAULT_ROLE_KEYS = ["product_manager", "engineer", "ux_designer", "privacy_safety",
                     "operations"]
FACILITATOR_TITLE = "Facilitator / Chief of Staff"
MEETING_TYPES = ("prd_panel", "standup", "go_no_go")
DEFAULT_MEETING_TYPE = "prd_panel"
STANDUP_DEFAULT_DIRECTIVE = "Daily standup — blockers, commitments, and today's plan."
GO_NO_GO_DEFAULT_DIRECTIVE = "Go/no-go — ship, hold, or stop with recorded dissent."
MEETING_TYPE_ALIASES = {
    "go_nogo": "go_no_go",
    "gonogo": "go_no_go",
}
TYPED_DEFAULT_DIRECTIVES = {
    "standup": STANDUP_DEFAULT_DIRECTIVE,
    "go_no_go": GO_NO_GO_DEFAULT_DIRECTIVE,
}
TYPED_TITLE_PREFIX = {
    "standup": "Standup",
    "go_no_go": "Go/No-Go",
}
TYPED_PURPOSE = {
    "standup": "No-spend simulated daily standup",
    "go_no_go": "No-spend simulated go/no-go fixture",
}
# ROLE_CATALOG above is the BASELINE seed every org/project starts with; orgs can
# then add/edit/disable their own roles in the meeting_roles table.


# ---- DB-backed, per-project role catalog -------------------------------------
def seed_roles(connection, project_id: str) -> None:
    """Seed the baseline roles for a project (idempotent). Called on onboarding
    and lazily on first read so existing projects backfill automatically."""
    for i, (key, r) in enumerate(ROLE_CATALOG.items()):
        connection.execute(
            text(
                """
                INSERT INTO meeting_roles (id, project_id, role_key, title, lens, risk_tier,
                                           is_default, enabled, sort_order)
                VALUES (:id, :p, :key, :title, :lens, 1, :is_default, true, :sort)
                ON CONFLICT (project_id, role_key) DO NOTHING
                """
            ),
            {
                "id": make_id("mrole"), "p": project_id, "key": key,
                "title": r["title"], "lens": r["lens"],
                "is_default": key in DEFAULT_ROLE_KEYS, "sort": i,
            },
        )


def list_project_roles(connection, project_id: str) -> list[dict]:
    """All roles for a project (enabled + disabled), for the management UI and the
    new-meeting form. Lazily seeds the baseline if the project has none yet."""
    rows = connection.execute(
        text("SELECT role_key, title, lens, risk_tier, data_boundary, is_default, enabled, sort_order "
             "FROM meeting_roles WHERE project_id = :p ORDER BY sort_order, title"),
        {"p": project_id},
    ).mappings().all()
    if not rows:
        seed_roles(connection, project_id)
        rows = connection.execute(
            text("SELECT role_key, title, lens, risk_tier, data_boundary, is_default, enabled, sort_order "
                 "FROM meeting_roles WHERE project_id = :p ORDER BY sort_order, title"),
            {"p": project_id},
        ).mappings().all()
    return [
        {"key": r["role_key"], "title": r["title"], "lens": r["lens"],
         # The agent behind the seat, in the one namespace (agent_identity.resolve).
         "role_key": _seat_role_key(r["role_key"], r["title"]),
         "risk_tier": int(r["risk_tier"]), "data_boundary": r["data_boundary"],
         "is_default": bool(r["is_default"]), "enabled": bool(r["enabled"])}
        for r in rows
    ]


def _seat_role_key(key: str, title: str) -> Optional[str]:
    from .agent_identity import resolve

    return resolve(key) or resolve(title)


def load_role_map(connection, project_id: str) -> dict[str, dict]:
    """Map of ENABLED roles for a project, keyed by role_key — for resolving a
    meeting's selected roles into personas."""
    return {r["key"]: r for r in list_project_roles(connection, project_id) if r["enabled"]}


def upsert_meeting_role(connection, project_id, *, key, title, lens, risk_tier=1,
                        data_boundary="internal", is_default=False, enabled=True) -> dict:
    role_key = re.sub(r"[^a-z0-9_]+", "_", (key or title or "role").lower()).strip("_") or "role"
    connection.execute(
        text(
            """
            INSERT INTO meeting_roles (id, project_id, role_key, title, lens, risk_tier,
                                       data_boundary, is_default, enabled, sort_order)
            VALUES (:id, :p, :key, :title, :lens, :risk, :db, :is_default, :enabled, 200)
            ON CONFLICT (project_id, role_key) DO UPDATE SET
              title = EXCLUDED.title, lens = EXCLUDED.lens, risk_tier = EXCLUDED.risk_tier,
              data_boundary = EXCLUDED.data_boundary, is_default = EXCLUDED.is_default,
              enabled = EXCLUDED.enabled, updated_at = now()
            """
        ),
        {"id": make_id("mrole"), "p": project_id, "key": role_key, "title": title, "lens": lens,
         "risk": int(risk_tier), "db": data_boundary, "is_default": bool(is_default), "enabled": bool(enabled)},
    )
    return {"key": role_key, "title": title}


def set_meeting_role_enabled(connection, project_id, role_key, enabled) -> bool:
    r = connection.execute(
        text("UPDATE meeting_roles SET enabled = :e, updated_at = now() "
             "WHERE project_id = :p AND role_key = :k RETURNING role_key"),
        {"e": bool(enabled), "p": project_id, "k": role_key},
    ).mappings().first()
    return r is not None


def delete_meeting_role(connection, project_id, role_key) -> bool:
    r = connection.execute(
        text("DELETE FROM meeting_roles WHERE project_id = :p AND role_key = :k RETURNING role_key"),
        {"p": project_id, "k": role_key},
    ).mappings().first()
    return r is not None


def resolve_roles(role_keys: Optional[list[str]], role_map: Optional[dict] = None) -> list[dict]:
    """Resolve selected role keys → persona dicts. Uses the project's DB role_map
    when provided; falls back to the hardcoded baseline otherwise."""
    if role_map:
        keys = [k for k in (role_keys or []) if k in role_map]
        if not keys:
            keys = [k for k, v in role_map.items() if v.get("is_default")]
        if not keys:
            keys = list(role_map.keys())
        seen: set = set()
        out = []
        for k in keys:
            if k not in seen and role_map.get(k):
                seen.add(k)
                out.append(role_map[k])
        return out
    # baseline fallback (no DB context)
    keys = [k for k in (role_keys or []) if k in ROLE_CATALOG] or DEFAULT_ROLE_KEYS
    seen2: set = set()
    out2 = []
    for k in keys:
        if k not in seen2:
            seen2.add(k)
            out2.append(ROLE_CATALOG[k])
    return out2


def _persona_system(p: dict) -> str:
    # THE SEAT IS AN AGENT. `agent_voice` (agent_persona.seat_voices) is the agent behind the
    # seat in its own voice — its callsign, that it is an AI, how it speaks, who else is in the
    # room — so a position reads as Vera's, not as "the QA / Test" in general.
    voice = str(p.get("agent_voice") or "").strip()
    return (
        f"You are the {p['title']} in a cross-functional product meeting at the company BOSS runs. "
        f"Your lens: {p['lens']} "
        "Speak only from your role. Be concrete, cite specifics from the shared research, and keep "
        "it to tight bullet points. Do not write the whole PRD — contribute your part of the thinking."
        + (f"\n\n{voice}" if voice else "")
    )


def _gather_research(connection: Connection, settings: Settings, project_id: str, url: str) -> dict:
    """One shared web-intel crawl the whole panel reasons over."""
    if not url:
        return {"note": "no URL supplied; agents reason from the directive only", "pages": []}
    server = next(
        (s for s in list_mcp_servers(connection, project_id, enabled_only=True)
         if s["name"] == "web-intel"),
        None,
    )
    return _study_site(settings, url, server)


def _study_site(settings: Settings, url: str, server: Optional[dict]) -> dict:
    """Crawl the site via the web-intel MCP server (no DB connection held).

    THROUGH THE REGISTRY, NEVER AROUND IT. Round-3 review: this called the server with
    MCPClient directly, so a web-intel an operator had re-pointed at another host (which makes
    it unreviewed), or a server registered from chat under that name after the built-in was
    deleted, received every meeting's URL and wrote the reply into every persona prompt with
    nobody asked. mcp_registry.call_tool_unattended refuses an unreviewed server; the meeting
    still runs, from the directive alone, and the research note says why and what to do.
    """
    if not url:
        return {"note": "no URL supplied; agents reason from the directive only", "pages": []}
    if not server:
        return {"note": "web-intel MCP server not available", "pages": []}
    try:
        result = call_tool_unattended(settings, server, "study_site",
                                      {"url": url, "max_pages": 6}, timeout=120.0)
    except McpServerUnreviewed as refused:
        return {"note": f"research skipped: {refused} The panel reasons from the directive "
                        "only.", "pages": []}
    except Exception as exc:
        return {"note": f"research failed: {type(exc).__name__}: {exc}", "pages": []}
    # trim each page so the shared brief stays compact for every persona prompt
    result = result if isinstance(result, dict) else {}
    pages = []
    for pg in (result.get("pages") or [])[:6]:
        pages.append({"url": pg.get("url"), "title": pg.get("title"), "text": (pg.get("text") or "")[:1100]})
    return {"entry_url": url, "sitemap": result.get("sitemap_routes", []), "pages": pages}


def _research_blob(research: dict) -> str:
    if not research.get("pages"):
        return research.get("note", "(no research)")
    lines = [f"Sitemap routes: {', '.join(research.get('sitemap', [])[:30])}", ""]
    for pg in research["pages"]:
        lines.append(f"### {pg['title']} — {pg['url']}\n{pg['text']}")
    return "\n\n".join(lines)


# ---- prompt builders (shared by all phases) ----------------------------------
def _make_base(directive: str, constraints: dict, research: dict, url: str) -> str:
    return (
        f"DIRECTIVE: {directive}\n\n"
        f"CONSTRAINTS: {json.dumps(constraints or {})}\n\n"
        f"SHARED RESEARCH (from a live crawl of {url or 'n/a'}):\n{_research_blob(research or {})}\n"
    )


def _seat_label(p: dict) -> str:
    """"Vera (QA / Test)" when an agent with a callsign holds the seat, else the title."""
    callsign = str(p.get("callsign") or "").strip()
    return f"{callsign} ({p['title']})" if callsign else p["title"]


def _panel_blob(personas: list[dict], rnd: dict) -> str:
    return "\n\n".join(f"--- {_seat_label(p)} ---\n{(rnd or {}).get(p['title'], '')}"
                         for p in personas)


def _ask(adapter, system_prompt: str, user_prompt: str) -> tuple[str, int, Optional[str]]:
    _ref, text_out, usage = adapter.create_text_response(user_prompt, system_prompt=system_prompt)
    return text_out, estimate_cost_usd_micros(usage), usage.get("model")


def _ask_panel(adapter, personas: list[dict], make_prompt) -> tuple[dict, int, Optional[str]]:
    out: dict[str, str] = {}
    cost = 0
    model = None
    lock = threading.Lock()

    def one(p):
        nonlocal cost, model
        txt, c, m = _ask(adapter, _persona_system(p), make_prompt(p))
        with lock:
            cost += c
            model = m or model
        return p["title"], txt

    with ThreadPoolExecutor(max_workers=max(1, len(personas))) as pool:
        for title, txt in pool.map(one, personas):
            out[title] = txt
    return out, cost, model


# ============================================================================
# Durable per-phase state machine. A meeting advances ONE phase per call and
# persists it, so it survives a crash/restart and resumes from the last phase.
# Each phase enqueues the next `meeting.step` job atomically with its own save.
# ============================================================================
#: The markers a workflow puts around the panel's prompt. Defined HERE, beside the function that
#: reads them back out, so the writer (request_meeting in tools/builtin.py) and the reader
#: cannot describe two different shapes.
PANEL_PURPOSE_MARKER = "--- WHAT THIS PANEL IS FOR ---"
PANEL_ASK_MARKER = "--- WHAT YOU ARE BEING ASKED ABOUT ---"
PANEL_RESEARCH_MARKER = "--- RESEARCH ALREADY DONE FOR THIS ---"

_TITLE_PREFIX = "PRD meeting:"
_TITLE_MAX = 120


def meeting_subject(text_value: Optional[str]) -> str:
    """What a meeting is ABOUT, in one line — never the framing a workflow wrapped around it.

    The incident (boss-1, 2026-10-02): request_meeting prepends "--- WHAT THIS PANEL IS FOR ---
    You are the panel inside BOSS's ... workflow" to the directive, the directive became the
    title, and every workflow-convened meeting read "PRD meeting: --- WHAT THIS PANEL IS FOR ---"
    — four identical cards and four identical consensus options. New runs carry a real title
    (meeting_runs.title); this reads the subject back out of older rows and of any directive.
    """
    value = str(text_value or "").strip()
    if value.startswith(_TITLE_PREFIX):
        value = value[len(_TITLE_PREFIX):].strip()
    if PANEL_ASK_MARKER in value:
        value = value.split(PANEL_ASK_MARKER, 1)[1]
    elif value.startswith(PANEL_PURPOSE_MARKER):
        # A title cut before the ask: all that survived is the framing. Name the workflow it
        # came from, which is the one distinguishing fact left in it.
        rest = value[len(PANEL_PURPOSE_MARKER):]
        # The name is one token followed by whitespace; a token the cut ran into is not used.
        found = re.search(r"inside BOSS's\s+(\S+)\s", rest)
        value = f"panel for the {found.group(1)} workflow" if found else "workflow panel"
    if PANEL_RESEARCH_MARKER in value:
        value = value.split(PANEL_RESEARCH_MARKER, 1)[0]
    line = " ".join(value.split())
    if len(line) > _TITLE_MAX:
        line = line[: _TITLE_MAX - 1].rstrip() + "…"
    return line or "Untitled meeting"


def display_title(raw: Optional[str]) -> str:
    """A stored meeting title as the console shows it: a PRD meeting keeps its "PRD meeting:"
    prefix, and whatever follows is the subject, never the framing (see meeting_subject)."""
    value = str(raw or "").strip()
    if value.startswith(_TITLE_PREFIX):
        return f"{_TITLE_PREFIX} {meeting_subject(value)}"
    return meeting_subject(value)


def meeting_title(title: Optional[str], directive: Optional[str] = None) -> str:
    """THE title of a meeting or meeting run, for every reader that shows one to a person or an
    agent: the stored title cleaned by display_title, or the directive's subject when the row
    has no title (rows written before meeting_runs.title existed).

    One function because the fix for framed titles reached only some readers: the chat tool
    list_meetings still returned the raw directive and v2 sessions cleaned meetings.title with
    the product-name cleaner, so older rows still read "PRD meeting: --- WHAT THIS PANEL IS FOR
    ---" (review of milestone/console-b, 2026-10-02). test_every_meeting_title_reader enumerates
    the readers.
    """
    if str(title or "").strip():
        return display_title(title)
    return meeting_subject(directive)


#: Where a meeting's PRD stands, in the operator's words. ONE table, read by the Meetings cards
#: and anything else that labels a meeting, so two screens cannot describe the same PRD two ways.
PRD_STATE_LABELS: dict[str, str] = {
    "running": "panel in session",
    "failed": "panel failed",
    "no_prd": "no PRD drafted",
    "awaiting_approval": "awaiting approval",
    "revision_requested": "revision requested",
    "approved": "PRD approved",
    "published": "PRD published",
    "rejected": "PRD rejected",
    # decisions.expire_stale_decisions sets 'expired'; it is not still awaiting anyone.
    "expired": "approval expired",
    "pr_open": "PRD in a pull request",
}

#: Every status decisions.py writes, mapped explicitly. The fall-through to "awaiting approval"
#: labelled an EXPIRED decision as still awaiting approval (review of milestone/console-b,
#: 2026-10-02); test_every_decision_status_has_a_prd_state keeps this table complete.
DECISION_TO_PRD_STATE: dict[str, str] = {
    "created": "awaiting_approval",
    "pending": "awaiting_approval",
    "delegated": "awaiting_approval",
    "approved": "approved",
    "executed": "published",
    "completed": "published",
    "rejected": "rejected",
    "revision_requested": "revision_requested",
    "revise": "revision_requested",
    "expired": "expired",
}

_PRD_DECISION_QUESTION = "Approve publishing the PRD the team drafted"


def prd_state_of(connection: Connection, *, run_status: Optional[str], decision_id: Optional[str],
                 task_id: Optional[str], pull_request_url: Optional[str] = None) -> dict:
    """Where this meeting's PRD stands, from the DECISION that gates it — not from whether a pull
    request URL happens to be recorded.

    The incident (boss-1, 2026-10-02): every card without a PR URL said "awaiting approval",
    including meetings whose PRD fed projects shown as SHIPPED 100%. The PRD's state lives on the
    publish decision; a PR is one possible ending of it, not the only evidence it was approved.
    """
    def _out(state: str) -> dict:
        return {"prd_state": state, "prd_label": PRD_STATE_LABELS[state]}

    status = (run_status or "").lower()
    if status == "running":
        return _out("running")
    if status == "failed":
        return _out("failed")
    if pull_request_url:
        return _out("pr_open")
    row = None
    if decision_id:
        row = connection.execute(
            text("SELECT status FROM decisions WHERE id = :d"), {"d": decision_id},
        ).mappings().first()
    elif task_id:
        row = connection.execute(
            text("SELECT status FROM decisions WHERE task_id = :t AND question = :q "
                 "ORDER BY created_at DESC LIMIT 1"),
            {"t": task_id, "q": _PRD_DECISION_QUESTION},
        ).mappings().first()
    if row is None:
        return _out("no_prd")
    decision = str(row["status"] or "").lower()
    return _out(DECISION_TO_PRD_STATE.get(decision, "awaiting_approval"))


def _row_to_run(r) -> dict:
    def _j(v):
        if isinstance(v, str):
            try:
                return json.loads(v)
            except Exception:
                return {}
        return v
    return {
        "id": r["id"], "project_id": r["project_id"], "task_id": r["task_id"],
        "status": r["status"], "phase": r["phase"], "directive": r["directive"], "url": r["url"],
        "role_keys": _j(r["role_keys"]) or [], "constraints": _j(r["constraints"]) or {},
        "state": _j(r["state"]) or {}, "usage": _j(r["usage"]) or {}, "finalize_ctx": _j(r["finalize_ctx"]) or {},
        "meeting_id": r["meeting_id"], "minutes_artifact_id": r["minutes_artifact_id"], "decision_id": r["decision_id"],
        # The run's own title when it has one; the directive's subject for rows written before
        # the column existed. Never the raw directive — see meeting_subject.
        "title": meeting_title(r.get("title") if hasattr(r, "get") else None, r["directive"]),
    }


def load_meeting_run(connection: Connection, run_id: str) -> Optional[dict]:
    r = connection.execute(
        text("SELECT id, project_id, task_id, status, phase, directive, url, role_keys, constraints, "
             "state, usage, finalize_ctx, meeting_id, minutes_artifact_id, decision_id, title "
             "FROM meeting_runs WHERE id = :id"),
        {"id": run_id},
    ).mappings().first()
    return _row_to_run(r) if r else None


def normalize_meeting_type(value: Optional[str]) -> str:
    meeting_type = (value or DEFAULT_MEETING_TYPE).strip().lower() or DEFAULT_MEETING_TYPE
    meeting_type = meeting_type.replace("-", "_").replace("/", "_")
    meeting_type = MEETING_TYPE_ALIASES.get(meeting_type, meeting_type)
    if meeting_type not in MEETING_TYPES:
        raise ValueError(f"unsupported meeting_type {meeting_type!r}; expected one of {MEETING_TYPES}")
    return meeting_type


def meeting_type_of(run_or_constraints) -> str:
    """Read persisted type from a run dict or constraints JSON. Defaults to prd_panel."""
    if isinstance(run_or_constraints, dict) and "constraints" in run_or_constraints:
        constraints = run_or_constraints.get("constraints") or {}
    else:
        constraints = run_or_constraints or {}
    if not isinstance(constraints, dict):
        return DEFAULT_MEETING_TYPE
    try:
        return normalize_meeting_type(constraints.get("meeting_type"))
    except ValueError:
        return DEFAULT_MEETING_TYPE


def meeting_fingerprint(project_id: str, directive: str, url: str = "", meeting_type: str = DEFAULT_MEETING_TYPE) -> str:
    """A stable identity for "this meeting, about this thing, in this project".

    Whitespace-normalised: a directive pasted twice differs by a trailing newline and means
    the same meeting. Non-PRD types are part of the identity so a standup and a PRD panel on
    the same subject can coexist, while two running standups cannot.
    """
    import hashlib

    parts = [project_id, " ".join((directive or "").split()), (url or "").strip()]
    normalized_type = normalize_meeting_type(meeting_type)
    if normalized_type != DEFAULT_MEETING_TYPE:
        parts.append(normalized_type)
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:32]


def active_duplicate_meeting(connection, project_id: str, fingerprint: str):
    """A meeting on the same subject that is STILL RUNNING, if there is one."""
    row = connection.execute(
        text("SELECT id, task_id FROM meeting_runs "
             "WHERE project_id = :p AND status = 'running' "
             "  AND finalize_ctx->>'meeting_fingerprint' = :fp "
             "ORDER BY created_at ASC LIMIT 1"),
        {"p": project_id, "fp": fingerprint}).mappings().first()
    return (row["id"], row["task_id"]) if row else (None, None)


#: Match kinds that make a run THIS benchmark's certified artifact.
#:
#: A name or subject match means "related work exists", not "the canary was run here": its
#: /report 404s, because nothing recorded it as a benchmark. Both the catalog badge and the
#: rebuild guard have to agree on that, and they did not -- the catalog treated a name match as
#: related while the guard treated it as already-built, so a card offered "Launch ApproveIt"
#: and then refused the press. A guard an operator can only discover by pressing is the defect
#: this whole area exists to remove.
REPORTABLE_MATCHES = frozenset({"benchmark_id", "launched_as_benchmark"})


def is_reportable(build: Optional[dict]) -> bool:
    """Is this build the benchmark's own artifact, rather than merely related work?"""
    return bool(build) and build.get("matched_on") in REPORTABLE_MATCHES


def completed_benchmark_build(connection, project_id: str, benchmark_id: str,
                              fingerprint: Optional[str] = None,
                              name: Optional[str] = None):
    """The finished build of THIS benchmark in this project, however it was started.

    Keyed on what the work IS, not on how it was launched. The first version of this asked
    "is there a completed meeting whose directive fingerprint matches", which recognised only
    benchmarks convened through the panel with the exact canonical wording. BugBox matched;
    ApproveIt did not, because it had been built through the older direct route and through
    meetings carrying an operator's own phrasing. The mechanism was general and its key was
    not, which is the same thing as not being general.

    Both routes already record the benchmark's identity, so both are asked:
      direct   code_task_runs.finalize_ctx -> qa_benchmark -> id
      panel    meeting_runs.constraints -> benchmark_id, joined by task
      subject  the meeting's directive fingerprint, for work convened on the same subject
               without the benchmark label
      name     the app's name in the run title, for work built from a hand-written PRD

    Four keys because each recognises a route the others miss. ApproveIt was built through a
    PRD meeting typed in the Meeting Room, so it carries no benchmark id, no launch-endpoint
    constraint and no canonical fingerprint -- nothing in that work records "this is
    ApproveIt" except its name. Keying only on the labels BOSS happens to stamp meant the card
    said "Launch ApproveIt" over an ApproveIt that plainly existed.

    The name key is deliberately last and deliberately weaker. Being wrong costs an operator a
    "View" button pointing at a similarly-named app, with "Build it again" beside it -- unlike
    the identity questions elsewhere in this codebase, where guessing once deployed the wrong
    application to a real host. `matched_on` records which key fired so the answer can be
    doubted where it should be.

    Three keys rather than one because each recognises a route the others miss, and a card
    that says "Launch" over an app that already exists is the failure being fixed.

    Returns the newest COMPLETED run and its live preview, or None. A failed run is not work
    that exists: reporting one made a card offer to show a build that had died.
    """
    row = connection.execute(
        text("""
            SELECT r.id, r.status, r.pull_request_url, r.title, r.task_id,
                   CASE
                     WHEN r.finalize_ctx->'qa_benchmark'->>'id' = :b THEN 'benchmark_id'
                     WHEN m.constraints->>'benchmark_id' = :b THEN 'launched_as_benchmark'
                     WHEN CAST(:fp AS TEXT) IS NOT NULL
                          AND m.finalize_ctx->>'meeting_fingerprint' = CAST(:fp AS TEXT)
                       THEN 'same_subject'
                     ELSE 'name'
                   END AS matched_on,
                   p.id AS preview_id, p.url AS preview_url, p.status AS preview_status
            FROM code_task_runs r
            LEFT JOIN meeting_runs m ON m.task_id = r.task_id
            LEFT JOIN LATERAL (
                SELECT id, url, status FROM app_previews
                WHERE run_id = r.id AND status IN ('running', 'starting')
                ORDER BY created_at DESC LIMIT 1
            ) p ON TRUE
            WHERE r.project_id = :p
              AND r.status = 'completed'
              AND (r.finalize_ctx->'qa_benchmark'->>'id' = :b
                   OR m.constraints->>'benchmark_id' = :b
                   OR (CAST(:fp AS TEXT) IS NOT NULL
                       AND m.finalize_ctx->>'meeting_fingerprint' = CAST(:fp AS TEXT))
                   OR (CAST(:nm AS TEXT) IS NOT NULL
                       AND r.title ILIKE CAST(:nm_like AS TEXT)))
            -- A RUNNING PREVIEW first, then the most recent build.
            --
            -- This ranked by how well-labelled a run was, which sounds principled and picked
            -- the wrong app: the old `QA benchmark: ApproveIt` carries a benchmark id, so it
            -- outranked the newer PRD-driven ApproveIt that the operator actually wanted and
            -- that had a live preview. Key strength answers "is this the same app"; it says
            -- nothing about WHICH build to open, and using it for both meant "View" opened a
            -- superseded one.
            ORDER BY (p.id IS NOT NULL) DESC, r.created_at DESC
            LIMIT 1"""),
        {"p": project_id, "b": benchmark_id, "fp": fingerprint,
         "nm": name, "nm_like": f"%{name}%" if name else None}).mappings().first()
    if row is None:
        return None
    return {
        "run_id": row["id"],
        "run_status": row["status"],
        "matched_on": row["matched_on"],
        "pull_request_url": row["pull_request_url"],
        "title": row["title"],
        "task_id": row["task_id"],
        "preview_id": row["preview_id"],
        # Never hand out app_previews.url (loopback). Operators open the gateway path.
        "preview_url": (
            f"/v1/previews/{row['preview_id']}/web/"
            if row["preview_id"]
            else None
        ),
        "preview_status": row["preview_status"],
    }


def start_meeting_run(connection, *, project_id, task_id, directive, url, constraints, role_keys,
                      finalize_ctx, title: Optional[str] = None) -> str:
    run_id = make_id("mrun")
    title = meeting_subject(title or directive)
    personas = resolve_roles(role_keys, load_role_map(connection, project_id))
    constraints = dict(constraints or {})
    constraints.setdefault("meeting_type", DEFAULT_MEETING_TYPE)
    constraints["meeting_type"] = normalize_meeting_type(constraints.get("meeting_type"))
    connection.execute(
        text("""
            INSERT INTO meeting_runs (id, project_id, task_id, status, phase, directive, url,
                                      role_keys, constraints, finalize_ctx, title)
            VALUES (:id, :p, :t, 'running', 'research', :d, :url,
                    CAST(:rk AS JSONB), CAST(:c AS JSONB), CAST(:fc AS JSONB), :title)
        """),
        {"id": run_id, "p": project_id, "t": task_id, "d": directive, "url": url, "title": title,
         "rk": json.dumps([p["key"] for p in personas]),
         "c": json.dumps(constraints or {}), "fc": json.dumps(finalize_ctx or {})},
    )
    emit_event(connection, project_id, "meeting.started", "boss", "meeting_run", run_id,
               {"task_id": task_id, "participants": [p["title"] for p in personas]})
    _sign_seats(connection, run_id)
    return run_id


def _sign_seats(connection, run_id: str) -> None:
    """Each seat is signed work (agent_acts): seated when the panel opens, closed when it ends.

    Never waits on the panel's lock: a tick holding it would hold it to the end of its own long
    transaction, and this panel's phase would stall behind it. Skipped, the holder signs it."""
    from . import agent_acts

    agent_acts.quietly(connection, "sign a panel's seats", agent_acts.sync_meeting, run_id,
                       wait=False)


#: The blocks request_meeting wraps around a directive so every seat reads them — the SAME
#: objects as the PANEL_*_MARKER constants above. The console-a and console-b streams each
#: defined these three strings for one incident ("PRD meeting: --- WHAT THIS PANEL IS FOR ---");
#: at integration one definition is kept (the MARKER names, which request_meeting and
#: meeting_subject use) and these names point at it, so directive_subject() and the console's
#: lib/directive-subject.ts mirror cannot drift from the builder.
PANEL_PURPOSE_MARK = PANEL_PURPOSE_MARKER
PANEL_ASK_MARK = PANEL_ASK_MARKER
PANEL_RESEARCH_MARK = PANEL_RESEARCH_MARKER


def directive_subject(directive: str) -> str:
    """What the operator asked for, without the prompt scaffolding around it.

    The scaffolding is for the panel; it is not a name. A workflow's panel step wrapped the ask
    in "--- WHAT THIS PANEL IS FOR --- You are the panel inside BOSS's … workflow", and the
    task was titled from the first 90 characters — so on boss-1 the task read "PRD meeting:
    --- WHAT THIS PANEL IS FOR --- You are the pane…" and the Command Center, the Factory's
    panel cards and the Approvals evidence chip all showed that instead of what was asked.
    """
    text_in = str(directive or "")
    if PANEL_ASK_MARK in text_in:
        text_in = text_in.split(PANEL_ASK_MARK, 1)[1]
    elif text_in.lstrip().startswith(PANEL_PURPOSE_MARK):
        # A purpose block with no ask marker after it: nothing but scaffolding to show.
        text_in = ""
    if PANEL_RESEARCH_MARK in text_in:
        text_in = text_in.split(PANEL_RESEARCH_MARK, 1)[0]
    return " ".join(text_in.split())


def convene_prd_meeting_core(
    connection, *, settings: Settings, budget, intake, jobs,
    project_id: str, directive: str, url: str = "",
    constraints: Optional[dict] = None, role_keys: Optional[list] = None,
    task_id: Optional[str] = None, title: Optional[str] = None,
    origin: Optional[dict] = None, brief: Optional[str] = None,
) -> dict:
    """Shared core for convening a durable multi-agent PRD meeting: create the
    task (if needed), reserve budget, start the run, enqueue the first step.
    Used by the /v1/meetings/prd endpoint and the Plane ``boss:design`` intake so
    both routes are governed identically.

    `origin` is who convened it — {act_id} for a chat turn, {workflow_run_id, step_run_id}
    for a workflow step, and the accountable {actor} — so its seats are signed under that
    work (agent_acts.sync_meeting), never under a made-up person."""
    constraints = dict(constraints or {})
    constraints.setdefault("meeting_type", DEFAULT_MEETING_TYPE)
    constraints["meeting_type"] = normalize_meeting_type(constraints.get("meeting_type"))
    # WHAT THE PERSON ASKED, kept apart from the prompt BOSS built around it (meeting_brief):
    # the console shows this, never the scaffolding.
    if str(brief or "").strip():
        constraints["brief"] = str(brief).strip()
    else:
        from .meeting_brief import split_brief
        constraints.setdefault("brief", split_brief(directive)["brief"])
    # WHAT THE MEETING IS CALLED is not its prompt. A caller that wrapped the directive in
    # framing passes the bare ask as `title`; anyone else gets the directive's subject.
    title = meeting_subject(title or directive)

    # Refuse to convene a meeting that is ALREADY being held.
    #
    # An operator pressed a button that gave no feedback and got six identical PRD meetings —
    # three of them inside the same second. Each ran the full agent panel and produced its own
    # 12k-word PRD, leaving six approvals in the inbox and no way to tell which to approve.
    # A UI guard alone cannot fix that: by the time the button re-renders, the other clicks are
    # already in flight. The check belongs here, where every route converges — the endpoint and
    # the Plane intake both call this.
    #
    # Idempotent, not an error: the caller wanted a meeting on this subject and there is one.
    fingerprint = meeting_fingerprint(
        project_id, directive, url, meeting_type=constraints["meeting_type"]
    )
    if not constraints.get("allow_duplicate"):
        existing_run, existing_task = active_duplicate_meeting(connection, project_id, fingerprint)
        if existing_run:
            emit_event(connection, project_id, "meeting.duplicate_suppressed", "boss",
                       "meeting_run", existing_run,
                       {"directive": directive[:200], "fingerprint": fingerprint})
            return {"task_id": existing_task, "run_id": existing_run, "job_id": None,
                    "duplicate_of": existing_run}

    if task_id is None:
        # Same directive, same project, same meeting. `make_id()` here meant a retried or
        # double-submitted convene spent for a second identical panel.
        dedupe_key = stable_dedupe_key("prd-meeting", project_id, directive)
        # ADOPT THE TASK, DO NOT COLLIDE WITH IT.
        #
        # The dedupe key outlives the meeting it was made for. `active_duplicate_meeting`
        # above catches a panel that is still going and returns it — right — but once that
        # run is CANCELLED there is no active duplicate, while the task row it created still
        # holds the key. So the next run on the same directive got:
        #
        #   ValueError: Task with dedupe_key already exists in this project: prd-meeting:…
        #
        # and deadlettered its panel step. "Stop and run again" walks into this every time,
        # because stopping and re-running the same directive is exactly the case: the whole
        # point of the key is that this IS the same meeting, so the run should take the task
        # over rather than fail for the reason the key exists to express.
        existing_task = intake.task_by_dedupe_key(connection, project_id, dedupe_key)
        if existing_task:
            task_id = existing_task
        else:
            task = intake.create_task(
                connection,
                project_id=project_id,
                title=f"PRD meeting: {title}",
                description=directive,
                dedupe_key=dedupe_key,
                priority="p1",
                risk_tier=2,
                inputs={"directive": directive, "url": url, "constraints": constraints},
                required_artifacts=["meeting_minutes"],
                data_boundary=str(constraints.get("data_boundary") or "internal"),
            )
            task_id = task["task_id"]
    reservation = budget.reserve(
        connection, project_id, "model",
        settings.plan_budget_usd_micros * 4,
        "Multi-agent PRD meeting.", 120,
    )
    reservation_id = (
        reservation.get("reservation_id")
        if reservation.get("status") == "reserved"
        else None
    )
    run_id = start_meeting_run(
        connection, project_id=project_id, task_id=task_id,
        directive=directive, url=url, constraints=constraints,
        role_keys=role_keys or [],
        finalize_ctx={"reservation_id": reservation_id, "meeting_fingerprint": fingerprint,
                      **({"origin": dict(origin)} if origin else {})},
        title=title,
    )
    job = jobs.create_job(
        connection, project_id=project_id, task_id=task_id,
        kind="meeting.step", priority=JOB_PRIORITY_INTERACTIVE, payload={"run_id": run_id},
    )
    return {"task_id": task_id, "run_id": run_id, "job_id": job["id"]}


def _save_phase(connection, run_id, state, add_cost, model, next_phase) -> None:
    connection.execute(
        text("""
            UPDATE meeting_runs SET
              state = CAST(:state AS JSONB), phase = :phase,
              usage = jsonb_build_object(
                        'cost_usd_micros', COALESCE((usage->>'cost_usd_micros')::bigint, 0) + CAST(:addc AS bigint),
                        'model', CAST(:model AS text)),
              updated_at = now()
            WHERE id = :id
        """),
        {"state": json.dumps(state), "phase": next_phase, "addc": int(add_cost or 0),
         "model": model, "id": run_id},
    )
    _sign_seats(connection, run_id)


def advance_meeting_run(engine, settings: Settings, jobs, run_id: str) -> dict:
    """Execute exactly ONE phase, persist it, and atomically enqueue the next step.
    Crash-safe: a crash before the persist re-runs only the current phase."""
    with begin(engine) as c:
        run = load_meeting_run(c, run_id)
    if run is None:
        return {"status": "not_found", "done": True}
    if run["status"] != "running":
        return {"status": run["status"], "done": True, "phase": run["phase"]}

    phase = run["phase"]
    if phase in ("finalize", "done"):
        with begin(engine) as c:
            res = _finalize_meeting(c, settings, run_id)
        return {"status": "completed", "done": True, **res}

    if _is_simulated_run(run):
        with begin(engine) as c:
            return _advance_simulated_phase(c, settings, jobs, run)

    with begin(engine) as c:
        personas = resolve_roles(run["role_keys"], load_role_map(c, run["project_id"]))
        # Each seat in the voice of the agent behind it (agent_persona.seat_voices).
        from .agent_persona import seat_voices

        personas = seat_voices(c, run["project_id"], personas)
    state = dict(run["state"])
    # THE SETTING SAYS "AND MEETINGS", AND THIS LINE WAS NOT READING IT.
    #
    # `chat_llm_engine` is labelled "Console chat, agents and meetings" and its help text
    # promises it "runs the agent panels and speaks in meetings". Only
    # `make_agent_chat_adapter` reads it. `make_chat_adapter` reads `llm_engine`, a DIFFERENT
    # knob that defaults to "auto" and falls through to the OpenAI adapter.
    #
    # So an operator who set this to `codex` — deliberately, in the box that names meetings —
    # watched every panel die with "OPENAI_API_KEY is not configured", and the obvious
    # remedy (set an OpenAI key) would have run their meetings on the provider they had just
    # chosen not to use.
    #
    # The comment above `chat_llm_engine` describes this exact failure for the chat: "an
    # operator who sets one lock believes they have moved everything." It was true of meetings
    # too. `make_agent_chat_adapter` falls back to `make_chat_adapter` when the setting is
    # empty, so an install that never set it is unaffected.
    # AND THE PROJECT'S CHOICE HAS TO REACH IT. The line above was right and still failed,
    # because `settings` here is the DEPLOYMENT's: `chat_llm_engine` was set to `codex` in the
    # console, on this project, and `make_agent_chat_adapter` never saw it. So the panel fell
    # through to the OpenAI adapter and every meeting died with
    # "OPENAI_API_KEY is not configured" — the exact failure the comment above describes,
    # arriving through the one path nobody had layered the project onto.
    #
    # The resolution is model_routing's, the one every model call uses: the project's choice,
    # then the deployment's, then a subscription or stored key that is actually connected —
    # built on that source's own credential. This was the sixth pasted copy of it.
    from . import model_routing

    with begin(engine) as c:
        adapter = model_routing.project_chat_adapter(c, settings, run["project_id"])
    add_cost = 0
    model = (run["usage"] or {}).get("model")

    if phase == "research":
        # Look up the web-intel server in a quick txn, then crawl WITHOUT holding a
        # DB connection during the (slow) Playwright render.
        with begin(engine) as c:
            server = next((s for s in list_mcp_servers(c, run["project_id"], enabled_only=True)
                           if s["name"] == "web-intel"), None)
        state["research"] = _study_site(settings, run["url"], server)
        next_phase = "round1"
    elif phase == "round1":
        base = _make_base(run["directive"], run["constraints"], state.get("research") or {}, run["url"])
        r1, add_cost, model = _ask_panel(adapter, personas,
            lambda p: base + f"\nAs the {p['title']}, give your initial perspective on building this. "
                             "List your top priorities, requirements, and concerns from your lens."
                             + GIST_INSTRUCTION)
        # EACH TURN CARRIES ITS GIST: the seat's own headline and key points, taken off the
        # reply and stored beside it, so the meeting room never cuts a card out of markdown.
        state["round1"], state["round1_gists"] = with_gists(r1)
        next_phase = "round2"
    elif phase == "round2":
        base = _make_base(run["directive"], run["constraints"], state.get("research") or {}, run["url"])
        r1_blob = _panel_blob(personas, state.get("round1") or {})
        r2, add_cost, model = _ask_panel(adapter, personas,
            lambda p: base + f"\nHere is what the whole panel said in round 1:\n\n{r1_blob}\n\n"
                             f"As the {p['title']}, respond: where do you AGREE, where do you DISAGREE or see "
                             "risk in your colleagues' points, and what is your refined position? Be specific."
                             + GIST_INSTRUCTION)
        state["round2"], state["round2_gists"] = with_gists(r2)
        next_phase = "synthesis"
    elif phase == "synthesis":
        base = _make_base(run["directive"], run["constraints"], state.get("research") or {}, run["url"])
        r2_blob = _panel_blob(personas, state.get("round2") or {})
        fac_sys = (f"You are the {FACILITATOR_TITLE}. You ran a cross-functional meeting and must reconcile "
                   "the team's debate into a single agreed direction. Be decisive, resolve conflicts explicitly, "
                   "and surface genuine open questions rather than papering over them.")
        synth, add_cost, model = _ask(adapter, fac_sys,
            base + f"\nThe panel's refined positions after debate:\n\n{r2_blob}\n\n"
                   "Produce the meeting's CONVERGED OUTPUT: (1) agreed problem & goals, (2) the agreed "
                   "requirements (functional + non-functional) reconciled across roles, (3) explicit "
                   "decisions made and any recorded dissent, (4) open questions for the human. Be concise.")
        state["synthesis"] = synth
        next_phase = "draft"
    elif phase == "draft":
        base = _make_base(run["directive"], run["constraints"], state.get("research") or {}, run["url"])
        prd, add_cost, model = _ask(adapter,
            f"You are the {FACILITATOR_TITLE} writing the final PRD on behalf of the whole panel. "
            "Write a complete, professional PRD in clean markdown. Reflect the team's agreed synthesis and "
            "incorporate each role's contribution. Ground every factual claim in the shared research; do not invent facts.",
            base + f"\nThe meeting's converged synthesis:\n\n{state.get('synthesis','')}\n\n"
                   "Now write the complete PRD in markdown. Include: Summary, Problem & Context, Goals & "
                   "Non-Goals, Target Users, Functional & Non-Functional Requirements, Proposed Solution, "
                   "Risks & Mitigations (note which role raised each), Acceptance Criteria, Rollout, Open "
                   "Questions, and the single decision needing human approval.\n\n"
                   "VISUALS — do NOT write a wall of text. Include at least: (1) a **Mermaid** "
                   "architecture/flow diagram in a ```mermaid code block (GitHub renders it inline) — "
                   "e.g. `flowchart LR` of the user journey or the system components; (2) for a UI/"
                   "website product, a simple page-layout sketch (a Mermaid block or an ASCII "
                   "wireframe of the key screen) so a reader sees the intended structure. Keep "
                   "diagrams accurate to the proposed solution.")
        state["prd_markdown"] = prd
        next_phase = "finalize"
    else:
        with begin(engine) as c:
            c.execute(text("UPDATE meeting_runs SET status='failed', error=:e, updated_at=now() WHERE id=:id"),
                      {"e": f"unknown phase {phase}", "id": run_id})
        return {"status": "failed", "done": True}

    # persist this phase + enqueue the next step ATOMICALLY (no stall on crash)
    with begin(engine) as c:
        _save_phase(c, run_id, state, add_cost, model, next_phase)
        jobs.create_job(c, project_id=run["project_id"], task_id=run["task_id"],
                        kind="meeting.step", priority=JOB_PRIORITY_INTERACTIVE, payload={"run_id": run_id})
    return {"status": "running", "done": False, "phase": next_phase}


def _finalize_meeting(connection, settings, run_id) -> dict:
    run = load_meeting_run(connection, run_id)
    if run is None:
        return {"status": "not_found"}
    if run["meeting_id"]:  # already finalized — idempotent
        return {"meeting_id": run["meeting_id"], "minutes_artifact_id": run["minutes_artifact_id"],
                "decision_id": run["decision_id"], "already": True}
    project_id = run["project_id"]
    task_id = run["task_id"]
    state = run["state"]
    personas = resolve_roles(run["role_keys"], load_role_map(connection, project_id))
    fctx = run["finalize_ctx"] or {}
    research = state.get("research") or {}
    minutes = {
        "directive": run["directive"], "url": run["url"],
        "participants": [p["title"] for p in personas] + [FACILITATOR_TITLE],
        "role_keys": [p["key"] for p in personas],
        "research_summary": {"sitemap": research.get("sitemap", []), "pages_studied": len(research.get("pages", []))},
        "round1_perspectives": state.get("round1") or {},
        "round2_debate": state.get("round2") or {},
        "round1_gists": state.get("round1_gists") or {},
        "round2_gists": state.get("round2_gists") or {},
        "synthesis": state.get("synthesis"),
        "prd_markdown": state.get("prd_markdown"),
        "cost_usd_micros": (run["usage"] or {}).get("cost_usd_micros", 0),
        "model": (run["usage"] or {}).get("model"),
        "output_text": state.get("synthesis"),
    }
    minutes_artifact_id = create_artifact(connection, settings, project_id, task_id, "meeting_minutes",
        "PRD meeting — minutes & debate", minutes, created_by_job_id=fctx.get("created_by_job_id"))
    meeting_id = make_id("mtg")
    connection.execute(
        text("""INSERT INTO meetings (id, project_id, title, purpose, status, participants,
                                      minutes_artifact_id, completed_at, created_by)
                VALUES (:id, :p, :title, :purpose, 'completed', CAST(:parts AS JSONB), :min, now(), 'boss')"""),
        {"id": meeting_id, "p": project_id, "title": f"PRD meeting: {meeting_title(run.get('title'), run['directive'])}",
         "purpose": "Cross-functional PRD review and drafting",
         "parts": json.dumps([p["title"] for p in personas] + [FACILITATOR_TITLE]), "min": minutes_artifact_id},
    )
    decision_id = create_blocking_decision(connection, project_id, task_id,
        _PRD_DECISION_QUESTION,
        {"action": "execute_agent_tool", "tool": "publish_document", "start_code_task": True,
         "args": {"title": "PRD: " + meeting_title(run.get("title"), run["directive"]), "markdown": state.get("prd_markdown") or "",
                  "subtitle": f"Drafted by a BOSS agent panel · source {run['url']}", "repo_subdir": "docs/prd"},
         "task_id": task_id}, {}, "meeting_prd_ready_to_publish", risk_tier=2)

    # Resolve the gate the panel has already satisfied.
    #
    # The meeting IS the review: product, engineering and QA personas argue the spec out and
    # only then draft it. Asking a person to re-approve that produced a queue rather than a
    # check -- every one was approved, and the only effect was the wait between "PRD ready"
    # and "build started".
    #
    # This approves the decision rather than skipping it: the row exists, the PRD stays
    # readable on the card, and the audit trail records `auto_approval` as the actor, so a
    # reader can always tell which approvals a human made. The follow-on (start the governed
    # build) is the same code path a human approval runs.
    if getattr(settings, "meeting_prd_auto_approve", False):
        from .decisions import SYSTEM_ACTOR, DecisionService

        try:
            DecisionService(settings).transition(
                connection, decision_id, action="approve", actor=SYSTEM_ACTOR,
                system_policy=True,
                note="Approved automatically: the agent panel reviewed and drafted this PRD. "
                     "Set BOSS_MEETING_PRD_AUTO_APPROVE=false to require a human approval.")
        except Exception as error:  # noqa: BLE001
            # A failed auto-approval must not lose the meeting's work. The decision stays
            # pending and a human can approve it in the console exactly as before.
            emit_event(connection, project_id, "meeting.prd_auto_approve_failed", "boss",
                       "decision", decision_id, {"error": str(error)[:400]})
    if fctx.get("reservation_id") and fctx.get("receipt_id"):
        from . import model_routing
        from .budget import BudgetGovernor
        from .receipts import ReceiptService

        actual_cost = int((run["usage"] or {}).get("cost_usd_micros", 0))
        model = (run["usage"] or {}).get("model") or "prd-meeting"
        ReceiptService().mark_succeeded(
            connection,
            fctx["receipt_id"],
            model,
            {"meeting_id": meeting_id, "minutes_artifact_id": minutes_artifact_id},
        )
        BudgetGovernor(settings).settle(
            connection,
            project_id,
            fctx["reservation_id"],
            fctx["receipt_id"],
            "model",
            model,
            int(fctx.get("estimated_usd_micros") or actual_cost),
            actual_cost,
            # The panel ran on the project's model; on a subscription its cost is list-price
            # value, not money (included_spend.py).
            included_in_subscription=model_routing.call_is_included(
                connection, settings, project_id, None),
        )
    elif fctx.get("reservation_id"):
        try:
            from .budget import BudgetGovernor
            BudgetGovernor(settings).release(connection, project_id, fctx["reservation_id"], "meeting complete")
        except Exception:
            pass
    connection.execute(
        text("UPDATE meeting_runs SET status='completed', phase='done', meeting_id=:m, "
             "minutes_artifact_id=:min, decision_id=:dec, updated_at=now() WHERE id=:id"),
        {"m": meeting_id, "min": minutes_artifact_id, "dec": decision_id, "id": run_id},
    )
    emit_event(connection, project_id, "meeting.completed", "boss", "meeting", meeting_id,
               {"minutes_artifact_id": minutes_artifact_id, "decision_id": decision_id})
    _sign_seats(connection, run_id)
    return {"meeting_id": meeting_id, "minutes_artifact_id": minutes_artifact_id, "decision_id": decision_id}


# ============================================================================
# No-spend simulated fixture (Community E2E). Never calls a model.
# ============================================================================
SIMULATED_PHASES = ("research", "round1", "round2", "synthesis", "draft", "finalize")


def _is_simulated_run(run: dict) -> bool:
    constraints = run.get("constraints") or {}
    finalize_ctx = run.get("finalize_ctx") or {}
    state = run.get("state") or {}
    return bool(
        constraints.get("simulated")
        or finalize_ctx.get("simulated")
        or state.get("simulated")
    )


def _fixture_state(directive: str, personas: list[dict], meeting_type: str = DEFAULT_MEETING_TYPE) -> dict:
    titles = [p["title"] for p in personas]
    meeting_type = normalize_meeting_type(meeting_type)
    if meeting_type == "standup":
        return {
            "simulated": True,
            "llm_called": False,
            "meeting_type": "standup",
            "research": {
                "note": "simulated standup check-in; no crawl and no model",
                "pages": [],
                "sitemap": [],
            },
            "round1": {
                title: f"[standup] {title}: yesterday shipped, today focused on: {directive[:80]}"
                for title in titles
            },
            "round2": {
                title: f"[standup] {title}: no live model; blocker called if any."
                for title in titles
            },
            "synthesis": (
                "[standup] Facilitator recap: commitments captured, one optional dissent recorded."
            ),
            "prd_markdown": (
                "# Simulated standup minutes\n\n"
                "This run used `simulated=true`. No model was invoked.\n\n"
                f"Directive: {directive}\n"
            ),
            "action_items": [
                {
                    "title": "Unblock CI flake on payments",
                    "description": "Fixture action from standup — owner follows up today.",
                    "priority": "p1",
                    "risk_tier": 1,
                }
            ],
            "dissent_records": [
                {
                    "role": "QA / Test",
                    "position": "caution",
                    "reason": "Flake still open; do not treat the build as green.",
                }
            ],
            "decision_items": [
                {
                    "question": "Accept today's standup plan and proceed?",
                    "recommendation": {"summary": "Proceed with listed actions; hold if flake worsens."},
                    "risk_tier": 1,
                    "default_action": "hold",
                }
            ],
        }
    if meeting_type == "go_no_go":
        return {
            "simulated": True,
            "llm_called": False,
            "meeting_type": "go_no_go",
            "research": {
                "note": "simulated go/no-go evidence pack; no crawl and no model",
                "pages": [],
                "sitemap": [],
            },
            "round1": {
                title: f"[go/no-go] {title}: evidence reviewed for: {directive[:80]}"
                for title in titles
            },
            "round2": {
                title: f"[go/no-go] {title}: fixture stance recorded; dissent allowed."
                for title in titles
            },
            "synthesis": (
                "[go/no-go] Facilitator recap: explicit GO recorded; one dissent preserved."
            ),
            "prd_markdown": (
                "# Simulated go/no-go minutes\n\n"
                "This run used `simulated=true`. No model was invoked.\n\n"
                f"Directive: {directive}\n\n"
                "Verdict: GO (fixture).\n"
            ),
            "action_items": [
                {
                    "title": "Record residual risk from dissent",
                    "description": "Fixture follow-up from go/no-go — owner tracks the recorded dissent.",
                    "priority": "p1",
                    "risk_tier": 1,
                }
            ],
            "dissent_records": [
                {
                    "role": "QA / Test",
                    "position": "no-go",
                    "reason": "Fixture residual risk; do not treat this as a live release gate.",
                }
            ],
            "decision_items": [
                {
                    "question": "Go or no-go on this change?",
                    "recommendation": {
                        "summary": (
                            "Fixture verdict: GO. Dissent is preserved; this is not a live LLM panel."
                        ),
                        "verdict": "go",
                    },
                    "options": [
                        {"action": "approve", "label": "Go"},
                        {"action": "reject", "label": "No-go"},
                        {"action": "revise", "label": "Hold"},
                    ],
                    "risk_tier": 2,
                    "default_action": "hold",
                }
            ],
        }
    return {
        "simulated": True,
        "llm_called": False,
        "meeting_type": "prd_panel",
        "research": {
            "note": "simulated research; no crawl and no model",
            "pages": [],
            "sitemap": [],
        },
        "round1": {
            title: f"[simulated] {title} opening perspective on: {directive[:120]}"
            for title in titles
        },
        "round2": {
            title: f"[simulated] {title} debate note — no live model was called."
            for title in titles
        },
        "synthesis": (
            "[simulated] Facilitator synthesis. Fixture only — not a production PRD."
        ),
        "prd_markdown": (
            "# Simulated PRD fixture\n\n"
            "This run used `simulated=true`. No model was invoked.\n\n"
            f"Directive: {directive}\n"
        ),
    }


def _project_scope(connection, project_id: str) -> dict:
    row = connection.execute(
        text(
            """
            SELECT p.workspace_id, w.org_id
            FROM projects p
            JOIN workspaces w ON w.id = p.workspace_id
            WHERE p.id = :project_id
            """
        ),
        {"project_id": project_id},
    ).mappings().first()
    if not row:
        raise ValueError("meeting project is missing workspace/org scope")
    return {"workspace_id": str(row["workspace_id"]), "org_id": str(row["org_id"])}


def _write_phase_artifact(
    connection,
    settings: Settings,
    *,
    project_id: str,
    task_id: Optional[str],
    phase: str,
    payload: dict,
) -> str:
    return create_artifact(
        connection,
        settings,
        project_id,
        task_id,
        f"meeting_phase_{phase}",
        f"Simulated meeting phase: {phase}",
        {"phase": phase, "simulated": True, "payload": payload},
    )


def _phase_timeline_from_state(state: dict) -> list[dict]:
    artifacts = state.get("artifacts") or {}
    timeline = []
    for phase in SIMULATED_PHASES:
        timeline.append(
            {
                "phase": phase,
                "status": "completed",
                "artifact_id": artifacts.get(phase),
                "simulated": True,
            }
        )
    return timeline


def _advance_simulated_phase(connection, settings: Settings, jobs, run: dict) -> dict:
    """Advance one simulated phase. No adapter, no budget, no enqueue of spendy work."""
    personas = resolve_roles(run["role_keys"], load_role_map(connection, run["project_id"]))
    fixture = _fixture_state(run["directive"], personas, meeting_type=meeting_type_of(run))
    state = dict(run["state"] or {})
    state.setdefault("artifacts", {})
    state["simulated"] = True
    state["llm_called"] = False
    phase = run["phase"]
    next_map = {
        "research": "round1",
        "round1": "round2",
        "round2": "synthesis",
        "synthesis": "draft",
        "draft": "finalize",
    }
    if phase not in next_map:
        raise ValueError(f"unknown simulated phase {phase}")
    key = "prd_markdown" if phase == "draft" else phase
    state[key] = fixture[key]
    artifact_id = _write_phase_artifact(
        connection,
        settings,
        project_id=run["project_id"],
        task_id=run.get("task_id"),
        phase=phase,
        payload=state[key] if not isinstance(state[key], dict) else state[key],
    )
    state["artifacts"][phase] = artifact_id
    state["phase_timeline"] = _phase_timeline_from_state(state)
    next_phase = next_map[phase]
    _save_phase(connection, run["id"], state, 0, "simulated-fixture", next_phase)
    jobs.create_job(
        connection,
        project_id=run["project_id"],
        task_id=run["task_id"],
        kind="meeting.step",
        priority=JOB_PRIORITY_INTERACTIVE,
        payload={"run_id": run["id"], "simulated": True},
    )
    return {"status": "running", "done": False, "phase": next_phase, "simulated": True}


def _first_meeting_decision_id(meeting) -> Optional[str]:
    if not meeting:
        return None
    decisions = meeting.get("decisions") or []
    if isinstance(decisions, str):
        try:
            decisions = json.loads(decisions)
        except Exception:
            return None
    if not decisions:
        return None
    first = decisions[0]
    if isinstance(first, dict):
        return first.get("decision_id")
    return str(first) if first else None


def meeting_evidence(connection, meeting_or_run_id: str) -> Optional[dict]:
    """Operator-visible phase timeline + artifact links for a run or meeting."""
    run = load_meeting_run(connection, meeting_or_run_id)
    if run is None:
        row = connection.execute(
            text(
                "SELECT id FROM meeting_runs WHERE meeting_id = :id ORDER BY updated_at DESC LIMIT 1"
            ),
            {"id": meeting_or_run_id},
        ).mappings().first()
        if row:
            run = load_meeting_run(connection, row["id"])
    meeting = connection.execute(
        text("SELECT * FROM meetings WHERE id = :id"),
        {"id": (run or {}).get("meeting_id") or meeting_or_run_id},
    ).mappings().first()
    if run is None and meeting is None:
        return None
    state = (run or {}).get("state") or {}
    artifacts = dict(state.get("artifacts") or {})
    timeline = list(state.get("phase_timeline") or [])
    if not timeline and meeting:
        minutes = {}
        if meeting.get("minutes_artifact_id"):
            art = connection.execute(
                text("SELECT content FROM artifacts WHERE id = :id"),
                {"id": meeting["minutes_artifact_id"]},
            ).mappings().first()
            content = (art or {}).get("content") or {}
            if isinstance(content, str):
                try:
                    content = json.loads(content)
                except Exception:
                    content = {}
            minutes = content.get("minutes") or content
            timeline = list(minutes.get("phase_timeline") or [])
        artifacts.setdefault("transcript", meeting.get("transcript_artifact_id"))
        artifacts.setdefault("minutes", meeting.get("minutes_artifact_id"))
    links = [
        {"kind": kind, "artifact_id": artifact_id}
        for kind, artifact_id in artifacts.items()
        if artifact_id
    ]
    if meeting:
        if meeting.get("transcript_artifact_id"):
            links.append({"kind": "transcript", "artifact_id": meeting["transcript_artifact_id"]})
        if meeting.get("minutes_artifact_id"):
            links.append({"kind": "minutes", "artifact_id": meeting["minutes_artifact_id"]})
    # de-dupe
    seen = set()
    unique_links = []
    for item in links:
        key = (item["kind"], item["artifact_id"])
        if key in seen:
            continue
        seen.add(key)
        unique_links.append(item)
    meeting_type = meeting_type_of(run) if run else DEFAULT_MEETING_TYPE
    if meeting_type == DEFAULT_MEETING_TYPE and state.get("meeting_type"):
        try:
            meeting_type = normalize_meeting_type(state.get("meeting_type"))
        except ValueError:
            meeting_type = DEFAULT_MEETING_TYPE
    return {
        "id": (run or {}).get("id") or meeting["id"],
        "project_id": (run or {}).get("project_id")
        or (meeting["project_id"] if meeting else None),
        "meeting_id": (run or {}).get("meeting_id") or (meeting["id"] if meeting else None),
        "run_id": (run or {}).get("id"),
        "status": (run or {}).get("status") or meeting["status"],
        "phase": (run or {}).get("phase"),
        "meeting_type": meeting_type,
        "simulated": bool(state.get("simulated") or (run and _is_simulated_run(run))),
        "phase_timeline": timeline,
        "artifact_links": unique_links,
        "receipt_id": ((run or {}).get("finalize_ctx") or {}).get("receipt_id"),
        "minutes_artifact_id": (run or {}).get("minutes_artifact_id")
        or (meeting["minutes_artifact_id"] if meeting else None),
        "decision_id": (run or {}).get("decision_id")
        or _first_meeting_decision_id(meeting),
        "action_task_ids": list((meeting or {}).get("action_task_ids") or []),
        "dissent_records": list((meeting or {}).get("dissent_records") or []),
        "llm_called": bool(state.get("llm_called")),
    }


def _materialize_standup_outputs(
    connection,
    *,
    project_id: str,
    meeting_id: str,
    actor: str,
    minutes_artifact_id: str,
    state: dict,
) -> dict:
    """Turn typed fixture lists into action tasks + a Decision. No model."""
    from .canonical import CanonicalObjectService

    canonical = CanonicalObjectService()
    action_items = list(state.get("action_items") or [])
    decision_items = list(state.get("decision_items") or [])
    dissent_records = list(state.get("dissent_records") or [])
    action_task_ids = [
        canonical._create_meeting_action_task(
            connection,
            project_id,
            meeting_id,
            index,
            item,
            actor,
            minutes_artifact_id,
        )
        for index, item in enumerate(action_items, start=1)
    ]
    decisions = [
        canonical._create_meeting_decision(
            connection,
            project_id,
            meeting_id,
            index,
            item,
            actor,
            minutes_artifact_id,
        )
        for index, item in enumerate(decision_items, start=1)
    ]
    connection.execute(
        text(
            """
            UPDATE meetings
            SET action_task_ids = CAST(:actions AS JSONB),
                decisions = CAST(:decisions AS JSONB),
                dissent_records = CAST(:dissent AS JSONB),
                updated_at = now()
            WHERE id = :id
            """
        ),
        {
            "actions": json.dumps(action_task_ids),
            "decisions": json.dumps(decisions),
            "dissent": json.dumps(dissent_records),
            "id": meeting_id,
        },
    )
    decision_id = decisions[0]["decision_id"] if decisions else None
    return {
        "action_task_ids": action_task_ids,
        "action_items": action_items,
        "decisions": decisions,
        "decision_id": decision_id,
        "dissent_records": dissent_records,
    }


def run_simulated_meeting(
    connection,
    settings: Settings,
    *,
    project_id: str,
    actor: str,
    meeting_id: Optional[str] = None,
    directive: Optional[str] = None,
    url: str = "",
    role_keys: Optional[list] = None,
    meeting_type: str = DEFAULT_MEETING_TYPE,
) -> dict:
    """Persist a full research→finalize fixture without calling a model or reserving budget.

    Side effects (artifacts, receipt) are receipted with a unique dedupe_key.
    Does not enqueue a worker job. Typed fixtures (standup, go/no-go) also
    materialize action items, optional dissent, and a Decision — still with no
    live model.
    """
    meeting_type = normalize_meeting_type(meeting_type)
    default_directive = TYPED_DEFAULT_DIRECTIVES.get(
        meeting_type, "Community simulated panel fixture"
    )
    meeting = None
    if meeting_id:
        meeting = connection.execute(
            text("SELECT * FROM meetings WHERE id = :id AND project_id = :p"),
            {"id": meeting_id, "p": project_id},
        ).mappings().first()
        existing_run = load_meeting_run(connection, meeting_id)
        if existing_run is None:
            row = connection.execute(
                text(
                    "SELECT id FROM meeting_runs WHERE meeting_id = :id AND project_id = :p "
                    "ORDER BY created_at DESC LIMIT 1"
                ),
                {"id": meeting_id, "p": project_id},
            ).mappings().first()
            if row:
                existing_run = load_meeting_run(connection, row["id"])
        if existing_run and existing_run.get("status") in {"completed", "simulated"}:
            evidence = meeting_evidence(connection, existing_run["id"]) or {}
            return {
                **evidence,
                "already": True,
                "simulated": True,
                "meeting_type": meeting_type_of(existing_run),
                "project_id": project_id,
            }
        if existing_run and existing_run.get("status") == "running":
            evidence = meeting_evidence(connection, existing_run["id"]) or {}
            return {
                **evidence,
                "already": True,
                "duplicate_of": existing_run["id"],
                "meeting_type": meeting_type_of(existing_run),
                "project_id": project_id,
            }

    resolved_directive = directive or (meeting["purpose"] if meeting else None) or default_directive
    fingerprint = meeting_fingerprint(
        project_id, resolved_directive, url, meeting_type=meeting_type
    )
    existing_run_id, _existing_task = active_duplicate_meeting(connection, project_id, fingerprint)
    if existing_run_id:
        emit_event(
            connection,
            project_id,
            "meeting.duplicate_suppressed",
            actor,
            "meeting_run",
            existing_run_id,
            {"directive": resolved_directive[:200], "fingerprint": fingerprint, "meeting_type": meeting_type},
        )
        evidence = meeting_evidence(connection, existing_run_id) or {}
        return {
            **evidence,
            "already": True,
            "duplicate_of": existing_run_id,
            "simulated": True,
            "meeting_type": meeting_type,
            "project_id": project_id,
            "llm_called": False,
        }

    if meeting is None:
        title_prefix = TYPED_TITLE_PREFIX.get(meeting_type, "Simulated meeting")
        title = f"{title_prefix}: {meeting_subject(resolved_directive)}"
        purpose = TYPED_PURPOSE.get(
            meeting_type, "No-spend simulated multi-agent panel fixture"
        )
        from .canonical import CanonicalObjectService

        meeting_dict = CanonicalObjectService().create_meeting(
            connection,
            project_id=project_id,
            title=title,
            purpose=purpose,
            actor=actor,
            participants=[],
            agenda=list(SIMULATED_PHASES),
        )
        meeting_id = meeting_dict["id"]
        meeting = connection.execute(
            text("SELECT * FROM meetings WHERE id = :id"),
            {"id": meeting_id},
        ).mappings().first()
    directive = resolved_directive
    personas = resolve_roles(role_keys, load_role_map(connection, project_id))
    state = _fixture_state(directive, personas, meeting_type=meeting_type)
    artifacts = {}
    for phase in SIMULATED_PHASES:
        key = "prd_markdown" if phase == "draft" else phase
        payload = state.get(key)
        artifacts[phase] = _write_phase_artifact(
            connection,
            settings,
            project_id=project_id,
            task_id=None,
            phase=phase,
            payload=payload if isinstance(payload, dict) else {"text": payload},
        )
    state["artifacts"] = artifacts
    state["phase_timeline"] = _phase_timeline_from_state(state)
    state["meeting_type"] = meeting_type
    scope = _project_scope(connection, project_id)
    from .receipts import ReceiptService

    receipt, created = ReceiptService().create_or_reuse(
        connection,
        scope["org_id"],
        scope["workspace_id"],
        project_id,
        f"meeting.simulated:{project_id}:{meeting['id']}",
        "meeting.simulated_run",
        {"meeting_id": meeting["id"], "phases": list(SIMULATED_PHASES), "meeting_type": meeting_type},
        {"actor": actor, "llm_called": False, "cost_usd_micros": 0, "meeting_type": meeting_type},
    )
    minutes = {
        "directive": directive,
        "url": url,
        "meeting_type": meeting_type,
        "simulated": True,
        "llm_called": False,
        "participants": [p["title"] for p in personas] + [FACILITATOR_TITLE],
        "role_keys": [p["key"] for p in personas],
        "research_summary": {
            "sitemap": [],
            "pages_studied": 0,
            "note": state["research"]["note"],
        },
        "round1_perspectives": state["round1"],
        "round2_debate": state["round2"],
        "synthesis": state["synthesis"],
        "prd_markdown": state["prd_markdown"],
        "action_items": list(state.get("action_items") or []),
        "dissent_records": list(state.get("dissent_records") or []),
        "phase_timeline": state["phase_timeline"],
        "artifacts": artifacts,
        "cost_usd_micros": 0,
        "model": "simulated-fixture",
    }
    minutes_artifact_id = create_artifact(
        connection,
        settings,
        project_id,
        None,
        "meeting_minutes",
        f"{meeting['title']} simulated minutes",
        minutes,
    )
    transcript_artifact_id = create_artifact(
        connection,
        settings,
        project_id,
        None,
        "meeting_transcript",
        f"{meeting['title']} simulated transcript",
        {
            "simulated": True,
            "meeting_type": meeting_type,
            "transcript": [
                {
                    "speaker": "fixture",
                    "message": f"phase {phase} recorded",
                    "phase": phase,
                }
                for phase in SIMULATED_PHASES
            ],
        },
    )
    connection.execute(
        text(
            """
            UPDATE meetings
            SET status = 'simulated',
                participants = CAST(:parts AS JSONB),
                transcript_artifact_id = :transcript,
                minutes_artifact_id = :minutes,
                completed_at = now(),
                updated_at = now()
            WHERE id = :id
            """
        ),
        {
            "parts": json.dumps([p["title"] for p in personas] + [FACILITATOR_TITLE]),
            "transcript": transcript_artifact_id,
            "minutes": minutes_artifact_id,
            "id": meeting["id"],
        },
    )
    standup_outputs: dict = {
        "action_task_ids": [],
        "action_items": [],
        "decisions": [],
        "decision_id": None,
        "dissent_records": [],
    }
    if meeting_type in {"standup", "go_no_go"}:
        standup_outputs = _materialize_standup_outputs(
            connection,
            project_id=project_id,
            meeting_id=meeting["id"],
            actor=actor,
            minutes_artifact_id=minutes_artifact_id,
            state=state,
        )
    run_id = make_id("mrun")
    connection.execute(
        text(
            """
            INSERT INTO meeting_runs (
              id, project_id, task_id, status, phase, directive, url,
              role_keys, constraints, state, usage, finalize_ctx,
              meeting_id, minutes_artifact_id, decision_id
            )
            VALUES (
              :id, :p, NULL, 'simulated', 'done', :d, :url,
              CAST(:rk AS JSONB), CAST(:c AS JSONB), CAST(:state AS JSONB),
              CAST(:usage AS JSONB), CAST(:fc AS JSONB),
              :meeting_id, :minutes, :decision_id
            )
            """
        ),
        {
            "id": run_id,
            "p": project_id,
            "d": directive,
            "url": url,
            "rk": json.dumps([p["key"] for p in personas]),
            "c": json.dumps({"simulated": True, "meeting_type": meeting_type}),
            "state": json.dumps(state),
            "usage": json.dumps(
                {"cost_usd_micros": 0, "model": "simulated-fixture", "simulated": True}
            ),
            "fc": json.dumps(
                {
                    "simulated": True,
                    "receipt_id": receipt["id"],
                    "meeting_fingerprint": fingerprint,
                    "meeting_type": meeting_type,
                }
            ),
            "meeting_id": meeting["id"],
            "minutes": minutes_artifact_id,
            "decision_id": standup_outputs.get("decision_id"),
        },
    )
    emit_event(
        connection,
        project_id,
        "meeting.simulated",
        actor,
        "meeting",
        meeting["id"],
        {
            "run_id": run_id,
            "receipt_id": receipt["id"],
            "minutes_artifact_id": minutes_artifact_id,
            "meeting_type": meeting_type,
            "decision_id": standup_outputs.get("decision_id"),
            "llm_called": False,
            "duplicate": not created,
        },
    )
    return {
        "id": meeting["id"],
        "run_id": run_id,
        "meeting_id": meeting["id"],
        "project_id": project_id,
        "status": "simulated",
        "phase": "done",
        "meeting_type": meeting_type,
        "simulated": True,
        "llm_called": False,
        "phase_timeline": state["phase_timeline"],
        "artifact_links": [
            {"kind": phase, "artifact_id": artifact_id}
            for phase, artifact_id in artifacts.items()
        ]
        + [
            {"kind": "transcript", "artifact_id": transcript_artifact_id},
            {"kind": "minutes", "artifact_id": minutes_artifact_id},
        ],
        "receipt_id": receipt["id"],
        "minutes_artifact_id": minutes_artifact_id,
        "transcript_artifact_id": transcript_artifact_id,
        "decision_id": standup_outputs.get("decision_id"),
        "action_task_ids": standup_outputs.get("action_task_ids") or [],
        "action_items": standup_outputs.get("action_items") or [],
        "decisions": standup_outputs.get("decisions") or [],
        "dissent_records": standup_outputs.get("dissent_records") or [],
        "already": not created,
    }
