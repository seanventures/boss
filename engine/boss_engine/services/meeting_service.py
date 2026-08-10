"""Multi-agent PRD meeting — BOSS convenes a panel of role-agents that debate,
then synthesize, then draft, before the deliverable goes through the human gate.

Each "agent" is the same brain (whatever make_chat_adapter returns — e.g. Claude on
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
from .claude_cli_adapter import make_chat_adapter
from .core import create_artifact, create_blocking_decision, emit_event, make_id
from .mcp_client import MCPClient
from .mcp_registry import list_mcp_servers
from .openai_adapter import estimate_cost_usd_micros

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
    "data": {
        "key": "data", "title": "Data / Analytics",
        "lens": (
            "measurement and evidence. What to instrument, the metrics that prove success, data "
            "quality, and how decisions will be validated post-launch."
        ),
    },
}
DEFAULT_ROLE_KEYS = ["product_manager", "engineer", "ux_designer", "privacy_safety"]
FACILITATOR_TITLE = "Facilitator / Chief of Staff"
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
         "risk_tier": int(r["risk_tier"]), "data_boundary": r["data_boundary"],
         "is_default": bool(r["is_default"]), "enabled": bool(r["enabled"])}
        for r in rows
    ]


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
    return (
        f"You are the {p['title']} in a cross-functional product meeting at the company BOSS runs. "
        f"Your lens: {p['lens']} "
        "Speak only from your role. Be concrete, cite specifics from the shared research, and keep "
        "it to tight bullet points. Do not write the whole PRD — contribute your part of the thinking."
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
    if not server:
        return {"note": "web-intel MCP server not available", "pages": []}
    try:
        result = MCPClient(settings, timeout=120.0).call_tool(server["url"], "study_site", {"url": url, "max_pages": 6})
    except Exception as exc:
        return {"note": f"research failed: {type(exc).__name__}: {exc}", "pages": []}
    # trim each page so the shared brief stays compact for every persona prompt
    pages = []
    for pg in (result.get("pages") or [])[:6]:
        pages.append({"url": pg.get("url"), "title": pg.get("title"), "text": (pg.get("text") or "")[:1100]})
    return {"entry_url": url, "sitemap": result.get("sitemap_routes", []), "pages": pages}


def _study_site(settings: Settings, url: str, server: Optional[dict]) -> dict:
    """Crawl the site via the web-intel MCP server (no DB connection held)."""
    if not url:
        return {"note": "no URL supplied; agents reason from the directive only", "pages": []}
    if not server:
        return {"note": "web-intel MCP server not available", "pages": []}
    try:
        result = MCPClient(settings, timeout=120.0).call_tool(server["url"], "study_site", {"url": url, "max_pages": 6})
    except Exception as exc:
        return {"note": f"research failed: {type(exc).__name__}: {exc}", "pages": []}
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


def _panel_blob(personas: list[dict], rnd: dict) -> str:
    return "\n\n".join(f"--- {p['title']} ---\n{(rnd or {}).get(p['title'], '')}" for p in personas)


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
    }


def load_meeting_run(connection: Connection, run_id: str) -> Optional[dict]:
    r = connection.execute(
        text("SELECT id, project_id, task_id, status, phase, directive, url, role_keys, constraints, "
             "state, usage, finalize_ctx, meeting_id, minutes_artifact_id, decision_id "
             "FROM meeting_runs WHERE id = :id"),
        {"id": run_id},
    ).mappings().first()
    return _row_to_run(r) if r else None


def meeting_fingerprint(project_id: str, directive: str, url: str = "") -> str:
    """A stable identity for "this meeting, about this thing, in this project".

    Whitespace-normalised: a directive pasted twice differs by a trailing newline and means
    the same meeting.
    """
    import hashlib

    parts = [project_id, " ".join((directive or "").split()), (url or "").strip()]
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


def start_meeting_run(connection, *, project_id, task_id, directive, url, constraints, role_keys, finalize_ctx) -> str:
    run_id = make_id("mrun")
    personas = resolve_roles(role_keys, load_role_map(connection, project_id))
    connection.execute(
        text("""
            INSERT INTO meeting_runs (id, project_id, task_id, status, phase, directive, url,
                                      role_keys, constraints, finalize_ctx)
            VALUES (:id, :p, :t, 'running', 'research', :d, :url,
                    CAST(:rk AS JSONB), CAST(:c AS JSONB), CAST(:fc AS JSONB))
        """),
        {"id": run_id, "p": project_id, "t": task_id, "d": directive, "url": url,
         "rk": json.dumps([p["key"] for p in personas]),
         "c": json.dumps(constraints or {}), "fc": json.dumps(finalize_ctx or {})},
    )
    emit_event(connection, project_id, "meeting.started", "boss", "meeting_run", run_id,
               {"task_id": task_id, "participants": [p["title"] for p in personas]})
    return run_id


def convene_prd_meeting_core(
    connection, *, settings: Settings, budget, intake, jobs,
    project_id: str, directive: str, url: str = "",
    constraints: Optional[dict] = None, role_keys: Optional[list] = None,
    task_id: Optional[str] = None,
) -> dict:
    """Shared core for convening a durable multi-agent PRD meeting: create the
    task (if needed), reserve budget, start the run, enqueue the first step.
    Used by the /v1/meetings/prd endpoint and the Plane ``boss:design`` intake so
    both routes are governed identically."""
    constraints = constraints or {}

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
    fingerprint = meeting_fingerprint(project_id, directive, url)
    if not constraints.get("allow_duplicate"):
        existing_run, existing_task = active_duplicate_meeting(connection, project_id, fingerprint)
        if existing_run:
            emit_event(connection, project_id, "meeting.duplicate_suppressed", "boss",
                       "meeting_run", existing_run,
                       {"directive": directive[:200], "fingerprint": fingerprint})
            return {"task_id": existing_task, "run_id": existing_run, "job_id": None,
                    "duplicate_of": existing_run}

    if task_id is None:
        task = intake.create_task(
            connection,
            project_id=project_id,
            title=f"PRD meeting: {directive[:90]}",
            description=directive,
            dedupe_key=make_id("mtg"),
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
        finalize_ctx={"reservation_id": reservation_id, "meeting_fingerprint": fingerprint},
    )
    job = jobs.create_job(
        connection, project_id=project_id, task_id=task_id,
        kind="meeting.step", priority=48, payload={"run_id": run_id},
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

    with begin(engine) as c:
        personas = resolve_roles(run["role_keys"], load_role_map(c, run["project_id"]))
    state = dict(run["state"])
    adapter = make_chat_adapter(settings)
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
                             "List your top priorities, requirements, and concerns from your lens.")
        state["round1"] = r1
        next_phase = "round2"
    elif phase == "round2":
        base = _make_base(run["directive"], run["constraints"], state.get("research") or {}, run["url"])
        r1_blob = _panel_blob(personas, state.get("round1") or {})
        r2, add_cost, model = _ask_panel(adapter, personas,
            lambda p: base + f"\nHere is what the whole panel said in round 1:\n\n{r1_blob}\n\n"
                             f"As the {p['title']}, respond: where do you AGREE, where do you DISAGREE or see "
                             "risk in your colleagues' points, and what is your refined position? Be specific.")
        state["round2"] = r2
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
                        kind="meeting.step", priority=48, payload={"run_id": run_id})
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
        {"id": meeting_id, "p": project_id, "title": f"PRD meeting: {run['directive'][:80]}",
         "purpose": "Cross-functional PRD review and drafting",
         "parts": json.dumps([p["title"] for p in personas] + [FACILITATOR_TITLE]), "min": minutes_artifact_id},
    )
    decision_id = create_blocking_decision(connection, project_id, task_id,
        "Approve publishing the PRD the team drafted",
        {"action": "execute_agent_tool", "tool": "publish_document", "start_code_task": True,
         "args": {"title": "PRD: " + run["directive"][:80].rstrip(), "markdown": state.get("prd_markdown") or "",
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
        from .decisions import DecisionService

        try:
            DecisionService(settings).transition(
                connection, decision_id, action="approve", actor="auto_approval",
                note="Approved automatically: the agent panel reviewed and drafted this PRD. "
                     "Set BOSS_MEETING_PRD_AUTO_APPROVE=false to require a human approval.")
        except Exception as error:  # noqa: BLE001
            # A failed auto-approval must not lose the meeting's work. The decision stays
            # pending and a human can approve it in the console exactly as before.
            emit_event(connection, project_id, "meeting.prd_auto_approve_failed", "boss",
                       "decision", decision_id, {"error": str(error)[:400]})
    if fctx.get("reservation_id") and fctx.get("receipt_id"):
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
    return {"meeting_id": meeting_id, "minutes_artifact_id": minutes_artifact_id, "decision_id": decision_id}
