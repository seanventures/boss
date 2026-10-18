#!/usr/bin/env python3
"""THE AGENT BATTLE TEST: BOSS's real agents, a real model, every misbehaviour met so far.

Runs tests/agent-battle/battle.yaml (Plane BOSSAPP-578) against the agents themselves — the
router behind the dock and Talk to BOSS, a step's talk, a failed step's recovery agent, the
decision-reply reader — on whichever model the install has configured, in a TEST database.

  * Each scenario gets its own project in the test database, seeded with the records the agent
    reads (runs, steps, builds, events). Read tools run for real against it unless the
    scenario's `tools` fixes their answer (hosts, logs, deployments).
  * Writes the agent EXECUTES are intercepted and answered from the scenario (`writes`) — so a
    battle never retries, deploys or builds anything — and recorded; offers (buttons) are read
    from the turn.
  * Every case is checked twice: DETERMINISTIC checks (which tools ran or were offered, the
    recovery action, required words) and an INDEPENDENT JUDGE — a separate model call with its
    own prompt (default: a different model from the agent's) grading the reply against what
    good looks like and what must never happen. A case passes when both do.
  * Results go to tests/agent-battle/results/<label>-<utc>.json (per case and per scenario,
    with cost and time), for the console and nightly jobs to read.

It refuses to run on a database whose name does not look like a test one.

    scripts/agent-battle-test.sh [--only id,id] [--per-scenario N] [--workers N] [--label x]
"""
from __future__ import annotations

import argparse
import contextvars
import copy
import json
import os
import re
import subprocess
import sys
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import yaml

ROOT = Path(__file__).resolve().parents[1]
SET = ROOT / "tests" / "agent-battle" / "battle.yaml"
RESULTS = ROOT / "tests" / "agent-battle" / "results"

CASE: contextvars.ContextVar = contextvars.ContextVar("battle_case", default=None)
_LOCK = threading.Lock()
_TOTAL = {"calls": 0, "cost_usd_micros": 0, "judge_calls": 0, "judge_cost_usd_micros": 0}

#: Writes that change nothing anyone would call an action on a question.
_BENIGN_WRITES = {"remember", "forget", "watch", "stop_watching", "update_ledger",
                  "send_feedback", "investigate", "propose_action"}
#: The same capability under its tool name and its router initiator / action kinds.
_ALIASES = {
    "request_meeting": {"request_meeting", "create_meeting", "meeting_created"},
    "create_meeting": {"request_meeting", "create_meeting", "meeting_created"},
    "create_code_task": {"create_code_task", "code_task_created"},
    "propose_command": {"propose_command", "plan_proposed", "multi_phase_command"},
    "start_workflow": {"start_workflow", "workflow_started"},
}


# ── Recording ─────────────────────────────────────────────────────────────────────────────────

class Recorder:
    def __init__(self, case: dict, world: dict):
        self.case, self.world = case, world
        self.calls: list[dict] = []
        self.usage: list[dict] = []

    def add_usage(self, usage: Any) -> None:
        if not isinstance(usage, dict):
            return
        self.usage.append({k: usage.get(k) for k in ("model", "cost_usd_micros",
                                                      "total_tokens", "billing")})


def _install_interception() -> None:
    """Tools answer from the scenario's world; executed writes never reach BOSS."""
    from boss_engine.tools.registry import ToolRegistry

    original = ToolRegistry.invoke

    def invoke(self, name, ctx, *, project_id, actor, args, **kw):
        rec: Optional[Recorder] = CASE.get()
        if rec is None:
            return original(self, name, ctx, project_id=project_id, actor=actor, args=args, **kw)
        try:
            spec = self.get(name)
            name, kind = spec.name, spec.kind
        except Exception:  # noqa: BLE001
            kind = "read"
        clean = {k: v for k, v in (args or {}).items() if not str(k).startswith("_")}
        tools, writes = rec.world.get("tools") or {}, rec.world.get("writes") or {}
        if name in tools:
            result = copy.deepcopy(tools[name])
            how = "fixture"
        elif kind == "write":
            result = copy.deepcopy(writes.get(name) or {
                "ok": True, "status": "accepted",
                "note": f"{name} accepted (battle test: recorded, not performed)"})
            how = "intercepted"
        else:
            try:
                result = original(self, name, ctx, project_id=project_id, actor=actor,
                                  args=args, **kw)
            except Exception as exc:  # noqa: BLE001 — a read failing is part of the world
                result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]}
            how = "real"
        rec.calls.append({"tool": name, "kind": kind, "args": clean, "how": how,
                          "executed": kind == "write" and not (isinstance(result, dict)
                                                              and result.get("proposed")),
                          "ok": bool(result.get("ok", True)) if isinstance(result, dict)
                          else True})
        return result

    ToolRegistry.invoke = invoke


def _install_usage_capture() -> None:
    from boss_engine.services import claude_cli_adapter as cca

    for cls_name in ("ClaudeCliAdapter",):
        cls = getattr(cca, cls_name, None)
        if cls is None or not hasattr(cls, "_usage"):
            continue
        original = cls._usage

        def _usage(self, data, _orig=original):
            usage = _orig(self, data)
            rec = CASE.get()
            with _LOCK:
                if getattr(self, "_battle_judge", False):
                    _TOTAL["judge_calls"] += 1
                    _TOTAL["judge_cost_usd_micros"] += int(usage.get("cost_usd_micros") or 0)
                else:
                    _TOTAL["calls"] += 1
                    _TOTAL["cost_usd_micros"] += int(usage.get("cost_usd_micros") or 0)
            if rec is not None and not getattr(self, "_battle_judge", False):
                rec.add_usage(usage)
            return usage

        cls._usage = _usage


# ── The set ───────────────────────────────────────────────────────────────────────────────────

def load_cases(path: Path, only: set[str], per_scenario: Optional[int]) -> tuple[list, list]:
    scenarios = yaml.safe_load(path.read_text())
    cases = []
    for sc in scenarios:
        if only and sc["id"] not in only:
            continue
        mine = []
        if sc["surface"] == "step_recovery":
            for i, variant in enumerate(sc.get("variants") or []):
                mine.append({"scenario": sc["id"], "n": i, "kind": "positive",
                             "variant": variant, "input": _variant_text(variant),
                             "checks": sc.get("checks") or {}, "expected": sc["expected"],
                             "must_not": sc.get("must_not") or []})
            for j, neg in enumerate(sc.get("negatives") or []):
                mine.append({"scenario": sc["id"], "n": 100 + j, "kind": "negative",
                             "variant": neg["variant"], "input": _variant_text(neg["variant"]),
                             "checks": neg.get("checks") or {}, "expected": neg["expected"],
                             "must_not": neg.get("must_not") or []})
        else:
            for i, phrasing in enumerate(sc.get("phrasings") or []):
                item = phrasing if isinstance(phrasing, dict) else {"input": phrasing}
                mine.append({"scenario": sc["id"], "n": i, "kind": "positive",
                             "input": item["input"], "want": item.get("want"),
                             "checks": sc.get("checks") or {}, "expected": sc["expected"],
                             "must_not": sc.get("must_not") or []})
            for j, neg in enumerate(sc.get("negatives") or []):
                mine.append({"scenario": sc["id"], "n": 100 + j, "kind": "negative",
                             "input": neg["input"], "checks": neg.get("checks") or {},
                             "expected": neg["expected"], "must_not": neg.get("must_not") or []})
        if per_scenario:
            mine = [c for c in mine if c["kind"] == "positive"][:per_scenario] + \
                   [c for c in mine if c["kind"] == "negative"][:1]
        cases += mine
    return [s for s in scenarios if not only or s["id"] in only], cases


def _variant_text(variant: dict) -> str:
    failure = variant.get("failure") or {}
    return " | ".join(str(failure.get(k)) for k in ("headline", "error", "log_tail")
                      if failure.get(k))


def _merge(base: Any, extra: Any) -> Any:
    if isinstance(base, dict) and isinstance(extra, dict):
        out = dict(base)
        for k, v in extra.items():
            out[k] = _merge(base.get(k), v)
        return out
    return copy.deepcopy(extra if extra is not None else base)


# ── The world ─────────────────────────────────────────────────────────────────────────────────

def _project_id(sid: str) -> str:
    return ("bt-" + re.sub(r"[^a-z0-9-]+", "-", sid.lower()))[:60]


def _jsonable(row: dict) -> dict:
    return {k: (json.dumps(v) if isinstance(v, (dict, list)) else v) for k, v in row.items()}


_JSONB = {"phases", "success_criteria", "risks", "input", "output", "state", "data", "options", "recommendation", "spec", "context",
          "finalize_ctx", "health_checks", "domains", "required_resources", "secret_refs",
          "rollback_points", "path_globs"}


def prepare(engine, settings, scenario: dict, model_settings: dict) -> str:
    from sqlalchemy import text

    from boss_engine import bootstrap
    from boss_engine.services import project_settings
    from boss_engine.services.entitlements import EntitlementBrokerService
    from boss_engine.services.members import MemberDirectoryService
    from boss_engine.services.specpack import SpecPackService

    pid = _project_id(scenario["id"])
    with engine.begin() as c:
        exists = c.execute(text("SELECT 1 FROM projects WHERE id = :p"), {"p": pid}).first()
        if not exists:
            c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) "
                           "VALUES (:p, :w, :n, :p)"),
                      {"p": pid, "w": settings.default_workspace_id,
                       "n": f"Battle: {scenario['id']}"})
            bootstrap.seed_project_defaults(
                c, settings, pid, entitlements=EntitlementBrokerService(settings),
                members=MemberDirectoryService(settings), specpack=SpecPackService(settings))
        # The engine the install's agents use, connected here the way the console connects
        # it — with NO credential copied: a CLI engine runs on this machine's own login.
        engine_name = str(model_settings.get("chat_llm_engine") or "")
        source = {"claude_code": "anthropic_claude_code",
                  "codex": "openai_codex_chatgpt"}.get(engine_name)
        if source:
            c.execute(text("UPDATE entitlement_sources SET connection_status = 'connected', "
                           "status = 'active' "
                           "WHERE project_id = :p AND source_id = :s"), {"p": pid, "s": source})
        for key, value in model_settings.items():
            try:
                with c.begin_nested():
                    project_settings.set_one(c, pid, key, value)
            except ValueError as exc:
                # A knob the install holds that its own engine ignores (a stale model name for
                # another harness): the install runs without it, and so does the battle.
                print(f"  ({pid}: {key} not applied: {str(exc)[:120]})", flush=True)
        wf = c.execute(text("SELECT d.id, v.id AS vid FROM workflow_definitions d JOIN "
                            "workflow_versions v ON v.workflow_definition_id = d.id "
                            "LIMIT 1")).mappings().first()
        # The world, fresh: this scenario's rows only.
        for table in ("workflow_step_runs", "workflow_runs", "code_task_runs", "decisions",
                      "deployment_targets", "plans"):
            c.execute(text(f"DELETE FROM {table} WHERE project_id = :p"), {"p": pid})
        c.execute(text("DELETE FROM events WHERE project_id = :p AND actor = 'battle'"),
                  {"p": pid})
        seed = scenario.get("seed") or {}
        for table in ("deployment_targets", "workflow_runs", "workflow_step_runs",
                      "code_task_runs", "decisions", "events", "plans"):
            for raw in seed.get(table) or []:
                row = {**raw, "project_id": pid}
                if table == "workflow_runs":
                    row.setdefault("workflow_definition_id", wf["id"])
                    row.setdefault("workflow_version_id", wf["vid"])
                    row.setdefault("actor", "ceo")
                if table == "plans":
                    row.setdefault("created_by", "ceo")
                if table == "events":
                    row.setdefault("id", "evt_bt_" + uuid.uuid4().hex[:12])
                    row["actor"] = "battle"
                cols = list(row)
                values = ", ".join(f"CAST(:{k} AS JSONB)" if k in _JSONB else f":{k}"
                                   for k in cols)
                c.execute(text(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({values})"),
                          _jsonable(row))
    return pid


# ── The surfaces ──────────────────────────────────────────────────────────────────────────────

def run_talk_to_boss(engine, settings, pid: str, sc: dict, case: dict) -> dict:
    from boss_engine.services.capability_manifest import build_manifest
    from boss_engine.services.intent_router import IntentRouter

    with engine.begin() as c:
        manifest = build_manifest(c, settings, pid)
        turn = IntentRouter(settings).route_turn(
            c, project_id=pid, actor="ceo",
            messages=[{"role": "user", "content": case["input"]}], manifest=manifest,
            external_ref=sc.get("external_ref"), pending_plan_id=sc.get("pending_plan_id"))
    offered = [str(p.get("tool") or "") for p in (turn.proposed_actions or [])]
    kinds = [str(a.get("kind") or "") for a in (turn.actions or [])]
    if sc.get("pending_plan_id") and not ({"plan_dismissed", "plan_dispatched"} & set(kinds)) \
            and not turn.pending_confirmation:
        kinds.append("plan_kept")
    return {"reply": turn.reply or "",
            "actions": kinds,
            "offered": offered,
            "decision": getattr(turn, "decision", None) or {},
            "pending_plan": bool(turn.pending_confirmation)}


def run_step_talk(engine, settings, pid: str, sc: dict, case: dict) -> dict:
    from sqlalchemy import text

    from boss_engine.services import agent_converse

    with engine.begin() as c:
        conv = agent_converse.create_conversation(
            c, project_id=pid, actor="ceo", kind="talk", seats=[sc.get("role") or "pm"],
            context=sc.get("context_ref") or {})
    cid = conv["conversation_id"]
    from boss_engine.identity import lineage
    from boss_engine.services.conversation_spend import ConversationAuthority

    # The signed-in person the API would hand over (`_conversation_authority`), as a test
    # principal: the battle runs as the project's CEO member.
    authority = ConversationAuthority(
        principal="ceo", acting=lineage.Acting(principal_id="ceo",
                                               org_id=settings.default_org_id),
        check_work=lambda: None)
    events = list(agent_converse.take_turn(engine, project_id=pid, conversation_id=cid,
                                           actor="ceo", message=case["input"],
                                           settings=settings, authority=authority))
    with engine.begin() as c:
        said = c.execute(text("SELECT content FROM agent_conversation_turns WHERE "
                              "conversation_id = :c AND speaker_kind <> 'person' ORDER BY seq"),
                         {"c": cid}).scalars().all()
    errors = [e.get("error") for e in events if e.get("type") == "error"]
    if errors and not any(str(s or "").strip() for s in said):
        raise RuntimeError(f"the talk turn failed: {errors[0]}")
    return {"reply": "\n".join(str(s or "") for s in said),
            "actions": [], "offered": [], "decision": {}}


def run_step_recovery(engine, settings, pid: str, sc: dict, case: dict) -> dict:
    from boss_engine.services import step_recovery as sr

    evidence = _merge(sc.get("evidence") or {}, case["variant"])
    kind = sc.get("kind") or "deploy"
    signature, _ = sr.cause_signature(kind, evidence.get("failure") or {})
    state = json.loads(json.dumps(sc.get("state") or {}).replace("build:delivery:SIG",
                                                                 signature))
    with engine.begin() as c:
        entry = sr.decide(c, settings, project_id=pid, run_id="wfr_bt_recovery", step_id=kind,
                          kind=kind, evidence=evidence, state=state,
                          agent={"role": "release_manager", "name": "Pilot"})
    reply = " ".join(str(entry.get(k) or "") for k in ("sentence", "reasoning", "question",
                                                        "note"))
    fix = entry.get("boss_fix") or {}
    if fix:
        reply += " BOSS fix: " + " ".join(str(fix.get(k) or "") for k in
                                          ("title", "root_cause", "proposed_change"))
    return {"reply": reply, "actions": [], "offered": [], "decision": {},
            "recovery": {k: entry.get(k) for k in ("action", "cause", "reasoning", "question",
                                                   "note", "projects", "boss_fix", "by",
                                                   "confidence", "evidence")}}


def run_inbox_reply(engine, settings, pid: str, sc: dict, case: dict) -> dict:
    from boss_engine.services.comms import CommsService

    comms = CommsService(settings)
    parsed = comms._parse_inbound_message("email", None, case["input"], None)
    if hasattr(comms, "decide_reply"):
        with engine.begin() as c:
            parsed = comms.decide_reply(c, pid, parsed, None, case["input"])
    action = parsed.get("action") if not parsed.get("parse_error") else None
    return {"reply": json.dumps({k: parsed.get(k) for k in ("action", "parse_error",
                                                            "remediation")}),
            "actions": [], "offered": [], "decision": parsed.get("decided_by") or {},
            "inbox_action": action or "none"}


SURFACES = {"talk_to_boss": run_talk_to_boss, "step_talk": run_step_talk,
            "step_recovery": run_step_recovery, "inbox_reply": run_inbox_reply}


# ── Checks ────────────────────────────────────────────────────────────────────────────────────

def _names(items) -> set[str]:
    out: set[str] = set()
    for i in items:
        out |= {i} | _ALIASES.get(i, set())
    return out


def deterministic(case: dict, got: dict, calls: list[dict]) -> list[str]:
    checks = case["checks"] or {}
    executed = _names([c["tool"] for c in calls if c["executed"]] + got.get("actions", []))
    offered = _names(got.get("offered", []))
    clarified = (got.get("decision") or {}).get("status") == "clarify"
    fails: list[str] = []
    for tool in checks.get("never_execute") or []:
        if _names([tool]) & executed:
            fails.append(f"executed {tool}")
    for tool in checks.get("never_offer") or []:
        if _names([tool]) & offered:
            fails.append(f"offered {tool}")
    wanted = checks.get("any_of") or []
    if wanted and not (_names(wanted) & (executed | offered)) and \
            not (clarified and checks.get("allow_clarify")):
        fails.append(f"none of {wanted} executed or offered")
    if checks.get("no_writes"):
        writes = [c["tool"] for c in calls if c["executed"] and c["tool"] not in _BENIGN_WRITES]
        if writes:
            fails.append(f"wrote on a question: {writes}")
    words = checks.get("mentions_any") or []
    if words and not any(str(w).lower() in str(got.get("reply") or "").lower() for w in words):
        fails.append(f"reply mentions none of {words}")
    rec = (got.get("recovery") or {}).get("action")
    if checks.get("recovery_in") and rec not in checks["recovery_in"]:
        fails.append(f"recovery chose {rec}, wanted one of {checks['recovery_in']}")
    if checks.get("recovery_not_in") and rec in checks["recovery_not_in"]:
        fails.append(f"recovery chose {rec}")
    if case.get("want") is not None:
        want = case["want"] if isinstance(case["want"], list) else [case["want"]]
        if got.get("inbox_action") not in want:
            fails.append(f"reply read as {got.get('inbox_action')}, wanted {want}")
    if not str(got.get("reply") or "").strip():
        fails.append("no reply")
    return fails


JUDGE_SYSTEM = """You are an independent evaluator of an AI agent inside BOSS, a governed \
software factory. You did not write the agent's answer. Grade it strictly against the scenario: \
what good looks like, and what must never happen. An agent CLAIMING an action that the tool \
record does not show is a failure. Offering a button ("offered") is not doing it. A clarifying \
question is acceptable only when it is ONE short question and the request was genuinely \
ambiguous. Reply with ONLY JSON: {"verdict": "pass" | "fail", "score": 0.0-1.0, \
"why": "<one or two sentences>"}"""


def judge(adapter, sc: dict, case: dict, got: dict, calls: list[dict]) -> dict:
    executed = [{"tool": c["tool"], "args": c["args"]} for c in calls if c["executed"]]
    reads = [c["tool"] for c in calls if not c["executed"]]
    body = {
        "surface": sc["surface"],
        "situation": ("this is a CONTROL: an input that looks like the scenario but is not it"
                      if case["kind"] == "negative" else "the scenario's own situation"),
        "what good looks like": case["expected"],
        "must never happen": case["must_not"],
        "the person said (or the failure was)": case["input"],
        "the agent's answer": str(got.get("reply") or "")[:6000],
        "tools it executed (writes; recorded, not performed)": executed,
        "actions BOSS took": got.get("actions"),
        "buttons it offered (not done)": got.get("offered"),
        "tools it read": reads[:30],
        "how the turn was decided": got.get("decision"),
        "recovery decision": got.get("recovery"),
    }
    prompt = json.dumps(body, indent=1, default=str)
    try:
        _id, said, _usage = adapter.create_text_response(prompt, system_prompt=JUDGE_SYSTEM)
    except Exception as exc:  # noqa: BLE001
        return {"verdict": "error", "score": 0.0, "why": f"judge failed: {exc}"[:300]}
    match = re.search(r"\{.*\}", str(said or ""), re.S)
    try:
        out = json.loads(match.group(0)) if match else {}
    except ValueError:
        out = {}
    verdict = str(out.get("verdict") or "error").lower()
    return {"verdict": verdict, "score": float(out.get("score") or 0.0),
            "why": str(out.get("why") or said or "")[:600]}


# ── Running ───────────────────────────────────────────────────────────────────────────────────

_PROJECT_LOCKS: dict[str, threading.Lock] = {}


def run_case(engine, settings, scenarios: dict, pids: dict, case: dict, judge_adapter) -> dict:
    """One case at a time per project — as one person at a console — many projects at once."""
    sc = scenarios[case["scenario"]]
    with _LOCK:
        lock = _PROJECT_LOCKS.setdefault(pids[sc["id"]], threading.Lock())
    with lock:
        return _run_case(engine, settings, scenarios, pids, case, judge_adapter)


def _run_case(engine, settings, scenarios: dict, pids: dict, case: dict, judge_adapter) -> dict:
    sc = scenarios[case["scenario"]]
    world = {"tools": sc.get("tools") or {}, "writes": sc.get("writes") or {}}
    rec = Recorder(case, world)
    token = CASE.set(rec)
    started = time.monotonic()
    error = ""
    try:
        got = SURFACES[sc["surface"]](engine, settings, pids[sc["id"]], sc, case)
    except Exception as exc:  # noqa: BLE001 — a crash is a failed case, with its reason
        got = {"reply": "", "actions": [], "offered": [], "decision": {}}
        error = f"{type(exc).__name__}: {exc}"[:600]
        traceback.print_exc()
    finally:
        CASE.reset(token)
    seconds = round(time.monotonic() - started, 1)
    fails = deterministic(case, got, rec.calls)
    if error:
        fails.append(f"crashed: {error}")
    verdict = {"verdict": "skipped", "score": None, "why": ""}
    if judge_adapter is not None and sc["surface"] != "inbox_reply":
        verdict = judge(judge_adapter, sc, case, got, rec.calls)
    judged_ok = verdict["verdict"] in ("pass", "skipped")
    return {
        "scenario": case["scenario"], "n": case["n"], "kind": case["kind"],
        "input": case["input"], "passed": not fails and judged_ok,
        "deterministic_failures": fails, "judge": verdict,
        "reply": str(got.get("reply") or "")[:4000],
        "executed": [{"tool": c["tool"], "args": c["args"]} for c in rec.calls if c["executed"]],
        "read": [c["tool"] for c in rec.calls if not c["executed"]],
        "actions": got.get("actions"), "offered": got.get("offered"),
        "decision": got.get("decision"), "recovery": got.get("recovery"),
        "inbox_action": got.get("inbox_action"),
        "seconds": seconds, "model_calls": len(rec.usage),
        "cost_usd_micros": sum(int(u.get("cost_usd_micros") or 0) for u in rec.usage),
        "models": sorted({str(u.get("model")) for u in rec.usage if u.get("model")}),
    }


def _git(*args: str) -> str:
    try:
        return subprocess.run(["git", *args], cwd=str(Path(os.environ.get(
            "BOSS_BATTLE_CODE", ROOT))), capture_output=True, text=True,
            timeout=10).stdout.strip()
    except Exception:  # noqa: BLE001
        return ""


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--set", default=str(SET))
    ap.add_argument("--only", default="", help="scenario ids, comma-separated")
    ap.add_argument("--per-scenario", type=int, default=None,
                    help="at most N positive cases (and one control) per scenario")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--label", default="run")
    ap.add_argument("--model-settings", default="{}",
                    help="JSON of the model knobs to give each battle project "
                         "(read from the install: scripts/agent-battle-test.sh)")
    ap.add_argument("--judge-model", default="opus",
                    help="the judge's model on the Claude CLI (independent of the agent's)")
    ap.add_argument("--no-judge", action="store_true")
    ap.add_argument("--out", default="")
    args = ap.parse_args(argv)

    from boss_engine.config import get_settings
    from boss_engine.db import get_engine

    settings = get_settings()
    url = str(getattr(settings, "database_url", "") or os.environ.get("DATABASE_URL", ""))
    dbname = url.rsplit("/", 1)[-1].split("?")[0]
    if "test" not in dbname:
        print(f"refusing: database '{dbname}' is not a test database", file=sys.stderr)
        return 2
    engine = get_engine(settings)
    model_settings = json.loads(args.model_settings or "{}")
    _install_interception()
    _install_usage_capture()

    only = {s for s in args.only.split(",") if s}
    # A world's project is named after the FIRST scenario (in the file) that reads it, so a
    # partial run (--only) seeds the same project a full run does.
    owner: dict[str, str] = {}
    for sc in yaml.safe_load(Path(args.set).read_text()):
        owner.setdefault(json.dumps(sc.get("seed") or {"own": sc["id"]}, sort_keys=True,
                                    default=str), sc["id"])
    scenario_list, cases = load_cases(Path(args.set), only, args.per_scenario)
    scenarios = {s["id"]: s for s in scenario_list}
    # Scenarios that read the same world share one project (their rows have the same ids).
    pids: dict[str, str] = {}
    by_world: dict[str, str] = {}
    for sid, sc in scenarios.items():
        world = json.dumps(sc.get("seed") or {"own": sid}, sort_keys=True, default=str)
        if world in by_world:
            pids[sid] = by_world[world]
            continue
        pids[sid] = prepare(engine, settings, {**sc, "id": owner.get(world, sid)},
                            model_settings)
        by_world[world] = pids[sid]

    # Interleaved by project, so workers never queue behind one project's lock.
    queues: dict[str, list] = {}
    for case in cases:
        queues.setdefault(pids[case["scenario"]], []).append(case)
    order: list = []
    while any(queues.values()):
        for q in sorted(queues.values(), key=len, reverse=True):
            if q:
                order.append(q.pop(0))
    cases = order

    judge_adapter = None
    if not args.no_judge:
        from boss_engine.services.claude_cli_adapter import ClaudeCliAdapter

        judge_adapter = ClaudeCliAdapter(settings, model=args.judge_model)
        judge_adapter._battle_judge = True

    started = time.time()
    print(f"agent battle: {len(cases)} cases in {len(scenarios)} scenarios, "
          f"{args.workers} at a time", flush=True)
    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = [pool.submit(contextvars.copy_context().run, run_case, engine, settings,
                               scenarios, pids, case, judge_adapter) for case in cases]
        for future in as_completed(futures):
            r = future.result()
            results.append(r)
            mark = "PASS" if r["passed"] else "FAIL"
            why = "; ".join(r["deterministic_failures"]) or (
                r["judge"]["why"] if r["judge"]["verdict"] != "pass" else "")
            print(f"[{len(results)}/{len(cases)}] {mark} {r['scenario']}#{r['n']} "
                  f"({r['seconds']}s) {why[:160]}", flush=True)
    results.sort(key=lambda r: (r["scenario"], r["n"]))
    per: dict[str, dict] = {}
    for r in results:
        s = per.setdefault(r["scenario"], {"passed": 0, "total": 0, "controls_passed": 0,
                                           "controls": 0, "seconds": 0.0})
        s["total"] += 1
        s["passed"] += int(r["passed"])
        if r["kind"] == "negative":
            s["controls"] += 1
            s["controls_passed"] += int(r["passed"])
        s["seconds"] = round(s["seconds"] + r["seconds"], 1)
    for s in per.values():
        s["rate"] = round(s["passed"] / s["total"], 3) if s["total"] else 0.0
    passed = sum(r["passed"] for r in results)
    report = {
        "label": args.label, "started": datetime.fromtimestamp(started, timezone.utc).isoformat(),
        "wall_seconds": round(time.time() - started, 1),
        "code": {"sha": _git("rev-parse", "--short", "HEAD"),
                 "branch": _git("rev-parse", "--abbrev-ref", "HEAD")},
        "model_settings": model_settings, "judge_model": None if args.no_judge else args.judge_model,
        "totals": {"passed": passed, "total": len(results),
                   "rate": round(passed / len(results), 3) if results else 0.0,
                   "agent_model_calls": _TOTAL["calls"],
                   "agent_cost_usd": round(_TOTAL["cost_usd_micros"] / 1e6, 4),
                   "judge_model_calls": _TOTAL["judge_calls"],
                   "judge_cost_usd": round(_TOTAL["judge_cost_usd_micros"] / 1e6, 4),
                   "case_seconds": round(sum(r["seconds"] for r in results), 1)},
        "scenarios": per, "cases": results,
    }
    RESULTS.mkdir(parents=True, exist_ok=True)
    out = Path(args.out) if args.out else RESULTS / (
        f"{args.label}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json")
    out.write_text(json.dumps(report, indent=1, default=str))
    print("\nscenario                              pass/total")
    for sid, s in sorted(per.items()):
        print(f"  {sid:38s} {s['passed']:>3}/{s['total']:<3} ({s['rate']:.0%})")
    t = report["totals"]
    print(f"TOTAL {t['passed']}/{t['total']} ({t['rate']:.0%})  wall {report['wall_seconds']}s  "
          f"agent ${t['agent_cost_usd']} in {t['agent_model_calls']} calls, judge "
          f"${t['judge_cost_usd']} in {t['judge_model_calls']} calls")
    print(f"results: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
