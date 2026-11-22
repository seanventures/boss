// @vitest-environment happy-dom
/**
 * DRY RUN 3, GAP 9: "Propose v2" opened change #20 while #15 "v2" was open. The engine refuses a
 * second proposal of the same version (409 `already_open`, with the change and its link); the
 * editor says "v2 is already open as change #15" and links to it, in place of an error.
 */
import React, { act } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { $, mount, settle, unmount } from "./_helpers/live-render";
import { alreadyOpen, parseRulesets } from "../lib/validation-review";
import { RULESET_NORTHGATE } from "../lib/validation-review-fixtures";
import { RulesetEditor } from "../app/validation/ruleset-editor";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(window.location.search),
  usePathname: () => "/",
}));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
(globalThis as Record<string, unknown>).React = React;
afterEach(() => { unmount(); vi.unstubAllGlobals(); });

const press = async (el: Element | null) => { await act(async () => { (el as HTMLElement).click(); }); await settle(); };
const type = async (el: Element | null, value: string) => {
  await act(async () => {
    const input = el as HTMLInputElement | HTMLTextAreaElement;
    const proto = input instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto, "value")!.set!.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
};

describe("gap 9: one open proposal per ruleset version", () => {
  it("reads the engine's already_open refusal; anything else is not one", () => {
    expect(alreadyOpen({ error: "already_open", message: "v2 is already open as change #15.", change: 15, version: 2 }, "northgate-gateway"))
      .toEqual({ text: "v2 is already open as change #15.", change: 15, href: "/changes/northgate-gateway/15" });
    expect(alreadyOpen({ error: "already_open", change: 15, version: 2 }, "p")!.text).toBe("v2 is already open as change #15.");
    expect(alreadyOpen({ error: "stale_base", change: 15 }, "p")).toBeNull();
    expect(alreadyOpen(null, "p")).toBeNull();
  });

  it("a second propose says 'v4 is already open as change #15' and links to it", async () => {
    let posted = 0;
    vi.stubGlobal("fetch", vi.fn(async (_url: string, init?: RequestInit) => {
      if ((init?.method ?? "GET") === "POST") {
        posted += 1;
        return new Response(JSON.stringify({ error: "already_open", message: "v4 is already open as change #15.", change: 15, version: 4,
          url: "/changes/northgate-gateway/15" }), { status: 409, headers: { "content-type": "application/json" } });
      }
      return new Response("{}", { status: 200, headers: { "content-type": "application/json" } });
    }));
    await mount(<RulesetEditor rs={parseRulesets([RULESET_NORTHGATE])[0]} projectId="northgate-gateway" />);
    await type($('[data-rule="cors-no-wildcard-with-credentials"] [data-testid="vr-rule-guide"]'), "Default to allow_credentials: false.");
    await press($('[data-testid="vr-ruleset-propose"]'));
    expect(posted).toBe(1);
    const said = $('[data-testid="vr-ruleset-said"]')!;
    expect(said.textContent).toContain("v4 is already open as change #15.");
    expect(said.getAttribute("data-tone")).toBe("amber");
    expect(said.querySelector("a")!.getAttribute("href")).toBe("/changes/northgate-gateway/15");
  });
});
