// @vitest-environment happy-dom
/**
 * BOSSAPP-1264: region, log retention, and whether the provider may train are
 * on screen before the prompt runs. A missing fact reads unknown. The run
 * stays held until those three facts have been shown.
 */
import React, { act } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

import { ProviderTermsScreen } from "../app/provider-terms/screen";
import { $, mount, unmount } from "./_helpers/live-render";

type Call = { url: string; method: string; body: Record<string, unknown> };

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

function words(text: string): string[] {
  return text.replace(/[·▸•|]/g, " ").trim().split(/\s+/).filter(Boolean);
}

function longVisible(root: ParentNode): string[] {
  const bad: string[] = [];
  const nodes = root instanceof Element ? [root, ...root.querySelectorAll("*")] : [...root.querySelectorAll("*")];
  for (const el of nodes) {
    if (el.closest("p, textarea, script, style")) continue;
    const own = [...el.childNodes].filter((n) => n.nodeType === 3).map((n) => n.textContent ?? "").join("");
    if (words(own).length > 6) bad.push(own.trim());
  }
  return bad;
}

function longTips(root: ParentNode): string[] {
  return [...root.querySelectorAll("[title]")].map((el) => el.getAttribute("title") ?? "").filter((tip) => words(tip).length > 14);
}

async function typeInto(testId: string, value: string) {
  const el = $(`[data-testid="${testId}"]`) as HTMLInputElement | HTMLTextAreaElement;
  const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  const setter = Object.getOwnPropertyDescriptor(proto, "value")!.set!;
  await act(async () => {
    setter.call(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

async function press(testId: string) {
  await act(async () => {
    ($(`[data-testid="${testId}"]`) as HTMLElement).click();
    await new Promise((resolve) => setTimeout(resolve, 0));
    await new Promise((resolve) => setTimeout(resolve, 0));
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
}

function fact(testId: string): string {
  return $(`[data-testid="${testId}"] [data-fact]`)?.textContent?.trim() ?? "";
}

function stubFacts(shownBody: Record<string, unknown>, runBody?: Record<string, unknown> | number) {
  const calls: Call[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    const method = String(init?.method ?? "GET");
    const body = init?.body ? JSON.parse(String(init.body)) as Record<string, unknown> : {};
    calls.push({ url: String(url), method, body });
    if (String(url).endsWith("/disclose")) {
      return json({ disclosure_id: "ptd_1", shown: false, ...shownBody });
    }
    if (String(url).endsWith("/shown")) {
      return json({ disclosure_id: "ptd_1", shown: true, ...shownBody });
    }
    if (String(url).endsWith("/run")) {
      if (typeof runBody === "number") return json({ detail: "Region, log retention, and whether the provider may train are not shown yet." }, runBody);
      return json(runBody ?? { run_id: "ptr_1", started: true, deduped: false, vendor_called: false });
    }
    if (String(url).endsWith("/record")) return json({ ...shownBody });
    return json({ detail: "Terms could not be read." }, 404);
  }));
  return calls;
}

afterEach(() => { unmount(); vi.restoreAllMocks(); });

describe("provider terms before a prompt", () => {
  it("holds the run until the three facts are shown, and unknown stays unknown", async () => {
    const calls = stubFacts({ region: "unknown", log_retention: "unknown", may_train: "unknown" });
    await mount(<ProviderTermsScreen projectId="proj_1264" />);
    expect(document.querySelector("[data-gate]")?.getAttribute("data-gate")).toBe("held");
    expect(fact("provider-terms-region")).toBe("Not shown");
    expect(fact("provider-terms-retention")).toBe("Not shown");
    expect(fact("provider-terms-train")).toBe("Not shown");
    expect(($("[data-testid=provider-terms-run]") as HTMLButtonElement).disabled).toBe(true);
    await press("provider-terms-run");
    await press("provider-terms-show");
    expect(calls).toEqual([]);

    await typeInto("provider-terms-provider", "acme");
    await typeInto("provider-terms-prompt", "  say hello  ");
    await press("provider-terms-show");
    expect(fact("provider-terms-region")).toBe("unknown");
    expect(fact("provider-terms-retention")).toBe("unknown");
    expect(fact("provider-terms-train")).toBe("unknown");
    expect(document.body.textContent).not.toMatch(/us-east-1|us-west-2|eu-west-1/);
    expect(($("[data-testid=provider-terms-run]") as HTMLButtonElement).disabled).toBe(false);
    const disclose = calls.find((call) => call.url.endsWith("/disclose"));
    expect(disclose?.body).toEqual({ project_id: "proj_1264", provider: "acme", prompt: "say hello" });
    expect(calls.some((call) => call.url.endsWith("/shown"))).toBe(true);

    await press("provider-terms-run");
    const run = calls.filter((call) => call.url.endsWith("/run"));
    expect(run).toHaveLength(1);
    expect(run[0].body).toEqual({ project_id: "proj_1264", disclosure_id: "ptd_1" });
    expect(run[0].body.region).toBeUndefined();
    expect(run[0].body.may_train).toBeUndefined();
    expect($("[data-testid=provider-terms-result]")?.textContent?.trim()).toBe("Run started");
  });

  it("shows a recorded region, retention, and training flag exactly", async () => {
    stubFacts({ region: "eu-central-1", log_retention: "30 days", may_train: "no" });
    await mount(<ProviderTermsScreen projectId="proj_1264" />);
    await typeInto("provider-terms-provider", "acme");
    await typeInto("provider-terms-prompt", "say hello");
    await press("provider-terms-show");
    expect(fact("provider-terms-region")).toBe("eu-central-1");
    expect(fact("provider-terms-retention")).toBe("30 days");
    expect(fact("provider-terms-train")).toBe("no");
    expect(fact("provider-terms-train")).not.toBe("yes");
  });

  it("holds the run again when the prompt changes", async () => {
    const calls = stubFacts({ region: "unknown", log_retention: "unknown", may_train: "unknown" });
    await mount(<ProviderTermsScreen projectId="proj_1264" />);
    await typeInto("provider-terms-provider", "acme");
    await typeInto("provider-terms-prompt", "say hello");
    await press("provider-terms-show");
    expect(($("[data-testid=provider-terms-run]") as HTMLButtonElement).disabled).toBe(false);
    await typeInto("provider-terms-prompt", "say goodbye");
    expect(fact("provider-terms-region")).toBe("Not shown");
    expect(fact("provider-terms-train")).toBe("Not shown");
    expect(($("[data-testid=provider-terms-run]") as HTMLButtonElement).disabled).toBe(true);
    await press("provider-terms-run");
    expect(calls.filter((call) => call.url.endsWith("/run"))).toHaveLength(0);
  });

  it("shows a refused run as an error and does not say it started", async () => {
    stubFacts({ region: "unknown", log_retention: "unknown", may_train: "unknown" }, 409);
    await mount(<ProviderTermsScreen projectId="proj_1264" />);
    await typeInto("provider-terms-provider", "acme");
    await typeInto("provider-terms-prompt", "say hello");
    await press("provider-terms-show");
    await press("provider-terms-run");
    const alert = $("[data-testid=provider-terms-error]");
    expect(alert?.tagName).toBe("P");
    expect(alert?.textContent).toMatch(/not shown/);
    expect($("[data-testid=provider-terms-result]")?.textContent ?? "").not.toMatch(/Run started/);
    expect(($("[data-testid=provider-terms-run]") as HTMLButtonElement).disabled).toBe(true);
  });

  it("records only a fact that was typed, and will not send an unclear training flag", async () => {
    const calls = stubFacts({ region: "unknown", log_retention: "unknown", may_train: "unknown" });
    await mount(<ProviderTermsScreen projectId="proj_1264" />);
    await typeInto("provider-terms-provider", "acme");
    await typeInto("provider-terms-prompt", "say hello");
    await press("provider-terms-show");
    await typeInto("provider-terms-known-train", "sometimes");
    await press("provider-terms-save");
    expect(calls.filter((call) => call.url.endsWith("/record"))).toHaveLength(0);
    expect($("[data-testid=provider-terms-error]")?.textContent).toMatch(/yes or no/);

    await typeInto("provider-terms-known-train", "");
    await typeInto("provider-terms-known-region", "eu-central-1");
    await press("provider-terms-save");
    const recorded = calls.filter((call) => call.url.endsWith("/record"));
    expect(recorded).toHaveLength(1);
    expect(recorded[0].body).toEqual({
      project_id: "proj_1264",
      provider: "acme",
      region: "eu-central-1",
    });
    expect(recorded[0].body.may_train).toBeUndefined();
    expect(recorded[0].body.log_retention).toBeUndefined();
    expect(fact("provider-terms-region")).toBe("Not shown");
    expect(($("[data-testid=provider-terms-run]") as HTMLButtonElement).disabled).toBe(true);
  });

  it("keeps long sentences in paragraphs and tooltips short", async () => {
    stubFacts({ region: "unknown", log_retention: "unknown", may_train: "unknown" });
    await mount(<ProviderTermsScreen projectId="proj_1264" />);
    const note = $("[data-testid=provider-terms-note]");
    expect(note?.tagName).toBe("P");
    expect(words(note?.textContent ?? "").length).toBeGreaterThan(6);
    expect(longVisible(document.body)).toEqual([]);
    expect(longTips(document.body)).toEqual([]);
    await typeInto("provider-terms-provider", "acme");
    await typeInto("provider-terms-prompt", "say hello");
    await press("provider-terms-show");
    expect(longVisible(document.body)).toEqual([]);
    expect(longTips(document.body)).toEqual([]);
  });
});
