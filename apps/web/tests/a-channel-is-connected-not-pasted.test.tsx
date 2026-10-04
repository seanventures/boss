// @vitest-environment happy-dom
/**
 * Onboarding a channel, without the credential ever coming back out — now YouTube's drawer in
 * Skills & MCP (skills-mcp spec §3.1 Media, J9). Two steps, because OAuth cannot be one: the
 * Google client is an ordinary form (secret write-only), and a channel is authorised on Google's
 * consent screen in a new tab, which this screen notices when the tab comes back.
 *
 * Folds in a-button-that-cannot-work-says-so: Connect cannot be pressed before there is a client,
 * and it says why before it is pressed.
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { codeOnly } from "./_helpers/source";
import { $, cleanup, click, render, settle, type } from "./_helpers/kit-render";
import { installKitApi } from "./_helpers/kit-api";

const { YoutubeConnection } = await import("../app/skills/youtube-connection");
const ROUTE = codeOnly("app/api/channels/route.ts");

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe("connecting a channel", () => {
  it("the client secret is write-only, and cleared once saved", async () => {
    const calls = installKitApi();
    await render(<YoutubeConnection projectId="p1" actor="ceo" />);
    const secret = $('[data-testid="channel-client-secret"]') as HTMLInputElement;
    expect(secret.type).toBe("password");
    expect(secret.getAttribute("autocomplete")).toBe("new-password");
    await type('[data-testid="channel-client-id"]', "id.apps.googleusercontent.com");
    await type('[data-testid="channel-client-secret"]', "GOCSPX-secret");
    await click('[data-testid="channel-save"]');
    expect(calls.find((c) => c.body?.action === "save_client")!.body).toMatchObject({ client_secret: "GOCSPX-secret" });
    expect(($('[data-testid="channel-client-secret"]') as HTMLInputElement).value).toBe("");
  });

  it("the redirect URI is prefilled from what the API expects", async () => {
    installKitApi();
    await render(<YoutubeConnection projectId="p1" actor="ceo" />);
    expect(($('[data-testid="channel-redirect"]') as HTMLInputElement).value)
      .toBe("https://boss.example/v1/channels/youtube/callback");
  });

  it("connect cannot be pressed until a client exists, and says why", async () => {
    installKitApi();
    await render(<YoutubeConnection projectId="p1" actor="ceo" />);
    const connect = $('[data-testid="channel-connect"]') as HTMLButtonElement;
    expect(connect.disabled).toBe(true);
    expect(connect.dataset.tip).toBe("Save the Google client first");
  });

  it("consent opens in a new tab, and the screen re-reads when the operator comes back", async () => {
    const calls = installKitApi(undefined, (url, body) => {
      if (url.includes("view=client")) return { configured: true, redirect_uri: "https://x/cb", client_id_hint: "…1234" };
      if (body?.action === "connect") return { authorization_url: "https://accounts.google.com/o/oauth2" };
      return undefined;
    });
    const open = vi.spyOn(window, "open").mockReturnValue(null);
    await render(<YoutubeConnection projectId="p1" actor="ceo" />);
    await click('[data-testid="channel-connect"]');
    expect(open).toHaveBeenCalledWith("https://accounts.google.com/o/oauth2", "_blank", "noopener");
    const before = calls.filter((c) => c.method === "GET").length;
    window.dispatchEvent(new Event("focus"));
    await settle();
    expect(calls.filter((c) => c.method === "GET").length).toBeGreaterThan(before);
    open.mockRestore();
  });

  it("disconnect asks first", async () => {
    const calls = installKitApi(undefined, (url) => {
      if (url.includes("view=client")) return { configured: true, redirect_uri: "https://x/cb" };
      if (url.startsWith("/api/channels")) return { channels: [{ connection_key: "youtube", status: "active", health: "healthy", channel: { title: "Makkal" } }] };
      return undefined;
    });
    await render(<YoutubeConnection projectId="p1" actor="ceo" />);
    await click('[data-testid="channel-disconnect-youtube"]');
    expect(calls.some((c) => c.body?.action === "disconnect")).toBe(false);
    await click('[data-testid="channel-disconnect-yes"]');
    expect(calls.find((c) => c.body?.action === "disconnect")!.body).toMatchObject({ connection_key: "youtube" });
  });

  it("the route takes its action from an allowlist, never from a path", () => {
    expect(ROUTE).toContain('const ALLOWED = ["save_client", "connect", "disconnect"] as const;');
    expect(ROUTE).not.toMatch(/\$\{[^}]*body\.action[^}]*\}/);
  });
});
