"""THE MODEL PRE-CHECK: an agent that reads the pre-check's evidence the way a senior engineer
would, and predicts the failures the coded checks were never written to look for.

WHY THIS EXISTS
---------------
The coded pre-check (workflow_precheck.py) is good at exactly what it was coded to look for.
On 2026-10-13 it passed a HelpDesk run whose build died because the Agent SDK engine's sandbox
needed `socat` and the image did not have it — nothing checked for that binary, and nothing
could have, until somebody wrote the check after the failure. An engineer reading the same
evidence (the engine each step runs on, the last failures of this workflow, the logs) would
have asked "is socat in this image?" before spending the run.

So, beside the coded checks, an agent gets the same evidence and BOSS's fundamentals read
access, and returns PREDICTED problems. It never replaces a coded check and never fixes
anything: its lines are `(model)` lines, counted on their own, advisory at the start door.

HOW IT IS CALLED
----------------
  * the CLI: `python -m boss_engine.tools.workflow_precheck ... --agent`;
  * the start door (`workflow_precheck.gate_start`), only when the project turned on the
    `workflow_precheck_agent` setting or the workflow's spec says `precheck_agent: true`.
    Off by default: starting a workflow must not get slower or cost money unasked.

MODEL-AGNOSTIC. The model is the one BOSS's router picks for this project
(`model_routing.project_chat_adapter`, purpose `RouteRequest(kind="agent",
step="workflow_precheck")`), every turn governed by `model_routing.governed` (reserved against
the budget, receipted, settled). No vendor SDK is imported here; an Anthropic key, a Codex
subscription or OpenRouter answer through the same adapter interface
(`create_chat_completion` / `create_text_response`), and the plumbing behaves identically.

READ-ONLY. Its tools are the BOSS-fundamentals READS every agent holds (logs, run history and
turns, diagnoses, configuration, code and git history, the machine's state, the model
sources), taken from the project-pinned discuss registry and filtered to `kind == "read"`,
plus `installed_binaries` (which programs this process's image has on PATH).

EVIDENCE OR IT DID NOT HAPPEN. Each prediction must cite concrete evidence (a log line, an
event or run id, file:line, a query result). A prediction with no evidence is dropped; one
whose citations appear nowhere in what the agent was shown or read is downgraded to `risk`.
Malformed output is asked for again once, then dropped item by item.

CAPPED. A cost cap (`workflow_precheck_agent_max_cents`, default 50 cents) and a turn cap
(`workflow_precheck_agent_max_turns`, default 8): no turn starts once the cap is spent, and
the turn before a cap answers with no tools so the run ends with an answer.
"""
from __future__ import annotations

import json
import re
import shutil
import time
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from sqlalchemy import text

SOURCE = "(model)"
VERDICTS = ("will_fail", "risk")
WHO = ("auto", "person")
SKILL_ID = "model.precheck_agent"
ACTOR = "boss precheck agent"
#: The router's purpose: an agent step's kind, named for "Why this model". Risk 2 of 3 routes
#: it Balanced — judgement over evidence, not a frontier build.
ROUTE_KIND = "agent"
ROUTE_STEP = "workflow_precheck"
MAX_FINDINGS = 12
MAX_PACK_CHARS = 60_000
MAX_TOOL_RESULT_CHARS = 12_000
#: One more ask when the answer was not the JSON asked for.
FORMAT_RETRIES = 1

#: BOSS-FUNDAMENTALS READS (docs: every agent knows BOSS fundamentals): logs, run history and
#: turns, diagnoses, configuration, code and git history, the machine, the model sources.
#: Every one must be `kind == "read"` in the registry, or it is not offered.
READ_TOOLS: tuple[str, ...] = (
    "read_logs", "read_app_logs", "read_preview_log", "read_run_turns", "get_run_activity",
    "describe_workflow_run", "describe_workflow", "diagnose_run", "explain_run",
    "diagnose_setup", "get_config", "list_harnesses", "provider_audit", "inspect_self",
    "system_health", "read_code", "search_code", "list_source", "git_history",
    "list_code_tasks", "get_code_task", "list_running_work", "project_status",
    "describe_deployment_target", "list_deployment_hosts", "list_deployments",
    "describe_schema", "factory_briefing", "consistency_report",
)
BINARIES_TOOL = "installed_binaries"
_BINARY_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")

#: Which job's model a step runs on (model_routing.resolve_job). Steps not listed use no model.
_JOB_OF = {
    **dict.fromkeys(("build", "more_tests", "plrr", "retry_build", "security_fix"), "build"),
    **dict.fromkeys(("review", "spec_review", "security_review", "security_rereview",
                     "ux_review"), "review"),
    **dict.fromkeys(("meeting", "research", "architecture", "document", "report",
                     "security_panel", "security_decide", "security_report"), "chat"),
}

SYSTEM_PROMPT = """You are BOSS's pre-check engineer. A workflow is about to run. BOSS's coded \
pre-check has already checked what it was written to check; its report is below with every \
check's evidence. Your job is what the coded checks do NOT cover: read the evidence the way a \
senior engineer would and predict what will make a step fail.

Look especially at: what each step's engine/harness needs at run time that nothing checked \
(binaries in the image, sandbox prerequisites, network, credentials actually working rather \
than merely present), errors in recent failures and logs that will recur, configuration that \
contradicts what the coded checks reported, and anything the coded checks marked ready that the \
evidence says is not.

You are READ-ONLY. Use the tools to look (logs, run turns, diagnoses, config, code, git \
history, installed binaries). Never propose that you will fix anything; name the fix.

EVERY prediction must cite concrete evidence you were shown or read: a log line (quoted), an \
event or run id, file:line, or a tool result (quoted). A prediction without evidence is \
discarded. Do not repeat a coded check's own will_fail; add only what it missed or got wrong.

Answer with ONLY a JSON object, no prose around it:
{"findings": [{"step": "<step id from the workflow, or (run)>", "title": "<one line>", \
"verdict": "will_fail" | "risk", "evidence": ["<citation>", ...], "fix": "<the action>", \
"who": "auto" | "person"}]}
`will_fail` means the evidence shows it will fail; `risk` means it may. `who` is "auto" only \
when BOSS itself can make the fix. An empty list is a fine answer when nothing is missed."""

ANSWER_NOW = ("Stop investigating now and answer with ONLY the JSON object described in the "
              "instructions, from what you have gathered.")
FORMAT_AGAIN = ("That was not the JSON object asked for. Answer again with ONLY "
                '{"findings": [...]} — each item with step, title, verdict, evidence, fix, who.')


# ── Switches ──────────────────────────────────────────────────────────────────────────────────

def wanted_at_the_door(connection: Any, settings: Any, project_id: str,
                       spec: Mapping[str, Any]) -> bool:
    """Whether the start door runs the model pre-check: the workflow's own `precheck_agent`
    (true/false) wins; otherwise the project's `workflow_precheck_agent` setting. Off by
    default."""
    own = spec.get("precheck_agent") if isinstance(spec, Mapping) else None
    if isinstance(own, bool):
        return own
    from . import project_settings

    # In a savepoint: a failed read must not poison the start's own transaction.
    value = _savepoint(connection, lambda: project_settings.effective(
        connection, project_id, settings, "workflow_precheck_agent"), None) \
        if connection is not None else getattr(settings, "workflow_precheck_agent", 0)
    try:
        return int(value or 0) == 1
    except (TypeError, ValueError):
        return False


def caps_for(connection: Any, settings: Any, project_id: str, *,
             max_usd: Optional[float] = None, max_turns: Optional[int] = None
             ) -> tuple[int, int]:
    """(cost cap in micros, turn cap): the caller's, else the project's, else the install's."""
    from . import project_settings

    def knob(key: str, default: int) -> int:
        value = _savepoint(connection, lambda: project_settings.effective(
            connection, project_id, settings, key), None) \
            if connection is not None else getattr(settings, key, None)
        try:
            return int(value) if value not in (None, "") else default
        except Exception:  # noqa: BLE001
            return default

    cap = int(round(max_usd * 1_000_000)) if max_usd is not None else \
        knob("workflow_precheck_agent_max_cents", 50) * 10_000
    turns = int(max_turns) if max_turns is not None else \
        knob("workflow_precheck_agent_max_turns", 8)
    return max(1, cap), max(1, turns)


# ── The evidence it gets up front ─────────────────────────────────────────────────────────────

def _savepoint(connection: Any, fn: Callable[[], Any], default: Any) -> Any:
    try:
        point = connection.begin_nested()
    except Exception:  # noqa: BLE001
        return default
    try:
        out = fn()
        point.commit()
        return out
    except Exception:  # noqa: BLE001 — a missing piece of evidence is not a failed pre-check
        try:
            point.rollback()
        except Exception:  # noqa: BLE001
            pass
        return default


def _rows(connection: Any, sql: str, params: Mapping[str, Any]) -> list[dict[str, Any]]:
    return _savepoint(connection, lambda: [dict(r) for r in connection.execute(
        text(sql), dict(params)).mappings().all()], [])


def step_models(connection: Any, settings: Any, project_id: str,
                spec: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Per step: which job's model it runs on and the engine / model / source that job resolves
    to now (model_routing.resolve_job — reads only, never probes)."""
    from . import model_routing

    resolved: dict[str, Any] = {}
    out = []
    for step in spec.get("steps") or []:
        capability = str((step.get("binding") or {}).get("capability") or "")
        job = _JOB_OF.get(capability) or ("chat" if step.get("kind") == "agent" else "")
        row: dict[str, Any] = {"step": str(step.get("id")), "capability": capability or
                               str(step.get("kind") or ""), "job": job or None}
        if job:
            if job not in resolved:
                resolved[job] = _savepoint(connection, lambda j=job: model_routing.resolve_job(
                    connection, settings, project_id, j), None)
            found = resolved[job]
            if found is not None:
                row.update({"engine": found.engine or None, "model": found.model,
                            "source": found.source_id or found.why, "source_kind": found.kind})
            else:
                row["engine"] = "unknown (could not resolve)"
        out.append(row)
    return out


def recent_failures(connection: Any, project_id: str, workflow_key: str, *,
                    days: int = 14, limit: int = 8) -> dict[str, Any]:
    """The last failures of this workflow and of this project's builds, with their errors."""
    runs = _rows(connection, (
        "SELECT r.id, r.status, r.error, r.updated_at FROM workflow_runs r JOIN "
        "workflow_definitions d ON d.id = r.workflow_definition_id WHERE r.project_id = :p "
        "AND d.workflow_key = :k AND r.status IN ('failed','deadlettered','blocked') AND "
        "r.updated_at > now() - make_interval(days => :d) ORDER BY r.updated_at DESC "
        "LIMIT :n"), {"p": project_id, "k": workflow_key, "d": days, "n": limit})
    steps = _rows(connection, (
        "SELECT s.workflow_run_id, s.step_id, s.attempt, s.last_error, s.updated_at FROM "
        "workflow_step_runs s WHERE s.workflow_run_id = ANY(:ids) AND s.status IN "
        "('failed','deadlettered') AND s.last_error IS NOT NULL ORDER BY s.updated_at DESC "
        "LIMIT :n"), {"ids": [r["id"] for r in runs], "n": limit * 2}) if runs else []
    builds = _rows(connection, (
        "SELECT id, status, error, updated_at FROM code_task_runs WHERE project_id = :p AND "
        "status IN ('failed','blocked') AND error IS NOT NULL AND "
        "updated_at > now() - make_interval(days => :d) ORDER BY updated_at DESC LIMIT :n"),
        {"p": project_id, "d": days, "n": limit})
    return {"workflow_runs": runs, "failed_steps": steps, "builds": builds}


def installed_version() -> str:
    try:
        from .. import __version__

        return str(__version__)
    except Exception:  # noqa: BLE001
        return "unknown"


def evidence_pack(connection: Any, settings: Any, *, project_id: str,
                  report: Mapping[str, Any], spec: Mapping[str, Any],
                  run_input: Mapping[str, Any]) -> dict[str, Any]:
    """Everything the agent is told before it looks for itself: the workflow and its input, the
    coded report with every check's evidence, which engine/model/source each step uses, recent
    failures and the installed version."""
    workflow_key = str(report.get("workflow_key") or "")
    return {
        "workflow": {"key": workflow_key, "name": report.get("workflow_name"),
                     "version_id": report.get("version_id"), "spec": spec},
        "run_input": {k: v for k, v in dict(run_input or {}).items()
                      if not str(k).startswith("_")},
        "coded_precheck": {"checkpoint": report.get("checkpoint"),
                           "summary": report.get("summary"), "steps": report.get("steps")},
        "step_models": step_models(connection, settings, project_id, spec)
        if connection is not None else [],
        "recent_failures": recent_failures(connection, project_id, workflow_key)
        if connection is not None else {},
        "boss_version": installed_version(),
    }


# ── Tools ─────────────────────────────────────────────────────────────────────────────────────

def installed_binaries(names: Iterable[Any]) -> dict[str, Any]:
    """Which of `names` this process's image has on PATH (shutil.which). Read-only."""
    found: dict[str, Optional[str]] = {}
    refused: list[str] = []
    for raw in list(names or [])[:40]:
        name = str(raw or "").strip()
        if not _BINARY_NAME.match(name):
            refused.append(name[:64])
            continue
        found[name] = shutil.which(name)
    return {"where": "the image this pre-check runs in (the engine/worker process)",
            "found": {k: v for k, v in found.items() if v},
            "missing": sorted(k for k, v in found.items() if not v),
            **({"refused": refused} if refused else {})}


_BINARIES_SCHEMA = {
    "type": "function",
    "function": {
        "name": BINARIES_TOOL,
        "description": ("Which programs are installed on PATH in the image this pre-check runs "
                        "in (e.g. socat, bwrap, git, node, docker). Read-only."),
        "parameters": {"type": "object", "properties": {
            "names": {"type": "array", "items": {"type": "string"},
                      "description": "program names to look for"}},
            "required": ["names"]},
    },
}


def read_tools(settings: Any = None) -> tuple[list[dict[str, Any]], Callable[..., dict]]:
    """(the OpenAI-shaped schemas, a runner) for the fundamentals reads, from the project-pinned
    discuss registry. A tool that is missing or not `kind == "read"` is not offered."""
    from ..tools import build_discuss_read_registry

    registry = build_discuss_read_registry(
        edition=getattr(settings, "boss_edition", None) if settings is not None else None)
    specs = []
    for name in READ_TOOLS:
        try:
            spec = registry.get(name)
        except Exception:  # noqa: BLE001 — not in this edition
            continue
        if spec.kind == "read":
            specs.append(spec)
    schemas = [s.openai() for s in specs] + [_BINARIES_SCHEMA]
    allowed = {s.name for s in specs}

    def run(ctx: Any, project_id: str, name: str, args: dict) -> dict:
        if name == BINARIES_TOOL:
            return installed_binaries((args or {}).get("names") or [])
        if name not in allowed:
            return {"ok": False, "error": f"'{name}' is not a read tool this agent holds"}
        return registry.invoke(name, ctx, project_id=project_id, actor=ACTOR, args=args or {})

    return schemas, run


# ── Output: parse, validate, ground ───────────────────────────────────────────────────────────

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)
_ID = re.compile(r"\b[a-z]{2,6}_[A-Za-z0-9]{6,}\b")
_FILE_LINE = re.compile(r"([\w./-]+\.[A-Za-z0-9]{1,6}):\d+")
_QUOTED = re.compile(r"[\"'`“‘]([^\"'`”’]{8,})[\"'`”’]")


def parse_answer(answer: str) -> Optional[list[Any]]:
    """The findings list from the model's answer, or None when the answer is not the JSON asked
    for (fenced or bare; an object with `findings`, or a bare list)."""
    body = str(answer or "").strip()
    candidates = [m.group(1) for m in _FENCE.finditer(body)] + [body]
    for chunk in candidates:
        chunk = chunk.strip()
        for start, end in (("{", "}"), ("[", "]")):
            i, j = chunk.find(start), chunk.rfind(end)
            if i < 0 or j <= i:
                continue
            try:
                value = json.loads(chunk[i:j + 1])
            except ValueError:
                continue
            if isinstance(value, dict) and isinstance(value.get("findings"), list):
                return value["findings"]
            if isinstance(value, list):
                return value
    return None


def _citation(item: Any) -> str:
    if isinstance(item, Mapping):
        parts = [str(item.get(k) or "").strip() for k in
                 ("source", "where", "ref", "id", "file", "line", "quote", "text", "result")]
        return " · ".join(p for p in parts if p)[:600]
    return str(item or "").strip()[:600]


def grounded(citation: str, corpus: str) -> bool:
    """Whether a citation points at something the agent was actually shown or read: an id, a
    file path, a quoted fragment, or a 24-character run of it appears in the corpus."""
    lowered = corpus.lower()
    c = citation.strip()
    if not c:
        return False
    for token in _ID.findall(c):
        if token.lower() in lowered:
            return True
    for path in _FILE_LINE.findall(c):
        if path.lower() in lowered or path.rsplit("/", 1)[-1].lower() in lowered:
            return True
    # A quoted fragment, or one field of a structured citation ("source · quote").
    for quote in _QUOTED.findall(c) + c.split(" · "):
        if len(quote.strip()) >= 12 and quote.strip().lower() in lowered:
            return True
    low = c.lower()
    if len(low) <= 24:
        return len(low) >= 12 and low in lowered
    return any(low[i:i + 24] in lowered for i in range(0, len(low) - 23, 4))


def _verdict(raw: Any) -> str:
    value = re.sub(r"[\s-]+", "_", str(raw or "").strip().lower())
    if value in ("will_fail", "fail", "fails", "failure", "blocking", "will_break"):
        return "will_fail"
    return "risk"


def validate(items: Sequence[Any], corpus: str, step_ids: Iterable[str]
             ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(findings, dropped). Each kept finding is {step, title, verdict, evidence, fix, who,
    grounded, source}. No evidence → dropped. Evidence that appears nowhere in what the agent
    saw → kept as `risk`, `grounded: false`."""
    known = {str(s) for s in step_ids} | {"(run)"}
    findings: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    for item in list(items or []):
        if not isinstance(item, Mapping):
            dropped.append({"reason": "not an object", "item": str(item)[:200]})
            continue
        title = str(item.get("title") or "").strip()
        if not title:
            dropped.append({"reason": "no title", "item": json.dumps(item, default=str)[:200]})
            continue
        raw_evidence = item.get("evidence")
        if isinstance(raw_evidence, (str, Mapping)):
            raw_evidence = [raw_evidence]
        evidence = [c for c in (_citation(e) for e in (raw_evidence or [])) if len(c) >= 4]
        if not evidence:
            dropped.append({"reason": "no evidence", "title": title[:160]})
            continue
        verdict = _verdict(item.get("verdict"))
        is_grounded = any(grounded(c, corpus) for c in evidence)
        if not is_grounded and verdict == "will_fail":
            verdict = "risk"
        step = str(item.get("step") or "").strip() or "(run)"
        who = str(item.get("who") or "").strip().lower()
        findings.append({
            "source": SOURCE, "step": step if step in known else "(run)",
            **({"named_step": step} if step not in known else {}),
            "title": title[:160], "verdict": verdict, "evidence": evidence[:8],
            "fix": str(item.get("fix") or "").strip()[:600],
            "who": who if who in WHO else "person", "grounded": is_grounded,
        })
        if len(findings) >= MAX_FINDINGS:
            break
    return findings, dropped


# ── The model, through BOSS's router ──────────────────────────────────────────────────────────

def routed_adapter(connection: Any, settings: Any, project_id: str, *, ref: str) -> Any:
    """The project's routed model for this work, every turn governed (budget, receipt)."""
    from . import model_routing
    from .model_router.router import RouteRequest

    purpose = RouteRequest(kind=ROUTE_KIND, step=ROUTE_STEP, project_id=project_id,
                           ref=ref, risk_tier=2,
                           text="Predict which steps of a workflow run will fail, from the "
                                "pre-check's evidence, logs and run history.")
    raw = model_routing.project_chat_adapter(connection, settings, project_id, purpose=purpose)
    adapter = model_routing.governed(raw, connection, settings, project_id,
                                     skill_id=SKILL_ID, ref=ref)
    return adapter, raw


def _who_answered(raw: Any) -> dict[str, Any]:
    choice = getattr(raw, "_boss_choice", None)
    route = getattr(raw, "_boss_route", None)
    engine = getattr(route, "engine", None) or getattr(choice, "engine", None) or ""
    model = getattr(route, "model", None) or getattr(raw, "model", None) or ""
    source = getattr(route, "source_id", None) or getattr(choice, "source_id", None) \
        or getattr(choice, "why", None) or ""
    return {"engine": str(engine or "unknown"), "model": str(model or ""),
            "source": str(source or "unknown")}


def _turn_cost(usage: Any) -> int:
    from .openai_adapter import estimate_cost_usd_micros

    if not isinstance(usage, Mapping):
        return 0
    try:
        return int(estimate_cost_usd_micros(dict(usage)))
    except Exception:  # noqa: BLE001
        return 0


def _money(micros: int) -> str:
    return f"${micros / 1_000_000:.2f}" if micros >= 10_000 else f"${micros / 1_000_000:.4f}"


def run_agent(*, adapter: Any, raw_adapter: Any = None, evidence: Mapping[str, Any],
              tools: Optional[list[dict[str, Any]]] = None,
              call_tool: Optional[Callable[[str, dict], dict]] = None,
              max_usd_micros: int = 500_000, max_turns: int = 8,
              step_ids: Iterable[str] = ()) -> dict[str, Any]:
    """THE LOOP. Pure over its seams (the adapter and the tool runner), so the tests drive it
    with a stub model and the CLI/door with the routed one. Never raises: a broken model call
    is reported in the result, never as a failed pre-check."""
    from .redaction import redact_secrets

    started = time.monotonic()
    who = _who_answered(raw_adapter if raw_adapter is not None else adapter)
    pack = redact_secrets(json.dumps(evidence, default=str, indent=1))
    if len(pack) > MAX_PACK_CHARS:
        pack = pack[:MAX_PACK_CHARS] + "\n… (truncated; read more with the tools)"
    corpus_parts = [pack]
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "THE EVIDENCE (JSON):\n" + pack},
    ]
    spent = 0
    last_cost = 0
    turns = 0
    tool_calls = 0
    retries = 0
    stopped: Optional[str] = None
    error: Optional[str] = None
    items: Optional[list[Any]] = None
    model_seen = ""
    can_tool = tools is not None and call_tool is not None
    while True:
        if spent >= max_usd_micros:
            stopped = "cost_cap"
            break
        if turns >= max_turns:
            stopped = "turn_cap"
            break
        # THE LAST AFFORDABLE TURN ANSWERS: no tools on the final turn, or when another turn at
        # the last one's price would cross the cap, so a capped run still ends with an answer.
        final = (turns == max_turns - 1) or (turns > 0 and spent + last_cost > max_usd_micros)
        if final and turns > 0 and messages[-1].get("role") == "tool":
            messages.append({"role": "user", "content": ANSWER_NOW})
        try:
            message, usage = adapter.create_chat_completion(
                messages, tools=(tools if (can_tool and not final) else None))
        except Exception as exc:  # noqa: BLE001 — the model failing is a result, not a crash
            error = f"{type(exc).__name__}: {exc}"[:400]
            stopped = "error"
            break
        turns += 1
        last_cost = _turn_cost(usage)
        spent += last_cost
        model_seen = str((usage or {}).get("model") or model_seen) if isinstance(usage, Mapping) \
            else model_seen
        message = message if isinstance(message, Mapping) else {"content": str(message or "")}
        messages.append(message.get("raw") or {"role": "assistant",
                                               "content": message.get("content") or ""})
        calls = message.get("tool_calls") or []
        if calls and can_tool and not final:
            for call in calls:
                tool_calls += 1
                try:
                    args = json.loads(call.get("arguments") or "{}")
                except (TypeError, ValueError):
                    args = {}
                try:
                    result = call_tool(str(call.get("name") or ""),
                                       args if isinstance(args, dict) else {})
                except Exception as exc:  # noqa: BLE001
                    result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:400]}
                body = redact_secrets(json.dumps(result, default=str))[:MAX_TOOL_RESULT_CHARS]
                corpus_parts.append(body)
                messages.append({"role": "tool", "tool_call_id": call.get("id"),
                                 "content": body})
            continue
        items = parse_answer(str(message.get("content") or ""))
        if items is not None:
            break
        if retries >= FORMAT_RETRIES:
            stopped = "malformed"
            break
        retries += 1
        messages.append({"role": "user", "content": FORMAT_AGAIN})
    findings, dropped = validate(items or [], "\n".join(corpus_parts), step_ids)
    counts = {v: sum(1 for f in findings if f["verdict"] == v) for v in VERDICTS}
    model = model_seen or who["model"] or "default"
    line = (f"model pre-check ran on {who['engine']}/{model} via {who['source']}, "
            f"{_money(spent)} ({turns} turn(s), {tool_calls} tool call(s))")
    if stopped:
        line += f" — stopped: {stopped.replace('_', ' ')}" + (f" ({error})" if error else "")
    return {"ran": True, "engine": who["engine"], "model": model, "source": who["source"],
            "cost_usd_micros": spent, "max_usd_micros": max_usd_micros, "turns": turns,
            "max_turns": max_turns, "tool_calls": tool_calls, "stopped": stopped,
            "error": error, "findings": findings, "dropped": dropped, "counts": counts,
            "line": line, "seconds": round(time.monotonic() - started, 2)}


def not_run(reason: str) -> dict[str, Any]:
    return {"ran": False, "findings": [], "dropped": [], "counts": dict.fromkeys(VERDICTS, 0),
            "line": f"model pre-check did not run: {reason}", "error": reason}


def precheck_with_model(engine: Any, settings: Any, *, project_id: str,
                        report: dict[str, Any], spec: Mapping[str, Any],
                        run_input: Mapping[str, Any], connection: Any = None,
                        max_usd: Optional[float] = None, max_turns: Optional[int] = None
                        ) -> dict[str, Any]:
    """Gather the evidence, route the model, run the loop, merge into `report`. Never raises.

    With `connection` (the start door) the turns are governed on it, so they commit or roll
    back with the start; without one (the CLI) on a transaction of their own."""
    def go(c: Any) -> dict[str, Any]:
        cap, turns = caps_for(c, settings, project_id, max_usd=max_usd, max_turns=max_turns)
        evidence = evidence_pack(c, settings, project_id=project_id, report=report, spec=spec,
                                 run_input=run_input)
        ref = f"precheck:{report.get('workflow_key') or ''}:{report.get('run_id') or 'pre_run'}"
        adapter, raw = routed_adapter(c, settings, project_id, ref=ref)
        schemas, run = read_tools(settings)
        from ..context import EngineContext

        ctx = EngineContext(settings=settings, engine=engine if engine is not None
                            else getattr(c, "engine", None))
        return run_agent(adapter=adapter, raw_adapter=raw, evidence=evidence, tools=schemas,
                         call_tool=lambda name, args: run(ctx, project_id, name, args),
                         max_usd_micros=cap, max_turns=turns,
                         step_ids=[str(s.get("id")) for s in spec.get("steps") or []])

    try:
        if connection is not None:
            result = go(connection)
        else:
            with engine.begin() as c:
                result = go(c)
    except Exception as exc:  # noqa: BLE001 — e.g. RouteRefused: no model may run this
        result = not_run(f"{type(exc).__name__}: {exc}"[:400])
    return merge(report, result)


# ── Into the report ───────────────────────────────────────────────────────────────────────────

def merge(report: dict[str, Any], result: Mapping[str, Any]) -> dict[str, Any]:
    """The model's lines go in `report["model"]`, separate from the coded checks: their own
    counts (`summary["model"]`), never in `summary["blocking"]` (advisory), and in `explain`
    after the coded items."""
    report["model"] = dict(result)
    summary = report.setdefault("summary", {})
    summary["model"] = dict(result.get("counts") or dict.fromkeys(VERDICTS, 0))
    explain = report.setdefault("explain", [])
    for verdict in VERDICTS:
        for f in result.get("findings") or []:
            if f["verdict"] != verdict:
                continue
            explain.append({"step": f["step"], "check": f"{SOURCE} {f['title']}",
                            "verdict": verdict, "kind": f"model ({f['who']})",
                            "path": f["fix"] or "no fix named",
                            "evidence": list(f["evidence"])})
    return report


__all__ = ["ACTOR", "READ_TOOLS", "SOURCE", "VERDICTS", "caps_for", "evidence_pack",
           "grounded", "installed_binaries", "merge", "not_run", "parse_answer",
           "precheck_with_model", "read_tools", "recent_failures", "routed_adapter",
           "run_agent", "step_models", "validate", "wanted_at_the_door"]
