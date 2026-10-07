/**
 * BOSS HAS NO CONNECTION TO THE PRODUCT ITS SIGN-IN LAYOUT WAS MODELLED ON (operator rule).
 *
 * The sign-in redesign borrowed a layout only: no name, logo, copy, colour, asset or comment
 * from that product is in BOSS. This is the web suite's twin of
 * engine/tests/test_the_tree_names_no_design_reference.py: every tracked file (path and bytes)
 * is scanned, and the word is assembled so neither test ever finds itself.
 */
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { describe, expect, it } from "vitest";

const WORD = ["ka", "ap", "pu"].join("");
const REPO = resolve(process.cwd(), "../..");

describe("the tree names no design reference", () => {
  it("no tracked file names it, in its path or its contents", () => {
    const files = execFileSync("git", ["-C", REPO, "ls-files", "-z"], { maxBuffer: 256 * 1024 * 1024 }).toString().split("\0").filter(Boolean);
    expect(files.length).toBeGreaterThan(1000);
    const hits: string[] = [];
    for (const rel of files) {
      if (rel.toLowerCase().includes(WORD)) { hits.push(`${rel} (path)`); continue; }
      let data: Buffer;
      try { data = readFileSync(join(REPO, rel)); } catch { continue; }
      if (data.toString("latin1").toLowerCase().includes(WORD)) hits.push(rel);
    }
    expect(hits).toEqual([]);
  });
});
