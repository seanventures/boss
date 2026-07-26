"""PRD -> scoped coding tasks (autonomous decomposition).

BOSS's software-factory promise is to take a product spec and DELIVER it — not hand a human a
"here's the PRD, now write the tasks yourself" dead-end. This turns a PRD (or a feature directive)
into a SMALL ordered set of scoped, composable coding tasks and runs them as ONE lineage that
shares a work branch, so each task builds on the previous one's committed code (scaffold ->
features -> wire-up) and the final task delivers the runnable product.

The chain reuses the existing feedback-iteration branch model (boss/code/{root_run_id}): every task
after the first is started as a continuation (fctx.root_run_id = root), so it clones the prior
task's code from the shared branch and adds to it. No PR-merging orchestration needed — the PR on
the shared branch accumulates the whole product.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Optional

from sqlalchemy import text

from ..config import Settings
from ..db import begin
from .claude_cli_adapter import make_chat_adapter
from .code_task_service import load_code_task_run, start_code_task
from .core import emit_event, make_id

logger = logging.getLogger(__name__)

_WEB_APP_RE = re.compile(
    r"\b(vite|react|next\.?js|svelte|vue|frontend|front-end|website|web ?app|tailwind|"
    r"single-page|\bspa\b|\bui\b|dashboard)\b", re.I)


def _looks_like_web_app(text_v: str) -> bool:
    return bool(_WEB_APP_RE.search(text_v or ""))


def seed_web_scaffold(settings: Settings, *, work_branch: str, name: str = "app",
                      kind: str = "vite") -> int:
    """Commit a DETERMINISTIC Vite+React+TS+Tailwind+shadcn scaffold to work_branch — no agent, no
    turns, can't hit error_max_turns. Feature tasks then build on it. Returns the file count."""
    from .scaffold_service import scaffold_project
    from .software_factory import SoftwareFactoryService
    files = scaffold_project(name=name, kind=kind)["files"]
    gh = SoftwareFactoryService(settings).github
    gh.ensure_branch(work_branch)
    for path, content in files.items():
        gh.upsert_file(work_branch, path, content,
                       "scaffold: deterministic Vite+React+TS+Tailwind base")
    return len(files)

_DECOMP_SYSTEM = (
    "You are a staff engineer deciding how to break a product spec into coding tasks for an "
    "autonomous coding pipeline. FIRST judge complexity, then use AS FEW tasks as correctly "
    "deliver it:\n"
    "- If the product is SIMPLE (one module, a small utility, a single component/endpoint), return "
    "EXACTLY ONE task. Do NOT invent scaffolding/finalize steps for something small.\n"
    "- Split into multiple tasks ONLY when there are genuinely separable components (e.g. a real "
    "app: scaffold, shared types, engine/logic, UI, wire-up). Use up to the given max, no more.\n"
    "- ORDER them so foundational tasks (scaffold, shared types/engine) come first and the LAST "
    "task WIRES everything into the finished, runnable product.\n"
    "- For each task add a `depends_on` array of the order-numbers it needs (empty [] if none). "
    "Tasks with disjoint depends_on and disjoint files CAN run in parallel; be honest about real "
    "dependencies so the executor can parallelize what's independent.\n"
    "- Each task builds ON TOP of prior tasks' code on a shared branch — later tasks may reference "
    "files earlier tasks create; say so in the instructions. Keep ONE tech stack across all tasks.\n"
    "- Each task must be self-contained, independently reviewable, with concrete testable criteria.\n"
    "Output ONLY a JSON array, no prose or fences:\n"
    '[{"title": "...", "instructions": "...", "acceptance_criteria": ["..."], "order": 1, '
    '"depends_on": []}]'
)


def _extract_json_array(textv: str) -> list:
    """Pull the first JSON array out of the model's reply (tolerates prose/fences around it)."""
    s = textv or ""
    s = s.replace("```json", "").replace("```", "")
    m = re.search(r"\[\s*\{.*}\s*\]", s, re.S)
    if not m:
        return []
    try:
        val = json.loads(m.group(0))
        return val if isinstance(val, list) else []
    except (json.JSONDecodeError, ValueError):
        return []


def decompose_prd_into_tasks(settings: Settings, prd_or_directive: str, *,
                             max_tasks: int = 6) -> list[dict]:
    """Read a PRD/directive and return an ORDERED list of scoped, composable coding tasks:
    [{title, instructions, acceptance_criteria: [...], order: int}]. Returns [] on failure."""
    messages = [
        {"role": "system", "content": _DECOMP_SYSTEM},
        {"role": "user", "content":
            f"Decompose this into at most {max_tasks} scoped, ordered, composable coding tasks:\n\n"
            + (prd_or_directive or "")[:9000]},
    ]
    try:
        resp, _usage = make_chat_adapter(settings).create_chat_completion(messages)
        raw = resp.get("content") or ""
    except Exception as exc:  # noqa: BLE001 — decomposition failure degrades to empty, not a crash
        logger.warning("PRD decomposition model call failed: %s", exc)
        return []
    out: list[dict] = []
    for i, t in enumerate(_extract_json_array(raw)[:max_tasks]):
        if not isinstance(t, dict) or not t.get("title") or not t.get("instructions"):
            continue
        deps = t.get("depends_on") or []
        out.append({
            "title": str(t["title"])[:180],
            "instructions": str(t["instructions"]),
            "acceptance_criteria": [str(c) for c in (t.get("acceptance_criteria") or [])][:8],
            "order": int(t.get("order", i + 1)) if str(t.get("order", "")).isdigit() else i + 1,
            "depends_on": [int(x) for x in deps if str(x).isdigit()],
        })
    out.sort(key=lambda x: x["order"])
    return out


def start_decomposed_build(engine, settings: Settings, jobs, *, project_id: str,
                           task_id: Optional[str], prd_or_directive: str, owner: str, repo: str,
                           base_branch: str = "main", profile: Optional[str] = None,
                           risk_tier: Optional[int] = None, max_tasks: int = 6,
                           seed_scaffold: bool = True) -> dict:
    """Decompose the PRD and START the build as one lineage. For a WEB APP, first SEED a
    deterministic Vite+React+TS+Tailwind scaffold onto a fresh lineage branch (no agent, no
    micro-VM turn limit — the exact thing that kept failing an agent-authored scaffold), then
    decompose FEATURE tasks that build on it. Each subsequent task auto-starts as a continuation
    on the shared branch when the previous lands (advance_decomposition)."""
    root: Optional[str] = None
    seeded = 0
    decomp_input = prd_or_directive
    if seed_scaffold and _looks_like_web_app(prd_or_directive):
        root = make_id("crun")
        try:
            seeded = seed_web_scaffold(settings, work_branch=f"boss/code/{root}",
                                       name=(repo or "app"))
            decomp_input = (
                "IMPORTANT: a buildable Vite + React + TypeScript + Tailwind + shadcn-style scaffold "
                "ALREADY EXISTS on the branch (package.json with dev/build/typecheck/lint/test:e2e "
                "scripts, vite.config.ts, tailwind.config.ts, tsconfig.json, index.html, "
                "src/main.tsx, src/App.tsx, src/index.css design tokens, src/lib/utils.ts (cn), "
                "src/components/ui/button.tsx + card.tsx, e2e/home.spec.ts). Do NOT create a scaffold "
                "task and do NOT re-init the project. Decompose ONLY the FEATURE tasks that build on "
                "this scaffold (types/taxonomy, engine, feature components, wire-up in src/App.tsx).\n\n"
                + prd_or_directive)
        except Exception as exc:  # noqa: BLE001 — seed failure degrades to an agent-authored scaffold
            logger.warning("deterministic scaffold seed failed, falling back to agent: %s", exc)
            root, seeded, decomp_input = None, 0, prd_or_directive
    tasks = decompose_prd_into_tasks(settings, decomp_input, max_tasks=max_tasks)
    if not tasks:
        return {"tasks": [], "started": False, "reason": "decomposition produced no tasks"}
    first, rest = tasks[0], tasks[1:]
    fctx = {"decomposition_plan": rest, "decomposition_index": 1, "decomposition_total": len(tasks)}
    task_base = base_branch
    web_tests: list = []
    if root:  # build the first feature ON the seeded scaffold branch (shared lineage)
        fctx["root_run_id"] = root
        # A scaffolded web build: `npm run build` IS the test, so downstream stages skip the
        # separate (Python-defaulting) write_tests stage. Propagated to every chained task below.
        fctx["web_app_build"] = True
        web_tests = ["npm run build"]
        task_base = f"boss/code/{root}"
    with begin(engine) as c:
        run_id = start_code_task(
            c, project_id=project_id, task_id=task_id,
            title=f"[1/{len(tasks)}] {first['title']}",
            instructions=first["instructions"],
            acceptance_criteria=first["acceptance_criteria"], test_commands=web_tests,
            owner=owner, repo=repo, base_branch=task_base,
            finalize_ctx=fctx, jobs=jobs, profile=profile, risk_tier=risk_tier)
        emit_event(c, project_id, "prd.decomposed", "boss", "code_task_run", run_id,
                   {"total_tasks": len(tasks), "titles": [t["title"] for t in tasks],
                    "first_run_id": run_id, "scaffold_seeded": seeded})
    return {"tasks": tasks, "started": True, "total": len(tasks), "first_run_id": run_id,
            "scaffold_seeded": seeded}


def start_web_app_build(engine, settings: Settings, jobs, *, project_id: str,
                        task_id: Optional[str], directive: str, owner: str, repo: str,
                        review: bool = False) -> dict:
    """SINGLE-TASK web-app build (the robust default for a website). Seed a deterministic
    Vite+React+TS+Tailwind scaffold (zero agent turns), then ONE governed task builds the whole app
    feature on top — a handful of files that fit comfortably under the guest's ~40-turn cap. No
    decomposition chain, no coordination. review=False runs it light (tier 1: no adversarial gate)
    so the FIRST pass just works; re-run with review=True to harden."""
    root = make_id("crun")
    seeded = seed_web_scaffold(settings, work_branch=f"boss/code/{root}", name=(repo or "app"))
    instructions = (
        "A buildable Vite + React + TypeScript + Tailwind + shadcn-style scaffold ALREADY EXISTS on "
        "this branch: package.json (scripts: dev='vite --host', build='tsc -b && vite build', "
        "typecheck, lint, test:e2e), vite.config.ts, tailwind.config.ts, tsconfig.json, index.html, "
        "src/main.tsx, src/App.tsx (placeholder), src/index.css (design tokens), src/lib/utils.ts "
        "(cn), src/components/ui/button.tsx + card.tsx. Do NOT re-init the project or edit the "
        "config/build files.\n\n"
        "Build the WHOLE feature ON this scaffold in a FEW focused files (aim for < 8 new files): "
        "put types/logic under src/lib/, presentational pieces under src/components/, and WIRE the "
        "finished feature into src/App.tsx so `npm run dev` shows the working app. `npm run build` "
        "MUST pass.\n\n"
        "VISUAL DESIGN — match the brand's PERSONALITY, do not default to plain corporate:\n"
        "• You MAY (and for a creative/portfolio/design/events/food/kids brand you SHOULD) RETHEME "
        "the palette by editing the design tokens in src/index.css — change --accent and add extra "
        "vivid accent hues, and use rich gradients (bg-gradient-to-*), not one flat blue. For a "
        "design studio / portfolio go BOLD and colorful (multi-hue gradients, vibrant sections, "
        "large expressive type); for finance/enterprise stay restrained. Read the domain and choose.\n"
        "• Add a distinctive Google Font via a <link> in index.html (e.g. a display font for "
        "headings + a clean body font) and set it in the Tailwind/CSS theme.\n"
        "• Add tasteful motion: CSS transitions on hover, subtle keyframe/scroll reveals, card lift "
        "shadows. Generous spacing, rounded corners, strong visual hierarchy.\n"
        "IMAGERY — never ship empty flat sections. Use real placeholder images (NO API key needed):\n"
        "• https://picsum.photos/seed/<word>/<w>/<h> (reliable) or "
        "https://source.unsplash.com/<w>x<h>/?<keywords> for heroes, galleries, cards. For a "
        "portfolio, build an image GRID/masonry of these. Leave a clear code comment where the owner "
        "swaps in their real photos.\n"
        "INTEGRATIONS (wire only if the app needs them):\n"
        "• PAYMENTS (Stripe): add a 'Buy / Book / Pay' button that opens a Stripe Payment Link — put "
        "the URL in a clearly-marked config const (e.g. STRIPE_PAYMENT_LINK = 'https://buy.stripe.com/"
        "test_XXXX') for the owner to replace. NO backend needed. (Only if a real cart/dynamic price "
        "is required, note that a serverless checkout endpoint would be a follow-up.)\n"
        "• DATABASE / PERSISTENCE (Supabase): if the app must SAVE data (leads, quote/booking "
        "requests, form submissions, results) use Supabase (`@supabase/supabase-js`, add it to "
        "package.json) — a hosted Postgres the frontend talks to DIRECTLY, NO backend server. Put "
        "the keys in clearly-marked consts for the owner to replace:\n"
        "    const SUPABASE_URL = 'https://YOUR_PROJECT.supabase.co'; // TODO owner replaces\n"
        "    const SUPABASE_ANON_KEY = 'YOUR_ANON_KEY'; // TODO owner replaces\n"
        "  Create the client and `insert` submissions into a table (e.g. 'leads'). CRITICAL: if the "
        "keys are still the YOUR_… placeholders, DO NOT call Supabase — keep the mock success state "
        "so the app still builds and works out of the box; only hit the DB once real keys are set. "
        "Leave a code comment noting the SQL for the table the owner should create.\n\n"
        "THE APP TO BUILD:\n" + directive)
    with begin(engine) as c:
        run_id = start_code_task(
            c, project_id=project_id, task_id=task_id,
            title=("Web app — " + directive[:70]).strip(),
            instructions=instructions,
            acceptance_criteria=[
                "npm run build passes (tsc clean + vite production build).",
                "The app renders a working, professional UI wired into src/App.tsx; the core "
                "interaction produces a correct, sensible result on screen.",
                "Only a handful of new files were added on top of the scaffold; config/build files "
                "were not rewritten.",
            ],
            test_commands=["npm run build"], owner=owner, repo=repo,
            base_branch=f"boss/code/{root}",
            finalize_ctx={"root_run_id": root, "web_app_build": True},
            # tier 0 (review=False): implement ONLY — no design, no separate write_tests stage
            # (which mis-generated Python unittest files for this JS/React app), no adversarial
            # gate. The guest validates itself with `npm run build`. review=True -> tier 2 hardens.
            jobs=jobs, profile="balanced", risk_tier=(2 if review else 0))
        emit_event(c, project_id, "web_app.build_started", "boss", "code_task_run", run_id,
                   {"scaffold_seeded": seeded, "review": review, "branch": f"boss/code/{root}"})
    return {"started": True, "run_id": run_id, "scaffold_seeded": seeded, "review": review,
            "branch": f"boss/code/{root}"}


def advance_decomposition(engine, settings: Settings, jobs, run_id: str) -> Optional[str]:
    """Called when a decomposed task lands. If the run carries a remaining plan, start the NEXT
    task as a continuation (same lineage/branch, so it builds on the just-finished code). Returns
    the new run_id, or None if the plan is complete / this isn't a decomposition run."""
    with begin(engine) as c:
        run = load_code_task_run(c, run_id)
    if run is None:
        return None
    fctx = run.get("finalize_ctx") or {}
    plan = list(fctx.get("decomposition_plan") or [])
    if not plan:
        return None
    nxt, remaining = plan[0], plan[1:]
    idx = int(fctx.get("decomposition_index", 1)) + 1
    total = int(fctx.get("decomposition_total", idx))
    root = fctx.get("root_run_id") or run_id  # keep the whole build in ONE branch lineage
    spec = run.get("spec") or {}
    # IDEMPOTENCY: a long run can be reclaimed/re-invoked, so advance() may be called more than
    # once for the same completed step. Never start a DUPLICATE next step — bail if a run for this
    # lineage+index already exists.
    with begin(engine) as c:
        dup = c.execute(text(
            "SELECT id FROM code_task_runs WHERE project_id = :p "
            "AND finalize_ctx->>'root_run_id' = :root "
            "AND (finalize_ctx->>'decomposition_index') = :idx LIMIT 1"),
            {"p": run["project_id"], "root": str(root), "idx": str(idx)}).first()
    if dup is not None:
        return None
    with begin(engine) as c:
        new_id = start_code_task(
            c, project_id=run["project_id"], task_id=run.get("task_id"),
            title=f"[{idx}/{total}] {nxt['title']}",
            instructions=("This task BUILDS ON the code already produced by the earlier tasks in "
                          "this project (present in your working tree on the shared branch). Add to "
                          "it; do not rebuild from scratch.\n\n" + nxt["instructions"]),
            acceptance_criteria=nxt.get("acceptance_criteria") or [],
            test_commands=(["npm run build"] if fctx.get("web_app_build") else []),
            owner=spec.get("owner") or settings.target_github_owner,
            repo=spec.get("repo") or settings.target_github_repo,
            base_branch=spec.get("base_branch") or "main",
            finalize_ctx={"decomposition_plan": remaining, "decomposition_index": idx,
                          "decomposition_total": total, "root_run_id": root,
                          # carry the web-app flag down the chain so every slice skips write_tests
                          "web_app_build": bool(fctx.get("web_app_build")),
                          "attempt": int(fctx.get("attempt", 1))},
            jobs=jobs, profile=spec.get("profile"), risk_tier=spec.get("risk_tier"))
        emit_event(c, run["project_id"], "prd.decomposition_advanced", "boss", "code_task_run",
                   new_id, {"step": idx, "total": total, "title": nxt["title"],
                            "prev_run_id": run_id})
    return new_id
