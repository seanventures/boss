// @vitest-environment happy-dom

import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

/**
 * A room's start card is the workflow's own start form, read the way the composer reads it
 * (lib/workflow-inputs.ts): every declared field, labelled from the schema, a yes/no as a box,
 * and Start held while a required field is blank. The card then goes through the composer's
 * door (POST …/start → start_delivery_workflow), which types each value by the same schema.
 */

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
(globalThis as Record<string, unknown>).React = React;

const { ProposalCard } = await import("../app/agents/converse-parts");
const { mapProposal } = await import("../lib/agent-converse");

let root: Root | null = null;
let host: HTMLElement | null = null;

afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  root = null;
  host = null;
});

const WIRE = {
  proposal_id: "prp_1", workflow_key: "video_edit", title: "Cut the launch video",
  directive: "Trim it", inputs: { video_url: "https://x.test/v.mp4" }, acceptance_criteria: [],
  agents: [], why: "", status: "proposed",
  input_schema: {
    required: ["video_url", "channel"],
    properties: {
      video_url: { type: "string", title: "Video link", format: "uri" },
      channel: { type: "string", title: "Channel" },
      shorts: { type: "boolean", title: "Also make Shorts" },
    },
  },
};

function render(onStart = vi.fn(async () => "wfr_1")) {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  act(() => root!.render(<ProposalCard proposal={mapProposal(WIRE)} onStart={onStart} />));
  return { card: host, onStart };
}

describe("the start card", () => {
  it("asks for every field the workflow's start form declares, labelled from the schema", () => {
    const { card } = render();
    const labels = [...card.querySelectorAll(".ag-input > span")].map((s) => s.textContent);
    expect(labels).toEqual(["Video link *", "Channel *", "Also make Shorts"]);
    expect(card.querySelector<HTMLInputElement>('input[aria-label="Also make Shorts"]')!.type).toBe("checkbox");
  });

  it("holds Start while a required field is blank, and names it", () => {
    const { card } = render();
    const start = card.querySelector<HTMLButtonElement>('[data-testid="ag-start"]')!;
    expect(start.disabled).toBe(true);
    expect(start.title).toContain("Channel");
  });
});
