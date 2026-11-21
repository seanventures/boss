"""A build that remediates an input file ships a minimal, evidenced edit of THAT file.

THE RUN (northgate-gateway, workflow api_onboarding v13, wfr_40229c5c, build crun_58aecc16): the
step said "validate examples/lumen-pay.bad.yaml with gwlint, --fix it, hand-edit the rest, write
config/tenants/lumen-pay.yaml". The shipped file was the repo's reference answer
examples/lumen-pay.good.yaml plus a refunds route the partner never sent (already on main from an
earlier build), under a comment header claiming "gwlint --fix applied" — no --fix ever ran. Tests
and review passed. The build's learned note stored `cp examples/lumen-pay.good.yaml …` as a
reusable workaround.

The class (input_fidelity): the contract (declared or read from the brief), a structural diff
input → output, copy detection, claims backed by the tool log, a held gate, the validator's
record on the step, and no copied-answer lessons.
"""
from __future__ import annotations

import inspect
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from boss_engine.services import checks_failing, input_fidelity as f
from boss_engine.services import governed_code_task_service as g
from boss_engine.services import quality_bar
from boss_engine.services import workflow_capability_runner as wcr
from boss_engine.services.knowledge import learning_writeback as lw

BAD = """\
tenant: lumen-pay
owner: "Lumen Pay Platform Team"
contact: "platform@lumenpay.example"
tier: standard
data_classification: pii
routes:
  - path: /v1/payments
    description: "List recent payments for the authenticated merchant."
    methods: ["GET"]
    upstream: lumen-pay-core
    auth:
      type: jwt
      issuer: "https://issuer.lumenpay.example"
      audience: "northgate"
      scopes: ["payments.read"]
      jwks_url: "https://issuer.lumenpay.example/.well-known/jwks.json"
      api_key: "sk_live_4242424242424242"
    cors:
      allow_origins: ["*"]
      allow_credentials: true
  - path: /v1/admin/accounts
    description: "Internal account admin surface, never meant for tenants."
    methods: ["GET"]
    upstream: lumen-pay-core
    auth:
      type: jwt
      issuer: "https://issuer.lumenpay.example"
      audience: "northgate"
      scopes: ["admin.read"]
      jwks_url: "https://issuer.lumenpay.example/.well-known/jwks.json"
    rate_limit:
      rps: 10
      burst: 10
upstreams:
  lumen-pay-core:
    url: "http://lumen-pay-core.internal.example"
    timeout_ms: 2000
"""

GOOD = """\
tenant: lumen-pay
owner: "Lumen Pay Platform Team"
contact: "platform@lumenpay.example"
tier: standard
data_classification: pii
pii_fields: ["merchant_id", "customer_email"]
rate_limit:
  rps: 50
  burst: 50
routes:
  - path: /v1/payments
    description: "List recent payments for the authenticated merchant."
    methods: ["GET"]
    upstream: lumen-pay-core
    max_body_bytes: 65536
    auth:
      type: jwt
      issuer: "https://issuer.lumenpay.example"
      audience: "northgate"
      scopes: ["payments.read"]
      jwks_url: "https://issuer.lumenpay.example/.well-known/jwks.json"
    rate_limit:
      rps: 10
      burst: 10
    cors:
      allow_origins: ["https://app.lumenpay.example"]
      allow_credentials: false
upstreams:
  lumen-pay-core:
    url: "https://lumen-pay-core.internal.example"
    timeout_ms: 2000
"""

REFUNDS = """\
  - path: /v1/payments/refunds
    description: "Issue a refund for a prior payment."
    methods: ["POST"]
    upstream: lumen-pay-core
    max_body_bytes: 65536
    auth:
      type: jwt
      issuer: "https://issuer.lumenpay.example"
      audience: "northgate"
      scopes: ["payments.write"]
      jwks_url: "https://issuer.lumenpay.example/.well-known/jwks.json"
    rate_limit:
      rps: 5
      burst: 5
"""

#: The shipped file: the answer, plus a route the partner never sent.
SHIPPED = GOOD.replace("upstreams:\n", REFUNDS + "upstreams:\n")

#: An honest remediation: --fix applied to a copy of the input, then the never-fixable edits.
MINIMAL = """\
tenant: lumen-pay
owner: "Lumen Pay Platform Team"
contact: "platform@lumenpay.example"
tier: standard
data_classification: pii
pii_fields: ["merchant_id"]
rate_limit:
  rps: 40
  burst: 40
routes:
  - path: /v1/payments
    max_body_bytes: 1048576
    rate_limit:
      rps: 10
      burst: 10
    description: "List recent payments for the authenticated merchant."
    methods: ["GET"]
    upstream: lumen-pay-core
    auth:
      type: jwt
      issuer: "https://issuer.lumenpay.example"
      audience: "northgate"
      scopes: ["payments.read"]
      jwks_url: "https://issuer.lumenpay.example/.well-known/jwks.json"
      api_key_hashed: "sha256:9f2c"
    cors:
      allow_origins: ["https://app.lumenpay.example"]
      allow_credentials: false
upstreams:
  lumen-pay-core:
    url: "https://lumen-pay-core.internal.example"
    timeout_ms: 2000
"""

#: gwlint's real report on the bad file (`gwlint validate --json`), the fields BOSS reads.
_FINDINGS = [
    ("tenant-rate-limit-required", "error", 1, "never", "Every tenant must declare a tenant-level "
     "rate limit within bounds", "Add a top-level rate_limit.rps and rate_limit.burst for the "
     "tenant (e.g. rps: 50, burst: 50)."),
    ("pii-fields-declared", "warn", 5, "never", "Tenants handling PII should declare which fields "
     "to redact", "Add pii_fields listing the request fields (e.g. query parameters) that carry "
     "personal data for this tenant."),
    ("rate-limit-required", "error", 7, "mechanical", "Every route must declare a rate limit "
     "within bounds", "Add rate_limit.rps and rate_limit.burst (e.g. rps: 10, burst: 10)."),
    ("request-size-capped", "error", 7, "mechanical", "Every route must declare a request body "
     "size cap", "Add max_body_bytes to the route, e.g. max_body_bytes: 1048576."),
    ("no-inlined-secrets", "error", 17, "never", "No API keys, tokens or passwords inlined in "
     "plaintext", "Remove the plaintext value; store a hash (api_key_hashed) or reference a "
     "secret manager entry instead."),
    ("cors-no-wildcard-with-credentials", "error", 20, "never", "CORS must not combine a wildcard "
     "origin with credentials", "List explicit allowed origins, or set allow_credentials: false."),
    ("no-sensitive-paths-exposed", "error", 21, "never", "Routes must not expose /admin, /internal "
     "or /debug paths", "Remove the route, or move the admin/internal surface behind a separate, "
     "non-tenant-facing deployment."),
    ("request-size-capped", "error", 21, "mechanical", "Every route must declare a request body "
     "size cap", "Add max_body_bytes to the route, e.g. max_body_bytes: 1048576."),
    ("upstream-https-required", "error", 35, "mechanical", "Upstream URLs must use https://",
     "Change the upstream url to https://."),
]
REPORT_BAD = json.dumps({"path": "examples/lumen-pay.bad.yaml", "violations": [
    {"rule_id": r, "severity": s, "line": ln, "fixable": fx, "title": t, "fix": fix,
     "why": "…"} for r, s, ln, fx, t, fix in _FINDINGS]})
REPORT_CLEAN = json.dumps({"path": "config/tenants/lumen-pay.yaml", "violations": []})

#: The brief as the workflow step wrote it (api_onboarding v13, trimmed).
BRIEF = (
    "Run `gwlint validate --json examples/lumen-pay.bad.yaml` and record the full report. Then "
    "run `gwlint --fix examples/lumen-pay.bad.yaml` on a working copy to apply the mechanically-"
    "fixable rules. For the violations gwlint reports as fixable:\"never\" make these edits by "
    "hand, using the repo's own existing conforming config for this tenant, "
    "examples/lumen-pay.good.yaml, as your reference for the right shape. Write the result to "
    "config/tenants/lumen-pay.yaml (the path the gateway hot-reloads).")


def _repo(tmp_path: Path, *, existing_output: str | None = None) -> Path:
    (tmp_path / "examples").mkdir()
    (tmp_path / "config" / "tenants").mkdir(parents=True)
    (tmp_path / "cmd" / "gwlint").mkdir(parents=True)
    (tmp_path / "go.mod").write_text("module northgate\n")
    (tmp_path / "examples" / "lumen-pay.bad.yaml").write_text(BAD)
    (tmp_path / "examples" / "lumen-pay.good.yaml").write_text(GOOD)
    if existing_output is not None:
        (tmp_path / "config" / "tenants" / "lumen-pay.yaml").write_text(existing_output)
    return tmp_path


def _declared(root: Path) -> dict:
    return f.contract({"remediate": {
        "input": "examples/lumen-pay.bad.yaml", "output": "config/tenants/lumen-pay.yaml"}},
        "", root)


def _write(root: Path, text: str) -> None:
    (root / "config" / "tenants" / "lumen-pay.yaml").write_text(text)


def _findings() -> list[dict]:
    return f.parse_findings(REPORT_BAD, "examples/lumen-pay.bad.yaml")


def _codes(result: dict) -> list[str]:
    return [v["code"] for v in result["violations"]]


# ── the structural diff ────────────────────────────────────────────────────────────────────────

def test_a_route_the_input_never_had_is_an_added_block():
    before = f.parse(BAD, "x.yaml")
    after = f.parse(SHIPPED, "x.yaml")
    added = [c for c in f.structural_diff(before, after) if c["op"] == "added"]
    assert {"op": "added", "kind": "item", "path": "routes[path=/v1/payments/refunds]",
            "key": "path", "identity": "/v1/payments/refunds"}.items() <= next(
        c for c in added if c["kind"] == "item").items()
    removed = [c["path"] for c in f.structural_diff(before, after) if c["op"] == "removed"]
    assert "routes[path=/v1/admin/accounts]" in removed


def test_an_added_subtree_is_one_change_and_scalar_lists_are_values():
    changes = f.structural_diff({"a": {"b": 1}, "m": ["GET"]}, {"a": {"b": 1, "c": {"d": 2}},
                                                                "m": ["GET", "POST"]})
    assert [c["path"] for c in changes] == ["a.c", "m"]
    assert changes[1]["op"] == "changed"


def test_an_added_route_fails_the_check(tmp_path):
    root = _repo(tmp_path)
    c = _declared(root)
    _write(root, MINIMAL.replace("upstreams:\n", REFUNDS + "upstreams:\n"))
    result = f.check(root, c, findings=_findings())
    assert result["ok"] is False
    assert _codes(result) == ["added_block"]
    assert "routes[path=/v1/payments/refunds]" in result["violations"][0]["message"]


def test_a_minimal_fix_passes(tmp_path):
    root = _repo(tmp_path)
    c = _declared(root)
    _write(root, MINIMAL)
    result = f.check(root, c, findings=_findings(), claims=["Applied `gwlint --fix` then edited"],
                     executed=["cp examples/lumen-pay.bad.yaml /tmp/t.yaml && "
                               "go run ./cmd/gwlint --fix /tmp/t.yaml"])
    assert result["ok"] is True, result["violations"]


def test_a_key_no_finding_asks_for_fails_once_findings_are_known(tmp_path):
    root = _repo(tmp_path)
    c = _declared(root)
    _write(root, MINIMAL.replace("tier: standard\n", "tier: standard\nbilling_plan: gold\n"))
    assert _codes(f.check(root, c, findings=_findings())) == ["added_key"]
    # Without the validator's findings an added key is not judged (only added blocks are).
    assert f.check(root, c, findings=None)["ok"] is True


# ── copies ─────────────────────────────────────────────────────────────────────────────────────

def test_a_byte_copy_of_the_sibling_good_file_is_a_copied_answer(tmp_path):
    root = _repo(tmp_path)
    c = _declared(root)
    assert c["answer_files"] == ["examples/lumen-pay.good.yaml"]   # found as a sibling
    _write(root, GOOD)
    result = f.check(root, c, findings=_findings())
    assert "copied_answer" in _codes(result)
    assert result["violations"][0]["source"] == "examples/lumen-pay.good.yaml"


def test_the_good_files_data_under_new_comments_is_still_a_copy(tmp_path):
    root = _repo(tmp_path)
    c = _declared(root)
    _write(root, "# derived from examples/lumen-pay.bad.yaml\n" + GOOD.replace('"', ""))
    assert "copied_answer" in _codes(f.check(root, c, findings=_findings()))


def test_a_byte_copy_of_any_other_data_file_is_a_copy(tmp_path):
    root = _repo(tmp_path)
    (root / "fixtures").mkdir()
    (root / "fixtures" / "tenant-sample.yaml").write_text(MINIMAL)
    c = _declared(root)
    _write(root, MINIMAL)
    assert _codes(f.check(root, c, findings=_findings())) == ["copied_file"]


def test_the_real_run_a_preexisting_output_left_as_it_was(tmp_path):
    """crun_58aecc16: config/tenants/lumen-pay.yaml was already on main; the build added only a
    comment header claiming --fix ran. Unchanged data, an added route, an unbacked claim."""
    root = _repo(tmp_path, existing_output=SHIPPED)
    c = _declared(root)
    _write(root, "# Derived from examples/lumen-pay.bad.yaml: gwlint --fix applied, then the\n"
                 "# never-fixable rules edited by hand.\n" + SHIPPED)
    result = f.check(root, c, findings=_findings(), claims=[],
                     executed=["go build -o /tmp/gwlint_check ./cmd/gwlint && "
                               "/tmp/gwlint_check validate --json config/tenants/lumen-pay.yaml"])
    codes = _codes(result)
    assert {"unchanged_preexisting", "added_block", "unbacked_claim"} <= set(codes)
    evidence = f.repair_evidence(result)
    assert "examples/lumen-pay.good.yaml" in evidence and "gwlint --fix" in evidence


# ── claims ─────────────────────────────────────────────────────────────────────────────────────

def test_a_claim_is_backed_only_by_a_command_that_ran():
    said = ["`gwlint validate --json config/tenants/lumen-pay.yaml` returns no violations.",
            "gwlint --fix applied to the bad file."]
    ran = ["/tmp/gwlint_check validate --json config/tenants/lumen-pay.yaml"]
    assert f.unbacked_claims(said, ran) == ["gwlint --fix"]
    assert f.unbacked_claims(said, ran + ["go run ./cmd/gwlint --fix /tmp/t.yaml"]) == []
    # Prose with no command, and a command quoted without a claim verb, are not claims.
    assert f.claims_in(["The brief asks for `gwlint --fix x.yaml` later"]) == []


def test_no_tool_log_means_claims_are_not_judged(tmp_path):
    root = _repo(tmp_path)
    c = _declared(root)
    _write(root, "# gwlint --fix applied\n" + MINIMAL)
    assert f.check(root, c, findings=_findings(), executed=None)["ok"] is True
    assert _codes(f.check(root, c, findings=_findings(), executed=[])) == ["unbacked_claim"]


# ── the contract ───────────────────────────────────────────────────────────────────────────────

def test_the_brief_declares_the_remediation_without_a_spec_key(tmp_path):
    root = _repo(tmp_path)
    c = f.contract({}, BRIEF, root)
    assert f.public(c) == {
        "input": "examples/lumen-pay.bad.yaml", "output": "config/tenants/lumen-pay.yaml",
        "answer_files": ["examples/lumen-pay.good.yaml"], "source": "brief",
        "validator": {"name": "gwlint",
                      "check": "go run ./cmd/gwlint validate --json '{file}'",
                      "fix": "go run ./cmd/gwlint --fix '{file}'"}}
    assert c["_input_text"] == BAD and c["_output_base"] is None
    note = f.brief(c)
    assert "`examples/lumen-pay.good.yaml`" in note and "do not open, read, copy" in note


def test_a_build_that_remediates_nothing_has_no_contract(tmp_path):
    root = _repo(tmp_path)
    assert f.contract({}, "Add a /healthz endpoint to the gateway.", root) is None
    # Two plausible inputs and no marker: not guessed.
    (root / "a.yaml").write_text("x: 1\n")
    (root / "b.yaml").write_text("y: 1\n")
    assert f.contract({}, "Fix a.yaml and b.yaml.", root) is None


def test_answer_paths_are_named_generically():
    for p in ("examples/acme.good.yaml", "fixtures/golden/out.json", "case.expected.yml",
              "x_reference.yaml"):
        assert f.is_answer_path(p), p
    for p in ("examples/acme.bad.yaml", "config/tenants/acme.yaml", "good.go", "invalid.yaml"):
        assert not f.is_answer_path(p), p


# ── no false positives on ordinary builds (review of #262) ─────────────────────────────────────

ROUTES = "routes:\n  - path: /\n    to: home\n"


def test_an_ordinary_config_fix_is_not_a_remediation(tmp_path):
    """"Fix the 404 on /health: add a /health route to config/routes.yaml" adds a route on
    purpose. No validator on the input, no answer sibling, no separate output: no contract."""
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "routes.yaml").write_text(ROUTES)
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / ".github" / "workflows" / "ci.yml").write_text("jobs: {}\n")
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")
    for brief in ("Fix the 404 on /health: add a /health route to config/routes.yaml.",
                  # A bare backticked path is the file, never a validator command.
                  "Fix the 404: add a /health route to `config/routes.yaml`",
                  "Fix the 404: add a /health route to `./config/routes.yaml`",
                  "Fix CI: add a lint step to .github/workflows/ci.yml.",
                  "Repair docker-compose.yml so the api waits for the db."):
        assert f.contract({}, brief, tmp_path) is None, brief


def test_an_inferred_contract_judges_additions_only_against_findings(tmp_path):
    root = _repo(tmp_path)
    (root / "examples" / "lumen-pay.good.yaml").unlink()     # no answer: added routes only
    brief = "Fix examples/lumen-pay.bad.yaml and write the result to config/tenants/lumen-pay.yaml."
    c = f.contract({}, brief, root)
    assert c and c["source"] == "brief" and c["validator"] is None
    _write(root, MINIMAL.replace("upstreams:\n", REFUNDS + "upstreams:\n"))
    advisory = f.check(root, c, findings=None)
    assert advisory["ok"] is True and [a["code"] for a in advisory["advisories"]] == ["added_block"]
    assert _codes(f.check(root, c, findings=_findings())) == ["added_block"]   # validator ran


K8S = """\
apiVersion: v1
kind: Service
metadata: {name: api}
---
apiVersion: apps/v1
kind: Deployment
metadata: {name: api}
spec:
  template:
    spec:
      containers:
        - name: api
          image: api:1.0
"""


def test_a_multi_document_manifest_is_diffed_per_document(tmp_path):
    (tmp_path / "k8s").mkdir()
    (tmp_path / "k8s" / "app.yaml").write_text(K8S)
    c = f.contract({"remediate": {"input": "k8s/app.yaml"}}, "", tmp_path)
    (tmp_path / "k8s" / "app.yaml").write_text(K8S.replace("api:1.0", "api:1.0.1"))
    result = f.check(tmp_path, c)
    assert result["ok"] is True, result["violations"]
    assert [ch["path"] for ch in result["changes"]] == [
        "[1].spec.template.spec.containers[name=api].image"]


def test_an_input_that_does_not_parse_skips_the_structure_never_fails(tmp_path):
    helm = "replicas: {{ .Values.replicas }}\nimage: {{ .Values.image }}\n"
    (tmp_path / "chart.yaml").write_text(helm)
    c = f.contract({"remediate": {"input": "chart.yaml"}}, "", tmp_path)
    (tmp_path / "chart.yaml").write_text(helm + "resources: {{ .Values.resources }}\n")
    result = f.check(tmp_path, c, executed=[])
    assert result["ok"] is True and "structural checks were skipped" in result["notes"][0]


def test_a_comment_the_input_already_had_is_not_the_builds_claim(tmp_path):
    root = _repo(tmp_path)
    c = _declared(root)
    c["_input_text"] = "# gwlint --fix applied by the partner's CI\n" + BAD
    _write(root, "# gwlint --fix applied by the partner's CI\n" + MINIMAL)
    assert f.check(root, c, findings=_findings(), executed=[])["ok"] is True
    _write(root, "# gwlint --fix applied by BOSS\n" + MINIMAL)
    assert _codes(f.check(root, c, findings=_findings(), executed=[])) == ["unbacked_claim"]


def test_a_fix_run_through_make_backs_the_claim(tmp_path):
    root = _repo(tmp_path)
    (root / "Makefile").write_text("fix-tenant:\n\tgo run ./cmd/gwlint --fix $(FILE)\n")
    assert f.unbacked_claims(["gwlint --fix applied"], ["make fix-tenant FILE=/tmp/t.yaml"],
                             root=root) == []
    (root / "scripts").mkdir()
    (root / "scripts" / "fix.sh").write_text("#!/bin/sh\ngwlint --fix \"$1\"\n")
    assert f.unbacked_claims(["gwlint --fix applied"], ["sh scripts/fix.sh /tmp/t.yaml"],
                             root=root) == []
    assert f.unbacked_claims(["gwlint --fix applied"], ["make test"], root=None) == \
        ["gwlint --fix"]


def test_a_fixer_run_on_a_copy_backs_a_claim_naming_the_input():
    said = ["Ran `gwlint --fix examples/lumen-pay.bad.yaml` and applied the result."]
    assert f.unbacked_claims(said, ["cp examples/lumen-pay.bad.yaml /tmp/t.yaml && "
                                    "gwlint --fix /tmp/t.yaml"]) == []


def test_a_capped_or_truncated_tool_log_leaves_claims_unknown(tmp_path):
    root = _repo(tmp_path)
    c = _declared(root)
    _write(root, "# gwlint --fix applied\n" + MINIMAL)
    capped = {"commands": ["ls"], "results": [], "complete": False}
    result = f.check(root, c, findings=_findings(), executed=capped)
    assert result["ok"] is True and "claims not judged" in result["notes"][0]


def test_the_fidelity_repairs_are_capped_and_then_say_exactly_what_to_do():
    from boss_engine.services import failure_owner as fo

    assert fo.MAX_ATTEMPTS == 2
    src = inspect.getsource(fo._own)
    capped = src.index("if made >= MAX_ATTEMPTS and cause_code in FIDELITY_CAUSES:")
    assert capped < src.index("        if made >= MAX_ATTEMPTS:\n            if cls == PLATFORM:")
    assert "action=FIDELITY_ACTION" in src[capped:capped + 600]
    assert "item.error or cause" in src[capped:capped + 900]
    assert "*.good.*" in fo.FIDELITY_ACTION and "edit of the named input file" in fo.FIDELITY_ACTION
    # The repair build's brief states the violations whole, not a 200-character cut.
    item = SimpleNamespace(error="Not a remediation of its input after 3 attempt(s). INPUT "
                                 "FIDELITY FAILED …\n- [copied_answer] " + "x" * 400)
    note = fo._fix_note(None, item, "short", "app:input_fidelity_failed")
    assert "- [copied_answer]" in note and "without reading or copying" in note
    assert len(note) > 400


# ── the validator's record (step output `validation`) ──────────────────────────────────────────

def test_the_validation_record_has_findings_before_fixes_and_after(tmp_path):
    root = _repo(tmp_path)
    c = f.contract({}, BRIEF, root)
    _write(root, MINIMAL)
    ran: list[str] = []

    def run(cmd):
        ran.append(cmd)
        if "--fix" in cmd:
            target = root / cmd.split()[-1].strip("'")
            target.write_text(target.read_text().replace("http://", "https://"))
            return SimpleNamespace(stdout="", exit_code=0)
        if "config/tenants" in cmd:
            return SimpleNamespace(stdout=REPORT_CLEAN, exit_code=0)
        return SimpleNamespace(stdout=REPORT_BAD, exit_code=1)

    rec = f.public_validation(f.run_validation(root, c, run))
    assert set(rec) >= {"validator", "command", "fix_command", "before", "fixes", "after"}
    assert rec["validator"] == "gwlint" and rec["error"] is None
    assert len(rec["before"]) == 9 and rec["after"] == []
    assert rec["before"][4] == {"rule": "no-inlined-secrets", "severity": "error",
                                "file": "examples/lumen-pay.bad.yaml", "line": 17,
                                "message": "No API keys, tokens or passwords inlined in "
                                           "plaintext", "fixable": "never"}
    by_after = {x["after"].strip(): x for x in rec["fixes"] if x["after"]}
    url = by_after['url: "https://lumen-pay-core.internal.example"']
    assert url["kind"] == "auto" and url["rule"] == "upstream-https-required"
    assert url["before"].strip() == 'url: "http://lumen-pay-core.internal.example"'
    assert by_after['api_key_hashed: "sha256:9f2c"']["kind"] == "manual"
    assert all(set(x) == {"rule", "file", "line", "before", "after", "kind", "op"}
               for x in rec["fixes"])
    # The input itself is never touched; the scratch copies are gone.
    assert (root / "examples" / "lumen-pay.bad.yaml").read_text() == BAD
    assert not (root / ".boss-validation").exists()
    assert ran[0].startswith("go run ./cmd/gwlint validate --json")


# ── the step: a violation goes to repair, the gate holds, the step carries the record ──────────

class _Engine:
    def connect(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, *_a, **_k):
        rows = [{"direction": "tool_call", "stage": "implement", "truncated": False,
                 "content": json.dumps({"command": "cat examples/lumen-pay.good.yaml"})}]
        return SimpleNamespace(mappings=lambda: SimpleNamespace(all=lambda: rows))


def test_one_attempts_check_fails_a_copied_answer_with_its_record(tmp_path):
    root = _repo(tmp_path)
    c = f.contract({}, BRIEF, root)
    _write(root, GOOD)
    sandbox = SimpleNamespace(run=lambda cmd, timeout=None: SimpleNamespace(
        stdout=REPORT_CLEAN if "config/tenants" in cmd else REPORT_BAD, exit_code=0))
    out = g._fidelity_check(_Engine(), "crun_x", root, c, sandbox,
                            summary="gwlint --fix applied", test_cmd="go test ./...")
    assert out["ok"] is False
    assert {"copied_answer", "unbacked_claim"} <= set(_codes(out))
    assert len(out["_validation"]["before"]) == 9


def test_the_implement_loop_sends_a_violation_to_repair_and_the_gate_holds():
    src = inspect.getsource(g._run_governed_code_task)
    loop = src[src.index("for attempt in range(max_repair + 2):"):
               src.index("# The implement budget ends with the stage")]
    # Every attempt is checked, and a violation is that attempt's failure ...
    assert "_fidelity = _fidelity_check(engine, run_id, root, _remediation, sandbox," in loop
    assert 'ok = ok and bool(_fidelity.get("ok"))' in loop
    assert loop.index('ok = ok and bool(_fidelity.get("ok"))') < loop.index(
        "if ok or attempt == max_repair + 1")
    # ... sent back with its exact reasons as the repair evidence.
    assert "_input_fidelity.repair_evidence(_fidelity)" in loop
    # The contract is taken before any agent runs, and every stage is told.
    assert src.index("_input_fidelity.contract(spec, instructions, root)") < src.index(
        "for attempt in range(max_repair + 2):")
    assert "_input_fidelity.brief(_remediation)" in src
    # The gate: judged, blocking at every bar, and never opened past.
    assert "gates[_input_fidelity.GATE] = bool(_fidelity.get(\"ok\"))" in src
    assert f.GATE in quality_bar.FLOOR
    assert f.GATE in checks_failing.HELD_GATES
    assert checks_failing.may_open([f.GATE]) is False


def test_still_failing_after_the_rounds_fails_the_build_coded_never_a_waiver_or_a_draft():
    """Agents own it: no waiver park, no draft PR. The build fails `input_fidelity_failed`
    before any review is spent, and failure_owner hands it to a new build as the app's fix."""
    src = inspect.getsource(g._run_governed_code_task)
    end_of_loop = src.index("# The implement budget ends with the stage")
    fail_at = src.index("return _fail_input_fidelity(engine, run_id, project_id, _fidelity,")
    assert end_of_loop < fail_at < src.index("_brief_items.assess(_items")
    assert fail_at < src.index("_review_gate_from_findings(")   # before review, PR or waiver

    calls: list = []

    class Conn:
        def execute(self, sql, params=None):
            calls.append((str(sql), params))

    class Eng:
        def begin(self):
            return self

        def __enter__(self):
            return Conn()

        def __exit__(self, *a):
            return False

    import boss_engine.services.governed_code_task_service as gm
    recorded = {}
    orig_fail, orig_tx = gm._fail, gm._transaction
    gm._fail = lambda c, run_id, pid, reason, **kw: recorded.update(reason=reason, **kw) or {
        "status": "failed", "done": True, **kw}
    gm._transaction = lambda engine: Eng()
    try:
        verdict = {"ok": False, "input": "examples/lumen-pay.bad.yaml",
                   "output": "config/tenants/lumen-pay.yaml",
                   "answer_files": ["examples/lumen-pay.good.yaml"], "violations": [
                       {"code": "copied_answer", "message": "copy of examples/lumen-pay.good.yaml"}]}
        out = gm._fail_input_fidelity(Eng(), "crun_x", "p", verdict, {"before": []}, attempts=3)
    finally:
        gm._fail, gm._transaction = orig_fail, orig_tx
    assert out["reason_code"] == "input_fidelity_failed" == g.INPUT_FIDELITY_FAILED
    assert recorded["failure_class"] == "terminal"
    assert "[copied_answer]" in recorded["reason"] and "after 3 attempt(s)" in recorded["reason"]
    assert any("{input_fidelity}" == (p or {}).get("k") for _s, p in calls)

    from boss_engine.services import failure_owner as fo

    # The words would otherwise read as a person's (a login, a budget): the code decides.
    cls, code, action = fo.classify("Budget exceeded; please sign in again",
                                    failure_class="terminal", reason_code=g.INPUT_FIDELITY_FAILED)
    assert (cls, code, action) == (fo.APP, "app:input_fidelity_failed", "")


def test_the_step_output_carries_the_validation_record():
    record = {"validator": "gwlint", "before": [{"rule": "no-inlined-secrets"}], "fixes": [],
              "after": []}
    verdict = {"ok": False, "violations": [{"code": "copied_answer"}]}

    class Conn:
        def begin_nested(self):
            return self

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params):
            text_ = str(sql)
            if "state->'validation'" in text_:
                row = {"validation": json.dumps(record), "fidelity": verdict}
            else:
                row = {"pull_request_url": "http://boss-local/o/r/pull/9",
                       "pull_request_number": 9, "checks_failing": None}
            return SimpleNamespace(mappings=lambda: SimpleNamespace(first=lambda: row))

    out = wcr._terminal_evidence(Conn(), "code_task", "crun_x")
    assert out["validation"] == record and out["input_fidelity"] == verdict
    assert out["pull_request_number"] == 9


@pytest.mark.parametrize("table", ["plain_words", "factory_view", "pr_description", "web"])
def test_the_gate_is_named_everywhere_gates_are(table):
    from boss_engine.services import factory_view, plain_words, pr_description

    if table == "plain_words":
        assert f.GATE in plain_words.FAILED_WORDS
    elif table == "factory_view":
        assert f.GATE in factory_view.GATE_LABELS and f.GATE in factory_view.GATE_NOUNS
    elif table == "pr_description":
        assert f.GATE in pr_description._GATE_WORDS
    else:
        web = Path(__file__).resolve().parents[2] / "apps" / "web" / "lib" / "plain-words.ts"
        assert web.read_text().count(f"{f.GATE}:") == 2


# ── lessons ────────────────────────────────────────────────────────────────────────────────────

def test_a_copied_answer_is_never_a_reusable_lesson():
    """kgn_5227123f: "Workaround: diff examples/lumen-pay.good.yaml: what worked after it failed"."""
    detail = {"problem": "`cp /sandbox/examples/lumen-pay.good.yaml ./t.yaml && go run "
                         "./cmd/gwlint --fix /tmp/t.yaml` failed: no such file",
              "workaround": "`cp examples/lumen-pay.good.yaml /tmp/t.yaml && go run "
                            "./cmd/gwlint --fix /tmp/t.yaml`"}
    assert lw.copied_answer_method("workaround", "diff examples/lumen-pay.good.yaml: what "
                                   "worked after it failed", "…", detail) == \
        "examples/lumen-pay.good.yaml"
    assert lw.copied_answer_method("fix", "Make go test pass", "use goldens/out.golden.json",
                                   {}) == "goldens/out.golden.json"
    # A normal workaround, and a finding that only mentions such a file, are kept.
    assert lw.copied_answer_method("workaround", "go test: what worked", "…",
                                   {"workaround": "`go vet ./... && go test ./...`"}) is None
    assert lw.copied_answer_method("review_finding", "x", "copied examples/a.good.yaml",
                                   {}) is None


def test_record_refuses_it_before_anything_is_written():
    with pytest.raises(lw.LearningRefused, match="reference answer"):
        lw.record(None, project_id="p", learning_kind="workaround",
                  title="diff examples/lumen-pay.good.yaml: what worked after it failed",
                  statement="`cp examples/lumen-pay.good.yaml /tmp/t.yaml` worked instead.",
                  detail={"problem": "x", "workaround": "`cp examples/lumen-pay.good.yaml t`"},
                  about={}, evidence=[{"type": "log", "ref": "r", "excerpt": "e"}],
                  confidence=0.3)
