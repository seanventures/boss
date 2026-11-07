"""A seeded demo of BOSS's agents working together on what they noticed (BOSSAPP-904/909).

WHAT IT SHOWS (the operator's bar: "show the agents working together towards the goal")
-------------------------------------------------------------------------------------
One morning on a shop's project, seven things happen that no person asked about. The scenario
records them as REAL signals and runs the REAL Chief-of-Staff triage over them (no shortcuts:
the same board, gate, guards and ledger), then plays the owners' first turns so every branch
of the flow is on screen:

  signal                               the Chief of Staff …            then
  ───────────────────────────────────  ──────────────────────────────  ───────────────────────────
  order-api 5xx at 6.6% (+ p99 alert)  joins both into ONE incident    asks you: "Hand to SRE?"
  CI failed on BOSS PR #41             routes it (A2) to the engineer  engineer proposes a draft
                                                                       PR, reviewer agrees,
                                                                       resolved
  lodash advisory (GHSA)               routes it (A2) to security      security is working on it
  spend 18× the weekly mean            routes it (A1) to FinOps        FinOps picks it up
  PR #37 waiting 4 days                nudges it itself (A1): its own  recorded; nobody asked
                                       act inside BOSS, not a hand-off
  14 TODOs in billing/                 observes (A0): digest only
  CI fails on #41 once more (repeat)   folds into the fixed problem    no second card (×2)

The demo is for a demo or test project only: the API refuses it unless the project's name says
demo/sample or BOSS_AMBIENT_DEMO=1. Every row it writes is ordinary data (it shows in Agent
Operations like real work) and carries `demo:` source refs, so it is recognisable and re-running
it records nothing twice.
"""
from __future__ import annotations

import argparse
import json
import os
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from . import board, chief_of_staff, collab, ledger, signals

REPO = "northpeak/shop"
PR_41 = f"pr:{REPO}#41"

#: The morning's signals, in the shape record_signal takes.
SCENARIO: tuple[dict[str, Any], ...] = (
    {"source": "alert", "kind": "alert.firing", "title": "order-api 5xx at 6.6%",
     "subject_kind": "app", "subject_id": "order-api", "app": "order-api", "severity": "high",
     "fingerprint_parts": ("alert", "order-api", "HighErrorRate"),
     "evidence": [{"kind": "query", "ref": "rate(http_5xx{app=\"order-api\"}[5m]) = 6.6%"}]},
    {"source": "alert", "kind": "alert.firing", "title": "order-api errors on POST /orders",
     "subject_kind": "app", "subject_id": "order-api", "app": "order-api", "severity": "critical",
     "fingerprint_parts": ("alert", "order-api", "OrdersErrors"),
     "evidence": [{"kind": "log", "ref": "POST /orders 500 OrderService.total"}]},
    {"source": "ci", "kind": "ci.failed", "title": "CI failed on BOSS PR: unit-tests",
     "subject_kind": "pull_request", "subject_id": PR_41, "severity": "high",
     "fingerprint_parts": ("ci", PR_41, "unit-tests"),
     "evidence": [{"kind": "pull_request", "ref": f"{REPO}#41"},
                  {"kind": "url", "ref": f"https://github.com/{REPO}/actions/runs/9141"}]},
    {"source": "osv", "kind": "osv.vulnerable_dependency",
     "title": "lodash 4.17.20 has GHSA-35jh-r3h4-6jhm", "subject_kind": "dependency",
     "subject_id": "lodash", "app": "web", "severity": "high",
     "fingerprint_parts": ("osv", "lodash", "GHSA-35jh-r3h4-6jhm"),
     "evidence": [{"kind": "url", "ref": "https://osv.dev/vulnerability/GHSA-35jh-r3h4-6jhm"}]},
    {"source": "cost", "kind": "cost.spend_spike", "title": "Spend $9.00 in 24h, 18× the mean",
     "subject_kind": "budget", "subject_id": "builds", "severity": "high",
     "fingerprint_parts": ("spend_spike", "demo"),
     "evidence": [{"kind": "query", "ref": "cost_receipts last 24h $9.00; 7-day mean $0.50/day"}]},
    {"source": "pr", "kind": "pr.stale", "title": "PR #37 waiting 4d for a merge decision",
     "subject_kind": "pull_request", "subject_id": f"pr:{REPO}#37", "severity": "low",
     "fingerprint_parts": ("pr", f"{REPO}#37"),
     "evidence": [{"kind": "pull_request", "ref": f"{REPO}#37"}]},
    {"source": "scan", "kind": "scan.todo_cluster", "title": "14 TODOs in billing/",
     "subject_kind": "repo", "subject_id": REPO, "severity": "low",
     "fingerprint_parts": ("scan.todo_cluster", REPO, "billing"),
     "evidence": [{"kind": "file", "ref": "billing/invoice.py"}]},
)


def allowed(connection: Connection, project_id: str) -> bool:
    """Only a demo or test project (or an install that opted in) gets the demo's rows."""
    if str(os.environ.get("BOSS_AMBIENT_DEMO", "")).strip().lower() in {"1", "true", "yes"}:
        return True
    name = connection.execute(text("SELECT lower(name || ' ' || COALESCE(slug, '')) FROM "
                                   "projects WHERE id = :p"), {"p": project_id}).scalar()
    return bool(name) and any(w in name for w in ("demo", "sample", "sandbox", "test"))


def _record(connection: Connection, project_id: str, n: int, spec: dict[str, Any],
            *, repeat: int = 1) -> dict[str, Any]:
    got = None
    for k in range(repeat):
        got = signals.record_signal(connection, project_id=project_id,
                                    source_ref=f"demo:{n}:{k}", actor="boss", **spec)
    return got["signal"]


def _engineer_fixes_ci(connection: Connection, project_id: str, item_id: str) -> None:
    """The software engineer's turn on the CI item: claim, diagnose, propose a draft PR, the
    adversarial reviewer agrees (maker-checker), decide through the gate, resolve."""
    engineer, reviewer = "agent:software_engineer", "agent:adversarial_reviewer"
    held = board.claim(connection, item_id, owner=engineer)
    token = held["lease_token"]
    board.start(connection, item_id, owner=engineer, token=token)
    sig = [l["ref"] for l in board.links(connection, item_id) if l["kind"] == "signal"][0]
    collab.post(connection, item_id, kind="finding", sender=engineer, body={
        "summary": "test_total expects rounding the PR changed",
        "evidence": [{"kind": "signal", "ref": sig},
                     {"kind": "pull_request", "ref": f"{REPO}#41"}]})
    proposal = collab.post(connection, item_id, kind="proposal", sender=engineer, body={
        "action": "open_draft_pr", "summary": "Fix the rounding test", "confidence": 0.86,
        "verify": "unit-tests green", "undo": {"action": "close_pr"}})
    collab.post(connection, item_id, kind="critique", sender=reviewer, reply_to=proposal["id"],
                body={"position": "agree", "summary": "Matches the spec; no behaviour change"})
    record = collab.tally(connection, item_id, proposal["id"])
    board.assert_lease(connection, item_id, owner=engineer, token=token, lock=True)
    ledger.record_decision(
        connection, project_id=project_id, decided_by=engineer, work_item_id=item_id,
        question="Fix CI on PR #41?", chosen="open_draft_pr", action="open_draft_pr",
        options=[{"key": "open_draft_pr", "label": "Open draft PR"},
                 {"key": "ask_author", "label": "Ask the author"}],
        proposal_id=proposal["id"], consensus=record,
        rationale="The PR changed rounding and one test still expects the old value. A one-line "
                  "test fix keeps the behaviour.",
        reversibility="reversible", undo={"action": "close_pr"}, confidence=0.86,
        evidence=[{"kind": "signal", "ref": sig}, {"kind": "pull_request", "ref": f"{REPO}#41"}],
        cost_usd_micros=420_000)
    board.link(connection, item_id, "pull_request", f"https://github.com/{REPO}/pull/42",
               actor=engineer, note="draft fix")
    board.finish(connection, item_id, actor=engineer, lifecycle="resolved", token=token,
                 outcome="CI green on #41 (draft PR #42)")


def _security_starts(connection: Connection, project_id: str, item_id: str) -> None:
    """The security reviewer picks up the advisory and is mid-way: claimed, a finding posted."""
    who = "agent:security_reviewer"
    held = board.claim(connection, item_id, owner=who, minutes=60)
    board.start(connection, item_id, owner=who, token=held["lease_token"])
    sig = [l["ref"] for l in board.links(connection, item_id) if l["kind"] == "signal"][0]
    collab.post(connection, item_id, kind="hypothesis", sender=who, body={
        "summary": "lodash 4.17.21 fixes it; no API change", "confidence": 0.8,
        "evidence": [{"kind": "signal", "ref": sig}]})


def load(connection: Connection, settings: Any, *, project_id: str, mode: str = "route",
         play_owners: bool = True) -> dict[str, Any]:
    """Record the morning's signals, run the Chief of Staff over them, and play the owners'
    first turns. Idempotent: a second run records nothing new and opens no second item."""
    if not allowed(connection, project_id):
        raise PermissionError("the ambient demo is only for a demo or test project "
                              "(name it 'demo…' or set BOSS_AMBIENT_DEMO=1)")
    recorded = [_record(connection, project_id, n, spec, repeat=3 if n == 0 else 1)
                for n, spec in enumerate(SCENARIO)]
    triage = chief_of_staff.triage_project(connection, settings, project_id, mode=mode,
                                           max_model_calls=0)
    played: list[str] = []
    if play_owners:
        for entry in triage["items"]:
            if entry.get("outcome") != "routed":
                continue
            if entry["task_class"] == "ci_fix_boss_pr":
                _engineer_fixes_ci(connection, project_id, entry["work_item_id"])
                played.append("software_engineer")
            elif entry["task_class"] == "security_patch":
                _security_starts(connection, project_id, entry["work_item_id"])
                played.append("security_reviewer")
    # CI fails on #41 once more after the fix: the same problem (the item is resolved, so the
    # signal folds into its row inside the dedupe window) — no second card.
    _record(connection, project_id, 99, SCENARIO[2])
    return {"project_id": project_id, "signals": [s["id"] for s in recorded],
            "triage": triage, "owners_played": played}



# ── everything at once: the console's "Load demo" ────────────────────────────────────────────

LOADERS = ("order_api_incident", "morning", "squad_cve", "roadmap")


def status(connection: Connection, project_id: str) -> dict[str, Any]:
    """Whether "Load demo" is offered on this project (a demo/test project only)."""
    ok = allowed(connection, project_id)
    return {"available": ok, "loaders": list(LOADERS),
            "reason": "" if ok else "only a demo or test project (its name says demo, sample, "
                                    "sandbox or test) can load the demo"}


def load_all(engine: Any, settings: Any, *, project_id: str, actor: str) -> dict[str, Any]:
    """Populate a fresh demo project with every story, through the real machinery:

      order_api_incident  alert → signal → Chief of Staff → SRE squad → Needs you (production
                          rollback, the squad's dissent on the card)
      morning             the demo morning: CI, CVE, spend, stale PR, TODOs triaged; the second
                          order-api alert JOINS the incident above (one problem, one item)
      squad_cve           the four-agent CVE squad: critique, panel, dissent, run, Needs you
      roadmap             the Recommended roadmap's history, detectors and two decided items

    Each loader runs in its own transaction; one that cannot run (already loaded, refused) is
    reported under `skipped` with why, and the rest still load."""
    from .. import incident_squad_demo
    from . import roadmap_demo, squad_demo

    with engine.begin() as c:
        if not allowed(c, project_id):
            raise PermissionError(status(c, project_id)["reason"])
    loaded: dict[str, Any] = {k: None for k in LOADERS}
    skipped: dict[str, str] = {}

    def run(key: str, fn: Any) -> None:
        try:
            loaded[key] = fn()
        except Exception as exc:  # noqa: BLE001 — reported, the next loader still runs
            skipped[key] = f"{type(exc).__name__}: {exc}"[:300]

    def incident() -> dict[str, Any]:
        with engine.begin() as c:
            if c.execute(text("SELECT 1 FROM work_items WHERE project_id = :p AND ambient "
                              "AND subject_ref = 'demo:order-api' LIMIT 1"),
                         {"p": project_id}).first():
                raise RuntimeError("already loaded")
            out = incident_squad_demo.run_through_chief_of_staff(c, project_id,
                                                                 settings=settings)
        return {"item_id": out["item_id"], "stages": out["stages"],
                "needs_you_decision_id": out["needs_you_decision_id"]}

    def morning() -> dict[str, Any]:
        with engine.begin() as c:
            out = load(c, settings, project_id=project_id, mode="route")
        return {"signals": len(out["signals"]), "counts": out["triage"].get("counts") or {},
                "owners_played": out["owners_played"]}

    def cve() -> dict[str, Any]:
        out = squad_demo.seed(engine, settings, project_id=project_id, confirm_demo=True,
                              ensure_roles=True)
        return {"item_id": out["item_id"], "stopped": (out["run"].get("stopped") or {}).get(
            "guard")}

    def roadmap() -> dict[str, Any]:
        with engine.begin() as c:
            out = roadmap_demo.seed(c, project_id, settings=settings, actor=actor)
        return {"report": out["report"], "decided": out["decided"]}

    run("order_api_incident", incident)
    run("morning", morning)
    run("squad_cve", cve)
    run("roadmap", roadmap)
    return {"project_id": project_id, "loaded": loaded, "skipped": skipped}


def main(argv: Optional[list[str]] = None) -> int:
    """python -m boss_engine.services.ambient.demo --project <id> [--mode route|propose]"""
    from ...config import get_settings
    from ...db import get_engine

    parser = argparse.ArgumentParser(description=main.__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--mode", default="route", choices=("route", "propose"))
    args = parser.parse_args(argv)
    settings = get_settings()
    with get_engine(settings).begin() as c:
        out = load(c, settings, project_id=args.project, mode=args.mode)
    print(json.dumps({"project_id": out["project_id"], "signals": len(out["signals"]),
                      "counts": out["triage"].get("counts"),
                      "owners_played": out["owners_played"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
