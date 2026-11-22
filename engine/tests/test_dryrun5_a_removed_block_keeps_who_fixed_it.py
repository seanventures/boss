"""Dry run 5 (Northgate change #23): Checks read "4 tool · 3 agent · 2 you" until the person
removed the admin route (O-1 on V-07); the re-run then saw V-08 (the tool's size cap inside that
route) as ``removed_with_block`` and counted it "you": "3 · 3 · 3". A finding's fix attribution
never changes because a later override removed its surrounding block."""
from __future__ import annotations

import copy
import json
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

from boss_engine.services import finding_overrides as fo

REC = json.loads((Path(__file__).parent / "fixtures" / "dryrun5_change23_validation.json").read_text())


def _owners(rec):
    return Counter({"auto": "tool", "agent": "agent", "human": "you"}[f["fix_kind"]]
                   for f in rec["findings"])


def _build_record():
    """The build's record before the person acted: the tool had capped the admin route's size."""
    base = copy.deepcopy(REC)
    v8 = next(f for f in base["findings"] if f["id"] == "V-08")
    v8.update(fix_kind="auto", fixed_by="line", fixed="    max_body_bytes: 1048576")
    return base


def test_the_packet_as_recorded_moved_v08_to_you():
    assert _owners(REC) == {"tool": 3, "agent": 3, "you": 3}


def test_a_removed_block_keeps_the_tool_and_the_counts():
    got = fo.keep_attribution(copy.deepcopy(REC), _build_record())
    assert _owners(got) == {"tool": 4, "agent": 3, "you": 2}
    v8 = next(f for f in got["findings"] if f["id"] == "V-08")
    assert v8["fix_kind"] == "auto" and v8["status"] == "fixed"
    # V-07 (the block the person removed) stays the person's.
    assert next(f for f in got["findings"] if f["id"] == "V-07")["fix_kind"] == "human"


def test_a_block_the_build_itself_removed_is_left_alone():
    base = copy.deepcopy(REC)      # the build's own record already said removed_with_block
    assert _owners(fo.keep_attribution(copy.deepcopy(REC), base)) == {"tool": 3, "agent": 3, "you": 3}
    assert _owners(fo.keep_attribution(copy.deepcopy(REC), None))["you"] == 3


def test_validation_of_applies_it_to_the_rerun(monkeypatch):
    base = _build_record()
    monkeypatch.setattr(fo, "original_of", lambda c, ch: base)
    monkeypatch.setattr(fo, "_lineage_records", lambda c, ch: [base])
    monkeypatch.setattr(fo.rp, "same_input", lambda a, b: True)

    class _C:
        def execute(self, *a, **k):
            return SimpleNamespace(fetchall=lambda: [(json.dumps(REC), REC["after"]["head_sha"])])

    got = fo.validation_of(_C(), SimpleNamespace(project_id="p", number=23))
    assert _owners(got) == {"tool": 4, "agent": 3, "you": 2}
