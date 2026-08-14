import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  formatProcessStepLine,
  mapCompilePreview,
  mapProcessRuns,
  mapProcessSteps,
  mapProcessTemplates,
  specFromBuilderSteps,
} from "../lib/agent-process";

vi.mock("next/cache", () => ({ revalidatePath: vi.fn() }));
vi.mock("../lib/client", () => ({
  API_URL: "https://boss-api.test",
  authHeaders: (extra?: Record<string, string>) => extra ?? {},
}));
vi.mock("../app/submit-button", () => ({
  SubmitButton: (
    props: React.ButtonHTMLAttributes<HTMLButtonElement> & { children?: React.ReactNode },
  ) =>
    React.createElement(
      "button",
      { type: "submit", className: props.className, disabled: props.disabled, name: props.name, value: props.value },
      props.children,
    ),
}));

const fetchMock = vi.fn();
vi.stubGlobal("fetch", fetchMock);

const { AgentProcesses } = await import("../app/agent-process-ui");
const {
  compileAgentProcessAction,
  saveAgentProcessTemplateAction,
  startAgentProcessAction,
} = await import("../app/agent-process-actions");

const pageSource = readFileSync(resolve(__dirname, "../app/page.tsx"), "utf8");
const actionsSource = readFileSync(
  resolve(__dirname, "../app/agent-process-actions.ts"),
  "utf8",
);
const uiSource = readFileSync(resolve(__dirname, "../app/agent-process-ui.tsx"), "utf8");

describe("Agent process island", () => {
  it("mounts on data-area=processes", () => {
    expect(pageSource).toContain('import { AgentProcesses } from "./agent-process-ui"');
    expect(pageSource).toContain('data-area="processes"');
    expect(pageSource).toContain("<AgentProcesses");
  });

  it("renders NL compile, manual builder, and PRD start form", () => {
    const markup = renderToStaticMarkup(
      React.createElement(AgentProcesses, {
        projectId: "boss-dogfood",
        actor: "ceo",
      }),
    );
    expect(markup).toContain('data-testid="agent-processes"');
    expect(markup).toContain('data-testid="agent-process-nl"');
    expect(markup).toContain('data-testid="agent-process-builder"');
    expect(markup).toContain('data-testid="agent-process-start"');
    expect(markup).toContain("Describe the handoff chain");
    expect(markup).toContain("Compile");
    expect(markup).toContain("Save as process");
    expect(markup).toContain("Start PRD process");
    expect(markup).toContain("Architect");
    expect(markup).toContain("QA");
    expect(markup).toContain("certified");
    expect(markup).toContain("Metatron can also build these from chat");
    expect(markup).toContain("product_manager");
    expect(markup).toContain("accept_prd");
    expect(markup).toContain("process_key");
    expect(markup).not.toMatch(/silently (apply|change)/i);
    expect(uiSource).not.toMatch(/silently (apply|change)/i);
    expect(actionsSource).toContain('"/v1/agent-processes/runs"');
    expect(actionsSource).toContain('"/v1/agent-processes/compile"');
    expect(actionsSource).toContain('"/v1/agent-processes/templates"');
  });
});

describe("Agent process mappers", () => {
  it("maps templates and runs without inventing rows", () => {
    expect(mapProcessTemplates({ templates: [] })).toEqual([]);
    expect(
      mapProcessTemplates({
        templates: [{ process_key: "prd_to_accepted_work", name: "PRD to accepted work", id: "t1" }],
      }),
    ).toEqual([
      {
        id: "t1",
        processKey: "prd_to_accepted_work",
        name: "PRD to accepted work",
        description: "",
      },
    ]);
    expect(mapProcessRuns({ runs: [{ title: "missing id" }] })).toEqual([]);
    expect(
      mapProcessRuns({
        runs: [{ id: "aprun_1", title: "HookWatch", status: "completed", process_key: "prd_to_accepted_work" }],
      }),
    ).toEqual([
      {
        id: "aprun_1",
        title: "HookWatch",
        status: "completed",
        processKey: "prd_to_accepted_work",
        templateName: undefined,
      },
    ]);
  });

  it("formats timeline lines from recorded verdicts only", () => {
    const steps = mapProcessSteps({
      steps: [
        {
          id: "s1",
          step_key: "review_architect",
          role_key: "chief_architect",
          action: "review_prd",
          status: "succeeded",
          output: { verdict: "approve" },
        },
      ],
    });
    expect(steps[0]?.verdict).toBe("approve");
    expect(formatProcessStepLine(steps[0])).toBe(
      "review_architect · chief_architect · review_prd · succeeded · approve",
    );
    expect(formatProcessStepLine({
      id: "s2",
      stepKey: "qa_check",
      role: "qa",
      action: "qa_review",
      status: "pending",
    })).toBe("qa_check · qa · qa_review · pending · —");
  });

  it("compiles builder steps into a sequential spec", () => {
    const spec = specFromBuilderSteps({
      processKey: "custom_handoff",
      name: "Custom handoff",
      description: "manual",
      steps: [
        { localId: "a", id: "intake", role: "product_manager", action: "accept_prd", kind: "role" },
        { localId: "b", id: "panel", role: "cto", action: "certify_spec", kind: "panel" },
      ],
    });
    expect(spec.entry).toBe("intake");
    expect(spec.steps[0]?.next).toEqual(["panel"]);
    expect(spec.steps[1]?.kind).toBe("panel");
    expect(spec.steps[1]?.roles).toEqual(["cto"]);
  });

  it("reads compile preview from spec or loose steps", () => {
    const preview = mapCompilePreview({
      spec: {
        key: "nl_chain",
        name: "NL chain",
        steps: [{ id: "intake", role: "product_manager", action: "accept_prd", kind: "role" }],
      },
    });
    expect(preview.spec?.key).toBe("nl_chain");
    expect(preview.steps[0]?.action).toBe("accept_prd");
  });
});

describe("Agent process actions", () => {
  beforeEach(() => {
    fetchMock.mockReset();
  });

  it("posts compile previews without spending", async () => {
    fetchMock.mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: async () => ({
        spec: {
          key: "nl_chain",
          steps: [{ id: "intake", role: "product_manager", action: "accept_prd" }],
        },
      }),
    });
    const form = new FormData();
    form.set("project_id", "boss-dogfood");
    form.set("actor", "ceo");
    form.set("description", "PM accepts, then QA reviews.");
    const result = await compileAgentProcessAction(form);
    expect(result.ok).toBe(true);
    expect(String(fetchMock.mock.calls[0]?.[0])).toBe(
      "https://boss-api.test/v1/agent-processes/compile",
    );
    expect(JSON.parse(String((fetchMock.mock.calls[0]?.[1] as RequestInit).body))).toMatchObject({
      project_id: "boss-dogfood",
      description: "PM accepts, then QA reviews.",
    });
    expect(actionsSource).toContain('"/v1/agent-processes/compile"');
  });

  it("posts saved templates", async () => {
    fetchMock.mockResolvedValueOnce({
      ok: true,
      status: 201,
      json: async () => ({ id: "aproc_1", process_key: "custom_handoff" }),
    });
    const form = new FormData();
    form.set("project_id", "boss-dogfood");
    form.set("actor", "ceo");
    form.set("process_key", "custom_handoff");
    form.set("name", "Custom handoff");
    form.set(
      "spec",
      JSON.stringify({
        key: "custom_handoff",
        steps: [{ id: "intake", role: "product_manager", action: "accept_prd", kind: "role" }],
      }),
    );
    const result = await saveAgentProcessTemplateAction(form);
    expect(result.ok).toBe(true);
    expect(String(fetchMock.mock.calls[0]?.[0])).toBe(
      "https://boss-api.test/v1/agent-processes/templates",
    );
    expect(actionsSource).toContain('"/v1/agent-processes/templates"');
  });

  it("starts a run with the selected process_key", async () => {
    fetchMock.mockResolvedValueOnce({
      ok: true,
      status: 201,
      json: async () => ({ id: "aprun_1", status: "completed", steps: [] }),
    });
    const form = new FormData();
    form.set("project_id", "boss-dogfood");
    form.set("actor", "ceo");
    form.set("prd", "Users must verify acceptance.");
    form.set("process_key", "custom_handoff");
    const result = await startAgentProcessAction(form);
    expect(result.ok).toBe(true);
    expect(JSON.parse(String((fetchMock.mock.calls[0]?.[1] as RequestInit).body))).toMatchObject({
      process_key: "custom_handoff",
      prd: "Users must verify acceptance.",
    });
  });
});
