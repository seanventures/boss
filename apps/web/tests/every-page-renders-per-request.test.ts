/**
 * EVERY PAGE RENDERS PER REQUEST (round 3 role walk). middleware.ts gives every page a CSP whose
 * script-src carries a fresh nonce, and Next stamps that nonce on its scripts only when it renders
 * the page for the request. /launch had nothing dynamic, Next prerendered it, and the browser
 * refused all of its scripts for every role. A page declares force-dynamic, or reads the request
 * (headers, cookies, searchParams), which makes it dynamic too.
 */
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const APP = join(__dirname, "..", "app");

function pages(dir: string): string[] {
  return readdirSync(dir).flatMap((n) => {
    const p = join(dir, n);
    return statSync(p).isDirectory() ? pages(p) : n === "page.tsx" ? [p] : [];
  });
}

describe("pages and the CSP nonce", () => {
  it("no page can be prerendered", () => {
    const fixed = pages(APP).filter((p) => {
      const s = readFileSync(p, "utf8");
      return !/export const dynamic = "force-dynamic"/.test(s) && !/\b(headers|cookies)\(\)|searchParams/.test(s);
    });
    expect(fixed.map((p) => p.slice(APP.length))).toEqual([]);
  });
});
