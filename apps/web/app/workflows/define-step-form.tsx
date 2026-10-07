"use client";

import { GatedButton } from "../../components/gated";
import React, { useMemo, useState } from "react";

import type { Bindable, CustomStep } from "../delivery-workflows-ui";
import { HelpTip } from "../help-tip";
import { notSetUp, setUpHref, useKitAbilities } from "../../lib/use-kit-abilities";
import { ChipInput } from "./editor/inspector/chip-input";

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
  // One chip per input name. A comma-separated box turned a trailing comma or a stray space
  // into an input called "" that every workflow placing the step then had to wire.
  const [inputs, setInputs] = useState<string[]>((editing?.inputs ?? []).map((i) => i.name));
  // WHAT IT MAKES, so a later step can be wired to it. Without this the inspector could offer
  // nothing from a custom step but "▸ …" and a free-text field name.
  const [produces, setProduces] = useState<string[]>(editing?.produces ?? []);
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
  // GROUPED BY ABILITY FIRST (skills-mcp spec §3.3): the Kit's outcomes, each with the skills
  // and MCP tools behind it, said `Not set up` when it is not. Everything else follows as before.
  const abilities = useKitAbilities(projectId);
  const inAbility = new Set<string>();
  const abilityGroups = (abilities ?? []).map((a) => {
    const skills = bindable.skills.filter((sk) => a.tools.some((t) => t.ref === `skill:${sk.skill_id}`));
    const tools = bindable.mcp_servers.flatMap((server) => server.tools
      .filter((t) => a.tools.some((x) => x.ref === `mcp:${server.name}.${t.name}`))
      .map((t) => ({ server, tool: t })));
    skills.forEach((sk) => inAbility.add(`skill:${sk.skill_id}`));
    tools.forEach(({ server, tool }) => inAbility.add(`mcp:${server.id}:${tool.name}`));
    return { ability: a, skills, tools };
  }).filter((g) => g.skills.length || g.tools.length);
  const chosenAbility = abilityGroups.find((g) => g.skills.some((sk) => `skill:${sk.skill_id}` === bindingRef)
    || g.tools.some(({ server, tool }) => `mcp:${server.id}:${tool.name}` === bindingRef))?.ability;
  const yours = bindable.skills.filter((s) => s.source === "authored" && !inAbility.has(`skill:${s.skill_id}`));
  const shipped = bindable.skills.filter((s) => s.source !== "authored" && !inAbility.has(`skill:${s.skill_id}`));
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
        inputs,
        produces,
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
          {abilityGroups.map(({ ability, skills, tools }) => (
            <optgroup key={ability.key} label={notSetUp(ability) ? `${ability.label} · Not set up` : ability.label}>
              {skills.map(skillOption)}
              {tools.map(({ server, tool }) => (
                <option key={`${server.id}:${tool.name}`} value={`mcp:${server.id}:${tool.name}`}>
                  {server.name} · {tool.name}
                </option>
              ))}
            </optgroup>
          ))}
          {yours.length ? (
            <optgroup label="Your skills">{yours.map(skillOption)}</optgroup>
          ) : null}
          {shipped.length ? (
            <optgroup label="Skills BOSS ships">{shipped.map(skillOption)}</optgroup>
          ) : null}
          {bindable.mcp_servers.map((server) => {
            const rest = server.tools.filter((tool) => !inAbility.has(`mcp:${server.id}:${tool.name}`));
            return rest.length ? (
              <optgroup key={server.id}
                        label={server.reachable ? server.name : `${server.name} (unreachable)`}>
                {rest.map((tool) => (
                  <option key={tool.name} value={`mcp:${server.id}:${tool.name}`}>
                    {tool.name}
                  </option>
                ))}
              </optgroup>
            ) : null;
          })}
        </select>
        {chosenAbility && notSetUp(chosenAbility) ? (
          <a className="wf-chip" data-tone="red" href={setUpHref(chosenAbility.key)}
             title="Open this ability in Skills & MCP" data-testid="dw-define-setup">
            Not set up · Set up ↗
          </a>
        ) : null}
      </label>
      <div className="field">
        <span>Needs</span>
        <ChipInput value={inputs} onChange={setInputs} label="Needs" placeholder="ticket, severity"
                   testId="dw-define-inputs" />
      </div>
      <div className="field">
        <span>Makes</span>
        <ChipInput value={produces} onChange={setProduces} label="Makes" placeholder="ticket_url"
                   testId="dw-define-produces" />
      </div>
      {/* WHAT THIS STEP KNOWS. Carried into every execution, so the step behaves the same on
          its hundredth run as on the one where somebody was watching. */}
      <label className="field">
        <span>
          What should it know?{" "}
          <HelpTip label="What should it know?" text="Optional. Handed to the skill or tool on every run as context." />
        </span>
        <textarea className="textarea" rows={3} value={knowledge}
                  data-testid="dw-step-knowledge"
                  placeholder="House style, key clauses, links"
                  onChange={(e) => setKnowledge(e.target.value)} />
      </label>
      <label className="field">
        <span>What it does</span>
        <input className="input" value={summary} placeholder="Optional, one line"
               onChange={(e) => setSummary(e.target.value)} />
      </label>

      {/* The consequence of the binding, stated before the step is created rather than at
          publish time when the workflow is already drawn. A line, with the why in a tip. */}
      {chosen ? (
        <div className="detail dw-define-risk" data-testid="dw-define-risk">
          Risk {chosen.risk} · {chosen.what}
          {chosen.risk >= riskFloor ? " · always needs a person" : " · runs on its own"}
          {chosen.unclassified ? (
            <>{" "}<HelpTip label="Unclassified"
                            text="Unclassified: nobody has classified this skill, so it counts as the floor." /></>
          ) : null}
        </div>
      ) : null}
      {blocked ? (
        <div className="detail dw-define-blocked" data-testid="dw-define-blocked"
             title="A workflow using it won't publish until the binding is reviewed.">
          Untrusted · won&apos;t publish until reviewed
        </div>
      ) : null}
      {failure ? <div className="detail dw-define-error" role="alert" data-testid="dw-define-error">{failure}</div> : null}

      <div className="dw-define-actions">
        <GatedButton permission="workflow:create" type="submit" className="cap-btn" data-testid="dw-define-save"
                disabled={!!busy || !label.trim() || !bindingRef || choices === 0}>
          {editing ? "Save changes" : "Define"}
        </GatedButton>
        <button type="button" className="cap-btn" onClick={onCancel}>Cancel</button>
      </div>
      {choices === 0 ? (
        // Counted in CHOICES, not in servers. A project with three registered MCP servers that
        // are all unreachable has three empty groups in the dropdown and nothing to pick — and
        // saying nothing there reads as a broken control rather than an empty one.
        <div className="detail dw-define-empty" data-testid="dw-define-empty">
          Nothing to bind yet{" "}
          <HelpTip label="Nothing to bind yet"
                   text={unreachable.length
                     ? `${unreachable.join(", ")} registered but not answering. Fix them in Skills & MCP.`
                     : "Register a skill, or connect an MCP server, in Skills & MCP."} />
        </div>
      ) : unreachable.length ? (
        // Said even when there is something else to pick. An unreachable server's tools are its
        // LAST-KNOWN ones (marked "(unreachable)" in the picker), and a step bound to one fails
        // until it answers again.
        <div className="detail dw-define-empty" data-testid="dw-define-unreachable">
          {unreachable.join(", ")} not answering{" "}
          <HelpTip label="Not answering"
                   text="Registered but not answering: tools shown are from the last answer, and steps on them fail until it answers." />
        </div>
      ) : null}
    </form>
  );
}
