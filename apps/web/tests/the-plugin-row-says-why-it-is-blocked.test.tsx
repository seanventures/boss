// @vitest-environment happy-dom
import { readFileSync } from "node:fs";
import { join } from "node:path";

import React from "react";
import { afterEach, describe, expect, it } from "vitest";

import { PluginsTab, pluginProblemSentence } from "../app/infra/plugins";
import { mockInfraFetch, type Route } from "./_helpers/infra-fixtures";
import { byTestId, mount, settle, TabHarness, type Mounted } from "./_helpers/infra-tab-render";

/**
 * A plugin with problems already says Blocked, and the problems are only on that title.
 * The same problems have to be readable beside Blocked. Blank is nothing. Whitespace is
 * nothing. A trimmed problem is the sentence. More than one is joined with "; ".
 * More than six words is a paragraph. The Blocked label and the description title stay.
 */

const LONG = "its files changed after it was enabled";
const PADDED = `  ${LONG}  `;
const SEVEN = "one two three four five six seven";
const SIX = "one two three four five six";
const SHORT = "checksum mismatch";
const JOINED = ["  disk is full  ", "   ", "the checksum does not match the pin"];
const JOINED_SENTENCE = "disk is full; the checksum does not match the pin";
const DESCRIPTION = "A community queue plugin";

let m: Mounted | null = null;
let restore: (() => void) | null = null;
afterEach(async () => { await m?.unmount(); m = null; restore?.(); restore = null; });

function plugin(id: string, name: string, problems: string[], description = "") {
  return {
    id, version: "0.0.1", name, description, source: "community", supportLevel: "community",
    capabilities: ["queue"], permissions: {}, enabled: false, usable: false, problems, checksum: "sha256:c",
  };
}

const routes = (): Route[] => [{
  match: /^\/v1\/infra\/plugins$/,
  reply: () => ({
    plugins: [
      plugin("community.long", "Long block", [PADDED], DESCRIPTION),
      plugin("community.seven", "Seven block", [SEVEN]),
      plugin("community.six", "Six block", [SIX]),
      plugin("community.short", "Short block", [`  ${SHORT}  `]),
      plugin("community.joined", "Joined block", JOINED),
      plugin("community.blank", "Blank block", ["   ", "\n\t"]),
      plugin("official.postgres", "PostgreSQL", [], "Ships with the install"),
    ],
  }),
}];

async function open() {
  const f = mockInfraFetch({ routes: routes() });
  restore = f.install();
  m = await mount(<TabHarness Tab={PluginsTab} />, "?area=infra&view=plugins");
  await settle(() => !!byTestId("inf-plugins-table"), "plugins table");
  return f;
}

function rowOf(id: string): HTMLElement {
  const row = byTestId(`inf-plugin-row-${id}`);
  expect(row).not.toBeNull();
  return row as HTMLElement;
}

function blocked(row: HTMLElement): HTMLElement {
  const el = byTestId("inf-plugin-problem", row);
  expect(el).not.toBeNull();
  return el as HTMLElement;
}

function problemsEl(row: HTMLElement): HTMLElement | null {
  return byTestId("inf-plugin-problems", row);
}

describe("pluginProblemSentence", () => {
  it("is nothing when the problems are blank", () => {
    expect(pluginProblemSentence(null)).toBeNull();
    expect(pluginProblemSentence(undefined)).toBeNull();
    expect(pluginProblemSentence([])).toBeNull();
    expect(pluginProblemSentence([""])).toBeNull();
  });

  it("is nothing when every problem is whitespace", () => {
    expect(pluginProblemSentence(["   "])).toBeNull();
    expect(pluginProblemSentence([" \n\t ", "\n"])).toBeNull();
  });

  it("is the trimmed problem", () => {
    expect(pluginProblemSentence([PADDED])).toBe(LONG);
    expect(pluginProblemSentence([` \n ${SHORT} \t `])).toBe(SHORT);
    expect(pluginProblemSentence([SIX])).toBe(SIX);
  });

  it("joins trimmed non-empty problems with a semicolon", () => {
    expect(pluginProblemSentence(JOINED)).toBe(JOINED_SENTENCE);
    expect(pluginProblemSentence(["  alpha  ", "", "  ", "beta"])).toBe("alpha; beta");
  });
});

describe("the plugin row says why it is blocked", () => {
  it("shows a long reason as a paragraph beside Blocked and keeps both titles", async () => {
    await open();
    const row = rowOf("community.long");
    const label = blocked(row);
    expect(label.textContent).toBe("Blocked");
    expect(label.getAttribute("title")).toBe(PADDED);
    expect(label.querySelector("[data-testid='inf-plugin-problems']")).toBeNull();
    const name = row.querySelector("strong")?.parentElement;
    expect(name?.getAttribute("title")).toBe(DESCRIPTION);
    const sentence = problemsEl(row);
    expect(sentence).not.toBeNull();
    expect(sentence!.textContent).toBe(LONG);
    expect(sentence!.tagName).toBe("P");
    expect(sentence!.hasAttribute("data-user-text")).toBe(true);
    expect(LONG.split(/\s+/).filter(Boolean).length).toBeGreaterThan(6);
  });

  it("shows six words as a span and seven as a paragraph", async () => {
    await open();
    const six = problemsEl(rowOf("community.six"));
    expect(six).not.toBeNull();
    expect(six!.textContent).toBe(SIX);
    expect(six!.tagName).toBe("SPAN");
    expect(six!.hasAttribute("data-user-text")).toBe(true);
    expect(SIX.split(/\s+/).filter(Boolean).length).toBe(6);
    expect(blocked(rowOf("community.six")).textContent).toBe("Blocked");

    const seven = problemsEl(rowOf("community.seven"));
    expect(seven).not.toBeNull();
    expect(seven!.textContent).toBe(SEVEN);
    expect(seven!.tagName).toBe("P");
    expect(SEVEN.split(/\s+/).filter(Boolean).length).toBe(7);
    expect(blocked(rowOf("community.seven")).textContent).toBe("Blocked");

    const short = problemsEl(rowOf("community.short"));
    expect(short).not.toBeNull();
    expect(short!.textContent).toBe(SHORT);
    expect(short!.tagName).toBe("SPAN");
    expect(blocked(rowOf("community.short")).getAttribute("title")).toBe(`  ${SHORT}  `);
  });

  it("joins the problems beside Blocked", async () => {
    await open();
    const row = rowOf("community.joined");
    expect(blocked(row).getAttribute("title")).toBe(JOINED.join("\n"));
    const sentence = problemsEl(row);
    expect(sentence).not.toBeNull();
    expect(sentence!.textContent).toBe(JOINED_SENTENCE);
    expect(sentence!.tagName).toBe("P");
    expect(sentence!.hasAttribute("data-user-text")).toBe(true);
    expect(JOINED_SENTENCE.split(/\s+/).filter(Boolean).length).toBeGreaterThan(6);
  });

  it("shows no sentence when the problems are blank or absent", async () => {
    await open();
    const blank = rowOf("community.blank");
    expect(blocked(blank).textContent).toBe("Blocked");
    expect(blocked(blank).getAttribute("title")).toBe("   \n\n\t");
    expect(problemsEl(blank)).toBeNull();

    const clear = rowOf("official.postgres");
    expect(byTestId("inf-plugin-problem", clear)).toBeNull();
    expect(problemsEl(clear)).toBeNull();
    expect(clear.querySelector("strong")?.parentElement?.getAttribute("title")).toBe("Ships with the install");
    expect(clear.textContent ?? "").not.toContain(LONG);
  });

  it("calls pluginProblemSentence with the plugin problems", () => {
    const src = readFileSync(join(__dirname, "../app/infra/plugins.tsx"), "utf8");
    const start = src.indexOf("function PluginRowView");
    const end = src.indexOf("export function PluginsTab");
    expect(start).toBeGreaterThan(-1);
    expect(end).toBeGreaterThan(start);
    const row = src.slice(start, end);
    expect(row).toContain("pluginProblemSentence(p.problems)");
    expect(row).toContain("title={p.description || undefined}");
    expect(row).toContain(">Blocked</span>");
    expect(row).toContain('data-testid="inf-plugin-problems"');
  });
});
