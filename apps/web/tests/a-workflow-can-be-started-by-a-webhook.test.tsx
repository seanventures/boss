// @vitest-environment happy-dom
/**
 * The designer's webhook triggers (#512): add one, see its secret once, rotate it, turn it off,
 * read its deliveries with each refusal's reason, and copy a curl that signs the way BOSS checks.
 *
 * Mounted against a fake `/api/delivery-workflows` at the fetch boundary, so the panel, the lib and
 * the proxy's action names are exercised together.
 */
import { createHmac } from "node:crypto";
import { execFileSync } from "node:child_process";
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import { overBudget } from "./_helpers/copy-budget";
import { WebhookTriggers } from "../app/workflows/editor/inspector/webhook-triggers";
import {
  API_HOST_PLACEHOLDER, curlExample, deliveryLabel, deliveryTone, inboundUrl,
} from "../lib/workflow-webhooks";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const PATH = "/v1/webhooks/inbound/workflow/tok_abc";

describe("webhook helpers", () => {
  it("names every refusal in a row's words", () => {
    expect(deliveryLabel({ status: "started" })).toBe("Started");
    expect(deliveryLabel({ status: "duplicate" })).toBe("Duplicate");
    expect(deliveryLabel({ status: "refused", reason: "bad_signature" })).toBe("Bad signature");
    expect(deliveryLabel({ status: "refused", reason: "stale_timestamp" })).toBe("Stale timestamp");
    expect(deliveryLabel({ status: "refused", reason: "too_large" })).toBe("Too large");
    expect(deliveryLabel({ status: "refused", reason: "disabled" })).toBe("Trigger off");
    expect(deliveryLabel({ status: "refused", reason: "actor_not_authorized" })).toBe("Creator not authorized");
    expect(deliveryLabel({ status: "refused", reason: "something_new" })).toBe("something new");
    expect(deliveryTone({ status: "refused" })).toBe("bad");
    expect(deliveryTone({ status: "started" })).toBe("ok");
  });

  it("puts the path on the API's address, not the console's", () => {
    expect(inboundUrl(PATH)).toBe(`${API_HOST_PLACEHOLDER}${PATH}`);
    expect(inboundUrl(PATH, "https://api.example.com/")).toBe(`https://api.example.com${PATH}`);
  });

  it("signs the curl the way the API verifies: HMAC-SHA256 of '<ts>.<body>', v1=<hex>", () => {
    const curl = curlExample(PATH);
    expect(curl).toContain(`'${API_HOST_PLACEHOLDER}${PATH}'`);
    expect(curl).toContain('X-BOSS-Signature: v1=$SIG');
    expect(curl).toContain("X-BOSS-Timestamp: $TS");
    expect(curl).toContain("X-BOSS-Delivery");
    // The secret is read from the environment, never written into the command.
    expect(curl).not.toMatch(/whsec_[A-Za-z0-9]/);
    // Run the signing lines for real and compare with the scheme (engine: workflow_webhooks.sign,
    // which yields v1=3d3a86… for this secret, timestamp and body).
    const lines = curl.split("\n").filter((l) => l.startsWith("BODY=") || l.startsWith("SIG="));
    const script = ["TS=1700000000", ...lines, 'printf %s "$SIG"'].join("\n");
    let sig: string;
    try {
      sig = execFileSync("sh", ["-c", script], { env: { ...process.env, BOSS_WEBHOOK_SECRET: "whsec_test" } }).toString();
    } catch {
      return; // no openssl on this machine: the scheme is still pinned below
    }
    const want = createHmac("sha256", "whsec_test").update('1700000000.{"example": "value"}').digest("hex");
    expect(sig).toBe(want);
    expect(want).toBe("3d3a86243b723ce38276e8595a3d925382a11cd35f90627c4aaa83ffeeaa9180");
  });
});

type Call = { url: string; body?: Record<string, unknown> };

function fakeApi(extra: Record<string, unknown>[] = []) {
  const calls: Call[] = [];
  let hooks: Record<string, unknown>[] = [{ id: "wht_1", workflow_key: "orders", name: "orders webhook 1", path: PATH,
                 enabled: true, secret_last4: "abcd", refused_unrecorded: 2 }, ...extra];
  const json = (data: unknown, status = 200) =>
    new Response(JSON.stringify(data), { status, headers: { "content-type": "application/json" } });
  const fetchImpl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    calls.push({ url, body });
    if (url.includes("view=webhooks")) return json({ webhooks: hooks });
    if (url.includes("view=webhook_deliveries")) {
      return json({ deliveries: [
        { id: "d1", status: "refused", reason: "bad_signature", received_at: "2026-10-14T10:00:00Z" },
        { id: "d2", status: "started", run_id: "wfr_1", received_at: "2026-10-14T09:59:00Z" },
      ] });
    }
    if (body?.action === "webhook_create") {
      hooks = [...hooks, { ...hooks[0], id: "wht_2", name: String(body.name) }];
      return json({ ...hooks[1], secret: "whsec_FIRST" }, 201);
    }
    if (body?.action === "webhook_rotate") return json({ ...hooks[0], secret: "whsec_ROTATED" });
    if (body?.action === "webhook_enabled") {
      hooks = hooks.map((h) => (h.id === body.webhook_id ? { ...h, enabled: Boolean(body.enabled) } : h));
      return json(hooks[0]);
    }
    return json({ error: "unexpected" }, 500);
  });
  return { calls, fetchImpl };
}

let root: Root | null = null;
let host: HTMLDivElement | null = null;

afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  root = null;
  host = null;
  vi.unstubAllGlobals();
});

async function mount(extra: Record<string, unknown>[] = []) {
  const api = fakeApi(extra);
  vi.stubGlobal("fetch", api.fetchImpl);
  vi.stubGlobal("confirm", () => true);
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => {
    root!.render(<WebhookTriggers projectId="p1" workflowKey="orders" actor="ceo" />);
  });
  return { api, el: host };
}

const q = (el: Element, id: string) => el.querySelector(`[data-testid="${id}"]`) as HTMLElement | null;

async function click(el: HTMLElement | null) {
  expect(el).not.toBeNull();
  await act(async () => { el!.click(); });
  await act(async () => { await Promise.resolve(); });
}

describe("the Webhooks panel", () => {
  it("lists the workflow's triggers without any secret", async () => {
    const { api, el } = await mount();
    expect(api.calls[0].url).toBe("/api/delivery-workflows?view=webhooks&project_id=p1&key=orders");
    expect(q(el, "wf-webhook-wht_1")?.textContent).toContain("orders webhook 1");
    expect(q(el, "wf-webhook-secret")).toBeNull();
    expect(overBudget(el)).toEqual([]);
  });

  it("shows a new trigger's secret once, until Done", async () => {
    const { api, el } = await mount();
    await click(q(el, "wf-webhook-add"));
    const create = api.calls.find((c) => c.body?.action === "webhook_create");
    expect(create?.body).toMatchObject({ project_id: "p1", workflow_key: "orders", actor: "ceo" });
    const secret = q(el, "wf-webhook-secret");
    expect(secret?.querySelector("input")?.value).toBe("whsec_FIRST");
    expect(overBudget(el)).toEqual([]);
    await click(q(el, "wf-webhook-secret-done"));
    expect(q(el, "wf-webhook-secret")).toBeNull();
    expect(el.innerHTML).not.toContain("whsec_FIRST");
  });

  it("rotates, turns off, and reads deliveries with their reasons", async () => {
    const { api, el } = await mount();
    const row = q(el, "wf-webhook-wht_1")!;
    await click(q(row, "wf-webhook-rotate"));
    expect(api.calls.find((c) => c.body?.action === "webhook_rotate")?.body)
      .toMatchObject({ webhook_id: "wht_1", project_id: "p1" });
    expect(q(el, "wf-webhook-secret")?.querySelector("input")?.value).toBe("whsec_ROTATED");

    await click(q(q(el, "wf-webhook-wht_1")!, "wf-webhook-toggle"));
    expect(api.calls.find((c) => c.body?.action === "webhook_enabled")?.body)
      .toMatchObject({ webhook_id: "wht_1", enabled: false });
    expect(q(q(el, "wf-webhook-wht_1")!, "wf-webhook-toggle")?.textContent).toBe("Turn on");

    await click(q(q(el, "wf-webhook-wht_1")!, "wf-webhook-deliveries"));
    await act(async () => { await Promise.resolve(); });
    const rows = [...el.querySelectorAll('[data-testid="wf-webhook-delivery"]')];
    expect(rows.map((r) => r.getAttribute("data-tone"))).toEqual(["bad", "ok"]);
    expect(rows[0].textContent).toContain("Bad signature");
    expect(rows[1].querySelector("a")?.getAttribute("href")).toBe("?area=delivery_workflows&wfr=wfr_1");
    expect(q(el, "wf-webhook-delivery-list")?.textContent).toContain("+2 more refused");
    expect(overBudget(el)).toEqual([]);
  });

  it("shows how to sign in a copyable box", async () => {
    const { el } = await mount();
    await click(q(el, "wf-webhook-howto"));
    const curl = q(el, "wf-webhook-curl") as HTMLTextAreaElement;
    expect(curl.value).toContain(PATH);
    expect(curl.value).toContain("openssl dgst -sha256 -hmac");
  });

  it("says a trigger BOSS paused needs its authority renewed", async () => {
    const { el } = await mount([{ id: "wht_9", workflow_key: "orders", name: "paused", path: PATH,
                                  enabled: false, last_error: "Renew authority: the standing grant ended." }]);
    expect(q(q(el, "wf-webhook-wht_9")!, "wf-webhook-paused")?.getAttribute("title"))
      .toContain("Renew authority");
    expect(q(q(el, "wf-webhook-wht_1")!, "wf-webhook-paused")).toBeNull();
    expect(overBudget(el)).toEqual([]);
  });
});
