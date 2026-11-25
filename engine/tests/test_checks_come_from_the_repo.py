"""R4: checkers come from the repo, in any workflow (requirement R4, design PR #287).

A build in ANY workflow — a plain Fast Fix here — runs every validator `.boss/validators.yaml`
declares whose `applies_to` matches a file the change touched; the record is the same
`boss.validation/v1` the packet (Checks) reads. "Checks must pass" decides whether its open
P0/P1 findings block the merge; off, they are reported (the light lane). A repo that declares no
validator, or a change that touches no matching file, runs nothing.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace

from boss_engine.schemas import FileChange
from boss_engine.services import governed_code_task_service as g
from boss_engine.services import input_fidelity as f
from boss_engine.services import quality_bar, review_packet as rp
from boss_engine.services import validators as v
from boss_engine.services import workflow_catalogue as wc

FIX = Path(__file__).parent / "fixtures" / "validation"
TOUCHED = "config/tenants/x.yaml"
WORK = {"title": "Fix the tenant", "workflow_run": "wfr_x", "run_no": 1, "build": "crun_x",
        "later_builds": [], "change": 3}


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "ng"
    for d in ("config/tenants", ".boss", "src"):
        (root / d).mkdir(parents=True)
    shutil.copy(FIX / "validators.yaml", root / ".boss" / "validators.yaml")
    (root / TOUCHED).write_text((FIX / "lumen-pay.bad.yaml").read_text())
    (root / "src" / "app.py").write_text("print('hi')\n")
    return root


def _gwlint(ran: list):
    """gwlint, replayed from its real outputs: the partner's file reports its 9 findings."""
    def run(cmd: str, timeout: int = 0):
        ran.append(cmd)
        if "--fix" in cmd:
            return SimpleNamespace(stdout="", exit_code=0)
        return SimpleNamespace(stdout=(FIX / "gwlint.before.json").read_text(), exit_code=1)
    return run


class _Sandbox:
    def __init__(self, paths, ran):
        self.paths, self.run = paths, _gwlint(ran)

    def diff(self):
        return [FileChange(path=p, content="") for p in self.paths]


class _Engine:
    """Only the event write touches it; the checks never depend on it."""
    def begin(self):
        raise RuntimeError("no database in this test")


def _github(base: dict):
    return SimpleNamespace(read_text_file=lambda ref, path: base.get(path))


def _checks(root, touched, ran, base=None):
    return g._run_repo_checks(_Engine(), "crun_x", "proj", root, _Sandbox(touched, ran),
                              _github(base if base is not None else
                                      {".boss/validators.yaml":
                                       (FIX / "validators.yaml").read_text()}),
                              "main")


# ── which checkers run ─────────────────────────────────────────────────────────────────────────

def test_a_fast_fix_build_that_touches_a_tenant_file_runs_gwlint(tmp_path):
    ran: list = []
    records = _checks(_repo(tmp_path), [TOUCHED, "src/app.py"], ran)
    assert len(records) == 1
    rec = records[0]
    assert rec["schema"] == v.SCHEMA and rec["kind"] == "repo_check"
    assert rec["validator"]["id"] == "gwlint" and rec["target"] == TOUCHED
    assert any("gwlint validate config/tenants/x.yaml" in c for c in ran)
    assert len(rec["findings"]) >= 9 and f.open_blocking(rec) > 0
    # Fast Fix itself is unchanged: no "Checks must pass", so the light lane.
    fast_fix = next(e for e in wc.CATALOGUE if e["key"] == "fast_fix")
    assert "checks_must_pass" not in wc.entry_spec(fast_fix)


def test_a_build_that_touches_no_matching_file_runs_nothing(tmp_path):
    ran: list = []
    assert _checks(_repo(tmp_path), ["src/app.py"], ran) == []
    assert ran == []


def test_a_repo_that_declares_no_validator_runs_nothing(tmp_path):
    root = _repo(tmp_path)
    (root / ".boss" / "validators.yaml").unlink()
    ran: list = []
    assert _checks(root, [TOUCHED], ran, base={}) == []
    assert ran == []


def test_the_declaration_is_read_from_the_base_branch_not_the_change(tmp_path):
    """The change's own `.boss/validators.yaml` never picks its checker: the base's does."""
    root = _repo(tmp_path)
    (root / ".boss" / "validators.yaml").write_text("validators: []\n")
    ran: list = []
    assert len(_checks(root, [TOUCHED], ran)) == 1


def test_an_existing_file_is_checked_against_its_base_version(tmp_path):
    root = _repo(tmp_path)
    base = {".boss/validators.yaml": (FIX / "validators.yaml").read_text(),
            TOUCHED: (FIX / "lumen-pay.bad.yaml").read_text()}
    rec = _checks(root, [TOUCHED], [], base=base)[0]
    assert rec["input"]["from"] == "base" and rec["input"]["ref"] == "main"
    assert not rec.get("guard"), "a file changed in place is not a substituted input"


# ── strict or not: the packet ──────────────────────────────────────────────────────────────────

def _packet(rec):
    return rp.build_packet(work=WORK, head_sha="abc", validations=[rec], evidence_head="abc")


def test_checks_must_pass_off_reports_but_does_not_block(tmp_path):
    rec = _checks(_repo(tmp_path), [TOUCHED], [])[0]
    rec["blocking"] = False
    p = _packet(rec)
    assert p["verdict"]["state"] == "ready", p["verdict"]
    assert p["validators"][0]["advisory"] is True
    assert any(x.get("advisory") for x in p["findings"])
    assert p["vs_input"]["unexplained_hunks"] == []
    assert rp.blocks_merge({rp.KEY: p}, force=False, current_head="def") is None


def test_checks_must_pass_on_blocks_an_open_p0_p1(tmp_path):
    rec = _checks(_repo(tmp_path), [TOUCHED], [])[0]
    rec["blocking"] = True
    p = _packet(rec)
    assert p["verdict"]["state"] == "blocked"
    assert any(r.startswith(("open_p0", "open_p1")) for r in p["verdict"]["reasons"])
    refused = rp.blocks_merge({rp.KEY: p}, force=False, current_head="abc")
    assert refused and refused[0] == "packet_blocked"


def test_an_advisory_packet_still_blocks_on_a_failing_test(tmp_path):
    rec = _checks(_repo(tmp_path), [TOUCHED], [])[0]
    rec["blocking"] = False
    p = rp.build_packet(work=WORK, head_sha="abc", validations=[rec], evidence_head="abc",
                        tests=[{"ok": False, "command": "pytest", "exit_code": 1}])
    assert any(r.startswith("tests_failed") for r in p["verdict"]["reasons"])


def test_the_setting_is_read_from_the_workflow_spec():
    assert quality_bar.checks_must_pass(None, {"checks_must_pass": True}) is True
    assert quality_bar.checks_must_pass(None, {}) is False
    assert quality_bar.checks_must_pass({"checks_must_pass": False},
                                        {"checks_must_pass": True}) is False


def test_the_lead_record_is_the_one_with_most_open_p0_p1():
    a = {"findings": [{"status": "open", "severity": "P2"}]}
    b = {"findings": [{"status": "open", "severity": "P0"}]}
    assert f.lead_record([a, b]) == b and f.lead_record([]) is None


def test_a_remediation_record_still_blocks_as_before(tmp_path):
    """No `blocking` key (a remediation's record): its findings block, unchanged."""
    rec = _checks(_repo(tmp_path), [TOUCHED], [])[0]
    rec.pop("kind")
    assert _packet(rec)["verdict"]["state"] == "blocked"


# ── no cost without a declaration; a bounded cost with one ────────────────────────────────────

def test_a_repo_with_no_declaration_never_diffs_the_tree(tmp_path):
    class _NoDiff(_Sandbox):
        def diff(self):
            raise AssertionError("diffed a tree whose repo declares no checkers")

    ran: list = []
    out = g._run_repo_checks(_Engine(), "crun_x", "proj", _repo(tmp_path),
                             _NoDiff([TOUCHED], ran), _github({}), "main")
    assert out == [] and ran == []


def test_the_repo_checks_stop_at_their_time_budget(tmp_path):
    root = _repo(tmp_path)
    timeouts: list = []

    def run(cmd, timeout):
        timeouts.append(timeout)
        return SimpleNamespace(stdout=(FIX / "gwlint.before.json").read_text(), exit_code=1)

    text = (FIX / "validators.yaml").read_text()
    assert f.repo_checks(root, [TOUCHED], run, trusted=lambda p: text if p == v.FILE else None,
                         budget_s=0) == []
    assert timeouts == []
    recs = f.repo_checks(root, [TOUCHED], run, trusted=lambda p: text if p == v.FILE else None)
    assert recs and timeouts and max(timeouts) <= f.REPO_CHECK_COMMAND_S


def test_a_repo_contract_test_never_names_its_own_host():
    import pytest

    from boss_engine.services import contract_tests as ct

    with pytest.raises(ct.CaseError):
        ct.from_repo("cases:\n  - name: x\n    url: https://elsewhere.example/\n")
    assert ct.from_repo("cases:\n  - name: x\n    path: /healthz\n")["cases"][0]["path"] == "/healthz"


def test_a_start_from_path_outside_the_repo_is_never_read(tmp_path):
    root = _repo(tmp_path)
    (tmp_path / "secret.txt").write_text("host file\n")
    spec = {"remediate": {"input": TOUCHED, "output": TOUCHED, "start_from": True,
                          "original_path": "../secret.txt"}}
    assert f.contract(spec, "fix it", root) is None
