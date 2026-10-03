/**
 * Fixtures for the inspector, JSON and save-flow tests, run against the REAL editor model (S1):
 * the reducer, the step view and the graph ops are the shipped ones, so a rule the store applies
 * (trimming a title, refusing visual edits under broken JSON, SAVED moving the baseline) is the
 * rule these tests see. A stand-in store once hid a title that could not take a space.
 *
 * Only the lane (S2) is stood in for, because these tests are about the inspector, not the lane.
 */
import React from "react";

import type { EditorMeta, EditorState, Palette, Spec } from "../../lib/workflow-editor-types";
import { stepView } from "../../lib/step-view";
import { editorReducer, emptyEditorState } from "../../lib/workflow-editor-store";

const clone = <T,>(v: T): T => JSON.parse(JSON.stringify(v)) as T;

export const PALETTE: Palette = {
  capabilities: [
    { key: "research", label: "Research", summary: "Research a topic", risk_tier: 0,
      required_inputs: ["topic"], optional_inputs: ["depth"], produces: ["dossier"],
      skills: ["web.search"] },
    { key: "build", label: "Build", summary: "Build it", risk_tier: 1, required_inputs: ["spec"],
      optional_inputs: [], produces: ["pr_url"], skills: ["github.create_pr"] },
    { key: "deploy", label: "Deploy", summary: "Ship it", risk_tier: 3, required_inputs: ["pr_url"],
      optional_inputs: [], produces: ["url"], skills: ["deploy.run"], approval_forced_manual: true },
  ],
  customSteps: [
    { step_key: "file_ticket", label: "File a ticket", binding_kind: "skill", skill_id: "jira.file",
      inputs: [{ name: "ticket", required: true }], produces: ["ticket_url"], risk_tier: 0, usable: true },
  ],
  blocks: [
    { kind: "decision", label: "Approval gate", summary: "", defaults: { approver_role: "human" },
      configure: [{ key: "question", label: "What is being approved?", type: "text" },
                  { key: "guidance", label: "What should the reviewer check?", type: "text" },
                  { key: "approver_role", label: "Who approves", type: "text" }] },
    { kind: "timer", label: "Wait", summary: "", defaults: { wait_seconds: 3600 },
      configure: [{ key: "wait_seconds", label: "Wait for (seconds)", type: "number" }] },
    { kind: "subworkflow", label: "Run another workflow", summary: "", defaults: {},
      configure: [{ key: "workflow_version_id", label: "Which workflow", type: "workflow" }] },
  ],
  agents: [
    { role_key: "qa", name: "QA Lead", skills: ["web.search"], mcp: ["github"] },
    { role_key: "dev", name: "Developer", skills: ["web.search", "github.create_pr", "jira.file"], mcp: ["github", "linear"] },
  ],
  humanRole: "human",
  riskFloor: 2,
  bindable: { skills: [], mcp_servers: [] },
};

/* ── the store ─────────────────────────────────────────────────────────────────── */

export const META: EditorMeta = {
  definition_id: "def_own", workflow_key: "my_flow", name: "My flow", description: "",
  is_catalogue: false, from_workflow_key: null, version: 3, copy: null, readOnlyVersion: null,
};

/** A workflow as the real store holds it right after LOAD, with `over` laid on top. */
export function makeState(spec: Spec, meta: Partial<EditorMeta> = {}, over: Partial<EditorState> = {}): EditorState {
  const m = { ...META, ...meta };
  return { ...editorReducer(emptyEditorState(), { type: "LOAD", spec: clone(spec), meta: m }), ...over };
}

/** The real S1 reducer: these tests run against the store the editor ships with. */
export const reducer = editorReducer;

/** The lane, as far as these tests need it: one button per step, labelled with its agent. */
export function FakeFlowLane({ spec, palette, selection, onSelect }: {
  spec: Spec; palette: Palette | null; selection?: string | null; onSelect?(id: string | null): void;
}) {
  return (
    <div data-testid="lane">
      {spec.steps.map((s) => (
        <button key={s.id} type="button" data-testid={`node-${s.id}`} aria-pressed={selection === s.id}
                onClick={() => onSelect?.(s.id)}>
          {palette ? stepView(s, palette).agent.label : s.id}
        </button>
      ))}
    </div>
  );
}

/* ── DOM helpers ───────────────────────────────────────────────────────────────── */

export function setValue(el: HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement, value: string) {
  const proto = Object.getPrototypeOf(el) as object;
  Object.getOwnPropertyDescriptor(proto, "value")!.set!.call(el, value);
  el.dispatchEvent(new Event(el instanceof HTMLSelectElement ? "change" : "input", { bubbles: true }));
}

export const q = <T extends Element = HTMLElement>(root: ParentNode, testId: string) =>
  root.querySelector<T & Element>(`[data-testid="${testId}"]`) as T | null;
