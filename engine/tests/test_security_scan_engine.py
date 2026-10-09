"""The security scan engine without a database (APP_TEMPLATES_AND_SECURITY_LOOP.md §4.9 "Unit").

Fixtures are REAL tool output (engine/tests/fixtures/security_scan/), produced by the pinned
toolchain-security stage (Semgrep 1.180.0 + the pinned rule packs, OSV-Scanner 2.6.0, ZAP 2.17.0
baseline) over tests/e2e/security/vulnapp (before) and vulnapp-fixed (after). Semgrep's SARIF is
trimmed to the rules its results reference; nothing else is edited.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from boss_engine.security_scan import fingerprints as fp
from boss_engine.security_scan import parsers, redact, rulepack, runners, scope
from boss_engine.security_scan.findings import (MAX_EVIDENCE_BYTES, MAX_FINDINGS, Finding, ParseError,
                                                cvss_severity)

ROOT = Path(__file__).resolve().parents[2]
FIX = ROOT / "engine" / "tests" / "fixtures" / "security_scan"
APP = ROOT / "tests" / "e2e" / "security" / "vulnapp"
APP_FIXED = ROOT / "tests" / "e2e" / "security" / "vulnapp-fixed"


def _semgrep(name: str, root: Path):
    return parsers.parse_semgrep_sarif((FIX / f"{name}.semgrep.sarif").read_bytes(), root=root)[0]


def _osv(name: str):
    return parsers.parse_osv_json((FIX / f"{name}.osv.json").read_bytes(), root=Path("/src"))[0]


def _zap(name: str):
    return parsers.parse_zap_json((FIX / f"{name}.zap.json").read_bytes())[0]


# ---- parsers: the planted flaws come out as normalised findings --------------------------------

def test_semgrep_sarif_finds_the_sql_injection_and_the_hardcoded_secret():
    found = {f.rule_id: f for f in _semgrep("vulnapp", APP)}
    sqli = found["javascript.express.security.injection.tainted-sql-string"]
    assert (sqli.severity, sqli.cwe, sqli.confidence) == ("high", "CWE-89", "low")
    assert sqli.location["file"] == "server.js" and sqli.location["line"] == 29
    assert sqli.location["function"] == "findUser"
    assert "req.query.name" in sqli.evidence["snippet"]
    jwt = found["javascript.jsonwebtoken.security.hardcoded-jwt-secret"]
    assert (jwt.cwe, jwt.location["function"]) == ("CWE-798", "issueToken")
    # the rule ids are the PACK's, not the image path's
    assert all(not f.rule_id.startswith("opt.") for f in found.values())


def test_osv_groups_aliases_into_one_finding_per_advisory():
    found = _osv("vulnapp")
    assert {f.rule_id for f in found} == {"GHSA-29mw-wpgm-hmr9", "GHSA-35jh-r3h4-6jhm", "GHSA-f23m-r3pf-42rh"}
    cmd = next(f for f in found if f.rule_id == "GHSA-35jh-r3h4-6jhm")
    assert cmd.severity == "high"                               # max CVSS 8.1
    assert cmd.location == {**cmd.location, "lockfile": "package-lock.json", "ecosystem": "npm",
                            "package": "lodash", "version": "4.17.20"}
    assert "GHSA-r5fr-rjxr-66jc" in cmd.evidence["aliases"] and cmd.evidence["fixed_in"] == ["4.17.21"]
    assert _osv("vulnapp-fixed") == []


def test_zap_baseline_reports_the_missing_headers_per_route_template():
    found = _zap("vulnapp")
    refs = {(f.rule_id, f.location["path"]) for f in found}
    assert {("10038-1", "/"), ("10020-1", "/"), ("10021", "/"), ("10037", "/")} <= refs
    assert all(f.location["method"] == "GET" for f in found)
    assert next(f for f in found if f.rule_id == "10020-1").cwe == "CWE-1021"
    # the fixed app sets the headers: those alerts are gone, the informational ones remain
    after = {f.rule_id for f in _zap("vulnapp-fixed")}
    assert not ({"10038-1", "10020-1", "10021", "10037"} & after)


@pytest.mark.parametrize("parse,raw", [
    (lambda r: parsers.parse_semgrep_sarif(r), b"not json"),
    (lambda r: parsers.parse_semgrep_sarif(r), b'{"version": "2.1.0"}'),
    (lambda r: parsers.parse_osv_json(r), b'{"results": 3}'),
    (lambda r: parsers.parse_zap_json(r), b'{"insights": []}'),
])
def test_output_a_tool_does_not_write_is_a_parse_error_never_zero_findings(parse, raw):
    with pytest.raises(ParseError):
        parse(raw)


def test_size_limits_raw_output_and_finding_count(monkeypatch):
    monkeypatch.setattr(parsers, "MAX_RAW_BYTES", 100)
    with pytest.raises(ParseError, match="limit"):
        parsers.parse_zap_json(b" " * 101)
    monkeypatch.undo()
    alerts = [{"alertRef": "1", "riskcode": "1", "confidence": "2", "name": "x", "cweid": "1",
               "instances": [{"uri": f"http://127.0.0.1:1/p{i}x", "method": "GET", "param": ""}]}
              for i in range(MAX_FINDINGS + 5)]
    found, meta = parsers.parse_zap_json(json.dumps({"site": [{"alerts": alerts}]}))
    assert len(found) == MAX_FINDINGS and meta["truncated"] is True


# ---- fingerprints ------------------------------------------------------------------------------

def test_fingerprints_survive_line_moves_between_the_before_and_after_scans():
    before = {f.rule_id: f for f in _semgrep("vulnapp", APP)}
    after = {f.rule_id: f for f in _semgrep("vulnapp-fixed", APP_FIXED)}
    rule = "javascript.express.security.audit.express-check-csurf-middleware-usage"
    assert before[rule].location["line"] != after[rule].location["line"]       # the line moved
    assert before[rule].fingerprint == after[rule].fingerprint
    zb = {(f.rule_id, f.location["path"], f.location["param"]): f.fingerprint for f in _zap("vulnapp")}
    za = {(f.rule_id, f.location["path"], f.location["param"]): f.fingerprint for f in _zap("vulnapp-fixed")}
    assert za and all(zb[k] == v for k, v in za.items())       # the remaining DAST findings match


def _finding(**kw) -> Finding:
    base = dict(tool="semgrep", rule_id="javascript.x.rule", title="t", severity="high",
                location={"file": "a.js", "line": 10, "function": "f"}, evidence={"snippet": "db.exec(q)"})
    base.update(kw)
    return Finding(**base)


def test_fingerprint_ignores_the_line_but_not_the_rule_the_file_or_the_function():
    a = fp.fingerprint(_finding())
    assert fp.fingerprint(_finding(location={"file": "a.js", "line": 99, "function": "f"})) == a
    assert fp.fingerprint(_finding(rule_id="javascript.y.rule")) != a
    assert fp.fingerprint(_finding(location={"file": "b.js", "line": 10, "function": "f"})) != a
    assert fp.fingerprint(_finding(location={"file": "a.js", "line": 10, "function": "g"})) != a
    assert fp.fingerprint(_finding(evidence={"snippet": "  db.exec(q)\n"})) == a       # whitespace
    assert fp.fingerprint(_finding(evidence={"snippet": "db.exec(other)"})) != a


def test_two_identical_findings_in_one_function_stay_two():
    pair = fp.assign([_finding(location={"file": "a.js", "line": 3, "function": "f"}),
                      _finding(location={"file": "a.js", "line": 9, "function": "f"})])
    assert pair[0].fingerprint != pair[1].fingerprint
    assert pair[0].location["snippet_key"] == pair[1].location["snippet_key"]


def test_dast_fingerprint_ignores_host_port_query_and_ids():
    a = _finding(tool="zap", rule_id="10021", location={"method": "GET", "param": "",
                                                       "path": fp.path_template("http://127.0.0.1:4000/users/42?x=1")})
    b = _finding(tool="zap", rule_id="10021", location={"method": "GET", "param": "",
                                                       "path": fp.path_template("http://127.0.0.1:5999/users/43")})
    assert a.location["path"] == "/users/{id}" and fp.fingerprint(a) == fp.fingerprint(b)


def test_dependency_fingerprint_ignores_the_version():
    a = _finding(tool="osv", rule_id="GHSA-1", location={"lockfile": "package-lock.json", "ecosystem": "npm",
                                                         "package": "lodash", "version": "4.17.20"})
    b = _finding(tool="osv", rule_id="GHSA-1", location={**a.location, "version": "4.17.19"})
    assert fp.fingerprint(a) == fp.fingerprint(b)


@pytest.mark.parametrize("lang,src,line,expected", [
    ("js", "const a = 1;\nfunction findUser(req, res) {\n  if (x) {\n    db.exec(q);\n  }\n}\nfoo();\n", 4, "findUser"),
    ("js", "function a() {\n}\nconst b = 2;\n", 3, "<module>"),
    ("js", "const handler = async (req, res) => {\n  run(req.body);\n};\n", 2, "handler"),
    ("js", "app.get('/users', (req, res) => {\n  run(req.query.x);\n});\n", 2, "app.get '/users'"),
    ("js", "class Repo {\n  find(name) {\n    return q(name);\n  }\n}\n", 3, "find"),
    ("python", "import os\n\ndef run(cmd):\n    if cmd:\n        os.system(cmd)\n\nx = 1\n", 5, "run"),
    ("python", "def a():\n    pass\n\nx = os.system(y)\n", 4, "<module>"),
    ("python", "class C:\n    def m(self):\n        return eval(self.x)\n", 3, "m"),
    ("java", "public class A {\n  public List<String> find(String n) throws Exception {\n    q(n);\n  }\n}\n", 3, "find"),
])
def test_enclosing_function(lang, src, line, expected):
    assert fp.enclosing_function(src, line, lang) == expected


# ---- redaction ---------------------------------------------------------------------------------

def test_a_hardcoded_secret_is_never_stored_and_its_fingerprint_is_not_a_hash_of_it():
    jwt = next(f for f in _semgrep("vulnapp", APP) if "hardcoded-jwt" in f.rule_id)
    raw = (FIX / "vulnapp.semgrep.sarif").read_text()
    assert "s4-proof-hardcoded-signing-key" in raw                   # the tool output has it
    assert "s4-proof-hardcoded-signing-key" not in json.dumps(jwt.evidence)
    rotated = _finding(rule_id=jwt.rule_id, cwe="CWE-798",
                       evidence=redact.redact_evidence({"snippet": 'jwt.sign(p, "another-secret")'},
                                                       rule_id=jwt.rule_id, cwe="CWE-798"))
    same = _finding(rule_id=jwt.rule_id, cwe="CWE-798",
                    evidence=redact.redact_evidence({"snippet": 'jwt.sign(p, "first-secret")'},
                                                    rule_id=jwt.rule_id, cwe="CWE-798"))
    assert fp.fingerprint(rotated) == fp.fingerprint(same)            # rotating the value is no fix


@pytest.mark.parametrize("text,leak", [
    ("Set-Cookie: session=abc123def456; HttpOnly", "abc123def456"),
    ("Cookie: sid=s3ss10nvalue; theme=dark", "s3ss10nvalue"),
    ("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJlLXZhbHVl", "c2lnbmF0dXJlLXZhbHVl"),
    ("key = AKIAABCDEFGHIJKLMNOP", "AKIAABCDEFGHIJKLMNOP"),
    ("token=ghp_abcdefghijklmnopqrstuvwxyz0123456789", "ghp_abcdefghijklmnopqrstuvwxyz0123456789"),
    ("GET /cb?code=4/0AY0e-g7secretcode&state=x", "4/0AY0e-g7secretcode"),
    ("stripe sk_live_4eC39HqLyjWDarjtT1zdp7dc here", "sk_live_4eC39HqLyjWDarjtT1zdp7dc"),
])
def test_scanner_evidence_shapes_are_redacted(text, leak):
    out = redact.redact_evidence({"evidence": text, "otherinfo": text})
    assert leak not in json.dumps(out)


def test_zap_url_with_a_credential_parameter_is_redacted_in_the_location():
    raw = json.dumps({"site": [{"alerts": [{"alertRef": "10024", "riskcode": "0", "confidence": "2",
                                            "name": "x", "cweid": "598", "instances": [
        {"uri": "http://127.0.0.1:9/login?password=hunter2hunter2", "method": "GET", "param": "password",
         "evidence": "password=hunter2hunter2"}]}]}]})
    f = parsers.parse_zap_json(raw)[0][0]
    assert "hunter2hunter2" not in json.dumps(f.location) + json.dumps(f.evidence)


def test_evidence_is_capped_at_4kb():
    out = redact.redact_evidence({"snippet": "x" * 20000, "message": "y" * 20000, "extra": "z" * 3000})
    assert len(json.dumps(out).encode()) <= MAX_EVIDENCE_BYTES and out["truncated"] is True


# ---- scope: the request alone (the database half is in the API suite) --------------------------

@pytest.mark.parametrize("target", ["http://evil.example/", "https://10.0.0.5:8443", "127.0.0.1:8080",
                                    "preview:", "preview:../../x", "deploy:prod", "host:shared-server",
                                    "file:///etc/passwd", "PREVIEW:abc"])
def test_a_free_target_is_refused(target):
    with pytest.raises(scope.ScopeRefused) as exc:
        scope.resolve_request(None, "p", "app", zap={"target": target})
    assert exc.value.code == "target_out_of_scope"


def test_unknown_tools_and_options_are_refused_and_order_is_fixed():
    with pytest.raises(scope.ScopeRefused, match="unknown tools"):
        scope.resolve_request(None, "p", "app", tools=["semgrep", "nmap"])
    with pytest.raises(scope.ScopeRefused):
        scope.resolve_request(None, "p", "app", zap={"url": "http://x"})
    with pytest.raises(scope.ScopeRefused):
        scope.resolve_request(None, "p", "app", zap={"mode": "attack"})
    req = scope.resolve_request(None, "p", "app", tools=["zap", "semgrep"])
    assert req.tools == ("semgrep", "zap") and req.zap_mode == "baseline"      # D12: passive default


# ---- runners: a failing tool is failed, never "0 findings" -------------------------------------

def _exec(returncode=0, raises=None, write=None):
    def run(argv, **kw):
        if raises:
            raise raises
        if write:
            path, data = write
            Path(path).write_bytes(data)
        return subprocess.CompletedProcess(argv, returncode, "", "boom")
    return run


def test_a_tool_timeout_is_a_failed_tool(tmp_path):
    res = runners.run_semgrep(tmp_path, tmp_path, exec_fn=_exec(raises=subprocess.TimeoutExpired("semgrep", 600)))
    assert res.status == "failed" and "timed out" in res.error and res.findings == []


def test_a_tool_error_exit_or_missing_output_is_a_failed_tool(tmp_path):
    assert runners.run_osv(tmp_path, tmp_path, exec_fn=_exec(returncode=127)).status == "failed"
    res = runners.run_semgrep(tmp_path, tmp_path, exec_fn=_exec(returncode=0))
    assert res.status == "failed" and "no output" in res.error


def test_osv_with_nothing_to_audit_is_done_with_a_note(tmp_path):
    res = runners.run_osv(tmp_path, tmp_path, exec_fn=_exec(returncode=128))
    assert res.status == "done" and "nothing to audit" in res.error


def test_semgrep_runs_offline_with_the_vendored_packs(tmp_path, monkeypatch):
    seen = {}

    def run(argv, **kw):
        seen["argv"] = argv
        Path(argv[argv.index("--output") + 1]).write_bytes((FIX / "vulnapp.semgrep.sarif").read_bytes())
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setenv(runners.RULES_DIR_ENV, str(tmp_path / "rules"))
    res = runners.run_semgrep(APP, tmp_path, exec_fn=run)
    assert res.status == "done" and len(res.findings) == 3
    argv = seen["argv"]
    assert argv[argv.index("--config") + 1] == str(tmp_path / "rules")
    assert "--metrics=off" in argv and not any(a.startswith("p/") or "semgrep.dev" in a for a in argv)


def test_zap_plan_scopes_to_the_target_and_is_passive_unless_asked(tmp_path):
    plan = runners.zap_plan("http://127.0.0.1:41000", tmp_path, mode="baseline", openapi=None, active_minutes=5)
    types = [j["type"] for j in plan["jobs"]]
    assert "activeScan" not in types and "openapi" not in types
    assert plan["env"]["contexts"][0]["includePaths"] == [r"http://127\.0\.0\.1:41000(/.*)?$"]
    api = runners.zap_plan("http://127.0.0.1:41000", tmp_path, mode="api", openapi=None, active_minutes=5)
    assert [j["type"] for j in api["jobs"]].count("activeScan") == 1


def test_tool_env_carries_no_credentials(monkeypatch, tmp_path):
    for k in ("ANTHROPIC_API_KEY", "GITHUB_TOKEN", "DATABASE_URL", "BOSS_MASTER_KEY", "OPENAI_API_KEY"):
        monkeypatch.setenv(k, "x")
    env = runners.tool_env(tmp_path)
    assert not {"ANTHROPIC_API_KEY", "GITHUB_TOKEN", "DATABASE_URL", "BOSS_MASTER_KEY", "OPENAI_API_KEY"} & set(env)
    assert "/opt/toolchains/bin" in env["PATH"].split(":")


# ---- the toolchain layer -----------------------------------------------------------------------

def test_the_security_layer_is_one_registry_entry_and_a_pinned_checksummed_stage():
    from boss_engine.services import toolchains

    assert "security" in toolchains.KNOWN
    docker = (ROOT / "Dockerfile.community").read_text()
    stage = docker.split("AS toolchains-security", 1)[1].split("\nFROM ", 1)[0]
    assert "FROM toolchains-java AS toolchains-security" in docker      # ZAP's JRE
    for needle in ("--require-hashes", "sha256sum -c -", "SEMGREP_RULES_DIGEST", "OSV_SCANNER_SHA256_ARM64",
                   "ZAP_SHA256", "rulepack.py select"):
        assert needle in stage
    lock = (ROOT / "engine/boss_engine/security_scan/semgrep-requirements.lock").read_text()
    pinned = [line for line in lock.splitlines() if "==" in line and not line.startswith("#")]
    assert pinned and all("--hash=sha256:" in lock.split(p, 1)[1].split("\n", 2)[1] for p in pinned)
    assert 'KNOWN_TOOLCHAINS=(java security)' in (ROOT / "scripts/install/boss.sh").read_text()
    from apps.egress.boss_egress import connect

    assert "api.osv.dev" in connect.DEFAULT_ALLOW                      # D6: OSV online


def test_rule_digest_is_over_content(tmp_path):
    (tmp_path / "javascript").mkdir()
    (tmp_path / "javascript" / "r.yaml").write_text("rules: []\n")
    (tmp_path / "javascript" / "r.js").write_text("// a test target, not a rule\n")
    (tmp_path / "javascript" / "r.test.yaml").write_text("x\n")
    d1 = rulepack.digest(tmp_path)
    (tmp_path / "javascript" / "r.js").write_text("changed\n")
    assert rulepack.digest(tmp_path) == d1
    (tmp_path / "javascript" / "r.yaml").write_text("rules: [1]\n")
    assert rulepack.digest(tmp_path) != d1


def test_cvss_bands():
    assert [cvss_severity(s) for s in ("9.8", "8.1", "5.3", "0.1", None)] == \
        ["critical", "high", "medium", "low", "medium"]
