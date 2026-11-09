// @vitest-environment happy-dom
/**
 * BOSSAPP-930 / 932 — a deploy you can watch, and an address that is live only when it is.
 *
 * From the laptop's Hello World (2026-11-08): the header said "hello world rest api ·
 * 2.29.2.202:8007" and the Deployments card linked it as live, "Unknown", while the only deploy
 * there had failed; nothing showed the deploy's progress while it ran. Rendered on the real
 * ProjectPage with the engine's answers (deploy_truth): a first deploy running shows its seven
 * phases live with times and a log behind a press, no live link, and the header's
 * "Deploying · 4/7"; a failed one says why, with Fix (to the project's agent) and Roll back.
 */
import React, { act } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(window.location.search),
  usePathname: () => "/",
}));

/** What the captain drawer sent to the project's agent (Fix hands the failure over). */
const sent: string[] = [];
vi.mock("../app/actions", async (original) => ({
  ...(await original<Record<string, unknown>>()),
  askBossAction: vi.fn(async (_prev: unknown, fd: FormData) => {
    sent.push(String(fd.get("message") ?? ""));
    return { ok: true, data: { thread_id: "athr_1", messages: [{ role: "user", content: String(fd.get("message")) },
      { role: "assistant", content: "On it." }], proposed_actions: [], tool_calls: [], next_steps: [] } };
  }),
}));

import { ProjectPage } from "../app/projects/project-page";
import { elapsedWords } from "../app/projects/deploy-live";
import { FX_DEPLOYMENTS, FX_HEALTH, FX_KNOWLEDGE, FX_ONBOARDING, FX_OVERVIEW, FX_SETTINGS, FX_SUMMARY, FX_WORK } from "../lib/project-experience-fixtures";
import { mountAs, stubFetch, tid, unmount, type Call } from "./_helpers/multirepo-fixtures";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const P = "hello-world";
const settle = async (n = 8) => { for (let i = 0; i < n; i++) await act(async () => { await new Promise((r) => setTimeout(r, 0)); }); };
const press = async (el: Element | null) => { if (!el) throw new Error("nothing to press"); await act(async () => { (el as HTMLElement).click(); }); await settle(); };

const phase = (key: string, label: string, status: string, elapsed: number | null, log = "") =>
  ({ key, label, status, started_at: elapsed === null ? "" : "2026-11-09T00:39:45+00:00", ended_at: "", elapsed_s: elapsed, log });
const RUNNING = { index: 4, total: 7, current: "switch", failed_phase: "", phases: [
  phase("prepare", "Prepare", "done", 3, "prepared /opt/boss-apps/hello-api"), phase("upload", "Upload", "done", 6),
  phase("environment", "Environment", "done", 2), phase("switch", "Switch", "running", 12, "switching release"),
  phase("install", "Install", "pending", null), phase("health", "Health", "pending", null), phase("smoke", "Smoke", "pending", null)] };
const FAILED = { index: 6, total: 7, current: "health", failed_phase: "health", phases: [
  phase("prepare", "Prepare", "done", 3), phase("upload", "Upload", "done", 6), phase("environment", "Environment", "done", 2),
  phase("switch", "Switch", "done", 4), phase("install", "Install", "done", 2, "bash: line 1: cd: hello-api: No such file or directory"),
  phase("health", "Health", "failed", 606, "BOSS_UNHEALTHY 000"), phase("smoke", "Smoke", "pending", null)] };
const REASON = "The app did not answer as expected at http://2.29.2.202:8007 within 600s.";

function env(state: "deploying" | "failed") {
  return { ...FX_DEPLOYMENTS.environments[0], key: "staging", label: "Staging", app: "Hello World REST API", target_id: "deptgt_362",
           url: "", address: "http://2.29.2.202:8007", health: "unknown", uptime_pct: null, p95_ms: null, hours: [], last_deploy: null,
           deploy: { state, word: state === "failed" ? "Last deploy failed" : "Setting up · 4/7", live: false,
                     reason: state === "failed" ? REASON : "",
                     attempt: { id: state === "failed" ? "sdep_f86f" : "sdep_run", kind: "stack", status: state === "failed" ? "failed" : "running", commit: "a0cb065", at: "2026-11-09T00:39:44+00:00" },
                     progress: state === "failed" ? FAILED : RUNNING } };
}

function engine(state: "deploying" | "failed", opts: { rollback?: boolean } = {}) {
  const overview = { ...FX_OVERVIEW, project: { ...FX_OVERVIEW.project, id: P, name: "Hello World", live_url: "",
    deploy: { state, word: state === "failed" ? "Last deploy failed" : "Setting up · 4/7", target_id: "deptgt_362", app: "Hello World REST API",
              environment: "staging", reason: state === "failed" ? REASON : "", index: state === "failed" ? 6 : 4, total: 7,
              deployment_id: state === "failed" ? "sdep_f86f" : "sdep_run" } } };
  const deployments = { ...FX_DEPLOYMENTS, environments: [env(state)], history: [] };
  const detail = {
    row: { ref: `stack:${state === "failed" ? "sdep_f86f" : "sdep_run"}`, kind: "stack", state: state === "failed" ? "failed" : "running",
           can: { rollback: !!opts.rollback } },
    steps: [], failure: state === "failed" ? { line: "Health check failed", error: REASON } : null,
    live: state !== "failed", progress: state === "failed" ? FAILED : RUNNING,
  };
  return stubFetch((c: Call) => {
    if (c.method !== "GET") return { body: { ok: true, ref: "stack:sdep_rb" } };
    if (c.path === "/v1/projects/summary") return { body: FX_SUMMARY };
    if (/^\/v1\/infra\/deploys\/stack(%3A|:)/.test(c.path)) return { body: detail };
    const m = c.path.match(/^\/v1\/projects\/([^/]+)\/(overview|work|knowledge|deployments|onboarding|settings|health|captain)$/);
    if (m) {
      const table: Record<string, unknown> = { overview, work: FX_WORK, knowledge: FX_KNOWLEDGE, deployments,
        onboarding: { ...FX_ONBOARDING, state: "ready" }, settings: FX_SETTINGS, health: FX_HEALTH };
      return { body: table[m[2]] };
    }
    return undefined;
  });
}

async function openDeployments() {
  window.history.replaceState(null, "", `/?org=org_1&project_id=${P}&area=project&tab=deployments`);
  await mountAs(<ProjectPage projectId={P} actor="ceo" orgId="org_1" />, "owner");
  await settle(10);
}

beforeEach(() => { sent.length = 0; window.history.replaceState(null, "", "/"); });
afterEach(() => { unmount(); });

describe("a deploy you can watch (BOSSAPP-932)", () => {
  it("a running first deploy shows its seven phases live, no live link, and the header says Deploying · 4/7", async () => {
    const calls = engine("deploying");
    await openDeployments();
    const card = tid("px-env-staging")!;
    expect(card.querySelector("a.px-env-url")).toBeNull();
    expect(tid("px-env-address-staging")!.textContent).toContain("Setting up · will answer at 2.29.2.202:8007");
    expect(tid("px-env-state-staging")!.textContent).toBe("Setting up · 4/7");
    expect(card.textContent).not.toContain("Unknown");
    expect(tid("px-env-live-count-staging")!.textContent).toBe("Step 4 of 7");
    for (const k of ["prepare", "upload", "environment", "switch", "install", "health", "smoke"]) {
      expect(tid(`px-env-phase-staging-${k}`)).not.toBeNull();
    }
    expect(tid("px-env-phase-staging-switch")!.getAttribute("data-s")).toBe("running");
    expect(tid("px-env-phase-time-staging-switch")!.textContent).toBe("12s");
    // The log of a phase opens in place.
    expect(tid("px-env-log-staging-switch")).toBeNull();
    await press(tid("px-env-phase-staging-switch")!.querySelector("button"));
    expect(tid("px-env-log-staging-switch")!.textContent).toContain("switching release");
    // One stream: Infra's deploy detail for this stack deployment.
    expect(calls.some((c) => c.method === "GET" && /^\/v1\/infra\/deploys\/stack(%3A|:)sdep_run$/.test(c.path))).toBe(true);
    // The header chip.
    const chip = tid("px-head-deploy")!;
    expect(chip.textContent).toContain("Deploying · 4/7");
    expect(tid("px-head-live")).toBeNull();
    expect(tid("px-env-fail-staging")).toBeNull();
  });

  it("a failed deploy says why, which phase, and offers Fix (to the agent) and Roll back", async () => {
    const calls = engine("failed", { rollback: true });
    await openDeployments();
    expect(tid("px-env-state-staging")!.textContent).toBe("Last deploy failed");
    expect(tid("px-env-address-staging")!.textContent).toContain("Not live");
    expect(tid("px-env-live-count-staging")!.textContent).toBe("Failed at health");
    expect(tid("px-env-phase-staging-health")!.getAttribute("data-s")).toBe("failed");
    expect(tid("px-env-phase-time-staging-health")!.textContent).toBe("10m 06s");
    expect(tid("px-env-fail-staging")!.textContent).toContain(REASON);
    expect(tid("px-head-deploy")!.textContent).toContain("Last deploy failed");

    await press(tid("px-env-fix-staging"));
    await settle(20);
    expect(new URLSearchParams(window.location.search).get("drawer")).toBe("captain");
    expect(sent).toHaveLength(1);
    expect(sent[0]).toContain("failed at health");
    expect(sent[0]).toContain(REASON);

    await press(tid("px-env-rollback-staging"));
    await settle();
    expect(calls.find((c) => c.method === "POST" && /^\/v1\/infra\/deploys\/stack(%3A|:)sdep_f86f\/rollback$/.test(c.path))).toBeTruthy();
  });

  it("Roll back is offered only when the engine says there is a release to go back to", async () => {
    engine("failed", { rollback: false });
    await openDeployments();
    expect(tid("px-env-fix-staging")).not.toBeNull();
    expect(tid("px-env-rollback-staging")).toBeNull();
  });

  it("says elapsed time in one short token", () => {
    expect(elapsedWords(0)).toBe("0s");
    expect(elapsedWords(59)).toBe("59s");
    expect(elapsedWords(606)).toBe("10m 06s");
    expect(elapsedWords(null)).toBe("");
  });
});
