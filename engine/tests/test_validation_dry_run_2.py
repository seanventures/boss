"""Dry run 2 of Lumen Pay → Northgate (change #14, run #20): the engine gaps, one test (or more)
each. The data is the dry run's own (fixtures/validation/dryrun2-change14.json: the target as it
stood when a person pressed Accept on V-07, and the proposal the build recorded) and the real
gwlint outputs on the partner's file (fixtures/validation, replayed by test_validation_tells_the
_truth's fake gwlint).

  #2  a person removes lines: `remove` (or an empty edit) deletes the finding's whole block;
      the real V-07 proposal applies (its pieces were matched across the whole file, and a line
      every route repeats made it "gone");
  #4  BOSS's mechanical fix pass works when the validator runs as the run's own uid;
  #5  BOSS's own guides for upstream-https-required and CORS origins; "deploy to staging"
      never changes a host;
  #6  a hunk a person's override wrote is explained by it;
  #7  `change` says what happened to the finding's own line: never "removed" for an open one,
      never "true → true";
  #8  a comment claiming a fix for an open finding is flagged;
  #11 `line` is the partner's line, `target_line` the target's;
  #12 counts include guards everywhere, `you` counts what needs a person; a ruleset edit
      rewrites only the edited rule.
"""
from __future__ import annotations

import difflib
import importlib.util
import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from boss_engine.services import finding_overrides as fo
from boss_engine.services import input_fidelity as f
from boss_engine.services import review_packet as rp
from boss_engine.services import ruleset_changes as rc
from boss_engine.services import rulesets
from boss_engine.services import validators as v

HERE = Path(__file__).parent
DRY = json.loads((HERE / "fixtures" / "validation" / "dryrun2-change14.json").read_text())


def _truth():
    spec = importlib.util.spec_from_file_location(
        "truth_dr2", HERE / "test_validation_tells_the_truth.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


T = _truth()
ADMIN_BLOCK = [
    "  - path: /v1/admin/accounts",
    '    description: "Internal account admin surface, never meant for tenants."',
    '    methods: ["GET"]',
    "    upstream: lumen-pay-core",
    "    auth:",
    "      type: jwt",
    '      issuer: "https://issuer.lumenpay.example"',
    '      audience: "northgate"',
    '      scopes: ["admin.read"]',
    '      jwks_url: "https://issuer.lumenpay.example/.well-known/jwks.json"',
    "    rate_limit:",
    "      rps: 10",
    "      burst: 10",
]


# ── #2 a person removes lines ──────────────────────────────────────────────────────────────────

def test_remove_deletes_the_admin_routes_whole_block_and_nothing_else():
    target = DRY["target"]
    out = fo.remove_block(target, DRY["V-07"])
    gone = [x for x in difflib.ndiff(target.splitlines(), out.splitlines()) if x.startswith("- ")]
    assert [x[2:] for x in gone] == ADMIN_BLOCK
    assert "  - path: /v1/payments" in out and "upstreams:" in out


def test_remove_on_a_key_deletes_only_that_keys_line():
    out = fo.remove_block(DRY["target"], DRY["V-05"])
    gone = [x[2:] for x in difflib.ndiff(DRY["target"].splitlines(), out.splitlines())
            if x.startswith("- ")]
    assert gone == ['      api_key: "[SECRET]"']


def test_remove_refuses_a_file_level_finding_rather_than_empty_the_file():
    tenant = {"original": "tenant: lumen-pay", "location": {"line": 1, "target_line": 7}}
    with pytest.raises(fo.OverrideError) as err:
        fo.remove_block(DRY["target"], tenant)
    assert err.value.code == "no_block"


def test_the_real_v07_proposal_applies_its_pieces_in_order():
    """THE CAUSE OF proposal_gone: piece 2 inserts after `upstream: lumen-pay-core`, a line both
    routes have. Matched across the whole file it was "not there exactly once"."""
    target, prop = DRY["target"], DRY["V-07"]["proposed"]
    assert target.count("    upstream: lumen-pay-core\n") == 2
    out = fo.apply_proposal(target, prop)
    assert "/v1/admin/accounts" not in out and "  - path: /v1/payments/refunds" in out
    # The insertion landed in the SECOND route (the one the proposal rewrote), not the first.
    lines = out.splitlines()
    refunds = lines.index("  - path: /v1/payments/refunds")
    assert lines.index("    max_body_bytes: 65536", refunds) > refunds
    assert "remove" in fo.ACTIONS and "remove" in fo.COMMITS


# ── #4 the mechanical fix pass, as the run's uid ───────────────────────────────────────────────

def _as_other_uid(root: Path, ran: list):
    """gwlint as the run's sandbox uid, which has NO supplementary groups: it can write a file
    only through the "other" bits (what boss-sandbox-exec does)."""
    real = T._gwlint(root, ran)

    def run(cmd: str):
        if "--fix" in cmd:
            p = root / cmd.split()[-1].strip("'")
            if not (stat.S_IMODE(p.stat().st_mode) & 0o002):
                ran.append("EACCES")
                return SimpleNamespace(stdout="", stderr="write: permission denied",
                                       exit_code=1)
        return real(cmd)
    return run


def test_the_fixer_can_write_its_copy_as_the_runs_uid_and_seeds_the_target(tmp_path):
    root = T._repo(tmp_path)
    os.umask(0o022)                       # BOSS's umask in the container
    c = f.contract(T.SPEC, "", root)
    ran: list = []
    cmd = f.seed_auto_fix(root, c, _as_other_uid(root, ran))
    assert "EACCES" not in ran, "the seed copy was not writable by the run's uid"
    assert cmd and c["auto_fix_seed"]["seeded"] is True
    assert (root / T.TARGET).read_text() == T.FIXED
    rec = f.run_validation(root, c, _as_other_uid(root, ran))
    assert "EACCES" not in ran
    assert rec["fix"]["seeded"] is True
    assert {x["kind"] for x in rec["fixes"] if x["finding"]} == {"auto"}
    assert sum(1 for x in rec["fixes"] if x["kind"] == "auto") >= 4


def test_a_seed_that_did_not_happen_says_why(tmp_path):
    root = T._repo(tmp_path)
    c = f.contract(T.SPEC, "", root)
    nothing = lambda cmd: SimpleNamespace(stdout="", stderr="go: cannot find main module",  # noqa: E731
                                          exit_code=1)
    assert f.seed_auto_fix(root, c, nothing) is None
    seed = c["auto_fix_seed"]
    assert seed["seeded"] is False and seed["exit"] == 1 and "cannot find" in seed["why"]
    assert f.public(c)["auto_fix_seed"] == seed       # on the run's contract event, said


# ── #5 guides that never invent a host ─────────────────────────────────────────────────────────

def test_the_fixing_agent_gets_boss_guides_for_https_and_cors_and_staging_is_not_a_host(tmp_path):
    root = T._repo(tmp_path)
    c = f.contract(T.SPEC, "", root)
    brief = f.brief(c)
    assert "Default to allow_credentials: false." in brief          # the ruleset's own guide
    assert rulesets.BUILTIN_GUIDES["cors-no-wildcard-with-credentials"] in brief
    assert rulesets.BUILTIN_GUIDES["upstream-https-required"] in brief
    assert "never use a mock, staging, test or localhost host" in brief
    assert "never changes a host, URL or origin" in brief
    assert "Never write a comment that says a finding was fixed" in brief


# ── #6 a person's own hunk is explained by their override ──────────────────────────────────────

def test_a_hunk_a_persons_edit_wrote_is_explained_by_that_override():
    rec = {"fixes": [{"finding": None, "unexplained": True, "hunk_id": "h-04", "kind": "agent",
                      "hunks": [{"id": "h-04", "before": ['      allow_origins: ["*"]'],
                                 "after": ['      allow_origins: ["https://dashboard.lumenpay.example"]']}]}]}
    before = DRY["target"]
    after = before.replace('allow_origins: ["https://app.lumenpay.example"]',
                           'allow_origins: ["https://dashboard.lumenpay.example"]')
    o1 = {"ref": "O-1", "action": "edit", "commit_sha": "c1", "before_text": before,
          "after_text": after}
    assert fo.override_hunks(rec, [o1]) == {"h-04": ["O-1"]}
    shown = fo.explain_fixes(rec, [o1])["fixes"][0]
    assert shown["override"] == "O-1" and shown["kind"] == "human" and "unexplained" not in shown
    vs = rp.explain_by_overrides({"unexplained_hunks": ["h-01", "h-04"]}, {"h-04": ["O-1"]})
    assert vs == {"unexplained_hunks": ["h-01"], "hunks_by_override": {"h-04": ["O-1"]}}
    p = rp.verdict({"vs_input": vs})
    assert "unexplained_hunk:h-04" not in p["reasons"]


# ── #7 what happened to the finding's own line ─────────────────────────────────────────────────

def test_an_open_untouched_finding_is_change_none_never_removed():
    for fid in ("V-05", "V-07"):
        g = v.set_change(json.loads(json.dumps(DRY[fid])))
        assert g["change"] == "none" and g["fixed"] is None


def test_a_fix_elsewhere_in_the_block_is_never_true_to_true():
    v06 = {"id": "V-06", "source": {"kind": "validator"}, "status": "fixed", "hunk_id": "h-04",
           "original": "      allow_credentials: true", "fixed": "      allow_credentials: true",
           "fixed_by": "nearest", "location": {"line": 20, "partner_line": 20, "target_line": 31}}
    g = v.set_change(v06)
    assert g["change"] == "elsewhere" and g["fixed"] is None and g["hunk_id"] == "h-04"


def test_the_record_says_what_changed_for_every_finding(tmp_path):
    root, c, _ = T._seeded(tmp_path)
    rec, _run = T._validate(root, c, T._agent(T.FIXED))
    by = {x["id"]: x for x in rec["findings"]}
    assert all("change" in x for x in rec["findings"])
    for x in rec["findings"]:
        if x["change"] == "none":
            assert x["fixed"] is None and x["status"] in ("open", "suppressed")
        if x["status"] == "fixed":
            assert x["change"] in ("edited", "added", "removed", "elsewhere")
    rate = next(x for x in by.values() if x["source"]["rule_id"] == "rate-limit-required")
    assert rate["change"] == "added" and rate["fixed"]


def test_override_records_carry_the_actor_and_their_name():
    rows = [{"ref": "O-1", "action": "edit", "actor": "usr_1", "actor_name": "Dana",
             "finding_id": "V-06", "reason": "r", "created_at": "t"}]
    out = fo.apply([{"id": "V-06", "status": "fixed", "source": {"kind": "validator"}}], rows)
    assert out[0]["override"]["action"] == "edit" and out[0]["override"]["actor"] == "usr_1"
    assert out[0]["override"]["actor_name"] == "Dana"


# ── #8 a comment that claims a fix ─────────────────────────────────────────────────────────────

def test_a_comment_claiming_an_open_finding_is_fixed_blocks_with_a_ref():
    lines = DRY["target"].splitlines()
    findings = [DRY["V-05"], DRY["V-07"]]
    claims = v.comment_claims(lines, findings)
    assert {x["rule"] for x in claims} == {"no-inlined-secrets", "no-sensitive-paths-exposed"}
    p = rp.build_packet(work={"title": "t"}, head_sha="h",
                        validations=[{"findings": findings, "comment_claims": claims,
                                      "validator": {"id": "gwlint"}, "hunks": [], "fixes": []}])
    assert {"unbacked_comment:V-05", "unbacked_comment:V-07"} <= set(p["verdict"]["reasons"])
    assert any(r["refs"] == ["V-05"] for r in p["risks"]) and rp.validate_packet(p) == []


def test_a_comment_that_says_a_finding_is_left_for_a_person_is_not_a_claim():
    lines = ["# - no-inlined-secrets: inlined auth.api_key below is left untouched (needs a person).",
             "# - no-sensitive-paths-exposed: not removed; a product decision."]
    assert v.comment_claims(lines, [DRY["V-05"], DRY["V-07"]]) == []


def test_a_comment_the_partner_sent_is_never_the_agents_claim():
    """Only comments the CHANGE wrote are claims: the partner's own comment naming a rule with a
    done-word ("dropped by request-size-capped") would otherwise block every change to the file."""
    lines = DRY["target"].splitlines()
    claims = v.comment_claims(lines, [DRY["V-05"], DRY["V-07"]], in_lines=lines)
    assert claims == []


# ── #11 one line basis ─────────────────────────────────────────────────────────────────────────

def test_line_is_the_partners_line_and_target_line_the_targets(tmp_path):
    root, c, _ = T._seeded(tmp_path)
    rec, _run = T._validate(root, c, T._agent(T.FIXED))
    for x in rec["findings"]:
        loc = x["location"]
        if x["source"]["kind"] == "validator" and not x.get("introduced"):
            assert loc["line"] == loc["partner_line"]
        assert "target_line" in loc
    secret = next(x for x in rec["findings"] if x["source"]["rule_id"] == "no-inlined-secrets")
    assert secret["location"]["line"] == 17                    # SPEC §6: "secret on L17"


# ── #12 counts ─────────────────────────────────────────────────────────────────────────────────

def test_guards_are_counted_the_same_in_the_row_and_the_counts_and_you_is_exposed():
    rec = T._record(T.DRYRUN)
    p = rp.build_packet(work={"title": "t"}, head_sha="h", validations=[rec],
                        apply_overrides=lambda fs: fs)
    guards = [x for x in p["findings"] if x["source"]["kind"] == "guard"
              and x["status"] == "open"]
    assert guards
    row = p["validators"][0]
    opened = [x for x in p["findings"] if x["source"]["kind"] in ("validator", "guard")
              and x["status"] in ("open", "reverted")]
    assert row["after_open"] == len(opened) and row["guards_open"] == len(guards)
    assert sum(p["counts"][lv]["open"] for lv in rp.PLEVELS) == len(opened)
    assert p["counts"]["you"] == row["you"] == sum(1 for x in opened if rp.needs_person(x))
    assert rp.validate_packet(p) == []
    # The record's own row (no overrides) says the same.
    _fs, row0, _vs = rp.from_validation(rec)
    assert row0["after_open"] == row["after_open"]


RULESET_FILE = """\
ruleset: northgate
version: 3
validator: gwlint
# P-levels agreed with the platform team.
rules:
  no-inlined-secrets:                {severity: P0, autofix: never,
     guide: "Delete the key if the route already authenticates another way."}
  cors-no-wildcard-with-credentials: {severity: P0, autofix: agent, guide: "Default to allow_credentials: false."}
  # removing a route is a product decision
  no-sensitive-paths-exposed:        {severity: P0, autofix: human}
  pii-fields-declared:               {severity: P2, autofix: agent}
"""


def test_a_ruleset_edit_changes_only_the_edited_rule_and_keeps_comments():
    base = rulesets.parse(RULESET_FILE)
    rules = [{"id": k, **r} for k, r in base["rules"].items()]
    next(r for r in rules if r["id"] == "pii-fields-declared")["severity"] = "P1"
    _full, new = rc.render(base, 4, rules)
    out = rc.minimal_edit(RULESET_FILE, base, new, 4)
    diff = [x for x in difflib.unified_diff(RULESET_FILE.splitlines(), out.splitlines(),
                                            lineterm="", n=0)
            if x[:1] in "+-" and not x.startswith(("+++", "---"))]
    assert diff == ["-version: 3", "+version: 4",
                    "-  pii-fields-declared:               {severity: P2, autofix: agent}",
                    "+  pii-fields-declared:               {severity: P1, autofix: agent}"]
    assert "# removing a route is a product decision" in out
    assert rulesets.parse(out)["rules"] == new["rules"]


def test_a_ruleset_edit_of_a_block_style_file_stays_block_style():
    text = ("ruleset: n\nversion: 1\nrules:\n  a-rule:\n    severity: P0\n    autofix: never\n"
            "  b-rule:\n    severity: P2\n    autofix: agent\n    guide: 'keep me'\n")
    base = rulesets.parse(text)
    rules = [{"id": k, **r} for k, r in base["rules"].items()]
    rules[0]["autofix"] = "human"
    _full, new = rc.render(base, 2, rules)
    out = rc.minimal_edit(text, base, new, 2)
    assert out == text.replace("version: 1", "version: 2").replace(
        "    autofix: never", "    autofix: human")


def test_a_proposal_whose_first_piece_is_on_the_branch_twice_is_gone_never_guessed():
    text_ = "routes:\n  - path: /a\n    upstream: core\n  - path: /b\n    upstream: core\n"
    with pytest.raises(fo.OverrideError) as exc:
        fo.apply_proposal(text_, {"pieces": [{"remove": ["    upstream: core"],
                                              "after": ["    upstream: edge"]}]})
    assert exc.value.code == "proposal_gone"
    # Placed by a unique first piece, a repeated line after it is the one in the same route.
    got = fo.apply_proposal(text_, {"pieces": [
        {"remove": ["  - path: /b"], "after": ["  - path: /b"]},
        {"remove": ["    upstream: core"], "after": ["    upstream: edge"]}]})
    assert got.splitlines()[2] == "    upstream: core" and got.splitlines()[4] == "    upstream: edge"


def test_a_wrapped_flow_entry_keeps_its_wrap_when_one_value_changes():
    """Dry run 3, gap 9: a `{…}` entry wrapped over two lines was rejoined onto one; only the
    changed value moves now, the wrap and the guide line stay byte for byte."""
    text = ("ruleset: n\nversion: 1\nrules:\n"
            "  a-rule:   {severity: P0, autofix: never}\n"
            "  pii-fields-declared:   {severity: P2, autofix: agent,\n"
            "     guide: \"Declare the PII fields; ask when unsure.\"}\n")
    base = rulesets.parse(text)
    rules = [{"id": k, **r} for k, r in base["rules"].items()]
    rules[1]["severity"] = "P1"
    _full, new = rc.render(base, 2, rules)
    out = rc.minimal_edit(text, base, new, 2)
    assert out == text.replace("version: 1", "version: 2").replace("{severity: P2", "{severity: P1")
    rules[1]["guide"] = 'Say "why"'
    _full, new = rc.render(base, 2, rules)
    out = rc.minimal_edit(text, base, new, 2)
    assert out.splitlines()[5] == '     guide: "Say \\"why\\""}'
    assert rulesets.parse(out)["rules"] == new["rules"]
