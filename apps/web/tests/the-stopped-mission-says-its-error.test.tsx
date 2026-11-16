/**
 * A stopped mission kept its error only as a tooltip. The flight row prints part
 * text and drops tips, so the error has to be one of those parts. A sentence of
 * more than six words is a paragraph. "Failed, step N" stays.
 */
import React from "react";
import { afterEach, describe, expect, it } from "vitest";

import { mission } from "./_helpers/live-board-fixture";
import { $, mount, unmount } from "./_helpers/live-render";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const { MissionRow, missionErrorSentence, nowParts } = await import("../app/agents/mission-row");
const { parseMission } = await import("../lib/agents-live-board");

const ERROR = "The sandbox ran out of disk before the build could finish";

afterEach(() => { unmount(); });

describe("the stopped mission says its error", () => {
  it("uses the trimmed error as the sentence, and nothing when the error is blank", () => {
    expect(missionErrorSentence(`  ${ERROR}  `)).toBe(ERROR);
    expect(missionErrorSentence("   ")).toBeNull();
    expect(missionErrorSentence("")).toBeNull();
    expect(missionErrorSentence(null)).toBeNull();
    expect(missionErrorSentence(undefined)).toBeNull();
  });

  it("puts that sentence in a part's text and keeps Failed, step N", () => {
    const parts = nowParts(parseMission(mission({
      state: "stopped", step_index: 3, error: `  ${ERROR}  `,
    })), null);
    expect(parts.map((p) => p.text)).toContain("Failed, step 3");
    expect(parts.map((p) => p.text)).toContain(ERROR);
  });

  it("shows a sentence of more than six words as a paragraph", async () => {
    const m = parseMission(mission({ state: "stopped", step_index: 3, error: `  ${ERROR}  ` }));
    await mount(<ul><MissionRow mission={m} band="wide" now={null} /></ul>);
    const now = $('[data-testid="ag-mission-wfr_182"] .ag-mission-now');
    const paragraph = now?.querySelector("p");
    expect(paragraph?.textContent).toBe(ERROR);
    expect(paragraph?.tagName).toBe("P");
    expect(now?.textContent).toContain("Failed, step 3");
  });
});
