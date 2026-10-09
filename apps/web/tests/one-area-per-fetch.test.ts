import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import { codeOnly } from "./_helpers/source";

/**
 * The companion to one-area-per-render: the console should not *fetch* the thirty-nine areas
 * it is not rendering either.
 *
 * Rendering one area fixed the payload (8.3 MB -> 25-235 KB) but not the wait, because the
 * server still opened every feed on every click. Measured against the live API, each of these
 * endpoints answers in 0.2-0.8s on its own — but the page fired roughly eighty-five of them at
 * once into a single API worker pool, and they queued behind each other. Time to first byte
 * came back 0.6-6.7s and varied run to run regardless of which area was asked for, which is the
 * signature of contention rather than of any one slow feed.
 *
 * So the gate that decides what renders now also decides what is requested. Each fetcher takes a
 * trailing `active` flag and, when it is false, returns the same empty value its unreachable
 * path already returned — so call-site positions are untouched. That matters: the wave below is
 * destructured positionally, and reordering it would silently swap two feeds.
 */
const PAGE = codeOnly("app/page.tsx");
const PAGE_RAW = readFileSync(join(process.cwd(), "app", "page.tsx"), "utf8");

/**
 * Every feed in the platform-ops wave, and the one area whose JSX reads it. Derived by finding
 * each destructured name's uses and the `shows(...)` block enclosing them; each of these had
 * zero uses outside such a block, which is what makes gating it safe.
 */
const PLATFORM_OPS_FEEDS: ReadonlyArray<readonly [string, string]> = [
  ["fetchIdentityProvidersFeed", "enterprise"],
  ["fetchIdentityRegistryFeed", "enterprise"],
  ["fetchOncallSreStatus", "reliability"],
  ["fetchReleaseStatus", "portfolio"],
  ["fetchReleaseBoard", "portfolio"],
  ["fetchReleases", "portfolio"],
  // The four token feeds left this wave: Govern reads its own data now (one GET /v1/govern,
  // then one read per tab opened), so a render fetches nothing for it (activity-govern §6.2).
  ["fetchKnowledgeGraphFeed", "reliability"],
  ["fetchLiveJobsFeed", "work"],
  ["fetchQaScorecardFeed", "reports"],
];

function waveSource(): string {
  const start = PAGE.indexOf("const platformOpsWaveP = Promise.all([");
  expect(start).toBeGreaterThan(-1);
  const end = PAGE.indexOf("]);", start);
  expect(end).toBeGreaterThan(start);
  return PAGE.slice(start, end);
}

describe("only the requested area is fetched", () => {
  it("the fetch gate is one seam, matching the render gate", () => {
    // Reversed along with `shows` — see one-area-per-render.test.ts for why. Every area is in
    // the DOM again, so every area's feeds must be fetched again or the sections render empty.
    // `needs` remains the single place that decides, so the two gates cannot drift apart: a
    // page that renders an area it did not fetch is worse than either trade.
    // REWRITTEN when areas went lazy (lib/area-loading.ts): both gates read the SAME set — the
    // requested area plus the ones this session opened — so they still cannot drift apart.
    // IDENTITY (S4): both gates also drop an area the viewer cannot open — the same `openable`
    // in both, so they still cannot drift apart, and a denied area is neither fetched nor drawn.
    expect(PAGE).toContain(
      "const needs = (...areas: string[]) => areas.some((area) => loadedAreas.has(area) && openable(area));");
    expect(PAGE).toContain("const shows = (area: string) => loadedAreas.has(area) && openable(area);");
  });

  it("every feed in the platform-ops wave is gated on the area that reads it", () => {
    const wave = waveSource();
    for (const [fn, area] of PLATFORM_OPS_FEEDS) {
      const call = new RegExp(`${fn}\\([^)]*\\)`).exec(wave)?.[0];
      expect(call, `${fn} is not called in the platform-ops wave`).toBeTruthy();
      expect(call, `${fn} fetches unconditionally`).toContain(`needs("${area}")`);
    }
  });

  it("the wave is still destructured positionally, so nothing may be reordered", () => {
    // The gate must not have become an excuse to drop or resequence a feed: the destructuring
    // above pairs names to positions, and a swap here is a silent wrong-data bug, not a crash.
    const wave = waveSource();
    const calledOrder = [...wave.matchAll(/(fetch[A-Za-z]+)\(/g)].map((m) => m[1]);
    expect(calledOrder).toEqual(PLATFORM_OPS_FEEDS.map(([fn]) => fn));
    // Two entries in the wave are already-resolved values from the assurance pack, not fetches.
    expect(wave).toContain("Promise.resolve(assuranceArea.securityScanners)");
    expect(wave).toContain("Promise.resolve(assuranceArea.latestSecurityScan)");
    const entries = PAGE_RAW.slice(
      PAGE_RAW.indexOf("const platformOpsWaveP = Promise.all(["),
    )
      .split("\n")
      .slice(1)
      .findIndex((l) => l.trim().startsWith("]"));
    expect(entries).toBe(11);
    // …and the destructuring names exactly as many, so each name still meets its own feed.
    const names = /const \[([^\]]*)\] = await platformOpsWaveP;/.exec(PAGE_RAW)?.[1]
      .split(",").map((n) => n.trim()).filter(Boolean) ?? [];
    expect(names.length).toBe(entries);
  });

  it("the main wave gates the feeds that only one area reads", () => {
    const start = PAGE.indexOf("await Promise.all([\n      fetchStatus(activeProjectId)");
    expect(start).toBeGreaterThan(-1);
    const wave = PAGE.slice(start, PAGE.indexOf("\n    ]);", start));
    // The expensive ones, named individually so a regression says which feed came unstuck.
    // fetchGitHubOperationsFeed alone fans out to twelve API calls and is read only by the
    // GitHub area; fetchAuditSummary serialises the whole audit trail and was measured at 13s.
    const mustGate: ReadonlyArray<readonly [string, string]> = [
      ["fetchGitHubOperationsFeed", "github"],
      ["fetchRepoActivity", "github"],
      ["fetchHealthSnapshotFeed", "ops"],
      ["fetchDiscoveriesFeed", "discovery"],
      ["fetchSandboxFeed", "sandboxes"],
      // Read by the Agents area now (Profile, Panels, New agent templates); Studio is gone.
      ["fetchAgentStudioFeed", "workforce"],
      ["fetchWorkOrchestrationFeed", "portfolio"],
      ["fetchAuditSummary", "ops"],
    ];
    for (const [fn, area] of mustGate) {
      const call = new RegExp(`${fn}\\((?:[^()]|\\([^()]*\\))*\\)`, "s").exec(wave)?.[0];
      expect(call, `${fn} is not called in the main wave`).toBeTruthy();
      expect(call, `${fn} fetches on every area`).toContain(`needs("${area}")`);
    }
  });

  it("Infra and Observability fetch nothing through the page (infra-observability spec §6.6)", () => {
    // The delivery pipeline, deployment health, secrets, deployments and the observability stack
    // (which SSHes to a host and probes Grafana) were fetched on EVERY render of EVERY area,
    // ungated, with the env PROJECT_ID. So were the ProviderOps fan-out (17 calls), the entitlement
    // feed, routing logs, InfraFabric, worker autoscaling and CI generators. The two areas read
    // their own ETag'd views client-side now; none of these may come back.
    for (const fn of [
      "getDeliveryPipelineAction", "getDeploymentHealthAction", "listSecretsAction",
      "listDeploymentsAction", "getObservabilityAction", "fetchProviderOpsFeed",
      "loadInfraAreaData", "fetchEntitlementFeed", "fetchRoutingLogFeed", "fetchInfraFabricFeed",
      "fetchWorkerAutoscalingStatus", "fetchObservabilitySuiteFeed", "fetchCICDStatus",
      "fetchInfrastructureResourceFeed",
    ]) {
      expect(PAGE, `${fn} is fetched by the page again`).not.toContain(`${fn}(`);
    }
  });

  it("the audit panel asks for counts, not for the whole audit trail", () => {
    // /v1/audit/export serialises every task, job, decision, artifact, receipt, reservation,
    // routing log and event so the caller can call len() on each list: 192 MB in 62 seconds
    // on a real project, to render eight integers. That was the whole Ops-area render time.
    const audit = codeOnly("lib/audit.ts");
    expect(audit).toContain("client.summarizeAuditTrail(");
    expect(audit).not.toContain("client.exportAuditTrail(");
  });

  it("the feeds still fetched everywhere are the ones read everywhere", () => {
    const start = PAGE.indexOf("await Promise.all([\n      fetchStatus(activeProjectId)");
    const wave = PAGE.slice(start + "await Promise.all([".length, PAGE.indexOf("\n    ]);", start));
    // One entry per position (split at top-level commas), so a feed wrapped in
    // `gate(needs(...), () => fetchX(...), ...)` is seen as the gated entry it is.
    const entries: string[] = [];
    let depth = 0;
    let current = "";
    for (const ch of wave) {
      if (ch === "(" || ch === "[" || ch === "{") depth += 1;
      if (ch === ")" || ch === "]" || ch === "}") depth -= 1;
      if (ch === "," && depth === 0) { entries.push(current); current = ""; continue; }
      current += ch;
    }
    if (current.trim()) entries.push(current);
    const ungated = entries
      .filter((entry) => !entry.includes("needs("))
      .map((entry) => entry.match(/(fetch[A-Za-z]+)\(/)?.[1])
      .filter((fn): fn is string => Boolean(fn))
      // Gated by a ternary outside its own parens rather than by an `active` argument,
      // so the per-call check above cannot see it. The assertion for it is in the test above.
      .filter((fn) => fn !== "fetchAuditSummary")
      // Passed a literal `false` (asserted below): it returns the empty shape and fetches nothing.
      .filter((fn) => fn !== "fetchPipelineData");
    expect(wave).toContain("fetchPipelineData(activeProjectId, false)");
    // These are read outside any `shows(...)` block — header counts, the nav rail, the "what
    // needs you" badge, who is acting — so they are genuinely needed no matter which area
    // renders. Adding to this list is a real cost paid by all forty areas; do it deliberately.
    // (console-latency 2026-10-09: decisions, tasks, meetings, artifacts, events, packs, roles,
    // skills, receipts, self-improvement, inbox and capabilities are read only inside the areas
    // their gates name, and GET /v1/auth/session was read by nothing — 16 of the Factory's 38
    // render calls.)
    expect(new Set(ungated)).toEqual(
      new Set([
        "fetchStatus",
        "fetchDeadletterFeed",
        "fetchMemberFeed",
        "fetchMetatronQuery",
        // Self-gating: returns null unless the URL names a run to trace.
        "fetchRunTrace",
      ]),
    );
    expect(wave).not.toContain("fetchAuthSession(");
  });

  it("each gated feed in the main wave is gated by the areas that read it", () => {
    // Derived by finding each destructured name's uses and the `shows(...)` blocks enclosing them
    // (none of these is read outside one); the gate must name every one of those areas.
    const GATES: ReadonlyArray<readonly [string, readonly string[]]> = [
      ["fetchDecisionFeed", ["work", "decisions"]],
      ["fetchTaskFeed", ["org", "meetings", "work", "decisions"]],
      ["fetchMeetingFeed", ["code_tasks", "meetings", "work"]],
      ["fetchArtifactFeed", ["ops", "work"]],
      ["fetchActivityFeed", ["ops", "work"]],
      ["fetchPackFeed", ["org"]],
      ["fetchRoleFeed", ["decisions", "meetings", "org"]],
      ["fetchSkillFeed", ["agent", "capabilities", "ops", "org", "skills"]],
      ["fetchReceiptFeed", ["ops", "reports", "work"]],
      ["fetchSelfImprovementFeed", ["ops"]],
      ["fetchInboxFeed", ["comms", "decisions"]],
      ["fetchCapabilities", ["capabilities"]],
      ["fetchWorkforce", ["workforce"]],
      ["fetchOrgChart", ["workforce"]],
      ["fetchReliabilityFeed", ["reliability"]],
      ["fetchAiSreCapabilities", ["ai-sre"]],
    ];
    for (const [fn, areas] of GATES) {
      const at = PAGE.indexOf(`() => ${fn}(`);
      expect(at, `${fn} is not behind gate(...)`).toBeGreaterThan(-1);
      const line = PAGE.slice(PAGE.lastIndexOf("gate(", at), at);
      for (const area of areas) expect(line, `${fn}'s gate omits ${area}`).toContain(`"${area}"`);
    }
  });

  it("a skipped feed returns the shape its unreachable path returns", () => {
    // The reason positions can stay put. If a fetcher ever returns null/undefined when
    // inactive instead of its own empty, the area that *does* read it still type-checks but
    // renders a crash — so the contract is stated here, at the seam.
    for (const file of [
      "lib/identity-providers.ts",
      "lib/oncall-sre.ts",
      "lib/release-planning.ts",
      "lib/observability-suite.ts",
      "lib/knowledge-graph.ts",
      "lib/live-jobs.ts",
      "lib/qa-scorecard.ts",
    ]) {
      const src = codeOnly(file);
      expect(src, `${file} has no inactive branch`).toMatch(/if \(!active\)/);
      expect(src, `${file} returns nothing when inactive`).not.toMatch(
        /if \(!active\) return (null|undefined);/,
      );
    }
  });
});
