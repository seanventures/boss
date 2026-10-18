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
