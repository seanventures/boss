"""AP-P4 acceptance on a real database (docs/design/AGENT_PLATFORM.md §5 Phase 4; BOSSAPP-1055).

(a) From a decision id, the trace API returns the full run tree within two hops (decision →
    run tree in one request; any span's redacted payload in the next), and the costs of the
    spans sum to the run's llm_calls total.
(b) A gate waiver produces a PROPOSED example; once a person confirms it, it fails the next
    (scripted) battle run, and every one after, until the agent's answer is fixed.
(c) An online evaluator sampling 10% stays under its weekly cap and pauses at the cap.
(d) The governed-versus-bare bench result for a real engine, from recorded runs, is served with
    noise bands.

Plus: payload tiers (hot → cold → expired, extended retention), "add to eval set" from a span,
the agent-health read, and the run-end hook that scores live runs.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import text

from boss_engine import kernel
from boss_engine.kernel import presets
from boss_engine.kernel.ledger import DbSink
from boss_engine.kernel.scripted import Call, Say, ScriptedEngine
from boss_engine.kernel.toolset import FunctionToolSet, Tool
from boss_engine.kernel.types import ToolSchema

PROJECT = "boss-dogfood"
ARGS = {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"],
        "additionalProperties": False}
TOKEN = "ghp_" + "Z9y8X7w6V5u4T3s2R1q0P9o8N7m6L5k4J3i2"


def _decision(engine, question: str = "Deploy v2 to staging?") -> str:
    did = f"dec_{uuid4().hex[:12]}"
    with engine.begin() as c:
        c.execute(text("INSERT INTO decisions (id, project_id, question) VALUES (:i, :p, :q)"),
                  {"i": did, "p": PROJECT, "q": question})
    return did


def _tools(engine, decision_id: str, child_runs: list) -> FunctionToolSet:
    def task(args):
        child = kernel.run(_spec(engine, [Call("read_logs", {"q": "child"}), Say("child done")],
                                 actor="agent:helper"))
        child_runs.append(child.run_id)
        return {"ok": True, "run_id": child.run_id, "summary": child.text}

    return FunctionToolSet([
        Tool(ToolSchema("read_logs", "Read logs.", ARGS),
             lambda a: {"ok": True, "lines": [f"{a['q']}: 3 errors"]}),
        Tool(ToolSchema("task", "Ask a helper agent.", ARGS), task),
        Tool(ToolSchema("deploy", "Deploy.", ARGS, kind="write"),
             lambda a: {"status": "awaiting_approval", "decision_id": decision_id},
             run_approved=lambda a: {"ok": True, "deployed": a["q"]}),
    ])


def _spec(engine, script, *, tools=None, actor="agent:triage", **kw):
    return kernel.RunSpec(
        engine=ScriptedEngine(list(script), cost_usd_micros=2_500, model="scripted-v1"),
        tools=tools or FunctionToolSet([Tool(ToolSchema("read_logs", "Read logs.", ARGS),
                                             lambda a: {"ok": True})]),
        policies=presets.agent_loop(8), project_id=PROJECT, system="You triage.",
        messages=[{"role": "user", "content": f"triage it; the token is {TOKEN}"}],
        sink=DbSink(engine), sleep=lambda s: None, actor=actor, **kw)


def _ledger_total(engine, run_id: str) -> int:
    with engine.connect() as c:
        return int(c.execute(text("SELECT COALESCE(sum(cost_usd_micros), 0) FROM llm_calls "
                                  "WHERE run_id = :r"), {"r": run_id}).scalar())


# ── (a) ───────────────────────────────────────────────────────────────────────────────────────

def test_a_decision_opens_the_full_run_tree_and_span_costs_sum_to_llm_calls(client, engine):
    did = _decision(engine)
    children: list = []
    tools = _tools(engine, did, children)
    first = kernel.run(_spec(engine, [Call("read_logs", {"q": "api"}), Call("task", {"q": "db"}),
                                      Call("deploy", {"q": "v2"}), Say("deployed")],
                             tools=tools))
    assert first.status == "suspended", first.error
    resumed = kernel.resume(_spec(engine, [Say("deployed v2")], tools=tools, run_id=first.run_id),
                            decision={"approved": True, "decision_id": did,
                                      "args_sha256": first.pending.get("args_sha256")})
    assert resumed.status == "completed", resumed.error
    run_id = first.run_id
    # A model call recorded for the run without a span of its own (a delegate CLI, a path not
    # yet on the kernel): it is still in the tree, so the sum still holds.
    with engine.begin() as c:
        c.execute(text("INSERT INTO llm_calls (id, project_id, run_id, stage, model, "
                       "cost_usd_micros, ok) VALUES (:i, :p, :r, 'delegate', 'cli', 777, true)"),
                  {"i": f"llm_{uuid4().hex[:12]}", "p": PROJECT, "r": run_id})

    # HOP 1: the decision card → its run's whole tree, in one request.
    got = client.get(f"/v1/decisions/{did}/trace", params={"project_id": PROJECT})
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["run_id"] == run_id and body["found_by"] == "interrupt_span"
    tree = body["trace"]
    kinds = [s["kind"] for s in tree["spans"]]
    assert kinds[0] == "run" and {"step", "model", "tool", "subagent", "interrupt"} <= set(kinds)
    assert tree["status"] == "ok" and tree["agent_key"] == "triage"
    assert tree["traceparent"] == f"00-{tree['trace_id']}-{tree['spans'][0]['span_id']}-01"
    interrupt = next(s for s in tree["spans"] if s["kind"] == "interrupt")
    assert interrupt["decision_id"] == did and interrupt["status"] == "ok"

    # Costs: every span's cost is its llm_calls row's; they sum to the run's llm_calls total.
    total = _ledger_total(engine, run_id)
    assert total == 4 * 2_500 + 777
    assert sum(s["cost_usd_micros"] for s in tree["spans"]) == total
    assert tree["totals"]["cost_usd_micros"] == tree["totals"]["llm_calls_cost_usd_micros"] == total
    assert tree["totals"]["reconciled"] is True
    assert any(s["attributes"].get("from_ledger") for s in tree["spans"]), \
        "the span-less llm_calls row is shown, not dropped"
    models = [s for s in tree["spans"] if s["kind"] == "model" and s["llm_call_id"]]
    assert all(s["usage"]["input_tokens"] == 10 for s in models if not s["attributes"].get(
        "from_ledger"))

    # The helper's run is a child of the subagent span, with its own costs.
    assert len(tree["children"]) == 1
    child = tree["children"][0]
    assert child["run_id"] == children[0] and child["trace_id"] == tree["trace_id"]
    sub = next(s for s in tree["spans"] if s["kind"] == "subagent")
    assert child["spans"][0]["parent_span_id"] == sub["span_id"]
    assert sub["child_run_id"] == children[0]
    assert child["totals"]["reconciled"] and child["totals"]["cost_usd_micros"] == \
        _ledger_total(engine, children[0])
    assert tree["totals"]["with_children_cost_usd_micros"] == total + \
        child["totals"]["cost_usd_micros"]

    # Waterfall geometry for the viewer.
    for span in tree["spans"]:
        assert 0 <= span["offset_pct"] <= 100 and 0 <= span["width_pct"] <= 100
        assert span["depth"] >= 0

    # HOP 2: any span's full input and output — redacted before it was stored.
    first_model = next(s for s in tree["spans"] if s["kind"] == "model" and s["input_ref"])
    detail = client.get(f"/v1/spans/{first_model['span_id']}", params={"project_id": PROJECT})
    assert detail.status_code == 200, detail.text
    span = detail.json()
    stored = json.dumps(span["input"]["body"])
    assert "triage it" in stored and TOKEN not in stored
    assert span["input"]["tier"] == "hot" and span["input"]["redactions"].get("SECRET")
    assert span["cost_usd_micros"] == 2_500
    with engine.connect() as c:
        raw = c.execute(text("SELECT string_agg(body::text, ' ') FROM trace_payloads "
                             "WHERE run_id = :r"), {"r": run_id}).scalar()
    assert TOKEN not in raw, "no payload row holds the secret"

    # The run trace directly, and a decision with no run.
    assert client.get(f"/v1/runs/{run_id}/trace", params={"project_id": PROJECT}).json()[
        "run_id"] == run_id
    lonely = _decision(engine, "Rename the project?")
    none = client.get(f"/v1/decisions/{lonely}/trace", params={"project_id": PROJECT}).json()
    assert none["trace"] is None and none["note"]
    assert client.get("/v1/decisions/dec_nope/trace",
                      params={"project_id": PROJECT}).status_code == 404


def test_a_code_task_run_without_spans_is_still_a_tree_from_its_ledger(client, engine):
    run_id = f"crun_{uuid4().hex[:12]}"
    did = _decision(engine, "Merge the PR?")
    with engine.begin() as c:
        c.execute(text("INSERT INTO code_task_runs (id, project_id, title, status, decision_id) "
                       "VALUES (:r, :p, 'Fix fee rounding', 'completed', :d)"),
                  {"r": run_id, "p": PROJECT, "d": did})
        for cost in (1_000, 2_000):
            c.execute(text("INSERT INTO llm_calls (id, project_id, code_task_run_id, stage, "
                           "model, cost_usd_micros, ok) VALUES (:i, :p, :r, 'implement', "
                           "'claude', :c, true)"),
                      {"i": f"llm_{uuid4().hex[:12]}", "p": PROJECT, "r": run_id, "c": cost})
    body = client.get(f"/v1/decisions/{did}/trace", params={"project_id": PROJECT}).json()
    assert body["found_by"] == "code_task_run" and body["run_id"] == run_id
    tree = body["trace"]
    assert tree["name"] == "Fix fee rounding" and tree["totals"]["cost_usd_micros"] == 3_000
    assert tree["totals"]["reconciled"] and [s["kind"] for s in tree["spans"]] == [
        "run", "model", "model"]


# ── (b) ───────────────────────────────────────────────────────────────────────────────────────

ALL_PASS = {"tests_passed": True, "ci_green": True, "lint_passed": True,
            "reviewer_independent": True, "test_author_independent": True,
            "adversarial_verdict": "survived"}


def _misses(client) -> dict:
    found = [d for d in client.get("/v1/datasets", params={"project_id": PROJECT}).json()["items"]
             if d["name"] == "misses"]
    return found[0]


def _battle(client, dataset_id: str, **body) -> dict:
    got = client.post(f"/v1/datasets/{dataset_id}/experiments",
                      json={"project_id": PROJECT, "actor": "ceo", "mode": "scripted", **body})
    assert got.status_code == 201, got.text
    return got.json()


def test_b_a_waived_gate_is_proposed_then_fails_every_battle_until_fixed(client, engine,
                                                                         tmp_path):
    from apps.api.app.config import get_settings
    from boss_engine.services.factory_scale import FactoryScaleService

    run_id = f"crun_{uuid4().hex[:12]}"
    gates = FactoryScaleService(get_settings())
    with engine.begin() as c:
        failed = gates.record_quality_gate(c, PROJECT, checks={**ALL_PASS, "tests_passed": False},
                                           actor="release_manager", code_task_run_id=run_id)
        waived = gates.record_quality_gate(c, PROJECT, checks=ALL_PASS, actor="ceo",
                                           code_task_run_id=run_id,
                                           override_reason="the flaky test is quarantined")
    assert failed["status"] == "fail" and waived["status"] == "waived"

    inbox = client.get("/v1/eval-inbox", params={"project_id": PROJECT}).json()["items"]
    proposed = [e for e in inbox if e["run_id"] == run_id]
    assert len(proposed) == 1, inbox
    example = proposed[0]
    assert example["status"] == "proposed" and example["source"] == "gate_waiver"
    assert example["reference"]["expected_verdict"] == "pass"
    assert example["observed"]["verdict"] == "fail"
    assert example["reference"]["reason"] == "the flaky test is quarantined"

    misses = _misses(client)
    # PROPOSED counts for nothing: the battle does not run it.
    before = _battle(client, misses["id"])
    assert example["id"] not in [r["example_id"] for r in client.get(
        f"/v1/dataset-experiments/{before['id']}", params={"project_id": PROJECT}).json()["results"]]

    review = client.post(f"/v1/dataset-examples/{example['id']}/review",
                         json={"project_id": PROJECT, "actor": "ceo", "verdict": "confirm",
                               "note": "the gate was wrong to block"})
    assert review.status_code == 200, review.text
    assert review.json()["status"] == "confirmed" and review.json()["reviewed_by"] == "ceo"
    again = client.post(f"/v1/dataset-examples/{example['id']}/review",
                        json={"project_id": PROJECT, "actor": "ceo", "verdict": "reject"})
    assert again.status_code == 409

    # Confirmed: it fails the next battle run, and the next, until it is fixed.
    for _ in range(2):
        xp = _battle(client, misses["id"])
        assert xp["status"] == "completed" and example["id"] in xp["summary"]["failing_examples"]
        assert xp["dataset_version"] > misses["version"]
    detail = client.get(f"/v1/dataset-experiments/{xp['id']}", params={"project_id": PROJECT})
    result = next(r for r in detail.json()["results"] if r["example_id"] == example["id"])
    assert result["passed"] is False
    assert result["failures"] == ["verdict 'fail', expected 'pass'"]
    assert result["output"]["verdict"] == "fail"
    # Each answer is a real kernel run with its own trace.
    assert client.get(f"/v1/runs/{result['run_id']}/trace",
                      params={"project_id": PROJECT}).status_code == 200

    # The battle runner's own callable (what the CLI and a P3 bot run): exit 1 while it fails.
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "agent_battle", Path(__file__).resolve().parents[3] / "scripts" / "agent_battle.py")
    battle = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(battle)
    assert battle.scripted_battle(engine, PROJECT, "misses", "") == 1

    # Fixed: the agent version that fixes it answers `pass`.
    fixes = tmp_path / "fixes.json"
    fixes.write_text(json.dumps({example["id"]: {"verdict": "pass"}}))
    assert battle.scripted_battle(engine, PROJECT, "misses", str(fixes)) == 0
    fixed = _battle(client, misses["id"], fixes={example["id"]: {"verdict": "pass"}})
    assert fixed["summary"]["failed"] == 0 and fixed["summary"]["passed"] >= 1

    # A real-engine battle is never started by a request (real-money runs are frozen).
    refused = client.post(f"/v1/datasets/{misses['id']}/experiments",
                          json={"project_id": PROJECT, "actor": "ceo", "mode": "real"})
    assert refused.status_code == 422


def test_a_delivery_sent_back_is_a_proposed_failed_acceptance(engine):
    from boss_engine.services import evals

    run_id = f"crun_{uuid4().hex[:12]}"
    with engine.begin() as c:
        got = evals.capture_failed_acceptance(
            c, PROJECT, run_id=run_id, review_id="drev_1", actor="ceo",
            note="the export ignores the date filter",
            snapshot={"delivered_head": "abc123", "criteria": ["exports honour the filter"]})
        again = evals.capture_failed_acceptance(
            c, PROJECT, run_id=run_id, review_id="drev_1", actor="ceo", note="x", snapshot={})
    assert got["status"] == "proposed" and got["source"] == "failed_acceptance"
    assert got["reference"]["expected_verdict"] == "changes_requested"
    assert again["duplicate"] is True and again["id"] == got["id"]


def test_add_to_eval_set_from_a_span_and_a_persons_feedback(client, engine):
    result = kernel.run(_spec(engine, [Call("read_logs", {"q": "api"}), Say("3 errors")]))
    tree = client.get(f"/v1/runs/{result.run_id}/trace", params={"project_id": PROJECT}).json()
    model = next(s for s in tree["spans"] if s["kind"] == "model")
    added = client.post(f"/v1/spans/{model['span_id']}/add-to-dataset",
                        json={"project_id": PROJECT, "actor": "ceo", "dataset_name": "triage-set",
                              "reference": {"mentions_any": ["error"]}})
    assert added.status_code == 201, added.text
    example = added.json()
    assert example["status"] == "confirmed" and example["source"] == "span"
    assert example["span_id"] == model["span_id"] and TOKEN not in json.dumps(example["input"])
    dup = client.post(f"/v1/spans/{model['span_id']}/add-to-dataset",
                      json={"project_id": PROJECT, "actor": "ceo", "dataset_name": "triage-set"})
    assert dup.json()["duplicate"] is True

    fb = client.post(f"/v1/runs/{result.run_id}/feedback",
                     json={"project_id": PROJECT, "actor": "ceo", "key": "useful", "score": 1,
                           "comment": "right answer"})
    assert fb.status_code == 201, fb.text
    assert fb.json()["source"] == "human"
    items = client.get(f"/v1/runs/{result.run_id}/feedback",
                       params={"project_id": PROJECT}).json()["items"]
    assert [i["key"] for i in items] == ["useful"]
    with engine.connect() as c:
        extended = c.execute(text("SELECT bool_and(extended) FROM trace_payloads WHERE "
                                  "run_id = :r"), {"r": result.run_id}).scalar()
    assert extended is True, "feedback keeps the run's payloads for the extended window"


# ── (c) ───────────────────────────────────────────────────────────────────────────────────────

def _agent(client, key: str, evaluators: list) -> None:
    doc = {"apiVersion": "boss.agent/v1", "kind": "Agent",
           "metadata": {"key": key, "name": "Eval " + key[-4:]},
           "spec": {"mission": "Triage issues.", "prompts": {"system": "You triage."},
                    "evaluators": evaluators}}
    made = client.post("/v1/agent-specs", json={"project_id": PROJECT, "actor": "ceo",
                                                "spec": doc})
    assert made.status_code == 201, made.text


def _run_span(engine, key: str, run_id: str, at: datetime) -> None:
    with engine.begin() as c:
        c.execute(text("""
            INSERT INTO trace_spans (span_id, trace_id, project_id, run_id, kind, name, status,
                agent_key, started_at, ended_at, duration_ms)
            VALUES (:s, :t, :p, :r, 'run', 'triage', 'ok', :k, :at, :at, 10)
        """), {"s": uuid4().hex[:16], "t": uuid4().hex, "p": PROJECT, "r": run_id, "k": key,
               "at": at})


def test_c_an_online_judge_at_ten_percent_stays_under_its_weekly_cap_and_pauses(client, engine):
    from boss_engine.services import online_evaluators as oe

    refused = client.post("/v1/agent-specs", json={"project_id": PROJECT, "actor": "ceo", "spec": {
        "apiVersion": "boss.agent/v1", "kind": "Agent",
        "metadata": {"key": f"ev_{uuid4().hex[:6]}", "name": "No cap"},
        "spec": {"mission": "x", "evaluators": [{"kind": "online", "name": "j", "by": "judge",
                                                 "sample": 0.1}]}}})
    assert refused.status_code == 422 and "weekly_usd_cap" in refused.text, \
        "an LLM judge is opt-in WITH a spend cap"

    key = f"ev_{uuid4().hex[:6]}"
    _agent(client, key, [
        {"kind": "online", "name": "finished", "by": "rule", "rule": "completed", "sample": 1.0},
        {"kind": "online", "name": "quality", "by": "judge", "sample": 0.1,
         "weekly_usd_cap": 0.05, "criteria": {"answer": "cites the failing check"}}])
    calls: list = []

    def judge(c, *, project_id, evaluator, trace, max_usd_micros):
        cost = min(10_000, max_usd_micros)        # $0.01 a call, never past the bound handed in
        calls.append(max_usd_micros)
        return {"score": 1.0, "comment": "fine", "cost_usd_micros": cost}

    now = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)   # a Tuesday
    outcomes: list = []
    for i in range(400):
        run_id = f"krun_{key}_{i:04d}"
        _run_span(engine, key, run_id, now)
        outcomes += oe.evaluate_run(engine, PROJECT, run_id, agent_key=key, now=now,
                                    runners={"judge": judge})
    quality = [o for o in outcomes if o["evaluator"] == "quality"]
    sampled = [o for o in quality if o["outcome"] != "not_sampled"]
    assert 25 <= len(sampled) <= 55, f"~10% of 400 sampled, got {len(sampled)}"
    evaluated = [o for o in quality if o["outcome"] == "evaluated"]
    assert len(evaluated) == 5 and len(calls) == 5
    assert all(o["outcome"] == "paused" for o in sampled[5:]), "paused at the cap, not before"
    finished = [o for o in outcomes if o["evaluator"] == "finished"]
    assert len(finished) == 400 and all(o["outcome"] == "evaluated" and o["score"] == 1.0
                                        for o in finished), "rules are free and never pause"

    health = client.get("/v1/observability/agent-health",
                        params={"project_id": PROJECT}).json()
    shown = [w for w in health["evaluators"] if w["agent_key"] == key
             and w["evaluator"] == "quality"]
    with engine.connect() as c:
        row = dict(c.execute(text(
            "SELECT * FROM online_evaluator_weeks WHERE project_id = :p AND agent_key = :a "
            "AND evaluator = 'quality'"), {"p": PROJECT, "a": key}).mappings().one())
        spent = int(c.execute(text(
            "SELECT COALESCE(sum(cost_usd_micros), 0) FROM feedback WHERE agent_key = :a "
            "AND evaluator = 'quality'"), {"a": key}).scalar())
        sources = {r[0] for r in c.execute(text(
            "SELECT DISTINCT source FROM feedback WHERE agent_key = :a"), {"a": key})}
    assert row["spent_usd_micros"] == spent == 50_000 <= row["cap_usd_micros"] == 50_000
    assert row["paused_at"] is not None and row["evaluated"] == 5
    assert row["skipped_at_cap"] == len(sampled) - 5 and row["seen"] == 400
    assert sources == {"rule", "judge"}
    assert all(w["paused"] for w in shown)   # shown when the fixed clock is this real week

    # A new ISO week: the cap is the week's, so the judge runs again.
    later = now + timedelta(days=7)
    for i in range(400, 800):
        run_id = f"krun_{key}_{i:04d}"
        _run_span(engine, key, run_id, later)
        got = oe.evaluate_run(engine, PROJECT, run_id, agent_key=key, now=later,
                              runners={"judge": judge})
        if any(o["evaluator"] == "quality" and o["outcome"] == "evaluated" for o in got):
            break
    else:
        raise AssertionError("the judge never ran in the next week")


def test_a_finished_run_is_scored_by_its_agents_online_evaluators(client, engine):
    key = f"ev_{uuid4().hex[:6]}"
    _agent(client, key, [{"kind": "online", "name": "finished", "by": "rule",
                          "rule": "completed", "sample": 1.0}])
    result = kernel.run(_spec(engine, [Say("ok")], actor=f"agent:{key}"))
    assert result.status == "completed"
    with engine.connect() as c:
        rows = c.execute(text("SELECT key, source, score, spec_version FROM feedback "
                              "WHERE run_id = :r"), {"r": result.run_id}).mappings().all()
    assert [dict(r) for r in rows] == [{"key": "finished", "source": "rule", "score": 1,
                                        "spec_version": 1}]
    health = client.get("/v1/observability/agent-health", params={"project_id": PROJECT}).json()
    mine = next(a for a in health["agents"] if a["agent_key"] == key)
    assert mine["runs"] == 1 and mine["ok_rate"] == 1.0 and mine["spec_version"] == 1
    assert mine["cost_usd_micros"] == _ledger_total(engine, result.run_id)


# ── (d) ───────────────────────────────────────────────────────────────────────────────────────

def test_d_governed_vs_bare_for_a_real_engine_from_recorded_runs_with_noise_bands(client):
    engines = client.get("/v1/bench/governed-vs-bare", params={"project_id": PROJECT}).json()
    assert "claude_code" in engines["engines"]
    got = client.get("/v1/bench/governed-vs-bare",
                     params={"project_id": PROJECT, "engine_name": "claude_code"})
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["governed"]["level"] == "delegate" and body["bare"]["level"] == "bare"
    assert body["governed"]["engine"] == body["bare"]["engine"] == "claude_code"
    assert body["governed"]["source"] == body["bare"]["source"] == "recorded_file"
    assert body["bare"]["repeat"] == 3, "the bare baseline has spread (3 passes)"
    assert body["verdict"] in ("better", "same", "worse")
    for name in ("success_rate", "cost_usd", "wall_s", "tokens_in"):
        metric = body["metrics"][name]
        assert metric["noise_band"] >= 0 and metric["verdict"] in ("better", "same", "worse")
        assert metric["bare"]["n"] >= 1 and metric["governed"]["n"] >= 1
    assert client.get("/v1/bench/governed-vs-bare",
                      params={"project_id": PROJECT, "engine_name": "nope"}).status_code == 404


# ── payload tiers ─────────────────────────────────────────────────────────────────────────────

def test_payloads_go_hot_then_cold_then_expire_and_feedback_extends_them(engine, tmp_path):
    from boss_engine.services import trace_payloads as tp

    backend = tp.LocalFileBackend(tmp_path)
    run_id, created = f"krun_{uuid4().hex[:8]}", datetime.now(timezone.utc)
    with engine.begin() as c:
        pid = tp.store(c, project_id=PROJECT, run_id=run_id, span_id="s1", direction="input",
                       value={"q": f"use {TOKEN}"}, backend=backend, now=created)
        big = tp.store(c, project_id=PROJECT, run_id=run_id, span_id="s2", direction="output",
                       value={"text": "x" * (tp.MAX_HOT_BYTES + 10)}, backend=backend,
                       now=created)
        assert tp.load(c, pid, backend=backend)["tier"] == "hot"
        assert tp.load(c, big, backend=backend)["tier"] == "cold", "too big for the database"
    moved = tp.sweep(engine, backend=backend, now=created + timedelta(days=tp.HOT_DAYS + 1))
    assert moved["moved"] >= 1
    with engine.begin() as c:
        cold = tp.load(c, pid, backend=backend)
        assert cold["tier"] == "cold" and TOKEN not in json.dumps(cold["body"])
        assert "use " in cold["body"]["q"]
    tp.sweep(engine, backend=backend, now=created + timedelta(days=tp.BASE_DAYS + 1))
    with engine.begin() as c:
        gone = tp.load(c, pid, backend=backend)
        assert gone["tier"] == "expired" and gone["body"] is None and gone["note"]
        other = f"krun_{uuid4().hex[:8]}"
        kept = tp.store(c, project_id=PROJECT, run_id=other, span_id="s3", direction="input",
                        value={"q": 1}, now=created)
        assert tp.extend_retention(c, other) == 1
    tp.sweep(engine, backend=None, now=created + timedelta(days=tp.BASE_DAYS + 1))
    with engine.begin() as c:
        assert tp.load(c, kept)["tier"] == "hot", "extended: kept 180 days"
