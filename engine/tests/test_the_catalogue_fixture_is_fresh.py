"""The console's workflow fixtures are the catalogue, not a copy of it someone made once.

The workflow editor's fidelity tests load every standard from
apps/web/tests/fixtures/catalogue-specs.json and require a save to give it back unchanged. A
fixture that drifted from the catalogue would keep those tests green against workflows nobody
ships — so it is generated (scripts/dump-catalogue-specs.py) and this refuses a stale one.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _script():
    spec = importlib.util.spec_from_file_location(
        "dump_catalogue_specs", ROOT / "scripts/dump-catalogue-specs.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_fixture_file_is_what_the_catalogue_publishes_today():
    script = _script()
    on_disk = script.FIXTURE.read_text(encoding="utf-8")
    assert on_disk == script.rendered(), (
        "apps/web/tests/fixtures/catalogue-specs.json is stale — run "
        "python3 scripts/dump-catalogue-specs.py and commit the result")


def test_it_holds_every_standard_and_a_branched_graph():
    from boss_engine.services.workflow_catalogue import CATALOGUE

    data = json.loads(_script().FIXTURE.read_text(encoding="utf-8"))
    assert [w["workflow_key"] for w in data["catalogue"]] == [e["key"] for e in CATALOGUE]
    kinds = {s["kind"] for s in data["branched"]["spec"]["steps"]}
    assert {"parallel", "join", "condition", "decision", "timer"} <= kinds
    assert data["branched"]["spec"]["x_team_note"], "an unknown key must be in the fixture"


def test_each_spec_is_in_the_form_the_editor_loads():
    """Published form: normalising it again changes nothing, so a no-op save is a no-op."""
    from boss_engine.services import workflows

    data = json.loads(_script().FIXTURE.read_text(encoding="utf-8"))
    for entry in data["catalogue"] + [data["branched"]]:
        again = workflows.validate_workflow_spec(json.loads(json.dumps(entry["spec"])))
        assert again == entry["spec"], entry["workflow_key"]
