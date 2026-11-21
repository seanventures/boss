# Validation and review: what gwlint caught, how it was fixed, and what you are merging

Spec + wireframe. Read against origin/main `2ee67573b`, live install, 2026-11-21.
Reference run: `api_onboarding` v13, project `northgate-gateway`, run `wfr_40229c5c963c4f599c51` (run #16), change #9, merge decision `dec_5956127a3df14163810d`.
Wireframe: `wireframe.html` in this folder (screenshots `wireframe-1440.png`, `wireframe-400.png`).
Sample partner: Lumen Pay → Northgate. All finding data below is real: `gwlint validate --json` and `gwlint --fix` were run on a temp copy of `examples/lumen-pay.bad.yaml` saved as `lumen-pay.yaml`. Rerun `go run ./cmd/gwlint validate examples/lumen-pay.bad.yaml --json` in the Northgate repo to reproduce.

**Status: APPROVED by the operator, 2026-11-21.** Decisions:
1. Rulesets live in the repo (`.boss/rulesets/*.yaml`); a console edit opens a ruleset change that BOSS merges itself (audited, still feels instant).
2. P-levels as in §1.1 / §4: secrets, CORS-with-credentials, admin path, plaintext upstream = P0; missing rate limit / size cap = P1; `pii-fields-declared` = P2.
3. The one human decision in the demo is removing `/v1/admin/accounts`.
4. The demo is recorded only after the remediation defect is fixed and one dry run shows a non-empty `fixes` list.

**Already merged (do not rebuild):** #262 (`engine/boss_engine/services/input_fidelity.py`) adds the remediation contract, the input-substitution / copied-answer / unbacked-claim checks, the `input_fidelity` gate, and the step-output `validation` record (`validator, command, fix_command, input, output, before[], fixes[], after[], exit`). PR 1 below extends that record to `boss.validation/v1` and adds `.boss/validators.yaml` + adapters; it must reuse input_fidelity, not duplicate it.

The operator's brief, verbatim: "All I wanted to see how the change looks and how the validation tool like gwlint caught the validations and how they are displayed for the user and how they are automatically fixed in the workflow - this is the core part of the workflow and we should have steller experience in this flow and there should be no regression. The Review packet display format is very bad - continuous sentence, no highlight of important issues like P0 and P1s etc.. we should be very concise and should not allow AI slop in the documentation.. any vague information will lead into the incorrect analysis.. is there a way to override some of the findings and revert back to old.. so we should be able to allow changes by humans and I hope the gwlint guides can be customized too."

---

## 0. What is wrong today (evidence)

| # | Symptom the operator saw | Cause (file:line on origin/main) |
|---|---|---|
| 1 | The merge card reads as one long sentence | `formatRecommendation()` joins four sentence fragments with `" "` (`apps/web/app/decision-inbox-ui.tsx:117-131`). The same code is duplicated at `apps/web/app/page.tsx:243`. |
| 2 | The title is "Follow-up: deferred review findings — Follow-up: More tests — Validate tenant config…" | Each follow-up build adds a prefix to its parent's title. The decision then takes `recommendation.title` from the newest follow-up (`crun_2bc5…`), not from the work. |
| 3 | P0 and P1 are not highlighted | The P1 follow-up (`internal/lint/fixer.go:69`, which silently rewrites the tenant quota) is only inside a collapsed "Ready to merge" disclosure. `followups.items[]` carries `tier`, but the card renders it as prose. |
| 4 | "Based on kgn_a491…, kgn_4d4e…" in large type | `cited_fact_ids` is rendered as raw ids (`apps/web/app/knowledge-console.tsx:15`). |
| 5 | The review packet is prose | The `review_packet` artifact (`art_a4741b8d…`) is a single markdown string, `content.summary`, built by `_review_packet_markdown` (`engine/boss_engine/services/software_factory.py:1832`, which uses an LLM with a fallback). It has no typed findings, no severity counts and no link to evidence. |
| 6 | Nothing about gwlint anywhere | The `validate_and_fix` step output holds only `{tool:create_code_task, await, run_id}`. gwlint's JSON is stored nowhere. `validation_summary` is a fixed sentence ("No hosted CI for a local repository…"). |
| 7 | "Built" says "The implementation already correctly fixes F-273 and F-274…" | `explained.for_work` → `summary_of(build(main))`. `main_build` picks `merge_pr.build_id`, which is the follow-up that last pushed to change #9, and shows the coder's free-text `impl_summary`. |
| 8 | No link to the workflow run or the build | The links exist only in the ⋯ menu (`work-panel.tsx:583-590`). On `/changes/northgate-gateway/9` the page calls `GET /v1/workflow-runs/crun_2bc5…` and gets a 404: it passes a build id where a run id is expected, so the run link never renders. |
| 9 | The partner's file was not remediated | The run shipped the repo's existing `config/tenants/lumen-pay.yaml` and claimed that "--fix" was applied. Another agent is fixing this. This spec makes the same failure visible and blocking (§1.4). |

---

## 1. Information contract (typed, produced by the engine, never free prose)

### 1.1 Severity: fixed definitions
Severity comes from the **ruleset** (§4), never from a model's judgement at render time.

| Level | Definition (all must hold) | Merge effect | Waive |
|---|---|---|---|
| **P0** | If merged, it exposes data, credentials or an internal surface, or it bypasses auth. Exploitable without insider access. | Blocks merge | Needs a second person with `validation:waive_p0`, plus a reason and an expiry |
| **P1** | Breaks availability, correctness or a declared contract under normal use (missing limits, unbounded input, wrong tenant binding). | Blocks merge | Project owner, with a reason |
| **P2** | Degrades hygiene or observability. Not exploitable and not contract-breaking. | Listed, does not block | Anyone with merge rights, with a reason |
| **P3** | Style or naming. | Collapsed | Not needed |

A reviewer finding (from an LLM seat) must name the rule or standard behind its tier (`basis`). A finding with no basis is entered as **P2 with an "unmapped" flag**, and the packet counts it under `unmapped`, so an unsupported "P0" cannot block a merge and an unsupported "P3" cannot hide one.

### 1.2 Finding (one record per issue, from any source)
```jsonc
// boss.finding/v1
{
  "id": "V-03",                      // stable within the validation; reviewer ids R-n, test ids T-n, contract C-n
  "source": {"kind": "validator|reviewer|test|contract",
             "name": "gwlint",       // validator id / seat / suite
             "rule_id": "no-inlined-secrets", "rule_doc": "rules/api-guidelines.yaml#no-inlined-secrets"},
  "severity": "P0",                  // from ruleset; "basis": "ruleset northgate v3"
  "native_severity": "error",        // what the tool said
  "location": {"file": "config/tenants/lumen-pay.yaml", "line": 17, "partner_line": 17},
  "original": "      api_key: \"sk_live_••••••••4242\"",   // masked by the secret masker before storage
  "fixed": null,                     // the line after (or null = removed)
  "hunk_id": "h-03",                 // the diff hunk on the change branch that addresses it
  "fix_kind": "auto|agent|human|none",
  "status": "open|fixed|overridden|reverted|waived|suppressed",
  "evidence": [{"id": "E-1", "kind": "command", "cmd": "gwlint validate lumen-pay.yaml --json", "exit": 1, "artifact": "art_…"},
               {"id": "E-4", "kind": "command", "cmd": "gwlint validate lumen-pay.yaml --json", "exit": 0, "absent": true}],
  "override": null                   // §3 record when a person acted
}
```
`status: fixed` is legal only when there is an after-run (`absent: true` on a re-run of the same validator, same file, same ruleset version) **and** a `hunk_id`. A test finding is `fixed` only when there is a passing run of that test at the packet's head sha.

### 1.3 Validation result (the step output; aligned with the in-flight `validation:` step field)
```jsonc
// boss.validation/v1 — the output of the Validate step; the other agent's
// {validator, command, before[], fixes[{rule,file,line,before,after,kind}], after[]} maps 1:1:
{
  "validator": {"id": "gwlint", "version": "3b7747c", "adapter": "gwlint-json"},
  "ruleset": {"id": "northgate", "version": 3, "sha": "…"},
  "input": {"file": "examples/lumen-pay.bad.yaml", "sha256": "…", "from": "partner"},   // partner's bytes, never the repo's good file
  "target": "config/tenants/lumen-pay.yaml",
  "before": {"command": "gwlint validate … --json", "exit": 1, "findings": ["V-01", "…"]},
  "fixes": [{"finding": "V-04", "rule": "rate-limit-required", "file": "…", "line": 7,
             "before": "  - path: /v1/payments", "after": "    rate_limit: {rps: 10, burst: 10}", "kind": "auto", "hunk_id": "h-01"}],
  "after": {"command": "gwlint validate … --json", "exit": 0, "findings": []},
  "findings": [ /* boss.finding/v1 */ ]
}
```
Guards are enforced in code, not in prompts:
- `input.sha256` must equal the partner file the run received. If the target file at the head sha equals a different repo file byte-for-byte (for example `examples/lumen-pay.good.yaml` or the existing tenant file), the validation fails with `input_substituted`. This catches defect #9.
- A build's commit message or comment that says "--fix applied" with no `fixes[]` entry is a claim without evidence (§1.4).

### 1.4 Review packet (what the merge card and the change page render)
```jsonc
// boss.review_packet/v2
{
  "packet_version": 4, "head_sha": "eaff448…", "built_at": "…",
  "work": {"title": "Lumen Pay tenant onboarding", "workflow_run": "wfr_40229c5c…", "run_no": 16,
           "build": "crun_58aecc16…", "later_builds": ["crun_a9df…", "crun_2bc5…"], "change": 9},
  "verdict": {"state": "ready|blocked|invalid", "reasons": ["open_p0:V-07"]},   // computed, not written
  "counts": {"P0": {"found": 4, "open": 0, "waived": 0}, "P1": {…}, "P2": {…}, "P3": {…}, "unmapped": 0},
  "findings": [ /* boss.finding/v1, sorted: open P0, open P1, overridden, waived, fixed */ ],
  "vs_input": {"file": "config/tenants/lumen-pay.yaml", "hunks": 7, "added": 9, "removed": 13,
               "hunks_by_finding": {"h-03": ["V-03"]}, "unexplained_hunks": []},
  "validators": [{"id": "gwlint", "ruleset": "northgate v3", "before": 9, "after_open": 0, "after_waived": 1, "exit": 0, "evidence": "E-4"}],
  "tests": [{"id": "T-1", "suite": "go test ./...", "passed": 214, "failed": 0, "evidence": "E-6"}],
  "contract": [{"id": "C-12", "case": "payments_rate_limited_11th", "expect": 429, "got": 429, "ms": 41, "covers": ["V-04"]}],
  "risks": [{"text": "CORS credentials kept for dashboard origin", "refs": ["V-06", "O-2"]}],
  "claims": [{"text": "All 4 P0 findings closed", "refs": ["V-03", "V-06", "V-07", "V-09", "E-4"]}]
}
```

**Anti-slop rules (`validate_packet()`, engine; a unit test for each):**
1. Every field above is required. The engine builds the packet from step outputs. No model writes the packet.
2. Free text exists only in `claims[].text` and `risks[].text`, each at most 120 characters, and each must have at least one `refs[]` entry that resolves to a finding, evidence, override or test id in this packet. An unreferenced claim, or a ref to an unknown id, means the packet is rejected.
3. `counts` must equal what is recomputed from `findings`, or the packet is rejected.
4. `status: fixed` without after-evidence plus a `hunk_id` is rejected. A hunk listed in `vs_input.unexplained_hunks` (a change no finding or override accounts for) is shown in amber as "Unexplained change", and it blocks only when it touches the partner's file.
5. The build's `impl_summary`, `summary_of`, and LLM review markdown are **not rendered** on the merge card, the packet, or the Built column. They remain available in the build's log.
6. A rejected packet sets `verdict.state = invalid`. The merge card then shows a red "Packet invalid: 2 claims without evidence" banner listing each failing rule, and **Merge is disabled** (the only action is "Rebuild packet"). The engine never falls back to prose.

### 1.5 How the UI renders the packet (no sentences joined with spaces)
Order is fixed: verdict bar → severity chips (P0/P1/P2/P3 found→open) → **P0/P1 table first** (id, rule, line, original → fixed, fix kind, status) → "Changed vs partner" (hunks; each hunk lists its finding ids) → validators row (command, exit, before → after) → tests and contract table → open risks (each with ref chips) → provenance line (run #16 ↗, build ↗, change #9 ↗, ruleset v3, packet v4, head sha). Each ref chip scrolls to the item it refers to. Fact ids (`kgn_…`) appear as a "14 facts" chip, never inline.

---

## 2. Validation flow view (generic "validators")

Where it lives: a **Validation** tab on the change page (`/changes/{p}/{n}?tab=validation`), and as the Built column's drill-down on the Work panel. Layout: four columns that become four stacked steps at 400 px.

1. **Partner file**: name, sha, size, and who sent it (the run's input). Line gutter with severity dots on the lines that have findings.
2. **Findings** (gwlint, ruleset v3): grouped by severity, then rule, then line. Each row shows the rule id, line, the fix-kind badge (Auto / Agent / Needs you) and the status.
3. **Fixes**: one before/after diff per finding, from the hunk on the change branch. Each diff carries the override controls (§3).
4. **Re-run**: the same command at the head sha, exit code, "0 open · 1 waived". Below it, the contract tests table (12 cases; each case lists the findings it covers).

**Validator declaration (per project, repo file `.boss/validators.yaml`):**
```yaml
validators:
  - id: gwlint
    applies_to: ["config/tenants/*.yaml"]
    run:   "go run ./cmd/gwlint validate {file} --json"
    fix:   "go run ./cmd/gwlint --fix {file}"        # optional: mechanical fixes
    output: {adapter: gwlint-json}                   # or: sarif | eslint-json | ruff-json | junit
    rules_doc: rules/api-guidelines.yaml             # where rule why/fix text lives
    ruleset: northgate                               # BOSS ruleset (§4)
```
Adapters are a small engine registry (`validator_adapters/`). Each one maps tool output to `boss.finding/v1`. `sarif` reuses `engine/boss_engine/security_scan/parsers.py`. Each adapter has a fixture test that runs on the tool's real output. ESLint, ruff or a custom script needs one adapter and no UI change.

---

## 3. Human override

Per finding: **Accept fix** · **Reject fix** (revert that hunk to the partner's original lines) · **Edit line** (hand-written replacement) · **Waive** (reason required; P0 needs an approver) · **Reopen**. Per file: **Revert all to partner's original**.

**API**
```
POST /v1/changes/{project}/{n}/findings/{finding_id}/override
  {action: accept|reject|edit|waive|reopen, text?, reason?, approver_id?, expires_at?, head_sha}
POST /v1/changes/{project}/{n}/validation/revert-all   {reason, head_sha}
GET  /v1/changes/{project}/{n}/validation              -> boss.validation/v1 + overrides + rerun state
```
- `head_sha` is a precondition. A stale sha returns 409 with the newer packet. This stops two people overriding against different states.
- `reject`, `edit` and `revert-all` write one commit to the change branch with the trailers `BOSS-Override: O-2 V-06 reject`, `On-behalf-of:` and `Reason:`. `accept` and `waive` change only records, with no commit.
- Each override starts an **automatic re-run**: the validator on the target file, plus the tests and contract cases whose `covers[]` include the finding. The result is packet v(n+1) and the merge decision is refreshed. Merge is disabled while the re-run is pending ("Re-checking after your change · 40 s").
- A P0 waive creates a decision for the named approver, and the finding stays `open` until that decision is approved. A waive may carry an expiry, which creates a follow-up ticket.
- An override never edits gwlint's output. The re-run still reports a waived rule, and the packet shows it as `waived` with the override's ref.

**Data model**
```sql
CREATE TABLE finding_overrides (
  id text PRIMARY KEY,              -- O-n per change, global ovr_…
  project_id text NOT NULL, change_number int NOT NULL, finding_id text NOT NULL,
  action text NOT NULL CHECK (action IN ('accept','reject','edit','waive','reopen','revert_all')),
  actor text NOT NULL, reason text, approver text, approved_at timestamptz, expires_at timestamptz,
  before_text text, after_text text,          -- secret-masked
  commit_sha text, rerun_id text, packet_version int NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now());
```
Every override is shown in the packet (who, when, why, ref O-n) and on the change's Conversation tab.

---

## 4. Customisable rules and guides

What exists today: the app's own rulebook `rules/api-guidelines.yaml` (id, severity, title, why, fix), which mirrors `internal/lint/rules.go`; and BOSS `engineering_standards` (free-text, project or global, `mandate_standard` / `list_standards`, policy:manage, **not versioned**). Neither has a P-level, an auto-fix policy, or a per-rule guide for the fixing agent.

**Design: a BOSS ruleset layered on the validator's own rulebook.**
```yaml
# .boss/rulesets/northgate.yaml  (in the repo, so the repo history is the version history)
ruleset: northgate
version: 3
validator: gwlint
rules:
  no-inlined-secrets:              {severity: P0, autofix: never,
     guide: "Delete the key if the route already authenticates another way; otherwise replace with api_key_secret_ref: vault://<tenant>/<name>. Never write a hash you computed."}
  cors-no-wildcard-with-credentials: {severity: P0, autofix: agent,
     guide: "Default to allow_credentials: false. Only keep credentials if the partner named exact origins in the brief."}
  no-sensitive-paths-exposed:      {severity: P0, autofix: human}   # removing a route is a product decision
  upstream-https-required:         {severity: P0, autofix: mechanical}
  tenant-rate-limit-required:      {severity: P1, autofix: agent, guide: "rps 50, burst 50 unless the brief names a quota."}
  rate-limit-required:             {severity: P1, autofix: mechanical}
  request-size-capped:             {severity: P1, autofix: mechanical}
  pii-fields-declared:             {severity: P2, autofix: agent}
  tenant-id-matches-filename:      {severity: P1, autofix: never}
custom:
  - id: lumen-no-refund-without-schema
    severity: P1
    check: agent                      # honest: no executable check; an agent reviewer applies it
    guide: "A POST route under /v1/payments/refunds must declare request_schema."
```
- Allowed edits: enable/disable a rule (a disabled rule's findings appear as `suppressed`, counted in the packet), change its P-level, set autofix to `mechanical`, `agent`, `human` or `never`, edit the guide text the fixing agent receives, and add a custom rule. A custom rule is marked **"agent-checked, not executed"** everywhere it appears.
- Editing in the console (Project › Settings › Validators) opens a small change titled "Ruleset northgate v4" with the diff. It takes effect when that change merges, and only people with `policy:manage` can merge it. **Lowering a P0, or switching a P0 from `never` to an auto-fix mode, requires a second approver.**
- One source of truth: the repo file. BOSS caches it by sha. `engineering_standards` stays for cross-project prose rules, and a ruleset guide may reference a standard by key.
- The packet's provenance line shows `ruleset northgate v3 (a1b2c3d)`. A run checked against an older ruleset than main has shows "ruleset changed since check · re-run".

---

## 5. Work panel changes (`apps/web/app/projects/work-panel.tsx`)
1. **Header links visible**: `Run #16 ↗` · `Build ↗` · `Change #9 ↗` sit as plain text links under the title, next to the workflow pill. The ⋯ menu keeps only "What it learned" and "Copy ids". Each link has a `data-testid`.
2. **The Built column reads the work's build.** `main_build` uses the root build of the change's lineage (the build that first opened change #9, `crun_58aecc16…`). Follow-ups are listed as "later" lines ("Follow-up: more tests · +172 lines of tests").
3. **For validation workflows, the Built column shows the validation summary, not prose**: `gwlint · 9 found → 4 auto · 3 agent · 1 you · 1 waived → 0 open`. It has P0 and P1 chips and a link to "See every fix" (§2). For other work, the column shows typed counts (files, tests, review findings by P-level). `summary_of` is no longer rendered.
4. Review comments are grouped P0/P1 first, with a red or amber rail. A P1 that is deferred to a follow-up shows "Deferred → follow-up #…" and the follow-up's link.
5. Change page: fix the 404 by resolving the run link from `finalize_ctx.workflow_run_id`, never from a `crun_` id.

---

## 6. Demo script (7 min, live install, Lumen Pay → Northgate)
| t | Screen | What happens / what is said |
|---|---|---|
| 0:00 | Work › New work | Lumen Pay's `lumen-pay.bad.yaml` arrives. "A partner sent a gateway config. Northgate's own linter is gwlint." Start API onboarding. |
| 0:40 | Work panel | The header shows Run #17 · Build · Change links. The flow lane goes live. |
| 1:20 | Validation tab: Findings | gwlint found **9**: **4 P0** (secret on L17, CORS * with credentials on L20, `/v1/admin/accounts` on L21, plaintext upstream on L35), **4 P1** and **1 P2**. Each rule's "why" is one click away. |
| 2:10 | Fixes column | 4 Auto fixes from `gwlint --fix` (rate limit, two size caps, https), each shown as a before/after diff. 3 Agent fixes following the ruleset guides (secret line removed because the route already uses jwt, CORS credentials off, tenant rate limit 50/50). 1 **Needs you**: the admin route. |
| 3:10 | Override | You **Accept** removing the admin route. You **Reject** the agent's CORS fix (the hunk reverts to the partner's lines), then **Edit** the line to `allow_origins: ["https://dashboard.lumenpay.example"]`. You **Waive** the P2 `pii-fields-declared` with the reason "no PII in query params, partner confirmed, KAN-12". |
| 4:10 | Re-run | It starts automatically and shows `gwlint exit 0 · 0 open · 1 waived`. Packet v4. |
| 4:40 | Rules & guides | Show the ruleset v3: P-levels and the CORS guide. Edit the guide text, and show that the edit opens "Ruleset v4" as a change rather than an invisible setting. |
| 5:20 | Merge card | The packet shows Ready, the P0 row is green, and the overrides O-1…O-3 are listed with who and why. Merge. |
| 6:00 | Staging | config_dir deploy to 2.29.2.202 (staging). |
| 6:30 | Contract tests | 12/12 pass, including `payments_rate_limited_11th → 429`, which covers V-04 (the auto-added rate limit). Close on the packet: every claim has a ref. |

Before recording: the remediation defect (#9) must be fixed, and a dry run must show a non-empty `fixes[]`.

---

## 7. Build plan (4 PRs)

**PR 1: Typed validation output + adapters (engine).** `boss.validation/v1` on the Validate step; `.boss/validators.yaml` loader; adapters `gwlint-json`, `sarif`, `eslint-json`, `ruff-json`; input-substitution guard; secret masking on `original` and `fixed`.
Acceptance: (a) on `examples/lumen-pay.bad.yaml` the step output has `before.findings` = 9 with the exact rule/line pairs above, `fixes` covers ≥4 auto, and `after.exit` = 0 or lists the open findings; (b) a run whose target equals `lumen-pay.good.yaml` fails with `input_substituted`; (c) `sk_live_…` never appears in the DB or the artifacts (grep test); (d) each adapter passes its fixture test.

**PR 2: Review packet v2 + validator (engine) and its rendering on the merge card and change page (web).** `build_packet()` from step outputs; `validate_packet()` with rules 1–6; `recommendation.packet`. The card renders §1.5. The `formatRecommendation` join and the raw `kgn_` ids are removed. Root-build title is used, without follow-up prefixes.
Acceptance: (a) an unreferenced claim, a count mismatch, or `fixed` without after-evidence each yields `invalid`, and Merge is disabled (unit + Playwright); (b) the card for change #9 shows the P1 `fixer.go:69` row above the fold at 1440 and at 400; (c) no element on the card contains more than 2 sentences (DOM test); (d) the title has no "Follow-up:" prefix.

**PR 3: Overrides + auto re-run.** `finding_overrides` table, the 3 endpoints, commit trailers, head_sha 409, P0-waive decision, re-run orchestration, packet versioning.
Acceptance: (a) reject restores the partner's exact bytes for that hunk (byte compare); (b) revert-all makes the target file sha equal the partner sha; (c) a P0 waive without an approver gives 403, and with one it creates a decision while the finding stays open until approved; (d) the re-run starts within 5 s and Merge is disabled until it is done; (e) a stale head_sha gives 409.

**PR 4: Rulesets + Validation tab + Work panel.** `.boss/rulesets/*.yaml`, the console editor that opens a ruleset change, the second-approver rule for P0 downgrades, the Validation tab (§2), the Work panel header links and Built column (§5), and the change-page 404 fix.
Acceptance: (a) changing `pii-fields-declared` to P1 makes the next packet block on it; (b) a guide edit appears verbatim in the fixing agent's prompt (prompt-capture test); (c) Playwright clicks every override control on the Validation tab at 1440 and 400; (d) the Work panel header shows Run/Build/Change links without opening ⋯; (e) the Built column for wfr_40229c5c… shows the gwlint summary and no `impl_summary` text; (f) `/changes/northgate-gateway/9` makes no 404 request.

No regression: the existing merge-card Playwright suite and the work-panel tests run on each PR. A full suite runs once before merging to main.
