// @vitest-environment happy-dom
/**
 * The Agents card draws twelve faces. A longer directory says how many of those
 * agents are on this page and how many are not. Twelve or fewer stays quiet.
 * The All agents link and the suspended count stay.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";

import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import { IdentityProvider } from "../components/identity/identity-provider";
import { NowProvider } from "../components/identity/kit";
import { Overview, agentRestSentence } from "../app/identity/overview";
import type { MePermissionsResponse, MeResponse } from "../lib/identity/contract";
import { ROLE_PERMISSIONS } from "../lib/identity/permissions.gen";
import * as F from "../lib/identity/stub/fixtures";
import type { Passport } from "../lib/identity/types";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
  usePathname: () => "/",
}));

const NOW = Date.parse("2026-10-06T11:00:00Z");
let root: Root | null = null;
let host: HTMLElement | null = null;

afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  root = null;
  host = null;
});

function perms(role: keyof typeof ROLE_PERMISSIONS): MePermissionsResponse {
  return {
    epoch: 1, scope: "org", permissions: [...ROLE_PERMISSIONS[role]],
    step_up_fresh_until: null, catalogue_fingerprint: "x",
  };
}

function me(): MeResponse {
  return {
    principal_id: "usr_x", kind: "user", display_name: "Param Gnanaguru", email: "x@acme.dev",
    org: F.ORG, status: "active", access_roles: ["owner"],
    sign_in: { password: true, totp: true, passkeys: 1, recovery_codes_remaining: 10, sso_links: [] },
    mfa_required: true, mfa_at: null, session_id: "ses_1", session_expires_at: null,
    csrf_token: "c", epoch: 1, network_bound: false, banners: [], avatar_seed: null,
  };
}

function agents(count: number): Passport[] {
  const base = F.passports(NOW);
  return Array.from({ length: count }, (_, i) => {
    const src = base[i % base.length]!;
    return { ...src, id: `agt_n${i}`, name: `Agent ${i + 1}`, role_key: `role_${i}` };
  });
}

function suspended(count: number): number {
  return agents(count).filter((a) => a.status === "suspended").length;
}

async function mount(count: number): Promise<HTMLElement> {
  const people = F.people(NOW);
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ grantors: [] }), { status: 200 })));
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => {
    root!.render(
      <IdentityProvider me={me()} org={perms("owner")}>
        <NowProvider now={NOW}>
          <Overview
            posture={null}
            people={people}
            roles={F.roles(NOW)}
            groups={F.groups(NOW, people)}
            serviceAccounts={F.serviceAccounts(NOW)}
            agents={agents(count)}
            events={F.auditEvents(NOW).slice(0, 6)}
          />
        </NowProvider>
      </IdentityProvider>,
    );
  });
  return host;
}

function card(el: HTMLElement): HTMLElement {
  const node = el.querySelector("[aria-labelledby='idn-agents-h']");
  if (!node) throw new Error("agents card missing");
  return node as HTMLElement;
}

describe("agentRestSentence", () => {
  it("says twelve are on the page and how many are not", () => {
    expect(agentRestSentence(13)).toBe("12 agents are on this page. 1 more are not.");
    expect(agentRestSentence(15)).toBe("12 agents are on this page. 3 more are not.");
  });

  it("is null when every agent fits on the page", () => {
    expect(agentRestSentence(0)).toBeNull();
    expect(agentRestSentence(1)).toBeNull();
    expect(agentRestSentence(12)).toBeNull();
  });
});

describe("the identity overview says how many agents are hidden", () => {
  it("says twelve faces are on the page and how many are not", async () => {
    const el = await mount(13);
    const agentsCard = card(el);
    const faces = agentsCard.querySelector(".idn-agents");
    const note = faces?.nextElementSibling;
    expect(faces?.querySelectorAll("a")).toHaveLength(12);
    expect(faces?.textContent).not.toContain("Agent 13");
    expect(note?.tagName).toBe("P");
    expect(note?.textContent).toBe("12 agents are on this page. 1 more are not.");
    expect(agentsCard.querySelector("a[href='/identity/agents']")?.textContent).toContain("All agents");
    expect(agentsCard.querySelector(".idn-sub")?.textContent).toBe(`${suspended(13)} suspended`);
  });

  it("stays quiet when twelve or fewer agents are in the directory", async () => {
    const el = await mount(12);
    const agentsCard = card(el);
    const faces = agentsCard.querySelector(".idn-agents");
    expect(faces?.querySelectorAll("a")).toHaveLength(12);
    expect(faces?.nextElementSibling?.tagName).not.toBe("P");
    expect(agentsCard.textContent).not.toContain("more are not");
    expect(agentsCard.textContent).not.toContain("are on this page");
    expect(agentsCard.querySelector("a[href='/identity/agents']")?.textContent).toContain("All agents");
    expect(agentsCard.querySelector(".idn-sub")?.textContent).toBe(`${suspended(12)} suspended`);
  });

  it("calls agentRestSentence with agents.length", () => {
    const src = readFileSync(join(__dirname, "../app/identity/overview.tsx"), "utf8");
    expect(src).toMatch(/agentRestSentence\(agents\.length\)/);
  });
});
