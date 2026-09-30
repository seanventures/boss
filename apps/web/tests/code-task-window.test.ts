import { describe, expect, it } from "vitest";

import { codeOnly } from "./_helpers/source";

/**
 * Every read of the code-task list must drop BOSS's own maintenance runs.
 *
 * The console fetches N newest and filters internal runs client-side, so a burst of
 * housekeeping does not merely add noise — it consumes the whole window. Fixing that in ONE
 * call site made it worse in a way that was harder to diagnose than the original bug:
 *
 *   - the server render used the filter, so the page arrived with the operator's apps on it
 *   - the LIVE POLL did not, so a few seconds later it replaced them with 200 internal runs
 *     that the client then dropped, and the grid emptied
 *   - and the poll only runs while a task is live, i.e. exactly when someone is watching
 *
 * Reported as "sometimes it displays and then vanishes". One filtered path and one unfiltered
 * path is worse than two unfiltered ones.
 */
const FILES = ["app/actions.ts", "lib/code-tasks.ts", "lib/pipeline-profiles.ts"];

describe("code-task reads", () => {
  it("every direct URL asks the API to exclude internal runs", () => {
    const offenders: string[] = [];
    for (const file of FILES) {
      const src = codeOnly(file);
      for (const m of src.matchAll(/`[^`]*\/v1\/code-tasks\?[^`]*`/g)) {
        if (!m[0].includes("exclude_internal")) offenders.push(`${file}: ${m[0].slice(0, 90)}`);
      }
    }
    expect(offenders).toEqual([]);
  });

  it("the typed client call passes the flag too", () => {
    // lib/code-tasks.ts goes through client.getCodeTasks rather than a URL.
    const src = codeOnly("lib/code-tasks.ts");
    // Matched to the end of the LINE, not to the first ")": the limit argument is itself a
    // call, so a lazy paren match stops inside it and the assertion fails against correct code.
    const call = src.split("\n").find((l) => l.includes("client.getCodeTasks(")) ?? "";
    expect(call).toBeTruthy();
    expect(call).toMatch(/,\s*true\s*\)/);
  });
});
