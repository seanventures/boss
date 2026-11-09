// @vitest-environment happy-dom

import { act } from "react";
import { afterEach, describe, expect, it } from "vitest";

import { cloudFixture, readOnly } from "./_helpers/infra-cloud-fixtures";
import { $, mountCloud, press, settle, type, type Mounted } from "./_helpers/infra-cloud-render";

/**
 * The cloud-account workspace keeps every capability it had (infra-observability spec §4.8,
 * parity P-S-03..P-S-15), now as Infra › Cloud › Accounts.
 *
 * Rewritten consciously: this file used to read provider-configuration-ui.tsx's source for
 * strings ("Paste and connect", "Advanced: bind an existing SecretRef", "Partial data (secondary
 * APIs):"). That screen moved into Infra › Cloud, so the same promises are checked by rendering
 * the new one: the paste card comes before the secret-reference form, a key is write-only and
 * never drawn, a planned adapter accepts nothing, rotation keeps its bounded overlap, revoking
 * asks for the account's name, a read-only viewer gets no write control, and Community says
 * `Bring your own key`.
 */

let m: Mounted | null = null;
afterEach(async () => { await m?.done(); m = null; });

describe("cloud accounts (was the provider configuration workspace)", () => {
  it("puts Paste key above Use secret ref, with write-only key fields", async () => {
    m = await mountCloud("?area=infra&view=cloud");
    await settle(() => !!$("inf-account-new"), "accounts");
    await press("inf-account-new");
    const paste = $("inf-account-paste")!;
    const refs = $("inf-account-secret-ref")!;
    expect(paste.compareDocumentPosition(refs) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    const secret = $("inf-paste-secret") as HTMLInputElement;
    expect(secret.type).toBe("password");
    expect(secret.autocomplete).toBe("new-password");
    expect(secret.minLength).toBe(8);
    for (const input of refs.querySelectorAll<HTMLInputElement>("input[placeholder='Secret reference']")) expect(input.type).toBe("password");
  });

  it("offers Base URL only for a provider that takes one, and clears the pasted key once sent", async () => {
    m = await mountCloud("?area=infra&view=cloud");
    await settle(() => !!$("inf-account-new"), "accounts");
    await press("inf-account-new");
    expect($("inf-account-paste")?.textContent).not.toContain("Base URL");
    await type($("inf-paste-provider"), "openai");
    expect($("inf-account-paste")?.textContent).toContain("Base URL");
    await type($("inf-paste-label"), "Local models");
    await type($("inf-paste-secret"), "sk-local-1234567890");
    await act(async () => { ($("inf-paste-submit") as HTMLButtonElement).click(); });
    await settle(() => m!.api.writes().length === 1, "paste");
    expect(document.body.textContent).not.toContain("sk-local-1234567890");
  });

  it("a planned adapter accepts no credential", async () => {
    m = await mountCloud("?area=infra&view=cloud");
    await settle(() => !!$("inf-account-new"), "accounts");
    await press("inf-account-new");
    await press("inf-catalog-hetzner");
    expect(($("inf-ref-submit") as HTMLButtonElement).disabled).toBe(true);
    expect($("inf-account-secret-ref")?.textContent).toContain("Adapter planned");
  });

  it("never draws a credential reference; rotation offers 5, 15, 30 and 60 minutes of overlap", async () => {
    m = await mountCloud("?area=infra&view=cloud&account=acct_1");
    await settle(() => !!$("inf-account-drawer"), "drawer");
    expect($("inf-account-credentials")?.textContent).toContain("•••••• set");
    expect(document.body.textContent).not.toMatch(/env:BOSS_/);
    const overlap = $("inf-account-overlap") as HTMLSelectElement;
    expect([...overlap.options].map((o) => o.value)).toEqual(["5", "15", "30", "60"]);
    expect(overlap.value).toBe("30");
  });

  it("revokes only after the account's exact name is typed", async () => {
    m = await mountCloud("?area=infra&view=cloud&account=acct_1");
    await settle(() => !!$("inf-account-revoke"), "drawer");
    await press("inf-account-revoke");
    const yes = () => $("inf-account-revoke-confirm") as HTMLButtonElement;
    expect(yes().disabled).toBe(true);
    await type($("inf-account-revoke-name"), "digitalocean · prod");
    expect(yes().disabled).toBe(true);
    await type($("inf-account-revoke-name"), "DigitalOcean · prod");
    expect(yes().disabled).toBe(false);
  });

  it("a disabled account offers Enable, an active one Disable", async () => {
    m = await mountCloud("?area=infra&view=cloud&account=acct_2");
    await settle(() => !!$("inf-account-drawer"), "drawer");
    expect($("inf-account-enable")).not.toBeNull();
    expect($("inf-account-disable")).toBeNull();
  });

  it("a read-only viewer sees the accounts and their impact, and no write control", async () => {
    const route = { match: /^\/v1\/infra\/cloud$/, etag: true, reply: (u: URL) => readOnly(cloudFixture(u.searchParams.get("view") ?? "accounts")) };
    m = await mountCloud("?area=infra&view=cloud&account=acct_1", [route]);
    await settle(() => !!$("inf-account-drawer"), "drawer");
    expect($("inf-cloud-notice")?.getAttribute("title")).toBe("You lack providerops_manage");
    for (const id of ["inf-account-new", "inf-account-save", "inf-account-test", "inf-account-rotate", "inf-account-revoke"]) expect($(id), id).toBeNull();
    expect($("inf-account-impact")).not.toBeNull();
  });

  it("Community says Bring your own key, as one chip", async () => {
    m = await mountCloud("?area=infra&view=cloud");
    await settle(() => !!$("inf-cloud-notice"), "notice");
    expect($("inf-cloud-notice")?.textContent).toBe("Bring your own key");
  });

  it("a feed that did not answer is named in one line", async () => {
    const route = { match: /^\/v1\/infra\/cloud$/, etag: true, reply: () => ({ ...cloudFixture("accounts"), partial: ["costs"] }) };
    m = await mountCloud("?area=infra&view=cloud", [route]);
    await settle(() => !!$("inf-cloud-partial"), "partial");
    expect($("inf-cloud-partial")?.textContent).toBe("Some data missing, costs");
  });
});
