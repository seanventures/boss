"""Phase 3 battle test, scripted: precedent scenarios (tests/agent-battle/precedent.yaml) must score >= 80%.

The decider is scripted: it reads ONLY Jev's knowledge pack (retrieval.knowledge_pack, recipe jev),
finds the precedent it cites, and picks the option that repeats a success or avoids a failure or a
rejection. So the score measures whether the pack brings the right precedent with its outcome.
The real-agent run is not approved; its estimated cost is in docs/product/KNOWLEDGE_GRAPH_PHASE3.md.
"""
from __future__ import annotations

import re
import uuid
from pathlib import Path

import yaml
from sqlalchemy import text

from apps.api.app.db import begin
from boss_engine.services.knowledge import Scope
from boss_engine.services.knowledge import retrieval as rt

SCENARIOS = yaml.safe_load((Path(__file__).resolve().parents[3] / "tests/agent-battle/precedent.yaml").read_text())
STOP = {"the", "a", "to", "with", "and", "of", "on", "it", "for", "without", "through", "first"}


def _words(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]+", s.lower()) if w not in STOP and len(w) > 2}


def scripted_decide(pack: rt.Pack, options: dict) -> tuple[str, str]:
    """(option, cited decision id) from the pack's precedent lines alone."""
    for line in pack.text.splitlines():
        m = re.match(r"- (.*) -> (\w+); outcome: ([a-z ]+?)(?:; why: (.*))? \(decision:(\S+)\)$", line)
        if not m:
            continue
        situation, decided, outcome, why, did = m.groups()
        good = decided != "rejected" and outcome == "succeeded"
        past = _words(situation + " " + (why or ""))
        # the option most like what happened, kept if it went well, avoided if it did not
        like = max(options, key=lambda k: (len(_words(options[k]) & past), k))
        other = next(k for k in options if k != like)
        return (like if good else other), did
    return sorted(options)[0], ""


def test_precedent_scenarios_score_at_least_80_percent(engine):
    score, cited, rows = 0, 0, []
    with begin(engine) as c:
        base = c.execute(text("SELECT id FROM projects ORDER BY created_at LIMIT 1")).scalar_one()
        for sc in SCENARIOS:
            pid = "proj-kg-prec-" + uuid.uuid4().hex[:6]
            c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) SELECT :n, workspace_id, :n, :n "
                           "FROM projects WHERE id = :p"), {"n": pid, "p": base})
            for past in sc["past"]:
                did = f"{past['id']}-{pid}"
                c.execute(text("INSERT INTO decisions (id, project_id, question, status, resolution_note) "
                               "VALUES (:i, :p, :q, :s, :n)"),
                          {"i": did, "p": pid, "q": past["question"], "s": past["status"], "n": past["note"]})
                if past["outcome"] in ("failed", "completed"):
                    c.execute(text("INSERT INTO code_task_runs (id, project_id, title, status, spec, state, "
                                   "finalize_ctx, decision_id) VALUES (:r, :p, 'run', :s, '{}'::jsonb, '{}'::jsonb, "
                                   "'{}'::jsonb, :d)"),
                              {"r": "run-" + uuid.uuid4().hex[:8], "p": pid, "s": past["outcome"], "d": did})
            situation = sc["ask"] + " Options: " + "; ".join(sc["options"].values())
            pack = rt.knowledge_pack(c, rt.Reader(Scope(pid)), "jev", "decide", {"text": situation}, 600)
            choice, did = scripted_decide(pack, sc["options"])
            ok = choice == sc["expect"]
            score += ok
            cited += did == f"{sc['cite']}-{pid}"
            rows.append((sc["id"], choice, sc["expect"], did))
    rate = score / len(SCENARIOS)
    print({"precedent_score": rate, "cited_expected": cited, "cases": rows})
    assert rate >= 0.8, rows
