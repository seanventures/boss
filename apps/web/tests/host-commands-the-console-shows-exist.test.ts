/**
 * Every host command the console shows is one that exists: `boss <cmd>` is a case in
 * scripts/install/boss.sh, and `boss identity <sub>` a subcommand of the identity CLI. The claim
 * page once said `docker exec boss-api boss identity setup-link` — a container no install has —
 * so a stranger had no working way to claim. No console file may spell a container command.
 */
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative } from "node:path";
import { describe, expect, it } from "vitest";

import { HOST_COMMANDS } from "../lib/identity/host-commands";

const REPO = join(__dirname, "..", "..", "..");
const WEB = join(__dirname, "..");
const bossSh = readFileSync(join(REPO, "scripts/install/boss.sh"), "utf8");
const identityCli = readFileSync(join(REPO, "engine/boss_engine/identity/cli.py"), "utf8");

describe("host commands the console shows", () => {
  it.each(Object.entries(HOST_COMMANDS))("%s exists", (_name, command) => {
    const [bin, cmd, sub] = command.split(/\s+/);
    expect(bin).toBe("boss");
    expect(bossSh).toMatch(new RegExp(`^\\s+${cmd}(\\|[a-z-]+)*\\) cmd_`, "m"));
    if (cmd === "identity") expect(identityCli).toContain(`add_parser("${sub}"`);
  });

  it("no console file tells someone to docker exec into a BOSS container", () => {
    const offenders: string[] = [];
    const walk = (dir: string) => {
      for (const name of readdirSync(dir)) {
        const full = join(dir, name);
        const rel = relative(WEB, full);
        if (/(^|\/)(node_modules|\.next|tests)(\/|$)/.test(rel)) continue;
        if (statSync(full).isDirectory()) { walk(full); continue; }
        if (!/\.(ts|tsx)$/.test(name)) continue;
        readFileSync(full, "utf8").split("\n").forEach((line, i) => {
          if (/docker (exec|compose exec)[^"'`]*boss/.test(line) && !/^\s*(\*|\/\/)/.test(line)) offenders.push(`${rel}:${i + 1}`);
        });
      }
    };
    for (const top of ["app", "lib", "components"]) walk(join(WEB, top));
    expect(offenders).toEqual([]);
  });
});
