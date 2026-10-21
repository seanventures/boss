"""Knowledge Graph Phase 2 acceptance: fact precision >= 85% on 100 labelled facts (scripted, no model).

The corpus and the scripted Archivist model are in scripts/knowledge/precision/corpus.py. The run goes
through the real pipeline: the gateway (`model_routing.governed`, category `knowledge`), scrubbing,
validation, the judgement calls, merge and supersession. It also measures the exact prompt and reply
sizes, which price the real-model run (not approved; set BOSS_KG_PRECISION_OUT to write the report).
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text

from apps.api.app.db import begin
from boss_engine.services.knowledge import Scope
from boss_engine.services.knowledge.archivist import Archivist

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts/knowledge/precision"))
import corpus  # noqa: E402

PRICED = ("claude-sonnet-5", "qwen3-32b")


@pytest.fixture
def scope(engine):
    with begin(engine) as c:
        pid = c.execute(text("SELECT id FROM projects ORDER BY created_at LIMIT 1")).scalar_one()
        new = "proj-kg-prec-" + uuid.uuid4().hex[:6]
        c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) SELECT :n, workspace_id, :n, :n "
                       "FROM projects WHERE id = :p"), {"n": new, "p": pid})
    return Scope(new)


def test_fact_precision_on_100_labelled_facts(engine, scope):
    from boss_engine.config import get_settings
    from boss_engine.services.openai_adapter import price_for_model

    model = corpus.ScriptedArchivist()
    records = corpus.records()
    labels = corpus.labels()
    assert sum(1 for r in records for f in r["gold"] if f["label"] == "correct") >= 100
    archivist = Archivist(get_settings(), adapter_factory=lambda *a: model)
    with begin(engine) as c:
        for r in records:
            c.execute(text("INSERT INTO code_task_runs (id, project_id, title, status, spec, state, finalize_ctx) "
                           "VALUES (:r, :p, :t, 'completed', CAST(:s AS JSONB), '{}'::jsonb, '{}'::jsonb)"),
                      {"r": r["id"], "p": scope.project_id, "t": f"Run {r['id']}",
                       "s": json.dumps({"instructions": r["text"]})})
            archivist.ingest(c, scope, "run", r["id"])
        rows = c.execute(text(
            "SELECT f.id, f.statement, f.tier, (SELECT min(s.evidence) FROM kg_fact_sources s WHERE s.fact_id = f.id) "
            "AS ev FROM kg_facts f WHERE f.project_id = :p AND f.valid_to IS NULL"), {"p": scope.project_id}).all()
        closed = c.execute(text("SELECT count(*) FROM kg_facts WHERE project_id = :p AND valid_to IS NOT NULL"),
                           {"p": scope.project_id}).scalar()
        stored = " ".join(r.statement for r in rows)
    cases = {r.id: next((p.split(":", 1)[1] for p in (r.ev or "").split("; ") if p.startswith("case:")), None)
             for r in rows}
    verdicts = {fid: labels.get(case, "unlabelled") for fid, case in cases.items()}
    correct = sum(v == "correct" for v in verdicts.values())
    precision = correct / len(rows)
    tokens_in = sum(len(s) + len(p) for s, p in model.prompts) / 4
    tokens_out = sum(len(x) for x in model.replies) / 4
    cost = {}
    for name in PRICED:
        inp, out, _ = price_for_model(name)
        cost[name] = round((tokens_in * inp + tokens_out * out) / 1e6, 4)
    report = {"records": len(records), "labelled_facts": sum(len(r["gold"]) for r in records),
              "stored_current": len(rows), "correct": correct, "precision": round(precision, 3),
              "closed": closed, "by_label": {k: list(verdicts.values()).count(k) for k in set(verdicts.values())},
              "tiers": sorted({r.tier for r in rows}), "model_calls": len(model.prompts),
              "tokens_in": int(tokens_in), "tokens_out": int(tokens_out), "real_run_cost_usd": cost}
    if os.environ.get("BOSS_KG_PRECISION_OUT"):
        Path(os.environ["BOSS_KG_PRECISION_OUT"]).write_text(json.dumps(report, indent=2) + "\n")
    assert precision >= 0.85, report
    assert report["tiers"] == ["inferred"]                       # distilled facts are never above inferred
    assert corpus.SECRET not in stored and "unlabelled" not in verdicts.values()
    assert report["by_label"].get("stale", 0) == 0               # every update superseded its fact
