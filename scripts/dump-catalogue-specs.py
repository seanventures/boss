#!/usr/bin/env python3
"""Write the console's workflow fixtures from the catalogue itself.

    python3 scripts/dump-catalogue-specs.py           # rewrite the fixture file
    python3 scripts/dump-catalogue-specs.py --check   # exit 1 if it is stale

The workflow editor's spec-fidelity tests (apps/web/tests) load every standard, edit nothing or
one property, save, and require the spec to come back exactly as it went in. Hand-copied specs
would drift from the catalogue the day a step changed, and the tests would go on passing against
a workflow nobody ships. So the fixture is generated from `workflow_catalogue` and a test
(engine/tests/test_the_catalogue_fixture_is_fresh.py) refuses a stale one.

Each spec is in its PUBLISHED form — what the editor actually loads from
`GET /v1/delivery-workflows/{key}/spec`: normalised by `validate_workflow_spec` and stamped with
the agent rules, exactly as `publish_version` stores it. One more workflow, `branched`, is not
in the catalogue: every standard is a straight line, and the editor must also keep a parallel /
join / if-else graph and keys it does not know about intact.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "apps/web/tests/fixtures/catalogue-specs.json"

#: A graph with every edge kind the lane draws and a key the editor does not know. Branch
#: structure is edited in JSON this release, so this is what "never flattens branches" is
#: tested against.
BRANCHED_SPEC: dict[str, Any] = {
    "entry_step": "research",
    "x_team_note": "Unknown keys survive a save.",
    "input_schema": {
        "type": "object",
        "required": ["directive"],
        "properties": {"directive": {"type": "string", "title": "What to build"}},
    },
    "steps": [
        {"id": "research", "kind": "capability", "title": "Look into it",
         "binding": {"capability": "research",
                     "inputs": {"seed": {"from_input": "directive"}}},
         "next": ["reviews"]},
        {"id": "reviews", "kind": "parallel", "branches": ["ux", "security"],
         "join": "reviewed", "next": []},
        {"id": "ux", "kind": "capability", "binding": {"capability": "ux_review", "inputs": {}},
         "next": ["reviewed"]},
        {"id": "security", "kind": "capability",
         "binding": {"capability": "security_review", "inputs": {}}, "next": ["reviewed"]},
        {"id": "reviewed", "kind": "join", "wait_for": ["ux", "security"],
         "success_policy": "all", "next": ["worth_it"]},
        {"id": "worth_it", "kind": "condition",
         "expression": {"path": "steps.research.output.dossier", "op": "exists"},
         "on_true": "go", "on_false": "stop", "next": []},
        {"id": "go", "kind": "decision", "captain": "human",
         "question": "Build it?", "guidance": "Read the dossier first.", "next": []},
        {"id": "stop", "kind": "timer", "wait_seconds": 60, "next": []},
    ],
}


def published(spec: dict[str, Any]) -> dict[str, Any]:
    """`spec` as `publish_version` stores it: normalised, and stamped with the agent rules."""
    from boss_engine.services import workflows

    normalized = workflows.validate_workflow_spec(json.loads(json.dumps(spec)))
    normalized[workflows.AGENT_RULES_KEY] = workflows.CURRENT_AGENT_RULES
    return normalized


def fixture() -> dict[str, Any]:
    from boss_engine.services.workflow_catalogue import CATALOGUE, entry_spec

    return {
        "generated_by": "scripts/dump-catalogue-specs.py",
        "catalogue": [
            {"workflow_key": entry["key"], "name": entry["name"],
             "description": entry["description"], "spec": published(entry_spec(entry))}
            for entry in CATALOGUE
        ],
        "branched": {"workflow_key": "branched_review", "name": "Branched review",
                     "description": "Parallel reviews, a join and an if / else.",
                     "spec": published(BRANCHED_SPEC)},
    }


def rendered() -> str:
    return json.dumps(fixture(), indent=2, ensure_ascii=False) + "\n"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true",
                        help="exit 1 when the fixture differs from the catalogue")
    args = parser.parse_args(argv)
    text = rendered()
    if args.check:
        current = FIXTURE.read_text(encoding="utf-8") if FIXTURE.exists() else ""
        if current != text:
            print(f"{FIXTURE.relative_to(ROOT)} is stale: run scripts/dump-catalogue-specs.py",
                  file=sys.stderr)
            return 1
        return 0
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_text(text, encoding="utf-8")
    print(f"wrote {FIXTURE.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.path[:0] = [str(ROOT), str(ROOT / "engine")]
    raise SystemExit(main(sys.argv[1:]))
