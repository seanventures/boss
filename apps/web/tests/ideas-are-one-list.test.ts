/**
 * One ideas list (operator, 2026-10-13): the Command Center's Ideas and the composer's "Start from
 * an idea" read the SAME source, the catalogue's starters then the written briefs, so the two can
 * never drift. A connector is never an idea and a training lab is never offered.
 */
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

import { IDEAS } from "../app/ask-boss-hero";
import { connectorsFrom, ideasFrom } from "../components/v2/templates/catalogue";
import { kindOf, parseCard } from "../lib/templates";

const card = (key: string, kind?: string, labels: string[] = []) =>
  parseCard({ id: `tpl_${key}`, template_key: key, name: key, summary: `${key} summary`, status: "active",
              stacks: ["node"], labels, adds: ["a thing"], egress: [], secrets: [], kind });

describe("one ideas list", () => {
  it("starters first, then every written brief; no connector, no training lab", () => {
    const cards = [card("login-oauth", "connector"), card("hello", "idea"), card("lab", "training", ["insecure_by_design"])];
    const ideas = ideasFrom(cards);
    expect(ideas[0].title).toBe("hello");
    expect(ideas[0].card?.key).toBe("hello");
    expect(ideas.slice(1)).toEqual(IDEAS);
    expect(ideas.map((i) => i.title)).not.toContain("login-oauth");
    expect(ideas.map((i) => i.title)).not.toContain("lab");
    expect(connectorsFrom(cards).map((c) => c.key)).toEqual(["login-oauth"]);
  });

  it("a card's kind is the server's, and an insecure item is always training", () => {
    expect(kindOf({ kind: "idea", labels: [] })).toBe("idea");
    expect(kindOf({ labels: [] })).toBe("connector");
    expect(kindOf({ kind: "idea", labels: ["insecure_by_design"] })).toBe("training");
  });

  it("the Command Center shelf and the composer both read useIdeas, never IDEAS alone", () => {
    const rails = readFileSync("app/command-center/rails.tsx", "utf8");
    const composer = readFileSync("app/workflows/new-project-composer.tsx", "utf8");
    expect(rails).toContain("useIdeas()");
    expect(composer).toContain("useIdeas()");
    expect(rails).not.toMatch(/\{IDEAS\.map\(/);
    expect(composer).not.toMatch(/IDEAS\.slice\(/);
  });
});
