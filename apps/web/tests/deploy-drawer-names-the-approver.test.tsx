// @vitest-environment happy-dom

import React, { act } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { DeployDrawer } from "../app/infra/deploy-drawer";
import { deployDetailFixture, mockInfraFetch, type W } from "./_helpers/infra-fixtures";
import { byTestId, mount, type Mounted } from "./_helpers/infra-tab-render";

/**
 * The deploy drawer says who approved. An actor is that person. A role key with no actor is the
 * role, not a made-up name. Nothing recorded is one sentence, not a guess.
 */

let m: Mounted | null = null;
let restore: (() => void) | null = null;
beforeEach(() => { vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval"] }); });
afterEach(async () => {
  await m?.unmount(); m = null; restore?.(); restore = null;
  vi.useRealTimers();
});

function detail(approver: unknown): W {
  const base = deployDetailFixture("stack:sd_ok");
  return { ...base, live: false, row: { ...(base.row as W), approver } };
}

async function show(approver: unknown) {
  const f = mockInfraFetch({ routes: [{ match: /^\/v1\/infra\/deploys\/[^/]+$/, reply: () => detail(approver) }] });
  restore = f.install();
  m = await mount(<DeployDrawer projectId="project_1" actor="ceo" deployRef="stack:sd_ok" onClose={() => {}} />);
  await act(async () => { await vi.advanceTimersByTimeAsync(0); });
}

const line = () => byTestId("inf-deploy-approver")?.textContent ?? "";

describe("the deploy drawer names who approved", () => {
  it.each([
    null,
    {},
    { actor: "", role_key: "" },
    { actor: "   ", role_key: "  " },
  ])("says the approval was not recorded for %j", async (approver) => {
    await show(approver);
    expect(line()).toBe("The approval was not recorded.");
  });

  it("names the actor and does not treat a role key as their name", async () => {
    await show({ actor: "Ada Lovelace", role_key: "deployment_manager" });
    expect(line()).toBe("Approved by Ada Lovelace.");
    expect(line()).not.toContain("deployment_manager");
    expect(line()).not.toContain("Deployment Manager");
  });

  it("says you when the server recorded you, not the role", async () => {
    await show({ actor: "you", role_key: "deployment_manager" });
    expect(line()).toBe("Approved by you.");
  });

  it("says you when the approver is the person reading the drawer", async () => {
    await show({ actor: "ceo", role_key: "deployment_manager" });
    expect(line()).toBe("Approved by you.");
  });

  it("does not invent a name or print an id when only an actor id was recorded", async () => {
    await show({ actor: "usr_kdzhawzy7mxmp5hpb3bqlgqojs", role_key: "deployment_manager" });
    expect(line()).toBe("Approved by a person.");
    expect(line()).not.toContain("usr_");
    expect(line()).not.toContain("Deployment Manager");
  });

  it("shows a role key with no actor as the role that approved", async () => {
    await show({ role_key: "deployment_manager" });
    expect(line()).toBe("Approved by the Deployment Manager role.");
    expect(line()).not.toMatch(/\b(Ada|Vera|you|someone|a person)\b/);
  });
});
