import { readdirSync, statSync } from "node:fs";
import { join, relative } from "node:path";

import { describe, expect, it } from "vitest";

import { codeOnly } from "./_helpers/source";

/**
 * No operator screen tells the operator to call a REST endpoint.
 *
 * The console walk on boss-1 (2026-10-02) read, on screens an operator uses: "Register one via
 * POST /v1/mcp/servers", "via PUT /v1/token-efficiency/policies", "Source: `/v1/budget/status`"
 * with the backticks printed literally, "Vendor docs · GET /v1/identity-providers/okta",
 * "Ensure the API exposes GET /v1/cicd/catalog". An operator in a browser cannot act on any of
 * it. Each line became a button, a link to the screen that does it, or plain words about where
 * the data comes from.
 *
 * Enumerated, not listed: every .tsx under app/ and components/ is scanned with its comments
 * removed, so a new screen that does it again fails here. Code that BUILDS a URL to fetch is not
 * copy and is not matched — only an HTTP verb next to a path, or a path shown in a code chip or
 * between backticks.
 */
const ROOT = join(__dirname, "..");

function tsxUnder(dir: string): string[] {
  const out: string[] = [];
  for (const name of readdirSync(dir)) {
    const path = join(dir, name);
    if (name === "node_modules" || name.startsWith(".")) continue;
    if (statSync(path).isDirectory()) out.push(...tsxUnder(path));
    else if (name.endsWith(".tsx")) out.push(path);
  }
  return out;
}

const PATTERNS: Array<[string, RegExp]> = [
  ["an HTTP verb and an API path", /\b(GET|POST|PUT|PATCH|DELETE)\s+\/v1\//],
  ["an API path in a code chip", /<code[^>]*>[^<{]*\/v1\//],
  ["an API path in a code chip, by expression", /<code[^>]*>\s*\{?\s*["'`][^"'`]*\/v1\//],
  ["a literal backtick-quoted API path in copy", /`\/v1\/[^`$]*`/],
  ["a 'Source:' line naming an API path", /Source:\s*`?\/v1\//],
];

function offenders(): string[] {
  const out: string[] = [];
  for (const file of [...tsxUnder(join(ROOT, "app")), ...tsxUnder(join(ROOT, "components"))]) {
    const src = codeOnly(file);
    src.split("\n").forEach((line, i) => {
      for (const [what, pattern] of PATTERNS) {
        if (pattern.test(line)) out.push(`${relative(ROOT, file)}:${i + 1} ${what}: ${line.trim().slice(0, 120)}`);
      }
    });
  }
  return out;
}

describe("operator copy names actions and screens, not endpoints", () => {
  it("scans every screen", () => {
    expect(tsxUnder(join(ROOT, "app")).length).toBeGreaterThan(100);
  });

  it("finds no screen telling the operator to call the API", () => {
    expect(offenders(), offenders().join("\n")).toEqual([]);
  });

  it("catches the shapes that shipped", () => {
    // Pinned so the scan cannot quietly stop matching what it exists to catch.
    const shipped = [
      'No MCP servers registered. Register one via POST /v1/mcp/servers.',
      'Source: `/v1/budget/status` for project {activeProjectId}.',
      '<code>GET /v1/identity-providers/{idp.id}</code>',
      '<code className="mono">/v1/improvement-programs</code>',
    ];
    for (const line of shipped) {
      expect(PATTERNS.some(([, p]) => p.test(line)), line).toBe(true);
    }
    // And code that builds a fetch URL is not copy.
    expect(PATTERNS.some(([, p]) => p.test("fetch(`${API_URL}/v1/activity?${params}`)"))).toBe(false);
  });
});
