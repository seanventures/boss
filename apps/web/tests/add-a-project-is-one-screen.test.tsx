// @vitest-environment happy-dom
/**
 * ADD A PROJECT v2 — ONE SCREEN (new-project-v2 wireframes, approved 2026-11-07), on the API's
 * real shapes (apps/api/app/routers/add_project.py):
 *
 *   the one input   tells an idea from a repo (owner/repo, a GitHub URL) or a folder (/path); the
 *                   kind pill forces another reading; repos and host folders in one result list,
 *                   ↵ picks the top match
 *   understood      Helm's rewrite as a brief, its additions marked; Looks right · Fix it (every
 *                   line) · Rewrite with a hint; no model keeps the words and lets you continue
 *   what BOSS does  six boxes from real data; each opens its popover in place
 *   Create & start  ONE POST /v1/add-project with every pick; a readiness refusal opens the same
 *                   checklist inline and the button becomes Start anyway; a kept project is reused
 *   Moved           Workflows › New build points at Add a project and Project › New work
 */
import React, { act } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AddProjectSheet } from "../app/start-project/add-project";
import { MovedNewBuild } from "../app/projects/moved";
import { resetCatalogue } from "../components/v2/templates/catalogue";
import { detect, estimateWords, hostPort, nameFromTitle, type Preview, type Understanding, type Workflow } from "../lib/add-project";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { $$, click, mountAs, registryFixture, settle, stubFetch, tid, type, unmount, type Call } from "./_helpers/multirepo-fixtures";

afterEach(() => { unmount(); window.history.replaceState(null, "", "/"); });
beforeEach(() => { resetCatalogue(); });

const WORDS = "A helpdesk for our support team.\nCustomers email support@ and it becomes a ticket. AI drafts the reply; staff sign in with Google and approve.\nNo chat or phone for now.";

const step = (id: string, label: string, you = false, short = label.toLowerCase()) => ({ id, label, short, you, agent: you ? null : { role_key: "pm", name: "Juno" } });
const STANDARD: Workflow = {
  key: "standard_product_delivery", name: "Standard Product Delivery", short: "Standard", detail: "", work_class: "project.new", path: true,
  steps: 5, step_list: [step("research", "Research"), step("prd", "PRD approval", true, "PRD"), step("build", "Build"), step("pr", "Pull request", true, "PR"), step("deploy", "Staging")],
  approvals: 2, approve_words: ["PRD", "PR"], input_schema: null,
  estimate: { minutes: 35, minutes_basis: "history", minutes_runs: 7, usd: 5.2, usd_runs: 7 },
};
const SMALL: Workflow = { ...STANDARD, key: "minor_feature", name: "Minor Feature", short: "Small", steps: 3,
  step_list: [step("plan", "Plan"), step("build", "Build"), step("pr", "Pull request", true)], approvals: 1, approve_words: ["Pull request"], estimate: null };
const PREVIEW: Preview = {
  mode: "idea", workflows: [SMALL, STANDARD], default_workflow: "standard_product_delivery",
  captain: { role_key: "engineering_captain", name: "Helm", role: "Engineering Captain",
             squad: [{ role_key: "pm", name: "Juno" }, { role_key: "software_engineer", name: "Forge" }],
             candidates: [{ agent_id: "a1", role_key: "engineering_captain", name: "Helm", role: "Engineering Captain", captain_capable: true, recommended: true },
                          { agent_id: "a2", role_key: "chief_of_staff", name: "Marshal", role: "Chief of Staff" },
                          { agent_id: "a3", role_key: "qa", name: "Vera", role: "QA Lead" }] },
  staging: [{ host_id: "dh_1", host: "boss-1", address: "203.0.113.9", port: 8005, suggested: true, shared: false },
            { host_id: "dh_2", host: "203.0.113.20", address: "203.0.113.20", port: 8006, suggested: false, shared: true }],
  readiness: [{ key: "model", label: "Model", ok: true, detail: "a model is connected", href: null },
              { key: "repository", label: "GitHub", ok: true, detail: "pushes to acme/web", href: null },
              { key: "budget", label: "Budget", ok: true, detail: "green", href: null },
              { key: "deployment_target", label: "Host", ok: false, detail: "no host", href: "/?area=infra&add=host" }],
  default_repository: "acme/apps", stacks: [],
};
const BRIEF: Understanding = {
  status: "ok",
  brief: { title: "HelpDesk AI: email support with approved AI replies", problem: "Replies are slow and uneven.", who_uses: "Support staff · customers by email only",
           must_haves: ["An email to support@ becomes a ticket", "Staff approve; nothing sends without approval"], out_of_scope: ["Live chat", "a customer portal"],
           done_when: "An email reaches the inbox as a ticket within a minute.", open_questions: [{ question: "Which mailbox?", options: ["Google Workspace", "Microsoft 365"], answer: null }],
           added: ["nothing sends without approval", "a customer portal"], pinned: [] },
  picks: { stack: { key: "react-vite+fastapi+postgres", because: "AI drafts" }, connectors: [{ key: "login-oauth", because: "sign in with Google" }], workflow: { key: "standard_product_delivery", because: "a new app" } },
};
const TEMPLATES = { templates: [
  { template_key: "login-oauth", name: "Login with Google", kind: "connector", status: "active", summary: "Sign in", stacks: ["python", "node"], secrets: [], egress: ["accounts.google.com"] },
  { template_key: "stripe-checkout", name: "Stripe Checkout", kind: "connector", status: "active", summary: "Payments", stacks: ["python", "node"],
    secrets: [{ name: "STRIPE_SECRET_KEY", purpose: "test key", test_default: "none" }] },
] };
const STACKS = { supported: { languages: [], adapters: [
  { id: "react-vite+fastapi+postgres", label: "React + Vite, FastAPI, PostgreSQL", support_level: "experimental", ready: true, languages: ["node", "python"],
    components: [{ role: "frontend", language: "node" }, { role: "backend", language: "python" }] },
] } };

function api(extra: (c: Call) => { status?: number; body: unknown } | undefined = () => undefined) {
  return stubFetch((c) => {
    const x = extra(c);
    if (x) return x;
    if (c.path === "/v1/github/repos") return { body: registryFixture() };
    if (c.path.endsWith("/host-folders/hosts")) return { body: { hosts: [{ id: "dh_1", name: "boss-1", address: "203.0.113.9", shared: false }] } };
    if (c.path.endsWith("/add-project/preview")) return { body: { ...PREVIEW, mode: new URLSearchParams(c.search).get("mode") } };
    if (c.path.endsWith("/add-project/understand")) return { body: BRIEF };
    if (c.path.endsWith("/add-project/connectors")) return { body: { status: "ok", picks: BRIEF.picks } };
    if (c.path === "/v1/templates") return { body: TEMPLATES };
    if (c.path === "/v1/stacks") return { body: STACKS };
    if (c.path.includes("/add-project/repo/")) return { body: {
      facts: { repo_id: "ghr_web", full_name: "acme/web", branch: "main", facts: { language: "TypeScript", framework: "Vite", build: "npm", tests: true }, files_total: 214, suggested_name: "Web", mapping: { state: "not_started", percent: 0, done: false } },
      what_it_is: "Storefront UI", parts: [{ path: "src/inbox", files: 44 }, { path: "api/tickets", files: 38 }], files_total: 214,
      health: [{ label: "86 test files", tone: "green" }, { label: "no staging yet", tone: "grey" }],
      onboarding_will: ["Map the code into the knowledge graph", "Run its build and tests; explain any failures", "List the business rules it finds", "Propose a first backlog for you to accept", "Set up staging"] } };
    return undefined;
  });
}

async function enter(el: HTMLElement | null) {
  await act(async () => { el!.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true })); });
  await settle();
}

describe("the one input tells what it is", () => {
  it("reads an idea, a repo (slug, URL, a bare name), a folder, and a note under a repo", () => {
    expect(detect(WORDS).kind).toBe("idea");
    expect(detect("acme/web")).toMatchObject({ kind: "repo", ref: "acme/web", query: "web" });
    expect(detect("https://github.com/acme/helpdesk-ai.git")).toMatchObject({ kind: "repo", ref: "acme/helpdesk-ai" });
    expect(detect("helpdesk")).toMatchObject({ kind: "repo", ref: "", query: "helpdesk" });
    expect(detect("/srv/apps/helpdesk")).toMatchObject({ kind: "folder", ref: "/srv/apps/helpdesk" });
    expect(detect("acme/web\nstart with the SLA bugs")).toMatchObject({ kind: "repo", note: "start with the SLA bugs" });
    expect(detect("A shift board")).toMatchObject({ kind: "idea" });
  });

  it("names the project from the title and says the estimate from history only", () => {
    expect(nameFromTitle("HelpDesk AI: email support with approved AI replies")).toBe("HelpDesk AI");
    expect(estimateWords(STANDARD, "idea")).toBe("≈ $5 · ≈ 35 min to PR");
    expect(estimateWords(SMALL, "idea")).toBeNull();
    expect(estimateWords(null, "repo")).toBe("≈ 5 min to ready");
  });
});

describe("an idea: Helm's understanding, then Create and start", () => {
  it("rewrites the words as a brief with its additions marked; Looks right, Fix it, Rewrite", async () => {
    const calls = api();
    await mountAs(<AddProjectSheet projectId="p1" homeId="p1" onClose={() => {}} onForm={() => {}} go={vi.fn()} />);
    expect(tid("add-project")!.dataset.kind).toBe("idea");
    await type(tid("ap-input"), WORDS);
    await enter(tid("ap-input"));
    expect(calls.find((c) => c.path === "/v1/projects/p1/add-project/understand")!.body).toMatchObject({ words: WORDS });
    const card = tid("ap-understood")!;
    expect(card.textContent).toContain("HelpDesk AI: email support with approved AI replies");
    // A confident heading that names the card (operator, 2026-11-08): never "Here's what I understood".
    expect(tid("ap-ub-heading")!.textContent).toBe("Project brief");
    expect(card.textContent).toContain("From your 3 lines · edit any line");
    expect(card.textContent).not.toMatch(/understood|rewrote/i);
    // The ONE brief card (app/projects/brief-card.tsx), the same New work draws.
    expect(tid("ap-ub")).not.toBeNull();
    expect([...card.querySelectorAll(".px-ub-grid mark")].map((m) => m.textContent).sort()).toEqual(["a customer portal", "nothing sends without approval"]);
    // The name follows the title; Helm's connector pick is on the box, with its words.
    expect(tid("ap-chip-name")!.textContent).toContain("HelpDesk AI");
    expect(tid("ap-box-connectors")!.textContent).toContain("Google");
    expect(tid("ap-box-workflow")!.textContent).toContain("Standard");
    expect(tid("ap-box-workflow")!.textContent).toContain("2 you approve");
    expect($$('[data-testid="ap-strip"] li[data-you]').length).toBe(2);
    // A quick answer to an open question.
    await click(tid("ap-q0-google-workspace"));
    expect(tid("ap-q0-google-workspace")!.getAttribute("aria-pressed")).toBe("true");
    // Fix it: every line in place; Done keeps the edit and asks for the picks again.
    await click(tid("ap-ub-fix"));
    await type(tid("ap-fix-title"), "HelpDesk: approved replies");
    await click(tid("ap-fix-add"));
    await click(tid("ap-ub-fix"));
    expect(tid("ap-understood")!.textContent).toContain("HelpDesk: approved replies");
    expect(calls.some((c) => c.path === "/v1/projects/p1/add-project/connectors")).toBe(true);
    // Rewrite with a hint keeps the edited line.
    await type(tid("ap-ub-hint"), "shorter");
    await click(tid("ap-ub-rewrite"));
    const again = calls.filter((c) => c.path === "/v1/projects/p1/add-project/understand").pop()!;
    expect(again.body).toMatchObject({ hint: "shorter", pinned: { title: "HelpDesk: approved replies" } });
    // Looks right: confirmed, folded to its chips.
    await click(tid("ap-ub-ok"));
    expect(tid("ap-ub")!.dataset.state).toBe("confirmed");
  });

  it("no model keeps the words and lets the person continue", async () => {
    api((c) => (c.path.endsWith("/understand") ? { body: { status: "no_model", message: "No model answered, so Helm could not rewrite it. Your words are kept as they are; you can continue.",
      brief: { title: "A helpdesk for our support team.", problem: "", who_uses: "", must_haves: ["Customers email support@"], out_of_scope: [], done_when: "", open_questions: [], added: [], pinned: [], raw: true },
      picks: { stack: null, connectors: [], workflow: null } } } : undefined));
    await mountAs(<AddProjectSheet projectId="p1" homeId="p1" onClose={() => {}} onForm={() => {}} go={vi.fn()} />);
    await type(tid("ap-input"), WORDS);
    await enter(tid("ap-input"));
    expect(tid("ap-understood")!.dataset.state).toBe("no_model");
    expect(tid("ap-understood")!.textContent).toContain("you can continue");
    expect((tid("ap-create") as HTMLButtonElement).disabled).toBe(false);
  });

  it("each box opens its popover in place; a pick changes the box", async () => {
    api();
    await mountAs(<AddProjectSheet projectId="p1" homeId="p1" onClose={() => {}} onForm={() => {}} go={vi.fn()} />);
    await click(tid("ap-box-workflow"));
    expect(tid("ap-pop-workflow")).not.toBeNull();
    expect(tid("ap-wf-steps")!.textContent).toContain("You approve");
    await click(tid("ap-wf-minor_feature"));
    await click(tid("ap-pop-workflow-done"));
    expect(tid("ap-pop-workflow")).toBeNull();
    expect(tid("ap-box-workflow")!.textContent).toContain("Small");
    await click(tid("ap-box-staging"));
    await click(tid("ap-staging-dh_2"));
    expect(tid("ap-box-staging")!.textContent).toContain("203.0.113.20:8006");
    expect(tid("ap-box-staging")!.textContent).not.toContain(" · :");
    await click(tid("ap-box-captain"));
    await click(tid("ap-captain-qa"));
    expect(tid("ap-box-captain")!.textContent).toContain("Vera");
    await click(tid("ap-box-connectors"));
    await click(tid("ap-cx-stripe-checkout"));
    expect(tid("ap-cx-secrets")!.textContent).toContain("STRIPE_SECRET_KEY");
    // The readiness dots say what this install has, and link what it lacks.
    expect(tid("ap-dot-deployment_target")!.hasAttribute("data-ok")).toBe(false);
    expect(tid("ap-dot-model")!.hasAttribute("data-ok")).toBe(true);
  });

  it("Create and start sends every pick once; a refusal opens the checklist and Start anyway resends", async () => {
    let posts = 0;
    const READY = { ready: false, checked: [{ key: "repository", label: "A GitHub repository", fix: "Connect one.", area: "setup", ok: false, detail: "none",
      needed_by: [{ step_id: "build", label: "Build" }], first_step_label: "Build", after_a_person: true }] };
    const calls = api((c) => {
      if (c.method === "POST" && c.path === "/v1/add-project") {
        posts += 1;
        return posts === 1 ? { status: 409, body: { step: "start", created: false, error: "not_ready", message: "Not ready",
                                                     readiness: { ...READY, missing: READY.checked } } }
          : { body: { created: true, project_id: "helpdesk-ai", name: "HelpDesk AI", kind: "idea", href: "?project_id=helpdesk-ai&area=project&tab=overview", steps: [], started: { run_id: "wfr_1" } } };
      }
      return undefined;
    });
    const go = vi.fn();
    await mountAs(<AddProjectSheet projectId="p1" homeId="p1" onClose={() => {}} onForm={() => {}} go={go} />);
    await type(tid("ap-input"), WORDS);
    await enter(tid("ap-input"));
    // The hosts the connectors' app talks to are allowed out loud before anything is sent.
    await click(tid("ap-create"));
    expect(tid("ap-egress")!.textContent).toContain("accounts.google.com");
    expect(calls.filter((c) => c.path === "/v1/add-project")).toHaveLength(0);
    await click(tid("ap-egress-allow"));
    await click(tid("ap-create"));
    expect(tid("ap-readiness")!.textContent).toContain("A GitHub repository");
    expect(tid("ap-create")!.textContent).toContain("Start anyway");
    await click(tid("dw-readiness-start-anyway"));
    await click(tid("ap-create"));
    const sent = calls.filter((c) => c.path === "/v1/add-project");
    expect(sent).toHaveLength(2);
    expect(sent[0].body).toMatchObject({ context_project_id: "p1", kind: "idea", name: "HelpDesk AI", words: WORDS,
      connectors: ["login-oauth"], workflow_key: "standard_product_delivery", captain: "engineering_captain",
      staging: { host_id: "dh_1", port: 8005 }, stack_language: "python", accept_egress: ["accounts.google.com"] });
    expect((sent[0].body as { brief: { title: string } }).brief.title).toBe(BRIEF.brief.title);
    expect(sent[1].body).toMatchObject({ start_anyway: ["repository"] });
    expect(new URLSearchParams(String(go.mock.calls[0][0]).split("?")[1]).get("project_id")).toBe("helpdesk-ai");
  });
});

describe("a repo: inline results, Helm's read, onboarding", () => {
  it("lists the org's repos and the host folders, ↵ picks the top match, and Create starts onboarding", async () => {
    const calls = api((c) => (c.method === "POST" && c.path === "/v1/add-project"
      ? { body: { created: true, project_id: "web", name: "Web", kind: "repo", href: "?project_id=web&area=project&tab=onboarding", steps: [], started: { onboarding: { run_id: "onb_1" } } } } : undefined));
    const go = vi.fn();
    await mountAs(<AddProjectSheet projectId="p1" homeId="p1" onClose={() => {}} onForm={() => {}} go={go} />);
    await type(tid("ap-input"), "acme/web");
    expect(tid("add-project")!.dataset.kind).toBe("repo");
    expect(tid("ap-results")!.textContent).toContain("acme/web");
    expect(tid("ap-results")!.textContent).toContain("Folders on hosts");
    expect(tid("ap-repo-ghr_web")!.getAttribute("aria-selected")).toBe("true");
    await enter(tid("ap-input"));
    expect(tid("ap-results")).toBeNull();
    const found = tid("ap-found")!;
    expect(found.textContent).toContain("Storefront UI");
    expect(tid("ap-repo-ub-heading")!.textContent).toBe("Repo summary");
    expect(found.textContent).not.toMatch(/understood/i);
    expect(found.textContent).toContain("src/inbox");
    expect(tid("ap-onboarding-will")!.textContent).toContain("Set up staging on boss-1:8005");
    expect(tid("ap-box-workflow")!.textContent).toContain("Onboard →");
    expect(tid("ap-then")!.textContent).toContain("You accept");
    // The THEN strip is the onboarding stages and nothing else: the file count is the summary's.
    expect(tid("ap-then")!.textContent!.trim().endsWith("You accept")).toBe(true);
    expect(tid("ap-then")!.textContent).not.toMatch(/\d+ files/);
    await click(tid("ap-chip-rules"));
    await click(tid("ap-create"));
    expect(calls.find((c) => c.path === "/v1/add-project")!.body).toMatchObject({ kind: "repo", repo_ids: ["ghr_web"], map_rules: true, name: "Web" });
    expect(new URLSearchParams(String(go.mock.calls[0][0]).split("?")[1]).get("tab")).toBe("onboarding");
  });

  it("the kind pill forces another reading", async () => {
    api();
    await mountAs(<AddProjectSheet projectId="p1" homeId="p1" onClose={() => {}} onForm={() => {}} go={vi.fn()} />);
    await type(tid("ap-input"), "helpdesk");
    expect(tid("add-project")!.dataset.kind).toBe("repo");
    await click(tid("ap-kind-idea"));
    expect(tid("add-project")!.dataset.kind).toBe("idea");
  });
});

describe("Workflows › New build has moved", () => {
  it("points a new app at Add a project and a feature at Project › New work", async () => {
    stubFetch(() => undefined);
    await mountAs(<MovedNewBuild projectId="helpdesk" />);
    expect(tid("moved-new-build-new-app")!.getAttribute("href")).toContain("new_project=idea");
    const work = new URLSearchParams(tid("moved-new-build-new-work")!.getAttribute("href")!.slice(1));
    expect([work.get("area"), work.get("tab"), work.get("compose"), work.get("project_id")]).toEqual(["project", "work", "1", "helpdesk"]);
  });
});

describe("the walk's gaps stay closed (laptop walk 2026-11-07)", () => {
  it("writes a staging host as host:port, one formatter", () => {
    expect(hostPort("203.0.113.20", 8006)).toBe("203.0.113.20:8006");
    expect(hostPort("boss-1", null)).toBe("boss-1");
    expect(hostPort("2001:db8::1", 8006)).toBe("[2001:db8::1]:8006");
  });

  it("counts, strips, legends and lists 'you approve' from the one `you` per step, in few words", async () => {
    api();
    await mountAs(<AddProjectSheet projectId="p1" homeId="p1" onClose={() => {}} onForm={() => {}} go={vi.fn()} />);
    await type(tid("ap-input"), WORDS);
    await enter(tid("ap-input"));
    const you = STANDARD.step_list.filter((s) => s.you).length;
    expect(tid("ap-box-workflow")!.textContent).toContain(`${you} you approve`);
    const strip = tid("ap-strip")!;
    expect(strip.querySelectorAll("li[data-you]").length).toBe(you);
    // The legend names what you approve in the step's few words, never its full label.
    expect(strip.textContent).toContain("you approve: PRD · PR");
    expect(strip.textContent).not.toContain("prd approval");
    await click(tid("ap-box-workflow"));
    expect(tid("ap-wf-steps")!.querySelectorAll(".npx-you-pill").length).toBe(you);
  });

  const css = readFileSync(join(__dirname, "../app/start-project/add-project.css"), "utf8");

  it("the sheet grows with the root type, and the foot is one row above phone width", () => {
    // A px width kept the sheet at 980px while the type grew 30% at 2260: Create wrapped left.
    expect(css).toMatch(/\.npx-sheet \{[^}]*width: min\([\d.]+rem,/);
    const wide = css.match(/@media \(min-width: 761px\) \{([\s\S]*?)\n\}/)![1];
    expect(wide).toMatch(/\.npx-foot \{ flex-wrap: nowrap; \}/);
    expect(wide).toMatch(/\.npx-foot > \.npx-faint \{[^}]*text-overflow: ellipsis/);
  });

  it("nothing is ever drawn under the foot: the body scrolls between a head and a foot that stay put", () => {
    // Walk 2026-11-07: long box values ran under the footer (it was sticky inside a scrolling
    // backdrop, 28 px above its edge). The sheet is the window's height at most, its body
    // scrolls, the foot is not sticky; on a phone a popover opens in the body's flow, so the
    // foot never covers it and needs no hiding.
    expect(css).toMatch(/\.npx-sheet \{[^}]*max-height: calc\(100dvh - 56px\)/);
    expect(css).toMatch(/\.npx-body \{[^}]*min-height: 0; overflow-y: auto/);
    expect(css).not.toMatch(/\.npx-foot \{[^}]*position: sticky/);
    expect(css).not.toMatch(/\.npx-foot \{ display: none; \}/);
  });

  it("a long name ends in an ellipsis inside its chip", () => {
    expect(css).toMatch(/\.npx-pchip \{[^}]*max-width: 100%[^}]*min-width: 0/);
    expect(css).toMatch(/\.npx-pchip b \{[^}]*overflow: hidden; text-overflow: ellipsis; white-space: nowrap/);
  });
});
