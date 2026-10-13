/** A person is shown by name, never by id.
 *
 *  "Stopped by usr_kdzhawzy7mxmp5hpb3bqlgqojs" in the Factory's build list, "Approved by ceo" on
 *  Approvals, "set by usr_…" on autonomy: every screen that named an actor printed the id it had.
 *  `actorWords` is the one way an actor becomes text; no screen prints an actor field raw. */
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

import { actorWords, looksLikeAnId } from "../lib/actor-words";

const ROOT = process.cwd();
function walk(dir: string): string[] {
  const out: string[] = [];
  for (const name of readdirSync(join(ROOT, dir))) {
    if (name === "node_modules" || name.startsWith(".") || name.startsWith("__")) continue;
    const path = join(dir, name);
    if (statSync(join(ROOT, path)).isDirectory()) out.push(...walk(path));
    else if (/\.tsx$/.test(name)) out.push(path);
  }
  return out;
}

const FIELDS = "actor|createdBy|created_by|stoppedBy|stopped_by|skippedBy|skipped_by|approvedBy|"
  + "approved_by|cancelledBy|cancelled_by|requestedBy|requested_by|setBy|set_by|decidedBy|decided_by";
/** `{x.actor}` or `${x.created_by}` printed straight into text. */
const RAW = new RegExp(`(?:\\$\\{|[>\\s]\\{)[A-Za-z_][\\w.?]*\\.(?:${FIELDS})\\}`);

describe("actorWords", () => {
  it("turns ids into words and never returns one", () => {
    expect(actorWords("usr_kdzhawzy7mxmp5hpb3bqlgqojs")).toBe("a person");
    expect(actorWords("usr_kdzhawzy7mxmp5hpb3bqlgqojs", "Paramaguru Gnanaguru")).toBe("Paramaguru Gnanaguru");
    expect(actorWords("usr_kdzhawzy7mxmp5hpb3bqlgqojs", null, "usr_kdzhawzy7mxmp5hpb3bqlgqojs")).toBe("you");
    expect(actorWords("agt_abc123def")).toBe("an agent");
    expect(actorWords("ceo")).toBe("the owner");
    expect(actorWords("policy:deppol_x")).toBe("a deploy rule");
    expect(actorWords("BOSS")).toBe("BOSS");
    expect(looksLikeAnId(actorWords("usr_abcdefghij", "usr_abcdefghij"))).toBe(false);
  });
});

describe("no screen prints an actor field raw", () => {
  const files = [...walk("app"), ...walk("components")];
  it.each(files)("%s", (file) => {
    const src = readFileSync(join(ROOT, file), "utf8");
    const lines = src.split("\n").filter((l) => RAW.test(l) && !/data-testid|key=|personId=|actor=\{/.test(l));
    expect(lines, `print it through actorWords(...)`).toEqual([]);
  });
});
