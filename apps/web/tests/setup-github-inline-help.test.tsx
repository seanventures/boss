// @vitest-environment happy-dom
/**
 * Setup › GitHub access: each way's explanation is a ?, not a paragraph.
 *
 * Breaks this file:
 * - painting "A token per build…", the As me sentence, or the vault sentence
 *   on the card before help opens
 * - a ? that does not open on hover, keyboard focus, click, or touch
 * - help that stays open after Escape, pointer leave, a second press, or an
 *   outside press — including Escape after a hover that never focused the ?
 * - hiding Recommended, Less safe, Nobody connected, or the token empty state
 */
import { act } from "react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { click, fakeApi, mountBoard, type Mounted } from "./_helpers/setup-board";
import { board } from "./_helpers/setup-fixtures";

let mounted: Mounted | null = null;
afterEach(() => { mounted?.unmount(); mounted = null; });

const NONE = {
  mode: "none", verdict: { tone: "red", label: "Builds can't reach GitHub", tip: "Connect a GitHub App below." },
  app: { configured: false, installed: false, slug: null, html_url: null, account: null, source: null },
  token: null, people_connected: 0, env_token: { present: false, read_by_builds: false },
};

const APP_HELP = "A token per build, only for its repos, gone when it ends.";
const AS_ME_HELP = "Runs you approve “as me on GitHub” use your own connection.";
const TOKEN_HELP = "Kept in the vault, used only by egress, only for these repos.";

function withGitHub(status: unknown) {
  const api = fakeApi(board());
  api.answer = (call) => (call.url.includes("/v1/identity/github") ? { status: 200, body: status } : null);
  return api;
}

function way(id: string) {
  const el = document.querySelector(`[data-testid="${id}"]`);
  if (!el) throw new Error(`missing ${id}`);
  return el;
}

function help(root: Element, label: string) {
  return root.querySelector<HTMLButtonElement>(`[aria-label="Help: ${label}"]`);
}

function tooltip() {
  return document.querySelector<HTMLElement>("[role=tooltip]");
}

async function pointer(el: HTMLElement, type: "pointerover" | "pointerout" | "pointerenter" | "pointerleave" | "pointerdown", pointerType: "mouse" | "touch") {
  await act(async () => {
    el.dispatchEvent(new PointerEvent(type, { bubbles: type !== "pointerenter" && type !== "pointerleave", pointerType }));
  });
}

/** The events a mouse hover actually fires. React's enter/leave listens to both shapes. */
async function hover(el: HTMLElement) {
  await pointer(el, "pointerover", "mouse");
  await pointer(el, "pointerenter", "mouse");
}

async function unhover(el: HTMLElement) {
  await pointer(el, "pointerout", "mouse");
  await pointer(el, "pointerleave", "mouse");
}

beforeEach(async () => {
  withGitHub(NONE);
  mounted = await mountBoard(board());
  const alternatives = document.querySelector('[data-testid="gh-other-ways"]') as HTMLDetailsElement | null;
  if (alternatives && !alternatives.open) await click(alternatives.querySelector("summary"));
});

describe("GitHub access explanations", () => {
  it("hides each way's explanation until its help opens, and keeps the warning and empty states", () => {
    const app = way("gh-way-app");
    const asMe = way("gh-way-as-me");
    const token = way("gh-way-token");

    expect(app.querySelector("p")).toBeNull();
    expect(asMe.querySelector("p")).toBeNull();
    expect(token.querySelector("p")).toBeNull();
    expect(app.textContent).not.toContain(APP_HELP);
    expect(asMe.textContent).not.toContain(AS_ME_HELP);
    expect(token.textContent).not.toContain(TOKEN_HELP);
    expect(tooltip()).toBeNull();

    expect(help(app, "GitHub App")?.textContent).toBe("?");
    expect(help(asMe, "As me")?.textContent).toBe("?");
    expect(help(token, "Fine-grained token")?.textContent).toBe("?");

    expect(app.textContent).toContain("Recommended");
    expect(token.textContent).toContain("Less safe");
    expect(asMe.textContent).toContain("Nobody connected");
    expect(token.textContent).toContain("None. A token pasted in Repository lands here.");
  });

  it("opens GitHub App help on hover and closes on leave and on Escape before the control was focused", async () => {
    const button = help(way("gh-way-app"), "GitHub App");
    expect(button).not.toBeNull();
    await hover(button!);
    expect(tooltip()?.textContent).toBe(APP_HELP);
    expect(button!.getAttribute("aria-expanded")).toBe("true");
    expect(button!.getAttribute("aria-describedby")).toBe(tooltip()?.id);

    await unhover(button!);
    expect(tooltip()).toBeNull();
    expect(button!.getAttribute("aria-expanded")).toBe("false");

    await hover(button!);
    expect(document.activeElement).not.toBe(button);
    await act(async () => {
      document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    });
    expect(tooltip()).toBeNull();
    expect(document.activeElement).toBe(button);
    expect(button!.getAttribute("aria-expanded")).toBe("false");
  });

  it("opens As me help on keyboard focus and closes on Escape without opening again", async () => {
    const button = help(way("gh-way-as-me"), "As me");
    expect(button).not.toBeNull();
    await act(async () => { button!.focus(); });
    expect(tooltip()?.textContent).toBe(AS_ME_HELP);

    await act(async () => {
      document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    });
    expect(tooltip()).toBeNull();
    expect(document.activeElement).toBe(button);
    expect(button!.getAttribute("aria-expanded")).toBe("false");
  });

  it("opens the token explanation on click and on touch, and closes on a second press or an outside press", async () => {
    const button = help(way("gh-way-token"), "Fine-grained token");
    expect(button).not.toBeNull();

    await act(async () => { button!.click(); });
    expect(tooltip()?.textContent).toBe(TOKEN_HELP);
    await act(async () => { button!.click(); });
    expect(tooltip()).toBeNull();

    await act(async () => {
      button!.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true, pointerType: "touch" }));
      button!.dispatchEvent(new PointerEvent("pointerup", { bubbles: true, pointerType: "touch" }));
      button!.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });
    expect(tooltip()?.textContent).toBe(TOKEN_HELP);
    await unhover(button!);
    expect(tooltip()?.textContent).toBe(TOKEN_HELP);

    await act(async () => {
      document.body.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true, pointerType: "touch" }));
    });
    expect(tooltip()).toBeNull();
    expect(way("gh-way-token").textContent).toContain("Less safe");
  });
});
