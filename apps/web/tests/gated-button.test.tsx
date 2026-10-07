// @vitest-environment happy-dom
/**
 * components/gated.tsx, the pre-grey primitive for buttons that press a server action:
 *   - refused, the button stays visible and focusable, is aria-disabled, its tooltip and
 *     screen-reader text say "Needs “<name>” · ask an Admin", and a click (or a form submit
 *     through it) does nothing;
 *   - allowed, it is the caller's own button: class, title and handler untouched;
 *   - useGate's guard and props do the same for a button a file draws itself.
 */
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import { GatedButton, useGate } from "../components/gated";
import { IdentityProvider } from "../components/identity/identity-provider";
import type { MePermissionsResponse, MeResponse } from "../lib/identity/contract";
import { ROLE_PERMISSIONS } from "../lib/identity/permissions.gen";
import { needsWords } from "../lib/identity/words";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let root: Root | null = null;
let host: HTMLElement | null = null;
afterEach(() => { act(() => root?.unmount()); host?.remove(); root = null; });

function perms(role: keyof typeof ROLE_PERMISSIONS): MePermissionsResponse {
  return { epoch: 1, scope: "org", permissions: [...ROLE_PERMISSIONS[role]], step_up_fresh_until: null, catalogue_fingerprint: "x" };
}
const ME = {
  principal_id: "usr_x", kind: "user", display_name: "Vee", email: "v@acme.dev", org: { id: "org_1", name: "Acme" }, status: "active",
  access_roles: ["viewer"], sign_in: { password: true, totp: false, passkeys: 0, recovery_codes_remaining: 0, sso_links: [] },
  mfa_required: false, mfa_at: null, session_id: "s", session_expires_at: null, csrf_token: "c", epoch: 1, network_bound: false, banners: [],
} as MeResponse;

async function mount(node: React.ReactElement, role: keyof typeof ROLE_PERMISSIONS) {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => root!.render(<IdentityProvider me={ME} org={perms(role)}>{node}</IdentityProvider>));
  return host;
}

function Drawn({ onPress }: { onPress: () => void }) {
  const g = useGate("system:operate");
  return <button type="button" className="mine" title="Mine" {...g.props} onClick={g.guard(onPress)}>Go{g.sr}</button>;
}

describe("GatedButton", () => {
  it("refused: visible, aria-disabled, says why, and a click does nothing", async () => {
    const press = vi.fn();
    const el = await mount(<GatedButton permission="system:operate" className="cc-btn" title="Clean up" onClick={press}>Clean up</GatedButton>, "viewer");
    const b = el.querySelector("button")!;
    expect(b.getAttribute("aria-disabled")).toBe("true");
    expect(b.hasAttribute("disabled")).toBe(false);
    expect(b.className).toBe("cc-btn");
    expect(b.getAttribute("title")).toBe(needsWords("system:operate"));
    expect(b.textContent).toContain(needsWords("system:operate"));
    expect(b.getAttribute("title")).not.toContain("system:operate");
    await act(async () => b.click());
    expect(press).not.toHaveBeenCalled();
  });

  it("refused submit: the form is not submitted", async () => {
    const submit = vi.fn((e: React.FormEvent) => e.preventDefault());
    const el = await mount(<form onSubmit={submit}><GatedButton permission="system:operate" type="submit">Go</GatedButton></form>, "viewer");
    await act(async () => el.querySelector("button")!.click());
    expect(submit).not.toHaveBeenCalled();
  });

  it("allowed: the caller's button, title and handler untouched", async () => {
    const press = vi.fn();
    const el = await mount(<GatedButton permission="system:operate" className="cc-btn" title="Clean up" onClick={press}>Clean up</GatedButton>, "owner");
    const b = el.querySelector("button")!;
    expect(b.hasAttribute("aria-disabled")).toBe(false);
    expect(b.getAttribute("title")).toBe("Clean up");
    expect(b.textContent).toBe("Clean up");
    await act(async () => b.click());
    expect(press).toHaveBeenCalledOnce();
  });
});

describe("useGate", () => {
  it("gates a button the file draws itself", async () => {
    const press = vi.fn();
    const viewer = await mount(<Drawn onPress={press} />, "viewer");
    const b = viewer.querySelector("button")!;
    expect(b.getAttribute("aria-disabled")).toBe("true");
    expect(b.getAttribute("title")).toBe(needsWords("system:operate"));
    await act(async () => b.click());
    expect(press).not.toHaveBeenCalled();
    act(() => root?.unmount()); host?.remove();

    const owner = await mount(<Drawn onPress={press} />, "owner");
    const o = owner.querySelector("button")!;
    expect(o.getAttribute("title")).toBe("Mine");
    await act(async () => o.click());
    expect(press).toHaveBeenCalledOnce();
  });
});
