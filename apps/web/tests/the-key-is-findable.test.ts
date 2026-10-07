/**
 * $BOSS_API_KEY appears in every example command and was explained nowhere. "no API key" told
 * somebody what was wrong and nothing about what to do.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const root = join(__dirname, "../../..");
const common = readFileSync(join(root, "examples/_common.py"), "utf8");
const all = readFileSync(join(root, "examples/setup_all.py"), "utf8");
const index = readFileSync(join(root, "examples/README.md"), "utf8");
const remote = readFileSync(join(root, "scripts/install/remote.sh"), "utf8");

describe("a missing API key explains itself", () => {
  it("covers the three places BOSS is ever run from", () => {
    // 215a6a54 (identity docs): the shared env-file key is gone; a personal key from My account
    // works on ANY MACHINE, and the server line mints the host tools' key.
    for (const where of ["ANY MACHINE", "My account › API keys › New key", "ON THE SERVER", "boss identity host-key", "FROM YOUR LAPTOP"]) {
      expect(common).toContain(where);
    }
    expect(common).toContain("source ./boss-community.env.sh");
    expect(common).toContain("remote.sh --host root@YOUR_HOST");
  });

  it("says it is a secret, on a command that prints one", () => {
    expect(common).toContain("The key is a secret: do not paste `env` output");
  });

  it("is shared, not copied, between the scripts", () => {
    // Two copies would drift into telling somebody different things.
    expect(all).toContain("from _common import HOW_TO_FIND_THE_KEY");
  });

  it("is answered in the README before anyone hits the error", () => {
    expect(index.indexOf("## First: your API key")).toBeGreaterThan(-1);
    // Before the first command that needs it.
    expect(index.indexOf("## First: your API key"))
      .toBeLessThan(index.indexOf("setup_all.py"));
  });
});

describe("the env helper exports what the scripts read", () => {
  it("uses BOSS_PROJECT, not PROJECT", () => {
    // It exported `PROJECT`, which the examples do not read — so the output was correct and
    // useless: everything looked set and the default project was used anyway.
    expect(remote).toContain('echo "export BOSS_PROJECT=${PROJECT_ID}"');
    expect(remote).not.toContain('echo "export PROJECT=${PROJECT_ID}"');
  });

  it("exports nothing the examples ignore", () => {
    const exported = [...remote.matchAll(/echo "export ([A-Z_]+)=/g)].map((m) => m[1]);
    const read = [...common.matchAll(/os\.environ\.get\("([A-Z_]+)"/g)].map((m) => m[1]);
    for (const name of exported) {
      expect(read, `remote.sh exports ${name} and no example reads it`).toContain(name);
    }
  });
});
