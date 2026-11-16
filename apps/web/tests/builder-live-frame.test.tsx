// @vitest-environment happy-dom
/**
 * The running app sits beside the chat, in a device frame, with Publish, while the
 * build is still going (BOSSAPP-1241). The build is a fake. Nothing here starts one.
 */
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { $, click, mount, unmount } from "./_helpers/live-render";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const { LiveFrame } = await import("../app/builder/live-frame");

beforeEach(() => {
  vi.spyOn(console, "error").mockImplementation(() => {});
  const settings = (window as unknown as { happyDOM?: { settings?: { disableIframePageLoading?: boolean } } }).happyDOM?.settings;
  if (settings) settings.disableIframePageLoading = true;
});

afterEach(() => {
  unmount();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

const NOT_CONNECTED = "Connect GitHub first: a new project reaches a repository through the "
  + "organisation's GitHub App (Setup › GitHub).";

const FAKE = {
  id: "build_fake_1241",
  title: "Harbor desk",
  status: "building",
  appUrl: "https://preview.invalid/harbor-desk",
  chat: [
    { role: "you", text: "A desk for the harbor office." },
    { role: "boss", text: "The desk is up." },
  ],
  fake: true,
};

function words(text: string): number {
  return text.replace(/[·▸•|]/g, " ").trim().split(/\s+/).filter(Boolean).length;
}

function copyAudit(root: Element): { long: string[]; tips: string[] } {
  const long: string[] = [];
  for (const el of [root, ...root.querySelectorAll("*")]) {
    if (el.closest("script, style, [data-user-text]")) continue;
    const own = [...el.childNodes].filter((n) => n.nodeType === 3).map((n) => n.textContent ?? "").join("");
    if (words(own) > 6 && !el.closest("p")) long.push(own.trim());
  }
  const tips = [...root.querySelectorAll("[title]")]
    .map((el) => el.getAttribute("title") ?? "")
    .filter((tip) => words(tip) > 14);
  return { long, tips };
}

describe("the running app sits beside the chat", () => {
  it("shows the fake app while the build is still going, and does not fetch a build", async () => {
    const fetchSpy = vi.fn();
    vi.stubGlobal("fetch", fetchSpy);
    await mount(<LiveFrame build={FAKE} />);
    expect(fetchSpy).not.toHaveBeenCalled();
    const split = $("[data-testid=blf-split]")!;
    expect(split.getAttribute("data-layout")).toBe("beside");
    expect(split.getAttribute("data-still-going")).toBe("true");
    expect(split.getAttribute("data-fake")).toBe("true");
    const chat = $("[data-testid=blf-chat]")!;
    const stage = $("[data-testid=blf-stage]")!;
    expect(chat.compareDocumentPosition(stage) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(chat.textContent).toContain("A desk for the harbor office.");
    expect(chat.textContent).toContain("The desk is up.");
    const app = $("[data-testid=blf-app]") as HTMLIFrameElement;
    expect(app).not.toBeNull();
    expect(app.getAttribute("src")).toBe("https://preview.invalid/harbor-desk");
    expect(stage.contains(app)).toBe(true);
    expect(chat.contains(app)).toBe(false);
    const publish = $("[data-testid=blf-publish]") as HTMLButtonElement;
    expect(publish.disabled).toBe(false);
    expect(publish.textContent).toBe("Publish");
    expect(copyAudit(split)).toEqual({ long: [], tips: [] });
  });

  it("switches phone, tablet and desktop frames", async () => {
    await mount(<LiveFrame build={FAKE} />);
    const bezel = $("[data-testid=blf-bezel]")!;
    expect(bezel.getAttribute("data-device")).toBe("desktop");
    expect(bezel.getAttribute("data-width")).toBe("1280");
    expect(bezel.getAttribute("data-height")).toBe("800");
    await click($("[data-testid=blf-device-phone]"));
    expect(bezel.getAttribute("data-device")).toBe("phone");
    expect(bezel.getAttribute("data-width")).toBe("390");
    expect(bezel.getAttribute("data-height")).toBe("844");
    expect($("[data-testid=blf-device-phone]")?.getAttribute("aria-checked")).toBe("true");
    await click($("[data-testid=blf-device-tablet]"));
    expect(bezel.getAttribute("data-device")).toBe("tablet");
    expect(bezel.getAttribute("data-width")).toBe("834");
    expect(bezel.getAttribute("data-height")).toBe("1194");
    await click($("[data-testid=blf-device-desktop]"));
    expect(bezel.getAttribute("data-device")).toBe("desktop");
  });

  it("publishes once while the build is still going", async () => {
    const seen: string[] = [];
    await mount(<LiveFrame build={FAKE} onPublish={(result) => { seen.push(result.code); }} />);
    await click($("[data-testid=blf-publish]"));
    expect($("[data-testid=blf-publish-result]")?.textContent).toBe("Published.");
    expect(seen).toEqual(["published"]);
    await click($("[data-testid=blf-publish]"));
    expect(seen).toEqual(["published"]);
    expect($("[data-testid=blf-publish-result]")?.textContent).toBe("Already published.");
  });

  it("refuses a script address, a missing app, and shows a failed build", async () => {
    await mount(<LiveFrame build={{ ...FAKE, appUrl: "javascript:alert(1)" }} />);
    expect($("[data-testid=blf-app]")).toBeNull();
    expect($("[data-testid=blf-error]")?.textContent).toBe("The app address was refused.");
    expect(($("[data-testid=blf-publish]") as HTMLButtonElement).disabled).toBe(true);
    unmount();

    await mount(<LiveFrame build={{ ...FAKE, appUrl: null, status: "failed", error: "The port was taken." }} />);
    expect($("[data-testid=blf-app]")).toBeNull();
    expect($("[data-testid=blf-split]")?.getAttribute("data-still-going")).toBe("false");
    const error = $("[data-testid=blf-error]")!;
    expect(error.tagName).toBe("P");
    expect(error.textContent).toBe("The port was taken.");
    expect(($("[data-testid=blf-publish]") as HTMLButtonElement).disabled).toBe(true);
    expect($("[data-testid=blf-publish-reason]")?.textContent).toBe("Nothing is running yet.");
  });

  it("says GitHub is not connected instead of publishing there", async () => {
    const seen: string[] = [];
    await mount(<LiveFrame build={FAKE} publishTo="github" onPublish={(result) => { seen.push(result.code); }} />);
    await click($("[data-testid=blf-publish]"));
    const note = $("[data-testid=blf-publish-result]")!;
    expect(note.tagName).toBe("P");
    expect(note.textContent).toBe(NOT_CONNECTED);
    expect(seen).toEqual(["github_not_connected"]);
    await click($("[data-testid=blf-publish]"));
    expect(seen).toEqual(["github_not_connected", "github_not_connected"]);
    expect(note.textContent).toBe(NOT_CONNECTED);
  });

  it("does not start a build when the frame is empty", async () => {
    const fetchSpy = vi.fn();
    vi.stubGlobal("fetch", fetchSpy);
    await mount(<LiveFrame />);
    expect(fetchSpy).not.toHaveBeenCalled();
    expect($("[data-testid=blf-split]")?.getAttribute("data-layout")).toBe("beside");
    expect($("[data-testid=blf-app]")).toBeNull();
    expect(($("[data-testid=blf-publish]") as HTMLButtonElement).disabled).toBe(true);
    expect($("[data-testid=blf-bezel]")?.getAttribute("data-device")).toBe("desktop");
  });
});
