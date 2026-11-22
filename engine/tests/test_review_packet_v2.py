"""boss.review_packet/v2 (SPEC docs/design/validation-review §1.4, §7 PR 2 (a) and (d)).

The packet is built by the engine from step outputs; ``validate_packet`` enforces the anti-slop
rules, one test each; an invalid packet refuses the merge (ci_gate) and never falls back to prose.
"""
from __future__ import annotations

import copy
import inspect
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from boss_engine.services import ci_gate, input_fidelity as f, review_packet as rp
from boss_engine.services import software_factory

FIX = Path(__file__).parent / "fixtures" / "validation"
INPUT, TARGET = "examples/lumen-pay.bad.yaml", "config/tenants/lumen-pay.yaml"
WORK = {"title": "Validate tenant config with gwlint", "workflow_run": "wfr_40229c5c963c4f599c51",
        "run_no": 16, "build": "crun_58aecc167e194942b0de", "later_builds": [], "change": 9}
TESTS = [{"ok": True, "command": "go vet ./... && go test ./...", "exit_code": 0}]
FOLLOWUPS = {"bar": "standard", "items": [
    {"text": "internal/lint/fixer.go:69 — The inline rate-limit clamp also rewrites the "
             "tenant-level rate_limit.", "tier": "P1", "severity": "major"},
    {"text": "internal/lint/fixer.go:216 — The flow-map matcher requires rps before burst.",
     "tier": "P2", "severity": "minor"}]}


def _validation(tmp_path: Path) -> dict:
    root = tmp_path / "ng"
    for d in ("examples", "config/tenants", ".boss"):
        (root / d).mkdir(parents=True)
    shutil.copy(FIX / "lumen-pay.bad.yaml", root / INPUT)
    shutil.copy(FIX / "validators.yaml", root / ".boss" / "validators.yaml")
    c = f.contract({"remediate": {"input": INPUT, "output": TARGET}}, "", root)
    (root / TARGET).write_text((FIX / "lumen-pay.fixed.yaml").read_text())

    def run(cmd):
        if "--fix" in cmd:
            (root / cmd.split()[-1]).write_text((FIX / "lumen-pay.fixed.yaml").read_text())
            return SimpleNamespace(stdout="", exit_code=1)
        bad = (root / cmd.split()[-2]).read_text() == (FIX / "lumen-pay.bad.yaml").read_text()
        out = FIX / ("gwlint.before.json" if bad else "gwlint.after.json")
        return SimpleNamespace(stdout=out.read_text(), exit_code=1)
    return f.public_validation(f.run_validation(root, c, run))


@pytest.fixture
def packet(tmp_path):
    return rp.build_packet(work=WORK, head_sha="eaff448", validations=[_validation(tmp_path)],
                           tests=TESTS, reviews=rp.from_followups(FOLLOWUPS["items"], "standard"))


def _rebuilt(p: dict) -> dict:
    p = copy.deepcopy(p)
    p.pop("verdict", None)
    return rp.finalize(p)


def test_a_packet_built_from_the_step_outputs_is_valid_and_computed(packet):
    assert rp.validate_packet(packet) == []
    assert packet["schema"] == "boss.review_packet/v2"
    assert set(rp.REQUIRED) <= set(packet)
    # No severity from a ruleset yet: gwlint's error → P1, warn → P2 (basis says so).
    assert packet["counts"]["P1"] == {"found": 9, "open": 5, "waived": 0}
    assert packet["counts"]["P2"] == {"found": 2, "open": 2, "waived": 0}
    assert packet["verdict"]["state"] == "blocked"
    assert "open_p1:R-1" in packet["verdict"]["reasons"]
    assert packet["validators"][0]["before"] == 9 and packet["validators"][0]["after_open"] == 5
    assert packet["vs_input"]["hunks"] == 3 and packet["vs_input"]["unexplained_hunks"] == []
    assert packet["tests"][0]["id"] == "T-1" and packet["tests"][0]["ok"] is True
    # Sorted: open P1 first, fixed last.
    assert [x["status"] for x in packet["findings"]][-4:] == ["fixed"] * 4
    assert packet["findings"][0]["severity"] == "P1" and packet["findings"][0]["status"] == "open"
    for claim in packet["claims"]:
        assert len(claim["text"]) <= rp.TEXT_MAX and set(claim["refs"]) <= rp.known_ids(packet)


def test_the_p1_review_row_has_its_location_and_basis(packet):
    r1 = next(x for x in packet["findings"] if x["id"] == "R-1")
    assert r1["severity"] == "P1" and r1["basis"] == "quality bar standard: major"
    assert r1["location"]["file"] == "internal/lint/fixer.go" and r1["location"]["line"] == 69
    assert r1["title"].startswith("The inline rate-limit clamp")


def test_a_reviewer_finding_without_a_basis_is_p2_unmapped():
    (r,) = rp.from_followups([{"text": "Something looks off", "tier": "P0"}], "standard")
    assert r["severity"] == "P2" and r["unmapped"] is True and r["basis"] is None
    p = rp.build_packet(work=WORK, head_sha="x", reviews=[r])
    assert p["counts"]["unmapped"] == 1 and p["verdict"]["state"] == "ready"


def test_a_follow_up_already_queued_is_listed_but_does_not_block():
    (r,) = rp.from_followups([FOLLOWUPS["items"][0]], "standard")
    r["deferred_to"] = "queued"
    p = rp.build_packet(work=WORK, head_sha="x", reviews=[r])
    assert p["counts"]["P1"]["open"] == 1 and p["verdict"]["state"] == "ready"


# ── rule 1: every field ────────────────────────────────────────────────────────────────────────

def test_rule1_a_missing_field_is_invalid(packet):
    p = copy.deepcopy(packet)
    del p["vs_input"]
    assert _rebuilt(p)["verdict"]["state"] == "invalid"


# ── rule 2: claims and risks ───────────────────────────────────────────────────────────────────

def test_rule2_an_unreferenced_claim_is_invalid(packet):
    p = copy.deepcopy(packet)
    p["claims"].append({"text": "All tenant limits are safe", "refs": []})
    out = _rebuilt(p)
    assert out["verdict"]["state"] == "invalid"
    assert any(r.startswith("rule2:claims[") and "no evidence ref" in r
               for r in out["verdict"]["reasons"])


def test_rule2_a_ref_to_an_unknown_id_is_invalid(packet):
    p = copy.deepcopy(packet)
    p["risks"].append({"text": "CORS credentials kept for dashboard origin", "refs": ["V-99"]})
    assert "rule2:risks[0]:unknown ref V-99" in _rebuilt(p)["verdict"]["reasons"]


def test_rule2_long_text_is_invalid(packet):
    p = copy.deepcopy(packet)
    p["risks"].append({"text": "x" * 121, "refs": ["V-01"]})
    assert _rebuilt(p)["verdict"]["state"] == "invalid"


# ── rule 3: counts ─────────────────────────────────────────────────────────────────────────────

def test_rule3_a_count_mismatch_is_invalid(packet):
    p = copy.deepcopy(packet)
    p["counts"]["P0"]["open"] = 0
    p["counts"]["P1"]["open"] = 0
    assert "rule3:counts:do not match the findings" in _rebuilt(p)["verdict"]["reasons"]


# ── rule 4: fixed needs evidence ───────────────────────────────────────────────────────────────

def test_rule4_fixed_without_after_evidence_is_invalid(packet):
    p = copy.deepcopy(packet)
    fixed = next(x for x in p["findings"] if x["status"] == "fixed")
    fixed["evidence"] = [e for e in fixed["evidence"] if not e.get("absent")]
    assert _rebuilt(p)["verdict"]["state"] == "invalid"


def test_rule4_fixed_without_a_hunk_is_invalid(packet):
    p = copy.deepcopy(packet)
    next(x for x in p["findings"] if x["status"] == "fixed")["hunk_id"] = None
    assert _rebuilt(p)["verdict"]["state"] == "invalid"


def test_rule4_a_test_finding_is_fixed_only_by_a_pass_at_the_head(packet):
    p = copy.deepcopy(packet)
    t = {"id": "T-9", "source": {"kind": "test"}, "severity": "P1", "status": "fixed",
         "evidence": [{"id": "E-T9", "kind": "test", "ok": True, "sha": "old"}]}
    p["findings"].append(t)
    p["counts"] = rp.count(p["findings"])
    assert _rebuilt(p)["verdict"]["state"] == "invalid"
    t["evidence"][0]["sha"] = p["head_sha"]
    p["counts"] = rp.count(p["findings"])
    assert _rebuilt(p)["verdict"]["state"] != "invalid"


# ── rule 5: no build prose ─────────────────────────────────────────────────────────────────────

def test_rule5_build_prose_is_invalid(packet):
    p = copy.deepcopy(packet)
    p["work"]["impl_summary"] = "The implementation already correctly fixes F-273 and F-274."
    assert "rule5:work.impl_summary:build prose" in _rebuilt(p)["verdict"]["reasons"]


# ── rule 6: invalid refuses the merge ──────────────────────────────────────────────────────────

def test_rule6_an_invalid_packet_refuses_the_merge_even_forced(packet):
    p = copy.deepcopy(packet)
    p["claims"].append({"text": "Done", "refs": []})
    p = _rebuilt(p)
    assert rp.blocks_merge({"packet": p}, force=True)[0] == "packet_invalid"


def test_a_blocked_packet_needs_a_forced_merge_and_a_ready_one_merges(packet):
    assert rp.blocks_merge({"packet": packet}, force=False)[0] == "packet_blocked"
    assert rp.blocks_merge({"packet": packet}, force=True) is None
    ready = rp.build_packet(work=WORK, head_sha="x", tests=TESTS)
    assert ready["verdict"] == {"state": "ready", "reasons": []}
    assert rp.blocks_merge({"packet": ready}, force=False) is None
    assert rp.blocks_merge({}, force=False) is None      # a card from before the packet


def test_a_rerun_in_progress_refuses_the_merge():
    p = rp.build_packet(work=WORK, head_sha="x", rerun={"id": "rr_1", "state": "pending"})
    assert p["verdict"]["reasons"] == ["rerun_pending"]
    assert rp.blocks_merge({"packet": p}, force=True)[0] == "rerun_pending"


def test_the_merge_gate_reads_the_packet():
    src = inspect.getsource(ci_gate.refuse_approval)
    assert "blocks_merge(rec, force=force and not system_policy)" in src


def test_a_substituted_input_blocks(tmp_path):
    rec = _validation(tmp_path)
    rec["guard"] = {"code": "input_substituted", "source": "examples/lumen-pay.good.yaml",
                    "why": "copy_of_repo_file"}
    p = rp.build_packet(work=WORK, head_sha="x", validations=[rec])
    assert "input_substituted:examples/lumen-pay.good.yaml" in p["verdict"]["reasons"]


# ── (d) the root build's title, on the card ────────────────────────────────────────────────────

def test_the_title_is_the_root_builds_without_follow_up_prefixes():
    assert rp.work_title("Follow-up: deferred review findings — Follow-up: More tests — "
                         "Validate tenant config with gwlint · retry 1") == \
        "Validate tenant config with gwlint"


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def mappings(self):
        return self

    def first(self):
        return self.rows[0] if self.rows else None

    def all(self):
        return self.rows

    def scalar(self):
        return 16


class _Conn:
    def __init__(self, lineage):
        self.lineage = lineage

    def begin_nested(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, stmt, params=None):
        q = str(stmt)
        if "count(*)" in q:
            return _Rows([])
        if "AS root" in q:
            return _Rows([{"id": params["r"], "root": "crun_root"}])
        return _Rows(self.lineage)


def test_for_run_uses_the_root_build_and_the_newest_validation(tmp_path):
    rec = _validation(tmp_path)
    lineage = [
        {"id": "crun_root", "title": "Validate tenant config with gwlint", "created_at": 1,
         "finalize_ctx": {"workflow_run_id": "wfr_1"}, "state": {"validation": rec}},
        {"id": "crun_more", "title": "Follow-up: More tests — Validate tenant config with gwlint",
         "created_at": 2, "finalize_ctx": {"root_run_id": "crun_root"}, "state": {}},
        {"id": "crun_last", "title": "Follow-up: deferred review findings — Follow-up: More "
                                      "tests — Validate tenant config with gwlint",
         "created_at": 3, "finalize_ctx": {"root_run_id": "crun_root"},
         "state": {"tests": TESTS, "impl_summary": "prose never shown"}},
    ]
    p = rp.for_run(_Conn(lineage), "crun_last", head_sha="eaff448", change=9,
                   followups=FOLLOWUPS)
    assert p["work"] == {"title": "Validate tenant config with gwlint", "workflow_run": "wfr_1",
                         "run_no": 16, "build": "crun_root",
                         "later_builds": ["crun_more", "crun_last"], "change": 9}
    assert p["validators"][0]["id"] == "gwlint" and p["tests"][0]["suite"].startswith("go vet")
    assert "prose never shown" not in str(p) and rp.validate_packet(p) == []


def test_the_merge_card_carries_the_packet_and_the_root_title():
    src = inspect.getsource(software_factory)
    assert "_review_packet.for_run(" in src
    assert "**({_review_packet.KEY: _packet} if _packet else {})" in src
    assert '"title": _work_title,' in src
    assert 'f"Approve code change: {_work_title} ({_ref})"' in src
