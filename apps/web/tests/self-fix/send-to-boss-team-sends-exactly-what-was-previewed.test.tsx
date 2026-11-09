// @vitest-environment happy-dom
/**
 * "Send to BOSS team" (docs/product/BOSS_FIXES_ITSELF.md §7.1–7.4, §10.4): opening it asks BOSS
 * to build and store the exact preview; the sheet shows the title, labels, the diff (not
 * editable), the editable body and evidence with their scrubbed counts, the trailers, the dedupe
 * line and where it goes. Send needs the "I checked" tick; editing resets the tick and must be
 * saved first; a private name, address or secret in the diff blocks it; telemetry off leaves only
 * Export patch. Send posts the stored send's id with `checked: true` and nothing else.
 */
import React from "react";
import { afterEach, describe, expect, it } from "vitest";

import { SELF_FIX_OPS } from "../../lib/self-fix-ops";
import { $$, click, mountAs, tid, type, unmount } from "../_helpers/multirepo-fixtures";
import { SFX, fakeSelfFixApi, newState, opsOf, send } from "./_fixtures";

const { SendSheet, channelSentence } = await import("../../app/self-fix/send-sheet");

afterEach(unmount);

async function open(state = newState()) {
  const calls = fakeSelfFixApi(state);
  await mountAs(<SendSheet projectId="p1" actor="ceo" selfFixId={SFX} onClose={() => {}} />, "owner");
  return calls;
}
const disabled = (id: string) => (tid(id) as HTMLButtonElement | null)?.disabled;

describe("Send to BOSS team", () => {
  it("builds the preview when none is stored, and shows exactly what will be sent", async () => {
    const calls = await open();
    expect(opsOf(calls, "preview")).toHaveLength(1);
    expect(tid("sfx-send-channel")?.textContent).toBe(
      "Opens a pull request on github.com/seanventures/boss from your fork example-person/boss.");
    expect(tid("sfx-send-similar")?.textContent).toBe("Similar: none open, none merged");
    expect(tid("sfx-send-title")?.textContent).toContain("fix(deploy)");
    expect($$('[data-testid="sfx-send-labels"] .cap-badge').map((b) => b.textContent)).toEqual(
      ["source:boss-agent", "boss:self-fix", "area:deploy", "verified:local", "engine:codex"]);
    expect(tid("sfx-send-diff")?.textContent).toContain("2 file(s), +154 −1");
    expect(tid("sfx-send-view-diff")?.getAttribute("title")).toContain("Not editable");
    expect(tid("sfx-send-trailers")?.textContent).toContain("BOSS-Self-Fix-Ref: 3fa9c0e1b2d4");
    expect(tid("sfx-send-check-ok")).toBeTruthy();
    expect(tid("sfx-send-export")?.getAttribute("href")).toContain("view=patch");
  });

  it("reuses a stored preview instead of building another", async () => {
    const calls = await open(newState({ upstream: { send: send(), telemetry_off: null } }));
    expect(opsOf(calls, "preview")).toHaveLength(0);
    expect(tid("sfx-send-title")).toBeTruthy();
  });

  it("Send waits for the tick, and sends the stored preview with checked: true", async () => {
    const state = newState();
    state.ops.send = { body: send({ status: "sent", result: { url: "https://github.com/seanventures/boss/pull/412" } }) };
    const calls = await open(state);
    expect(disabled("sfx-send-go")).toBe(true);
    await click(tid("sfx-send-checked"));
    expect(disabled("sfx-send-go")).toBe(false);
    await click(tid("sfx-send-go"));
    expect(opsOf(calls, "send")).toEqual([expect.objectContaining({ id: SFX, send_id: "sfs_1", checked: true })]);
    expect(tid("sfx-send-sent")?.querySelector("a")?.getAttribute("href")).toContain("/pull/412");
  });

  it("editing resets the tick and must be saved before Send", async () => {
    const state = newState();
    state.ops.edit_preview = { body: send({ body: "edited body", send_id: "sfs_2" }) };
    state.ops.send = { body: send({ send_id: "sfs_2", status: "sent" }) };
    const calls = await open(state);
    await click(tid("sfx-send-checked"));
    await type(tid("sfx-send-body"), "edited body");
    expect((tid("sfx-send-checked") as HTMLInputElement).checked).toBe(false);
    expect(disabled("sfx-send-go")).toBe(true);
    await click(tid("sfx-send-save"));
    expect(opsOf(calls, "edit_preview")).toEqual([expect.objectContaining({ send_id: "sfs_1", body: "edited body" })]);
    expect(tid("sfx-send-save")).toBeNull();
    await click(tid("sfx-send-checked"));
    await click(tid("sfx-send-go"));
    expect(opsOf(calls, "send")[0]).toMatchObject({ send_id: "sfs_2" });
  });

  it("a private name, address or secret in the diff blocks the send and says where", async () => {
    const state = newState();
    state.ops.preview = { body: send({ status: "previewed", check: { ok: false, findings: [
      { where: "diff", kind: "install name", detail: "line 12" }, { where: "evidence", kind: "address", detail: "" }] } }) };
    await open(state);
    expect(tid("sfx-send-blocked")?.textContent).toContain("diff: install name (line 12)");
    expect($$('[data-testid="sfx-send-finding"]')).toHaveLength(2);
    expect(disabled("sfx-send-go")).toBe(true);
    expect((tid("sfx-send-checked") as HTMLInputElement).disabled).toBe(true);
  });

  it("with sharing off on this install, only Export patch is offered, with the reason", async () => {
    await open(newState({ upstream: { send: send(), telemetry_off: "BOSS_TELEMETRY=off" } }));
    expect(tid("sfx-send-go")).toBeNull();
    expect(tid("sfx-send-off")?.textContent).toContain("BOSS_TELEMETRY=off");
    expect(tid("sfx-send-export")).toBeTruthy();
  });

  it("a pull request without a fork offers Create my fork, then rebuilds the preview", async () => {
    const state = newState();
    state.ops.preview = { body: send({ target: { upstream: "seanventures/boss", fork: null, pr_url: null } }) };
    state.ops.fork = { body: { fork: "example-person/boss" } };
    const calls = await open(state);
    expect(tid("sfx-send-channel")?.textContent).toContain("needs your fork first");
    expect(disabled("sfx-send-go")).toBe(true);
    await click(tid("sfx-send-fork"));
    expect(opsOf(calls, "fork")).toHaveLength(1);
    expect(opsOf(calls, "preview")).toHaveLength(2);
  });

  it("says each channel in words", () => {
    expect(channelSentence(send({ channel: "github_comment", dedupe: { state: "open_pr", checked_at: null,
      match: { url: "https://github.com/seanventures/boss/pull/7", number: 7, title: "x" }, why: "" } })))
      .toBe("Adds this install's evidence to #7 instead of a new pull request.");
    expect(channelSentence(send({ channel: "collector" }))).toContain("collector");
    expect(channelSentence(send({ channel: "patch_export" }))).toBe("Exported as a patch for you to send.");
  });

  it("the send op never defaults the check to true", () => {
    expect(SELF_FIX_OPS.send.body({ project_id: "p1", actor: "ceo", id: SFX, send_id: "s" }).checked).toBe(false);
    expect(SELF_FIX_OPS.send.body({ project_id: "p1", actor: "ceo", id: SFX, send_id: "s", checked: "yes" }).checked).toBe(false);
  });
});
