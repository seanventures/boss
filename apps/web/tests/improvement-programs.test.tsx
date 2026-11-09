import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("next/cache", () => ({
  revalidatePath: vi.fn(),
}));

vi.mock("../lib/client", () => ({
  PROJECT_ID: "boss-dogfood",
  API_URL: "http://127.0.0.1:8000",
  authHeaders: (extra?: Record<string, string>) => ({
    Authorization: "Bearer test",
    ...(extra ?? {}),
  }),
  resolveProjectId: (projectId?: string) => projectId?.trim() || "boss-dogfood",
  client: {},
}));

vi.mock("../app/submit-button", () => ({
  SubmitButton: (
    props: React.ButtonHTMLAttributes<HTMLButtonElement> & {
      children?: React.ReactNode;
    },
  ) =>
    React.createElement(
      "button",
      { type: "submit", className: props.className },
      props.children,
    ),
}));

const fetchMock = vi.fn();
vi.stubGlobal("fetch", fetchMock);

const { revalidatePath } = await import("next/cache");
const { createImprovementProgramAction } = await import("../app/actions");
const { ImprovementProgramsPanel } = await import("../app/improvement-programs-ui");

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");

describe("createImprovementProgramAction", () => {
  afterEach(() => {
    fetchMock.mockReset();
    vi.mocked(revalidatePath).mockReset();
  });

  it("posts to /v1/improvement-programs and never calls /v1/self-improvement", async () => {
    fetchMock.mockResolvedValueOnce({
      ok: true,
      status: 201,
      json: async () => ({ id: "improg_1", name: "routing-baseline" }),
    });

    const form = new FormData();
    form.set("project_id", "boss-dogfood");
    form.set("actor", "ceo");
    form.set("name", "routing-baseline");
    form.set("objective", "Hold routing quality");
    form.set("benchmark_id", "bugbox");
    form.set("baseline_evaluation_id", "eval_1");

    await expect(createImprovementProgramAction(form)).resolves.toMatchObject({ ok: true });

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("http://127.0.0.1:8000/v1/improvement-programs");
    expect(url).not.toContain("/v1/self-improvement");
    expect(init.method).toBe("POST");
    const body = JSON.parse(String(init.body));
    expect(body).toEqual({
      project_id: "boss-dogfood",
      name: "routing-baseline",
      objective: "Hold routing quality",
      baseline_evaluation_id: "eval_1",
      actor: "ceo",
    });
    expect(JSON.stringify(body)).not.toContain("self-improvement");
    expect(revalidatePath).toHaveBeenCalledWith("/");
  });

  it("rejects missing required create fields before calling the API", async () => {
    const form = new FormData();
    form.set("project_id", "boss-dogfood");
    form.set("actor", "ceo");
    form.set("name", "routing-baseline");
    form.set("objective", "Hold routing quality");
    // REWRITTEN for the ActionResult contract (lib/action-result.ts): a server action now
    // returns its failure as a sentence instead of throwing one Next would redact.

    await expect(createImprovementProgramAction(form)).resolves.toMatchObject({

      ok: false, error: expect.stringContaining("baseline_evaluation_id is required"),

    });
    expect(fetchMock).not.toHaveBeenCalled();
    expect(revalidatePath).not.toHaveBeenCalled();
  });
});

describe("Community improvement programs panel", () => {
  it("renders the create form and keeps promote/adopt disabled", () => {
    const html = renderToStaticMarkup(
      <ImprovementProgramsPanel
        programs={[
          {
            id: "improg_1",
            name: "routing-baseline",
            objective: "Hold routing quality",
            status: "active",
            baselineEvaluationId: "eval_1",
            updatedAt: "2026-08-11T00:00:00Z",
          },
        ]}
        programsApiReachable
        projectId="boss-dogfood"
        actor="ceo"
      />,
    );

    expect(html).toContain('data-testid="improvement-program-create-form"');
    expect(html).toContain('name="name"');
    expect(html).toContain('name="objective"');
    expect(html).toContain('name="benchmark_id"');
    expect(html).toContain('value="bugbox"');
    expect(html).toContain('name="project_id"');
    expect(html).toContain('name="actor"');
    expect(html).toContain('type="hidden"');
    expect(html).toContain("routing-baseline");
    expect(html).toContain("Hold routing quality");
    expect(html).toMatch(
      /<button\b[^>]*\bdisabled\b[^>]*data-testid="improvement-program-promote"|<button\b[^>]*data-testid="improvement-program-promote"[^>]*\bdisabled\b/,
    );
    expect(html).toMatch(
      /<button\b[^>]*\bdisabled\b[^>]*data-testid="improvement-program-adopt-baseline"|<button\b[^>]*data-testid="improvement-program-adopt-baseline"[^>]*\bdisabled\b/,
    );
    expect(html).toContain(
      "Promote is an enterprise SIM step, Community records trials only.",
    );
  });

  it("keeps the Community write form when the list is empty", () => {
    const html = renderToStaticMarkup(
      <ImprovementProgramsPanel
        programs={[]}
        programsApiReachable
        projectId="boss-dogfood"
        actor="ceo"
      />,
    );

    expect(html).toContain('data-testid="improvement-program-create-form"');
    expect(html).toContain("No improvement programs recorded yet.");
    expect(html).toMatch(
      /<button\b[^>]*\bdisabled\b[^>]*data-testid="improvement-program-promote"|<button\b[^>]*data-testid="improvement-program-promote"[^>]*\bdisabled\b/,
    );
  });

  it("wires the Community panel in Mission Control without touching the enterprise SIM propose form", () => {
    // The Community tree ships no enterprise SIM propose form at all (it lives only in the
    // private repo, where this test was written): the panel is the whole of it, in Operations.
    const page = readFileSync(join(ROOT, "app/page.tsx"), "utf8");
    expect(page).toContain('data-testid="improvement-programs-panel"');
    const communitySlice = page
      .split('data-testid="improvement-programs-panel"')[1]
      .split("</section>")[0];
    expect(communitySlice).toContain("<ImprovementProgramsPanel");
    expect(page.slice(page.lastIndexOf("<section", page.indexOf('data-testid="improvement-programs-panel"')),
      page.indexOf('data-testid="improvement-programs-panel"'))).toContain('data-area="ops"');
    expect(page).not.toContain("submitSelfImprovementProposalAction");
    expect(page).not.toContain("/v1/self-improvement");
  });
});
