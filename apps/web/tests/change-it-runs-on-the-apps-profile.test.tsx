// @vitest-environment happy-dom
/**
 * CHANGE IT RUNS ON THE APP'S OWN PROFILE, OR THE ONE PICKED (AR-22).
 *
 * "Change it" on a lean app's build page started the change "balanced" — the engine picked by
 * the change's size and the sheet offered no choice. The sheet now opens on the lineage's profile
 * (the build's `profile`), offers lean / balanced / strict, and sends the pick with the proposal.
 */
import React, { act } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { $, click, mount, settle, unmount } from "./_helpers/live-render";

const actions = vi.hoisted(() => ({ propose: vi.fn(), start: vi.fn() }));
vi.mock("../app/actions", () => ({
  submitCodeTaskFeedbackAction: (...args: unknown[]) => actions.propose(...args),
  confirmCodeTaskFeedbackAction: (...args: unknown[]) => actions.start(...args),
  conveneMeetingAction: vi.fn(),
}));
vi.mock("../components/identity/identity-provider", () => ({ useCan: () => ({ allowed: true }) }));

import { CHANGE_PROFILES, ChangeIt, defaultChangeProfile } from "../app/factory/change-it";

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  actions.propose.mockReset();
  actions.propose.mockResolvedValue({ thread_id: "fbt_1", status: "awaiting_confirmation", proposal: { fix_note: "x", classification: "scoped_tweak", path: "inline" } });
  vi.stubGlobal("fetch", vi.fn(async () => Response.json({ items: [] })));
});
afterEach(() => { unmount(); vi.unstubAllGlobals(); });

const select = () => $("[data-testid=fx-change-profile]") as HTMLSelectElement | null;
const sentProfile = () => (actions.propose.mock.calls[0][1] as FormData).get("profile");

async function propose(words = "Add a /healthz route that reports the upstream pool") {
  const box = $("[data-testid=fx-change-what]") as HTMLTextAreaElement;
  Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!.call(box, words);
  await act(async () => { box.dispatchEvent(new Event("input", { bubbles: true })); });
  await settle();
  await click($("[data-testid=fx-change-propose]"));
  await settle();
}

describe("Change it's profile", () => {
  it("opens on a lean app's own profile and sends it", async () => {
    await mount(<ChangeIt projectId="northgate-gateway" runId="crun_9a9a" actor="ceo" profile="lean" onClose={() => {}} />);
    expect(select()?.value).toBe("lean");
    expect(Array.from(select()!.options).map((o) => o.value)).toEqual(["lean", "balanced", "strict"]);
    expect(select()!.selectedOptions[0].textContent).toContain("this app's");
    await propose();
    expect(actions.propose).toHaveBeenCalledTimes(1);
    expect(sentProfile()).toBe("lean");
  });

  it("sends the profile the person picks instead", async () => {
    await mount(<ChangeIt projectId="northgate-gateway" runId="crun_9a9a" actor="ceo" profile="lean" onClose={() => {}} />);
    const s = select()!;
    s.value = "strict";
    await act(async () => { s.dispatchEvent(new Event("change", { bubbles: true })); });
    await settle();
    await propose();
    expect(sentProfile()).toBe("strict");
  });

  it("an app with no recorded profile opens on balanced; an unknown one never reaches the API", () => {
    expect(defaultChangeProfile(undefined)).toBe("balanced");
    expect(defaultChangeProfile("vag")).toBe("balanced");
    expect(defaultChangeProfile("Strict")).toBe("strict");
    expect(CHANGE_PROFILES.map((p) => p.key)).toEqual(["lean", "balanced", "strict"]);
  });
});
