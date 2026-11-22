"""Dry run 4 (Northgate change #21): a `human` / `never` rule's lines never leave the branch.

The mechanical fixer quoted the admin route's path (`- path: "/v1/admin/accounts"`), so the
agent's start no longer held the partner's line byte for byte: the route's block was not found,
the agent's deletion of it was not refused, and the build's final commit removed a route only a
person may remove while the record said "Needs you · unchanged". Then "Remove lines" found no
line (409 no_line).

Every build commit (commit_change: stage commits, checkpoints, the partial or final PR, follow-
ups and repairs) now passes the build's autofix guard, and the block is found through the
fixer's rewording.
"""
from __future__ import annotations

import re
from types import SimpleNamespace

from boss_engine.services import finding_overrides as fo
from boss_engine.services import governed_code_task_service as g
from boss_engine.services import input_fidelity as f
from boss_engine.services import local_repo
from boss_engine.services import software_factory as sf

from .test_validation_tells_the_truth import FIXED, TARGET, _after_report, _gwlint, _repo, SPEC, BAD

ROUTE = "  - path: /v1/admin/accounts"
QUOTED = FIXED.replace(ROUTE, '  - path: "/v1/admin/accounts"')
BRANCH = "boss/code/crun_dr4"


def _runner(root):
    """gwlint as the real one behaves: its fixer quotes the route paths, and its checks read a
    quoted path as the same path."""
    base = _gwlint(root, [])

    def run(cmd: str):
        if "--fix" in cmd:
            (root / cmd.split()[-1].strip("'")).write_text(QUOTED)
            return SimpleNamespace(stdout="", exit_code=1)
        body = (root / cmd.split()[-2].strip("'")).read_text()
        if body == BAD:
            return base(cmd)
        out, code = _after_report(re.sub(r'- path: "([^"]+)"', r"- path: \1", body))
        return SimpleNamespace(stdout=out, exit_code=code)
    return run


class _Host:
    """The local repo host's commit surface (local_repo_host.LocalGitAdapter) on a bare repo."""

    def __init__(self, git_dir):
        self.git_dir = git_dir

    def commit_files(self, branch, files, message):
        return local_repo.commit_files(self.git_dir, branch, files, message)

    def read_text_file(self, branch, path):
        return local_repo.read_file(self.git_dir, branch, path)


def _build(tmp_path):
    root = _repo(tmp_path)
    c = f.contract(SPEC, "", root)
    run = _runner(root)
    f.seed_auto_fix(root, c, run)
    c["_start_text"] = (root / TARGET).read_text()
    assert '- path: "/v1/admin/accounts"' in c["_start_text"]
    git_dir = local_repo.init_bare(tmp_path / "repo.git")
    local_repo.commit_files(git_dir, BRANCH, {TARGET: c["_start_text"]}, "start")
    return root, c, run, _Host(git_dir)


def _agent_deletes_the_route(text: str) -> str:
    i, j = text.index('  - path: "/v1/admin/accounts"'), text.index("upstreams:")
    return (text[:i] + text[j:]).replace("allow_credentials: true", "allow_credentials: false")


def _commit_through_the_build(monkeypatch, root, c, run, host, content):
    """commit_change, inside a build's guard scope, with the governed build's guard."""
    stored: dict = {}

    class _Tx:
        def __enter__(self):
            return SimpleNamespace(execute=lambda _q, p: stored.update(p))

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(g, "_transaction", lambda _e: _Tx())
    monkeypatch.setattr(g, "emit_event", lambda *a, **k: None)
    box = SimpleNamespace(run=lambda cmd, timeout=None: run(cmd))
    with sf.commit_guard_scope() as holder:
        holder["guard"] = g._autofix_commit_guard(None, "crun_dr4", "p", root, box, BRANCH, c)
        sf.commit_change(host, BRANCH, [(TARGET, content)], "governed: checkpoint after implement")
    return stored


def _v07(rec):
    return next(x for x in rec["findings"]
                if x["source"]["rule_id"] == "no-sensitive-paths-exposed")


def test_the_check_finds_the_block_through_the_fixers_quoting(tmp_path):
    root, c, run, _host = _build(tmp_path)
    (root / TARGET).write_text(_agent_deletes_the_route(c["_start_text"]))
    rec = f.enforce_autofix(root, c, f.run_validation(root, c, run), run)
    assert "/v1/admin/accounts" in (root / TARGET).read_text()
    v07 = _v07(rec)
    assert v07["status"] == "open" and v07["fix_kind"] == "human" and v07["proposed"]
    assert "allow_credentials: false" in (root / TARGET).read_text()   # the agent's fix stands


def test_a_build_commit_never_removes_a_person_only_route(tmp_path, monkeypatch):
    root, c, run, host = _build(tmp_path)
    agent = _agent_deletes_the_route(c["_start_text"])
    (root / TARGET).write_text(agent)
    # No check ran yet (a build that exits mid-way): the commit itself puts the block back.
    stored = _commit_through_the_build(monkeypatch, root, c, run, host, agent)
    on_branch = local_repo.read_file(host.git_dir, BRANCH, TARGET)
    assert '- path: "/v1/admin/accounts"' in on_branch
    assert "allow_credentials: false" in on_branch
    assert (root / TARGET).read_text() == on_branch          # the tree is what the branch holds
    # The record is the branch's: the finding is "Needs you" with the agent's change proposed.
    import json
    rec = json.loads(stored["v"])
    v07 = _v07(rec)
    assert v07["status"] == "open" and v07["fix_kind"] == "human" and v07["proposed"]
    # The person's "Remove lines" finds the block on the branch and removes it.
    removed = fo.remove_block(on_branch, v07)
    assert "/v1/admin/accounts" not in removed and "/v1/payments" in removed


def test_a_commit_outside_the_target_or_the_build_branch_passes_untouched(tmp_path, monkeypatch):
    root, c, run, host = _build(tmp_path)
    agent = _agent_deletes_the_route(c["_start_text"])
    guard = g._autofix_commit_guard(None, "crun_dr4", "p", root, None, BRANCH, c)
    assert guard("other/branch", TARGET, agent) == agent
    assert guard(BRANCH, "README.md", agent) == agent
    # Outside a build's scope commit_change applies no guard (a person's own commit).
    sf.commit_change(host, BRANCH, [(TARGET, agent)], "BOSS override: remove V-07")
    assert "/v1/admin/accounts" not in local_repo.read_file(host.git_dir, BRANCH, TARGET)


def test_a_finding_that_goes_with_a_person_only_block_is_the_persons(tmp_path):
    """#4: V-08 (a size cap on the admin route) is never "fixed by agent" because the route went."""
    root, c, run, _host = _build(tmp_path)
    (root / TARGET).write_text(_agent_deletes_the_route(c["_start_text"]))
    rec = f.run_validation(root, c, run)
    gone = [x for x in rec["findings"] if x.get("fixed_by") == "removed_with_block"
            and x["source"]["rule_id"] != "no-sensitive-paths-exposed"]
    assert gone and all(x["fix_kind"] == "human" for x in gone)


# ── #5 Checks never contradicts itself; #7 the work list reads the work ───────────────────────

def test_a_review_note_is_held_to_the_test_run():
    from boss_engine.services import review_packet as rp

    notes = rp.from_followups([
        {"text": "config/tenants/lumen-pay.yaml:25 F-349 remains unresolved. "
                 "`TestGwlint_ShippedTenantConfigs_ZeroViolations` still fails", "tier": "P1",
         "severity": "major"},
        {"text": "unknown: the suite failed but no failing existing test could be named, so "
                 "BOSS cannot say an old test broke", "tier": "P1", "severity": "major"},
        {"text": "Rename the helper for clarity", "tier": "P2", "severity": "minor"}], "standard")
    passed = [{"id": "T-1", "ok": True}]
    got = rp.judge_review_claims(list(notes), passed)
    assert [x["id"] for x in got] == ["R-1", "R-3"]
    assert got[0]["status"] == "suppressed" and got[0]["refuted_by"] == ["T-1"]
    assert got[1]["status"] == "open"
    # A failing run leaves the reviewer's word standing.
    assert rp.judge_review_claims(list(notes), [{"id": "T-1", "ok": False}])[0]["status"] == "open"


def test_a_finished_follow_up_is_done_and_a_merged_change_is_not_cancelled():
    from boss_engine.services.project_experience import work as w

    item = w._attempt_item("p", {"id": "crun_b", "_state": "needs_you", "status": "completed",
                                 "pull_request_url": None}, {}, "crun_b")
    assert item["state"] == "done"
    assert w.run_state("merged", False) == "done"
