/**
 * THE PARITY TABLE (infra-observability spec §8): every capability the old Infra (`delivery`),
 * System Configuration (`?area=infra`) and Observability (`monitoring`) screens had, and where
 * it lives now. 144 rows; nothing silently dropped. Frozen in Phase 0.
 *
 * Each row is one assertion in tests/infra-observability-parity.test.tsx, which renders the new
 * areas against the shared fixtures (tests/_helpers/infra-fixtures.ts, obs-fixtures.ts):
 *
 *   testid    open `surface` at `url`, press each of `press` in order, then `testid` must render
 *   redirect  resolveLegacyArea(`from`) must carry every param in `to`
 *   removed   `absent` must not render anywhere on the surface (the reason is `why`)
 *   code      a source-level guard (`file` must / must not match `pattern`)
 *
 * Fixture ids every stream uses: hosts `h_ready` (Ready, 2 residents), `h_setup` (Setting up,
 * stage verify), `h_down` (Unreachable), `this` (This computer); targets `t_live` (stack, on
 * h_ready, Up), `t_down` (stack, on h_down, Down), `t_cloud` (registry, NOT PLACED); deploys
 * `stack:sd_wait` (awaiting approval), `stack:sd_live`, `stack:sd_failed` (has an earlier good
 * deploy), `registry:dr_1` (with a promotion and a changeset `cs_1`); preview `pv_live`; account
 * `acct_1`; resource `res_1` and droplet `drop_1`; pool `pool_1`; provider `aws`.
 */

export type ParityCheck =
  | { kind: "testid"; surface: "infra" | "obs" | "setup" | "skills"; url: string; press?: string[]; testid: string;
      /** A fixture variant the row needs (P-I-10: outward SSH off hides the host-write stages). */
      variant?: "blocked" }
  | { kind: "redirect"; from: string; to: Record<string, string> }
  | { kind: "removed"; surface: "infra" | "obs"; url: string; absent: string; why: string }
  | { kind: "code"; file: string; pattern: string; must: boolean; why: string };

export type ParityRow = { id: string; today: string; home: string; fate: "Kept" | "Merged" | "Moved" | "Removed" | "Changed" | "Added"; check: ParityCheck };

const fleet = (testid: string, url = "?view=fleet", press?: string[]): ParityCheck => ({ kind: "testid", surface: "infra", url, testid, press });
const host = (id: string, testid: string, press?: string[]): ParityCheck => fleet(testid, `?view=fleet&host=${id}`, press);
const target = (id: string, testid: string, press?: string[]): ParityCheck => fleet(testid, `?view=fleet&target=${id}`, press);
const deploys = (testid: string, url = "?view=deploys", press?: string[]): ParityCheck => ({ kind: "testid", surface: "infra", url, testid, press });
const deploy = (ref: string, testid: string, press?: string[]): ParityCheck => deploys(testid, `?view=deploys&deploy=${encodeURIComponent(ref)}`, press);
const cloud = (sub: string, testid: string, extra = "", press?: string[]): ParityCheck => ({ kind: "testid", surface: "infra", url: `?view=cloud&cloud=${sub}${extra}`, testid, press });
const add = (item: string, testid: string): ParityCheck => fleet(testid, "?view=fleet", ["infra-add", item]);
const obs = (testid: string, url = "?view=apps", press?: string[]): ParityCheck => ({ kind: "testid", surface: "obs", url, testid, press });
const app = (testid: string, press?: string[]): ParityCheck => obs(testid, "?view=apps&app=t_live", press);
const sources = (testid: string, press?: string[]): ParityCheck => obs(testid, "?view=sources", press);

export const PARITY: ParityRow[] = [
  // ── 8.1 Infra today (`delivery`) ──────────────────────────────────────────────────────────
  { id: "P-I-01", today: "Deployment Manager host list", home: "Fleet rack head + meta", fate: "Merged", check: fleet("inf-rack-meta") },
  { id: "P-I-02", today: "Deployment Manager starters + Open", home: "Header Ask BOSS starters", fate: "Kept", check: fleet("infra-ask-starter", "?view=fleet", ["infra-ask"]) },
  { id: "P-I-03", today: "This computer card", home: "This computer rack + host FACTS", fate: "Kept", check: fleet("inf-rack-this") },
  { id: "P-I-04", today: "Correct this computer's record", home: "Host drawer Correct record", fate: "Kept", check: host("this", "inf-host-correct") },
  { id: "P-I-05", today: "Add hardware: enroll command", home: "+ Add › Enroll machine", fate: "Kept", check: add("infra-add-enroll", "inf-enroll-sheet") },
  { id: "P-I-06", today: "Connect a provider (hardware)", home: "+ Add › Hardware account", fate: "Kept", check: add("infra-add-hwaccount", "inf-hwaccount-sheet") },
  { id: "P-I-07", today: "#hardware-manager anchor", home: "host=this", fate: "Kept", check: { kind: "code", file: "app/infra/infra-home.tsx", pattern: "#hardware-manager", must: true, why: "the old anchor opens This computer" } },
  { id: "P-I-08", today: "Infra h2 sentences", home: "Empty state / header counts", fate: "Removed", check: { kind: "removed", surface: "infra", url: "?view=fleet", absent: "Nothing deploys yet", why: "copy budget" } },
  { id: "P-I-09", today: "SandboxDeployFlagNotice", home: "Header line Sandbox deploys off", fate: "Kept", check: fleet("inf-sandbox-off") },
  { id: "P-I-10", today: "SSH-outward + host-write blockers", home: "SSH out is off line", fate: "Kept", check: { kind: "testid", surface: "infra", url: "?view=fleet", testid: "inf-ssh-off", variant: "blocked" } },
  { id: "P-I-11", today: "Host StageBar", home: "Stage line on rack + SETUP", fate: "Kept", check: fleet("inf-stage-line") },
  { id: "P-I-12", today: "I have a key that works → Adopt key", home: "Use my key → Adopt key", fate: "Kept", check: host("h_setup", "inf-stage-adopt") },
  { id: "P-I-13", today: "Generate a key for me", home: "Generate key", fate: "Kept", check: host("h_setup", "inf-stage-generate") },
  { id: "P-I-14", today: "store in select", home: "Store in", fate: "Kept", check: host("h_setup", "inf-stage-store") },
  { id: "P-I-15", today: "Copy public key / Copy line", home: "Copy key / Copy line", fate: "Kept", check: host("h_setup", "inf-stage-copy-line") },
  { id: "P-I-16", today: "Route select + command + Copy command", home: "Reach it by + Copy command", fate: "Kept", check: host("h_setup", "inf-stage-route") },
  { id: "P-I-17", today: "I've run it — verify", home: "I ran it", fate: "Kept", check: host("h_setup", "inf-stage-ran") },
  { id: "P-I-18", today: "Verify, Let BOSS install its own key", home: "Verify, Install key for me", fate: "Kept", check: host("h_setup", "inf-stage-verify") },
  { id: "P-I-19", today: "Install docker, Verify", home: "Install Docker", fate: "Kept", check: host("h_setup", "inf-stage-docker") },
  { id: "P-I-20", today: "Ready + Running here + Re-verify", home: "Rack Ready + residents; Re-verify", fate: "Kept", check: host("h_ready", "inf-host-reverify") },
  { id: "P-I-21", today: "Retire host (no confirm)", home: "Retire… sheet + typed confirm", fate: "Kept", check: host("h_ready", "inf-retire-confirm", ["inf-host-retire"]) },
  { id: "P-I-22", today: "Findings list", home: "Host drawer CHECKS", fate: "Kept", check: host("h_ready", "inf-host-checks") },
  { id: "P-I-23", today: "Add a VM form", home: "+ Add › Host sheet", fate: "Kept", check: add("infra-add-host", "inf-host-sheet") },
  { id: "P-I-24", today: "Stack target cards", home: "Resident tiles + target WHERE", fate: "Merged", check: fleet("inf-tile") },
  { id: "P-I-25", today: "Stack card Deploy", home: "Target drawer Deploy", fate: "Kept", check: target("t_live", "inf-target-deploy") },
  { id: "P-I-26", today: "Stack card recent deployments", home: "Target RECENT DEPLOYS", fate: "Kept", check: target("t_live", "inf-target-recent") },
  { id: "P-I-27", today: "Add a stack target", home: "App target › On a machine", fate: "Merged", check: add("infra-add-target", "inf-target-kind-machine") },
  { id: "P-I-28", today: "DeployRepository", home: "Deploys + Deploy sheet", fate: "Merged", check: deploys("inf-deploy-sheet", "?view=deploys", ["inf-deploy-new"]) },
  { id: "P-I-29", today: "StackDeploymentView status/close/failure", home: "Deploy drawer header, FAILED", fate: "Kept", check: deploy("stack:sd_failed", "inf-deploy-failed") },
  { id: "P-I-30", today: "What the manifest will do", home: "Deploy drawer PLAN", fate: "Kept", check: deploy("stack:sd_wait", "inf-deploy-plan") },
  { id: "P-I-31", today: "Steps progress list", home: "STEPS + row dots", fate: "Kept", check: deploy("stack:sd_live", "inf-deploy-steps") },
  { id: "P-I-32", today: "Log (log_tail)", home: "Deploy drawer LOG", fate: "Kept", check: deploy("stack:sd_live", "inf-deploy-log") },
  { id: "P-I-33", today: "Approve / Reject", home: "Card + row + drawer", fate: "Kept", check: deploy("stack:sd_wait", "inf-deploy-approve") },
  { id: "P-I-34", today: "Open app", home: "Open app ↗", fate: "Kept", check: deploy("stack:sd_live", "inf-deploy-open-app") },
  { id: "P-I-35", today: "Roll back to <sha>", home: "Row/card Roll back; drawer", fate: "Kept", check: deploys("inf-deploy-rollback") },
  { id: "P-I-36", today: "DeliveryTargetCard badges", home: "Tile line 2-3 + drawer header", fate: "Kept", check: fleet("inf-tile-state") },
  { id: "P-I-37", today: "repo · kind · ref", home: "Target WHERE", fate: "Kept", check: target("t_live", "inf-target-where") },
  { id: "P-I-38", today: "JourneySpine", home: "Target DEPLOY last deploy + Build", fate: "Merged", check: target("t_live", "inf-target-last-deploy") },
  { id: "P-I-39", today: "Foot: repo, domain", home: "WHERE Domain", fate: "Kept", check: target("t_live", "inf-target-domain") },
  { id: "P-I-40", today: "plan deployment on merge", home: "Deploy on merge toggle", fate: "Kept", check: target("t_live", "inf-target-auto") },
  { id: "P-I-41", today: "paused checkbox", home: "Paused toggle", fate: "Kept", check: target("t_live", "inf-target-paused") },
  { id: "P-I-42", today: "Ref input with refs", home: "Ref picker", fate: "Kept", check: target("t_live", "inf-target-ref") },
  { id: "P-I-43", today: "Plan deployment / redeployment", home: "Deploy (routed)", fate: "Kept", check: target("t_cloud", "inf-target-deploy") },
  { id: "P-I-44", today: "Host removal unavailable text", home: "Target Retire… or hidden", fate: "Changed", check: { kind: "removed", surface: "infra", url: "?view=fleet&target=t_live", absent: "Host removal unavailable", why: "no dead text" } },
  { id: "P-I-45", today: "Retired targets as text", home: "Show retired", fate: "Kept", check: fleet("inf-show-retired") },
  { id: "P-I-46", today: "ChangeSetNextStep", home: "Toast + NEEDS YOU / Open change", fate: "Kept", check: deploy("registry:dr_1", "inf-deploy-open-change") },
  { id: "P-I-47", today: "RecentDeployments rows", home: "Deploys rail", fate: "Kept", check: deploys("inf-deploy-row") },
  { id: "P-I-48", today: "steps & logs / plan details", home: "Deploy drawer", fate: "Kept", check: deploy("stack:sd_live", "inf-deploy-drawer") },
  { id: "P-I-49", today: "plan redeploy", home: "Drawer Redeploy", fate: "Kept", check: deploy("stack:sd_live", "inf-deploy-redeploy") },
  { id: "P-I-50", today: "DeployWizard", home: "+ Deploy sheet", fate: "Merged", check: deploys("inf-deploy-sheet", "?view=deploys", ["inf-deploy-new"]) },
  { id: "P-I-51", today: "OnboardWizard 11 fields", home: "App target sheet More", fate: "Merged", check: add("infra-add-target", "inf-target-more") },
  { id: "P-I-52", today: "Prepare host unavailable", home: "—", fate: "Removed", check: { kind: "removed", surface: "infra", url: "?view=fleet", absent: "Prepare host unavailable", why: "a dead button" } },
  { id: "P-I-53", today: "SecretsPanel", home: "Secrets sheet + target SECRETS", fate: "Kept", check: add("infra-add-secret", "inf-secrets-sheet") },
  { id: "P-I-54", today: "CICD API pill + vendor cards", home: "CI config sheet", fate: "Kept", check: deploys("inf-ci-vendor", "?view=deploys", ["inf-deploys-more", "inf-deploys-ci"]) },
  { id: "P-I-55", today: "Generate config + Validate YAML", home: "CI config sheet", fate: "Kept", check: deploys("inf-ci-generate", "?view=deploys", ["inf-deploys-more", "inf-deploys-ci"]) },
  { id: "P-I-56", today: "Pipeline run provenance", home: "CI config sheet", fate: "Kept", check: deploys("inf-ci-runs", "?view=deploys", ["inf-deploys-more", "inf-deploys-ci"]) },
  { id: "P-I-57", today: "DeploymentHealth (from delivery)", home: "Observability › Apps", fate: "Moved", check: obs("obs-pulse-row") },
  { id: "P-I-58", today: "ProviderOps ChangeSet links", home: "Deep links to Cloud › Changes", fate: "Kept", check: { kind: "code", file: "lib/infra-view.ts", pattern: "cloud=changes&changeset=", must: true, why: "links carry the change id" } },
  { id: "P-I-59", today: "notifyChanged(\"delivery\") bus", home: "notifyChanged(\"infra\") + usePolled kick", fate: "Kept", check: { kind: "code", file: "app/infra/*.tsx", pattern: "notifyChanged\\(\"infra\"\\)", must: true, why: "a write elsewhere re-polls the views" } },

  // ── 8.2 System Configuration today (`?area=infra`) ────────────────────────────────────────
  { id: "P-S-01", today: "MCP servers link", home: "Skills & MCP", fate: "Moved", check: { kind: "redirect", from: "?area=infra&infra_view=mcp", to: { area: "skills", tab: "library", lib: "mcp" } } },
  { id: "P-S-02", today: "ProviderOps h2 + status sentences", home: "—", fate: "Removed", check: { kind: "removed", surface: "infra", url: "?view=cloud", absent: "Elastic Resource Plane", why: "copy budget" } },
  { id: "P-S-03", today: "Community/edition/partial/read-only notices", home: "Chips + Some data missing", fate: "Kept", check: cloud("accounts", "inf-cloud-notice") },
  { id: "P-S-04", today: "5 summary tiles", home: "Cloud sub-view counts", fate: "Merged", check: cloud("accounts", "inf-cloud-seg") },
  { id: "P-S-05", today: "?infra_view= subnav", home: "cloud= (redirected)", fate: "Kept", check: { kind: "redirect", from: "?area=infra&infra_view=connections", to: { area: "infra", view: "cloud", cloud: "accounts" } } },
  { id: "P-S-06", today: "ProviderCatalog grid", home: "+ Account sheet catalog", fate: "Kept", check: cloud("accounts", "inf-account-catalog", "", ["inf-account-new"]) },
  { id: "P-S-07", today: "PasteAndConnect", home: "Paste key", fate: "Kept", check: cloud("accounts", "inf-account-paste", "", ["inf-account-new"]) },
  { id: "P-S-08", today: "ConnectionCreate (SecretRef)", home: "Use secret ref", fate: "Kept", check: cloud("accounts", "inf-account-secret-ref", "", ["inf-account-new"]) },
  { id: "P-S-09", today: "ProviderAccountTable", home: "Accounts table", fate: "Kept", check: cloud("accounts", "inf-cloud-table") },
  { id: "P-S-10", today: "AccountDrawer impact", home: "Account IMPACT", fate: "Kept", check: cloud("accounts", "inf-account-impact", "&account=acct_1") },
  { id: "P-S-11", today: "Credentials (write-only refs)", home: "Account CREDENTIALS", fate: "Kept", check: cloud("accounts", "inf-account-credentials", "&account=acct_1") },
  { id: "P-S-12", today: "Save policy / Test / Sync / Enable", home: "Account drawer actions", fate: "Kept", check: cloud("accounts", "inf-account-test", "&account=acct_1") },
  { id: "P-S-13", today: "Rotate credentials", home: "Rotate", fate: "Kept", check: cloud("accounts", "inf-account-rotate", "&account=acct_1") },
  { id: "P-S-14", today: "Revoke with typed confirm", home: "Revoke… (same confirm)", fate: "Kept", check: cloud("accounts", "inf-account-revoke", "&account=acct_1") },
  { id: "P-S-15", today: "Open audit", home: "Audit ↗", fate: "Kept", check: cloud("accounts", "inf-account-audit", "&account=acct_1") },
  { id: "P-S-16", today: "Resources ScopeFilters", home: "Resources scope chips", fate: "Kept", check: cloud("resources", "inf-cloud-filters") },
  { id: "P-S-17", today: "Resources table + ResourceDrawer", home: "Resources table + drawer", fate: "Kept", check: cloud("resources", "inf-resource-drawer", "&resource=res_1") },
  { id: "P-S-18", today: "HealthView provider status", home: "Capacity API health", fate: "Kept", check: cloud("capacity", "inf-cloud-api-health") },
  { id: "P-S-19", today: "HealthView pools + plans", home: "Capacity table + pool drawer", fate: "Merged", check: cloud("capacity", "inf-pool-drawer", "&pool=pool_1") },
  { id: "P-S-20", today: "FinOps distribution bars", home: "Spend bars", fate: "Kept", check: cloud("spend", "inf-spend-bars") },
  { id: "P-S-21", today: "FinOps anomalies + reconciliation", home: "Spend table + provider drawer", fate: "Kept", check: cloud("spend", "inf-spend-anomalies") },
  { id: "P-S-22", today: "Footer + Audit & events", home: "Audit ↗ in Cloud", fate: "Kept", check: cloud("accounts", "inf-cloud-audit") },
  { id: "P-S-23", today: "InfraFabric multi-cloud table", home: "+ Account catalog (adapter, categories, skills)", fate: "Merged", check: cloud("accounts", "inf-catalog-adapter", "", ["inf-account-new"]) },
  { id: "P-S-24", today: "InfraFabric elastic tiles", home: "Capacity counts", fate: "Merged", check: cloud("capacity", "inf-capacity-counts") },
  { id: "P-S-25", today: "InfraFabric provider usage", home: "Spend provider drawer", fate: "Merged", check: cloud("spend", "inf-provider-drawer", "&account=aws") },
  { id: "P-S-26", today: "WorkerAutoscaling queue", home: "Capacity queue line", fate: "Kept", check: cloud("capacity", "inf-capacity-queue") },
  { id: "P-S-27", today: "Pools desired vs actual", home: "Capacity table", fate: "Merged", check: cloud("capacity", "inf-cloud-table") },
  { id: "P-S-28", today: "Autoscaling recent history", home: "Pool drawer history", fate: "Kept", check: cloud("capacity", "inf-pool-history", "&pool=pool_1") },
  { id: "P-S-29", today: "Sync all providers", home: "Sync all", fate: "Kept", check: cloud("resources", "inf-resources-sync-all") },
  { id: "P-S-30", today: "Run Resource Janitor", home: "Clean up…", fate: "Kept", check: cloud("resources", "inf-resources-cleanup") },
  { id: "P-S-31", today: "Resource Explorer rows", home: "Resources table", fate: "Merged", check: cloud("resources", "inf-cloud-table") },
  { id: "P-S-32", today: "Register worker pool (micros)", home: "+ Pool (dollars)", fate: "Kept", check: cloud("capacity", "inf-capacity-add-pool") },
  { id: "P-S-33", today: "Run autoscaler review", home: "Review scaling", fate: "Kept", check: cloud("capacity", "inf-capacity-review") },
  { id: "P-S-34", today: "Pool + scale-plan rows", home: "Capacity Plan column", fate: "Kept", check: cloud("capacity", "inf-capacity-plan") },
  { id: "P-S-35", today: "Create ChangeSet (JSON, micros)", home: "+ Change (validated, dollars)", fate: "Kept", check: cloud("changes", "inf-change-new") },
  { id: "P-S-36", today: "ChangeSet Approve / Apply", home: "Changes table + drawer", fate: "Kept", check: cloud("changes", "inf-change-approve", "&changeset=cs_1") },
  { id: "P-S-37", today: "Save environment policy + list", home: "Deploys ⋯ Rules", fate: "Moved", check: deploys("inf-rules-sheet", "?view=deploys", ["inf-deploys-more", "inf-deploys-rules"]) },
  { id: "P-S-38", today: "Register deployment target", home: "App target › Cloud account", fate: "Merged", check: add("infra-add-target", "inf-target-kind-cloud") },
  { id: "P-S-39", today: "Plan deployment run (micros, JSON)", home: "+ Deploy › More", fate: "Merged", check: deploys("inf-deploy-more", "?view=deploys", ["inf-deploy-new"]) },
  { id: "P-S-40", today: "Registry target list", home: "Fleet NOT PLACED + racks", fate: "Merged", check: fleet("inf-unplaced") },
  { id: "P-S-41", today: "5 runs + Run health checks", home: "Rail + drawer Run check", fate: "Kept", check: deploy("registry:dr_1", "inf-deploy-health-check") },
  { id: "P-S-42", today: "DeploymentPromotionTimeline", home: "Deploy drawer PROMOTION", fate: "Kept", check: deploy("registry:dr_1", "inf-deploy-promotion") },
  { id: "P-S-43", today: "Health, Drift, and Receipts", home: "Change drawer drift + receipts", fate: "Merged", check: cloud("changes", "inf-change-drift", "&changeset=cs_1") },
  { id: "P-S-44", today: "Cost and Reconciliation Ledger", home: "Spend", fate: "Merged", check: cloud("spend", "inf-cloud-table") },
  { id: "P-S-45", today: "Infrastructure Resources (droplets)", home: "Resources rows of kind Droplet", fate: "Kept", check: cloud("resources", "inf-resource-droplet") },
  { id: "P-S-46", today: "AI Capacity entitlement sources", home: "Repo and models", fate: "Moved", check: { kind: "testid", surface: "setup", url: "?area=setup", testid: "setup-entitlements" } },
  { id: "P-S-47", today: "Official sessions", home: "Repo and models", fate: "Moved", check: { kind: "testid", surface: "setup", url: "?area=setup", testid: "setup-entitlement-session" } },
  { id: "P-S-48", today: "Model route decisions + filters", home: "Observability › Models › ROUTING", fate: "Moved", check: obs("obs-routing", "?view=models") },
  { id: "P-S-49", today: "Broker skills", home: "Skills & MCP (link)", fate: "Moved", check: { kind: "testid", surface: "setup", url: "?area=setup", testid: "setup-broker-skills-link" } },
  { id: "P-S-50", today: "fetchReportSummary gated on infra", home: "—", fate: "Removed", check: { kind: "code", file: "app/page.tsx", pattern: "fetchReportSummary\\(activeProjectId, needs\\([^)]*\"infra\"", must: false, why: "a wasted fetch" } },
  { id: "P-S-51", today: "/v2/infra", home: "Infra (redirect)", fate: "Merged", check: { kind: "code", file: "app/legacy-areas.ts", pattern: "\"/v2/infra\": \\{ area: \"infra\" \\}", must: true, why: "the v2 screen redirects" } },

  // ── 8.3 Observability today (`monitoring`) ────────────────────────────────────────────────
  { id: "P-O-01", today: "h2 Not configured / Health of…", home: "Empty state + Infra ↗", fate: "Removed", check: { kind: "removed", surface: "obs", url: "?view=apps", absent: "Not configured", why: "copy budget" } },
  { id: "P-O-02", today: "?watch=app|env deep link", home: "app drawer", fate: "Kept", check: { kind: "redirect", from: "?area=monitoring&watch=invoice%7Cprod", to: { area: "observability", view: "apps", watch: "invoice|prod" } } },
  { id: "P-O-03", today: "Monitoring is not set up yet", home: "Apps empty / Sources Needs a machine", fate: "Kept", check: sources("obs-stack-state") },
  { id: "P-O-04", today: "Health title counts", home: "Header counts + ordering", fate: "Kept", check: obs("obs-head-counts") },
  { id: "P-O-05", today: "Per target dot, app, env", home: "Pulse row", fate: "Kept", check: obs("obs-pulse-row") },
  { id: "P-O-06", today: "URL ↗ / planned", home: "Drawer Open app ↗", fate: "Kept", check: app("obs-app-open") },
  { id: "P-O-07", today: "status since / never probed", home: "Row down <dur> / since", fate: "Kept", check: app("obs-app-since") },
  { id: "P-O-08", today: "Open dashboard / no dashboard", home: "Drawer Dashboard ↗", fate: "Kept", check: app("obs-app-dashboard") },
  { id: "P-O-09", today: "uptime 24h, 7d", home: "Row uptime + drawer", fate: "Kept", check: obs("obs-pulse-uptime") },
  { id: "P-O-10", today: "latency, p95", home: "Row p95 + chart", fate: "Kept", check: obs("obs-pulse-p95") },
  { id: "P-O-11", today: "build, deployed, checked", home: "Drawer facts", fate: "Kept", check: app("obs-app-facts") },
  { id: "P-O-12", today: "HealthHistory toggle", home: "Strip tooltip + chart", fate: "Kept", check: app("obs-latency-chart") },
  { id: "P-O-13", today: "History facts", home: "Drawer facts + chart", fate: "Kept", check: app("obs-app-facts") },
  { id: "P-O-14", today: "HealthGraph SVG", home: "Pulse strip + latency chart", fate: "Kept", check: obs("obs-pulse-strip") },
  { id: "P-O-15", today: "last_error", home: "INCIDENTS first error + Show error", fate: "Kept", check: app("obs-app-incidents") },
  { id: "P-O-16", today: "Incidents list", home: "Drawer INCIDENTS", fate: "Kept", check: app("obs-app-incidents") },
  { id: "P-O-17", today: "Scraped but not health-checked", home: "NOT WATCHED + Not watched chip", fate: "Kept", check: obs("obs-not-watched") },
  { id: "P-O-18", today: "Observability stack state", home: "Sources STACK", fate: "Kept", check: sources("obs-stack") },
  { id: "P-O-19", today: "Deploy / Redeploy stack", home: "Deploy stack / Redeploy", fate: "Kept", check: sources("obs-stack-deploy") },
  { id: "P-O-20", today: "Open Grafana", home: "Grafana ↗", fate: "Kept", check: sources("obs-stack-grafana") },
  { id: "P-O-21", today: "Scraped table", home: "SCRAPED APPS", fate: "Kept", check: sources("obs-scraped") },
  { id: "P-O-22", today: "Container status", home: "STACK container chips", fate: "Kept", check: sources("obs-stack-containers") },
  { id: "P-O-23", today: "No target to host the stack", home: "Needs a machine + Add host ↗", fate: "Kept", check: { kind: "code", file: "app/observability/sources.tsx", pattern: "Needs a machine", must: true, why: "the no-host state" } },
  { id: "P-O-24", today: "Ask BOSS starters + Open BOSS here", home: "Header Ask BOSS", fate: "Kept", check: obs("obs-ask-starter", "?view=apps", ["obs-ask"]) },
  { id: "P-O-25", today: "Suite stats", home: "Sources block counts", fate: "Merged", check: sources("obs-sources-list") },
  { id: "P-O-26", today: "GapBanner", home: "Sources GAPS", fate: "Kept", check: sources("obs-gaps") },
  { id: "P-O-27", today: "Registered sources", home: "Sources SOURCES", fate: "Kept", check: sources("obs-sources-list") },
  { id: "P-O-28", today: "Model spend on Govern/Evidence/v2", home: "Observability › Models", fate: "Merged", check: obs("obs-models-table", "?view=models") },
  { id: "P-O-29", today: "Recommended OSS stack cards", home: "Add tools… catalog", fate: "Moved", check: sources("obs-tools-catalog", ["obs-add-tools"]) },
  { id: "P-O-30", today: "Recommend for workload", home: "Add tools… Workload + Recommend", fate: "Kept", check: sources("obs-tools-recommend", ["obs-add-tools"]) },
  { id: "P-O-31", today: "Health-check (dry-run)", home: "Check config", fate: "Kept", check: sources("obs-check-config") },
  { id: "P-O-32", today: "Control plane unreachable", home: "Can't reach BOSS", fate: "Kept", check: { kind: "code", file: "app/infra/kit.tsx", pattern: "Can&apos;t reach BOSS", must: true, why: "one unreachable line" } },
  { id: "P-O-33", today: "router.refresh as the only update", home: "usePolled per tab", fate: "Changed", check: { kind: "code", file: "app/observability/*.tsx", pattern: "router\\.refresh|refreshConsole|requestRefresh", must: false, why: "no console refresh" } },
  { id: "P-O-34", today: "HostAdviceBanner on Workflows/Factory", home: "Also on System", fate: "Added", check: obs("obs-system-advice", "?view=system") },
];
