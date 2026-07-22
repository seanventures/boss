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
from .core import emit_event

logger = logging.getLogger(__name__)

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
                           risk_tier: Optional[int] = None, max_tasks: int = 6) -> dict:
    """Decompose the PRD and START the build as one lineage: task 1 is the root run (carries the
    full ordered plan in its finalize_ctx); each subsequent task auto-starts as a continuation when
    the previous one lands (see advance_decomposition). Returns the plan + the first run_id."""
    tasks = decompose_prd_into_tasks(settings, prd_or_directive, max_tasks=max_tasks)
    if not tasks:
        return {"tasks": [], "started": False, "reason": "decomposition produced no tasks"}
    first, rest = tasks[0], tasks[1:]
    with begin(engine) as c:
        run_id = start_code_task(
            c, project_id=project_id, task_id=task_id,
            title=f"[1/{len(tasks)}] {first['title']}",
            instructions=first["instructions"],
            acceptance_criteria=first["acceptance_criteria"], test_commands=[],
            owner=owner, repo=repo, base_branch=base_branch,
            finalize_ctx={"decomposition_plan": rest, "decomposition_index": 1,
                          "decomposition_total": len(tasks)},
            jobs=jobs, profile=profile, risk_tier=risk_tier)
        emit_event(c, project_id, "prd.decomposed", "boss", "code_task_run", run_id,
                   {"total_tasks": len(tasks),
                    "titles": [t["title"] for t in tasks], "first_run_id": run_id})
    return {"tasks": tasks, "started": True, "total": len(tasks), "first_run_id": run_id}


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
    with begin(engine) as c:
        new_id = start_code_task(
            c, project_id=run["project_id"], task_id=run.get("task_id"),
            title=f"[{idx}/{total}] {nxt['title']}",
            instructions=("This task BUILDS ON the code already produced by the earlier tasks in "
                          "this project (present in your working tree on the shared branch). Add to "
                          "it; do not rebuild from scratch.\n\n" + nxt["instructions"]),
            acceptance_criteria=nxt.get("acceptance_criteria") or [], test_commands=[],
            owner=spec.get("owner") or settings.target_github_owner,
            repo=spec.get("repo") or settings.target_github_repo,
            base_branch=spec.get("base_branch") or "main",
            finalize_ctx={"decomposition_plan": remaining, "decomposition_index": idx,
                          "decomposition_total": total, "root_run_id": root,
                          "attempt": int(fctx.get("attempt", 1))},
            jobs=jobs, profile=spec.get("profile"), risk_tier=spec.get("risk_tier"))
        emit_event(c, run["project_id"], "prd.decomposition_advanced", "boss", "code_task_run",
                   new_id, {"step": idx, "total": total, "title": nxt["title"],
                            "prev_run_id": run_id})
    return new_id
