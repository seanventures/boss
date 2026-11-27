/**
 * A preview's Open goes through the console's own gateway, never the sandbox's 127.0.0.1:<port>
 * (enterprise validation: three Infra previews at once, Open linked to the internal port). The
 * engine publicizes the row; the console still refuses a loopback that slips through.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

import { previewOpenHref } from "../lib/browser-links";

describe("a preview's Open", () => {
  it("keeps the gateway path or the public address the engine gave it", () => {
    expect(previewOpenHref("pv_1", "/v1/previews/pv_1/web/")).toBe("/v1/previews/pv_1/web/");
    expect(previewOpenHref("pv_1", "https://previews.example.test/v1/previews/pv_1/web/"))
      .toBe("https://previews.example.test/v1/previews/pv_1/web/");
  });

  it("never opens a sandbox's loopback address", () => {
    for (const internal of ["http://127.0.0.1:43053", "http://localhost:3000/", "http://[::1]:8080", "http://0.0.0.0:5173"]) {
      expect(previewOpenHref("pv 1", internal)).toBe("/v1/previews/pv%201/web/");
    }
  });

  it("Infra › Previews opens through it", () => {
    const src = readFileSync(join(__dirname, "../app/infra/previews.tsx"), "utf8");
    expect(src).toMatch(/href=\{previewOpenHref\(row\.id, row\.url\)\}/);
    expect(src).not.toMatch(/href=\{row\.url\}/);
  });
});
