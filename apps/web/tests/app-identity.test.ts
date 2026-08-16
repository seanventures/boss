import { describe, expect, it } from "vitest";

import { appIdentity } from "../app/app-identity";

/**
 * A card that leads with "Web app — # PRD: BugBox — Single-Project Issue Tracker **Status:**
 * Ready for hu" is showing pipeline exhaust, not a product. These pin the extraction, because
 * the wrappers are added in several places and drift silently.
 */
const run = (title: string, fctx: Record<string, unknown> = {}) =>
  ({ id: "crun_1", title, finalize_ctx: fctx }) as never;

describe("an app's identity, taken from the run that built it", () => {
  it("uses the benchmark's own name and tagline when there is one", () => {
    const id = appIdentity(run("anything at all", {
      qa_benchmark: { name: "BugBox", tagline: "Mini issue tracker", summary: "CRUD and search." },
    }));
    expect(id).toMatchObject({ name: "BugBox", kind: "Mini issue tracker", blurb: "CRUD and search." });
  });

  it("strips the Web app / PRD wrappers and the trailing status boilerplate", () => {
    const id = appIdentity(run(
      "Web app — # PRD: BugBox — Single-Project Issue Tracker **Status:** Ready for human review",
    ));
    expect(id.name).toBe("BugBox");
    expect(id.kind).toBe("Single-Project Issue Tracker");
    expect(id.kind).not.toMatch(/Status/);
  });

  it("names a follow-up after the app, not after the follow-up", () => {
    expect(appIdentity(run(
      "Follow-up: deferred review findings — ApproveIt: persist expenses",
    )).name).toBe("ApproveIt");
  });

  it("drops the slice prefix a decomposed build adds", () => {
    expect(appIdentity(run("[2/5] HookWatch — replay")).name).toBe("HookWatch");
  });

  it("never renders an empty name", () => {
    expect(appIdentity(run("")).name).toBe("Untitled app");
  });
});
