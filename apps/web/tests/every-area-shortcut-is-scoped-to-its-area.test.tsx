// @vitest-environment happy-dom
/**
 * AN AREA'S SHORTCUTS FIRE ONLY WHILE THAT AREA IS ON SCREEN (KG tester round 1, P0-5).
 *
 * The Activity log registered j/k/↓/↑ on `window` from an always-mounted area, so arrowing
 * through the Graph's tree stepped the hidden log and moved the URL to it. Every area stays
 * mounted, so a raw window/document keydown listener that answers anything but closing an
 * overlay (Escape, the Tab focus trap) is a shortcut that leaks into every other area. Those go
 * through lib/area-keys.ts `useAreaKeydown`, which listens only while its area is shown.
 */
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { describe, expect, it, vi } from "vitest";

import { AreaShownContext } from "../lib/area-shown";
import { useAreaKeydown } from "../lib/area-keys";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

function files(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    if (name === "node_modules" || name.startsWith(".") || name.startsWith("__")) return [];
    const path = join(dir, name);
    if (statSync(path).isDirectory()) return files(path);
    return /\.tsx?$/.test(name) && !/\.test\./.test(name) ? [path] : [];
  });
}

/** The shell's own global keys (the palette, Esc closing it) are the one place a global is right. */
const GLOBAL = new Set(["app/ConsoleShell.tsx", "lib/area-keys.ts", "lib/user-gesture.ts"]);

/** Handlers registered on window/document, with the keys each answers (the handler's body). */
function rawHandlers(src: string): Array<{ name: string; keys: string[] }> {
  const out: Array<{ name: string; keys: string[] }> = [];
  for (const m of src.matchAll(/(?:window|document)\.addEventListener\("keydown",\s*(\w+)/g)) {
    const name = m[1];
    // The handler's own definition: the nearest one above where it is registered.
    const at = m.index ?? 0;
    const def = src.lastIndexOf(`const ${name} = (`, at);
    const body = def >= 0 ? src.slice(def, at) : "";
    const keys = [...body.matchAll(/(?:key|k)(?:\.toLowerCase\(\))?\s*(?:===|!==)\s*"([^"]+)"/g)].map((x) => x[1]);
    out.push({ name, keys });
  }
  return out;
}

describe("every area shortcut is scoped to its area", () => {
  const all = ["app", "components", "lib"].flatMap(files);

  it("finds the listeners it is checking (the scan works)", () => {
    const n = all.reduce((s, f) => s + rawHandlers(readFileSync(f, "utf8")).length, 0);
    expect(n).toBeGreaterThan(30);
    expect(all.filter((f) => readFileSync(f, "utf8").includes("useAreaKeydown(")).length).toBeGreaterThan(8);
  });

  it("no raw window/document keydown listener answers a shortcut", () => {
    const leaks: string[] = [];
    for (const f of all) {
      if (GLOBAL.has(f)) continue;
      for (const h of rawHandlers(readFileSync(f, "utf8"))) {
        const shortcut = h.keys.filter((k) => !/^(Escape|escape|Tab)$/.test(k));
        if (shortcut.length) leaks.push(`${f} ${h.name} answers ${shortcut.join(", ")} — use useAreaKeydown (lib/area-keys.ts)`);
      }
    }
    expect(leaks).toEqual([]);
  });

  it("the Activity log's arrows and the Graph's ⌘K go through the hook", () => {
    expect(readFileSync("app/activity/log-explorer.tsx", "utf8")).toContain("useAreaKeydown(");
    expect(readFileSync("app/knowledge/graph-view.tsx", "utf8")).toMatch(/useAreaKeydown\([\s\S]{0,300}"k"[\s\S]{0,300}capture: true/);
  });

  it("a hidden area's shortcut does nothing; shown, it answers", async () => {
    const hit = vi.fn();
    function Probe() { useAreaKeydown((e) => { if (e.key === "ArrowDown") hit(); }); return null; }
    const host = document.createElement("div");
    document.body.appendChild(host);
    const root = createRoot(host);
    await act(async () => root.render(<AreaShownContext.Provider value={false}><Probe /></AreaShownContext.Provider>));
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown" }));
    expect(hit).not.toHaveBeenCalled();
    await act(async () => root.render(<AreaShownContext.Provider value><Probe /></AreaShownContext.Provider>));
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown" }));
    expect(hit).toHaveBeenCalledTimes(1);
    act(() => root.unmount());
    host.remove();
  });
});
