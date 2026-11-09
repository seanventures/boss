// @vitest-environment happy-dom
import React, { act } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { $, click, mount, rerender, settle, unmount } from "./_helpers/live-render";
import { agreementFixture, BRIEF } from "./_helpers/work-agreement-fixture";
import { readWorkAgreement } from "../lib/work-agreement";
import { WorkAgreement } from "../components/work-agreement";
vi.mock("../app/actions", () => ({ askBossAction: vi.fn(), runAgentActionAction: vi.fn() }));
vi.mock("../app/default-agent-picker", () => ({ DefaultAgentPicker: () => null }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn() }) }));
let currentIdentity: { principal_id: string; org: { id: string } } | null = null;
vi.mock("../components/identity/identity-provider", () => ({
  useIdentity: () => ({ me: currentIdentity }),
  useCan: () => ({ allowed: true, reason: "", needsStepUp: false }),
}));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
(globalThis as Record<string, unknown>).React = React;
const { AskBossHero } = await import("../app/ask-boss-hero");
let calls: { url: string; body: Record<string, unknown> }[];
let previewResponse: (body: Record<string, unknown>) => Promise<Response>;
let startResponse: () => Promise<Response>;
let runResponse: () => Promise<Response>;
let reads: string[];
const INTENT = `agreement:${"4".repeat(64)}`;
const STORED = "boss.first-work-start:local:p1:owner";
const runFixture = (id = "wfr_real_record") => ({ id, project_id: "p1", dedupe_key: INTENT });

beforeEach(() => {
  calls = []; reads = []; window.sessionStorage.clear();
  currentIdentity = { principal_id: "owner", org: { id: "local" } };
  previewResponse = async (body) => Response.json(agreementFixture(String(body.directive), String(body.project_id)));
  startResponse = async () => Response.json({ run: runFixture() });
  runResponse = async () => Response.json({ project_id: "p1", runs: [] });
  vi.stubGlobal("fetch", vi.fn(async (url: unknown, init?: RequestInit) => {
    const path = String(url);
    if (path === "/api/builds/preview" || path === "/api/builds") {
      const body = JSON.parse(String(init?.body ?? "{}")) as Record<string, unknown>;
      calls.push({ url: path, body });
      return path.endsWith("preview") ? previewResponse(body) : startResponse();
    }
    if (path.startsWith("/api/delivery-workflows?view=runs")) { reads.push(path); return runResponse(); }
    return Response.json({ items: [], capabilities: [], costs: {} });
  }));
  HTMLElement.prototype.scrollIntoView = vi.fn();
});
afterEach(() => { unmount(); vi.unstubAllGlobals(); window.history.replaceState(null, "", "/"); });

async function open() {
  await mount(<AskBossHero projectId="p1" actor="owner" />);
  await draft();
}
async function draft() {
  const field = $("[data-testid=askboss-input]") as HTMLTextAreaElement;
  field.focus();
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!.call(field, BRIEF);
    field.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await settle();
  const form = field.closest("form")!;
  await act(async () => { form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })); });
  await settle();
}

describe("the first build agreement", () => {
  it("reads the actual workflow and starts only after a separate explicit confirmation", async () => {
    await open();
    expect(calls.map((c) => c.url)).toEqual(["/api/builds/preview"]);
    expect($("[role=dialog]")?.textContent).toContain(BRIEF);
    expect($("[role=dialog]")?.textContent).toContain("Bounded software change, version 3");
    expect($("[role=dialog]")?.textContent).toContain("A person must release this step");
    expect($("[role=dialog]")?.textContent).toContain("Not recorded yet");
    await click($("[data-testid=work-agreement-confirm]"));
    expect(calls.map((c) => c.url)).toEqual(["/api/builds/preview", "/api/builds"]);
    expect(calls[1].body).toMatchObject({ directive: BRIEF, agreement_content_hash: "1".repeat(64) });
    expect(calls[0].body).not.toHaveProperty("actor");
    expect(calls[1].body).not.toHaveProperty("actor");
    expect(calls[1].body.agreement_binding_token).toMatch(/^\d+\.1{64}\.3{32}\.2{64}$/);
    expect(new URLSearchParams(window.location.search).get("wfr")).toBe("wfr_real_record");
  });

  it("Cancel keeps the draft and dispatches no start", async () => {
    await open();
    const cancel = Array.from(document.querySelectorAll("[role=dialog] button")).find((x) => x.textContent === "Cancel")!;
    await click(cancel);
    expect($("[role=dialog]")).toBeNull();
    expect(($("[data-testid=askboss-input]") as HTMLTextAreaElement).value).toBe(BRIEF);
    expect(calls).toHaveLength(1);
  });

  it("a preview arriving after Cancel cannot reopen the sheet or start work", async () => {
    let resolve!: (r: Response) => void;
    previewResponse = () => new Promise((r) => { resolve = r; });
    await open();
    await click(Array.from(document.querySelectorAll("[role=dialog] button")).find((x) => x.textContent === "Cancel")!);
    await act(async () => { resolve(Response.json(agreementFixture())); });
    await settle();
    expect($("[role=dialog]")).toBeNull(); expect(calls).toHaveLength(1);
  });

  it("a failed preview says why; Review again reads without dispatching work", async () => {
    previewResponse = async () => Response.json({ error: "No workflow is selected." }, { status: 409 });
    await open();
    expect($("[role=alert]")?.textContent).toContain("No workflow is selected");
    expect(($("[data-testid=work-agreement-confirm]") as HTMLButtonElement).disabled).toBe(true);
    previewResponse = async () => Response.json(agreementFixture());
    await click(Array.from(document.querySelectorAll("[role=dialog] button")).find((x) => x.textContent === "Review again")!);
    expect(calls.map((c) => c.url)).toEqual(["/api/builds/preview", "/api/builds/preview"]);
    expect(($("[data-testid=work-agreement-confirm]") as HTMLButtonElement).disabled).toBe(false);
  });

  it("incomplete or wrong-scope response never enables confirmation", async () => {
    previewResponse = async () => Response.json(agreementFixture(BRIEF, "another-project"));
    await open();
    expect($("[role=alert]")?.textContent).toContain("incomplete");
    expect(($("[data-testid=work-agreement-confirm]") as HTMLButtonElement).disabled).toBe(true);
    expect(calls).toHaveLength(1);
  });

  it("rapid confirmation presses dispatch one start", async () => {
    let resolve!: (r: Response) => void;
    startResponse = () => new Promise((r) => { resolve = r; });
    await open();
    const confirm = $("[data-testid=work-agreement-confirm]")!;
    await act(async () => { confirm.click(); confirm.click(); });
    expect(calls.filter((c) => c.url === "/api/builds")).toHaveLength(1);
    await act(async () => { resolve(Response.json({ run: runFixture("one") })); });
    await settle();
  });

  it("an expired preview requires a new read and sends no start", async () => {
    previewResponse = async () => Response.json({ ...agreementFixture(), expires_at_unix: 1 });
    await open(); await click($("[data-testid=work-agreement-confirm]"));
    expect($("[role=alert]")?.textContent).toContain("expired"); expect(calls).toHaveLength(1);
  });

  it("a project change discards the preview before confirmation", async () => {
    await open(); await rerender(<AskBossHero projectId="p2" actor="owner" />);
    expect($("[role=dialog]")).toBeNull(); expect(calls).toHaveLength(1);
  });

  it("a stale start refusal stays visible and cannot silently start again", async () => {
    startResponse = async () => Response.json({ error: "The workflow changed. Review again." }, { status: 409 });
    await open(); await click($("[data-testid=work-agreement-confirm]"));
    expect($("[role=alert]")?.textContent).toContain("workflow changed");
    expect(($("[data-testid=work-agreement-confirm]") as HTMLButtonElement).disabled).toBe(true);
    expect(calls.filter((c) => c.url === "/api/builds")).toHaveLength(1);
  });

  it("Escape closes the preview and returns focus without starting", async () => {
    await open();
    await act(async () => { $("[role=dialog]")!.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })); });
    await settle(); expect($("[role=dialog]")).toBeNull(); expect(calls).toHaveLength(1);
    expect(document.activeElement).toBe($("[data-testid=askboss-input]"));
  });

  it("a lost response keeps only the opaque key and reload holds a fresh intent", async () => {
    startResponse = async () => { throw new Error("response lost"); };
    await open(); await click($("[data-testid=work-agreement-confirm]"));
    expect(window.sessionStorage.getItem(STORED)).toBe(INTENT);
    const stored = window.sessionStorage.getItem(STORED)!;
    expect(stored).not.toContain(BRIEF); expect(stored).not.toContain("binding_token");
    unmount(); await mount(<AskBossHero projectId="p1" actor="owner" />);
    expect($("[role=dialog]")?.textContent).toContain("Check the original start");
    await draft();
    expect(calls.map((c) => c.url)).toEqual(["/api/builds/preview", "/api/builds"]);
    await click($("[data-testid=work-agreement-check-start]"));
    expect($("[role=alert]")?.textContent).toContain("outcome remains unknown");
    expect(window.sessionStorage.getItem(STORED)).toBe(INTENT);
    expect(reads).toHaveLength(1);
    expect(calls.filter((c) => c.url === "/api/builds")).toHaveLength(1);
  });

  it("reconciliation opens only the exact recorded project and intent", async () => {
    startResponse = async () => { throw new Error("response lost"); };
    await open(); await click($("[data-testid=work-agreement-confirm]"));
    runResponse = async () => Response.json({ project_id: "p1", runs: [
      { ...runFixture("foreign"), project_id: "p2" }, { ...runFixture("wrong-key"), dedupe_key: "wrong" }, runFixture("recorded") ] });
    await click($("[data-testid=work-agreement-check-start]"));
    expect(new URLSearchParams(window.location.search).get("wfr")).toBe("recorded");
    expect(window.sessionStorage.getItem(STORED)).toBeNull(); expect($("[role=dialog]")).toBeNull();
    expect(calls.filter((c) => c.url === "/api/builds")).toHaveLength(1);
  });

  it("a foreign-project runs response cannot clear the unknown hold", async () => {
    startResponse = async () => { throw new Error("response lost"); };
    await open(); await click($("[data-testid=work-agreement-confirm]"));
    runResponse = async () => Response.json({ project_id: "p2", runs: [runFixture()] });
    await click($("[data-testid=work-agreement-check-start]"));
    expect(window.sessionStorage.getItem(STORED)).toBe(INTENT);
    expect(new URLSearchParams(window.location.search).get("wfr")).toBeNull();
  });

  it("a deterministic refusal clears the pending intent and allows a new read", async () => {
    startResponse = async () => Response.json({ error: "The workflow changed." }, { status: 409 });
    await open(); await click($("[data-testid=work-agreement-confirm]"));
    expect(window.sessionStorage.getItem(STORED)).toBeNull();
    await click(Array.from(document.querySelectorAll("[role=dialog] button")).find((x) => x.textContent === "Review again")!);
    expect(calls.map((c) => c.url)).toEqual(["/api/builds/preview", "/api/builds", "/api/builds/preview"]);
  });

  it("identity, project and organization changes do not reveal another scope's pending intent", async () => {
    window.sessionStorage.setItem(STORED, INTENT);
    await mount(<AskBossHero projectId="p1" actor="owner" />);
    expect($("[data-testid=work-agreement-check-start]")).not.toBeNull();
    for (const scope of [{ principal_id: "other", org: "local", project: "p1" },
      { principal_id: "owner", org: "local", project: "p2" },
      { principal_id: "owner", org: "another-org", project: "p1" }]) {
      currentIdentity = { principal_id: scope.principal_id, org: { id: scope.org } };
      await rerender(<AskBossHero projectId={scope.project} actor="owner" />);
      expect($("[role=dialog]")).toBeNull(); expect(calls).toEqual([]);
    }
    expect(window.sessionStorage.getItem(STORED)).toBe(INTENT);
  });

  it("a malformed success cannot claim a recorded start or clear the intent", async () => {
    startResponse = async () => Response.json({ run: { id: "wrong", project_id: "foreign", dedupe_key: INTENT } });
    await open(); await click($("[data-testid=work-agreement-confirm]"));
    expect($("[role=alert]")?.textContent).toContain("did not identify");
    expect(window.sessionStorage.getItem(STORED)).toBe(INTENT);
    expect(new URLSearchParams(window.location.search).get("wfr")).toBeNull();
  });

  it("keys recovery to the authenticated viewer, even when the selected legacy actor is unchanged", async () => {
    const originalKey = "boss.first-work-start:org-auth:p1:viewer-one";
    window.sessionStorage.setItem(originalKey, INTENT);
    currentIdentity = { principal_id: "viewer-one", org: { id: "org-auth" } };
    await mount(<AskBossHero projectId="p1" actor="executive_console" orgId="legacy-org" />);
    expect($("[data-testid=work-agreement-check-start]")).not.toBeNull();
    currentIdentity = { principal_id: "viewer-two", org: { id: "org-auth" } };
    await rerender(<AskBossHero projectId="p1" actor="executive_console" orgId="legacy-org" />);
    expect($("[role=dialog]")).toBeNull();
    expect(window.sessionStorage.getItem(originalKey)).toBe(INTENT);
    expect(calls).toEqual([]);
  });

  it("does not restore another viewer's hold when authenticated identity is unavailable", async () => {
    window.sessionStorage.setItem(STORED, INTENT);
    currentIdentity = null;
    await mount(<AskBossHero projectId="p1" actor="owner" />);
    expect($("[role=dialog]")).toBeNull();
    startResponse = async () => { throw new Error("lost response"); };
    await draft(); await click($("[data-testid=work-agreement-confirm]"));
    expect($("[data-testid=work-agreement-check-start]")).not.toBeNull();
    expect(window.sessionStorage.getItem(STORED)).toBe(INTENT);
  });
});

describe("the typed agreement reader", () => {
  it("accepts only a complete server projection for the displayed brief and project", () => {
    expect(readWorkAgreement(agreementFixture(), "p1", BRIEF)).not.toBeNull();
    for (const changed of [{ input: { directive: "edited" } }, { financial: {} }, { steps: [{}] },
      { binding_token: "client-created" }, { selected_account: "invented" }, { repository: {} }]) {
      expect(readWorkAgreement({ ...agreementFixture(), ...changed }, "p1", BRIEF)).toBeNull();
    }
  });
  it("labels subscription history as value, not a cash quote or cap", async () => {
    const agreement = agreementFixture();
    agreement.financial.history = { runs: 3, low_usd: 0.25, high_usd: 1.2, metered: false };
    await mount(<WorkAgreement agreement={agreement} />);
    expect(document.body.textContent).toContain("API-equivalent value");
    expect(document.body.textContent).toContain("not a quote, cash charge, reservation or spending cap");
    expect(document.body.textContent).toContain("Not recorded yet");
  });
});
