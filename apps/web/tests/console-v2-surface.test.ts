import { readFileSync, readdirSync, existsSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

import { cssOnly } from "./_helpers/source";

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

describe("no link goes nowhere", () => {
  it("every internal href in a v2 component resolves to a route on disk", () => {
    // The console's own primary call to action -- "New project" -- pointed at /v2/new, which
    // does not exist. A request recorder cannot see this; a link check can, mechanically.
    const routes = new Set(
      ALL_PAGES.map((p) => {
        const rel = p.split("/app/")[1].replace(/\/page\.tsx$/, "");
        return "/" + rel;
      }),
    );
    routes.add("/");                                   // the v1 console
    const offenders: string[] = [];
    for (const path of ALL_COMPONENTS) {
      const src = readFileSync(path, "utf8");
      for (const m of src.matchAll(/href=\{?`?(\/[^`"'${}\s?]*)/g)) {
        const target = m[1].replace(/\/$/, "") || "/";
        // Dynamic segments are checked by their template, not their instance.
        if (target.includes("[") || target.startsWith("/api")) continue;
        const known = routes.has(target)
          || [...routes].some((r) => r.includes("[") && target.startsWith(r.split("/[")[0]));
        if (!known) offenders.push(`${path.split("/").pop()} -> ${target}`);
      }
    }
    expect(offenders).toEqual([]);
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

  it("every interactive element states its font face", () => {
    // Buttons, inputs and textareas do NOT inherit font-family. The top bar's "New project"
    // was a Link; when it became a <button> to open the launch modal it silently fell back to
    // Arial on every screen — invisible in review, caught only because the checker measures.
    //
    // The class list is DERIVED from the components rather than guessed from names: an earlier
    // version matched on the name and flagged containers like .v2-tabs, which render no text
    // and cannot fall back to anything.
    // Comments stripped: a check for `font-family` was satisfied by the comment saying
    // "buttons do not inherit font-family", so deleting the declaration still passed.
    const css = cssOnly(resolve(COMPONENTS, "shell.css"));
    // Per ELEMENT, not per class: .v2-btn-quiet is a modifier worn beside .v2-btn, which does
    // state a face. Checking classes independently flags the modifier for a gap that is not
    // there.
    const elements: string[][] = [];
    for (const path of ALL_COMPONENTS) {
      const src = readFileSync(path, "utf8");
      for (const [, cls] of src.matchAll(
        /<(?:button|input|textarea)[^>]*className="([^"]+)"/g,
      )) {
        const names = cls.split(/\s+/).filter((n) => n.startsWith("v2-"));
        if (names.length) elements.push(names);
      }
    }
    expect(elements.length, "found no interactive elements to check").toBeGreaterThan(3);

    const statesFace = (name: string) => {
      const rule = css.match(new RegExp(`\\.${name}\\s*\\{([^}]*)\\}`));
      return Boolean(rule && /font-family|font:\s*inherit/.test(rule[1]));
    };
    const offenders = elements
      .filter((names) => !names.some(statesFace))
      .map((names) => names.join("."));
    expect([...new Set(offenders)]).toEqual([]);
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

  it("every v2 screen links to the v1 area that is that screen, and shares its label", () => {
    // boss-1, 2026-10-02: "Workflows" linked to v1 Infra and "Infra & Models" to Observability,
    // and v1 and v2 named the same screens differently. A different label is allowed only with
    // a reason written in the parity map.
    const shell = readFileSync(resolve(__dirname, "../app/ConsoleShell.tsx"), "utf8");
    const v1Labels = new Map(
      [...shell.matchAll(/\{ id: "([a-z_-]+)", label: "([^"]+)"/g)].map((m) => [m[1], m[2]]),
    );
    const parity = readFileSync(resolve(__dirname, "../../../docs/product/console_v2_parity.yaml"), "utf8");
    for (const item of V2_NAV) {
      expect(v1Labels.has(item.v1Area), `${item.id}: ${item.v1Area} is not a v1 area`).toBe(true);
      const line = parity.split("\n").find((l) => l.trim().startsWith(`${item.id}: {v1Area: ${item.v1Area}`));
      expect(line, `${item.id} is missing from v2_nav in the parity map`).toBeTruthy();
      if (item.label !== v1Labels.get(item.v1Area)) {
        expect(line, `${item.id} is labelled "${item.label}" but v1 says "${v1Labels.get(item.v1Area)}"`)
          .toContain("label_differs_because");
      }
    }
  });
});
