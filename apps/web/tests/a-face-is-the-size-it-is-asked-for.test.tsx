/**
 * A face is the size its `size` prop says (spec §3.1, AvatarSize).
 *
 * The face keeps the `.mr-avatar` class some surfaces already sized in globals.css: 26px, and
 * 24px inside a chip, the meeting list and the Ask BOSS openers. `.agent-chip .mr-avatar` is
 * two classes, `.ag-avatar` one, so <AgentChip size={48}> drew a 48px picture in a 24px box.
 * agent-world.css restates every such rule with `.ag-face` added (one class more specific, and
 * loaded after globals.css) so --ag-size wins. This test fails when globals gains a new one.
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { AgentChip } from "../app/agent-chip";

(globalThis as Record<string, unknown>).React = React;

const read = (p: string) => readFileSync(resolve(__dirname, "..", p), "utf8").replace(/\/\*[\s\S]*?\*\//g, "");

/** Every rule in a stylesheet as [selectors, declarations]. Flat files only (no nesting). */
function rules(css: string): [string[], string][] {
  return [...css.matchAll(/([^{}@]+)\{([^{}]*)\}/g)].map((m) => [m[1].split(",").map((s) => s.trim().replace(/\s+/g, " ")), m[2]]);
}

describe("a face is the size it is asked for", () => {
  const GLOBALS = rules(read("app/globals.css"));
  const WORLD = rules(read("app/agents/agent-world.css"));

  /** globals.css selectors that size an .mr-avatar box. */
  const sizers = GLOBALS
    .filter(([, body]) => /(^|;|\s)(width|height)\s*:/.test(body))
    .flatMap(([sels]) => sels.filter((s) => /\.mr-avatar$/.test(s)));

  it("finds the globals rules that size the old avatar box", () => {
    expect(sizers).toEqual(expect.arrayContaining([".mr-avatar", ".agent-chip .mr-avatar", ".mr-meeting-avatars .mr-avatar", ".askboss-opener .mr-avatar"]));
  });

  it.each(sizers)("agent-world.css out-ranks %s with --ag-size", (sel) => {
    const own = WORLD.find(([sels]) => sels.includes(`${sel}.ag-face`));
    expect(own, `${sel}.ag-face`).toBeTruthy();
    expect(own![1]).toMatch(/width:\s*var\(--ag-size/);
    expect(own![1]).toMatch(/height:\s*var\(--ag-size/);
  });

  it("loads agent-world.css after globals.css, so an equal-specificity tie also goes to the face", () => {
    const layout = read("app/layout.tsx");
    expect(layout.indexOf('"./agents/agent-world.css"')).toBeGreaterThan(layout.indexOf('"./globals.css"'));
  });

  it("a 48px chip sets a 48px face on the element those rules size", () => {
    const html = renderToStaticMarkup(<AgentChip roleKey="qa" size={48} />);
    expect(html).toMatch(/<span class="ag-avatar mr-avatar tone-green ag-face" style="--ag-size:48px"/);
    expect(html).toContain('data-lod="48"');
  });
});
