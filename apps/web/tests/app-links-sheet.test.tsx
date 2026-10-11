// @vitest-environment happy-dom
/**
 * App ▸ Links (APP_TEMPLATES_AND_SECURITY_LOOP.md §5.5): one surface, opened from an app row's
 * menu. Rows say `ALIAS → App · preview · test users · active` with the address (copy), last
 * used and Revoke; no credential value is ever on the page until a person presses Reveal (D9).
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import realShape from "./_helpers/apps-real-shape.json";
import { $, $$, click, mount, settle, unmount } from "./_helpers/live-render";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }) }));
const actions = vi.hoisted(() => ({
  createLinkAction: vi.fn(), revokeLinkAction: vi.fn(), revealTestUsersAction: vi.fn(),
}));
vi.mock("../app/factory/links-actions", () => actions);
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const { AppsTab } = await import("../app/factory/apps-tab");
const { LinksSheet, linkAddress, linkWords, parseLinks } = await import("../app/factory/links-sheet");

afterEach(() => { unmount(); vi.unstubAllGlobals(); vi.clearAllMocks(); });

const LINKS = {
  app_key: "bank attacker",
  links: [
    { id: "lnk_1", alias: "BANK", provider_app_key: "banking", provider_app: "Banking", env: "preview",
      credentials: "test_users", status: "active", url: "http://egress:18100", env_prefix: "BOSS_LINK_BANK_",
      test_users: { count: 3, generation: 2 }, last_used_at: null, gateway_port: 18100 },
    { id: "lnk_0", alias: "OLD", provider_app_key: "banking", provider_app: "Banking", env: "preview",
      credentials: "none", status: "revoked", url: null, env_prefix: "BOSS_LINK_OLD_", test_users: null },
  ],
};

describe("App ▸ Links", () => {
  it("reads as one line per link, with no credential value anywhere", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Response.json(LINKS)));
    await mount(<LinksSheet projectId="p1" appKey="r1" app="Bank attacker" others={[{ key: "r2", app: "Banking" }]}
                            onClose={() => {}} />);
    await settle();
    const row = $("[data-testid='fx-link-BANK']")!;
    expect(row.querySelector(".fx-link-words")!.textContent).toBe("BANK → Banking · preview · test users · active");
    // THE APP-FACING ADDRESS (tester P2): the env var the app reads, never the gateway's
    // internal host, which a person cannot open and which means nothing to them.
    expect(row.textContent).toContain("BOSS_LINK_BANK_URL");
    expect(document.body.textContent).not.toContain("egress");
    expect(row.textContent).toContain("never used");
    expect(row.textContent).toContain("3 test users");
    expect($("[data-testid='fx-link-OLD']")).toBeNull();
    expect($(".fx-links-revoked")!.textContent).toBe("1 revoked");
    expect($("[data-testid='fx-link-reveal-BANK']")).not.toBeNull();
    expect($("[data-testid='fx-link-users-BANK']")).toBeNull();
    expect(document.body.textContent).not.toMatch(/password|boss_lnk_/i);
  });

  it("opens from the app row's menu, offering only the project's other apps", async () => {
    vi.stubGlobal("fetch", vi.fn(async (url: string) => Response.json(String(url).includes("/links") ? LINKS : realShape)));
    await mount(<AppsTab projectId="p1" runs={[]} previews={[]} query="" onOpen={() => {}} onNew={() => {}} />);
    await settle();
    const first = $$(".fx-app")[0]!;
    const key = first.getAttribute("data-testid")!.replace("fx-app-", "");
    await click($(`[data-testid='fx-app-more-${key}']`)!);
    await click($(`[data-testid='fx-app-menu-links-${key}']`)!);
    expect($("[data-testid='fx-links-sheet']")).not.toBeNull();
    // A SIDE SHEET over the whole console, not a modal inside the Apps panel.
    expect($("[data-testid='fx-links-sheet']")!.classList.contains("tpl-sheet")).toBe(true);
    await click($("[data-testid='fx-link-add']")!);
    const options = $$("[data-testid='fx-link-provider'] option").map((o) => o.getAttribute("value")).filter(Boolean);
    expect(options.length).toBe((realShape as { apps: unknown[] }).apps.length - 1);
    expect(options).not.toContain(key);
  });

  it("parses defensively", () => {
    expect(parseLinks(null)).toEqual([]);
    expect(parseLinks({ links: [{ nope: 1 }] })).toEqual([]);
    expect(linkWords({ ...LINKS.links[0], env: "deploy", credentials: "none" } as never))
      .toBe("BANK → Banking · deployed · active");
  });

  it("revoke asks first, and only the second press revokes", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Response.json(LINKS)));
    actions.revokeLinkAction.mockResolvedValue({ ok: true, message: "Link revoked" });
    await mount(<LinksSheet projectId="p1" appKey="r1" app="Bank attacker" others={[{ key: "r2", app: "Banking" }]} onClose={() => {}} />);
    await click($("[data-testid='fx-link-revoke-BANK']"));
    expect(actions.revokeLinkAction).not.toHaveBeenCalled();
    const ask = $("[data-testid='fx-link-confirm-BANK']")!;
    expect(ask.textContent).toContain("Revoke BANK?");
    expect(ask.textContent).toContain("Bank attacker loses Banking's address");
    await click($("[data-testid='fx-link-revoke-yes-BANK']"));
    expect(actions.revokeLinkAction).toHaveBeenCalledTimes(1);
  });

  it("revealed test users are a table with headers, and Hide puts them away", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Response.json(LINKS)));
    actions.revealTestUsersAction.mockResolvedValue({ ok: true, data: { users: [
      { username: "boss-test-1@banking.test", password: "pw-one", role: "user" },
      { username: "boss-test-2@banking.test", password: "pw-two", role: "admin" },
    ] } });
    await mount(<LinksSheet projectId="p1" appKey="r1" app="Bank attacker" others={[]} onClose={() => {}} />);
    await click($("[data-testid='fx-link-reveal-BANK']"));
    const table = $("[data-testid='fx-link-users-BANK'] table")!;
    expect($$("[data-testid='fx-link-users-BANK'] th").map((th) => th.textContent)).toEqual(["Login", "Password", "Role"]);
    expect(table.querySelectorAll("tbody tr").length).toBe(2);
    await click($("[data-testid='fx-link-hide-BANK']"));
    expect($("[data-testid='fx-link-users-BANK']")).toBeNull();
    expect(document.body.textContent).not.toContain("pw-one");
  });

  it("a bad alias says what an alias is before any press, and a refusal shows the action's words", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ links: [] })));
    actions.createLinkAction.mockResolvedValue({ ok: false, error: "BANK is already used by another link of this app. Choose another alias." });
    await mount(<LinksSheet projectId="p1" appKey="r1" app="Bank attacker" others={[{ key: "r2", app: "Banking" }]} onClose={() => {}} />);
    await click($("[data-testid='fx-link-add']"));
    const input = $("[data-testid='fx-link-alias']") as HTMLInputElement;
    const setValue = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
    const { act } = await import("react");
    await act(async () => { setValue.call(input, "9x"); input.dispatchEvent(new Event("input", { bubbles: true })); });
    await settle();
    expect($("[data-testid='fx-link-alias-why']")!.textContent).toMatch(/starting with a letter/);
    expect(($("[data-testid='fx-link-submit']") as HTMLButtonElement).disabled).toBe(true);
    await act(async () => { setValue.call(input, "bank"); input.dispatchEvent(new Event("input", { bubbles: true })); });
    await settle();
    expect(input.value).toBe("BANK");
    expect($("[data-testid='fx-link-alias-why']")).toBeNull();
    const form = $("[data-testid='fx-link-add-form']") as HTMLFormElement;
    (form.querySelector("select[name=provider_app_key]") as HTMLSelectElement).value = "r2";
    await act(async () => { form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })); });
    await settle();
    expect($("[data-testid='fx-link-error']")!.textContent).toBe("BANK is already used by another link of this app. Choose another alias.");
    expect($("[data-testid='fx-link-error']")!.textContent).not.toMatch(/reload/i);
  });

  it("a deployed link shows the provider's public address; a revoked one none", () => {
    const base = LINKS.links[0] as never as Parameters<typeof linkAddress>[0];
    expect(linkAddress({ ...base, env: "deploy", url: "https://bank.example.com" })!.text).toBe("https://bank.example.com");
    expect(linkAddress({ ...base, env: "deploy", url: null })).toBeNull();
    expect(linkAddress({ ...base, status: "revoked" })).toBeNull();
    expect(linkAddress(base)!.copy).toBe("BOSS_LINK_BANK_URL");
  });
});
