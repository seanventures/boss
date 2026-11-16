"""THE ACCEPTANCE CITATION CHECK READS CODE THE WAY ITS LANGUAGE WRITES IT (AR-23).

THE CASE (Northgate, Go gateway, 2026-11-16): acceptance check of run crun_5c4fca0ac93e4239a72f
against contract acn_4aef727a71cf44d1b3c6 — 30 items, 20 met / 10 partial. Seven of the ten
partials were the reviewer's MET downgraded by BOSS's own citation check, not by the reviewer:

  * "the loader it names, 'Load', is not a function or variable name" (items 2, 8, 17, 19):
    `Load` is an exported Go function, refused because `load` sat in a keyword list;
  * "cited paths not in the tree: auth.VerifyJWT, proxy.Match, …" (item 26): Go's
    package-qualified names read as file paths;
  * "loader not named" (items 11, 25): the evidence named the loader as `file:Symbol` (item 25
    confirmed the README table is generated from the rulebook and in sync), but without the
    words LOADED BY.

The fixture is that real record (read read-only from the laptop install: items with their tags,
and each answer as the reviewer gave it) and a small Go tree with the files it cites. The three
partials the REVIEWER gave (9, 13, 30) stay partial; a citation that does not hold still fails.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from boss_engine.services import acceptance_gate as ag

ROW = json.loads((Path(__file__).parent / "fixtures" / "acceptance_ar23" /
                  "acn_4aef727a.json").read_text(encoding="utf-8"))

GO_TREE = {
    "go.mod": "module northgate\n\ngo 1.23\n\nrequire gopkg.in/yaml.v3 v3.0.1\n",
    "go.sum": "gopkg.in/yaml.v3 v3.0.1 h1:x\n",
    "Dockerfile": "FROM golang:1.23-alpine AS build\nRUN go build ./cmd/...\nFROM alpine:3.20\n",
    "Makefile": "run-staging:\n\tdocker compose up gateway-staging mock-upstream-staging\n",
    "docker-compose.yml": "services:\n  gateway-staging: {build: .}\n  gateway-production: {build: .}\n",
    "README.md": "# Northgate\n\n## Rulebook\n| id | severity |\n",
    "config/gateway.yaml": "listen: :8080\n",
    "config/tenants/lumen-pay.yaml": "id: lumen-pay\npii_fields: [user_email]\n",
    "examples/lumen-pay.good.yaml": "id: lumen-pay\n",
    "examples/lumen-pay.bad.yaml": "id: lumen-pay\n",
    "rules/api-guidelines.yaml": "rules:\n  - id: rate-limit-required\n",
    "schema/tenant.schema.json": "{\"type\": \"object\"}\n",
    "rules/loader.go": (
        "package rules\n\nimport _ \"embed\"\n\n//go:embed api-guidelines.yaml\nvar raw []byte\n\n"
        "// Load parses the embedded rulebook.\nfunc Load() ([]Rule, error) { return parse(raw) }\n"),
    "schema/schema.go": ("package schema\n\n//go:embed tenant.schema.json\nvar doc []byte\n\n"
                         "func Validate(b []byte) error { return check(doc, b) }\n"),
    "internal/config/loader.go": (
        "package config\n\nimport \"path/filepath\"\n\n"
        "// Load reads gateway.yaml and every tenant file.\n"
        "func Load(dir string) (*Snapshot, error) {\n"
        "\tglobal := filepath.Join(dir, \"gateway.yaml\")\n"
        "\tfiles, _ := filepath.Glob(filepath.Join(dir, \"tenants\", \"*.yaml\"))\n"
        "\treturn build(global, files)\n}\n\n"
        "func validateTenant(t *TenantConfig) error { return nil }\n"),
    "internal/config/watcher.go": (
        "package config\n\nfunc Watch(dir string, s *Store) { for { ReloadOnce(dir, s) } }\n\n"
        "func ReloadOnce(dir string, s *Store) error {\n\tsnap, err := Load(dir)\n"
        "\tif err != nil {\n\t\tlog.Printf(\"config reload rejected: %v\", err)\n"
        "\t\treturn err\n\t}\n\ts.Swap(snap)\n\treturn nil\n}\n"),
    "internal/config/atomic.go": (
        "package config\n\ntype Store struct{ p atomic.Pointer[Snapshot] }\n\n"
        "func (s *Store) Swap(n *Snapshot) { s.p.Store(n) }\n"),
    "internal/config/types.go": (
        "package config\n\ntype Route struct {\n\tRequestSchema string `yaml:\"request_schema\"`\n}\n\n"
        "type TenantConfig struct {\n\tPIIFields []string `yaml:\"pii_fields\"`\n}\n"),
    "internal/config/reload_state.go": ("package config\n\ntype ReloadState struct{ everLoaded bool }\n\n"
                                        "func (r *ReloadState) Ready() bool { return r.everLoaded }\n"),
    "internal/config/reload_metrics_test.go": (
        "package config\n\nfunc TestReloadOnce_Rejected_IncrementsRejectedReloadMetric_KeepsLastGood"
        "(t *testing.T) {}\n"),
    "internal/config/atomic_test.go": "package config\n\nfunc TestSwap(t *testing.T) {}\n",
    "internal/auth/jwt.go": "package auth\n\nfunc VerifyJWT(tok string, c Config) (*Claims, error) { return nil, nil }\n",
    "internal/auth/jwks.go": (
        "package auth\n\ntype JWKSCache struct{}\n\nfunc (c *JWKSCache) Get(kid string) (Key, error) "
        "{ return c.fetch(kid) }\n\nfunc (c *JWKSCache) fetch(kid string) (Key, error) { return Key{}, nil }\n"),
    "internal/auth/apikey.go": "package auth\n\nfunc CheckAPIKey(h, k string) bool { return false }\n",
    "internal/auth/mtls.go": "package auth\n\nfunc VerifyClientCert(c *x509.Certificate) error { return nil }\n",
    "internal/auth/auth_test.go": "package auth\n\nfunc TestVerifyJWT(t *testing.T) {}\n",
    "internal/proxy/router.go": (
        "package proxy\n\nfunc Match(snap *config.Snapshot, req *http.Request) *Matched { return nil }\n\n"
        "func pathMatches(route, p string) bool { return strings.HasPrefix(p, route) }\n\n"
        "func rewritePath(p string) string { return p }\n\nfunc methodAllowed(m string) bool { return true }\n"),
    "internal/proxy/router_test.go": "package proxy\n\nfunc TestMatchPathPrefixAndRewrite(t *testing.T) {}\n",
    "internal/proxy/handler.go": (
        "package proxy\n\nfunc (h *Handler) ServeHTTP(w http.ResponseWriter, r *http.Request) {}\n\n"
        "func (h *Handler) authenticate(r *http.Request) error { return nil }\n\n"
        "func applyCORS(w http.ResponseWriter, m *Matched) {}\n\n"
        "func logAccess(r *http.Request) {}\n\nfunc applyHeaderTransforms(r *http.Request) {}\n"),
    "internal/proxy/middleware.go": "package proxy\n\nfunc RenderMetricsHandler() http.Handler { return nil }\n",
    "internal/ratelimit/bucket.go": ("package ratelimit\n\ntype Bucket struct{}\n\n"
                                     "func (b *Bucket) Allow() bool { return true }\n"),
    "internal/ratelimit/limiter_test.go": "package ratelimit\n\nfunc TestAllow(t *testing.T) {}\n",
    "internal/redact/redact.go": "package redact\n\nfunc RedactFields(m map[string]any) {}\n",
    "internal/validate/jsonschema.go": "package validate\n\nfunc Against(schema, doc []byte) error { return nil }\n",
    "internal/metrics/metrics.go": ("package metrics\n\ntype Registry struct{}\n\n"
                                    "func (r *Registry) Render() string { return \"\" }\n"),
    "internal/lint/docs.go": ("package lint\n\nimport \"northgate/rules\"\n\n"
                              "// RenderReadme writes the rulebook table into the README.\n"
                              "func RenderReadme(readme string) error { rs, _ := rules.Load(); "
                              "return write(readme, rs) }\n"),
    "internal/lint/engine.go": ("package lint\n\nfunc Validate(path string) []Violation { return nil }\n\n"
                                "func ValidateDoc(b []byte) []Violation { return nil }\n"),
    "internal/lint/fixer.go": "package lint\n\nfunc Fix(path string) error { return nil }\n",
    "internal/lint/report.go": "package lint\n\nfunc Report(v []Violation) {}\n",
    "internal/lint/rules.go": "package lint\n\nfunc Rules() []Rule { rs, _ := rules.Load(); return rs }\n",
    "cmd/gwlint/main.go": "package main\n\nfunc main() { os.Exit(runValidate(os.Args)) }\n\n"
                          "func runValidate(args []string) int { return 0 }\n",
    "cmd/gwlint/gwlint_test.go": ("package main\n\nfunc TestGwlint_Fix_RoundTrip_ListsUnfixableAsNeedingHuman"
                                  "(t *testing.T) {}\n\nfunc TestGwlint_NeverLeaksSecretValue(t *testing.T) {}\n"),
    "cmd/gwlintdocs/main.go": "package main\n\nfunc main() { lint.RenderReadme(os.Args[1]) }\n",
    "cmd/northgate-gateway/main.go": "package main\n\nfunc main() { config.Load(os.Getenv(\"CONFIG_DIR\")) }\n",
    "cmd/northgate-gateway/integration_test.go": (
        "package main\n\nfunc TestGateway_HotReload_InvalidEditRejectedKeepsServing(t *testing.T) {}\n"),
    "mocks/echo-upstream/main.go": "package main\n\nfunc main() {}\n",
}


def _tree(root: Path, files=GO_TREE) -> Path:
    for rel, body in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(body, encoding="utf-8")
    return root


def _reply(answers) -> str:
    return "\n\n".join(f"ITEM {a['n']}: {a['said']} — {a['evidence']}" for a in answers) \
        + "\n\nACCEPTANCE: NOT MET\n"


def test_the_real_northgate_row_keeps_every_met_the_reviewer_evidenced(tmp_path):
    root = _tree(tmp_path)
    # The record as it was: 20 met / 10 partial, seven of the partials BOSS's own doing.
    assert ROW["counts"] == {"met": 20, "missing": 0, "partial": 10, "unjudged": 0}
    downgraded = {a["n"] for a in ROW["answers"] if a["gate_reason"]}
    assert downgraded == {2, 8, 11, 17, 19, 25, 26}

    got = {i["n"]: i for i in ag.parse_acceptance(_reply(ROW["answers"]), ROW["items"], root=root)}
    for n in sorted(downgraded):
        assert got[n]["status"] == "met", (n, got[n]["evidence"][-200:])
    # The reviewer's OWN partials stand: the request-id bug, the half-enforced public reason,
    # the untested /readyz state.
    reviewer_partial = {a["n"] for a in ROW["answers"] if a["said"] == "PARTIAL"}
    assert reviewer_partial == {9, 13, 30}
    assert {n for n, i in got.items() if i["status"] == "partial"} == reviewer_partial

    rec = ag.judge(ROW["items"], _reply(ROW["answers"]), {}, root=root)
    assert rec["counts"]["met"] == 27 and rec["partial"] == [9, 13, 30]


@pytest.mark.parametrize("cited,where", [
    ("auth.VerifyJWT", "internal/auth/jwt.go"),           # Go: package-qualified
    ("Store.Swap", "internal/config/atomic.go"),          # Go: method on its type
    ("JWKSCache.fetch", "internal/auth/jwks.go"),         # Go: unexported method
    ("proxy.pathMatches", "internal/proxy/router.go"),    # Go: unexported function
    ("ratelimit.Bucket", "internal/ratelimit/bucket.go"),  # Go: a type
    ("proxy.Match", "internal/proxy/router.go"),
])
def test_go_symbols_resolve_to_the_file_that_defines_them(tmp_path, cited, where):
    root = _tree(tmp_path)
    assert ag.evidence_paths(root, f"see `{cited}`") == ([where], [])


@pytest.mark.parametrize("files,cited,where", [
    ({"app/billing.py": "class Invoice:\n    def total(self):\n        return 1\n",
      "app/util.py": "def helper():\n    pass\n"}, "Invoice.total", "app/billing.py"),
    ({"app/billing.py": "def charge(card):\n    return card\n"}, "billing.charge", "app/billing.py"),
    ({"src/main/java/com/x/AuthService.java":
      "public class AuthService {\n  public boolean verify(String t) {\n    return true;\n  }\n}\n"},
     "AuthService.verify", "src/main/java/com/x/AuthService.java"),
    ({"src/auth.ts": "export class Auth {\n  verifyToken(t: string): boolean {\n    return true;\n  }\n}\n"},
     "Auth.verifyToken", "src/auth.ts"),
    ({"src/session.ts": "export const createSession = (u: User) => ({ u });\n"},
     "session.createSession", "src/session.ts"),
])
def test_python_java_and_ts_symbols_resolve_the_same_way(tmp_path, files, cited, where):
    root = _tree(tmp_path, files)
    assert ag.evidence_paths(root, f"`{cited}` does it") == ([where], [])


@pytest.mark.parametrize("cited", [
    "auth.Nope",              # defined nowhere
    "log.Printf",             # the standard library, not this tree
    "auth.TestVerifyJWT",     # defined only in a test
    "rules.lumen-pay.yaml",   # a file name, and no such file
])
def test_a_symbol_the_tree_does_not_define_is_never_evidence(tmp_path, cited):
    root = _tree(tmp_path)
    found, absent = ag.evidence_paths(root, f"`{cited}`")
    assert found == []
    # A short, path-shaped one is reported as not in the tree, exactly as before.
    if cited in ("auth.Nope", "log.Printf"):
        assert absent == [cited]


def test_a_name_mentioned_only_in_a_comment_is_not_a_definition(tmp_path):
    root = _tree(tmp_path, {"internal/auth/jwt.go": "package auth\n// VerifyJWT checks tokens (TODO)\n"})
    assert ag.evidence_paths(root, "`auth.VerifyJWT`") == ([], ["auth.VerifyJWT"])


def test_a_plain_name_is_a_loader_when_the_file_defines_it_and_reads_the_file(tmp_path):
    """`Load` is no longer refused by name; it must still be defined there and name the file."""
    root = _tree(tmp_path)
    item = ("Editing `config/tenants/lumen-pay.yaml` is picked up without restart.")
    ok = "LOADED BY `internal/config/loader.go:Load`, `internal/config/watcher.go:Watch`"
    assert ag.wiring_problem(root, ok, item_text=item, needs_loader=True) is None
    # Defined in the cited file but the file is named nowhere in its code: still partial.
    (root / "internal/config/loader.go").write_text(
        "package config\n// reads config/tenants/*.yaml\nfunc Load() error { return nil }\n")
    assert "does not name" in ag.wiring_problem(root, ok, item_text=item, needs_loader=True)


def test_a_loader_is_found_in_the_evidence_or_the_tree_without_the_words_loaded_by(tmp_path):
    root = _tree(tmp_path)
    item = "A rulebook: `rules/api-guidelines.yaml`, rendered into the README."
    why = ("`rules/api-guidelines.yaml` has 23 rules; the README table is generated by "
           "`internal/lint/docs.go:RenderReadme` and is in sync.")
    assert ag.wiring_problem(root, why, item_text=item, needs_loader=True) is None
    # No code in the tree reads the rulebook: the loader is not named, whatever the words say.
    (root / "rules/loader.go").write_text("package rules\n\nfunc Load() ([]Rule, error) "
                                          "{ return builtin, nil }\n")
    assert "loader not named" in ag.wiring_problem(root, why, item_text=item, needs_loader=True)
