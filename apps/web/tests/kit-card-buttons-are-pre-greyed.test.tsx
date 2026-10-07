// @vitest-environment happy-dom
/**
 * A kit card's Test and Turn on are drawn greyed for a viewer who cannot run or enable the
 * ability: aria-disabled, the reason in the tooltip, and a press does nothing — not the action,
 * and not the card's drawer behind it. A viewer who can sees the same button as before.
 */
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import { IdentityProvider } from "../components/identity/identity-provider";
import { PrimaryAction, type CardPermissions } from "../app/skills/ability-card";
import type { MePermissionsResponse, MeResponse } from "../lib/identity/contract";
import type { Ability } from "../lib/kit-types";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn() }), usePathname: () => "/", useSearchParams: () => new URLSearchParams() }));

const PERMS: CardPermissions = { test: "skill:invoke", turn_on: "skill:manage" };
const me = { principal_id: "usr_v", display_name: "V", org: { id: "o", name: "O" }, csrf_token: "c", epoch: 1 } as unknown as MeResponse;
const org = (permissions: string[]): MePermissionsResponse => ({ epoch: 1, scope: "org", permissions, step_up_fresh_until: null, catalogue_fingerprint: "x" });
const ability = { key: "web_search", label: "Web search" } as unknown as Ability;

let host: HTMLElement | null = null;
afterEach(() => { host?.remove(); host = null; });

async function render(status: "ready" | "off", held: string[]) {
  const onAction = vi.fn();
  const onOpen = vi.fn();
  host = document.createElement("div");
  document.body.appendChild(host);
  await act(async () => createRoot(host!).render(
    <IdentityProvider me={me} org={org(held)}>
      <div onClick={onOpen}><PrimaryAction ability={ability} status={status as never} onAction={onAction} permissions={PERMS} /></div>
    </IdentityProvider>,
  ));
  const btn = host.querySelector("[data-testid='kit-action-web_search']") as HTMLButtonElement;
  return { btn, onAction, onOpen };
}

describe("kit card buttons", () => {
  it.each([["ready", "test"], ["off", "turn_on"]] as const)("%s: refused is greyed with the reason and does nothing", async (status, action) => {
    const { btn, onAction, onOpen } = await render(status, []);
    expect(btn.getAttribute("aria-disabled")).toBe("true");
    expect(btn.getAttribute("title")).toMatch(/^Needs “.+” · ask an /);
    expect(btn.getAttribute("data-permission")).toBe(PERMS[action]);
    await act(async () => btn.click());
    expect(onAction).not.toHaveBeenCalled();
    expect(onOpen).not.toHaveBeenCalled();
  });

  it("allowed, the button runs its action as before", async () => {
    const { btn, onAction, onOpen } = await render("ready", ["skill:invoke", "skill:manage"]);
    expect(btn.getAttribute("aria-disabled")).toBeNull();
    await act(async () => btn.click());
    expect(onAction).toHaveBeenCalledWith("test");
    expect(onOpen).not.toHaveBeenCalled();
  });
});
