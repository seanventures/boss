import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

/**
 * Lineage is a chain, and `kind` names only the nearest hop.
 *
 * The real path is Discovery -> task -> PRD meeting -> build. Promoting a discovery outcome
 * stores `discovery_run_id` in the TASK's inputs; the `origin` dict was assembled at the LAST
 * hop and dropped it. So a build that genuinely started from a research dossier arrived at the
 * journey spine with `kind === "meeting"` and rendered "Research: none" — the operator had no
 * way to see that the panel they approved came from anything at all.
 *
 * The fix reads the FIELD rather than the kind: `discovery_run_id` is present only when
 * research actually happened, whatever the last hop was called.
 */

const SPINE = readFileSync(resolve(__dirname, "../app/journey-spine-ui.tsx"), "utf8");
const DECISIONS = readFileSync(
  resolve(__dirname, "../../../engine/boss_engine/services/decisions.py"),
  "utf8",
);

describe("the journey spine", () => {
  it("claims Research from the field, not from the nearest hop's name", () => {
    expect(SPINE).toContain("origin?.discovery_run_id");
    expect(SPINE).toMatch(/fromDiscovery =\s*Boolean\(discoveryRunId\)/);
  });

  it("still honours a run that came straight from a discovery", () => {
    // Some runs really do have kind === "discovery"; the new rule must not drop them.
    expect(SPINE).toContain('origin?.kind === "discovery"');
  });

  it("links to the specific dossier when it knows which one", () => {
    expect(SPINE).toContain('area: "discovery"');
    expect(SPINE).toContain("discovery: discoveryRunId");
  });

  it("still leaves Research hollow when there was none", () => {
    // A spine that claims every stage is the reason I32 was caught at all — it must keep
    // being able to say "this did not happen".
    expect(SPINE).toContain('sub: fromDiscovery ? "open dossier ↗" : "none"');
  });

  it("types the field so a typo cannot silently read undefined", () => {
    expect(SPINE).toContain("discovery_run_id?: string;");
  });
});

describe("the origin that feeds it", () => {
  it("reads one level further to find the discovery", () => {
    expect(DECISIONS).toContain("discovery_run_id");
    expect(DECISIONS).toContain("SELECT inputs FROM tasks WHERE id = :t");
  });

  it("omits the field entirely when there was no research", () => {
    // Present-but-null would make `Boolean(discoveryRunId)` false anyway, but an absent key is
    // the honest encoding of "this did not happen".
    expect(DECISIONS).toContain(
      '**({"discovery_run_id": discovery_run_id} if discovery_run_id else {}),',
    );
  });

  it("cannot fail the build when the back-link is missing", () => {
    const block = DECISIONS.slice(DECISIONS.indexOf("SELECT inputs FROM tasks"));
    expect(block.slice(0, 600)).toContain("except Exception");
  });
});


// --------------------------------------------------------------------------- //
// Where an app came from is a property of the APP, not of its newest run
// --------------------------------------------------------------------------- //
describe("the card spine's origin", () => {
  it("takes origin from whichever run in the group recorded one", async () => {
    const { appOrigin } = await import("../app/code-tasks-ui");
    const members = [
      { finalize_ctx: { is_followup: true } },            // newest: a follow-up, no origin
      { finalize_ctx: { origin: { kind: "meeting", meeting_run_id: "mrun_1" } } },
    ];
    expect(appOrigin(members)).toEqual({ kind: "meeting", meeting_run_id: "mrun_1" });
  });

  it("returns undefined when no run in the group has one", async () => {
    const { appOrigin } = await import("../app/code-tasks-ui");
    expect(appOrigin([{ finalize_ctx: {} }, { finalize_ctx: null }])).toBeUndefined();
  });

  it("prefers the newest origin when several runs carry one", async () => {
    const { appOrigin } = await import("../app/code-tasks-ui");
    const members = [
      { finalize_ctx: { origin: { kind: "meeting", meeting_run_id: "newest" } } },
      { finalize_ctx: { origin: { kind: "meeting", meeting_run_id: "older" } } },
    ];
    expect((appOrigin(members) as { meeting_run_id: string }).meeting_run_id).toBe("newest");
  });

  it("the engine carries origin across the follow-up hop", () => {
    // Fixing only the console would leave every NEW follow-up wrong again.
    const svc = readFileSync(
      resolve(__dirname, "../../../engine/boss_engine/services/governed_code_task_service.py"),
      "utf8",
    );
    expect(svc).toContain("parent_origin");
    expect(svc).toContain('**({"origin": parent_origin} if parent_origin else {})');
  });
});
