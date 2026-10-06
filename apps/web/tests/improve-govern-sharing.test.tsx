// @vitest-environment happy-dom
/**
 * Govern › Sharing (docs/product/RUN_SCORECARDS.md): the switch (off by default) in the doc's own
 * words, the heartbeat and its one opt-out, the exact next payload, the send log, export, rotate,
 * delete, and the score's weights — within the copy budget, with short toasts, honest when the
 * environment turns everything off.
 */
import { readFileSync } from "node:fs";
import path from "node:path";

import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { setConsoleLive } from "../lib/console-live";
import { overBudget } from "./_helpers/copy-budget";
import { fakeGovernApi, newGovern } from "./_helpers/govern-fixtures";
import { fakeImproveApi, newImprove } from "./_helpers/improve-fixtures";
import { $, click, mount, settle, unmount } from "./_helpers/live-render";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const { GovernHome } = await import("../app/govern/govern-home");
const { CONSENT_TEXT, CONSENT_TITLE } = await import("../app/improve/consent");

const toasts: string[] = [];
window.addEventListener("boss:toast", (e) => toasts.push(String((e as CustomEvent).detail?.message ?? "")));

async function open(f = newImprove()) {
  const g = newGovern();
  vi.stubGlobal("fetch", fakeImproveApi(f, fakeGovernApi(g) as never));
  setConsoleLive(true);
  window.history.replaceState({}, "", "/?area=budget&tab=sharing");
  await mount(<GovernHome projectId="p1" actor="ceo" />);
  await settle();
  return f;
}

afterEach(() => { unmount(); vi.unstubAllGlobals(); toasts.length = 0; });

describe("Govern › Sharing", () => {
  it("says the consent in the doc's exact words", () => {
    const doc = readFileSync(path.resolve(__dirname, "../../../docs/product/RUN_SCORECARDS.md"), "utf8");
    const quoted = doc.split("\n").filter((l) => l.startsWith("  >")).map((l) => l.replace(/^\s*>\s?/, "")).join(" ")
      .replace(/\s+/g, " ").trim();
    expect(quoted).toBe(`**${CONSENT_TITLE}** ${CONSENT_TEXT}`);
  });

  it("shows the switch off, the heartbeat on, the payload and the log — within the budget", async () => {
    await open();
    const root = $("[data-testid=gv-sharing]")!;
    expect(root).toBeTruthy();
    expect(overBudget($("[data-testid=gv]")!)).toEqual([]);
    expect(root.querySelectorAll("p")).toHaveLength(0);
    expect($("[data-testid=gv-switch-improve]")!.getAttribute("aria-checked")).toBe("false");
    expect($("[data-testid=gv-switch-heartbeat]")!.getAttribute("aria-checked")).toBe("true");
    expect($("[data-testid=im-consent]")!.textContent).toContain("Never your code, prompts, briefs");
    const json = JSON.parse($("[data-testid=gv-payload-json]")!.textContent ?? "{}");
    expect(Object.keys(json.heartbeat).sort()).toEqual(
      ["age", "arch", "edition", "install_id", "install_method", "os", "version", "worked_24h"]);
    expect(root.querySelectorAll("[data-testid=gv-send]")).toHaveLength(2);
    expect($("[data-testid=gv-export]")!.getAttribute("href")).toContain("view=export");
    for (const el of root.querySelectorAll("[title]")) {
      const words = (el.getAttribute("title") ?? "").trim().split(/\s+/).length;
      if (!el.closest("[data-user-text]")) expect(words, el.getAttribute("title") ?? "").toBeLessThanOrEqual(14);
    }
  });

  it("flips the switch with who and when, and toasts in four words or fewer", async () => {
    const f = await open();
    await click($("[data-testid=gv-switch-improve]"));
    await settle();
    const write = f.calls.find((c) => c.op === "switch");
    expect(write?.body).toMatchObject({ switch: "improve", on: true, project_id: "p1", actor: "ceo" });
    await click($("[data-testid=gv-switch-heartbeat]"));
    await settle();
    expect(f.calls.filter((c) => c.op === "switch").map((c) => [c.body?.switch, c.body?.on]))
      .toEqual([["improve", true], ["heartbeat", false]]);
    expect(toasts.length).toBeGreaterThan(0);
    for (const t of toasts) expect(t.split(/\s+/).length, t).toBeLessThanOrEqual(4);
  });

  it("deletes what was sent only after a confirm, and rotates", async () => {
    const f = await open();
    await click($("[data-testid=gv-forget]"));
    expect(f.calls.some((c) => c.op === "forget")).toBe(false);
    await click($("[data-testid=gv-forget-yes]"));
    await settle();
    expect(f.calls.some((c) => c.op === "forget")).toBe(true);
    await click($("[data-testid=gv-rotate]"));
    await settle();
    expect(f.calls.some((c) => c.op === "rotate")).toBe(true);
    for (const t of toasts) expect(t.split(/\s+/).length, t).toBeLessThanOrEqual(4);
  });

  it("is honest when the environment turns everything off", async () => {
    const f = newImprove();
    f.sharing.env_disabled = "DO_NOT_TRACK=1";
    f.sharing.blocked = "DO_NOT_TRACK=1";
    f.preview = { pending: null, why_not: "DO_NOT_TRACK=1", next_due_at: null };
    await open(f);
    expect($("[data-testid=gv-sharing-locked]")!.textContent).toContain("DO_NOT_TRACK=1");
    expect(($("[data-testid=gv-switch-improve]") as HTMLButtonElement).disabled).toBe(true);
    expect(($("[data-testid=gv-switch-heartbeat]") as HTMLButtonElement).disabled).toBe(true);
    expect($("[data-testid=gv-switch-heartbeat]")!.getAttribute("aria-checked")).toBe("false");
    expect($("[data-testid=gv-payload-none]")).toBeTruthy();
  });

  it("saves only the weights that changed", async () => {
    const f = await open();
    const input = $("[data-testid=gv-weight-tests]") as HTMLInputElement;
    const { act } = await import("react");
    await act(async () => {
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
      setter.call(input, "0.7");
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await click($("[data-testid=gv-weights-save]"));
    await settle();
    expect(f.calls.find((c) => c.op === "weights")?.body?.weights).toEqual({ tests: 0.7 });
  });

  it("with no collector: says so, sends nothing, disables Delete with the reason, takes a URL", async () => {
    const f = newImprove();
    f.sharing.url = ""; f.sharing.url_source = "none"; f.sharing.blocked = "No collector set";
    await open(f);
    expect($("[data-testid=gv-sharing-locked]")!.textContent).toContain("No collector set");
    expect(($("[data-testid=gv-forget]") as HTMLButtonElement).disabled).toBe(true);
    expect($("[data-testid=gv-forget-why]")!.textContent).toContain("none set");
    const input = $("[data-testid=gv-url]") as HTMLInputElement;
    const { act } = await import("react");
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, "https://c.example.com/v1/telemetry/ingest");
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await click($("[data-testid=gv-url-save]"));
    await settle();
    expect(f.calls.find((c) => c.op === "collector_url")?.body?.url).toBe("https://c.example.com/v1/telemetry/ingest");
  });

  it("a rotation says what happened to the old id", async () => {
    const f = newImprove();
    f.rotateOld = "kept";
    await open(f);
    await click($("[data-testid=gv-rotate]"));
    await settle();
    expect($("[data-testid=gv-rotated]")!.textContent).toBe("Old id kept until its delete goes through");
  });

  it("names people, not member ids, and the send log has a time", async () => {
    const f = newImprove();
    f.sharing.switches.improve = { ...f.sharing.switches.improve, asked: true, by: "executive_console", by_name: "Priya" } as never;
    await open(f);
    expect($("[data-testid=gv-sharing-improve]")!.textContent).toContain("Priya");
    expect($("[data-testid=gv]")!.textContent).not.toContain("executive_console");
    expect($("[data-testid=gv-send]")!.textContent).toMatch(/\d{1,2}:\d{2}/);
  });

  it("says the collector held nothing rather than deleted", async () => {
    const f = newImprove();
    f.rotateOld = "nothing_held";
    await open(f);
    await click($("[data-testid=gv-rotate]"));
    await settle();
    expect($("[data-testid=gv-rotated]")!.textContent).toBe("Collector held nothing for the old id");
  });

  it("a refused collector URL shows the reason beside the field", async () => {
    const f = newImprove();
    f.urlError = "The collector URL has to be https.";
    await open(f);
    const input = $("[data-testid=gv-url]") as HTMLInputElement;
    const { act } = await import("react");
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, "http://c.example.test/v1/telemetry/ingest");
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await click($("[data-testid=gv-url-save]"));
    await settle();
    expect($("[data-testid=gv-url-error]")!.textContent).toBe("The collector URL has to be https.");
  });

  it("saves the words a shared run always masks", async () => {
    const f = await open();
    const input = $("[data-testid=gv-mask]") as HTMLInputElement;
    const { act } = await import("react");
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, "Nightjar, Larkspur");
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await click($("[data-testid=gv-mask-save]"));
    await settle();
    expect(f.calls.find((c) => c.op === "mask_words")?.body?.words).toBe("Nightjar, Larkspur");
  });
});
