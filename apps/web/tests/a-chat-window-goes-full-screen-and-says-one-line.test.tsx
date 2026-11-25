// @vitest-environment happy-dom
/**
 * THE CHAT WINDOW, OPERATOR FEEDBACK 2026-11-19 (captain drawer, "Helm · Captain").
 *
 * 1. "Attach" rendered as a large plain word beside the field. It is an icon button now: a
 *    Phosphor paperclip, a name for screen readers and a tooltip, in the shared composer every
 *    chat uses (AgentChat), with the drawer's one-row composer showing the icon only.
 * 2. Every chat window has a full-screen control at its top, one shared component
 *    (components/chat-fullscreen): pressed, the WINDOW is laid over the screen; Esc brings it
 *    back, and that Esc must not also close the drawer underneath.
 * 3. A reply to an action request is one sentence and a button. Supporting detail comes after
 *    a `Details:` line (the engine's reply_style contract) and is folded behind "Details".
 */
import React, { act } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { codeOnly } from "./_helpers/source";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(window.location.search),
  usePathname: () => "/",
}));

const ANSWER = "Adding bench names to the map pins, press Start to run it (about 10 min, updates change #2).\n\n"
  + "Details:\n- touches the pin popup and the bench panel\n- checked the current map code";

vi.mock("../app/actions", async (original) => ({
  ...(await original<Record<string, unknown>>()),
  askBossAction: vi.fn(async (_prev: unknown, fd: FormData) => ({ ok: true, data: {
    thread_id: "athr_1",
    messages: [{ id: "amsg_1", role: "user", content: String(fd.get("message")) },
               { id: "amsg_2", role: "assistant", content: ANSWER }],
    proposed_actions: [], tool_calls: [], next_steps: [] } })),
}));

import { $, mountAs, stubFetch, tid, unmount } from "./_helpers/multirepo-fixtures";
const { AgentChat } = await import("../app/agent-ui");
const { ChatFullscreenButton, useChatFullscreen } = await import("../components/chat-fullscreen");
const { splitDetails, DETAILS_MARKER } = await import("../lib/answer-details");

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const settle = async (n = 10) => { for (let i = 0; i < n; i++) await act(async () => { await new Promise((r) => setTimeout(r, 0)); }); };
const press = async (el: Element | null) => { if (!el) throw new Error("nothing to press"); await act(async () => { (el as HTMLElement).click(); }); await settle(); };

afterEach(() => { unmount(); vi.unstubAllGlobals(); window.history.replaceState(null, "", "/"); });

describe("an answer's details are folded", () => {
  it("splits at a Details line, in any of the ways a model writes it", () => {
    for (const marker of ["Details:", "**Details**", "**Details:**", "### Details", "Details"]) {
      const parts = splitDetails(`Adding it now.\n\n${marker}\n- one\n- two`);
      expect(parts.lead).toBe("Adding it now.");
      expect(parts.details).toBe("- one\n- two");
    }
  });

  it("leaves prose that merely mentions details alone, and never folds the whole answer", () => {
    expect(splitDetails("Details: see the PR.").details).toBe("");
    expect(splitDetails("Plain answer, no fold.")).toEqual({ lead: "Plain answer, no fold.", details: "" });
    expect(splitDetails("Details:\n- only detail")).toEqual({ lead: "- only detail", details: "" });
  });

  it("uses the same marker the engine's prompt asks for", () => {
    const style = codeOnly("../../engine/boss_engine/services/reply_style.py");
    expect(style).toContain(`DETAILS_MARKER = "${DETAILS_MARKER}"`);
  });

  it("in the conversation: one sentence, and the rest behind Details", async () => {
    stubFetch(() => ({ body: {} }));
    await mountAs(<AgentChat projectId="helpdesk" actor="ceo" fresh />);
    const box = $("textarea") as HTMLTextAreaElement;
    await act(async () => {
      const set = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!;
      set.call(box, "add the bench name label to the map bubble");
      box.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await press(tid("agent-send"));
    await settle(20);
    const fold = tid("agent-answer-details") as HTMLDetailsElement;
    expect(fold).not.toBeNull();
    expect(fold.open).toBe(false);
    expect(fold.textContent).toContain("touches the pin popup");
    const answer = fold.closest("article")!;
    expect(answer.textContent).toContain("Adding bench names to the map pins");
  });
});

describe("Attach is an icon button", () => {
  it("a paperclip with a name and a tooltip, not a bare word", async () => {
    stubFetch(() => ({ body: {} }));
    await mountAs(<AgentChat projectId="helpdesk" actor="ceo" fresh />);
    const attach = tid("agent-attach")!;
    expect(attach.querySelector("svg")).not.toBeNull();
    expect(attach.getAttribute("aria-label")).toBe("Attach a file");
    expect(attach.getAttribute("title")).toContain("paste an image");
  });

  it("sized like the send button, in tokens; icon only in the captain drawer", () => {
    const css = codeOnly("app/globals.css");
    const rule = css.slice(css.indexOf(".agent-attach {"), css.indexOf("}", css.indexOf(".agent-attach {")));
    expect(rule).toContain("height: 32px");
    expect(rule).toContain("var(--fs-tag)");
    expect(rule).not.toMatch(/#[0-9a-f]{3,6}\b/i);
    const px = codeOnly("app/projects/px.css");
    expect(px).toContain(".px-capd .agent-attach-label");
    expect(px).not.toMatch(/\.px-capd \.agent-attach \{[^}]*--fs-title/);
  });
});

function Window({ onDrawerEsc }: { onDrawerEsc: () => void }) {
  const fs = useChatFullscreen();
  React.useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onDrawerEsc(); };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onDrawerEsc]);
  return <aside data-testid="win" {...fs.attr}><ChatFullscreenButton full={fs.full} onToggle={fs.toggle} /></aside>;
}

describe("every chat window can go full screen", () => {
  it("the control toggles the window, and Esc leaves full screen without closing the drawer", async () => {
    const drawerEsc = vi.fn();
    await mountAs(<Window onDrawerEsc={drawerEsc} />);
    const btn = tid("chat-fullscreen")!;
    expect(btn.getAttribute("aria-label")).toBe("Full screen");
    expect(btn.getAttribute("aria-pressed")).toBe("false");
    await press(btn);
    expect(tid("win")!.getAttribute("data-chat-full")).toBe("1");
    expect(tid("chat-fullscreen")!.getAttribute("aria-label")).toBe("Exit full screen");
    await act(async () => { document.body.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })); });
    await settle();
    expect(tid("win")!.getAttribute("data-chat-full")).toBeNull();
    expect(drawerEsc).not.toHaveBeenCalled();
    // Not full screen, Esc is the drawer's again.
    await act(async () => { document.body.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })); });
    expect(drawerEsc).toHaveBeenCalledTimes(1);
  });

  it("is in the header of every chat window", () => {
    for (const [file, mark] of [
      ["app/projects/captain-drawer.tsx", "full={fs.full}"],
      ["app/ConsoleShell.tsx", "{...dockFs.attr}"],
      ["app/agents/talk-sheet.tsx", "{...fs.attr}"],
      ["components/v2/AgentChat.tsx", "{...fs.attr}"],
      ["app/boss-agent-home.tsx", "expandable"],
    ] as const) {
      const src = codeOnly(file);
      expect(src, file).toMatch(/ChatFullscreenButton|expandable/);
      expect(src, file).toContain(mark);
    }
    const css = codeOnly("app/globals.css");
    expect(css).toContain('[data-chat-full="1"] {');
  });
});
