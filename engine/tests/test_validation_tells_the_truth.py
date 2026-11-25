"""The validation record tells the truth (dry run of Lumen Pay → Northgate, change #11).

Gaps fixed, one test (or more) each:
  #1  a `human` / `never` rule is never agent-fixed: the agent's lines are refused (put back), the
      finding stays open as "Needs you" with the agent's change as a proposal; the brief says so;
  #2  a fix may not make the config less safe: an unexplained change to auth / upstream keys, or
      one that moves a URL to another host, is a P0 guard finding; a P0 sharing its hunk is not
      fixed until a person accepts it; a security reviewer's HIGH on a P0 line is never deferred;
  #3  a finding the after-run no longer reports is fixed — by the hunk on its block, or "removed
      with its block" — never left open;
  #4  BOSS runs the mechanical fixer first; its lines are `auto`, one `fixes[]` entry per finding;
  #5  masking keeps `allow_credentials: true|false`, masks `api_key: sk_live_…`;
  #11 a removed line's location falls back to the partner's line.

Data: the real `gwlint validate --json` / `gwlint --fix` outputs on Lumen Pay's partner file
(fixtures/validation, from #276). An agent's edits are applied on top of the real fixer output;
gwlint's after-run on such a file replays the real after-run violations whose trigger is still in
the file (same rule ids, titles and fix text), at the line the trigger now sits on.
`lumen-pay.dryrun-agent.yaml` is the dry run's target, rebuilt from its recorded hunks.
"""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from types import SimpleNamespace

from boss_engine.services import finding_overrides as fo
from boss_engine.services import input_fidelity as f
from boss_engine.services import review_packet as rp
from boss_engine.services import rulesets
from boss_engine.services import validators as v
from boss_engine.services.redaction import redact_secrets

FIX = Path(__file__).parent / "fixtures" / "validation"
INPUT, TARGET = "examples/lumen-pay.bad.yaml", "config/tenants/lumen-pay.yaml"
SPEC = {"remediate": {"input": INPUT, "output": TARGET}}
BAD = (FIX / "lumen-pay.bad.yaml").read_text()
FIXED = (FIX / "lumen-pay.fixed.yaml").read_text()
DRYRUN = (FIX / "lumen-pay.dryrun-agent.yaml").read_text()
RULESET = """\
ruleset: northgate
version: 3
validator: gwlint
rules:
  no-inlined-secrets: {severity: P0, autofix: never, guide: "Delete the key if the route already authenticates another way."}
  cors-no-wildcard-with-credentials: {severity: P0, autofix: agent, guide: "Default to allow_credentials: false."}
  no-sensitive-paths-exposed: {severity: P0, autofix: human}
  upstream-https-required: {severity: P0, autofix: mechanical}
  tenant-rate-limit-required: {severity: P1, autofix: agent, guide: "rps 50, burst 50 unless the brief names a quota."}
  rate-limit-required: {severity: P1, autofix: mechanical}
  request-size-capped: {severity: P1, autofix: mechanical}
  pii-fields-declared: {severity: P2, autofix: agent}
"""
#: Where each after-run rule's trigger is (the real gwlint.after.json entries are replayed only
#: while it is in the file).
TRIGGER = {
    "tenant-rate-limit-required": lambda ls: None if any(x.startswith("rate_limit:") for x in ls)
    else 1,
    "pii-fields-declared": lambda ls: None if any(x.startswith("pii_fields:") for x in ls)
    else next((n for n, x in enumerate(ls, 1) if x.startswith("data_classification:")), None),
    "no-inlined-secrets": lambda ls: next((n for n, x in enumerate(ls, 1)
                                           if re.search(r'api_key:\s*"sk_live_', x)), None),
    "cors-no-wildcard-with-credentials": lambda ls: next(
        (n for n, x in enumerate(ls, 1) if "allow_credentials: true" in x), None),
    "no-sensitive-paths-exposed": lambda ls: next(
        (n for n, x in enumerate(ls, 1) if re.match(r"\s*- path: /v1/admin", x)), None),
}


def _after_report(text: str) -> tuple[str, int]:
    ls = text.splitlines()
    real = json.loads((FIX / "gwlint.after.json").read_text())["violations"]
    out = []
    for viol in real:
        at = TRIGGER[viol["rule_id"]](ls)
        if at is not None:
            out.append({**viol, "line": at})
    return json.dumps({"path": "lumen-pay.yaml", "violations": out}), (1 if out else 0)


def _gwlint(root: Path, ran: list):
    def run(cmd: str):
        ran.append(cmd)
        if "--fix" in cmd:
            (root / cmd.split()[-1].strip("'")).write_text(FIXED)
            return SimpleNamespace(stdout="", exit_code=1)
        body = (root / cmd.split()[-2].strip("'")).read_text()
        if body == BAD:
            return SimpleNamespace(stdout=(FIX / "gwlint.before.json").read_text(), exit_code=1)
        out, code = _after_report(body)
        return SimpleNamespace(stdout=out, exit_code=code)
    return run


def _repo(tmp_path: Path, *, ruleset: str = RULESET) -> Path:
    root = tmp_path / "northgate"
    for d in ("examples", "config/tenants", ".boss/rulesets"):
        (root / d).mkdir(parents=True, exist_ok=True)
    (root / INPUT).write_text(BAD)
    shutil.copy(FIX / "validators.yaml", root / ".boss" / "validators.yaml")
    (root / ".boss" / "rulesets" / "northgate.yaml").write_text(ruleset)
    return root


def _seeded(tmp_path: Path, ran=None):
    """The build's start: contract, then BOSS's fixer run first (the target is its output)."""
    root = _repo(tmp_path)
    c = f.contract(SPEC, "", root)
    cmd = f.seed_auto_fix(root, c, _gwlint(root, ran if ran is not None else []))
    return root, c, cmd


def _agent(text: str) -> str:
    """What a fixing agent typically does on the seeded file: every never-fixable finding."""
    text = text.replace('      api_key: "sk_live_4242424242424242"\n', "")      # V-05 (never)
    text = text.replace("allow_credentials: true", "allow_credentials: false")  # V-06 (agent)
    i, j = text.index("  - path: /v1/admin/accounts"), text.index("upstreams:")
    text = text[:i] + text[j:]                                                 # V-07 (human)
    return text.replace("data_classification: pii\n", "data_classification: pii\n"
                        'pii_fields: ["merchant_id"]\nrate_limit:\n  rps: 50\n  burst: 50\n')


def _validate(root: Path, c: dict, target: str = None, *, ruleset=None):
    if target is not None:
        (root / TARGET).write_text(target)
    run = _gwlint(root, [])
    rec = f.run_validation(root, c, run, ruleset=ruleset)
    return rec, run


def _by_rule(rec: dict) -> dict:
    out: dict = {}
    for x in rec["findings"]:
        out.setdefault(x["source"]["rule_id"], []).append(x)
    return out


# ── #4 the mechanical fixer first; one fixes[] entry per finding ───────────────────────────────

def test_boss_runs_the_fixer_first_and_the_target_starts_as_its_output(tmp_path):
    ran: list = []
    root, c, cmd = _seeded(tmp_path, ran)
    assert cmd == "go run ./cmd/gwlint --fix .boss-validation/seed/lumen-pay.yaml"
    assert (root / TARGET).read_text() == FIXED and c["_auto_seeded"] == FIXED
    assert not (root / ".boss-validation").exists()
    assert "BOSS ALREADY RAN THE MECHANICAL FIXER" in f.brief(c)


def test_the_fixers_lines_are_auto_and_the_agents_are_agent_one_entry_per_finding(tmp_path):
    root, c, _ = _seeded(tmp_path)
    rec, _run = _validate(root, c, _agent(FIXED))
    by = _by_rule(rec)
    auto = sorted((x["source"]["rule_id"], x["location"]["partner_line"]) for x in rec["findings"]
                  if x["fix_kind"] == "auto" and x["status"] == "fixed")
    # The admin route's size cap went with the route the agent removed (refused below).
    assert auto == [("rate-limit-required", 7), ("request-size-capped", 7),
                    ("upstream-https-required", 35)]
    size21 = next(x for x in by["request-size-capped"] if x["location"]["partner_line"] == 21)
    # It went with a block only a person may remove: the person's, never "fixed by agent".
    assert size21["fixed_by"] == "removed_with_block" and size21["fix_kind"] == "human"
    assert by["tenant-rate-limit-required"][0]["fix_kind"] == "agent"
    assert by["cors-no-wildcard-with-credentials"][0]["fix_kind"] == "agent"
    # One entry per finding, its lines grouped by hunk — never one entry per changed line.
    fixes = [x for x in rec["fixes"] if x["finding"]]
    assert len({x["finding"] for x in fixes}) == len(fixes)
    rl = next(x for x in fixes if x["rule"] == "tenant-rate-limit-required")
    assert rl["lines"] == 3 and rl["after"] == "rate_limit:\n  rps: 50\n  burst: 50"
    assert all(h["id"] and isinstance(h["after"], list) for x in fixes for h in x["hunks"])
    assert [x for x in rec["fixes"] if not x["finding"]] == []


def test_lines_with_no_rule_are_grouped_as_unexplained_per_hunk():
    rec = _record(DRYRUN)
    unexplained = [x for x in rec["fixes"] if not x["finding"]]
    assert unexplained and all(x["unexplained"] is True and x["rule"] is None for x in unexplained)
    assert len({x["hunk_id"] for x in unexplained}) == len(unexplained)
    header = next(x for x in unexplained if x["hunk_id"] == "h-01")
    assert header["lines"] == 8 and header["after"].startswith("# Remediated per gwlint")


# ── #1 a human / never rule is never agent-fixed ──────────────────────────────────────────────

def test_the_brief_says_plainly_which_rules_are_not_the_agents():
    text = rulesets.brief(rulesets.parse(RULESET))
    assert "A rule with autofix `human` or `never` is NOT YOURS TO FIX" in text
    assert "Leave for a person: `no-inlined-secrets`, `no-sensitive-paths-exposed`." in text


def test_an_agent_fix_of_a_human_or_never_rule_is_held_not_counted_fixed(tmp_path):
    root, c, _ = _seeded(tmp_path)
    rec, _run = _validate(root, c, _agent(FIXED))
    by = _by_rule(rec)
    for rule, autofix in (("no-inlined-secrets", "never"), ("no-sensitive-paths-exposed", "human")):
        x = by[rule][0]
        assert x["status"] == "open" and x["fix_kind"] == "human", x
        assert x["held"] == {"autofix": autofix, "by": "ruleset"} and x["fixed_if_released"]
    # The agent's own rule is its to fix.
    assert by["cors-no-wildcard-with-credentials"][0]["status"] == "fixed"


def test_the_build_refuses_the_agents_lines_and_shows_needs_you_with_a_proposal(tmp_path):
    root, c, _ = _seeded(tmp_path)
    rec, run = _validate(root, c, _agent(FIXED))
    rec = f.enforce_autofix(root, c, rec, run)
    target = (root / TARGET).read_text()
    # Put back: the partner's key line and the admin route (as the fixer left it); the agent's
    # own fixes (CORS, tenant rate limit, pii) stay.
    assert 'api_key: "sk_live_4242424242424242"' in target
    assert "  - path: /v1/admin/accounts\n    max_body_bytes: 1048576\n" in target
    assert "allow_credentials: false" in target and "  rps: 50" in target
    by = _by_rule(rec)
    secret, admin = by["no-inlined-secrets"][0], by["no-sensitive-paths-exposed"][0]
    assert secret["status"] == admin["status"] == "open"
    assert secret["fix_kind"] == admin["fix_kind"] == "human"
    assert {r["finding"] for r in rec["refused"]} == {secret["id"], admin["id"]}
    assert admin["proposed"]["applicable"] is True and admin["proposed"]["after"] is None
    assert admin["proposed"]["pieces"][0]["remove"][0] == "  - path: /v1/admin/accounts"
    # The secret never reaches the proposal.
    assert "sk_live_" not in json.dumps(f.public_validation(rec))
    assert by["cors-no-wildcard-with-credentials"][0]["status"] == "fixed"
    # The §6 demo's counts: 4 Auto (gwlint --fix), the agent's own, 2 Needs you.
    auto = sorted((x["source"]["rule_id"], x["location"]["partner_line"]) for x in rec["findings"]
                  if x["fix_kind"] == "auto" and x["status"] == "fixed")
    assert auto == [("rate-limit-required", 7), ("request-size-capped", 7),
                    ("request-size-capped", 21), ("upstream-https-required", 35)]
    assert sorted(x["source"]["rule_id"] for x in rec["findings"] if x["fix_kind"] == "human") \
        == ["no-inlined-secrets", "no-sensitive-paths-exposed"]


def test_accepting_needs_you_applies_the_proposal_and_counts_it_human(tmp_path):
    root, c, _ = _seeded(tmp_path)
    rec, run = _validate(root, c, _agent(FIXED))
    rec = f.enforce_autofix(root, c, rec, run)
    admin = _by_rule(rec)["no-sensitive-paths-exposed"][0]
    applied = fo.apply_proposal((root / TARGET).read_text(), admin["proposed"])
    assert "/v1/admin/accounts" not in applied
    rerun, _ = _validate(root, c, applied)
    again = next(x for x in rerun["findings"] if x["id"] == admin["id"])
    assert again["status"] == "open" and again["held"]           # the agent's lines, held
    accepted = fo.apply(rerun["findings"], [{"ref": "O-1", "action": "accept",
                                             "finding_id": admin["id"]}])
    done = next(x for x in accepted if x["id"] == admin["id"])
    assert done["status"] == "fixed" and done["fix_kind"] == "human"


# ── #2 a fix can't make the config less safe ──────────────────────────────────────────────────

def _record(target: str, after_raw=None, ruleset=RULESET) -> dict:
    from boss_engine.services.validator_adapters import parse

    return v.record(
        c={"input": INPUT, "output": TARGET, "_input_text": BAD},
        validator={"name": "gwlint", "check": "c {file}", "fix": "f {file}"}, version=None,
        before_raw=parse("gwlint-json", (FIX / "gwlint.before.json").read_text(), INPUT),
        after_raw=after_raw if after_raw is not None else [],
        commands={"before": "b", "fix": "f", "after": "a"},
        exits={"before": 1, "fix": 1, "after": 0}, out_text=target, auto_text=FIXED,
        ruleset=rulesets.parse(ruleset) if ruleset else None)


def test_an_unexplained_jwks_change_and_an_upstream_moved_to_a_mock_are_p0_guards():
    rec = _record(DRYRUN)
    guards = [x for x in rec["findings"] if x["source"]["kind"] == "guard"]
    assert [(g["id"], g["severity"], g["status"], g["security"]["keys"]) for g in guards] == [
        ("U-01", "P0", "open", ["auth.jwks_url"]), ("U-02", "P0", "open", ["lumen-pay-core.url"])]
    assert "unexplained" in guards[0]["security"]["why"]
    assert guards[1]["security"]["why"] == ["host_changed"]
    https = _by_rule(rec)["upstream-https-required"][0]
    assert https["status"] == "open" and https["blocked_by"] == ["U-02"]
    p = rp.build_packet(work={"title": "t"}, head_sha="x", validations=[rec])
    assert "open_p0:U-01" in p["verdict"]["reasons"] and "open_p0:U-02" in p["verdict"]["reasons"]
    assert not any(c["text"].startswith("All ") and "P0" in c["text"] for c in p["claims"])
    assert rp.validate_packet(p) == []


def test_a_p0_counts_fixed_once_a_person_accepts_the_security_change():
    rec = _record(DRYRUN)
    u2 = next(x for x in rec["findings"] if x["id"] == "U-02")
    out = fo.apply(json.loads(json.dumps(rec["findings"])),
                   [{"ref": "O-1", "action": "accept", "finding_id": "U-02",
                     "before_text": u2["original"], "after_text": u2["fixed"]}])
    by = {x["id"]: x for x in out}
    assert by["U-02"]["status"] == "overridden"
    https = next(x for x in out if x["source"]["rule_id"] == "upstream-https-required")
    assert https["status"] == "fixed"


def test_an_accepted_guard_does_not_carry_over_to_a_different_security_change():
    # U-nn are numbered per record: after a push, U-02 may be ANOTHER change. The accept was
    # for the lines the person saw, so a different U-02 stays open and keeps blocking its P0.
    rec = _record(DRYRUN)
    out = fo.apply(json.loads(json.dumps(rec["findings"])),
                   [{"ref": "O-1", "action": "accept", "finding_id": "U-02",
                     "before_text": "    url: https://lumen-pay-core.internal",
                     "after_text": "    url: https://lumen-pay-core.internal:8443"}])
    by = {x["id"]: x for x in out}
    assert by["U-02"]["status"] == "open"
    assert next(x for x in out if x["source"]["rule_id"] == "upstream-https-required"
                )["status"] == "open"


def test_a_host_move_is_guarded_under_a_renamed_or_nested_key():
    inp = ("tenant: t\nroutes:\n  - path: /pay\n    policy:\n      jwksUrl: https://idp.t.com/k\n"
           "    backend:\n      url: https://core.t.com\n")
    out = inp.replace("https://idp.t.com/k", "https://mock-staging:9001/k") \
             .replace("https://core.t.com", "https://mock-upstream:9001")
    _, lines = v.changed_lines(inp, out, None, [])
    got = {x["keys"][-1]: x["why"] for x in v.security_changes(inp, out, lines)}
    assert "host_changed" in got["jwksUrl"] and "unexplained" in got["jwksUrl"]
    assert got["url"] == ["host_changed"]
    assert v.sensitive(["routes", "JWKS-URI"]) and v.sensitive(["upstreamURL"])


def test_a_scheme_fix_that_keeps_the_host_is_not_a_security_change():
    rec = _record(FIXED.replace('allow_credentials: true', 'allow_credentials: false'))
    assert not [x for x in rec["findings"] if x["source"]["kind"] == "guard"]


def test_the_fixer_is_told_never_to_point_auth_or_upstream_at_a_mock(tmp_path):
    root, c, _ = _seeded(tmp_path)
    assert "Never point auth (issuer, jwks_url, audience) or an upstream at a mock, test" in \
        f.brief(c)


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
        return 3


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
        if "AS root" in q:
            return _Rows([{"id": params["r"], "root": "crun_a"}])
        if "workflow_runs" in q:
            return _Rows([])
        return _Rows(self.lineage)


def test_a_security_reviewers_high_on_a_p0_line_is_never_deferred():
    rec = _record(DRYRUN)
    p0 = next(x for x in rec["findings"] if x["id"] == "U-01")
    line = p0["location"]["target_line"]      # a reviewer cites the TARGET's line
    items = [{"text": f"{TARGET}:{line} — jwks_url points at a mock host anyone can mint for.",
              "tier": "P1", "severity": "high", "seat": "security"},
             {"text": f"{TARGET}:2 — owner string could be shorter.", "tier": "P2",
              "severity": "minor", "seat": "security"}]
    lineage = [{"id": "crun_a", "title": "x", "created_at": 1, "status": "completed",
                "pull_request_url": "u", "finalize_ctx": {}, "state": {"validation": rec}}]
    p = rp.for_run(_Conn(lineage), "crun_a", head_sha="h", change=11,
                   followups={"items": items, "bar": "standard", "queued": True,
                              "handled_by": "queued"})
    r1, r2 = (next(x for x in p["findings"] if x["id"] == i) for i in ("R-1", "R-2"))
    assert r1.get("blocks") is True and "deferred_to" not in r1
    assert "security_review:R-1" in p["verdict"]["reasons"]
    assert r2["deferred_to"]["by"] == "queued"
    assert p["counts"]["review"]["blocking"] == 1


def test_the_security_checks_own_high_as_the_dry_run_carried_it_blocks_too():
    # The real producer (checks_failing.followup_findings): no seat, severity "security", the
    # level and the path:line inside the text. The dry run showed it "deferred → follow-up".
    rec = _record(DRYRUN)
    line = next(x for x in rec["findings"] if x["id"] == "U-01")["location"]["target_line"]
    items = [{"text": f"(security_check) HIGH — {TARGET}:{line} — The route trusts the staging "
                      "mock's JWKS.", "tier": "P1", "severity": "security"},
             {"text": "(security_check) LOW — README.md:3 — wording.", "tier": "P2",
              "severity": "security"}]
    lineage = [{"id": "crun_a", "title": "x", "created_at": 1, "status": "completed",
                "pull_request_url": "u", "finalize_ctx": {}, "state": {"validation": rec}}]
    p = rp.for_run(_Conn(lineage), "crun_a", head_sha="h", change=11,
                   followups={"items": items, "bar": "standard", "queued": True,
                              "handled_by": "queued"})
    r1, r2 = (next(x for x in p["findings"] if x["id"] == i) for i in ("R-1", "R-2"))
    assert r1.get("blocks") is True and "security_review:R-1" in p["verdict"]["reasons"]
    assert not r2.get("blocks") and r2["deferred_to"]["by"] == "queued"


# ── #3 absent from the after-run → fixed, attributed; "exit 0 · 2 open" cannot happen ─────────

def test_a_finding_the_after_run_no_longer_reports_is_never_left_open():
    rec = _record(DRYRUN)
    by = _by_rule(rec)
    rate = by["rate-limit-required"][0]
    assert rate["status"] == "fixed" and rate["fixed_by"] == "block" and rate["hunk_id"]
    # Not "(removed)": the route still exists and now carries a rate limit.
    assert rate["fixed"] == "    rate_limit:\n      rps: 10\n      burst: 10"
    size21 = next(x for x in by["request-size-capped"] if x["location"]["partner_line"] == 21)
    assert size21["status"] == "fixed" and size21["fixed_by"] == "removed_with_block"
    assert size21["fixed"] is None
    validator_open = [x for x in rec["findings"] if x["status"] == "open"
                      and x["source"]["kind"] == "validator" and not x.get("held")
                      and not x.get("blocked_by")]
    assert rec["after"]["exit"] == 0 and validator_open == []
    p = rp.build_packet(work={"title": "t"}, head_sha="x", validations=[rec])
    assert rp.validate_packet(p) == []


# ── #11 a removed line's location ──────────────────────────────────────────────────────────────

def test_a_finding_on_a_removed_line_points_at_the_partners_line():
    rec = _record(DRYRUN)
    admin = _by_rule(rec)["no-sensitive-paths-exposed"][0]
    assert admin["location"]["line"] == 21 and admin["location"]["line_from"] == "partner"


# ── #5 masking by value shape and real secret keys ─────────────────────────────────────────────

def test_allow_credentials_true_or_false_is_not_a_secret_and_a_live_key_still_is():
    assert redact_secrets("      allow_credentials: true") == "      allow_credentials: true"
    assert redact_secrets("allow_credentials: false") == "allow_credentials: false"
    assert redact_secrets("withCredentials: true") == "withCredentials: true"
    assert redact_secrets('      api_key: "sk_live_4242424242424242"') == \
        '      api_key: "[SECRET]"'
    assert redact_secrets("password: hunter2hunter2") == "password: [SECRET]"
    assert redact_secrets('credentials: "abcdefgh1234"') == 'credentials: "[SECRET]"'
    # A flag's NAME never unmasks a value: only a boolean / null is kept.
    assert redact_secrets("allow_credentials: hunter2hunter2") == "allow_credentials: [SECRET]"
    rec = _record(DRYRUN)
    cors = _by_rule(rec)["cors-no-wildcard-with-credentials"][0]
    assert cors["original"] == "      allow_credentials: true"
    assert "allow_credentials: false" in json.dumps(rec["fixes"])


# ── #8 the packet never claims a head its evidence did not run on ──────────────────────────────

def test_a_packet_whose_evidence_ran_on_an_older_head_is_stale_and_rechecking():
    rec = _record(FIXED, after_raw=[])
    rec["after"]["head_sha"] = "1c1e2c8"
    p = rp.build_packet(work={"title": "t"}, head_sha="25e7f99", validations=[rec],
                        evidence_head=rp.evidence_head(rec))
    assert p["head_sha"] == "1c1e2c8" and p["branch_head"] == "25e7f99" and p["stale"] is True
    assert "stale_evidence" in p["verdict"]["reasons"]
    code, _ = rp.blocks_merge({"packet": p}, force=False)
    assert code == "packet_stale"
    p["rerun"] = {"state": "pending", "created_at": rp._now()}
    p = rp.finalize({k: x for k, x in p.items() if k != "verdict"})
    code, words = rp.blocks_merge({"packet": p}, force=True)
    assert code == "rerun_pending" and "stale → re-checking" in words
    ok = rp.build_packet(work={"title": "t"}, head_sha="1c1e2c8", validations=[rec],
                         evidence_head="1c1e2c8")
    assert ok["stale"] is False and rp.blocks_merge({"packet": ok}, force=False,
                                                    current_head="25e7f99")[0] == "packet_stale"


def test_no_checkpoint_commits_land_on_a_change_up_for_merge():
    import inspect

    from boss_engine.services import governed_code_task_service as g

    src = inspect.getsource(g)
    i = src.index("def _checkpoint(label: str) -> int:")
    body = src[i:i + 3000]
    # Only a validation build skips them; every other build checkpoints (crash / pause resume).
    assert body.index("if _validation_build[0] and _branch_up_for_merge():") \
        < body.index("commit_change(")
    assert "_validation_build[0] = True" in src
    assert "pull_request_url IS NOT NULL" in src and "FROM local_change_sets WHERE project_id" in src
