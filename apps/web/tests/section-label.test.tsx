// @vitest-environment happy-dom
/**
 * One green label for Setup, the agent Passport and the Kit (agent-loadout spec §5, D-3): the
 * component renders `.section-label[data-level]`, ONE CSS rule styles it, and Setup's rows use it.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import React from "react";
import { afterEach, describe, expect, it } from "vitest";

import { fakeApi, mountBoard, type Mounted } from "./_helpers/setup-board";
import { board } from "./_helpers/setup-fixtures";

let mounted: Mounted | null = null;
afterEach(() => { mounted?.unmount(); mounted = null; document.body.innerHTML = ""; });

describe("the shared section label", () => {
  it("labels every Setup row", async () => {
    fakeApi(board());
    mounted = await mountBoard(board());
    const rows = [...document.body.querySelectorAll('[data-testid^="su-row-"]')];
    expect(rows.length).toBeGreaterThan(3);
    for (const row of rows) {
      // A part of another row (rc9: the model sources inside Models) shares that row's label.
      if (row.parentElement?.closest('[data-testid^="su-row-"]')) continue;
      const label = row.querySelector(".su-row-label .section-label");
      expect(label?.getAttribute("data-level"), row.getAttribute("data-testid") ?? "").toBe("2");
    }
    expect(document.body.querySelector(".su-row-label strong")).toBeNull();
  });

  it("is styled by one rule: Setup's green, untracked, sentence case", () => {
    const css = readFileSync(join(__dirname, "..", "app", "globals.css"), "utf8");
    const rules = [...css.matchAll(/([^{}]+)\{([^}]*)\}/g)]
      .filter(([, selector]) => selector.split(",").some((s) => s.trim() === ".section-label"));
    expect(rules.length).toBe(1);
    const [, selector, body] = rules[0];
    expect(selector).toContain(".section-head h2.area-name");
    expect(body).toContain("color: var(--accent-text)");
    expect(body).toMatch(/letter-spacing:\s*0\s*;/);
    expect(body).not.toContain("0.16em");
    expect(body).toContain("text-transform: none");
    expect(body).not.toContain("uppercase");
  });
});
