"""The agent battle-test scenarios (tests/agent-battle/scenarios.yaml) stay usable as an
evaluation set: every scenario says where it came from, what was asked, what good looks like
and what must never happen."""
from pathlib import Path

import yaml

SET = Path(__file__).resolve().parents[2] / "tests" / "agent-battle" / "scenarios.yaml"


def test_every_scenario_is_complete_and_unique():
    items = yaml.safe_load(SET.read_text())
    assert isinstance(items, list) and items
    ids = [i["id"] for i in items]
    assert len(ids) == len(set(ids))
    for i in items:
        for key in ("origin", "surface", "context", "input", "expected", "must_not"):
            assert i.get(key), (i.get("id"), key)
        assert isinstance(i["must_not"], list)


BATTLE = SET.parent / "battle.yaml"
SURFACES = {"talk_to_boss", "step_talk", "step_recovery", "inbox_reply"}


def test_every_recorded_misbehaviour_is_battle_tested_with_many_phrasings():
    """The runnable set (battle.yaml, scripts/agent-battle-test.sh) covers EVERY recorded
    scenario, and each runnable scenario is asked many ways (operator, 2026-10-18: "not
    everyone will ask the same question")."""
    recorded = {i["id"] for i in yaml.safe_load(SET.read_text())}
    battle = yaml.safe_load(BATTLE.read_text())
    ids = [b["id"] for b in battle]
    assert len(ids) == len(set(ids))
    covered = {c for b in battle for c in b.get("covers") or []}
    assert recorded <= covered, f"not battle-tested yet: {sorted(recorded - covered)}"
    for b in battle:
        assert b["surface"] in SURFACES, b["id"]
        assert b.get("expected") and isinstance(b.get("must_not"), list), b["id"]
        asked = b.get("variants") if b["surface"] == "step_recovery" else b.get("phrasings")
        assert len(asked or []) >= 8, (b["id"], "needs at least 8 phrasings/variants")
        for neg in b.get("negatives") or []:
            assert neg.get("expected") and (neg.get("input") or neg.get("variant")), b["id"]


def test_every_seeded_table_is_one_the_runner_seeds():
    """A seed the runner ignores tests nothing: boss_recommendations rows were not seeded."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "agent_battle", Path(__file__).resolve().parents[2] / "scripts" / "agent_battle.py")
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    for b in yaml.safe_load(BATTLE.read_text()):
        assert set(b.get("seed") or {}) <= set(runner.SEED_TABLES), (b["id"], b.get("seed"))
