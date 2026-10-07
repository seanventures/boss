// @vitest-environment happy-dom
/**
 * IDENTITY › ACCESS REQUESTS (round 2 follow-up 1). A project Admin holds role_binding:assign at a
 * project and nothing at the org, so People (org user:read) stays closed to them; they answer
 * their project's requests on /identity/requests, which opens for that permission held at the org
 * OR any project. The request's audit event links there.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { describe, expect, it, vi } from "vitest";

import { RequestsScreen } from "../app/identity/requests/requests-screen";
import { snapshotFrom, type MePermissionsResponse } from "../lib/identity/contract";
import { candidateProjects, heldSomewhere, MAX_PROJECTS } from "../lib/identity/held-anywhere";
import { IDENTITY_TABS, pageFor } from "../lib/identity/pages";
import { ORG_QUEUE, requestQueues } from "../lib/identity/request-queues";
import { eventHref, REQUESTS_PATH } from "../lib/identity/words";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }), useSearchParams: () => new URLSearchParams(), usePathname: () => "/" }));

const WEB = process.cwd();
const snap = (permissions: string[]) => snapshotFrom({ epoch: 1, scope: "org", permissions, step_up_fresh_until: null, catalogue_fingerprint: "x" } as MePermissionsResponse);

describe("the page and its tab", () => {
  it("is declared for role_binding:assign at any scope, as an Identity tab; People stays org user:read", () => {
    expect(pageFor("/identity/requests")?.access).toEqual({ anyScope: "role_binding:assign" });
    expect(IDENTITY_TABS.map((t) => t.path)).toContain("/identity/requests");
    expect(pageFor("/identity/people")?.access).toEqual({ permission: "user:read" });
  });
  it("the shell shows the tab, and opens the page, only when it is held somewhere", () => {
    const shell = readFileSync(join(WEB, "components/identity/route-shell.tsx"), "utf8");
    expect(shell).toMatch(/anyScope\.set\(p\.access\.anyScope, heldSomewhere\(await heldAnywhere\(p\.access\.anyScope\)\)\)/);
    expect(shell).toMatch(/if \(page && "anyScope" in page\.access\) return heldSomewhere\(await heldAnywhere\(page\.access\.anyScope\)\);/);
    expect(heldSomewhere({ org: false, projects: [] })).toBe(false);
    expect(heldSomewhere({ org: false, projects: ["p1"] })).toBe(true);
    expect(heldSomewhere(null)).toBe(true); // identity unreadable: the API decides
  });
  it("the page loads only after the same guard", () => {
    const page = readFileSync(join(WEB, "app/identity/requests/page.tsx"), "utf8");
    expect(page).toMatch(/export default async function RequestsPage\(\) \{\s*if \(!\(await pageAllowed\("\/identity\/requests"\)\)\) return null;/);
  });
});

describe("which queues it reads", () => {
  it("an org admin reads the org's list", () => {
    expect(requestQueues(snap(["role_binding:read", "role_binding:assign"]), { org: true, projects: [] }, ["p1"])).toEqual([ORG_QUEUE]);
  });
  it("a project Admin reads each project where they assign roles, and nothing else", () => {
    expect(requestQueues(snap(["project:read"]), { org: false, projects: ["p1", "p2"] }, ["p1", "p2", "p3"]).map((q) => q.path)).toEqual([
      "/v1/identity/projects/p1/access-requests?status=open", "/v1/identity/projects/p2/access-requests?status=open"]);
  });
  it("org-wide assign without org read: every candidate project's queue", () => {
    expect(requestQueues(snap(["role_binding:assign"]), { org: true, projects: [] }, ["p1", "p2"]).map((q) => q.projectId)).toEqual(["p1", "p2"]);
  });
  it("asks about the active project first, each once, and at most MAX_PROJECTS", () => {
    expect(candidateProjects("p1", ["p2", "p1", "p3"])).toEqual(["p1", "p2", "p3"]);
    expect(candidateProjects("a", Array.from({ length: 60 }, (_, i) => `p${i}`))).toHaveLength(MAX_PROJECTS);
  });
});

describe("the screen", () => {
  it("shows each project's queue, and says when nothing waits", async () => {
    const host = document.createElement("div");
    document.body.appendChild(host);
    const root = createRoot(host);
    const req = { id: "acr_1", requester_principal_id: "usr_l", requester_name: "Lena", permission_key: "deployment:production",
      project_id: "p1", reason: null, status: "open", created_at: new Date().toISOString() };
    await act(async () => root.render(<RequestsScreen roles={[]} queues={[{ project: "p1", requests: [req], failure: null }]} />));
    expect(host.querySelector("[data-testid='access-requests']")?.textContent).toContain("Lena");
    expect(host.textContent).toContain("1 waiting");
    await act(async () => root.render(<RequestsScreen roles={[]} queues={[{ project: "p1", requests: [], failure: null }]} />));
    expect(host.querySelector("[data-testid='requests-empty']")?.textContent).toContain("Nothing waiting");
    act(() => root.unmount());
    host.remove();
  });
});

describe("the request flow points there", () => {
  it("a request's security event opens Access requests; others open in the audit log", () => {
    expect(eventHref({ id: "ev_1", event_type: "access.requested" })).toBe(REQUESTS_PATH);
    expect(eventHref({ id: "ev_2", event_type: "user.invited" })).toBe("/identity/audit?event=ev_2");
    expect(readFileSync(join(WEB, "app/identity/overview.tsx"), "utf8")).toMatch(/href=\{eventHref\(e\)\}/);
    expect(readFileSync(join(WEB, "app/identity/audit/audit-screen.tsx"), "utf8")).toMatch(/e\.event_type === "access\.requested"[\s\S]{0,80}href=\{REQUESTS_PATH\}/);
    expect(readFileSync(join(WEB, "components/identity/access-denied.tsx"), "utf8")).toContain("Access requests");
  });
});
