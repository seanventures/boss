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
    # The P-level counts are the checks' (#10): the review's follow-ups are counted apart.
    assert packet["counts"]["P1"] == {"found": 8, "open": 4, "waived": 0}
    assert packet["counts"]["P2"] == {"found": 1, "open": 1, "waived": 0}
    assert packet["counts"]["review"] == {"found": 2, "open": 2, "deferred": 0, "blocking": 0}
    assert packet["verdict"]["state"] == "blocked"
    # Only the validator's open findings block; the review's P1 is a follow-up (light lane).
    assert "open_p1:R-1" not in packet["verdict"]["reasons"]
    assert all(r.startswith("open_p1:V-") for r in packet["verdict"]["reasons"])
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
    assert p["counts"]["P1"]["open"] == 0 and p["counts"]["review"]["open"] == 1
    assert p["counts"]["review"]["deferred"] == 1 and p["verdict"]["state"] == "ready"


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
    assert "blocks_merge(rec, force=force and not system_policy," in src
    assert "current_head=head_for_url(connection, rec.get(\"pull_request_url\"))" in src


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
    def __init__(self, lineage, wfrs=None):
        self.lineage = lineage
        self.wfrs = wfrs or {}

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
        if "FROM workflow_runs WHERE id" in q:
            w = self.wfrs.get(params["w"])
            return _Rows([w] if w else [])
        return _Rows(self.lineage)


def test_for_run_uses_the_producing_build_the_works_title_and_run(tmp_path):
    """#10, #12: the packet's build is the one that produced the change (not the failed first
    attempt); its title is the work's root title and its run is the work's own workflow run,
    numbered as the Work panel numbers it."""
    rec = _validation(tmp_path)
    lineage = [
        {"id": "crun_root", "title": "Validate tenant config with gwlint", "created_at": 1,
         "status": "failed", "pull_request_url": None,
         "finalize_ctx": {"workflow_run_id": "wfr_step"}, "state": {}},
        {"id": "crun_retry", "title": "Validate tenant config with gwlint · retry 1",
         "created_at": 2, "status": "completed", "pull_request_url": "http://boss-local/a/b/pull/9",
         "finalize_ctx": {"root_run_id": "crun_root"}, "state": {"validation": rec}},
        {"id": "crun_last", "title": "Follow-up: deferred review findings — Validate tenant "
                                      "config with gwlint · retry 1",
         "created_at": 3, "status": "completed", "pull_request_url": "http://boss-local/a/b/pull/9",
         "finalize_ctx": {"root_run_id": "crun_root"},
         "state": {"tests": TESTS, "impl_summary": "prose never shown"}},
    ]
    wfrs = {"wfr_step": {"id": "wfr_step", "parent_run_id": "wfr_work", "title": None},
            "wfr_work": {"id": "wfr_work", "parent_run_id": None,
                         "title": "Lumen Pay tenant onboarding"}}
    p = rp.for_run(_Conn(lineage, wfrs), "crun_last", head_sha="eaff448", change=9,
                   followups=FOLLOWUPS)
    assert p["work"] == {"title": "Lumen Pay tenant onboarding", "workflow_run": "wfr_work",
                         "run_no": 16, "build": "crun_retry", "later_builds": ["crun_last"],
                         "change": 9}
    assert " · retry" not in str(p["work"])
    assert p["validators"][0]["id"] == "gwlint" and p["tests"][0]["suite"].startswith("go vet")
    assert "prose never shown" not in str(p) and rp.validate_packet(p) == []


def test_the_run_number_counts_every_run_of_the_project_like_the_work_panel():
    src = inspect.getsource(rp._run_no)
    assert "workflow_definition_id" not in src and "w.project_id = me.project_id" in src


def test_without_a_titled_workflow_run_the_title_is_the_builds_without_retry():
    lineage = [{"id": "crun_root", "title": "Recipe box · retry 2", "created_at": 1,
                "status": "completed", "pull_request_url": "u", "finalize_ctx": {},
                "state": {"tests": TESTS}}]
    p = rp.for_run(_Conn(lineage), "crun_root", head_sha="abc", change=3)
    assert p["work"]["title"] == "Recipe box" and p["work"]["build"] == "crun_root"


def test_the_merge_card_carries_the_packet_and_the_root_title():
    src = inspect.getsource(software_factory)
    assert "_review_packet.for_run(" in src
    assert "**({_review_packet.KEY: _packet} if _packet else {})" in src
    assert '"title": _work_title,' in src
    assert 'f"Approve code change: {_work_title} ({_ref})"' in src


def test_a_non_prod_build_with_a_p1_follow_up_listed_for_later_merges_as_before():
    """Review fix #277: a P1 follow-up the bar let through ("listed for later", handled_by None)
    is deferred — the packet is ready and the merge (policy, delegate or person) is not held."""
    lineage = [{"id": "crun_root", "title": "Recipe box", "created_at": 1,
                "finalize_ctx": {}, "state": {"tests": TESTS}}]
    fu = {**FOLLOWUPS, "queued": False, "handled_by": None}
    p = rp.for_run(_Conn(lineage), "crun_root", head_sha="abc", change=3, followups=fu)
    r1 = next(x for x in p["findings"] if x["id"] == "R-1")
    assert r1["severity"] == "P1" and r1["status"] == "open"
    assert r1["deferred_to"] == {"by": "later", "number": None, "href": None}
    assert p["verdict"] == {"state": "ready", "reasons": []}
    assert rp.blocks_merge({"packet": p}, force=False) is None


def test_a_queued_follow_up_says_which_change_it_lands_on():
    lineage = [{"id": "crun_root", "title": "Recipe box", "created_at": 1,
                "finalize_ctx": {}, "state": {"tests": TESTS}}]
    fu = {**FOLLOWUPS, "queued": True, "handled_by": "queued"}
    p = rp.for_run(_Conn(lineage), "crun_root", head_sha="abc", change=9, followups=fu,
                   project_id="northgate-gateway")
    r1 = next(x for x in p["findings"] if x["id"] == "R-1")
    assert r1["deferred_to"] == {"by": "queued", "number": 9,
                                 "href": "/changes/northgate-gateway/9"}
