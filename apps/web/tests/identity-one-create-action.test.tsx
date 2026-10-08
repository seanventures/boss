// @vitest-environment happy-dom
/**
 * EVERY IDENTITY LIST HAS ITS CREATE ACTION IN EXACTLY ONE PLACE (components/identity/list-header.tsx).
 *
 * Identity › Groups, empty, showed "New group" twice: in the header and in the empty state. API
 * keys and People did the same, and an empty Service accounts page offered three. The rule: in
 * the empty state while the list is empty, in the header otherwise. Each list is mounted empty
 * and with rows, and its create label is counted.
 */
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import { IdentityProvider } from "../components/identity/identity-provider";
import { NowProvider } from "../components/identity/kit";
import { GroupsList } from "../app/identity/groups/groups-list";
import { PeopleList } from "../app/identity/people/people-list";
import { ServiceAccounts } from "../app/identity/service-accounts/service-accounts";
import { SignInSettings } from "../app/identity/sign-in/sign-in-settings";
import { MyKeys } from "../app/account/keys/my-keys";
import { AgentsForMe } from "../app/account/agents/agents-for-me";
import type { MePermissionsResponse, MeResponse } from "../lib/identity/contract";
import { ROLE_PERMISSIONS } from "../lib/identity/permissions.gen";
import * as F from "../lib/identity/stub/fixtures";
import { codeOnly } from "./_helpers/source";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }), useSearchParams: () => new URLSearchParams(), usePathname: () => "/" }));

const NOW = Date.parse("2026-10-06T11:00:00Z");
let root: Root | null = null;
let host: HTMLElement | null = null;
afterEach(() => { act(() => root?.unmount()); host?.remove(); root = null; vi.unstubAllGlobals(); });

const ME: MeResponse = {
  principal_id: "usr_param", kind: "user", display_name: "Param Gnanaguru", email: "param@acme.dev", org: F.ORG, status: "active", access_roles: ["owner"],
  sign_in: { password: true, totp: true, passkeys: 1, recovery_codes_remaining: 10, sso_links: [] }, mfa_required: true, mfa_at: null,
  session_id: "ses_1", session_expires_at: null, csrf_token: "c", epoch: 1, network_bound: false, banners: [], avatar_seed: null,
};
const ORG: MePermissionsResponse = { epoch: 1, scope: "org", permissions: [...ROLE_PERMISSIONS.owner], step_up_fresh_until: null, catalogue_fingerprint: "x" };

async function mount(node: React.ReactElement): Promise<HTMLElement> {
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({}), { status: 200, headers: { "content-type": "application/json" } })));
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => root!.render(<IdentityProvider me={ME} org={ORG}><NowProvider now={NOW}>{node}</NowProvider></IdentityProvider>));
  for (let i = 0; i < 4; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
  return host;
}
function unmount() { act(() => root?.unmount()); host?.remove(); root = null; }

/** Buttons and links whose visible text is the create label. */
const creates = (el: Element, label: RegExp) =>
  [...el.querySelectorAll("button, a")].filter((b) => label.test((b.textContent ?? "").trim()));
/** Where each one sits: the empty state or the list header. */
const where = (el: Element, label: RegExp) => creates(el, label).map((b) =>
  b.closest("[data-testid$='empty'], [data-testid='sa-empty']") ? "empty" : b.closest("[data-list-header]") ? "header" : "elsewhere");

const roles = F.roles(NOW);
const people = F.people(NOW);
const POLICY = F.policy();

const LISTS: { name: string; label: RegExp; empty: () => React.ReactElement; full: () => React.ReactElement }[] = [
  { name: "Groups", label: /^New group$/,
    empty: () => <GroupsList groups={[]} />, full: () => <GroupsList groups={F.groups(NOW, people)} /> },
  { name: "People", label: /^Invite people$/,
    empty: () => <PeopleList people={[]} roles={roles} />, full: () => <PeopleList people={people} roles={roles} /> },
  { name: "Service accounts", label: /^New( service account)?$/,
    empty: () => <ServiceAccounts initial={[]} roles={roles} people={[]} noExpiryAllowed={false} maxDays={365} selected={null} />,
    full: () => <ServiceAccounts initial={F.serviceAccounts(NOW)} roles={roles} people={[]} noExpiryAllowed={false} maxDays={365} selected={null} /> },
  { name: "Sign-in & SSO", label: /^Add provider$/,
    empty: () => <SignInSettings providers={[]} policy={POLICY} legacy={[]} />,
    full: () => <SignInSettings providers={F.providers(NOW)} policy={POLICY} legacy={[]} /> },
  { name: "API keys", label: /^New key$/,
    empty: () => <MyKeys keys={[]} />, full: () => <MyKeys keys={Object.values(F.personalKeys(NOW)).flat()} /> },
  { name: "Agents for me", label: /^Let an agent act for me$/,
    empty: () => <AgentsForMe delegations={[]} agents={null} connections={[]} untilRevokedAllowed={false} openNew={false} />,
    full: () => <AgentsForMe delegations={F.delegations(NOW)} agents={null} connections={[]} untilRevokedAllowed={false} openNew={false} /> },
];

describe("one create action per list", () => {
  for (const l of LISTS) {
    it(`${l.name}: in the empty state when empty, in the header otherwise — never both`, async () => {
      const empty = await mount(l.empty());
      expect(where(empty, l.label), `${l.name} empty`).toEqual(["empty"]);
      unmount();
      const full = await mount(l.full());
      expect(where(full, l.label), `${l.name} with rows`).toEqual(["header"]);
    });
  }

  it("every Identity and My account list heads itself with ListHeader, not a hand-rolled header", () => {
    for (const file of [
      "app/identity/groups/groups-list.tsx", "app/identity/people/people-list.tsx", "app/identity/service-accounts/service-accounts.tsx",
      "app/identity/sign-in/sign-in-settings.tsx", "app/account/keys/my-keys.tsx", "app/account/agents/agents-for-me.tsx",
    ]) expect(codeOnly(file), file).toMatch(/<ListHeader[^>]*empty=/);
  });
});
