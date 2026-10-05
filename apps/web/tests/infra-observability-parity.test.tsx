// @vitest-environment happy-dom
/**
 * THE PARITY TEST (infra-observability spec §8, §9.4): all 144 rows of lib/infra-obs-parity.ts,
 * one assertion each. Nothing the old Infra (`delivery`), System Configuration (`?area=infra`) or
 * Observability (`monitoring`) screens could do is silently dropped:
 *
 *   testid    the new area, opened at the row's URL against the shared wire fixtures, with the
 *             row's presses made, renders the row's testid
 *   redirect  the old URL resolves (app/legacy-areas.ts) to the row's new parameters
 *   removed   the removed sentence or dead control is nowhere on the surface
 *   code      a source-level guard holds (a link carries its id, nothing refreshes the console)
 */
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { resolveLegacyArea } from "../app/legacy-areas";
import { PARITY, type ParityRow } from "../lib/infra-obs-parity";
import { byTestId, click as clickInfra, mountInfra, settle as settleInfra, unmountInfra } from "./_helpers/infra-render";
import { mockObsFetch } from "./_helpers/obs-fixtures";
import { $, click, mount, settle, unmount } from "./_helpers/live-render";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(window.location.search),
  usePathname: () => "/",
}));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const { ObsHome } = await import("../app/observability/obs-home");
const { SetupBoard } = await import("../app/setup/setup-board");

let infraMounted = false;
afterEach(() => {
  if (infraMounted) unmountInfra(); else unmount();
  infraMounted = false;
  vi.unstubAllGlobals();
});

async function openInfra(url: string, press: string[] = [], variant?: "blocked"): Promise<void> {
  await mountInfra(url.replace(/^\?/, ""), variant ? { variant } : {});
  infraMounted = true;
  for (const p of press) await clickInfra(p);
  await settleInfra();
}

async function openObs(url: string, press: string[] = []): Promise<void> {
  mockObsFetch();
  window.history.replaceState(null, "", `/?area=observability&${url.replace(/^\?/, "")}`);
  await mount(<ObsHome projectId="p1" actor="ceo" />);
  await settle();
  for (const p of press) await click($(`[data-testid="${p}"]`));
  await settle();
}

async function openSetup(url: string): Promise<void> {
  mockObsFetch();
  window.history.replaceState(null, "", `/${url}`);
  await mount(<SetupBoard projectId="p1" actor="ceo" />);
  await settle();
}

/** Files a `code` row names: a path, or `dir/*.tsx`. */
function sources(spec: string): string {
  const root = process.cwd();
  if (!spec.includes("*")) return readFileSync(join(root, spec), "utf8");
  const [dir, pattern] = [spec.slice(0, spec.lastIndexOf("/")), spec.slice(spec.lastIndexOf("/") + 1)];
  const ext = pattern.replace("*", "");
  return readdirSync(join(root, dir)).filter((f) => f.endsWith(ext))
    .map((f) => readFileSync(join(root, dir, f), "utf8")).join("\n");
}

const label = (r: ParityRow) => `${r.id} · ${r.fate} · ${r.today} → ${r.home}`;

describe("the parity table", () => {
  it("has every row of spec §8 exactly once", () => {
    expect(PARITY).toHaveLength(144);
    expect(new Set(PARITY.map((r) => r.id)).size).toBe(144);
    expect(PARITY.filter((r) => r.id.startsWith("P-I-"))).toHaveLength(59);
    expect(PARITY.filter((r) => r.id.startsWith("P-S-"))).toHaveLength(51);
    expect(PARITY.filter((r) => r.id.startsWith("P-O-"))).toHaveLength(34);
  });
});

describe("every capability has its new home", () => {
  for (const row of PARITY) {
    const c = row.check;
    it(label(row), async () => {
      if (c.kind === "redirect") {
        const next = resolveLegacyArea(new URLSearchParams(c.from.replace(/^\?/, "")));
        expect(next, c.from).not.toBeNull();
        for (const [k, v] of Object.entries(c.to)) expect(next!.get(k), `${c.from} → ${k}`).toBe(v);
        return;
      }
      if (c.kind === "code") {
        const src = sources(c.file);
        const hit = new RegExp(c.pattern).test(src);
        expect(hit, `${c.file} ${c.must ? "must" : "must not"} match /${c.pattern}/ (${c.why})`).toBe(c.must);
        return;
      }
      if (c.kind === "removed") {
        if (c.surface === "infra") await openInfra(c.url); else await openObs(c.url);
        expect(document.body.textContent ?? "", `${c.absent} (${c.why})`).not.toContain(c.absent);
        return;
      }
      if (c.surface === "infra") {
        await openInfra(c.url, c.press ?? [], c.variant);
        expect(byTestId(c.testid), `${c.testid} at ${c.url}`).not.toBeNull();
      } else if (c.surface === "obs") {
        await openObs(c.url, c.press ?? []);
        expect($(`[data-testid="${c.testid}"]`), `${c.testid} at ${c.url}`).not.toBeNull();
      } else {
        await openSetup(c.url);
        expect($(`[data-testid="${c.testid}"]`), c.testid).not.toBeNull();
      }
    });
  }
});
