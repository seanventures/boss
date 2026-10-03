"use client";

import React, { useMemo, useState } from "react";

import type { Bindable, CustomStep } from "../delivery-workflows-ui";

/** Defining a step: name it, choose what it runs, say what it needs.
 *
 *  There is deliberately NO risk field. The tier comes from the skill or MCP tool being bound
 *  and is shown here as a consequence of that choice — a customer who could type their own
 *  number would have a documented way past every approval gate in the product.
 */
export function DefineStepForm({ bindable, riskFloor, busy, projectId, onDefine, onCancel,
                         editing }: {
  bindable: Bindable;
  riskFloor: number;
  busy: string | null;
  projectId: string;
  onDefine: (fields: Record<string, unknown>) => Promise<{ ok: boolean; error?: string }>;
  onCancel: () => void;
  /** The step being changed, or absent when defining a new one. ONE FORM FOR BOTH: an edit
   *  form that drifted from the define form would accept fields the other rejects, and the
   *  operator would meet two different sets of rules for the same object. */
  editing?: CustomStep | null;
}) {
  const [label, setLabel] = useState(editing?.label ?? "");
  const [summary, setSummary] = useState(editing?.summary ?? "");
  // One control, not two. "skill:github.create_pr" / "mcp:<server>:<tool>" keeps the choice a
  // single decision — picking a kind and then a thing is two ways to get it half-made.
  const [bindingRef, setBindingRef] = useState(
    editing
      ? (editing.binding_kind === "skill"
          ? `skill:${editing.skill_id ?? ""}`
          : `mcp:${(editing as { mcp_server_id?: string }).mcp_server_id ?? ""}:`
            + `${editing.mcp_tool_name ?? ""}`)
      : "");
  const [inputs, setInputs] = useState(
    (editing?.inputs ?? []).map((i) => i.name).join(", "));
  // WHAT THE STEP KNOWS. A binding says which tool runs; it says nothing about the standard
  // the step has to hold to, and that lived in the head of whoever defined it.
  const [knowledge, setKnowledge] = useState(editing?.knowledge ?? "");
  const [failure, setFailure] = useState<string | null>(null);

  const chosen = useMemo(() => {
    if (bindingRef.startsWith("skill:")) {
      const id = bindingRef.slice(6);
      const skill = bindable.skills.find((s) => s.skill_id === id);
      return skill
        ? { risk: skill.risk_tier, trusted: skill.trusted, unclassified: skill.unclassified,
            what: id }
        : null;
    }
    if (bindingRef.startsWith("mcp:")) {
      const [, serverId, ...rest] = bindingRef.split(":");
      const server = bindable.mcp_servers.find((m) => m.id === serverId);
      const tool = server?.tools.find((t) => t.name === rest.join(":"));
      return server && tool
        ? { risk: tool.risk_tier, trusted: server.trusted, unclassified: false,
            what: `${server.name} · ${tool.name}` }
        : null;
    }
    return null;
  }, [bindingRef, bindable]);

  const blocked = chosen !== null && chosen.risk >= riskFloor && !chosen.trusted;
  // How many things can ACTUALLY be picked, which is not the same as how many servers exist.
  const choices = bindable.skills.filter((s) => s.usable !== false).length
    + bindable.mcp_servers.reduce((n, server) => n + server.tools.length, 0);
  // TWO GROUPS, NAMED FOR WHAT THEY ARE. "Your skills" used to list project_skills — on a real
  // install five BOSS system skills and none of the operator's — while the operator's authored
  // skills and most shipped ones were missing. Both come from the one catalogue now.
  const yours = bindable.skills.filter((s) => s.source === "authored");
  const shipped = bindable.skills.filter((s) => s.source !== "authored");
  const skillOption = (skill: Bindable["skills"][number]) => (
    <option key={skill.skill_id} value={`skill:${skill.skill_id}`}
            disabled={skill.usable === false}
            title={skill.usable === false ? (skill.reason ?? "cannot run") : undefined}>
      {skill.label && skill.label !== skill.skill_id
        ? `${skill.label} (${skill.skill_id})` : skill.skill_id}
      {skill.unclassified ? " (unclassified)" : ""}
      {skill.usable === false ? ` — ${skill.reason ?? "cannot run"}` : ""}
    </option>
  );
  const unreachable = bindable.mcp_servers.filter((m) => !m.reachable).map((m) => m.name);

  return (
    <form className="dw-define-form" data-testid="dw-define-form" onSubmit={async (e) => {
      e.preventDefault();
      setFailure(null);
      const [kind, ...rest] = bindingRef.split(":");
      const fields: Record<string, unknown> = {
        project_id: projectId,
        label: label.trim(),
        summary: summary.trim(),
        binding_kind: kind === "skill" ? "skill" : "mcp_tool",
        inputs: inputs.split(",").map((i) => i.trim()).filter(Boolean),
        knowledge: knowledge.trim(),
      };
      if (kind === "skill") {
        fields.skill_id = rest.join(":");
      } else {
        fields.mcp_server_id = rest[0];
        fields.mcp_tool_name = rest.slice(1).join(":");
      }
      // The KEY, when this is an edit. It is what workflow specs bind to, so it travels as an
      // identifier and is never one of the fields being changed — renaming a step must not
      // orphan every workflow that places it.
      if (editing) fields.step_key = editing.step_key;
      const result = await onDefine(fields);
      if (!result.ok) {
        setFailure(result.error
                   ?? (editing ? "could not change the step" : "could not define the step"));
      }
    }}>
      <label className="field">
        <span>Name</span>
        <input className="input" autoFocus value={label} data-testid="dw-define-label"
               placeholder="File a compliance ticket"
               onChange={(e) => setLabel(e.target.value)} />
      </label>
      <label className="field">
        <span>Runs</span>
        <select className="input" value={bindingRef} data-testid="dw-define-binding"
                onChange={(e) => setBindingRef(e.target.value)}>
          <option value="">Choose a skill or tool</option>
          {yours.length ? (
            <optgroup label="Your skills">{yours.map(skillOption)}</optgroup>
          ) : null}
          {shipped.length ? (
            <optgroup label="Skills BOSS ships">{shipped.map(skillOption)}</optgroup>
          ) : null}
          {bindable.mcp_servers.map((server) => (
            <optgroup key={server.id}
                      label={server.reachable ? server.name : `${server.name} (unreachable)`}>
              {server.tools.map((tool) => (
                <option key={tool.name} value={`mcp:${server.id}:${tool.name}`}>
                  {tool.name}
                </option>
              ))}
            </optgroup>
          ))}
        </select>
      </label>
      <label className="field">
        <span>Needs</span>
        <input className="input" value={inputs} data-testid="dw-define-inputs"
               placeholder="ticket, severity"
               onChange={(e) => setInputs(e.target.value)} />
      </label>
      {/* WHAT THIS STEP KNOWS. Carried into every execution, so the step behaves the same on
          its hundredth run as on the one where somebody was watching. */}
      <label className="field">
        <span>What should it know?</span>
        <textarea className="textarea" rows={4} value={knowledge}
                  data-testid="dw-step-knowledge"
                  placeholder="House style, the clauses that matter, the internal doc that explains this system — anything the step must hold to that its tool does not already know."
                  onChange={(e) => setKnowledge(e.target.value)} />
        <span className="detail">
          Optional. Handed to the skill or tool on every run as context.
        </span>
      </label>
      <label className="field">
        <span>What it does</span>
        <input className="input" value={summary} placeholder="Optional, one line"
               onChange={(e) => setSummary(e.target.value)} />
      </label>

      {/* The consequence of the binding, stated before the step is created rather than at
          publish time when the workflow is already drawn. */}
      {chosen ? (
        <p className="detail dw-define-risk" data-testid="dw-define-risk">
          Risk tier {chosen.risk}, from {chosen.what}
          {chosen.unclassified ? " — nobody has classified this skill, so it is treated as "
                                 + "the floor" : ""}
          {chosen.risk >= riskFloor
            ? ". This step will always stop for a person."
            : ". This step can run automatically."}
        </p>
      ) : null}
      {blocked ? (
        <p className="detail dw-define-blocked" data-testid="dw-define-blocked">
          This is untrusted and at tier {chosen?.risk}. You can define it, but a workflow using
          it will not publish until it has been reviewed.
        </p>
      ) : null}
      {failure ? <p className="detail dw-define-error" data-testid="dw-define-error">{failure}</p> : null}

      <div className="dw-define-actions">
        <button type="submit" className="cap-btn" data-testid="dw-define-save"
                disabled={!!busy || !label.trim() || !bindingRef || choices === 0}>
          {editing ? "Save changes" : "Define"}
        </button>
        <button type="button" className="cap-btn" onClick={onCancel}>Cancel</button>
      </div>
      {choices === 0 ? (
        // Counted in CHOICES, not in servers. A project with three registered MCP servers that
        // are all unreachable has three empty groups in the dropdown and nothing to pick — and
        // saying nothing there reads as a broken control rather than an empty one.
        <p className="detail dw-define-empty" data-testid="dw-define-empty">
          Nothing to bind a step to yet.{" "}
          {unreachable.length
            ? `${unreachable.join(", ")} ${unreachable.length === 1 ? "is" : "are"} `
              + "registered but not answering, so their tools cannot be listed. "
            : ""}
          Register a skill, or connect an MCP server, and it will appear here.
        </p>
      ) : unreachable.length ? (
        // Said even when there is something else to pick. The API lists an unreachable
        // server's LAST-KNOWN tools (marked "(unreachable)" in the picker), so this used to
        // claim they were "not listed" right above a list of them (round-1 review). What is
        // true: they are what it offered when last reached, and a step on one fails until it
        // answers again.
        <p className="detail dw-define-empty" data-testid="dw-define-unreachable">
          {unreachable.join(", ")} {unreachable.length === 1 ? "is" : "are"} not answering.
          Any tools shown under {unreachable.length === 1 ? "it" : "them"} are what
          {unreachable.length === 1 ? " it" : " they"} offered when last reached, and a step
          bound to one will fail until the server answers. Fix the URL or start the server on
          the Skills and MCP page.
        </p>
      ) : null}
    </form>
  );
}
