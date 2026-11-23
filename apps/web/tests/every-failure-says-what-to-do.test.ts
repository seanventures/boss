// @vitest-environment happy-dom
/**
 * EVERY FAILURE SAYS WHAT HAPPENED AND WHAT TO DO (BOSSAPP-1428).
 *
 * A customer kept hitting failures whose message named a symptom, not the cause or the next
 * step: the Needs-you card said "Couldn't do that" with the server's reason only in a tooltip,
 * Setup rows said "Could not change it", the process editor said "Save failed.". Each one made
 * the real cause slower to find, because the person could not tell us what broke.
 *
 * Every console failure line is now built by couldNot() (lib/action-result.ts). This file:
 *   1. scans app/**, lib/**, components/** and fails on a vague failure literal;
 *   2. proves the scanner catches a planted vague line (so it cannot rot into a no-op);
 *   3. checks couldNot's sentences;
 *   4. renders the attention card and checks the server's reason is ON SCREEN, not only a tooltip.
 */
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative } from "node:path";

import { createElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { NO_REASON, couldNot, describeActionError, failureSentence } from "../lib/action-result";
import { board, failed } from "./_helpers/live-board-fixture";
import { $, click, mount, stateOf, unmount } from "./_helpers/live-render";

const post = vi.fn<(action: string, payload: Record<string, unknown>, key?: string) => Promise<unknown>>();
vi.mock("../app/workflows/delivery-api", async (original) => ({
  ...(await original<typeof import("../app/workflows/delivery-api")>()),
  post: (action: string, payload: Record<string, unknown>, key?: string) => post(action, payload, key),
}));
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }) }));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const WEB = join(__dirname, "..");
const ROOTS = ["app", "lib", "components"];

/** A failure message that says nothing, wherever it appears as a whole string or JSX text. */
const STRONG = [
  "Couldn't do that", "Could not change it", "Something went wrong", "That did not work",
  "Save failed", "Could not save", "Could not start it", "Couldn't load", "Could not load",
  "An error occurred", "Unknown error", "Request failed", "Action failed",
];
/** Fine as a button label or a status chip; vague only as the error message itself. */
const WEAK = ["Try again", "Failed", "Error"];

const norm = (s: string) => s.replace(/[\u2018\u2019]|&apos;|&#39;/g, "'").trim();
const STRONG_RE = new RegExp(
  `^(?:${STRONG.map((p) => p.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|")})[.!]?(?:\\s*Try again[.!]?)?$`, "i");
// Case-sensitive: lowercase "failed"/"error" are status and toast-kind codes ("error" in r).
const WEAK_RE = new RegExp(`^(?:${WEAK.join("|")})[.!]?$`);
/** The text just before a literal that makes it the error message. */
const ERROR_SLOT = [
  /\?\?\s*$/, /\|\|\s*$/, /\bsetErr\w*\(\s*$/, /\btoast\w*\(\s*$/,
  /\bshowToast\(\s*["']error["']\s*,\s*$/, /\bnew Error\(\s*$/, /instanceof Error\b[^;]*\?[^;]*:\s*$/,
];

/** Source with comments blanked but every newline kept, so a hit has its real line number. */
export function stripComments(src: string): string {
  return src
    .replace(/\/\*[\s\S]*?\*\//g, (m) => m.replace(/[^\n]/g, " "))
    .replace(/(^|[^:"'`\\])\/\/[^\n]*/g, "$1");
}

export function vagueLines(src: string): { line: number; text: string }[] {
  const hits: { line: number; text: string }[] = [];
  const lines = stripComments(src).split("\n");
  lines.forEach((code, i) => {
    // String literals (no ${} in a template: a template with a value carries its reason).
    const lit = /(["'`])((?:\\.|(?!\1)[^\\\n])*)\1/g;
    for (let m = lit.exec(code); m; m = lit.exec(code)) {
      const body = norm(m[2]);
      if (m[1] === "`" && body.includes("${")) continue;
      const before = code.slice(0, m.index);
      if (STRONG_RE.test(body) || (WEAK_RE.test(body) && ERROR_SLOT.some((re) => re.test(before)))) {
        hits.push({ line: i + 1, text: code.trim() });
      }
    }
    // JSX text: <span title={why}>Could not load</span>, the reason only in a tooltip.
    const jsx = />([^<>{}\n]+)</g;
    for (let m = jsx.exec(code); m; m = jsx.exec(code)) {
      if (STRONG_RE.test(norm(m[1]))) hits.push({ line: i + 1, text: code.trim() });
    }
  });
  return hits;
}

function sources(dir: string): string[] {
  const out: string[] = [];
  for (const name of readdirSync(dir)) {
    if (name === "node_modules" || name.startsWith(".")) continue;
    const path = join(dir, name);
    if (statSync(path).isDirectory()) out.push(...sources(path));
    else if (/\.(ts|tsx)$/.test(name) && !/\.(test|spec)\.tsx?$/.test(name) && !name.endsWith(".d.ts")) out.push(path);
  }
  return out;
}

describe("no console failure message is vague", () => {
  const files = ROOTS.flatMap((r) => sources(join(WEB, r)));

  it("scans the whole console", () => {
    expect(files.length).toBeGreaterThan(300);
  });

  it("every failure literal names what failed and why, or what to do", () => {
    const offenders = files.flatMap((f) =>
      vagueLines(readFileSync(f, "utf8")).map((h) => `${relative(WEB, f)}:${h.line}  ${h.text.slice(0, 140)}`));
    expect(offenders, `Vague failure messages — build them with couldNot() (lib/action-result.ts):\n${offenders.join("\n")}`)
      .toEqual([]);
  });

  it("catches a planted vague line (the scanner is not a no-op)", () => {
    const planted = [
      `setError(r.error ?? "Couldn't do that");`,
      `toast(error instanceof Error ? error.message : "Could not change it", "error");`,
      `setError(saved.error ?? "Save failed.");`,
      `setError(words(e, "That did not work."));`,
      `<span title={failed}>Could not load</span>`,
      `<span title={why}>Couldn&apos;t load</span>`,
      `throw new Error("Failed");`,
      `setErr(r.error || "Try again.");`,
      `showToast("error", "Error");`,
    ];
    for (const line of planted) expect(vagueLines(line), line).toHaveLength(1);
    // A comment explaining the old text, a button label and a status chip are not messages.
    const fine = [
      `// it used to say "Couldn't do that"`,
      `<button>Try again</button>`,
      `const chip = status === "failed" ? "Failed" : "Done";`,
      `setError(couldNot("save Build speed", reasonOf(e), "Nothing changed."));`,
      "setError(`Could not load roles: ${why}`);",
    ];
    for (const line of fine) expect(vagueLines(line), line).toEqual([]);
  });
});

describe("couldNot says what, why and what next", () => {
  it("puts the server's reason inline", () => {
    expect(couldNot("retry step 2", "the step is still running"))
      .toBe("Couldn't retry step 2: the step is still running.");
    expect(couldNot("save Build speed", "Docker isn't answering.", "Nothing changed."))
      .toBe("Couldn't save Build speed: Docker isn't answering. Nothing changed.");
  });

  it("with no reason, says so and names a next step and the place to look", () => {
    const s = couldNot("retry 'Build'");
    expect(s).toMatch(/^Couldn't retry 'Build': BOSS gave no reason\./);
    expect(s).toContain("Reload the page");
    expect(s).toContain("Activity log");
    expect(couldNot("save Build speed", "", "Nothing changed."))
      .toBe("Couldn't save Build speed: BOSS gave no reason. Nothing changed.");
  });

  it("says a bare API code in words and never repeats a server's own 'Couldn't'", () => {
    expect(couldNot("retry step 1", "forbidden")).toBe("Couldn't retry step 1: You don't have permission to do this. Ask an Admin.");
    expect(couldNot("retry step 1", "role_review_missing")).not.toMatch(/role_review_missing/);
    expect(couldNot("retry step 1", "Couldn't reach the worker.")).toBe("Couldn't reach the worker.");
  });

  it("the no-reason fallbacks of the older helpers say the same", () => {
    expect(failureSentence("")).toBe(NO_REASON);
    expect(describeActionError(undefined)).toBe(NO_REASON);
    expect(NO_REASON).toContain("Activity log");
    expect(NO_REASON).not.toMatch(/Try again\.$/);
  });
});

describe("the attention card shows the server's reason on screen", () => {
  beforeEach(() => { post.mockReset(); });
  afterEach(() => { unmount(); });

  it("a refused retry reads 'Couldn't retry step N: <reason>.' in the visible line", async () => {
    post.mockResolvedValue({ ok: false, error: "the step's worker is still busy" });
    const { LiveBoardView } = await import("../app/agents/live-board");
    const noop = () => {};
    const body = board({ attention: [failed({ answers: [
      { key: "retry_step", label: "Retry", enabled: true, reason: null, step_id: "build" },
    ] })] });
    await mount(createElement(LiveBoardView, { projectId: "p1", actor: "ceo", live: stateOf(body),
      onOpenMap: noop, onActivity: noop, onDirectory: noop, onNewAgent: noop }));
    await click($('[data-testid="ag-att-answer-retry_step"]'));
    const err = $('[data-testid="ag-att-error"]')!;
    expect(err.textContent).toMatch(/^Couldn't retry step \d+: the step's worker is still busy\.$/);
    // The reason is the visible line's own words, so it is the server's text, not ours.
    expect(err.hasAttribute("data-user-text")).toBe(true);
  }, 20_000); // first import of the live board is slow on a loaded machine
});
