// @vitest-environment happy-dom
/**
 * A run the Infra Agent holds (wait code `infra_hold`) wears one chip, "Paused by Infra Agent",
 * with the agent's face, on every surface a run shows why it waits (WhyLine); every other wait
 * reason draws as before.
 */
import React from "react";
import { afterEach, describe, expect, it } from "vitest";

import { WhyLine, isInfraHold } from "../components/stuck-work";
import { parseWaitReason } from "../lib/stuck-work";
import { $, mount, unmount } from "./_helpers/live-render";

afterEach(() => unmount());

describe("a run paused by the Infra Agent", () => {
  it("wears the chip with the agent's face and the checkpoint promise", async () => {
    const why = parseWaitReason({ code: "infra_hold", who: "boss", words: "Paused by Infra Agent", detail: "Storage incident on laptop-boss · resumes from its checkpoint" })!;
    await mount(<WhyLine why={why} testId="why" />);
    const chip = $('[data-testid="why"]')!;
    expect(chip.classList.contains("sw-why-infra")).toBe(true);
    expect(chip.dataset.code).toBe("infra_hold");
    expect(chip.textContent).toContain("Paused by Infra Agent");
    expect(chip.textContent).toContain("resumes from its checkpoint");
    expect(chip.querySelector("svg")).not.toBeNull();
    expect(chip.getAttribute("title")).toContain("Paused by Infra Agent");
  });

  it("is compact in a row: the words only", async () => {
    const why = parseWaitReason({ code: "infra_hold:checkpoint", who: "boss", words: "Paused by Infra Agent", detail: "long detail" })!;
    await mount(<WhyLine why={why} testId="why" compact />);
    expect($('[data-testid="why"]')!.textContent).not.toContain("long detail");
    expect($('[data-testid="why"]')!.getAttribute("title")).toContain("long detail");
  });

  it("leaves every other wait reason as it was", async () => {
    const why = parseWaitReason({ code: "queue_full", who: "queue", words: "In the queue", detail: "" })!;
    await mount(<WhyLine why={why} testId="why" />);
    expect($('[data-testid="why"]')!.classList.contains("sw-why-infra")).toBe(false);
    expect(isInfraHold("infra_hold")).toBe(true);
    expect(isInfraHold("infra_hold_checkpoint")).toBe(true);
    expect(isInfraHold("github_wait")).toBe(false);
    expect(isInfraHold(null)).toBe(false);
  });
});
