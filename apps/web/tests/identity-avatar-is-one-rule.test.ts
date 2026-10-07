/**
 * A PERSON'S AVATAR COLOUR IS ONE RULE (lib/identity/avatar.ts): the colour they chose, else one
 * from their id. Every person the console draws carries the chosen colour — the person view, the
 * directory, group members, the account menu — and no file keeps a private copy of the id hash
 * (which is how the menu and the lists drew the same person in two colours).
 */
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

import { avatarTone } from "../lib/identity/avatar";
import { adaptGrantor, adaptPerson, nameIndex, roleIndex } from "../lib/identity/adapt-people";

describe("one avatar rule", () => {
  it("a chosen colour wins; otherwise the id decides, the same everywhere", () => {
    expect(avatarTone("usr_sam", "3")).toBe(3);
    expect(avatarTone("usr_sam", "9")).toBe(avatarTone("usr_sam"));
    expect(avatarTone("usr_sam", null)).toBe(avatarTone("usr_sam"));
  });

  it("the person view, the index built from it and a grantor carry the chosen colour", () => {
    const raw = { id: "usr_x", display_name: "Ana Costa", email: "ana@acme.dev", status: "active", access_roles: [], avatar_seed: "5" };
    const person = adaptPerson(raw, { index: roleIndex([]) });
    expect(person.hue).toBe(5);
    expect(nameIndex([person]).get("usr_x")?.hue).toBe(5);
    expect(adaptGrantor({ id: "usr_x", name: "Ana", avatar_seed: "2" }).hue).toBe(2);
  });

  it("no file hashes an id into a colour on its own", () => {
    const files = (dir: string): string[] => readdirSync(dir).flatMap((n) => {
      const p = join(dir, n);
      return statSync(p).isDirectory() ? (n === "stub" ? [] : files(p)) : /\.tsx?$/.test(n) ? [p] : [];
    });
    const bad = ["app", "components", "lib"].flatMap((d) => files(join(process.cwd(), d)))
      .filter((f) => !f.endsWith("lib/identity/avatar.ts"))
      .filter((f) => /h \* 31 \+ [\w.]+\.charCodeAt\([^)]*\)\) >>> 0;[\s\S]{0,80}% 8\b/.test(readFileSync(f, "utf8")))
      .map((f) => f.replace(process.cwd() + "/", ""));
    expect(bad).toEqual([]);
  });
});
