/**
 * CONFLUENCE IS A DOCUMENT REPOSITORY, NOT A TICKET SYSTEM (operator, 2026-11-18: "Why
 * Confluence is under Ticketing System - it is Document Repository right?"). Setup shows it in
 * its own Documents fold; the Ticket systems fold holds ticket systems only.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const board = readFileSync(join(__dirname, "..", "app", "setup", "setup-board.tsx"), "utf8");

function section(id: string): string {
  const start = board.indexOf(`<SetupSection id="${id}"`);
  expect(start, `section ${id}`).toBeGreaterThan(-1);
  const end = board.indexOf("</SetupSection>", start);
  return board.slice(start, end);
}

describe("Setup › Documents", () => {
  it("holds Confluence in its own Documents section", () => {
    expect(section("documents")).toContain("<ConfluenceRow");
    expect(section("documents")).toContain('title="Documents"');
  });
  it("never puts Confluence under Ticket systems", () => {
    expect(section("ticket_systems")).not.toContain("ConfluenceRow");
  });
});
