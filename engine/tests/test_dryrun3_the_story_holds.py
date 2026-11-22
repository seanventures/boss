"""Dry run 3 (Northgate change #19): the validation story survives every follow-up.

  #1  a later build on the change (repair, follow-up, review-fix, retry) is held to the ruleset's
      autofix policy too: it inherits the change's contract, and only ITS OWN changes since it
      started are judged — the agent's removal of a person-only route is put back;
  #2  the story is computed against the ORIGINAL: a record checked against any other input
      (a partly fixed file) never replaces it; a re-run finds the original commit by itself;
  #3  an upload's secrets are masked before they are stored or committed, the true sha256 kept;
      every file/diff view masks secret-shaped values;
  #4  failing tests nobody can name are "unknown", never "old tests broke";
  #10 the upload keeps its bytes exactly, the final newline included.
"""
from __future__ import annotations

import subprocess
from types import SimpleNamespace

from boss_engine.services import finding_overrides as fo
from boss_engine.services import input_fidelity as f
from boss_engine.services import quality_bar as qb
from boss_engine.services import review_packet as rp
from boss_engine.services import start_from_file as sff
from boss_engine.services import validators as v
from boss_engine.services.redaction import mask_content

from .test_validation_tells_the_truth import (
    BAD, FIXED, TARGET, _agent, _by_rule, _seeded, _validate,
)

KEY = 'sk_live_' + '4242424242424242'


# ── #1 every build on the change is held to the autofix policy ────────────────────────────────

def test_a_follow_up_that_removes_a_person_only_route_has_it_put_back(tmp_path):
    root, c, _ = _seeded(tmp_path)
    rec, run = _validate(root, c, _agent(FIXED))
    rec = f.enforce_autofix(root, c, rec, run)
    first = (root / TARGET).read_text()           # the change's head after build 1
    before = list(rec["before"]["findings"])
    # THE FOLLOW-UP: it starts from the branch's target (earlier fixes kept), never re-seeded.
    c2 = {k: val for k, val in c.items() if k != "_auto_seeded"}
    c2["_start_text"] = first
    i, j = first.index("  - path: /v1/admin/accounts"), first.index("upstreams:")
    follow = first[:i] + first[j:]
    follow = follow.replace(f'      api_key: "{KEY}"\n', "")
    rec2, run2 = _validate(root, c2, follow)
    rec2 = f.enforce_autofix(root, c2, rec2, run2)
    target = (root / TARGET).read_text()
    assert "  - path: /v1/admin/accounts" in target and KEY in target
    by = _by_rule(rec2)
    assert by["no-sensitive-paths-exposed"][0]["status"] == "open"
    assert by["no-sensitive-paths-exposed"][0]["proposed"]
    # The earlier fixes the follow-up started from are not undone, and "before" is unchanged.
    assert "allow_credentials: false" in target
    assert rec2["before"]["findings"] == before


def test_a_follow_up_keeps_a_route_a_person_already_removed(tmp_path):
    root, c, _ = _seeded(tmp_path)
    rec, run = _validate(root, c, _agent(FIXED))
    f.enforce_autofix(root, c, rec, run)
    head = (root / TARGET).read_text()
    i, j = head.index("  - path: /v1/admin/accounts"), head.index("upstreams:")
    accepted = head[:i] + head[j:]                 # the person accepted the removal
    c2 = {k: val for k, val in c.items() if k != "_auto_seeded"}
    c2["_start_text"] = accepted
    rec2, run2 = _validate(root, c2, accepted)
    f.enforce_autofix(root, c2, rec2, run2)
    assert "/v1/admin/accounts" not in (root / TARGET).read_text()


def test_a_later_build_inherits_the_changes_contract():
    rows = {"id": "crun_first", "r": {"input": TARGET, "output": TARGET, "content": BAD,
                                      "start_from": True},
            "s": {"original_commit": "abc123"}}
    seen = {}

    class _C:
        def execute(self, _sql, params):
            seen.update(params)
            return SimpleNamespace(mappings=lambda: SimpleNamespace(first=lambda: rows))

    got = f.lineage_contract(_C(), "northgate", "crun_follow", "crun_first", "boss/code/x")
    assert got["run_id"] == "crun_first" and got["remediate"]["input"] == TARGET
    assert got["state"]["original_commit"] == "abc123"
    assert seen["me"] == "crun_follow" and seen["b"] == "boss/code/x"


# ── #2 the story is computed against the original ─────────────────────────────────────────────

def test_a_record_checked_against_a_partly_fixed_file_never_replaces_the_story():
    orig = {"input": {"file": TARGET, "sha256": "A"}, "before": {"findings": ["V-01"] * 9}}
    partial = {"input": {"file": TARGET, "sha256": "B"}, "before": {"findings": ["V-01"] * 2}}
    later = {"input": {"file": TARGET, "sha256": "A"}, "before": {"findings": ["V-01"] * 9},
             "after": {"findings": []}}
    assert rp.story_record([orig, partial]) == orig
    assert rp.story_record([orig, partial, later]) == later
    masked = {"input": {"file": TARGET, "sha256": "A", "masked_sha256": "M"}}
    assert rp.same_input({"input": {"file": TARGET, "sha256": "M"}}, masked)


def test_a_rerun_finds_the_original_commit_without_a_ref(tmp_path):
    def git(*a):
        return subprocess.run(["git", "-C", str(tmp_path), *a], check=True,
                              capture_output=True, text=True).stdout.strip()
    git("init", "-q")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    (tmp_path / "a.yaml").write_text("one\n")
    git("add", ".")
    git("commit", "-qm", f"{f.ORIGINAL_COMMIT_PREFIX}: a.yaml → a.yaml")
    first = git("rev-parse", "HEAD")
    (tmp_path / "a.yaml").write_text("two\n")
    git("commit", "-qam", "fix")
    assert fo._original_commit(tmp_path, "a.yaml") == first


# ── #3 the secret is never stored, committed or shown ─────────────────────────────────────────

def test_an_upload_is_masked_its_true_sha_kept_and_its_newline_kept():
    raw = BAD if BAD.endswith("\n") else BAD + "\n"
    assert KEY in raw
    got = sff.remediate({"start_from": "lumen-pay.yaml", "start_from_content": raw,
                         "target_path": TARGET})
    assert KEY not in got["content"] and got["content"].endswith("\n")
    assert got["content_sha256"] == v.sha256(raw)
    stored = sff.mask_run_input({"start_from_content": raw, "title": "x"})
    assert KEY not in stored["start_from_content"]
    assert stored[sff.CONTENT_SHA_KEY] == v.sha256(raw)
    # The stored input's sha travels: the remediation keeps the TRUE bytes' sha.
    again = sff.remediate({**stored, "start_from": "lumen-pay.yaml", "target_path": TARGET})
    assert again["content_sha256"] == v.sha256(raw)


def test_the_record_keeps_the_true_sha_and_accepts_the_masked_original(tmp_path):
    raw = BAD
    spec = {"remediate": {"input": TARGET, "output": TARGET, "content": raw, "start_from": True,
                          "content_sha256": v.sha256(raw)}}
    (tmp_path / "config" / "tenants").mkdir(parents=True)
    c = f.contract(spec, "", tmp_path)
    assert KEY not in c["_input_text"] and c["input_sha256"] == v.sha256(raw)
    rec = f.run_validation(tmp_path, c, None)
    assert rec["input"]["sha256"] == v.sha256(raw)
    assert rec["input"]["masked_sha256"] == v.sha256(c["_input_text"])
    assert v.is_original(c["_input_text"], rec["input"]) and v.is_original(raw, rec["input"])
    assert not v.is_original("something else", rec["input"])


def test_every_file_and_diff_view_masks_secret_shaped_values():
    payload = {"files": [{"path": TARGET, "patch": f'+      api_key: "{KEY}"\n'}],
               "content": f"api_key: {KEY}", "n": 3}
    out = mask_content(payload)
    assert KEY not in str(out) and out["n"] == 3 and out["files"][0]["path"] == TARGET


# ── #4 unnamed failing tests are unknown, never "old tests broke" ─────────────────────────────

def test_failing_tests_nobody_can_name_are_unknown_at_every_bar():
    for bar in ("prototype", "standard", "production"):
        blocks, why = qb.broken_tests_block(bar, [])
        assert blocks is False
        state = qb.old_tests_state(why)
        assert state["state"] == "unknown" and "could be named" in state["why"]
    assert qb.broken_tests_block("production", ["test/x.test.js > y"])[0] is True
    assert qb.old_tests_state("2 existing test(s) failing") is None
