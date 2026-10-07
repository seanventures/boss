/**
 * THE JOURNEYS CHECK THE COPY THE CONSOLE SHOWS (round 3, W4; J11, J12, J13).
 *
 * The identity screens name a permission by its title and keep the key in `title` /
 * `data-permission` (lib/identity/words.ts): Needs “Invite people” · ask an Admin. The real-stack
 * journeys still looked for the raw key in visible text, so a correct screen failed. They now
 * build the same words from the API's catalogue; this keeps the Python mirror and the tester's
 * script in step with the console's words.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

import { askWho, needsWords, permissionName } from "../lib/identity/words";
import { PERMISSION_META } from "../lib/identity/permissions.gen";
import { JOURNEYS } from "./_helpers/identity-journeys";

const PY = readFileSync(join(process.cwd(), "../../tests/e2e/identity/journeys_real.py"), "utf8");

describe("journeys_real.py mirrors needsWords", () => {
  it("the same sentence, from the catalogue's title and roles", () => {
    expect(PY).toMatch(/return f"Needs “\{m\.get\('title'\) or key\}” · \{who\}"/);
    expect(PY).toMatch(/who = "ask an Admin" if "admin" in \(m\.get\("roles"\) or \[\]\) else "ask an Owner"/);
    expect(PY).toMatch(/api\(page, "GET", "\/v1\/identity\/catalogue"\)/);
    // …which is what the console renders for the controls J11 reads.
    expect(needsWords("user:invite")).toBe(`Needs “${permissionName("user:invite")}” · ${askWho("user:invite")}`);
    expect(needsWords("user:invite")).toBe("Needs “Invite people” · ask an Admin");
  });

  it("J11–J13 judge permissions by key in attributes, never by the key in visible text", () => {
    for (const probe of [
      `inv.get_attribute("data-permission") == "user:invite" and title == want`,
      `denied.first.get_attribute("data-permission") == "user:read"`,
      `[data-testid=diff] .idn-dl.m[title='deployment:create']`,
      `[data-testid=scopes] [data-permission]`,
    ]) expect(PY, probe).toContain(probe);
    expect(PY).not.toMatch(/"user:invite" in title/);
    expect(PY).not.toMatch(/"user:read" in txt/);
    expect(PY).not.toMatch(/"deployment:create" in diff/);
  });
});

describe("the tester's script names permissions as the console does", () => {
  const titles = new Set(Object.values(PERMISSION_META).map((m) => m.title));
  for (const id of ["J11", "J12", "J13"]) {
    it(`${id}: every quoted permission name is a real permission title`, () => {
      const j = JOURNEYS.find((x) => x.id === id);
      expect(j, id).toBeTruthy();
      const text = j!.steps.map((s) => `${s.do} ${s.expect}`).join(" ");
      const quoted = [...text.matchAll(/“([^”“]+)”/g)].map((m) => m[1]).filter((q) => /^(View|Invite|Create|Start|Manage|Edit|Delete)\b/.test(q));
      expect(quoted.length, id).toBeGreaterThan(0);
      for (const q of quoted) expect(titles.has(q), `${id}: “${q}”`).toBe(true);
    });
  }
});
