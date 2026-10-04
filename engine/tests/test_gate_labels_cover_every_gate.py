"""Every gate the pipeline can write has one label and an owner station (factory spec §5.1).

The web never maps a gate name: `gate_label` is the one map, and Outputs › Checks, the
checks-failed card and the waiver sheet all read it. A new gate written without a label would
reach the operator as `code_review_passed`. This scans the gate-name literals the governed
pipeline writes, so a new one fails here until it is named.
"""
from __future__ import annotations

import re
from pathlib import Path

from boss_engine.services import factory_view as fv

ROOT = Path(__file__).resolve().parents[2]
PIPELINE = ROOT / "engine/boss_engine/services/governed_code_task_service.py"


def written_gates() -> set[str]:
    source = PIPELINE.read_text()
    names = set(re.findall(r'gates\["([a-z_]+)"\]\s*=', source))
    # The gate dict the pipeline builds: `        gates = {` … `        }`, its keys at 12 spaces.
    block = re.search(r"\n {8}gates = \{\n(.*?)\n {8}\}\n", source, re.S)
    if block:
        names |= set(re.findall(r'(?m)^ {12}"([a-z_]+)":', block.group(1)))
    return names


def test_the_scan_sees_the_pipelines_gates():
    found = written_gates()
    assert {"code_written", "written_tests_pass", "code_review_passed", "security_clean",
            "app_serves", "consistent"} <= found


def test_every_written_gate_has_a_label_and_a_station():
    for name in sorted(written_gates()):
        if name.startswith("iac_"):
            assert fv.gate_label(name).startswith("Infra: ")
            continue
        assert name in fv.GATE_LABELS, f"{name} has no label in factory_view.GATE_LABELS"
        assert fv.gate_station(name) in fv.CORE_STATIONS


def test_labels_are_short_and_human():
    for name, (label, station) in fv.GATE_LABELS.items():
        assert "_" not in label
        assert len(label.split()) <= 3, label
        assert station in fv.CORE_STATIONS


def test_the_app_must_start_cannot_be_waived():
    assert "app_serves" in fv.UNWAIVABLE_GATES
    assert fv.gate_label("app_serves") == "App starts"
