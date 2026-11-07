// @vitest-environment happy-dom
/**
 * Onboarding's "Use for staging" is done IN PLACE, and Infra's App target sheet asks for staging
 * with a free port and says refusals in words (2026-11-07, helpdesk from paramg/helpdesk-ai-v3).
 *
 * The button used to open Infra's generic App target sheet: environment "production", no port,
 * and the create's refusal printed as raw JSON — {"headline":"No such host in this project.",
 * "action":"Add the VM in Infra first, then create the target on it."} — for the shared host
 * onboarding itself had offered. Now: one confirm sheet on the onboarding page (What · Where ·
 * Port · Manifest) whose press POSTs the action's route; the page refreshes and never navigates.
 */
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(window.location.search),
  usePathname: () => "/",
}));

import { AppTargetSheet } from "../app/infra/target-sheet";
import { OnboardingView } from "../app/projects/onboarding";
import { apiSentence } from "../app/infra/infra-actions";
import { detailText } from "../lib/api-error";
import { errorSentence } from "../lib/identity/api";
import { parseOnboarding } from "../lib/project-experience";
import type { Rack } from "../lib/infra-view";
import REAL from "./_helpers/onboarding-helpdesk-real.json";
import { click, mountAs, settle, stubFetch, tid, unmount, type Call } from "./_helpers/multirepo-fixtures";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const P = "helpdesk";
const HOST = "dh_9c480ba29fdb";
const REFUSAL = { headline: "No such host in this project or its organisation.", action: "Add the VM in Infra first, then create the target on it." };
const ACTION = { label: "Use for staging", do: "staging_target", host_id: HOST, post: `/v1/projects/${P}/onboarding/deploy-target`,
                 href: `?project_id=${P}&area=infra&add=target&host=${HOST}&env=staging` };
const PLAN = { project: "HelpDesk", app_name: "helpdesk-ai-v3", repository: "paramg/helpdesk-ai-v3", environment: "staging",
               host_id: HOST, host: "edge", address: "2.29.2.202", shared: true, port: 8004, manifest_path: "boss.deploy.yaml" };

const withAction = (r: { key: string }) => (r.key === "deploy_target" ? { ...r, owner: "you", state: "todo", action: ACTION } : r);
const NEEDS_TARGET = parseOnboarding({
  ...REAL, ready: REAL.ready.map(withAction), checklist: REAL.checklist.map(withAction), only_you: REAL.only_you.map(withAction),
});

beforeEach(() => { window.history.replaceState(null, "", `/?project_id=${P}&area=project&tab=onboarding`); });
afterEach(() => { unmount(); });

describe("Use for staging, on the onboarding page", () => {
  it("opens one confirm sheet with What · Where · Port · Manifest and creates the target in place", async () => {
    const calls = stubFetch((c: Call) => {
      if (c.method === "GET" && c.path === `/v1/projects/${P}/onboarding/deploy-target`) return { body: PLAN };
      if (c.method === "POST" && c.path === ACTION.post) return { body: { target_id: "deptgt_1", onboarding: {} } };
      return undefined;
    });
    const refresh = vi.fn();
    await mountAs(<OnboardingView projectId={P} ob={NEEDS_TARGET} onRefresh={refresh} />, "owner");
    const before = window.location.href;
    const button = tid("px-ob-act-deploy_target")!;
    expect(button.tagName).toBe("BUTTON");             // not a link to Infra
    await click(button);
    await settle();
    expect(tid("px-ob-staging")).not.toBeNull();
    expect(tid("px-ob-staging-what")!.textContent).toBe("Staging target for HelpDesk");
    expect(tid("px-ob-staging-where")!.textContent).toBe("2.29.2.202 · shared host");
    expect(tid("px-ob-staging-port")!.textContent).toBe("8004");
    expect(tid("px-ob-staging-manifest")!.textContent).toBe("boss.deploy.yaml");
    expect(calls.find((c) => c.method === "GET" && c.path.endsWith("/onboarding/deploy-target"))!.search).toBe(`?host_id=${HOST}`);

    await click(tid("px-ob-staging-go"));
    await settle();
    const post = calls.find((c) => c.method === "POST")!;
    expect(post.path).toBe(ACTION.post);
    expect(post.body).toEqual({ host_id: HOST, port: 8004 });
    expect(refresh).toHaveBeenCalled();
    expect(tid("px-ob-staging")).toBeNull();           // the sheet closed
    expect(window.location.href).toBe(before);         // and nothing navigated to Infra
  });

  it("the same action from Only you can opens the same sheet", async () => {
    stubFetch((c: Call) => (c.method === "GET" && c.path.endsWith("/onboarding/deploy-target") ? { body: PLAN } : undefined));
    await mountAs(<OnboardingView projectId={P} ob={NEEDS_TARGET} />, "owner");
    await click(tid("px-ob-you-act-deploy_target"));
    await settle();
    expect(tid("px-ob-staging-port")!.textContent).toBe("8004");
  });

  it("a refusal shows as words inside the sheet, never as JSON", async () => {
    stubFetch((c: Call) => {
      if (c.method === "GET" && c.path.endsWith("/onboarding/deploy-target")) return { body: PLAN };
      if (c.method === "POST") return { status: 422, body: { detail: { ...REFUSAL, message: `${REFUSAL.headline} ${REFUSAL.action}` } } };
      return undefined;
    });
    await mountAs(<OnboardingView projectId={P} ob={NEEDS_TARGET} />, "owner");
    await click(tid("px-ob-act-deploy_target"));
    await settle();
    await click(tid("px-ob-staging-go"));
    await settle();
    const error = tid("px-ob-staging-error")!.textContent!;
    expect(error).toContain(REFUSAL.headline);
    expect(error).not.toContain("{");
  });
});

describe("Infra's App target sheet", () => {
  const racks = [{ id: HOST, name: "edge", address: "2.29.2.202", state: { tone: "green" } }] as unknown as Rack[];
  const engine = (create: (c: Call) => { status?: number; body: unknown }) => stubFetch((c: Call) => {
    if (c.path === `/v1/projects/${P}/repositories`) return { body: { repositories: [{ full_name: "paramg/helpdesk-ai-v3" }], suggested: "paramg/helpdesk-ai-v3" } };
    if (c.path === `/v1/projects/${P}/hosts/${HOST}/free-port`) return { body: { host_id: HOST, address: "2.29.2.202", port: 8004 } };
    if (c.method === "POST" && c.path === "/v1/deployments/stack/targets") return create(c);
    return undefined;
  });

  it("defaults to staging, suggests the first free port and sends it", async () => {
    const calls = engine(() => ({ status: 201, body: { id: "deptgt_2" } }));
    const created = vi.fn();
    await mountAs(<AppTargetSheet projectId={P} actor="ceo" racks={racks} hostId={HOST} onClose={() => {}} onCreated={created} />, "owner");
    await settle();
    expect((tid("inf-target-env") as HTMLInputElement).value).toBe("staging");
    expect((tid("inf-target-port") as HTMLInputElement).value).toBe("8004");
    await click(tid("inf-target-create"));
    await settle();
    const body = calls.find((c) => c.method === "POST")!.body as Record<string, unknown>;
    expect(body).toMatchObject({ host_id: HOST, environment: "staging", port: 8004, manifest_path: "boss.deploy.yaml" });
    expect(created).toHaveBeenCalledWith("deptgt_2");
  });

  it("takes the environment the opener asks for", async () => {
    engine(() => ({ status: 201, body: {} }));
    await mountAs(<AppTargetSheet projectId={P} actor="ceo" racks={racks} hostId={HOST} environment="production" onClose={() => {}} onCreated={() => {}} />, "owner");
    expect((tid("inf-target-env") as HTMLInputElement).value).toBe("production");
  });

  it("says a {headline, action} refusal in words, never as raw JSON", async () => {
    engine(() => ({ status: 404, body: { detail: REFUSAL } }));
    await mountAs(<AppTargetSheet projectId={P} actor="ceo" racks={racks} hostId={HOST} onClose={() => {}} onCreated={() => {}} />, "owner");
    await settle();
    await click(tid("inf-target-create"));
    await settle();
    const error = tid("inf-target-sheet-error")!.textContent!;
    expect(error).toContain(`${REFUSAL.headline} ${REFUSAL.action}`);
    expect(error).not.toMatch(/[{}"]/);
  });
});

describe("one reader for a refusal object", () => {
  it("reads {headline, action} as words in every reader", () => {
    expect(detailText(REFUSAL)).toBe(`${REFUSAL.headline} ${REFUSAL.action}`);
    expect(apiSentence({ detail: REFUSAL }, 404)).toBe(`${REFUSAL.headline} ${REFUSAL.action}`);
    expect(errorSentence(422, "error", undefined, { detail: REFUSAL })).toBe(`${REFUSAL.headline} ${REFUSAL.action}`);
  });
});
