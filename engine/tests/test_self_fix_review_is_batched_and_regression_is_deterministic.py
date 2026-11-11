"""BOSSAPP-1002: a self-fix repair round no longer pays four sequential review sessions.

The audit (model-call-audit §7.5) measured the self-fix pipeline at $216.94/mo (12.6% of spend):
every repair round ran FOUR full engine sessions over one small diff — code, security,
regression and consistency — including a model-driven `regression` seat the governed pipeline
answers deterministically. Now:

- seats on the same lane (engine and model) share ONE batched session (4 sessions/round → 1
  when every lane is the same, 2 when the security seat is on another engine);
- the `regression` seat is a TEST RUN PLUS A COMPARISON (the recorded red_then_green,
  affected_suites and p0_tier runs of this round's diff), never a model opinion;
- a batched session that does not answer every seat falls back to the sequential path — a seat
  is never skipped — and each seat's verdict keeps its exact downstream shape.

Scripted conformance engine only: no model anywhere.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import pytest

from boss_engine.services import scripted_engine
from boss_engine.services import self_fix as sf
from boss_engine.services import self_fix_build as sfb

from ._self_fix_support import self_fix_on  # noqa: F401 - fixture used by name
from ._self_fix_support import harness  # noqa: F401
from .test_self_fix_conformance_scripted import _build
from .test_workflow_capability_runner import eng, project  # noqa: F401

#: Every stage a review session can run under (the batch, or one seat sequentially).
REVIEW_STAGES = {sfb.BATCH_STAGE} | {f"self_fix_{seat}" for seat in sfb.REVIEW_SEATS}

#: What every seat's verdict carries downstream (gates/events/console read these).
SEAT_SHAPE = {"ok", "verdict", "findings", "open", "engine", "model", "summary"}


def _review_calls(sfx_id: str) -> list[dict[str, Any]]:
    return [c for c in scripted_engine.run_state(sfx_id).calls if c["stage"] in REVIEW_STAGES]


# ── sessions per round: 4 → 1 ────────────────────────────────────────────────────────────────

def test_one_review_session_per_round_and_no_regression_session(harness):
    row = _build(harness, "good_fix")["row"]
    assert row["status"] == sf.AWAITING_APPROVAL, (row["refusal"], row["refusal_detail"])
    calls = _review_calls(row["id"])
    assert [c["stage"] for c in calls] == [sfb.BATCH_STAGE], \
        "one round = ONE review session (was 4: code, security, regression, consistency)"
    assert calls[0]["read_only"] and calls[0]["resumed"] is None
    # The batched prompt carries every model seat's own ask and the per-seat answer contract.
    prompt = calls[0]["full_prompt"]
    for seat in sfb.MODEL_REVIEW_SEATS:
        assert f"SEAT {seat}:" in prompt, seat
    assert "SEAT regression:" not in prompt, "no model holds the regression seat"
    assert "one block PER SEAT" in prompt and "VERDICT: PASS" in prompt
    seats = row["review"]["seats"]
    assert set(seats) == set(sfb.REVIEW_SEATS), "downstream shape: all four seats, as before"
    for seat, verdict in seats.items():
        assert SEAT_SHAPE <= set(verdict), (seat, sorted(verdict))
        assert verdict["ok"] and verdict["verdict"] == "PASS", seat
    assert row["review"]["batched_seats"] == list(sfb.MODEL_REVIEW_SEATS)
    assert "review_fallback" not in row["review"]


def test_a_repair_round_pays_one_review_session_too(harness):
    """review_blocker: the security seat holds a BLOCKER in round 1 (inside the batch), the
    repair round clears it. Two rounds = two review sessions, where they were eight."""
    row = _build(harness, "review_blocker")["row"]
    assert row["status"] == sf.AWAITING_APPROVAL and row["repair_rounds"] == 1
    assert [c["stage"] for c in _review_calls(row["id"])] == [sfb.BATCH_STAGE, sfb.BATCH_STAGE]
    security = row["review"]["seats"]["security_check"]
    assert security["ok"], "the repair round cleared the finding"


# ── the regression seat is a test run, not an opinion ───────────────────────────────────────

def test_the_regression_seat_is_the_recorded_test_runs(harness):
    row = _build(harness, "good_fix")["row"]
    regression = row["review"]["seats"]["regression"]
    assert regression["engine"] == "deterministic" and regression["model"] == "tests"
    assert regression["ok"] and regression["verdict"] == "PASS" and regression["open"] == []
    assert "test run, not opinion" in regression["summary"]
    assert row["review"]["lanes"]["regression"] == {"engine": "deterministic", "model": "tests"}
    # Its evidence is the gates' real runs of this round's diff.
    for name in ("red_then_green", "affected_suites", "p0_tier"):
        assert row["gates"][name]["ok"], name
    # Downstream rendering is unchanged: the lineage names all four seats.
    steps = sf.lineage_steps(row)
    review = next(s for s in steps if s["key"] == "review")
    for word in ("review code ✓", "security ✓", "regression ✓", "consistency ✓"):
        assert word in review["detail"], review["detail"]


def test_regression_verdict_comes_from_a_real_test_run_both_directions(tmp_path):
    """The deterministic seat on the pipeline's own runner (src.run_pytest via Build._compare):
    a head where everything that passed on the base still passes → PASS; a head that breaks a
    passing test → FINDINGS naming it. No model anywhere."""
    from boss_engine.config import get_settings

    module = ("def double(x):\n    return x + 2\n")
    test = ("from boss_engine.services import widget\n\n\n"
            "def test_double_of_two_is_four():\n"
            "    assert widget.double(2) == 4\n")

    def tree(name: str, body: str) -> Path:
        root = tmp_path / name
        for rel, content in {
            "engine/boss_engine/__init__.py": "",
            "engine/boss_engine/services/__init__.py": "",
            "engine/boss_engine/services/widget.py": body,
            "engine/tests/__init__.py": "",
            "engine/tests/test_widget_existing.py": test,
        }.items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        return root

    base = tree("base", module)
    good = tree("good", "def double(x):\n    return x * 2\n")       # 2*2 == 4: still passes
    broken = tree("broken", "def double(x):\n    return x + 3\n")   # 2+3 != 4: breaks it
    build = sfb.Build(engine=None, settings=get_settings(), sfx_id="sfx_t1002")
    tests = ["engine/tests/test_widget_existing.py"]
    quiet = {"red_then_green": {"ok": True, "detail": {}},
             "p0_tier": {"ok": True, "detail": {"tests": 0}}}

    passing = build._compare(base, good, tests, "affected_suites")  # noqa: SLF001
    assert passing.ok, passing.why
    verdict = sfb.regression_verdict({**quiet, "affected_suites": passing.as_dict()})
    assert verdict["ok"] and verdict["verdict"] == "PASS" and verdict["open"] == []
    assert verdict["engine"] == "deterministic" and verdict["model"] == "tests"

    breaking = build._compare(base, broken, tests, "affected_suites")  # noqa: SLF001
    assert not breaking.ok
    bad = sfb.regression_verdict({**quiet, "affected_suites": breaking.as_dict()})
    assert not bad["ok"] and bad["verdict"] == "FINDINGS"
    assert any("test_double_of_two_is_four" in f["what"]
               and "passed on BOSS as it is and fails with the fix" in f["what"]
               for f in bad["open"]), bad["open"]


def test_a_missing_recorded_run_fails_the_regression_seat_closed():
    verdict = sfb.regression_verdict({})
    assert not verdict["ok"] and verdict["verdict"] == "FINDINGS"
    assert len(verdict["open"]) == 3 and all(f["severity"] == "BLOCKER"
                                             for f in verdict["open"])
    assert all("run is missing" in f["what"] for f in verdict["open"])


# ── a batched session that fails falls back; no seat is skipped ──────────────────────────────

def test_a_garbled_batched_answer_falls_back_to_one_session_per_seat(harness):
    from sqlalchemy import text

    row = _build(harness, "review_batch_garbled")["row"]
    assert row["status"] == sf.AWAITING_APPROVAL, (row["refusal"], row["refusal_detail"])
    stages = [c["stage"] for c in _review_calls(row["id"])]
    assert stages == [sfb.BATCH_STAGE] + [f"self_fix_{s}" for s in sfb.MODEL_REVIEW_SEATS], \
        "the failed batch is followed by every seat, one by one — none skipped"
    assert row["review"]["review_fallback"] == "sequential"
    assert row["review"]["batched_seats"] == []
    seats = row["review"]["seats"]
    assert set(seats) == set(sfb.REVIEW_SEATS) and all(v["ok"] for v in seats.values())
    with harness.eng.begin() as c:
        said = [r[0] for r in c.execute(text(
            "SELECT data->>'sentence' FROM events WHERE event_type = 'self_fix.progress' "
            "AND data->>'self_fix_id' = :i"), {"i": row["id"]}).all()]
    assert any("running the seats one by one" in (s or "") for s in said), said


def test_an_engine_error_in_the_batched_session_falls_back_too(monkeypatch, tmp_path):
    """A batched session that raises (the engine failed) re-runs its seats sequentially instead
    of refusing or skipping; a cancel (the kill switch, the record gone) still stands."""
    from boss_engine.services import self_fix_source as src

    turns: list[str] = []

    def fake_turn(self, agent, stage, prompt, cwd, *, read_only, model=None,
                  resume_key: Optional[str] = None):
        turns.append(stage)
        if stage == sfb.BATCH_STAGE:
            raise sfb.Stop("engine", "the engine failed at the batch")
        return "no defects found.\nVERDICT: PASS"

    writes: dict[str, Any] = {}
    gates = {"red_then_green": {"ok": True, "detail": {}},
             "affected_suites": {"ok": True, "detail": {"tests": 1}},
             "p0_tier": {"ok": True, "detail": {"tests": 1}}}
    monkeypatch.setattr(sfb.Build, "turn", fake_turn)
    monkeypatch.setattr(sfb.Build, "boundary", lambda self, **k: None)
    monkeypatch.setattr(sfb.Build, "move", lambda self, *a, **k: {})
    monkeypatch.setattr(sfb.Build, "say", lambda self, s: None)
    monkeypatch.setattr(sfb.Build, "write", lambda self, **f: writes.update(f))
    monkeypatch.setattr(sfb.Build, "sfx",
                        lambda self: {"project_id": "p", "evidence": {}, "gates": gates})
    monkeypatch.setattr(sfb.Build, "_base_invariants", lambda self, *a: "")
    monkeypatch.setattr(src, "diff_text", lambda *a: "the diff")
    monkeypatch.setattr(src, "changes", lambda *a: [])
    same = object()
    build = sfb.Build(engine=None, settings=None, sfx_id="sfx_x",
                      engines=sfb.Engines(same, same, "engine_a", "engine_a"))
    open_findings = build.review(tmp_path, "base", {"location": {}})
    assert open_findings == []
    assert turns == [sfb.BATCH_STAGE] + [f"self_fix_{s}" for s in sfb.MODEL_REVIEW_SEATS]
    seats = writes["review"]["seats"]
    assert set(seats) == set(sfb.REVIEW_SEATS) and all(v["ok"] for v in seats.values())
    assert writes["review"]["review_fallback"] == "sequential"

    # A cancel is never swallowed into a fallback.
    def cancel_turn(self, agent, stage, prompt, cwd, *, read_only, model=None,
                    resume_key: Optional[str] = None):
        raise sfb.Stop("policy", "the self-fix is gone", cancel=True)

    monkeypatch.setattr(sfb.Build, "turn", cancel_turn)
    with pytest.raises(sfb.Stop) as stop:
        build.review(tmp_path, "base", {"location": {}})
    assert stop.value.cancel


# ── the batch holds only co-located seats ────────────────────────────────────────────────────

def test_a_security_seat_on_another_engine_keeps_its_own_session(monkeypatch, tmp_path):
    """Two lanes → two sessions: one batch for the builder-lane seats, one single-seat session
    for the security seat on the other engine (before: four)."""
    from boss_engine.services import self_fix_source as src

    turns: list[tuple[str, str]] = []

    def fake_turn(self, agent, stage, prompt, cwd, *, read_only, model=None,
                  resume_key: Optional[str] = None):
        turns.append((getattr(agent, "name", "?"), stage))
        if stage == sfb.BATCH_STAGE:
            return ("SEAT: review_code\nVERDICT: PASS\n\n"
                    "SEAT: consistency_check\nVERDICT: PASS")
        return "VERDICT: PASS"

    writes: dict[str, Any] = {}
    monkeypatch.setattr(sfb.Build, "turn", fake_turn)
    monkeypatch.setattr(sfb.Build, "boundary", lambda self, **k: None)
    monkeypatch.setattr(sfb.Build, "move", lambda self, *a, **k: {})
    monkeypatch.setattr(sfb.Build, "say", lambda self, s: None)
    monkeypatch.setattr(sfb.Build, "write", lambda self, **f: writes.update(f))
    monkeypatch.setattr(sfb.Build, "sfx", lambda self: {"project_id": "p", "evidence": {},
                                                        "gates": {}})
    monkeypatch.setattr(sfb.Build, "_base_invariants", lambda self, *a: "")
    monkeypatch.setattr(src, "diff_text", lambda *a: "the diff")
    monkeypatch.setattr(src, "changes", lambda *a: [])

    class Agent:
        def __init__(self, name: str) -> None:
            self.name = name

    build = sfb.Build(engine=None, settings=None, sfx_id="sfx_y",
                      engines=sfb.Engines(Agent("builder"), Agent("reviewer"),
                                          "engine_a", "engine_b"))
    build.review(tmp_path, "base", {"location": {}})
    assert turns == [("builder", sfb.BATCH_STAGE), ("reviewer", "self_fix_security_check")]
    seats = writes["review"]["seats"]
    assert list(seats) == list(sfb.REVIEW_SEATS), "seat order downstream is unchanged"
    assert seats["security_check"]["engine"] == "engine_b"
    assert seats["review_code"]["engine"] == "engine_a"
    assert writes["review"]["batched_seats"] == ["review_code", "consistency_check"]


# ── the parser holds each seat to its own verdict contract ───────────────────────────────────

def test_parse_batched_review_demands_every_seat():
    seats = ["review_code", "consistency_check"]
    good = ("SEAT: review_code\nFINDING: MINOR a.py:1 — nit\nVERDICT: PASS\n"
            "SEAT: consistency_check\nVERDICT: PASS\n")
    parsed = sfb.parse_batched_review(good, seats)
    assert parsed and parsed["review_code"]["ok"] and parsed["consistency_check"]["ok"]
    assert parsed["review_code"]["findings"] == [{"severity": "MINOR", "what": "a.py:1 — nit"}]
    blocker = good.replace("SEAT: consistency_check\nVERDICT: PASS",
                           "SEAT: consistency_check\nFINDING: BLOCKER b.py:2 — the diagnosis "
                           "and the test disagree\nVERDICT: FINDINGS")
    parsed = sfb.parse_batched_review(blocker, seats)
    assert parsed and not parsed["consistency_check"]["ok"]
    assert parsed["consistency_check"]["open"][0]["severity"] == "BLOCKER"
    # A missing seat, a verdictless seat, or prose with no blocks → None (the caller falls back).
    assert sfb.parse_batched_review("SEAT: review_code\nVERDICT: PASS\n", seats) is None
    assert sfb.parse_batched_review(good.replace("SEAT: consistency_check\nVERDICT: PASS\n",
                                                 "SEAT: consistency_check\nlooks fine\n"),
                                    seats) is None
    assert sfb.parse_batched_review("All seats pass, great diff.", seats) is None
    assert sfb.parse_batched_review("", seats) is None
