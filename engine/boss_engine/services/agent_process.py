"""Agent process runtime — authorable sequences of role handoffs.

This sits on top of existing BOSS capabilities (PRD text, factory, scorecard,
captain validation). It does not replace the DAG workflow registry.

A process is a published template: ordered/parallel role steps with named
actions. The default template is PRD → architect/CTO/VP-PM review → improve
→ engineering captain build → QA → UI/UX → captain verify → panel certify
→ captain accept.

Hard rules:
  * Every handoff is recorded. Spendy build needs a Receipt / code.task.
  * Decisions stay human. Certification cannot invent a passing scorecard.
  * Simulated/dry-run never enqueues a budgeted factory job.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import Settings
from .core import emit_event, make_id
from .receipts import ReceiptService


PROCESS_STATUSES = frozenset({
    "draft", "queued", "running", "waiting", "blocked",
    "completed", "failed", "cancelled",
})
STEP_STATUSES = frozenset({
    "pending", "ready", "running", "waiting", "succeeded", "failed", "skipped",
})
ROLES = frozenset({
    "product_manager",
    "vp_product",
    "chief_architect",
    "cto",
    "engineering_captain",
    "qa",
    "ux_engineer",
})
ACTIONS = frozenset({
    "accept_prd",
    "review_prd",
    "improve_prd",
    "build",
    "qa_review",
    "ux_review",
    "verify_build",
    "certify_spec",
    "accept_work",
})

PRD_TO_ACCEPTED_SPEC: dict[str, Any] = {
    "key": "prd_to_accepted_work",
    "name": "PRD to accepted work",
    "description": (
        "PRD is reviewed by architect, CTO, and VP product, improved, built by "
        "the engineering captain, checked by QA and UI/UX, verified, panel-"
        "certified, then accepted by the captain."
    ),
    "entry": "intake_prd",
    "steps": [
        {
            "id": "intake_prd",
            "kind": "role",
            "role": "product_manager",
            "action": "accept_prd",
            "next": ["review_architect", "review_cto", "review_vp"],
        },
        {
            "id": "review_architect",
            "kind": "role",
            "role": "chief_architect",
            "action": "review_prd",
            "next": ["improve_prd"],
        },
        {
            "id": "review_cto",
            "kind": "role",
            "role": "cto",
            "action": "review_prd",
            "next": ["improve_prd"],
        },
        {
            "id": "review_vp",
            "kind": "role",
            "role": "vp_product",
            "action": "review_prd",
            "next": ["improve_prd"],
        },
        {
            "id": "improve_prd",
            "kind": "role",
            "role": "product_manager",
            "action": "improve_prd",
            "wait_for": ["review_architect", "review_cto", "review_vp"],
            "next": ["captain_build"],
        },
        {
            "id": "captain_build",
            "kind": "role",
            "role": "engineering_captain",
            "action": "build",
            "next": ["qa_check"],
        },
        {
            "id": "qa_check",
            "kind": "role",
            "role": "qa",
            "action": "qa_review",
            "next": ["ux_review"],
        },
        {
            "id": "ux_review",
            "kind": "role",
            "role": "ux_engineer",
            "action": "ux_review",
            "next": ["captain_verify"],
        },
        {
            "id": "captain_verify",
            "kind": "role",
            "role": "engineering_captain",
            "action": "verify_build",
            "next": ["panel_certify"],
        },
        {
            "id": "panel_certify",
            "kind": "panel",
            "roles": ["chief_architect", "cto", "qa", "ux_engineer"],
            "action": "certify_spec",
            "next": ["captain_accept"],
        },
        {
            "id": "captain_accept",
            "kind": "role",
            "role": "engineering_captain",
            "action": "accept_work",
            "next": [],
        },
    ],
}

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS agent_process_templates (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  process_key TEXT NOT NULL,
  name TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  spec JSONB NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  created_by TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(project_id, process_key)
);
CREATE INDEX IF NOT EXISTS idx_agent_process_templates_project
  ON agent_process_templates(project_id, status, updated_at DESC);
CREATE TABLE IF NOT EXISTS agent_process_runs (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  template_id TEXT NOT NULL REFERENCES agent_process_templates(id) ON DELETE CASCADE,
  title TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'queued',
  actor TEXT NOT NULL,
  input JSONB NOT NULL DEFAULT '{}'::jsonb,
  context JSONB NOT NULL DEFAULT '{}'::jsonb,
  current_steps JSONB NOT NULL DEFAULT '[]'::jsonb,
  receipt_id TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_agent_process_runs_project
  ON agent_process_runs(project_id, status, updated_at DESC);
CREATE TABLE IF NOT EXISTS agent_process_steps (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES agent_process_runs(id) ON DELETE CASCADE,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  step_key TEXT NOT NULL,
  role_key TEXT NOT NULL,
  action TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  input JSONB NOT NULL DEFAULT '{}'::jsonb,
  output JSONB NOT NULL DEFAULT '{}'::jsonb,
  handoff_from TEXT,
  receipt_id TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_agent_process_steps_run
  ON agent_process_steps(run_id, created_at);
"""


def validate_process_spec(spec: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(spec, dict):
        raise ValueError("process spec must be an object")
    steps = spec.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ValueError("process spec needs at least one step")
    ids: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for raw in steps:
        if not isinstance(raw, dict):
            raise ValueError("every step must be an object")
        step_id = str(raw.get("id") or "").strip()
        if not step_id or step_id in ids:
            raise ValueError(f"invalid or duplicate step id {step_id!r}")
        ids.add(step_id)
        kind = str(raw.get("kind") or "role").strip() or "role"
        if kind not in {"role", "panel"}:
            raise ValueError(f"step {step_id} kind must be role or panel")
        action = str(raw.get("action") or "").strip()
        if action not in ACTIONS:
            raise ValueError(f"step {step_id} unknown action {action!r}")
        roles = [str(item) for item in (raw.get("roles") or [])]
        role = str(raw.get("role") or "")
        if kind == "role":
            if role not in ROLES:
                raise ValueError(f"step {step_id} unknown role {role!r}")
            roles = [role]
        else:
            if not roles:
                raise ValueError(f"panel step {step_id} needs roles")
            for item in roles:
                if item not in ROLES:
                    raise ValueError(f"step {step_id} unknown role {item!r}")
        nxt = raw.get("next") or []
        if isinstance(nxt, str):
            nxt = [nxt]
        wait_for = raw.get("wait_for") or []
        if isinstance(wait_for, str):
            wait_for = [wait_for]
        normalized.append({
            "id": step_id,
            "kind": kind,
            "role": roles[0],
            "roles": roles,
            "action": action,
            "next": [str(item) for item in nxt],
            "wait_for": [str(item) for item in wait_for],
        })
    entry = str(spec.get("entry") or normalized[0]["id"])
    known = {item["id"] for item in normalized}
    if entry not in known:
        raise ValueError(f"entry {entry} does not exist")
    for step in normalized:
        for target in step["next"] + step["wait_for"]:
            if target not in known:
                raise ValueError(f"step {step['id']} references missing {target}")
    return {
        "key": str(spec.get("key") or "custom"),
        "name": str(spec.get("name") or "Custom process"),
        "description": str(spec.get("description") or ""),
        "entry": entry,
        "steps": normalized,
    }


def review_prd(prd: str, role: str) -> dict[str, Any]:
    """Deterministic role review. Findings are evidence, not invented scores."""
    text = prd or ""
    lowered = text.lower()
    checks: list[dict[str, Any]] = []

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": ok, "detail": detail})

    check("nonempty", bool(text.strip()), "PRD has body" if text.strip() else "PRD is empty")
    if role == "chief_architect":
        check("data_or_components", bool(re.search(r"\b(data|schema|component|api|service)\b", lowered)), "architecture surface named")
        check("risks", bool(re.search(r"\b(risks?|constraints?|assumptions?|non-functional)\b", lowered)), "risks or constraints present")
    elif role == "cto":
        check("security_or_delivery", bool(re.search(r"\b(security|auth|deploy|sla|budget|test)\b", lowered)), "delivery/security mentioned")
        check("acceptance", bool(re.search(r"\b(acceptance|must |shall |verify)\b", lowered)), "acceptance language present")
    elif role in {"vp_product", "product_manager"}:
        check("user", bool(re.search(r"\b(user|customer|operator|requester)\b", lowered)), "user named")
        check("success", bool(re.search(r"\b(success|metric|goal|outcome)\b", lowered)), "success criteria present")
    else:
        check("acceptance", bool(re.search(r"\b(acceptance|must )\b", lowered)), "acceptance present")
    failed = [item["name"] for item in checks if not item["ok"]]
    verdict = "revise" if failed else "approve"
    return {
        "role": role,
        "verdict": verdict,
        "checks": checks,
        "failed": failed,
        "findings": [item["detail"] for item in checks if not item["ok"]],
    }


def improve_prd(prd: str, reviews: list[dict[str, Any]]) -> str:
    body = (prd or "").rstrip()
    findings: list[str] = []
    for review in reviews:
        for item in review.get("findings") or []:
            findings.append(f"- [{review.get('role')}] {item}")
    if not findings:
        return body + "\n\n## Review outcome\nAll reviewers approved. No structural gaps recorded.\n"
    return (
        body
        + "\n\n## Improvements required by review\n"
        + "\n".join(findings)
        + "\n\nThese gaps must be addressed in the engineering buildout.\n"
    )


class AgentProcessService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.receipts = ReceiptService()
        self._schema_ready = False

    def ensure_schema(self, connection: Connection) -> None:
        if self._schema_ready:
            return
        for statement in (chunk.strip() for chunk in SCHEMA_SQL.split(";")):
            if statement:
                connection.exec_driver_sql(statement)
        self._schema_ready = True

    def ensure_default_template(self, connection: Connection, *, project_id: str, actor: str) -> dict[str, Any]:
        self.ensure_schema(connection)
        existing = self.get_template_by_key(connection, project_id, PRD_TO_ACCEPTED_SPEC["key"])
        if existing:
            return existing
        return self.create_template(
            connection,
            project_id=project_id,
            actor=actor,
            process_key=PRD_TO_ACCEPTED_SPEC["key"],
            name=PRD_TO_ACCEPTED_SPEC["name"],
            description=PRD_TO_ACCEPTED_SPEC["description"],
            spec=PRD_TO_ACCEPTED_SPEC,
        )

    def create_template(
        self, connection: Connection, *, project_id: str, actor: str,
        process_key: str, name: str, description: str, spec: dict[str, Any],
    ) -> dict[str, Any]:
        self.ensure_schema(connection)
        normalized = validate_process_spec(spec)
        key = re.sub(r"[^a-z0-9_]+", "_", (process_key or normalized["key"]).lower()).strip("_") or "custom"
        row = connection.execute(
            text(
                """
                INSERT INTO agent_process_templates
                  (id, project_id, process_key, name, description, spec, created_by)
                VALUES (:id, :p, :key, :name, :d, CAST(:spec AS JSONB), :actor)
                RETURNING *
                """
            ),
            {
                "id": make_id("aproc"),
                "p": project_id,
                "key": key[:80],
                "name": (name or normalized["name"])[:160],
                "d": (description or normalized["description"])[:1000],
                "spec": json.dumps(normalized),
                "actor": actor,
            },
        ).mappings().one()
        emit_event(connection, project_id, "agent_process.template_created", actor,
                   "agent_process_template", row["id"], {"process_key": key})
        return self._row(row)

    def list_templates(self, connection: Connection, project_id: str) -> list[dict[str, Any]]:
        self.ensure_schema(connection)
        self.ensure_default_template(connection, project_id=project_id, actor="system")
        rows = connection.execute(
            text("SELECT * FROM agent_process_templates WHERE project_id=:p ORDER BY updated_at DESC"),
            {"p": project_id},
        ).mappings().all()
        return [self._row(row) for row in rows]

    def get_template_by_key(self, connection: Connection, project_id: str, process_key: str) -> Optional[dict[str, Any]]:
        self.ensure_schema(connection)
        row = connection.execute(
            text("SELECT * FROM agent_process_templates WHERE project_id=:p AND process_key=:k"),
            {"p": project_id, "k": process_key},
        ).mappings().first()
        return self._row(row) if row else None

    def start_run(
        self, connection: Connection, *, project_id: str, actor: str,
        prd: str, title: str = "", process_key: str = "prd_to_accepted_work",
        simulated: bool = False, auto_build: bool = False,
        origin: str | None = None,
        attach_run_id: str | None = None,
        official: bool = False,
        engine: str | None = None,
        review_panel_size: int | None = None,
        qa_benchmark: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        template = self.get_template_by_key(connection, project_id, process_key)
        if template is None:
            template = self.ensure_default_template(connection, project_id=project_id, actor=actor)
        spec = template.get("spec") or {}
        if isinstance(spec, str):
            spec = json.loads(spec)
        if not (prd or "").strip():
            raise ValueError("prd is required")
        run = connection.execute(
            text(
                """
                INSERT INTO agent_process_runs
                  (id, project_id, template_id, title, status, actor, input, context, current_steps)
                VALUES (:id, :p, :t, :title, 'queued', :actor, CAST(:inp AS JSONB), CAST(:ctx AS JSONB), CAST(:cur AS JSONB))
                RETURNING *
                """
            ),
            {
                "id": make_id("aprun"),
                "p": project_id,
                "t": template["id"],
                "title": (title or "PRD delivery")[:200],
                "actor": actor,
                "inp": json.dumps({
                    "prd": prd,
                    "simulated": bool(simulated),
                    "auto_build": bool(auto_build),
                    **({"origin": origin} if origin else {}),
                    "attach_run_id": attach_run_id,
                    "official": bool(official),
                    "engine": engine,
                    "review_panel_size": review_panel_size,
                    "qa_benchmark": qa_benchmark if isinstance(qa_benchmark, dict) else None,
                }),
                "ctx": json.dumps({
                    "prd": prd,
                    "reviews": [],
                    "simulated": bool(simulated),
                    "run_id": attach_run_id,
                    "build_status": "queued" if attach_run_id else None,
                    "official": bool(official),
                }),
                "cur": json.dumps([spec.get("entry")]),
            },
        ).mappings().one()
        emit_event(connection, project_id, "agent_process.started", actor,
                   "agent_process_run", run["id"], {"process_key": template.get("process_key")})
        receipt = self._record_process_receipt(
            connection,
            project_id=project_id,
            run_id=run["id"],
            step_key="start",
            payload={
                "process_key": template.get("process_key"),
                "simulated": bool(simulated),
                "auto_build": bool(auto_build),
                "title": run["title"],
            },
        )
        if receipt.get("id"):
            connection.execute(
                text("UPDATE agent_process_runs SET receipt_id=:r WHERE id=:id"),
                {"r": receipt["id"], "id": run["id"]},
            )
        return self.advance(connection, project_id=project_id, actor=actor, run_id=run["id"])

    def get_run(self, connection: Connection, project_id: str, run_id: str) -> dict[str, Any]:
        self.ensure_schema(connection)
        row = connection.execute(
            text("SELECT * FROM agent_process_runs WHERE id=:id AND project_id=:p"),
            {"id": run_id, "p": project_id},
        ).mappings().first()
        if row is None:
            raise ValueError("process run not found")
        out = self._row(row)
        out["steps"] = self.list_steps(connection, run_id)
        return out

    def list_runs(self, connection: Connection, project_id: str, limit: int = 30) -> list[dict[str, Any]]:
        self.ensure_schema(connection)
        rows = connection.execute(
            text(
                """
                SELECT r.*, t.process_key, t.name AS template_name
                FROM agent_process_runs r
                JOIN agent_process_templates t ON t.id = r.template_id
                WHERE r.project_id=:p
                ORDER BY r.updated_at DESC LIMIT :n
                """
            ),
            {"p": project_id, "n": limit},
        ).mappings().all()
        return [self._row(row) for row in rows]

    def list_steps(self, connection: Connection, run_id: str) -> list[dict[str, Any]]:
        rows = connection.execute(
            text("SELECT * FROM agent_process_steps WHERE run_id=:r ORDER BY created_at"),
            {"r": run_id},
        ).mappings().all()
        return [self._row(row) for row in rows]

    def list_active(self, connection: Connection, project_id: str) -> list[dict[str, Any]]:
        self.ensure_schema(connection)
        rows = connection.execute(
            text(
                """
                SELECT id FROM agent_process_runs
                WHERE project_id=:p AND status IN ('queued','running','waiting')
                ORDER BY updated_at
                """
            ),
            {"p": project_id},
        ).scalars().all()
        return [self.get_run(connection, project_id, run_id) for run_id in rows]

    def advance(
        self, connection: Connection, *, project_id: str, actor: str, run_id: str,
    ) -> dict[str, Any]:
        self.ensure_schema(connection)
        run = self.get_run(connection, project_id, run_id)
        if run["status"] in {"completed", "failed", "cancelled"}:
            return run
        template = connection.execute(
            text("SELECT * FROM agent_process_templates WHERE id=:id"),
            {"id": run["template_id"]},
        ).mappings().one()
        spec = template["spec"]
        if isinstance(spec, str):
            spec = json.loads(spec)
        spec = validate_process_spec(spec)
        done = {item["step_key"]: item for item in run.get("steps") or []}
        progressed = True
        safety = 0
        while progressed and safety < 40:
            safety += 1
            progressed = False
            ready = self._ready_steps(spec, done)
            if not ready:
                break
            for step in ready:
                if step["id"] in done and done[step["id"]].get("status") in {"succeeded", "failed", "waiting"}:
                    continue
                result = self._execute_step(
                    connection, project_id=project_id, actor=actor,
                    run=run, step=step, done=done,
                )
                recorded = self._record_step(
                    connection, run=run, step=step, result=result, actor=actor,
                )
                done[step["id"]] = recorded
                progressed = True
                run = self.get_run(connection, project_id, run_id)
                if result.get("status") == "waiting":
                    self._set_run_status(connection, run_id, "waiting", [step["id"]])
                    return self.get_run(connection, project_id, run_id)
                if result.get("status") == "failed":
                    self._set_run_status(connection, run_id, "failed", [step["id"]])
                    return self.get_run(connection, project_id, run_id)
        accept = done.get("captain_accept") or {}
        if accept.get("status") == "succeeded":
            self._set_run_status(connection, run_id, "completed", [])
        elif any(item.get("status") == "waiting" for item in done.values()):
            waiting = [key for key, item in done.items() if item.get("status") == "waiting"]
            self._set_run_status(connection, run_id, "waiting", waiting)
        else:
            current = [item["id"] for item in self._ready_steps(spec, done)]
            self._set_run_status(connection, run_id, "running" if current or done else "queued", current)
        return self.get_run(connection, project_id, run_id)

    def tick_due(self, connection: Connection, *, project_id: str, actor: str) -> dict[str, Any]:
        ran = []
        for item in self.list_active(connection, project_id):
            ran.append(self.advance(
                connection, project_id=project_id, actor=actor, run_id=item["id"],
            ))
        return {"project_id": project_id, "ran": len(ran), "runs": [row["id"] for row in ran]}

    def _ready_steps(self, spec: dict[str, Any], done: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        ready: list[dict[str, Any]] = []
        for step in spec["steps"]:
            existing = done.get(step["id"])
            if existing and existing.get("status") in {"succeeded", "failed"}:
                continue
            if existing and existing.get("status") == "waiting":
                ready.append(step)
                continue
            wait_for = step.get("wait_for") or []
            predecessors = wait_for or self._predecessors(spec, step["id"])
            if any((done.get(item) or {}).get("status") != "succeeded" for item in predecessors):
                if predecessors:
                    continue
            if not predecessors and spec.get("entry") != step["id"] and step["id"] not in done:
                # reachable only via next pointers
                inbound = [
                    other["id"] for other in spec["steps"]
                    if step["id"] in (other.get("next") or [])
                ]
                if inbound and any((done.get(item) or {}).get("status") != "succeeded" for item in inbound):
                    continue
            ready.append(step)
        return ready

    @staticmethod
    def _predecessors(spec: dict[str, Any], step_id: str) -> list[str]:
        return [
            other["id"] for other in spec["steps"]
            if step_id in (other.get("next") or [])
        ]

    def _execute_step(
        self, connection: Connection, *, project_id: str, actor: str,
        run: dict[str, Any], step: dict[str, Any], done: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        ctx = run.get("context") or {}
        if isinstance(ctx, str):
            ctx = json.loads(ctx)
        inp = run.get("input") or {}
        if isinstance(inp, str):
            inp = json.loads(inp)
        action = step["action"]
        prd = str(ctx.get("prd") or inp.get("prd") or "")
        if action == "accept_prd":
            return {"status": "succeeded", "verdict": "accepted", "prd": prd, "role": step["role"]}
        if action == "review_prd":
            review = review_prd(prd, step["role"])
            reviews = list(ctx.get("reviews") or [])
            reviews.append(review)
            self._patch_context(connection, run["id"], {"reviews": reviews})
            return {"status": "succeeded", **review}
        if action == "improve_prd":
            reviews = list(ctx.get("reviews") or [])
            improved = improve_prd(prd, reviews)
            self._patch_context(connection, run["id"], {"prd": improved, "prd_improved": True})
            return {"status": "succeeded", "verdict": "improved", "prd": improved}
        if action == "build":
            return self._execute_build(
                connection, project_id=project_id, actor=actor, run=run, ctx=ctx, inp=inp,
                process_run_id=str(run.get("id") or ""),
            )
        if action == "qa_review":
            return self._qa_review(ctx)
        if action == "ux_review":
            return self._ux_review(ctx)
        if action == "verify_build":
            return self._captain_verify(ctx)
        if action == "certify_spec":
            return self._panel_certify(done, ctx)
        if action == "accept_work":
            panel = done.get("panel_certify") or {}
            output = panel.get("output") if isinstance(panel.get("output"), dict) else {}
            verdict = output.get("verdict") or panel.get("verdict")
            if verdict not in {"certified", "certified_planned"}:
                return {
                    "status": "failed",
                    "verdict": "rejected",
                    "reason": "panel did not certify",
                }
            return {"status": "succeeded", "verdict": "accepted", "closed": True}
        return {"status": "failed", "reason": f"unknown action {action}"}

    def _execute_build(
        self, connection: Connection, *, project_id: str, actor: str,
        run: dict[str, Any], ctx: dict[str, Any], inp: dict[str, Any],
        process_run_id: str = "",
    ) -> dict[str, Any]:
        process_run_id = process_run_id or str(run.get("id") or "")
        attach = str(inp.get("attach_run_id") or ctx.get("run_id") or "").strip()
        if attach:
            self._patch_context(connection, process_run_id, {
                "run_id": attach,
                "build_status": ctx.get("build_status") or "queued",
                "attached": True,
                "process_run_id": process_run_id,
            })
            ctx = {**ctx, "run_id": attach, "process_run_id": process_run_id}
            return self._refresh_build(
                connection, project_id, ctx, process_run_id=process_run_id,
            )
        if ctx.get("run_id") and ctx.get("build_status") in {"queued", "running", "repairing"}:
            return self._refresh_build(
                connection, project_id, ctx, process_run_id=process_run_id,
            )
        if inp.get("simulated") or not inp.get("auto_build"):
            self._patch_context(connection, process_run_id, {
                "build_status": "planned",
                "run_id": None,
                "simulated": bool(inp.get("simulated")),
            })
            return {
                "status": "succeeded",
                "verdict": "planned",
                "detail": "Build recorded as planned (simulated or auto_build=false). No budgeted code.task.",
            }
        try:
            from .code_task_service import start_code_task
            from .jobs import JobLeaseService

            prd = str(ctx.get("prd") or inp.get("prd") or "")
            official = bool(inp.get("official") or ctx.get("official"))
            bench = inp.get("qa_benchmark") if isinstance(inp.get("qa_benchmark"), dict) else None
            fctx: dict[str, Any] = {
                "source": "agent_process",
                "process_run_id": process_run_id,
                "actor": actor,
            }
            if official:
                fctx["official"] = True
            if bench:
                fctx["qa_benchmark"] = dict(bench)
            engine_name = str(inp.get("engine") or "").strip().lower()
            model_config = None
            if engine_name:
                from .spend_cap_failover import pin_engine_model_config

                model_config = pin_engine_model_config(engine_name, None)
            panel = inp.get("review_panel_size")
            run_id = start_code_task(
                connection,
                project_id=project_id,
                task_id=None,
                title=f"Process: {run.get('title') or 'PRD build'}"[:200],
                instructions=prd,
                acceptance_criteria=["npm run verify or equivalent exits 0"],
                test_commands=["npm run verify"],
                owner=self.settings.target_github_owner,
                repo=self.settings.target_github_repo,
                base_branch="main",
                finalize_ctx=fctx,
                jobs=JobLeaseService(self.settings),
                open_pr=True,
                risk_tier=1,
                model_config=model_config,
                review_panel_size=int(panel) if isinstance(panel, int) else (5 if official else None),
                repair_retries=2 if official else None,
            )
        except Exception as exc:  # noqa: BLE001
            return {"status": "failed", "verdict": "blocked", "reason": f"factory launch failed: {exc}"}
        self._patch_context(connection, process_run_id, {
            "run_id": run_id,
            "build_status": "queued",
            "process_run_id": process_run_id,
        })
        return {"status": "waiting", "verdict": "queued", "run_id": run_id}

    def _refresh_build(
        self, connection: Connection, project_id: str, ctx: dict[str, Any],
        process_run_id: str = "",
    ) -> dict[str, Any]:
        run_id = ctx.get("run_id")
        process_run_id = process_run_id or str(ctx.get("process_run_id") or "")
        try:
            row = connection.execute(
                text("SELECT status, state FROM code_task_runs WHERE id=:id AND project_id=:p"),
                {"id": run_id, "p": project_id},
            ).mappings().first()
        except Exception:
            row = None
        if row is None:
            return {"status": "waiting", "verdict": "queued", "run_id": run_id}
        status = row["status"]
        state = row.get("state") or {}
        if isinstance(state, str):
            try:
                state = json.loads(state)
            except json.JSONDecodeError:
                state = {}
        if status in {"queued", "running", "repairing"}:
            return {"status": "waiting", "verdict": status, "run_id": run_id}
        raw_tests = state.get("tests_passed")
        if isinstance(raw_tests, bool):
            tests_passed = raw_tests
        elif raw_tests is None:
            tests_passed = None
        else:
            tests_passed = bool(raw_tests)
        ui_evidence = state.get("ui_evidence") or {}
        ux_evidence = state.get("ux_evidence") or {}
        self._patch_context(connection, process_run_id, {
            "tests_passed": tests_passed,
            "ui_evidence": ui_evidence,
            "ux_evidence": ux_evidence,
            "build_status": status,
        })
        return {
            "status": "succeeded" if status == "completed" else "failed",
            "verdict": status,
            "run_id": run_id,
            "tests_passed": tests_passed,
            "ui_evidence": ui_evidence,
            "ux_evidence": ux_evidence,
            "build_status": status,
        }

    @staticmethod
    def _qa_review(ctx: dict[str, Any]) -> dict[str, Any]:
        if ctx.get("simulated") or ctx.get("build_status") == "planned":
            return {
                "status": "succeeded",
                "verdict": "checked",
                "detail": "QA recorded planned/simulated build. No invented pass.",
                "tests_passed": None,
            }
        tests = ctx.get("tests_passed")
        if tests is False:
            return {"status": "failed", "verdict": "qa_failed", "tests_passed": False}
        return {"status": "succeeded", "verdict": "qa_passed" if tests else "qa_observed", "tests_passed": tests}

    @staticmethod
    def _ux_review(ctx: dict[str, Any]) -> dict[str, Any]:
        ui = ctx.get("ui_evidence") or {}
        ux = ctx.get("ux_evidence") or {}
        states = ui.get("states_covered") or []
        return {
            "status": "succeeded",
            "verdict": "reviewed",
            "states_covered": states,
            "has_e2e": bool(ux.get("has_e2e")),
            "detail": "UX review records observed evidence only. Missing rubric stays not_scored.",
        }

    @staticmethod
    def _captain_verify(ctx: dict[str, Any]) -> dict[str, Any]:
        if ctx.get("simulated") or ctx.get("build_status") == "planned":
            return {"status": "succeeded", "verdict": "verified_planned", "detail": "Captain verified the planned handoff. No live preview invented."}
        tests = ctx.get("tests_passed")
        if tests is False:
            return {"status": "failed", "verdict": "verify_failed", "reason": "tests_passed is false"}
        return {"status": "succeeded", "verdict": "verified", "tests_passed": tests}

    @staticmethod
    def _step_payload(done: dict[str, dict[str, Any]], key: str) -> dict[str, Any]:
        item = done.get(key) or {}
        output = item.get("output")
        merged = dict(item)
        if isinstance(output, dict):
            merged.update(output)
        return merged

    @staticmethod
    def _panel_checklist(done: dict[str, dict[str, Any]], ctx: dict[str, Any]) -> list[dict[str, Any]]:
        """Observed evidence only. No invented scores."""
        prd = str(ctx.get("prd") or "")
        improved = AgentProcessService._step_payload(done, "improve_prd")
        qa = AgentProcessService._step_payload(done, "qa_check")
        ux = AgentProcessService._step_payload(done, "ux_review")
        verify = AgentProcessService._step_payload(done, "captain_verify")
        prd_improved = bool(
            ctx.get("prd_improved")
            or improved.get("verdict") == "improved"
            or "Improvements required by review" in prd
            or "Review outcome" in prd
            or bool(prd.strip())
        )
        return [
            {
                "name": "improved_prd",
                "ok": bool(prd.strip()) and prd_improved,
                "detail": "improved PRD present" if prd.strip() else "PRD missing",
            },
            {
                "name": "qa_output",
                "ok": qa.get("status") == "succeeded",
                "verdict": qa.get("verdict"),
                "tests_passed": qa.get("tests_passed"),
                "detail": qa.get("detail") or qa.get("verdict"),
            },
            {
                "name": "ux_output",
                "ok": ux.get("status") == "succeeded",
                "states_covered": ux.get("states_covered") or [],
                "has_e2e": ux.get("has_e2e"),
                "detail": ux.get("detail") or ux.get("verdict"),
            },
            {
                "name": "verify_output",
                "ok": verify.get("status") == "succeeded",
                "verdict": verify.get("verdict"),
                "tests_passed": verify.get("tests_passed"),
                "detail": verify.get("detail") or verify.get("verdict"),
            },
            {
                "name": "tests_passed",
                "ok": ctx.get("tests_passed") is True,
                "value": ctx.get("tests_passed"),
            },
        ]

    @staticmethod
    def _panel_certify(done: dict[str, dict[str, Any]], ctx: dict[str, Any]) -> dict[str, Any]:
        required = ["qa_check", "ux_review", "captain_verify"]
        missing = [key for key in required if (done.get(key) or {}).get("status") != "succeeded"]
        checklist = AgentProcessService._panel_checklist(done, ctx)
        if missing:
            return {
                "status": "failed",
                "verdict": "not_certified",
                "missing": missing,
                "checklist": checklist,
            }
        if ctx.get("simulated") or ctx.get("build_status") == "planned":
            return {
                "status": "succeeded",
                "verdict": "certified_planned",
                "detail": "Panel certified the process evidence. Simulated builds are not live software certification.",
                "checklist": checklist,
            }
        if ctx.get("tests_passed") is not True:
            return {
                "status": "failed",
                "verdict": "not_certified",
                "reason": "live certification requires tests_passed",
                "checklist": checklist,
            }
        return {
            "status": "succeeded",
            "verdict": "certified",
            "roles": ["chief_architect", "cto", "qa", "ux_engineer"],
            "checklist": checklist,
        }

    def _record_step(
        self, connection: Connection, *, run: dict[str, Any], step: dict[str, Any],
        result: dict[str, Any], actor: str,
    ) -> dict[str, Any]:
        existing = connection.execute(
            text("SELECT id FROM agent_process_steps WHERE run_id=:r AND step_key=:k"),
            {"r": run["id"], "k": step["id"]},
        ).scalar_one_or_none()
        receipt = self._record_process_receipt(
            connection,
            project_id=run["project_id"],
            run_id=run["id"],
            step_key=step["id"],
            payload={
                "action": step["action"],
                "role": step.get("role"),
                "status": result.get("status"),
                "verdict": result.get("verdict"),
            },
        )
        payload = {
            "id": existing or make_id("apstep"),
            "r": run["id"],
            "p": run["project_id"],
            "k": step["id"],
            "role": step.get("role") or ",".join(step.get("roles") or []),
            "action": step["action"],
            "st": result.get("status") or "failed",
            "out": json.dumps(result),
            "rid": receipt.get("id"),
        }
        if existing:
            row = connection.execute(
                text(
                    """
                    UPDATE agent_process_steps
                    SET status=:st, output=CAST(:out AS JSONB), receipt_id=:rid, updated_at=now()
                    WHERE id=:id
                    RETURNING *
                    """
                ),
                {"st": payload["st"], "out": payload["out"], "rid": payload["rid"], "id": existing},
            ).mappings().one()
        else:
            row = connection.execute(
                text(
                    """
                    INSERT INTO agent_process_steps
                      (id, run_id, project_id, step_key, role_key, action, status, output, handoff_from, receipt_id)
                    VALUES (:id, :r, :p, :k, :role, :action, :st, CAST(:out AS JSONB), :hf, :rid)
                    RETURNING *
                    """
                ),
                {**payload, "hf": (run.get("current_steps") or [None])[0] if isinstance(run.get("current_steps"), list) else None},
            ).mappings().one()
        emit_event(
            connection, run["project_id"], "agent_process.handoff", actor,
            "agent_process_step", row["id"],
            {"step": step["id"], "role": step.get("role"), "action": step["action"], "status": result.get("status"), "receipt_id": receipt.get("id")},
        )
        if result.get("run_id") or result.get("prd") or result.get("build_status"):
            patch = {}
            if result.get("run_id"):
                patch["run_id"] = result["run_id"]
            if result.get("tests_passed") is not None:
                patch["tests_passed"] = result["tests_passed"]
            if result.get("ui_evidence"):
                patch["ui_evidence"] = result["ui_evidence"]
            if result.get("ux_evidence"):
                patch["ux_evidence"] = result["ux_evidence"]
            if result.get("build_status"):
                patch["build_status"] = result["build_status"]
            if patch:
                self._patch_context(connection, run["id"], patch)
        return self._row(row)

    def _record_process_receipt(
        self, connection: Connection, *, project_id: str, run_id: str,
        step_key: str, payload: dict[str, Any],
    ) -> dict[str, Any]:
        scope = self._scope(connection, project_id)
        dedupe_key = f"agent_process:{run_id}:{step_key}"
        # RECORDED, NOT STARTED. Every caller already has the step's result in hand -- the
        # payload carries its status and verdict -- so the effect is over by the time this
        # runs. Leaving it open recorded eleven finished steps per process run as permanently
        # in progress. See receipts.record_completed.
        receipt, _created = self.receipts.record_completed(
            connection,
            scope["org_id"],
            scope["workspace_id"],
            project_id,
            dedupe_key,
            f"agent_process.{step_key}",
            {"run_id": run_id, "step_key": step_key},
            payload,
        )
        return receipt

    def _scope(self, connection: Connection, project_id: str) -> dict[str, str]:
        row = connection.execute(
            text(
                """
                SELECT p.id AS project_id, w.id AS workspace_id, o.id AS org_id
                FROM projects p
                JOIN workspaces w ON w.id = p.workspace_id
                JOIN orgs o ON o.id = w.org_id
                WHERE p.id = :project_id
                """
            ),
            {"project_id": project_id},
        ).mappings().first()
        if row is None:
            return {"project_id": project_id, "workspace_id": "workspace_unknown", "org_id": "org_unknown"}
        return dict(row)

    def _patch_context(self, connection: Connection, run_id: str, patch: dict[str, Any]) -> None:
        if not run_id or not patch:
            return
        connection.execute(
            text(
                """
                UPDATE agent_process_runs
                SET context = COALESCE(context, '{}'::jsonb) || CAST(:p AS JSONB),
                    updated_at=now()
                WHERE id=:id
                """
            ),
            {"p": json.dumps(patch), "id": run_id},
        )

    def _set_run_status(self, connection: Connection, run_id: str, status: str, current: list[str]) -> None:
        connection.execute(
            text(
                """
                UPDATE agent_process_runs
                SET status=:s, current_steps=CAST(:c AS JSONB), updated_at=now()
                WHERE id=:id
                """
            ),
            {"s": status, "c": json.dumps(current), "id": run_id},
        )

    @staticmethod
    def _row(row) -> dict[str, Any]:
        out = dict(row)
        for key, value in list(out.items()):
            if hasattr(value, "isoformat"):
                out[key] = value.isoformat()
        if isinstance(out.get("spec"), str):
            try:
                out["spec"] = json.loads(out["spec"])
            except json.JSONDecodeError:
                pass
        return out


def bind_captain_swarm(
    connection: Connection,
    *,
    settings: Any,
    project_id: str,
    actor: str,
    prd: str,
    title: str,
    attach_run_id: str,
    official: bool = False,
    engine: str | None = None,
    review_panel_size: int | None = None,
    qa_benchmark: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Attach the PRD→captain→QA→UX→panel swarm to an existing factory run.

    Does not start a second code.task. Tick advances the captains against the
    attached run. Simulated/auto_build=false stays planned-only.
    """
    service = AgentProcessService(settings)
    return service.start_run(
        connection,
        project_id=project_id,
        actor=actor,
        prd=prd,
        title=title[:200],
        auto_build=True,
        attach_run_id=attach_run_id,
        official=official,
        engine=engine,
        review_panel_size=review_panel_size,
        qa_benchmark=qa_benchmark,
    )
