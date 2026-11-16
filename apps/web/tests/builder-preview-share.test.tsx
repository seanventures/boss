// @vitest-environment happy-dom
/**
 * A builder issues a named gate pass to an in-progress app. The password is
 * not written into the page or the log after the pass is issued. An expired
 * pass and a stopped pass stay refused. An admin can stop new passes.
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { $, cleanup, click, render, type } from "./_helpers/kit-render";

const PASSWORD = "cedar-harbor-91";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

const { PreviewShare } = await import("../app/builder/preview-share");

function share(over: Record<string, unknown> = {}) {
  return {
    id: "psh_live",
    name: "Friday review",
    slug: "friday-review",
    href: "/preview-share/friday-review",
    appKey: "harbor-notes",
    expiresAt: "2026-11-16T18:00:00Z",
    stopped: false,
    expired: false,
    ...over,
  };
}

function screen(over: Record<string, unknown> = {}) {
  const onCreate = vi.fn(async () => share({ id: "psh_new", name: "Friday review" }));
  const onStop = vi.fn(async () => ({ stopped: true }));
  const onStopNew = vi.fn(async () => ({ newLinksStopped: true }));
  const onOpen = vi.fn(async () => ({ ok: false as const, reason: "password" as const }));
  const props = {
    projectId: "p1",
    appKey: "harbor-notes",
    appName: "Harbor notes",
    actor: "bea",
    admin: false,
    newLinksStopped: false,
    shares: [] as ReturnType<typeof share>[],
    onCreate,
    onStop,
    onStopNew,
    onOpen,
    ...over,
  };
  return { props, onCreate, onStop, onStopNew, onOpen };
}

describe("a preview gate pass", () => {
  it("issues a named link and does not keep the password on the page", async () => {
    const log = vi.spyOn(console, "log").mockImplementation(() => {});
    const { props, onCreate } = screen();
    await render(<PreviewShare {...props} />);
    await type('[data-testid="share-name"]', "Friday review");
    await type('[data-testid="share-password"]', PASSWORD);
    await type('[data-testid="share-expiry"]', "2026-11-16T18:00");
    await click('[data-testid="issue-pass"]');
    expect(onCreate).toHaveBeenCalledWith({
      name: "Friday review",
      password: PASSWORD,
      expiresAt: "2026-11-16T18:00",
    });
    expect(document.body.textContent).toContain("/preview-share/friday-review");
    expect(document.body.textContent).not.toContain(PASSWORD);
    const field = $('[data-testid="share-password"]') as HTMLInputElement;
    expect(field.type).toBe("password");
    expect(field.value).toBe("");
    expect(log.mock.calls.flat().join(" ")).not.toContain(PASSWORD);
    expect(document.querySelector("[data-testid='share-log']")).toBeNull();
  });

  it("refuses an expired pass and a stopped pass", async () => {
    const { props } = screen({
      shares: [
        share({ id: "psh_old", name: "Old tide", slug: "old-tide",
          href: "/preview-share/old-tide", expired: true }),
        share({ id: "psh_held", name: "Held gate", slug: "held-gate",
          href: "/preview-share/held-gate", stopped: true }),
      ],
    });
    await render(<PreviewShare {...props} />);
    const states = [...document.querySelectorAll("[data-testid='share-state']")].map(
      (node) => node.textContent);
    expect(states).toEqual(["Expired", "Stopped"]);
    expect(document.querySelector("[data-testid='open-pass']")).toBeNull();
  });

  it("an admin stops new passes and a builder does not see that control", async () => {
    const admin = screen({ admin: true, shares: [share()] });
    await render(<PreviewShare {...admin.props} />);
    expect($("[data-testid='stop-new']")).not.toBeNull();
    await click("[data-testid='stop-pass']");
    expect(admin.onStop).toHaveBeenCalledWith("psh_live");
    await click("[data-testid='stop-new']");
    expect(admin.onStopNew).toHaveBeenCalled();
    cleanup();

    const stopped = screen({ admin: true, newLinksStopped: true });
    await render(<PreviewShare {...stopped.props} />);
    const issue = $("[data-testid='issue-pass']") as HTMLButtonElement;
    expect(issue.disabled).toBe(true);
    expect(document.body.textContent).toContain("New passes are stopped.");
    await click("[data-testid='issue-pass']");
    expect(stopped.onCreate).not.toHaveBeenCalled();
    cleanup();

    const builder = screen({ admin: false });
    await render(<PreviewShare {...builder.props} />);
    expect($("[data-testid='stop-new']")).toBeNull();
    expect($("[data-testid='stop-pass']")).toBeNull();
  });

  it("a wrong password on a live pass is refused in a short line", async () => {
    const { props, onOpen } = screen({ shares: [share()] });
    await render(<PreviewShare {...props} />);
    await type("[data-testid='open-password']", "not-the-pass");
    await click("[data-testid='open-pass']");
    expect(onOpen).toHaveBeenCalledWith("friday-review", "not-the-pass");
    expect(document.body.textContent).toContain("Password refused.");
    expect(document.body.textContent).not.toContain("not-the-pass");
    for (const node of document.querySelectorAll("[title]")) {
      const words = (node.getAttribute("title") || "").split(/\s+/).filter(Boolean);
      expect(words.length).toBeLessThanOrEqual(14);
    }
    const intro = document.querySelector("p");
    expect(intro).not.toBeNull();
    expect((intro?.textContent || "").split(/\s+/).filter(Boolean).length).toBeGreaterThan(6);
  });
});
