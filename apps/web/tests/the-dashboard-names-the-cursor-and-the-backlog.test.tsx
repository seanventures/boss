/**
 * cursor() is "{event count}:{newest event timestamp}", or "{count}:0" when
 * there is no event. It is a change token, not a spend cursor.
 * Ack backlog counts inbound rows whose terminal flag is false.
 * The canary hint names passed, failed, and skipped. No canary stays "not run".
 * A visible sentence longer than six words is a paragraph. KPI values stay put.
 */
import { createRequire } from "node:module";
import { existsSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const CURSOR = "9:1710000000.654321";
const CURSOR_SENTENCE = "The cursor is the event count and the time of the newest event.";
const ACK_SENTENCE = "It counts inbound rows that are not terminal.";
const CANARY_HINT = "3 passed, 1 failed, 0 skipped";

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

function snapshot(canary: { status: string; passed: number; failed: number; skipped: number } | null, ackBacklog: number) {
  return {
    cursor: CURSOR,
    budgetState: "green",
    queue: { queued: 0, leased: 0, retryable: 0, succeeded: 0 },
    workload: { openTasks: 2, completedTasks: 5, pendingDecisions: 1, highRiskPending: 0 },
    incidents: { deadletters: 0, blockedOutbox: 0, deadletteredOutbox: 0 },
    providerHealth: { total: 1, healthy: 1 },
    canary,
    evals: null,
    spend: {
      capUsdMicros: 10_000_000,
      reservedUsdMicros: 1_000_000,
      actualUsdMicros: 9_000_000,
      runRate1hUsdMicros: 3_000_000,
    },
    ackBacklog,
  };
}

function cellForLabel(html: string, label: string): string {
  const cells = html.split('<div class="kpi-cell">').slice(1);
  for (const cell of cells) {
    const shown = /<div class="kpi-label">([^<]*)<\/div>/.exec(cell)?.[1];
    if (shown === label) return cell;
  }
  return "";
}

function words(text: string): string[] {
  return text.trim().split(/\s+/).filter(Boolean);
}

async function renderPanel(canary: { status: string; passed: number; failed: number; skipped: number } | null, ackBacklog: number): Promise<string> {
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
  return renderToStaticMarkup(React.createElement(Panel, { snapshot: snapshot(canary, ackBacklog) }));
}

describe("dashboard words for cursor, ack backlog, and canary", () => {
  it("says the cursor is the event count and the newest event time, beside the raw token", async () => {
    const html = await renderPanel({ status: "pass", passed: 3, failed: 1, skipped: 0 }, 4);
    const cursorAt = html.indexOf(`cursor ${CURSOR}`);
    const sentenceAt = html.indexOf(CURSOR_SENTENCE);
    expect(cursorAt).toBeGreaterThan(-1);
    expect(sentenceAt).toBeGreaterThan(cursorAt);
    expect(sentenceAt).toBeLessThan(html.indexOf('class="kpi-strip"'));
    expect(html).toMatch(new RegExp(`<p[^>]*>${CURSOR_SENTENCE}</p>`));
    expect(words(CURSOR_SENTENCE).length).toBeGreaterThan(6);
    expect(html).not.toContain("spend cursor");
  });

  it("says ack backlog counts inbound rows that are not terminal, and keeps the count", async () => {
    const html = await renderPanel({ status: "pass", passed: 3, failed: 1, skipped: 0 }, 4);
    const ackAt = html.indexOf("Ack backlog");
    const sentenceAt = html.indexOf(ACK_SENTENCE);
    expect(ackAt).toBeGreaterThan(-1);
    expect(sentenceAt).toBeGreaterThan(ackAt);
    expect(sentenceAt).toBeLessThan(html.indexOf("Eval harness"));
    expect(html).toMatch(new RegExp(`<p[^>]*>${ACK_SENTENCE}</p>`));
    expect(words(ACK_SENTENCE).length).toBeGreaterThan(6);
    expect(html).toContain("4 PENDING");
    expect(html).toContain("Ack backlog (no silent drops)");
  });

  it("names passed, failed, and skipped, and keeps not run when there is no canary", async () => {
    const withCanary = await renderPanel({ status: "pass", passed: 3, failed: 1, skipped: 0 }, 4);
    const canary = cellForLabel(withCanary, "Canary");
    expect(/<div class="kpi-value"[^>]*>([^<]*)<\/div>/.exec(canary)?.[1]).toBe("PASS");
    expect(/<div class="kpi-hint">([^<]*)<\/div>/.exec(canary)?.[1]).toBe(CANARY_HINT);
    expect(words(CANARY_HINT).length).toBeLessThanOrEqual(6);
    expect(canary).not.toContain("3/1/0");
    expect(/<div class="kpi-value"[^>]*>([^<]*)<\/div>/.exec(cellForLabel(withCanary, "Budget"))?.[1]).toBe("GREEN");
    expect(/<div class="kpi-value"[^>]*>([^<]*)<\/div>/.exec(cellForLabel(withCanary, "Open Tasks"))?.[1]).toBe("2");
    expect(/<div class="kpi-value"[^>]*>([^<]*)<\/div>/.exec(cellForLabel(withCanary, "1h new reservations, not settled receipts"))?.[1]).toBe("$3");

    const without = await renderPanel(null, 0);
    const quiet = cellForLabel(without, "Canary");
    expect(/<div class="kpi-value"[^>]*>([^<]*)<\/div>/.exec(quiet)?.[1]).toBe("—");
    expect(/<div class="kpi-hint">([^<]*)<\/div>/.exec(quiet)?.[1]).toBe("not run");
    expect(without).toContain("CLEAR");
    expect(without).toContain(`cursor ${CURSOR}`);
  });
});
