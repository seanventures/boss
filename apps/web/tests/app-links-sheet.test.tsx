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
vi.mock("../app/factory/links-actions", () => ({
  createLinkAction: vi.fn(), revokeLinkAction: vi.fn(), revealTestUsersAction: vi.fn(),
}));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const { AppsTab } = await import("../app/factory/apps-tab");
const { LinksSheet, linkWords, parseLinks } = await import("../app/factory/links-sheet");

afterEach(() => { unmount(); vi.unstubAllGlobals(); });

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
    expect(row.textContent).toContain("http://egress:18100");
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
    click($(`[data-testid='fx-app-more-${key}']`)!);
    await settle();
    click($(`[data-testid='fx-app-menu-links-${key}']`)!);
    await settle();
    expect($("[data-testid='fx-links-sheet']")).not.toBeNull();
    click($("[data-testid='fx-link-add']")!);
    await settle();
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
});
