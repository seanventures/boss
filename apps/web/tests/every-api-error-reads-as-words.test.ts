import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative } from "node:path";

import { describe, expect, it } from "vitest";

import { apiErrorText } from "../lib/api-error";
import { codeOnly } from "./_helpers/source";

/**
 * Every API refusal reaches the operator as words, never "[object Object]".
 *
 * Console walk on boss-1, 2026-10-02: the route handlers under app/api forwarded FastAPI's
 * `detail` as-is. A validation error's detail is an ARRAY of {loc, msg} objects; the screens
 * render errors with String(...), so the operator read "[object Object]". Every route now builds
 * its error text with apiErrorText (lib/api-error.ts). Enumerated: every route.ts under app/api
 * is scanned, and one that forwards `detail` raw fails here.
 */
const API = join(__dirname, "..", "app", "api");

function routes(dir: string): string[] {
  const out: string[] = [];
  for (const name of readdirSync(dir)) {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) out.push(...routes(path));
    else if (name === "route.ts") out.push(path);
  }
  return out;
}

const RAW_FORWARD = [
  /error:\s*\w+\?\.detail\b/,
  /error:\s*\(\w+ as \{ detail\?: string \}\)\?\.detail/,
  /error:\s*detail\s*(\?\?|\|\|)/,
];

describe("route handlers turn an API refusal into words", () => {
  it("scans every route", () => {
    expect(routes(API).length).toBeGreaterThan(25);
  });

  it("no route forwards the API's detail raw", () => {
    const offenders: string[] = [];
    for (const file of routes(API)) {
      const src = codeOnly(file);
      const usesDetail = /\.detail\b/.test(src);
      for (const pattern of RAW_FORWARD) {
        if (pattern.test(src) && !(file.endsWith("agent/route.ts") && src.includes("detail = apiErrorText("))) {
          offenders.push(relative(API, file));
        }
      }
      // A route that reads the API's error at all goes through the helper.
      if (usesDetail && /error:/.test(src) && !src.includes("apiErrorText(") &&
          /\.detail\s*(\?\?|\|\||\))/.test(src)) {
        offenders.push(`${relative(API, file)} (reads .detail without apiErrorText)`);
      }
    }
    expect([...new Set(offenders)], offenders.join("\n")).toEqual([]);
  });
});

describe("apiErrorText", () => {
  it("reads a FastAPI validation array as field: message", () => {
    expect(apiErrorText({ detail: [
      { loc: ["body", "name"], msg: "Field required", type: "missing" },
      { loc: ["body", "url"], msg: "Input should be a valid URL" },
    ] }, 422)).toBe("name: Field required; url: Input should be a valid URL");
  });

  it("keeps a sentence as the sentence", () => {
    expect(apiErrorText({ detail: "step 'd' is risk tier 2 and always requires a person" }, 409))
      .toBe("step 'd' is risk tier 2 and always requires a person");
  });

  it("reads an object's message, and never prints [object Object]", () => {
    expect(apiErrorText({ detail: { message: "resend the token", field: "token" } }, 409))
      .toBe("resend the token");
    expect(apiErrorText({ detail: { code: 7 } }, 500)).not.toContain("[object Object]");
  });

  it("says what happened when the body says nothing", () => {
    expect(apiErrorText({}, 503)).toBe("The BOSS API refused this (HTTP 503).");
    expect(apiErrorText(null, 502)).toBe("The BOSS API refused this (HTTP 502).");
  });
});
