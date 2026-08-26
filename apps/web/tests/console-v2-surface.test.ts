import { readFileSync, readdirSync, existsSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

import { V2_NAV } from "../components/v2/Shell";

/**
 * Properties that hold across the whole v2 surface, rather than inside one screen.
 *
 * These exist because the v1 console's worst problems were not in any single component. They
 * were patterns repeated across fifty areas: eighty-two feeds per render, Server Actions on
 * every reply path, empty states that could not be told apart from outages. A per-screen test
 * cannot see a pattern; these can.
 */

const V2_DIR = resolve(__dirname, "../app/v2");
const COMPONENTS = resolve(__dirname, "../components/v2");

function pages(dir = V2_DIR, acc: string[] = []): string[] {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = resolve(dir, entry.name);
    if (entry.isDirectory()) pages(full, acc);
    else if (entry.name === "page.tsx") acc.push(full);
  }
  return acc;
}

const ALL_PAGES = pages();
const ALL_COMPONENTS = readdirSync(COMPONENTS)
  .filter((f) => f.endsWith(".tsx"))
  .map((f) => resolve(COMPONENTS, f));

describe("no screen reintroduces the v1 render cost", () => {
  it.each(ALL_PAGES.map((p) => [p.split("/app/")[1], p] as const))(
    "%s issues no Server Action",
    (_name, path) => {
      // A Server Action re-renders the whole route to reply. That single fact produced the
      // 115-second "Set target", the 82 route renders on one Factory load, and the 25-second
      // activity log. v2 pages fetch server-side or through route handlers.
      const src = readFileSync(path, "utf8");
      expect(src).not.toMatch(/from "\.\.\/.*\/app\/actions"/);
      expect(src).not.toMatch(/"use server"/);
    },
  );

  it.each(ALL_PAGES.map((p) => [p.split("/app/")[1], p] as const))(
    "%s awaits its independent fetches together, not in sequence",
    (_name, path) => {
      // Promise.all waits for its slowest member, so sequencing two INDEPENDENT fetches makes
      // a page twice as slow for no reason.
      //
      // fetchOrgs is excluded because everything else genuinely depends on it: the project id
      // comes out of the resolved org, so it cannot run in parallel with anything. Banning all
      // sequencing would flag that legitimate dependency and train everyone to ignore this
      // test — which is worse than not having it.
      const src = readFileSync(path, "utf8");
      const independent = (src.match(/await fetch\w+\(/g) ?? [])
        .filter((call) => !call.includes("fetchOrgs"));
      if (independent.length > 1) {
        expect(src, `${_name} sequences ${independent.length} independent fetches`)
          .toMatch(/Promise\.all/);
      }
    },
  );
});

describe("no screen can be read as a confident nothing", () => {
  it.each(
    ALL_COMPONENTS.filter((p) => /Screen\.tsx$/.test(p)).map((p) => [p.split("/").pop()!, p] as const),
  )("%s distinguishes an outage from an empty result", (_name, path) => {
    // "The API did not answer" and "there is nothing here" render identically as an empty
    // list and mean opposite things. This console has shipped that confusion repeatedly: an
    // outage read as an empty company, a deployed app read as never deployed.
    const src = readFileSync(path, "utf8");
    expect(src, `${_name} never checks reachability`)
      .toMatch(/apiReachable|blockedReason|found/);
  });
});

describe("the shell is consistent", () => {
  it("every screen renders inside it, so the org and the toggle are never lost", () => {
    for (const path of ALL_PAGES) {
      const src = readFileSync(path, "utf8");
      expect(src, path.split("/app/")[1]).toContain("<Shell");
    }
  });

  it("every page passes the org through", () => {
    // Losing the org mid-navigation throws the operator into another company or back to the
    // picker — the "console resets" bug.
    for (const path of ALL_PAGES) {
      const src = readFileSync(path, "utf8");
      expect(src, path.split("/app/")[1]).toMatch(/orgId=\{selected\?\.org_id\}/);
    }
  });

  it("every page marks itself dynamic, so nothing is served from a build-time cache", () => {
    // A cached control plane view is a stale one, and stale operational data is worse than
    // slow operational data.
    for (const path of ALL_PAGES) {
      expect(readFileSync(path, "utf8"), path.split("/app/")[1])
        .toContain('export const dynamic = "force-dynamic"');
    }
  });

  it("the active nav id on each page is a real nav id", () => {
    const ids = new Set<string>(V2_NAV.map((n) => n.id));
    for (const path of ALL_PAGES) {
      const src = readFileSync(path, "utf8");
      const match = src.match(/<Shell\s+active="([^"]+)"/);
      expect(match, path.split("/app/")[1]).toBeTruthy();
      expect(ids.has(match![1]), `${path.split("/app/")[1]} -> ${match![1]}`).toBe(true);
    }
  });
});

describe("style", () => {
  it("every v2 component uses the token palette rather than raw hexes", () => {
    // The artifact prescribes an exact palette. A stray hex is how a redesign drifts back into
    // looking like the thing it replaced.
    for (const path of ALL_COMPONENTS) {
      const src = readFileSync(path, "utf8");
      const hexes = src.match(/#[0-9a-fA-F]{3,8}\b/g) ?? [];
      expect(hexes, `${path.split("/").pop()} hardcodes ${hexes.join(", ")}`).toHaveLength(0);
    }
  });

  it("the token file is the single place colours are defined", () => {
    const tokens = readFileSync(resolve(COMPONENTS, "tokens.css"), "utf8");
    for (const name of ["--v2-canvas", "--v2-mint", "--v2-amber", "--v2-red", "--v2-ink"]) {
      expect(tokens, name).toContain(name);
    }
  });

  it("the parity map exists and covers the nav", () => {
    const parity = resolve(__dirname, "../../../docs/product/console_v2_parity.yaml");
    expect(existsSync(parity)).toBe(true);
    const text = readFileSync(parity, "utf8");
    for (const item of V2_NAV) {
      expect(text, `${item.id} -> ${item.v1Area}`).toContain(`${item.v1Area}:`);
    }
  });
});
