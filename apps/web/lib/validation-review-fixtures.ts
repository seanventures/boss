/**
 * The spec's reference data (docs/design/validation-review/SPEC.md §1, §6): Lumen Pay's
 * `lumen-pay.bad.yaml` sent to Northgate, checked with gwlint against ruleset northgate v3.
 * 9 findings (4 P0 · 4 P1 · 1 P2). `gwlint --fix` made 4 auto fixes; 5 are still open (three
 * agent fixes proposed, the admin route that needs a person, the P2). Used by the tests and by
 * sample mode; the secret is masked as the engine stores it.
 */
export const PARTNER_TEXT = [
  "tenant: lumen-pay",
  "owner: \"Lumen Pay Platform Team\"",
  "contact: platform@lumenpay.example",
  "tier: standard",
  "data_classification: pii",
  "routes:",
  "  - path: /v1/payments",
  "    methods: [GET, POST]",
  "    upstream: lumen-pay-core",
  "    auth:",
  "      type: jwt",
  "      issuer: https://auth.lumenpay.example",
  "      jwks_url: https://auth.lumenpay.example/.well-known/jwks.json",
  "      audiences: [northgate]",
  "    query_params: [account_id, email]",
  "    upstream_auth:",
  "      api_key: \"sk_live_••••4242\"",
  "    cors:",
  "      allow_origins: [\"*\"]",
  "      allow_credentials: true",
  "  - path: /v1/admin/accounts",
  "    description: \"Internal account admin surface for Lumen Pay ops\"",
  "    methods: [GET, POST, DELETE]",
  "    upstream: lumen-pay-core",
  "    auth:",
  "      type: jwt",
  "      issuer: https://auth.lumenpay.example",
  "      jwks_url: https://auth.lumenpay.example/.well-known/jwks.json",
  "    timeouts:",
  "      connect_ms: 500",
  "      read_ms: 5000",
  "    retries: 2",
  "    tags: [admin]",
  "upstreams:",
  "  lumen-pay-core:",
  "    url: \"http://lumen-pay-core.internal.example\"",
  "    health: /healthz",
].join("\n");

const FILE = "config/tenants/lumen-pay.yaml";
const DOC = "rules/api-guidelines.yaml";
const E1 = { id: "E-1", kind: "command", cmd: "gwlint validate lumen-pay.yaml --json", exit: 1, artifact: "art_e1" };
const E2 = { id: "E-2", kind: "command", cmd: "gwlint validate lumen-pay.yaml --json", exit: 1, absent: true, artifact: "art_e2" };

type F = Record<string, unknown>;
const finding = (id: string, rule: string, severity: string, line: number, fix_kind: string, status: string, original: string | null, fixed: string | null,
  hunk: string, extra: F = {}): F => ({
  id, source: { kind: "validator", name: "gwlint", rule_id: rule, rule_doc: `${DOC}#${rule}` }, severity, basis: "ruleset northgate v3",
  native_severity: severity === "P2" ? "warning" : "error", location: { file: FILE, line, partner_line: line, ...(extra.end_line ? { end_line: extra.end_line } : {}) },
  original, fixed, hunk_id: hunk, fix_kind, status, evidence: status === "fixed" ? [E1, E2] : [E1], override: null, ...extra,
});

/** The findings after `gwlint --fix` (4 auto fixed) with the agent's fixes proposed: 5 open. */
export const FINDINGS_OPEN: F[] = [
  finding("V-01", "tenant-rate-limit-required", "P1", 1, "agent", "open", "(none)", "rate_limit: {rps: 50, burst: 50}", "h-01",
    { guide: "rps 50, burst 50 unless the brief names a quota.", why: "A tenant without a quota can starve every other tenant on the gateway." }),
  finding("V-02", "pii-fields-declared", "P2", 5, "agent", "open", "data_classification: pii", null, "",
    { why: "A route that carries personal data must name which query params hold it, so logs can mask them." }),
  finding("V-03", "no-inlined-secrets", "P0", 17, "agent", "open", "      api_key: \"sk_live_••••4242\"", null, "h-03",
    { guide: "Delete the key if the route already authenticates another way; otherwise use api_key_secret_ref: vault://<tenant>/<name>.",
      why: "A secret committed to a config repo is permanently compromised, because it stays in git history even after deletion." }),
  finding("V-04", "rate-limit-required", "P1", 7, "auto", "fixed", "(none)", "rate_limit: {rps: 10, burst: 10}", "h-04"),
  finding("V-05", "request-size-capped", "P1", 7, "auto", "fixed", "(none)", "max_body_bytes: 1048576", "h-04"),
  finding("V-06", "cors-no-wildcard-with-credentials", "P0", 19, "agent", "open", "      allow_credentials: true", "      allow_credentials: false", "h-06",
    { end_line: 20, guide: "Default to allow_credentials: false. Keep credentials only if the brief names exact origins.",
      why: "A wildcard origin with credentials lets any site read a signed-in user's data." }),
  finding("V-07", "no-sensitive-paths-exposed", "P0", 21, "human", "open", "  - path: /v1/admin/accounts", null, "h-07",
    { end_line: 33, guide: "Removing a route is a product decision; propose removal, a person accepts.", why: "An internal admin surface must never be routed to a partner." }),
  finding("V-08", "request-size-capped", "P1", 21, "auto", "fixed", "(none)", "max_body_bytes: 1048576", "h-08"),
  finding("V-09", "upstream-https-required", "P0", 35, "auto", "fixed", "    url: \"http://lumen-pay-core.internal.example\"",
    "    url: \"https://lumen-pay-core.internal.example\"", "h-09"),
];

export const VALIDATION_OPEN = {
  validator: { id: "gwlint", version: "3b7747c", adapter: "gwlint-json" },
  ruleset: { id: "northgate", version: 3, sha: "a1b2c3d" },
  input: { file: "examples/lumen-pay.bad.yaml", sha256: "7c1e9a0b52", from: "Lumen Pay", text: PARTNER_TEXT },
  target: FILE,
  before: { command: "gwlint validate lumen-pay.yaml --json", exit: 1, findings: ["V-01", "V-02", "V-03", "V-04", "V-05", "V-06", "V-07", "V-08", "V-09"], evidence: "E-1" },
  fixes: [
    { finding: "V-04", rule: "rate-limit-required", file: FILE, line: 7, before: "  - path: /v1/payments", after: "  - path: \"/v1/payments\"\n    max_body_bytes: 1048576\n    rate_limit:\n      rps: 10\n      burst: 10", kind: "auto", hunk_id: "h-04" },
    { finding: "V-05", rule: "request-size-capped", file: FILE, line: 7, before: "", after: "    max_body_bytes: 1048576", kind: "auto", hunk_id: "h-04" },
    { finding: "V-08", rule: "request-size-capped", file: FILE, line: 21, before: "", after: "    max_body_bytes: 1048576", kind: "auto", hunk_id: "h-08" },
    { finding: "V-09", rule: "upstream-https-required", file: FILE, line: 35, before: "    url: \"http://lumen-pay-core.internal.example\"", after: "    url: \"https://lumen-pay-core.internal.example\"", kind: "auto", hunk_id: "h-09" },
    { finding: "V-01", rule: "tenant-rate-limit-required", file: FILE, line: 1, before: "tenant: lumen-pay", after: "tenant: lumen-pay\nrate_limit:\n  rps: 50\n  burst: 50", kind: "agent", hunk_id: "h-01" },
    { finding: "V-03", rule: "no-inlined-secrets", file: FILE, line: 17, before: "    upstream_auth:\n      api_key: \"sk_live_••••4242\"", after: "", kind: "agent", hunk_id: "h-03" },
    { finding: "V-06", rule: "cors-no-wildcard-with-credentials", file: FILE, line: 19, before: "      allow_credentials: true", after: "      allow_credentials: false", kind: "agent", hunk_id: "h-06" },
    { finding: "V-07", rule: "no-sensitive-paths-exposed", file: FILE, line: 21, before: "  - path: /v1/admin/accounts\n    description: \"Internal account admin surface…\"\n    …11 more lines", after: "", kind: "human", hunk_id: "h-07" },
  ],
  after: { command: "gwlint validate lumen-pay.yaml --json", exit: 1, findings: ["V-01", "V-02", "V-03", "V-06", "V-07"], head_sha: "eaff448", seconds: 1.2, evidence: "E-2" },
  findings: FINDINGS_OPEN,
  overrides: [],
  rerun: { state: "idle" },
  head_sha: "eaff448",
  contract_target: "staging 203.0.113.10",
  contract: [
    { id: "C-1", case: "healthz", expect: 200, got: 200, ms: 31, covers: [] },
    { id: "C-2", case: "payments_unauth ×10", expect: 401, got: 401, ms: 38, covers: ["auth"] },
    { id: "C-12", case: "payments_rate_limited_11th", expect: 429, got: 429, ms: 41, covers: ["V-04"] },
  ],
};

const done = (f: F, status: string, extra: F = {}): F => ({ ...f, status, evidence: [E1, { ...E2, id: "E-4", exit: 0 }], ...extra });
const byId = Object.fromEntries(FINDINGS_OPEN.map((f) => [f.id as string, f]));
const O1 = { id: "O-1", action: "accept", finding_id: "V-07", actor: "Paramaguru", reason: "Partner confirmed the admin route is internal-only.", created_at: "2026-11-21T14:19:00Z", rerun_id: "E-3" };
const O2 = { id: "O-2", action: "edit", finding_id: "V-06", actor: "Paramaguru", reason: "Dashboard needs cookies. Partner named one origin in the brief.",
  created_at: "2026-11-21T14:21:00Z", commit_sha: "9f2c1d0", rerun_id: "E-4", before_text: "      allow_origins: [\"*\"]", after_text: "      allow_origins: [\"https://dashboard.lumenpay.example\"]" };
const O3 = { id: "O-3", action: "waive", finding_id: "V-02", actor: "Paramaguru", reason: "no PII in query params, KAN-12", created_at: "2026-11-21T14:23:00Z" };

/** The end of the demo (wireframe C): every P0/P1 closed, one override each kind, packet v4. */
export const PACKET_READY = {
  packet_version: 4, head_sha: "9f2c1d0", built_at: "2026-11-21T14:24:00Z",
  work: { title: "Lumen Pay tenant onboarding", workflow_run: "wfr_40229c5c963c4f599c51", run_no: 16, build: "crun_58aecc16a1b2", later_builds: ["crun_a9df01", "crun_2bc5f2"], change: 9 },
  verdict: { state: "ready", reasons: [] },
  counts: { P0: { found: 4, open: 0, waived: 0 }, P1: { found: 4, open: 0, waived: 0 }, P2: { found: 1, open: 0, waived: 1 }, P3: { found: 0, open: 0, waived: 0 }, unmapped: 0 },
  findings: [
    done(byId["V-03"], "fixed", { fixed: "(removed)" }),
    done(byId["V-06"], "overridden", { original: "[\"*\"]", fixed: "[\"https://dashboard.lumenpay.example\"]", fix_kind: "human", override: O2 }),
    done(byId["V-07"], "fixed", { original: "/v1/admin/accounts", fixed: "(route removed)", override: O1 }),
    done(byId["V-09"], "fixed", { original: "http://", fixed: "https://" }),
    done(byId["V-01"], "fixed", { fixed: "rps 50 · burst 50" }),
    done(byId["V-04"], "fixed", { fixed: "rps 10 · burst 10" }),
    done(byId["V-05"], "fixed", { fixed: "1048576" }),
    done(byId["V-08"], "fixed", { fixed: "1048576" }),
    { id: "R-1", source: { kind: "reviewer", name: "security seat", rule_id: "never-fix-rule-rewritten", rule_doc: "" }, severity: "P1", basis: "ruleset northgate v3",
      location: { file: "internal/lint/fixer.go", line: 69 }, original: "--fix rewrites the tenant quota (a never-fix rule)", fixed: null, hunk_id: "", fix_kind: "none",
      status: "open", followup: { change: 10, href: "/changes/northgate-gateway/10" }, evidence: [] },
    done(byId["V-02"], "waived", { override: O3 }),
  ],
  vs_input: { file: FILE, hunks: 7, added: 9, removed: 13, hunks_by_finding: { "h-01": ["V-01"], "h-03": ["V-03"], "h-04": ["V-04", "V-05"], "h-06": ["V-06"], "h-07": ["V-07"], "h-08": ["V-08"], "h-09": ["V-09"] }, unexplained_hunks: [] },
  validators: [{ id: "gwlint", ruleset: "northgate v3", before: 9, after_open: 0, after_waived: 1, exit: 0, evidence: "E-4" }],
  tests: [{ id: "T-1", suite: "go test ./...", passed: 214, failed: 0, evidence: "E-6" }],
  contract: [{ id: "C-12", case: "payments_rate_limited_11th", expect: 429, got: 429, ms: 41, covers: ["V-04"] }, { id: "C-1", case: "healthz", expect: 200, got: 200, ms: 31, covers: [] }],
  risks: [{ text: "CORS credentials kept for one dashboard origin", refs: ["V-06", "O-2"] }, { text: "Fixer can rewrite tenant quota (not hit by this file)", refs: ["R-1"] }],
  claims: [{ text: "All 4 P0 findings closed", refs: ["V-03", "V-06", "V-07", "V-09", "E-4"] }],
  overrides: [O1, O2, O3],
  ruleset: { id: "northgate", version: 3, sha: "a1b2c3d" },
  cited_fact_ids: Array.from({ length: 14 }, (_, i) => `kgn_fact${i}`),
};

/** What the run would show today (wireframe C, right): the packet failed validate_packet(). */
export const PACKET_INVALID = {
  ...PACKET_READY,
  packet_version: 2, head_sha: "eaff448",
  verdict: { state: "invalid", reasons: [
    "input_substituted: The target file equals the repo's config/tenants/lumen-pay.yaml, not the partner's file (sha 7c1e…).",
    "claim_without_evidence: Commit message says \"--fix applied\", but the step recorded no fixes[] and no gwlint run.",
  ] },
  findings: [], overrides: [], risks: [], claims: [],
  counts: { P0: { found: 0, open: 0, waived: 0 }, P1: { found: 0, open: 0, waived: 0 }, P2: { found: 0, open: 0, waived: 0 }, P3: { found: 0, open: 0, waived: 0 }, unmapped: 0 },
  validators: [],
};

export const RULESET_NORTHGATE = {
  id: "northgate", version: 3, sha: "a1b2c3d", path: ".boss/rulesets/northgate.yaml", validator: "gwlint",
  merged_at: "2026-11-20T10:00:00Z", merged_by: "Paramaguru", rules_doc: DOC,
  rules: [
    { id: "no-inlined-secrets", severity: "P0", autofix: "never", guide: "Delete the key if the route already authenticates another way; otherwise use api_key_secret_ref: vault://<tenant>/<name>." },
    { id: "cors-no-wildcard-with-credentials", severity: "P0", autofix: "agent", guide: "Default to allow_credentials: false. Keep credentials only if the brief names exact origins." },
    { id: "no-sensitive-paths-exposed", severity: "P0", autofix: "human", guide: "Removing a route is a product decision; propose removal, a person accepts." },
    { id: "upstream-https-required", severity: "P0", autofix: "mechanical", guide: "" },
    { id: "tenant-rate-limit-required", severity: "P1", autofix: "agent", guide: "rps 50, burst 50 unless the brief names a quota." },
    { id: "rate-limit-required", severity: "P1", autofix: "mechanical", guide: "" },
    { id: "public-route-no-auth-block", severity: "P0", autofix: "never", guide: "", enabled: false },
    { id: "pii-fields-declared", severity: "P2", autofix: "agent", guide: "List query params that carry personal data; ask the partner if unknown." },
    { id: "lumen-no-refund-without-schema", severity: "P1", autofix: "agent", guide: "A POST route under /v1/payments/refunds must declare request_schema.", custom: true, check: "agent" },
  ],
};
