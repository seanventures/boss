"""boss.validation/v1 (SPEC docs/design/validation-review §1.3, §2, §7 PR 1).

The fixtures are real: `gwlint validate --json` (before), `gwlint --fix` and the re-validate
(after) on a copy of Northgate's `examples/lumen-pay.bad.yaml` (Lumen Pay's partner file) saved as
`lumen-pay.yaml`; `ruff check --output-format json` on a small file; the security scanner's
Semgrep SARIF fixture. The ESLint fixture follows `eslint -f json`'s documented shape.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from boss_engine.services import input_fidelity as f
from boss_engine.services import validator_adapters as va
from boss_engine.services import validators as v

FIX = Path(__file__).parent / "fixtures" / "validation"
SARIF = Path(__file__).parent / "fixtures" / "security_scan" / "vulnapp.semgrep.sarif"
INPUT = "examples/lumen-pay.bad.yaml"
TARGET = "config/tenants/lumen-pay.yaml"
SPEC = {"remediate": {"input": INPUT, "output": TARGET}}

BEFORE = [("tenant-rate-limit-required", 1), ("pii-fields-declared", 5),
          ("rate-limit-required", 7), ("request-size-capped", 7), ("no-inlined-secrets", 17),
          ("cors-no-wildcard-with-credentials", 20), ("no-sensitive-paths-exposed", 21),
          ("request-size-capped", 21), ("upstream-https-required", 35)]
LEFT_OPEN = {"tenant-rate-limit-required", "pii-fields-declared", "no-inlined-secrets",
             "cors-no-wildcard-with-credentials", "no-sensitive-paths-exposed"}


def _repo(tmp_path: Path, target: str = "lumen-pay.fixed.yaml") -> Path:
    root = tmp_path / "northgate"
    (root / "examples").mkdir(parents=True)
    (root / "config" / "tenants").mkdir(parents=True)
    (root / ".boss").mkdir()
    shutil.copy(FIX / "lumen-pay.bad.yaml", root / INPUT)
    shutil.copy(FIX / "lumen-pay.good.yaml", root / "examples" / "lumen-pay.good.yaml")
    shutil.copy(FIX / "validators.yaml", root / ".boss" / "validators.yaml")
    return root


def _gwlint(root: Path, ran: list[str]):
    """gwlint, replayed from its real outputs: the input copy reports the 9, --fix writes the
    real fixed file, the target reports whatever is left on it."""
    def run(cmd: str):
        ran.append(cmd)
        if "--fix" in cmd:
            (root / cmd.split()[-1].strip("'")).write_text((FIX / "lumen-pay.fixed.yaml").read_text())
            return SimpleNamespace(stdout="", exit_code=1)
        path = root / cmd.split()[-2].strip("'")
        if path.read_text() == (FIX / "lumen-pay.bad.yaml").read_text():
            return SimpleNamespace(stdout=(FIX / "gwlint.before.json").read_text(), exit_code=1)
        if path.read_text() == (FIX / "lumen-pay.fixed.yaml").read_text():
            return SimpleNamespace(stdout=(FIX / "gwlint.after.json").read_text(), exit_code=1)
        return SimpleNamespace(stdout='{"path": "x", "violations": []}', exit_code=0)
    return run


def _validate(root: Path, target_text: str, ran=None):
    c = f.contract(SPEC, "", root)
    (root / TARGET).write_text(target_text)
    return c, f.run_validation(root, c, _gwlint(root, ran if ran is not None else []))


# ── (a) the 9 findings, ≥4 auto fixes, the 5 left open ─────────────────────────────────────────

def test_the_repo_declares_the_validator_in_validators_yaml(tmp_path):
    c = f.contract(SPEC, "", _repo(tmp_path))
    assert c["validator"] == {"name": "gwlint", "check": "go run ./cmd/gwlint validate {file} --json",
                              "fix": "go run ./cmd/gwlint --fix {file}", "adapter": "gwlint-json",
                              "rules_doc": "rules/api-guidelines.yaml", "ruleset": "northgate",
                              "source": ".boss/validators.yaml"}


def test_the_partner_file_gives_9_findings_4_auto_fixes_and_5_open(tmp_path):
    root = _repo(tmp_path)
    ran: list[str] = []
    _c, rec = _validate(root, (FIX / "lumen-pay.fixed.yaml").read_text(), ran)
    rec = f.public_validation(rec)
    assert rec["schema"] == "boss.validation/v1" and rec["error"] is None and rec["guard"] is None
    assert rec["validator"]["id"] == "gwlint" and rec["validator"]["adapter"] == "gwlint-json"
    assert rec["input"]["file"] == INPUT and rec["input"]["from"] == "partner"
    assert rec["input"]["sha256"] == v.sha256((FIX / "lumen-pay.bad.yaml").read_text())
    assert rec["target"] == TARGET
    by_id = {x["id"]: x for x in rec["findings"]}
    assert [(by_id[i]["source"]["rule_id"], by_id[i]["location"]["partner_line"])
            for i in rec["before"]["findings"]] == BEFORE
    assert rec["before"]["exit"] == 1
    # The copy carries the target's name, so gwlint's filename rule judges the real name.
    assert ran[0] == "go run ./cmd/gwlint validate .boss-validation/lumen-pay.yaml --json"
    auto = [x for x in rec["findings"] if x["fix_kind"] == "auto" and x["status"] == "fixed"]
    assert sorted((x["source"]["rule_id"], x["location"]["partner_line"]) for x in auto) == [
        ("rate-limit-required", 7), ("request-size-capped", 7), ("request-size-capped", 21),
        ("upstream-https-required", 35)]
    assert all(x["hunk_id"] and any(e.get("absent") for e in x["evidence"]) for x in auto)
    assert rec["after"]["exit"] == 1
    assert {by_id[i]["source"]["rule_id"] for i in rec["after"]["findings"]} == LEFT_OPEN
    assert {x["status"] for x in rec["findings"] if x["id"] in rec["after"]["findings"]} == {"open"}
    url = next(x for x in rec["fixes"] if x["after"] and "https://lumen-pay-core" in x["after"])
    assert url["kind"] == "auto" and url["rule"] == "upstream-https-required"
    assert url["before"].strip() == 'url: "http://lumen-pay-core.internal.example"'
    assert all({"finding", "rule", "file", "line", "before", "after", "kind", "hunk_id", "hunks",
                "lines"} <= set(x) for x in rec["fixes"])
    # One entry per finding (#4), never one per line.
    assert len({x["finding"] for x in rec["fixes"]}) == len(rec["fixes"])
    # Moved lines are followed: the secret on partner line 17 is line 21 of the target.
    secret = next(x for x in rec["findings"] if x["source"]["rule_id"] == "no-inlined-secrets")
    # One line basis (dry run 2, gap 11): `line` is the partner's, `target_line` the target's.
    assert secret["location"] == {"file": TARGET, "line": 17, "partner_line": 17,
                                  "target_line": 21}
    assert secret["source"]["rule_doc"] == "rules/api-guidelines.yaml#no-inlined-secrets"
    assert not (root / ".boss-validation").exists()


def test_a_finding_is_fixed_only_with_an_after_run_and_a_hunk(tmp_path):
    root = _repo(tmp_path)
    c = f.contract(SPEC, "", root)
    (root / TARGET).write_text((FIX / "lumen-pay.fixed.yaml").read_text())
    rec = f.run_validation(root, {**c, "validator": {**c["validator"], "check": None}},
                           _gwlint(root, []))
    assert rec["after"] is None and rec["findings"] == []


# ── (b) the input-substitution guard ───────────────────────────────────────────────────────────

def test_a_target_equal_to_the_good_file_fails_input_substituted(tmp_path):
    root = _repo(tmp_path)
    _c, rec = _validate(root, (FIX / "lumen-pay.good.yaml").read_text())
    assert rec["error"] == "input_substituted"
    assert rec["guard"] == {"code": "input_substituted", "source": "examples/lumen-pay.good.yaml",
                            "why": "copy_of_repo_file"}
    assert f.guard_violation(rec)["code"] == "input_substituted"


def test_a_target_left_as_it_was_before_the_build_is_substituted(tmp_path):
    root = _repo(tmp_path)
    (root / TARGET).write_text("tenant: lumen-pay\nroutes: []\n")
    c = f.contract(SPEC, "", root)
    rec = f.run_validation(root, c, _gwlint(root, []))
    assert rec["guard"]["why"] == "target_unchanged"


def test_an_input_changed_under_the_run_is_substituted(tmp_path):
    root = _repo(tmp_path)
    c = f.contract(SPEC, "", root)
    (root / INPUT).write_text((FIX / "lumen-pay.good.yaml").read_text())
    (root / TARGET).write_text((FIX / "lumen-pay.fixed.yaml").read_text())
    assert f.run_validation(root, c, _gwlint(root, []))["guard"]["why"] == "input_changed"


def test_the_attempt_fails_on_the_guard(tmp_path):
    from boss_engine.services import governed_code_task_service as g

    class NoLog:
        def connect(self):
            raise RuntimeError("no tool log")

    root = _repo(tmp_path)
    c = f.contract(SPEC, "", root)
    (root / TARGET).write_text((FIX / "lumen-pay.good.yaml").read_text())
    out = g._fidelity_check(NoLog(), "crun_x", root, c, None)
    assert out["ok"] is False
    assert "input_substituted" in [x["code"] for x in out["violations"]]
    assert out["_validation"]["error"] == "input_substituted"


# ── (c) the secret never reaches a stored record ───────────────────────────────────────────────

def test_the_live_key_never_reaches_the_record_or_the_verdict(tmp_path):
    root = _repo(tmp_path)
    raw_secret = next(line for line in (FIX / "lumen-pay.bad.yaml").read_text().splitlines()
                      if "api_key:" in line).split('"')[1]
    c, rec = _validate(root, (FIX / "lumen-pay.fixed.yaml").read_text().replace(
        f'      api_key: "{raw_secret}"\n', ""))
    stored = json.dumps([f.public_validation(rec), f.check(root, c, findings=[])])
    assert "sk_live_" not in stored and raw_secret[8:] not in stored
    secret = next(x for x in rec["findings"] if x["source"]["rule_id"] == "no-inlined-secrets")
    assert secret["original"] == '      api_key: "[SECRET]"'
    assert secret["status"] == "fixed" and secret["fixed"] is None and secret["fix_kind"] == "agent"


# ── (d) every adapter on its tool's real output ────────────────────────────────────────────────

def test_gwlint_json_adapter():
    out = va.parse("gwlint-json", (FIX / "gwlint.before.json").read_text(), "t.yaml")
    assert [(x["rule_id"], x["line"]) for x in out] == BEFORE
    assert out[1]["native_severity"] == "warn" and out[2]["fixable"] == "mechanical"
    assert va.parse("gwlint-json", "not json", "t.yaml") is None


def test_sarif_adapter_reuses_the_security_scanner():
    out = va.parse("sarif", SARIF.read_text(), "server.js")
    assert [(x["rule_id"].rsplit(".", 1)[-1], x["line"], x["native_severity"]) for x in out] == [
        ("express-check-csurf-middleware-usage", 11, "low"),
        ("tainted-sql-string", 29, "high"), ("hardcoded-jwt-secret", 34, "medium")]
    assert va.parse("sarif", '{"no": "runs"}', "x") is None


def test_eslint_json_adapter():
    out = va.parse("eslint-json", (FIX / "eslint.json").read_text(), "x.js")
    assert [(x["rule_id"], x["line"], x["native_severity"], x["fixable"]) for x in out] == [
        ("no-unused-vars", 3, "error", "never"), ("eqeqeq", 9, "warn", "never"),
        ("prefer-const", 12, "error", "mechanical")]
    assert all(x["file"] == "src/routes/payments.js" for x in out)


def test_ruff_json_adapter():
    out = va.parse("ruff-json", (FIX / "ruff.json").read_text(), "app.py")
    assert [(x["rule_id"], x["line"], x["fixable"]) for x in out] == [
        ("F401", 1, "mechanical"), ("F401", 2, "mechanical"), ("E711", 5, "never"),
        ("E741", 7, "never"), ("F841", 7, "never"), ("F821", 8, "never")]


def test_an_unknown_adapter_is_refused_and_none_means_the_generic_reader():
    with pytest.raises(KeyError):
        va.parse("junit-xml", "", "x")
    out = va.parse(None, (FIX / "gwlint.before.json").read_text(), "t.yaml")
    assert [(x["rule_id"], x["line"]) for x in out] == BEFORE


@pytest.mark.parametrize("text,error", [
    ("validators: {}", "needs a `validators:` list"),
    ("validators: [{id: x, applies_to: ['*.yaml']}]", "needs `id` and `run`"),
    ("validators: [{id: x, run: 'lint {file}'}]", "needs `applies_to`"),
    ("validators: [{id: x, applies_to: '*.y', run: 'lint'}]", "`run` needs {file}"),
    ("validators: [{id: x, applies_to: '*.y', run: 'l {file}', output: {adapter: tap}}]",
     "unknown adapter"),
])
def test_a_bad_validators_yaml_is_said(text, error):
    with pytest.raises(v.ValidatorsError, match=error.replace("{", r"\{").replace("}", r"\}")
                       .replace("`", "`").replace("[", r"\[")):
        v.parse(text)


def test_severity_is_the_rulesets_else_the_tools_word():
    rs = {"ruleset": "northgate", "version": 3, "rules": {"no-inlined-secrets": {"severity": "P0"}}}
    assert v.severity("no-inlined-secrets", "error", rs) == ("P0", "ruleset northgate v3")
    assert v.severity("pii-fields-declared", "warn", rs) == ("P2", "native:warn")
    assert v.severity("x", "error") == ("P1", "native:error")


# ── trust: the branch never picks its own validator or lowers its own P0 (review of #278) ─────

RULESET = """\
ruleset: northgate
version: 3
rules:
  no-inlined-secrets: {severity: P0, autofix: never}
"""


def test_the_build_reads_validator_and_ruleset_from_the_base_branch(tmp_path):
    root = _repo(tmp_path)
    (root / ".boss" / "rulesets").mkdir()
    (root / ".boss" / "rulesets" / "northgate.yaml").write_text(RULESET.replace("P0", "P3"))
    base = {".boss/validators.yaml": (FIX / "validators.yaml").read_text(),
            ".boss/rulesets/northgate.yaml": RULESET}
    c = f.contract(SPEC, "", root, trusted=base.get)
    assert c["ruleset"]["rules"]["no-inlined-secrets"]["severity"] == "P0"
    assert c["pending_config"] == [{"ruleset": "northgate", "sha": v.sha256(
        RULESET.replace("P0", "P3"))[:12], "version": 3, "applied": False}]
    # No declaration on the base: the branch's own validators.yaml is not used.
    assert f.contract(SPEC, "", root, trusted=lambda p: None)["validator"] is None


def test_a_ruleset_symlink_out_of_the_repo_is_not_read(tmp_path):
    from boss_engine.services import rulesets

    root = _repo(tmp_path)
    (root / ".boss" / "rulesets").mkdir()
    outside = tmp_path / "elsewhere.yaml"
    outside.write_text(RULESET)
    (root / ".boss" / "rulesets" / "northgate.yaml").symlink_to(outside)
    assert rulesets.load(root, "northgate") is None
    assert rulesets.load(root, "../elsewhere") is None


def test_a_parse_error_names_the_place_never_the_content():
    from boss_engine.services import rulesets

    with pytest.raises(rulesets.RulesetError) as err:
        rulesets.parse("ruleset: x\nrules: {a: [sk_live_" + "9" * 16 + "\n", path="r.yaml")
    assert "sk_live_" not in str(err.value) and "line" in str(err.value)
    with pytest.raises(v.ValidatorsError) as err:
        v.parse("validators: [{id: x, run: 'token=SECRETVALUE {file}'\n")
    assert "SECRETVALUE" not in str(err.value)
