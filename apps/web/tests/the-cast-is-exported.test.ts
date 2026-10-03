/**
 * docs/cast is the README's "Meet the cast": it must be exactly what the kit draws today.
 * With CAST_EXPORT=1 (`pnpm --filter boss-web cast:export`) this writes the files instead.
 */
import { existsSync, mkdirSync, readdirSync, readFileSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

import { castFiles } from "../scripts/export-cast";

const DIR = resolve(__dirname, "../../../docs/cast");

describe("the README cast is the kit's own drawing", () => {
  const files = castFiles();

  if (process.env.CAST_EXPORT === "1") {
    it("writes docs/cast", () => {
      mkdirSync(DIR, { recursive: true });
      for (const [name, body] of Object.entries(files)) writeFileSync(resolve(DIR, name), body);
      expect(readdirSync(DIR).length).toBeGreaterThanOrEqual(Object.keys(files).length);
    });
    return;
  }

  it("has a file for every shipped role and the grid", () => {
    expect(existsSync(DIR)).toBe(true);
    for (const name of Object.keys(files)) expect(existsSync(resolve(DIR, name)), name).toBe(true);
  });

  it("matches what avatarSvg draws now (run cast:export after changing the kit)", () => {
    for (const [name, body] of Object.entries(files)) {
      expect(readFileSync(resolve(DIR, name), "utf8"), name).toBe(body);
    }
  });
});
