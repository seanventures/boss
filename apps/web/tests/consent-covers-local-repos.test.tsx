// @vitest-environment happy-dom
/**
 * CONSENT COVERS REPOSITORIES ON THIS MACHINE, AND A 409 ALWAYS OPENS THE SHEET (BOSSAPP-1163).
 *
 * BOSS found this on a project with code "On this machine": Request changes on the merge decision
 * answered 409 consent_required and the console showed a dead end — no sheet — because the merge
 * card's form action described the refusal as a failure; the person's 30-day grant named no
 * repository, since the delegation sheet listed only GitHub ones. Here:
 *   - the 409's `local` block becomes the sheet's request, shown as "on this machine", and the
 *     grant it creates sends `local: {repos}` back;
 *   - a server action's 409 consent_required travels as `data.consent` (lib/server-action act);
 *   - EVERY ActionForm opens the consent sheet on it and sends the same submit once more;
 *   - the dock keeps a consent-refused offer standing (no repair turn, not spent);
 *   - "Let an agent act for me" lists a project's local repository, labelled, and grants it.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ActionForm } from "../app/client-forms";
import { AgentsForMe, grantBody } from "../app/account/agents/agents-for-me";
import { ConsentSheet } from "../components/identity/consent-sheet";
import type { ActionResult } from "../lib/action-result";
import { CONSENT_EVENT, consentFromBody, grantBodyFor, type ConsentRequest } from "../lib/identity/consent";
import { act as actionAct, consentOfThrown, refusalFrom } from "../lib/server-action";
import { codeOnly } from "./_helpers/source";
import { mountAs, settle, stubFetch, unmount, type Call } from "./_helpers/multirepo-fixtures";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }), useSearchParams: () => new URLSearchParams(), usePathname: () => "/" }));
vi.mock("next/cache", () => ({ revalidatePath: vi.fn() }));

afterEach(unmount);

const LOCAL = "local_prj_desk/desk";
/** Verbatim: identity/consent.py require_authority's 409 for a follow-up build on a local repository. */
const LOCAL_409 = {
  error: "consent_required", task: "Fix change #1: Add an SLA timer", task_run_id: "pending:Zk9Q",
  agents: [{ id: "agt_forge", name: "Software Engineer", role_key: "forge" }],
  permissions: ["code_task:read", "code_task:steer", "repository:read"], projects: ["prj_desk"],
  github: null, local: { repo: LOCAL, repos: [LOCAL], summary: "on this machine" },
  durations: [{ kind: "task", label: "For this build only", days: null }, { kind: "standing", label: "For 30 days", days: 30 }],
  max_days: 90,
};
const tid = (id: string) => document.querySelector(`[data-testid='${id}']`);
async function click(el: Element | null) { expect(el, "element to click").not.toBeNull(); await React.act(async () => { (el as HTMLElement).click(); }); await settle(); }

describe("the consent request names the repository on this machine", () => {
  it("reads `local` from the 409 and sends it back as the grant's local block", () => {
    const req = consentFromBody(LOCAL_409)!;
    expect(req.github).toBeNull();
    expect(req.local).toEqual({ repo: LOCAL, repos: [LOCAL], summary: "on this machine" });
    const standing = grantBodyFor(req, req.durations[1]);
    expect(standing).toEqual({ agents: ["agt_forge"], permissions: LOCAL_409.permissions, projects: ["prj_desk"],
      kind: "standing", days: 30, purpose: LOCAL_409.task, local: { repos: [LOCAL] } });
    expect(standing).not.toHaveProperty("github");
    // A body without `local` is exactly what it was.
    expect(consentFromBody({ ...LOCAL_409, local: null })!.local).toBeNull();
  });

  it("shows it on the sheet as on this machine, and the grant covers it", async () => {
    const calls = stubFetch((c: Call) => (c.method === "POST" && c.path === "/v1/identity/me/delegations" ? { status: 201, body: { id: "dlg_1" } } : undefined));
    const decided: Array<string | null> = [];
    const req: ConsentRequest = { ...consentFromBody(LOCAL_409)!, onDecided: (id) => decided.push(id) };
    await mountAs(<ConsentSheet request={req} onClose={() => undefined} />, "owner");
    expect(tid("consent-local")?.textContent).toBe(`${LOCAL}, on this machine`);
    await click(tid("consent-standing"));
    expect(calls[0].body).toMatchObject({ kind: "standing", local: { repos: [LOCAL] } });
    expect(decided).toEqual(["dlg_1"]);
  });
});

describe("the Easy Mode sheet on a local repository (stranger test r3)", () => {
  const LONG = "local_build-a-python-app-named-csv-quality-des-29bc4174/build-a-python-app-named-csv-quality-desk-with-a-command";

  it("keeps a long repository chip inside the sheet, with the whole name on hover", async () => {
    stubFetch(() => undefined);
    const req = consentFromBody({ ...LOCAL_409, local: { repo: LONG, repos: [LONG], summary: "on this machine" } })!;
    await mountAs(<ConsentSheet request={req} onClose={() => undefined} />, "owner");
    const chip = tid("consent-local") as HTMLElement;
    expect(chip.classList.contains("idn-chip-repo")).toBe(true);
    expect(chip.getAttribute("title")).toBe(`${LONG}, on this machine`);
    expect(chip.querySelector(".idn-chip-text")?.textContent).toBe(`${LONG}, on this machine`);
  });

  it("asks for no GitHub ability when the build is on this machine", async () => {
    stubFetch(() => undefined);
    await mountAs(<ConsentSheet request={consentFromBody(LOCAL_409)!} onClose={() => undefined} />, "owner");
    const need = document.querySelector(".idn-need")!.textContent ?? "";
    expect(need).not.toMatch(/GitHub/);
    expect(tid("consent-github")).toBeNull();
  });

  it("the chip ellipsizes and never grows past its row", () => {
    const css = readFileSync(join(__dirname, "../app/identity/identity.css"), "utf8");
    expect(css).toMatch(/\.idn-chip-repo \{[^}]*max-width: 100%[^}]*min-width: 0/);
    expect(css).toMatch(/\.idn-chip-repo \.idn-chip-text \{[^}]*text-overflow: ellipsis/);
  });
});

describe("a 409 consent_required from a server action is a question, not a failure", () => {
  it("act() returns the consent request as data, flat or under FastAPI's detail", async () => {
    const thrown = Object.assign(new Error("consent_required"), { status: 409, body: LOCAL_409 });
    const r = await actionAct(async () => { throw thrown; });
    expect(r.ok).toBe(false);
    expect(r.error).toBe("Needs your consent");
    expect(consentFromBody(r.data?.consent)?.local?.repos).toEqual([LOCAL]);
    const wrapped = Object.assign(new Error("x"), { status: 409, body: { detail: LOCAL_409 } });
    expect(consentOfThrown(wrapped)).toEqual(LOCAL_409);
    expect(consentOfThrown(Object.assign(new Error("x"), { status: 409, body: { error: "not_pending" } }))).toBeNull();
    expect(consentOfThrown(Object.assign(new Error("x"), { status: 403, body: LOCAL_409 }))).toBeNull();
  });

  it("apiFetch's refusal keeps the body, so an action that fetches gets the same answer", async () => {
    const refusal = await refusalFrom(new Response(JSON.stringify(LOCAL_409), { status: 409, headers: { "content-type": "application/json" } }));
    expect(refusal.status).toBe(409);
    expect(consentOfThrown(refusal)).toEqual(LOCAL_409);
  });

  it("every ActionForm opens the consent sheet on it and sends the same submit once more", async () => {
    stubFetch(() => undefined);
    const answers: ActionResult[] = [
      { ok: false, error: "Needs your consent", data: { consent: LOCAL_409 } },
      { ok: true, message: "Sent back with your note." },
    ];
    const seen: FormData[] = [];
    const action = vi.fn(async (fd: FormData) => { seen.push(fd); return answers.shift()!; });
    const asked: ConsentRequest[] = [];
    window.addEventListener(CONSENT_EVENT, (e) => {
      const detail = (e as CustomEvent<ConsentRequest>).detail;
      asked.push(detail);
      detail.onDecided?.("dlg_1");                         // the person grants
    }, { once: true });
    const host = await mountAs(
      <ActionForm permission="decision:approve" action={action} data-testid="revise-form">
        <input type="hidden" name="decision_id" value="dec_1" />
        <input type="hidden" name="action" value="revise" />
        <button type="submit">Request changes</button>
      </ActionForm>, "owner");
    const form = host.querySelector("form") as HTMLFormElement;
    await React.act(async () => { form.requestSubmit(); });
    await settle(); await settle();
    expect(asked).toHaveLength(1);
    expect(asked[0].local?.repos).toEqual([LOCAL]);
    expect(action).toHaveBeenCalledTimes(2);
    expect(seen[1].get("decision_id")).toBe("dec_1");
    expect(tid("action-form-done")?.textContent).toContain("Sent back with your note.");
    expect(tid("action-form-error")).toBeNull();
  });

  it("cancelled, the refusal stands as written and nothing is sent again", async () => {
    stubFetch(() => undefined);
    const action = vi.fn(async () => ({ ok: false, error: "Needs your consent", data: { consent: LOCAL_409 } } as ActionResult));
    window.addEventListener(CONSENT_EVENT, (e) => (e as CustomEvent<ConsentRequest>).detail.onDecided?.(null), { once: true });
    const host = await mountAs(<ActionForm permission="decision:approve" action={action}><button type="submit">Go</button></ActionForm>, "owner");
    await React.act(async () => { (host.querySelector("form") as HTMLFormElement).requestSubmit(); });
    await settle(); await settle();
    expect(action).toHaveBeenCalledTimes(1);
    expect(tid("action-form-error")?.textContent).toBe("Needs your consent");
  });
  // The dock's offer that needs consent: rendered in tests/the-dock-keeps-an-offer-that-needs-consent.test.tsx.
});

describe("Let an agent act for me lists a project's repository on this machine", () => {
  const PROJECTS = { items: [{ id: "prj_desk", name: "Desk" }] };
  const REPOS = { repositories: [
    { full_name: LOCAL, role: "primary", host: "local", connection_label: "On this machine" },
    { full_name: "acme/desk-docs", role: "secondary", host: "github" },
  ] };

  it("labels it, ticks it, and grants it as local beside the GitHub one", async () => {
    const calls = stubFetch((c: Call) => {
      if (c.path === "/v1/projects") return { body: PROJECTS };
      if (c.path === "/v1/projects/prj_desk/repositories") return { body: REPOS };
      if (c.path === "/v1/identity/me/delegations" && c.method === "GET") return { body: { delegations: [] } };
      if (c.path === "/v1/identity/me/delegations" && c.method === "POST") return { status: 201, body: { id: "dlg_1" } };
      if (c.path === "/v1/identity/me/permissions") return { body: { epoch: 1, scope: "org", permissions: [], step_up_fresh_until: null, catalogue_fingerprint: "x" } };
      if (c.path === "/v1/auth/github") return { body: { configured: false } };
      return undefined;
    });
    await mountAs(<AgentsForMe delegations={[]} agents={[{ id: "agt_forge", name: "Software Engineer" }]} connections={[]} untilRevokedAllowed={false} openNew />, "owner");
    await settle();
    const local = document.querySelector(`[data-testid='grant-repos'] label[data-local]`);
    expect(local, "the local repository is listed").not.toBeNull();
    expect(local!.textContent).toContain(LOCAL);
    expect(local!.textContent).toContain("on this machine");
    expect((local!.querySelector("input") as HTMLInputElement).checked).toBe(true);
    expect(document.querySelector(`[data-testid='grant-repos'] label:not([data-local])`)!.textContent).not.toContain("on this machine");
    await click(document.querySelector("button.idn-agent-pick"));
    await click(tid("grant-submit"));
    const post = calls.find((c) => c.method === "POST" && c.path === "/v1/identity/me/delegations");
    expect(post?.body).toMatchObject({ projects: ["prj_desk"], kind: "standing",
      github: { repos: ["acme/desk-docs"], contents: "write", pull_requests: "write" }, local: { repos: [LOCAL] } });
  });

  it("shows the local repository on a grant that covers it", async () => {
    stubFetch(() => undefined);
    const d = {
      id: "dlg_l", kind: "standing" as const, user: { id: "usr_x", name: "Param", hue: 4 }, agents: [{ id: "agt_forge", name: "Software Engineer" }],
      label: "Build and steer code in Desk", permissions: ["code_task:read" as const], projects: ["prj_desk"], github: null,
      local: { repos: [LOCAL] }, subdelegation: { allowed: false, to: [] }, max_depth: 3, created_at: "2026-11-14T10:00:00Z",
      expires_at: "2026-12-14T10:00:00Z", purpose: null, status: "active" as const, last_used_at: null, last_used_run: null,
    };
    await mountAs(<AgentsForMe delegations={[d]} agents={null} connections={[]} untilRevokedAllowed={false} openNew={false} />, "owner");
    expect(tid("grant-local-chip")?.textContent).toBe(` ${LOCAL}, on this machine`);
  });

  it("grantBody sends local only for an ability that pushes, apart from github", () => {
    const b = { agents: ["a"], permissions: ["preview:read" as const], projects: ["p"], repos: ["o/r"], localRepos: [LOCAL], days: 30, purpose: "x" };
    expect(grantBody({ ...b, pushes: false })).not.toHaveProperty("local");
    expect(grantBody({ ...b, pushes: true })).toMatchObject({ github: { repos: ["o/r"] }, local: { repos: [LOCAL] } });
    expect(grantBody({ ...b, repos: [], pushes: true })).not.toHaveProperty("github");
  });
});
