// @vitest-environment happy-dom
/**
 * App ▸ Sign-in (APP_TEMPLATES_AND_SECURITY_LOOP.md §6.4): one press turns on Sign in with BOSS for an
 * app built from login-oauth. The sheet shows the client, the issuer and where people return to, in
 * words; never a secret.
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import realShape from "./_helpers/apps-real-shape.json";
import { $, $$, click, mount, settle, unmount } from "./_helpers/live-render";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }) }));
const use = vi.fn(async () => ({ ok: true, message: "Sign in with BOSS is on" }));
const stop = vi.fn(async () => ({ ok: true, message: "Sign in with BOSS is off for this app" }));
vi.mock("../app/factory/sign-in-actions", () => ({ useSignInWithBossAction: use, stopSignInWithBossAction: stop }));
vi.mock("../app/factory/links-actions", () => ({
  createLinkAction: vi.fn(), revokeLinkAction: vi.fn(), revealTestUsersAction: vi.fn(),
}));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const { AppsTab } = await import("../app/factory/apps-tab");
const { SignInSheet, parseSignIn, uriWords } = await import("../app/factory/sign-in-sheet");

afterEach(() => { unmount(); vi.unstubAllGlobals(); use.mockClear(); stop.mockClear(); });

const OFF = { app_key: "login demo", template_applied: true, connected: false, secrets: { preview: [], deploy: [] }, client: null };
const ON = {
  app_key: "login demo", template_applied: true, connected: true,
  secrets: { preview: ["OIDC_BOSS_CLIENT_ID", "OIDC_BOSS_CLIENT_SECRET", "OIDC_BOSS_ISSUER"],
             deploy: ["OIDC_BOSS_CLIENT_ID", "OIDC_BOSS_CLIENT_SECRET", "OIDC_BOSS_ISSUER"] },
  client: { client_id: "boc_123", status: "active", pool_id: "opl_1", allowed_scopes: ["openid"],
            redirect_uris: ["https://previews.example.test/v1/previews/prv_9/web/auth/callback/boss",
                            "https://login.example.test/auth/callback/boss"] },
  issuer: "https://boss.example.test/oidc/org_1",
};

function serve(...answers: unknown[]) {
  const queue = [...answers];
  const fetchMock = vi.fn(async (_url: unknown) => Response.json(queue.length > 1 ? queue.shift() : queue[0]));
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

describe("App ▸ Sign-in", () => {
  it("is off, then one press turns it on and shows the client and where people return to", async () => {
    const f = serve(OFF, ON);
    await mount(<SignInSheet projectId="p1" appKey="crun_1" app="Login demo" onClose={() => {}} />);
    await settle();
    expect(String(f.mock.calls[0][0])).toBe("/v1/apps/crun_1/sign-in-with-boss?project_id=p1");
    expect($("[data-testid='fx-signin-state']")!.textContent).toBe("Sign in with BOSS is off");
    expect($("[data-testid='fx-signin-stop']")).toBeNull();
    await click($("[data-testid='fx-signin-use']")!);
    await settle();
    expect(use).toHaveBeenCalledTimes(1);
    const sent = (use.mock.calls[0] as unknown as [FormData])[0];
    expect(sent.get("project_id")).toBe("p1");
    expect(sent.get("app_key")).toBe("crun_1");
    expect($("[data-testid='fx-signin-state']")!.textContent).toContain("Sign in with BOSS is on");
    expect($("[data-testid='fx-signin-state']")!.textContent).toContain("previews and deploys");
    expect($("[data-testid='fx-signin-client']")!.textContent).toBe("boc_123");
    expect($$(".fx-signin-uris li").map((li) => li.textContent)).toEqual(["preview prv_9", "https://login.example.test"]);
    expect($("[data-testid='fx-signin-use']")!.textContent).toBe("Refresh addresses");
    expect(document.body.textContent).not.toMatch(/secret[^s]|password/i);
  });

  it("asks for the login template first", async () => {
    serve({ ...OFF, template_applied: false });
    await mount(<SignInSheet projectId="p1" appKey="crun_1" app="Plain" onClose={() => {}} />);
    await settle();
    expect($("[data-testid='fx-signin-needs-template']")).not.toBeNull();
    expect($("[data-testid='fx-signin-use']")).toBeNull();
  });

  it("says why a press failed", async () => {
    serve(OFF);
    use.mockResolvedValueOnce({ ok: false, error: "This organisation has no sign-in pool yet." } as never);
    await mount(<SignInSheet projectId="p1" appKey="crun_1" app="Login demo" onClose={() => {}} />);
    await settle();
    await click($("[data-testid='fx-signin-use']")!);
    await settle();
    expect($("[role='alert']")!.textContent).toContain("no sign-in pool yet");
  });

  it("stops using it", async () => {
    serve(ON, OFF);
    await mount(<SignInSheet projectId="p1" appKey="crun_1" app="Login demo" onClose={() => {}} />);
    await settle();
    await click($("[data-testid='fx-signin-stop']")!);
    await settle();
    expect(stop).toHaveBeenCalledTimes(1);
    expect($("[data-testid='fx-signin-state']")!.textContent).toBe("Sign in with BOSS is off");
  });

  it("opens from the app row's menu", async () => {
    vi.stubGlobal("fetch", vi.fn(async (url: string) =>
      Response.json(String(url).includes("/sign-in-with-boss") ? OFF : realShape)));
    await mount(<AppsTab projectId="p1" runs={[]} previews={[]} query="" onOpen={() => {}} onNew={() => {}} />);
    await settle();
    const key = $$(".fx-app")[0]!.getAttribute("data-testid")!.replace("fx-app-", "");
    await click($(`[data-testid='fx-app-more-${key}']`)!);
    await settle();
    await click($(`[data-testid='fx-app-menu-signin-${key}']`)!);
    await settle();
    expect($("[data-testid='fx-signin-sheet']")).not.toBeNull();
  });

  it("parses defensively and names addresses in words", () => {
    expect(parseSignIn(null)).toBeNull();
    expect(parseSignIn({ connected: "yes" })).toBeNull();
    expect(parseSignIn({ connected: false, client: { nope: 1 } })!.client).toBeNull();
    expect(uriWords("http://localhost:3000/auth/callback/boss")).toBe("local (until a preview starts)");
  });
});
