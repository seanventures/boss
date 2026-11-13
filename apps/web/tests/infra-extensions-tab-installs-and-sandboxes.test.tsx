// @vitest-environment happy-dom

import React, { act } from "react";
import { afterEach, describe, expect, it } from "vitest";

import { ExtensionFrame, clampHeight } from "../app/extension-frame";
import { ExtensionsTab } from "../app/infra/extensions";
import { parseExtensionList, parsePreview, parseSlots } from "../lib/extensions-view";
import { INFRA_TABS, readInfraUrl } from "../lib/infra-view";
import { mockInfraFetch, type Route } from "./_helpers/infra-fixtures";
import { type } from "./_helpers/infra-cloud-render";
import { byTestId, mount, press, settle, TabHarness, type Mounted } from "./_helpers/infra-tab-render";

/**
 * Infra › Extensions (boss.extension/v1, BOSSAPP-861): install shows the capabilities like an app
 * store and installs only with a matching checksum and the box ticked; each row shows the trust
 * gate's verdict and turns on per project; a panel renders in a sandboxed frame that answers only
 * its own window's two messages.
 */

const CAPS = [
  { capability: "tools.register", words: "Add tools agents can call" },
  { capability: "state.write", words: "Store its own data" },
];
const LIST = {
  extensions: [
    { id: "example.deploy-notes", version: "1.0.0", name: "Deploy notes", capabilities: CAPS,
      tools: [{ name: "add_note", description: "Add", riskTier: 1 }, { name: "clear_notes", description: "Clear", riskTier: 2 }],
      webhooks: [{ name: "deploy", url: "/v1/ext/example.deploy-notes/webhook/deploy" }],
      slots: [{ type: "settings_panel", name: "settings", title: "Deploy notes settings" }],
      trust: { trusted: true, problems: [], approvedChecksum: "sha256:aa" }, enabled: false, enabledProjects: [] },
    { id: "acme.broken", version: "0.1.0", name: "Broken", capabilities: CAPS, tools: [], webhooks: [], slots: [],
      trust: { trusted: false, problems: ["its files changed after it was installed"] }, enabled: false },
  ],
};
const PREVIEW = { preview: { id: "example.deploy-notes", version: "1.0.0", name: "Deploy notes", capabilities: CAPS,
                             computedChecksum: "sha256:abc", declaredChecksum: "sha256:abc", trust: { trusted: true, problems: [] } } };

let m: Mounted | null = null;
let restore: (() => void) | null = null;
afterEach(async () => { await m?.unmount(); m = null; restore?.(); restore = null; });

async function open() {
  const routes: Route[] = [
    { match: /^\/v1\/extensions$/, reply: () => LIST },
    { method: "POST", match: /^\/v1\/extensions\/preview$/, reply: () => PREVIEW },
  ];
  const f = mockInfraFetch({ routes });
  restore = f.install();
  m = await mount(<TabHarness Tab={ExtensionsTab} />, "?area=infra&view=extensions");
  await settle(() => !!byTestId("inf-extensions-table"), "extensions table");
  return f;
}

describe("Infra › Extensions", () => {
  it("is a tab and parses the wire", () => {
    expect(INFRA_TABS.map((t) => t.key)).toContain("extensions");
    expect(readInfraUrl("?view=extensions").view).toBe("extensions");
    const rows = parseExtensionList(LIST).rows;
    expect(rows[0]).toMatchObject({ id: "example.deploy-notes", enabled: false });
    expect(rows[0].trust.trusted).toBe(true);
    expect(rows[1].trust.problems).toEqual(["its files changed after it was installed"]);
    expect(parsePreview(PREVIEW).capabilities.map((c) => c.capability)).toEqual(["tools.register", "state.write"]);
    expect(parseSlots({ slots: [{ extensionId: "x.y", name: "notes", type: "project_panel" }, { name: "no-ext" }] }))
      .toEqual([{ extensionId: "x.y", name: "notes", title: "notes", type: "project_panel" }]);
  });

  it("shows the trust verdict and refuses to turn a blocked extension on", async () => {
    await open();
    const ok = byTestId("inf-ext-row-example.deploy-notes")!;
    expect(byTestId("inf-ext-trusted", ok)).toBeTruthy();
    const broken = byTestId("inf-ext-row-acme.broken")!;
    expect(byTestId("inf-ext-blocked", broken)!.getAttribute("title")).toMatch(/files changed/);
    expect((byTestId("inf-ext-on", broken) as HTMLButtonElement).disabled).toBe(true);
  });

  it("previews the consent and installs only with a matching checksum and the box ticked", async () => {
    const f = await open();
    await type(byTestId("inf-ext-source"), "plugins/extensions/deploy-notes");
    await type(byTestId("inf-ext-checksum"), "sha256:wrong");
    await press(byTestId("inf-ext-check"), "Check");
    await settle(() => !!byTestId("inf-ext-consent"), "consent");
    expect(byTestId("inf-ext-asks")!.textContent).toContain("Add tools agents can call");
    expect(byTestId("inf-ext-checksum-verdict")!.textContent).toContain("does not match");
    const go = byTestId("inf-ext-install-go") as HTMLButtonElement;
    expect(go.disabled).toBe(true);
    await type(byTestId("inf-ext-checksum"), "sha256:abc");
    await press(byTestId("inf-ext-accept"), "accept");
    expect((byTestId("inf-ext-install-go") as HTMLButtonElement).disabled).toBe(false);
    await press(byTestId("inf-ext-install-go"), "Install");
    await settle(() => f.writes().some((w) => w.path === "/v1/extensions/install"), "install post");
    const install = f.writes().find((w) => w.path === "/v1/extensions/install")!;
    expect(install.body).toEqual({ source: "plugins/extensions/deploy-notes", checksum: "sha256:abc",
                                   accept: ["tools.register", "state.write"] });
  });

  it("turns an extension on in this project", async () => {
    const f = await open();
    await press(byTestId("inf-ext-on", byTestId("inf-ext-row-example.deploy-notes")!), "On");
    await settle(() => f.writes().length > 0, "enable post");
    expect(f.writes()[0]).toMatchObject({ path: "/v1/extensions/example.deploy-notes/enable", body: { project_id: "project_1" } });
  });
});

describe("ExtensionFrame", () => {
  it("renders the document in an opaque sandbox and answers only its own frame", async () => {
    const html = "<!doctype html><html><head></head><body>panel</body></html>";
    const contexts: string[] = [];
    const prev = globalThis.fetch;
    globalThis.fetch = (async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/context")) {
        contexts.push(url);
        return new Response(JSON.stringify({ context: { extensionId: "x.y", data: { notes: [] } } }),
                            { status: 200, headers: { "Content-Type": "application/json" } });
      }
      return new Response(html, { status: 200, headers: { "Content-Type": "text/html" } });
    }) as typeof fetch;
    restore = () => { globalThis.fetch = prev; };
    m = await mount(<ExtensionFrame slot={{ extensionId: "x.y", name: "notes", title: "Notes", type: "project_panel" }} projectId="p1" />);
    await settle(() => !!byTestId("ext-frame-x.y-notes"), "frame");
    const frame = byTestId("ext-frame-x.y-notes") as HTMLIFrameElement;
    expect(frame.getAttribute("sandbox")).toBe("allow-scripts");
    expect(frame.getAttribute("srcdoc")).toBe(html);
    expect(frame.getAttribute("referrerpolicy")).toBe("no-referrer");

    // a message from any other window is ignored
    await act(async () => {
      window.dispatchEvent(new MessageEvent("message", { data: { bossExt: 1, type: "resize", height: 999 }, source: window }));
    });
    expect(frame.style.height).toBe("120px");
    expect(contexts).toHaveLength(0);
    expect(clampHeight(5)).toBe(40);
    expect(clampHeight(99999)).toBe(2000);
  });
});
