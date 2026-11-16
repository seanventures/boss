/**
 * The 1h figure is SUM(budget_reservations.amount_usd_micros) for rows created
 * in the last hour. It is not settled receipts. The label has to say so.
 * The printed number stays that reservation sum.
 */
import { createRequire } from "node:module";
import { existsSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const RESERVATION_SUM_MICROS = 3_000_000;
const SETTLED_ACTUAL_MICROS = 9_000_000;
const LABEL = "1h new reservations, not settled receipts";

function nodePathDirs(): string[] {
  return (process.env.NODE_PATH ?? "").split(":").filter(Boolean);
}

function packageJsonFor(pkg: string): string {
  const starts = [
    ...nodePathDirs(),
    ...ancestorNodeModules(dirname(fileURLToPath(import.meta.url))),
  ];
  for (const dir of starts) {
    const candidate = join(dir, pkg, "package.json");
    if (existsSync(candidate)) return candidate;
  }
  throw new Error(`cannot find ${pkg} on NODE_PATH`);
}

function ancestorNodeModules(start: string): string[] {
  const out: string[] = [];
  let dir = start;
  for (let i = 0; i < 8; i += 1) {
    out.push(join(dir, "node_modules"));
    const parent = dirname(dir);
    if (parent === dir) break;
    dir = parent;
  }
  return out;
}

function load(spec: string): any {
  const pkg = spec.startsWith("@") ? spec.split("/").slice(0, 2).join("/") : spec.split("/")[0];
  return createRequire(packageJsonFor(pkg))(spec);
}

function snapshot() {
  return {
    cursor: "1:0",
    budgetState: "green",
    queue: { queued: 0, leased: 0, retryable: 0, succeeded: 0 },
    workload: { openTasks: 0, completedTasks: 0, pendingDecisions: 0, highRiskPending: 0 },
    incidents: { deadletters: 0, blockedOutbox: 0, deadletteredOutbox: 0 },
    providerHealth: { total: 1, healthy: 1 },
    canary: null,
    evals: null,
    spend: {
      capUsdMicros: 10_000_000,
      reservedUsdMicros: 1_000_000,
      actualUsdMicros: SETTLED_ACTUAL_MICROS,
      runRate1hUsdMicros: RESERVATION_SUM_MICROS,
    },
    ackBacklog: 0,
  };
}

function labelForValue(html: string, value: string): string {
  const cells = html.split('<div class="kpi-cell">').slice(1);
  for (const cell of cells) {
    const shown = /<div class="kpi-value"[^>]*>([^<]*)<\/div>/.exec(cell)?.[1];
    if (shown === value) return /<div class="kpi-label">([^<]*)<\/div>/.exec(cell)?.[1] ?? "";
  }
  return "";
}

async function renderPanel(): Promise<string> {
  const esbuild = load("esbuild");
  const entry = fileURLToPath(new URL("../app/dashboard-ui.tsx", import.meta.url));
  const built = await esbuild.build({
    entryPoints: [entry],
    bundle: true,
    write: false,
    format: "cjs",
    platform: "node",
    jsx: "automatic",
    nodePaths: nodePathDirs(),
    absWorkingDir: dirname(entry),
    logLevel: "silent",
  });
  const mod = { exports: {} as { DashboardPanel?: (props: { snapshot: ReturnType<typeof snapshot> }) => unknown } };
  const run = new Function("exports", "require", "module", built.outputFiles[0].text);
  run(mod.exports, createRequire(packageJsonFor("react")), mod);
  const React = load("react");
  const { renderToStaticMarkup } = load("react-dom/server");
  const Panel = mod.exports.DashboardPanel;
  if (!Panel) throw new Error("DashboardPanel did not export");
  return renderToStaticMarkup(React.createElement(Panel, { snapshot: snapshot() }));
}

describe("the one-hour spend figure", () => {
  it("says new reservations, not settled receipts, and prints the reservation sum", async () => {
    const html = await renderPanel();
    const label = labelForValue(html, "$3");
    expect(label).toBe(LABEL);
    expect(label.split(/\s+/).filter(Boolean).length).toBeLessThanOrEqual(6);
    expect(labelForValue(html, "$9")).toBe("");
  });
});
