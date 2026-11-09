"use client";

import React, {
  useEffect, useLayoutEffect, useMemo, useRef, useState, type Dispatch, type ReactNode,
} from "react";
import {
  Clock, DotsThree, GitBranch, GitFork, GitMerge, ListBullets, Lock, PencilSimple, PuzzlePiece, Repeat,
  Robot, SealCheck, Stack, X,
} from "@phosphor-icons/react";

import { HelpTip } from "../../../help-tip";
import { StepIcon, hasStepIcon } from "../../../step-icons";
import { TITLE_MAX, canRemove, stepView, upstreamOf, wireInputs } from "../../../../lib/workflow-editor-types";
import { blockOfHead, canRemoveBlock, removeRefusal } from "../../../../lib/workflow-blocks";
import type {
  EditorAction, EditorState, FlowLink, Palette, Problem, SpecStep, StepPatch, StepView,
} from "../../../../lib/workflow-editor-types";
import { errorLinkOf } from "../../../../lib/workflow-graph";
import { FAILURE_FIELDS, FailureSettings, hasFailureSettings } from "./failure-settings";
import { AgentCombobox } from "./agent-combobox";
import { ApproverSelect, BlockSettings, FieldError, blockFieldKeys } from "./block-settings";
import { ChipInput } from "./chip-input";
import { Editable } from "./editable";
import { GrantRow } from "./grant-row";
import { InputSourceRow, type Upstream } from "./input-source";
import { Popover } from "./popover";
import { ScopePicker } from "./scope-picker";
import { StepAbility } from "./step-ability";
import { itemLabel, useKitAbilities } from "../../../../lib/use-kit-abilities";
import type { Ability } from "../../../../lib/kit-types";

/** A lacked item's words: a skill's Kit label, else a server's ability (K15). */
function kitLabel(abilities: Ability[] | null, id: string): string {
  const asSkill = itemLabel(abilities, "skill", id);
  return asSkill !== id ? asSkill : itemLabel(abilities, "mcp", id);
}
import {
  AGENTED, APPROVABLE, STEP_ID, approvalMode, approvalOf, blockOf, declaredInputs, followsPolicy,
  forcedApproval, formFields, instructionsOf, isAgentTask, isDeploy, producesOf, scopedMcp,
  scopedSkills, sourceOf, usesOf,
} from "./step-facts";

const TYPE_LABEL: Record<StepView["type"], string> = {
  builtin: "Built-in", agent: "Agent task", custom: "Your step", gate: "Approval gate", wait: "Wait",
  subworkflow: "Run a workflow", parallel: "Parallel", condition: "If / else", join: "Join",
  loop: "Loop", foreach: "For each", other: "Step",
};

const TYPE_ICON: Partial<Record<StepView["type"], typeof Robot>> = {
  agent: Robot, custom: PuzzlePiece, gate: SealCheck, wait: Clock, subworkflow: Stack,
  parallel: GitFork, condition: GitBranch, join: GitMerge, loop: Repeat, foreach: ListBullets,
};

export function TypeIcon({ view, size = 20 }: { view: StepView; size?: number }) {
  if (hasStepIcon(view.icon)) return <StepIcon capability={view.icon} size={size} />;
  const Icon = TYPE_ICON[view.type] ?? PuzzlePiece;
  return <Icon size={size} weight="bold" aria-hidden />;
}

/**
 * An 11px caps label, an `(i)`, an optional header action, then the controls. No paragraphs.
 * The action and the controls are Editable (locked with the drawer); the label and `(i)` are not.
 */
export function Section({ label, tip, action, children, testId }: {
  label: string; tip?: string; action?: ReactNode; children: ReactNode; testId?: string;
}) {
  return (
    <section className="wf-sec" data-testid={testId}>
      <div className="wf-sec-head">
        <h3 className="wf-sec-label">{label}</h3>
        {tip ? <HelpTip label={label} text={tip} /> : null}
        <span className="wf-spacer" />
        {action ? <Editable inline>{action}</Editable> : null}
      </div>
      <Editable>{children}</Editable>
    </section>
  );
}

const BLOCK_TYPES: StepView["type"][] = ["gate", "wait", "subworkflow", "condition", "parallel", "join", "loop",
  "foreach"];

/**
 * One step's properties. Every control dispatches exactly one action, `PATCH_STEP`,
 * `RENAME_ID`, `WIRE` (Auto-wire), so undo, the node and the JSON follow on their own.
 */
export function StepInspector({ state, step, palette, dispatch, projectId, actor, refreshPalette, onClose, onRewire }: {
  state: EditorState;
  step: SpecStep;
  palette: Palette;
  dispatch: Dispatch<EditorAction>;
  projectId: string;
  actor?: string;
  refreshPalette(): Promise<void>;
  onClose(): void;
  /** An arrow pointed elsewhere, through the editor's check (its error path, here). Without it,
   *  REWIRE is dispatched as is. */
  onRewire?(link: FlowLink, target: string | null): void;
}) {
  const view = stepView(step, palette);
  const kitAbilities = useKitAbilities(projectId);
  const patch = (p: StepPatch) => dispatch({ type: "PATCH_STEP", id: step.id, patch: p });
  const problems = state.problems.filter((p) => p.step_id === step.id);
  const inputs = declaredInputs(step, palette);
  const agentTask = isAgentTask(step);
  const captain = typeof step.captain === "string" && step.captain && step.captain !== "human"
    ? step.captain : null;
  const named = captain ? palette.agents.find((a) => a.role_key === captain) : undefined;
  const makes = producesOf(step, palette);

  // WHICH SECTIONS THIS STEP DRAWS, decided once, and used both to render them and to place
  // each problem: a problem whose field has a control here shows on that control; every other
  // one (scope on a built-in step, approval on a gate, a captain on a Wait…) shows at the top,
  // so no problem the server names is ever invisible.
  const shows = {
    agent: AGENTED.includes(view.type),
    scope: agentTask,
    approval: APPROVABLE.includes(view.type),
    inputs: inputs.length > 0 || makes.length > 0 || agentTask,
    makes: makes.length > 0 || agentTask,
    settings: BLOCK_TYPES.includes(view.type),
  };
  const inline = new Set<string>(["title", "id", ...(hasFailureSettings(step) ? FAILURE_FIELDS : [])]);
  if (shows.agent) inline.add("captain");
  if (shows.scope) ["instructions", "skills", "mcp"].forEach((f) => inline.add(f));
  if (shows.approval) inline.add("approval");
  // Every declared input is drawn when it has a problem, optional or not (InputsSection).
  if (shows.inputs) inputs.forEach((i) => inline.add(i.name));
  if (shows.inputs && shows.makes) inline.add("produces");
  if (shows.settings) blockFieldKeys(view, blockOf(step, palette)).forEach((f) => inline.add(f));
  const topProblems = problems.filter((p) => !p.field || !inline.has(p.field));

  return (
    <div className="wf-insp-step" data-testid={`wf-inspector-${step.id}`}>
      <StepHeader step={step} view={view} state={state} dispatch={dispatch} problems={problems}
                  onClose={onClose} />

      {topProblems.length ? (
        <ul className="wf-problems" role="alert" data-testid="wf-step-problems">
          {topProblems.map((p, i) => <li key={i} title={p.message}>{p.message}</li>)}
        </ul>
      ) : null}

      {shows.agent ? (
        <Section label="Agent" tip="Who runs this step." testId="wf-sec-agent">
          <AgentCombobox step={step} view={view} palette={palette}
                         onChange={(next) => patch({ captain: next })} />
          <FieldError problems={problems} field="captain" />
          {captain && named ? (
            <GrantRow projectId={projectId} actor={actor} roleKey={captain}
                      agentName={named.name || captain} lacks={view.badges.lacks}
                      labelOf={(id) => kitLabel(kitAbilities, id)}
                      onGranted={refreshPalette} />
          ) : null}
        </Section>
      ) : null}

      {agentTask ? <TaskSection step={step} problems={problems} onPatch={patch} /> : null}

      {shows.scope ? (
        <Section label="Skills" tip="Limits what the agent may use here. Never grants." testId="wf-sec-scope">
          <ScopePicker kind="skill" selected={scopedSkills(step)} captain={captain} palette={palette}
                       projectId={projectId} onChange={(skills) => patch({ skills })}
                       problem={problems.find((p) => p.field === "skills")?.message ?? null} />
          <div className="wf-sub-label">MCP</div>
          <ScopePicker kind="mcp" selected={scopedMcp(step)} captain={captain} palette={palette}
                       projectId={projectId} onChange={(mcp) => patch({ mcp })}
                       problem={problems.find((p) => p.field === "mcp")?.message ?? null} />
        </Section>
      ) : usesOf(step, palette).length ? (
        <Section label="Uses" tip="Fixed by this step. Use an Agent task to choose." testId="wf-sec-uses">
          <div className="wf-chips">
            {usesOf(step, palette).map((u) => <span key={u} className="wf-chip wf-chip-ro" title={u}>{kitLabel(kitAbilities, u)}</span>)}
            <StepAbility step={step} projectId={projectId} />
          </div>
          <FieldError problems={problems} field="ability" />
        </Section>
      ) : null}

      {step.binding?.capability === "deploy" ? (
        <DeployWhere step={step} projectId={projectId} onPatch={patch} />
      ) : null}

      {shows.approval ? (
        <ApprovalSection step={step} palette={palette} problems={problems} onPatch={patch} />
      ) : null}

      {shows.inputs ? (
        <InputsSection step={step} state={state} palette={palette} view={view} problems={problems}
                       showMakes={shows.makes} onPatch={patch}
                       onAutoWire={() => {
                         // The store's own WIRE (which also marks the `auto` chips), sent only
                         // when it will wire something, so "Nothing to wire" can say so.
                         if (!wireInputs(state.spec, palette, [step.id]).wired.length) return 0;
                         dispatch({ type: "WIRE", palette, ids: [step.id] });
                         return 1;
                       }} />
      ) : null}

      {shows.settings ? (
        <Section label="Settings" testId="wf-sec-settings">
          <BlockSettings step={step} view={view} block={blockOf(step, palette)} palette={palette}
                         projectId={projectId} workflowKey={state.meta.workflow_key}
                         problems={problems} onConfig={(config) => patch({ config })}
                         structure={{
                           spec: state.spec,
                           onSelect: (id) => dispatch({ type: "SELECT", id }),
                           onAddBranch: () => dispatch({ type: "ADD_BRANCH", id: step.id }),
                           onRemoveBranch: (branch) => dispatch({ type: "REMOVE_BRANCH", id: step.id, branch }),
                         }} />
        </Section>
      ) : null}

      {hasFailureSettings(step) ? (
        <FailureSettings step={step} spec={state.spec} palette={palette} problems={problems} onPatch={patch}
                         onErrorPath={(target) => {
                           const link = errorLinkOf(state.spec, step.id);
                           if (onRewire) onRewire(link, target);
                           else dispatch({ type: "REWIRE", link, target });
                         }} />
      ) : null}
    </div>
  );
}

/**
 * The title box's text, as typed. The store keeps a title trimmed (S1 `applyStepPatch`), so a
 * box bound to `step.title` loses a trailing space before the next key arrives and "Build it"
 * becomes "Buildit". The box keeps the raw text; every key still dispatches (the node and JSON
 * follow as you type, and Ctrl+S saves what is shown), and the box only takes the stored title
 * back when it changes underneath, undo, a JSON edit, or when the box is left.
 */
function useTitleDraft(step: SpecStep, dispatch: Dispatch<EditorAction>) {
  const stored = typeof step.title === "string" ? step.title : "";
  const [value, setValue] = useState(stored);
  const asStored = (text: string) => text.trim().slice(0, TITLE_MAX);
  useEffect(() => { setValue((typed) => (asStored(typed) === stored ? typed : stored)); }, [stored]);
  return {
    value,
    change(text: string) {
      setValue(text);
      dispatch({ type: "PATCH_STEP", id: step.id, patch: { title: text } });
    },
    settle() { setValue(stored); },
  };
}

/**
 * Sizes a title box to the text it SHOWS: its value, or, for an untitled step, the label it
 * shows as a placeholder. A browser leaves a wrapping placeholder out of `scrollHeight`, so the
 * box is measured holding the label as its value for that moment, then given back its own.
 */
export function fitTitleBox(el: HTMLTextAreaElement): void {
  const own = el.value;
  const shown = own || el.placeholder;
  if (shown !== own) el.value = shown;
  el.style.height = "auto";
  const height = el.scrollHeight;
  if (shown !== own) el.value = own;
  if (height) el.style.height = `${height}px`;
}

/**
 * The step's title, editable in place. A textarea, not an input, so a long title WRAPS onto a
 * second line instead of being cut off; it grows to fit (the built-in label of an untitled step
 * too), and the stylesheet clamps it at two lines. It re-fits when its width changes: the drawer
 * goes full width on a narrow screen. It is still one line of text: Enter leaves the box, and a
 * pasted line break is a space.
 */
function TitleBox({ value, placeholder, onChange, onBlur }: {
  value: string; placeholder: string; onChange(text: string): void; onBlur(): void;
}) {
  const box = useRef<HTMLTextAreaElement>(null);
  useLayoutEffect(() => {
    if (box.current) fitTitleBox(box.current);
  }, [value, placeholder]);
  useEffect(() => {
    const el = box.current;
    if (!el) return;
    let width = el.clientWidth;
    const refit = () => {
      if (el.clientWidth === width) return; // a height change of our own is not a reason
      width = el.clientWidth;
      fitTitleBox(el);
    };
    if (typeof ResizeObserver !== "undefined") {
      const watch = new ResizeObserver(refit);
      watch.observe(el);
      return () => watch.disconnect();
    }
    window.addEventListener("resize", refit);
    return () => window.removeEventListener("resize", refit);
  }, []);
  return (
    <textarea ref={box} className="wf-insp-title" rows={1} value={value} placeholder={placeholder}
              maxLength={TITLE_MAX} aria-label="Step title" data-testid="wf-step-title" title={value || placeholder}
              onChange={(e) => onChange(e.target.value.replace(/[\r\n]+/g, " "))}
              onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); e.currentTarget.blur(); } }}
              onBlur={onBlur} />
  );
}

function StepHeader({ step, view, state, dispatch, problems, onClose }: {
  step: SpecStep; view: StepView; state: EditorState; dispatch: Dispatch<EditorAction>;
  problems: Problem[]; onClose(): void;
}) {
  const [editingId, setEditingId] = useState(false);
  const title = useTitleDraft(step, dispatch);
  const [idDraft, setIdDraft] = useState(step.id);
  const [idError, setIdError] = useState("");
  const [menu, setMenu] = useState(false);
  const menuBtn = useRef<HTMLButtonElement>(null);
  useEffect(() => { setIdDraft(step.id); setIdError(""); setEditingId(false); }, [step.id]);

  const commitId = () => {
    const next = idDraft.trim();
    if (next === step.id) { setEditingId(false); return; }
    if (!STEP_ID.test(next)) { setIdError("Letters, numbers, . _ - only"); return; }
    if (state.spec.steps.some((s) => s.id === next)) { setIdError("Id in use"); return; }
    dispatch({ type: "RENAME_ID", from: step.id, to: next });
    setEditingId(false);
  };

  return (
    <header className="wf-insp-head">
      <span className="wf-insp-icon" data-type={view.type}><TypeIcon view={view} /></span>
      <div className="wf-insp-names">
        <Editable>
          <TitleBox value={title.value} placeholder={view.title}
                    onChange={title.change} onBlur={title.settle} />
        </Editable>
        <span className="wf-insp-id">
          <Editable inline>
          {editingId ? (
            <input className="wf-input wf-mono wf-id-input" autoFocus value={idDraft}
                   aria-label="Step id" aria-invalid={idError ? true : undefined}
                   data-testid="wf-step-id-input"
                   onChange={(e) => { setIdDraft(e.target.value); setIdError(""); }}
                   onBlur={commitId}
                   onKeyDown={(e) => {
                     if (e.key === "Enter") { e.preventDefault(); commitId(); }
                     if (e.key === "Escape") { e.stopPropagation(); setIdDraft(step.id); setEditingId(false); }
                   }} />
          ) : (
            <>
              <span className="wf-mono wf-faint" data-testid="wf-step-id">{step.id}</span>
              <button type="button" className="wf-icon-btn" aria-label="Rename step id"
                      title="Rename id" onClick={() => setEditingId(true)}>
                <PencilSimple size={12} weight="bold" />
              </button>
            </>
          )}
          </Editable>
          {editingId ? null : <HelpTip label="Step id" text="Used in JSON and run history" />}
          {/* The type rides the id line, so the title has the whole row. */}
          <span className="wf-chip wf-type-chip" data-testid="wf-step-type">{TYPE_LABEL[view.type]}</span>
          {idError ? <span className="wf-field-error" role="alert">{idError}</span> : null}
        </span>
        <FieldError problems={problems} field="title" />
        <FieldError problems={problems} field="id" />
      </div>
      <Editable inline>
        <button ref={menuBtn} type="button" className="wf-icon-btn" aria-label="Step actions"
                aria-haspopup="menu" aria-expanded={menu} onClick={() => setMenu((m) => !m)}>
          <DotsThree size={18} weight="bold" />
        </button>
      </Editable>
      <button type="button" className="wf-icon-btn" aria-label="Close" title="Close (Esc)"
              data-testid="wf-inspector-close" onClick={onClose}>
        <X size={16} weight="bold" />
      </button>
      <Popover anchor={menuBtn} open={menu} onClose={() => setMenu(false)} label="Step actions"
               width={180} role="menu">
        <button type="button" role="menuitem" className="wf-menu-item"
                onClick={() => { setMenu(false); dispatch({ type: "DUPLICATE", id: step.id }); }}>Duplicate</button>
        {(() => {
          const spec = state.spec;
          const removable = canRemove(spec, step.id) || canRemoveBlock(spec, step.id);
          return (
            <button type="button" role="menuitem" className="wf-menu-item wf-red" data-testid="wf-step-remove"
                    aria-disabled={!removable || undefined}
                    title={removable ? undefined : removeRefusal(spec, step.id)}
                    onClick={() => { if (!removable) return; setMenu(false); dispatch({ type: "REMOVE", id: step.id }); }}>
              {blockOfHead(step) && !canRemove(spec, step.id) ? "Remove with its paths" : "Remove"}
            </button>
          );
        })()}
      </Popover>
    </header>
  );
}

function TaskSection({ step, problems, onPatch }: {
  step: SpecStep; problems: Problem[]; onPatch(p: StepPatch): void;
}) {
  const task = instructionsOf(step);
  // The last fixed text survives a flip to "Ask at start" and back, within this session.
  const kept = useRef(task.text);
  if (task.mode === "fixed") kept.current = task.text;
  return (
    <Section label="Task" testId="wf-sec-task">
      <span className="wf-seg" role="group" aria-label="Task">
        <button type="button" aria-pressed={task.mode === "ask"} data-testid="wf-task-ask"
                title="Asked for when someone starts a run"
                onClick={() => task.mode !== "ask" && onPatch({ instructions: { mode: "ask" } })}>Ask at start</button>
        <button type="button" aria-pressed={task.mode === "fixed"} data-testid="wf-task-fixed"
                onClick={() => task.mode !== "fixed" && onPatch({ instructions: { mode: "fixed", text: kept.current } })}>Fixed</button>
      </span>
      {task.mode === "fixed" ? (
        <textarea className="wf-text" rows={4} value={task.text} placeholder="What should the agent do?"
                  aria-label="Task" data-testid="wf-task-text"
                  onChange={(e) => onPatch({ instructions: { mode: "fixed", text: e.target.value } })} />
      ) : null}
      <FieldError problems={problems} field="instructions" />
    </Section>
  );
}

function ApprovalSection({ step, palette, problems, onPatch }: {
  step: SpecStep; palette: Palette; problems: Problem[]; onPatch(p: StepPatch): void;
}) {
  const { forced, risk } = forcedApproval(step, palette);
  const on = forced || approvalMode(step) === "manual";
  const approval = approvalOf(step);
  const [more, setMore] = useState(false);
  const onTimeout = String(approval.on_timeout ?? "block");
  const sla = approval.sla_hours;
  // A DEPLOY'S PROMISE IS ITS OWN SETTING (deploy_policy): a person, whatever the environment's
  // policy, or, ticked, the policy decides. The lock says which, so it never promises a person
  // a policy would replace.
  const deploy = isDeploy(step);
  const byPolicy = followsPolicy(step);
  const lockTitle = !forced ? undefined
    : byPolicy ? "Asks a person unless this environment's deploy policy ships without asking"
    : deploy ? `Risk ${risk}, always needs a person, whatever the environment's deploy policy`
    : `Risk ${risk}, always needs a person`;
  return (
    <Section label="Approval" testId="wf-sec-approval">
      <div className="wf-switch-row" title={lockTitle}
             data-testid="wf-approval-row">
        <button type="button" role="switch" aria-checked={on} className="wf-switch"
                disabled={forced} data-testid="wf-approval-switch" aria-label="Needs approval"
                title={lockTitle}
                onClick={() => onPatch({ approval: on ? "auto" : "manual" })}>
          <span className="wf-switch-knob" />
        </button>
        <span>Needs approval</span>
        {forced ? <Lock size={13} weight="bold" className="wf-faint" aria-label="Locked" /> : null}
        <span className="wf-spacer" />
        {on ? (
          <button type="button" className="wf-link" aria-expanded={more}
                  onClick={() => setMore((m) => !m)}>More</button>
        ) : null}
      </div>
      {deploy ? (
        <label className="wf-switch-row" data-testid="wf-approval-policy"
               title="Ticked, the environment's deploy policy (Infra) may ship this without asking, and the plan says so. Unticked, a person approves it, whatever the policy.">
          <input type="checkbox" checked={byPolicy} data-testid="wf-approval-policy-box"
                 onChange={(e) => onPatch({ approvalMore: { follows_policy: e.target.checked ? true : "" } })} />
          <span>Let the environment&apos;s deploy policy decide</span>
        </label>
      ) : null}
      <FieldError problems={problems} field="approval" />
      {on && more ? (
        <div className="wf-approval-more" data-testid="wf-approval-more">
          <label className="wf-field">
            <span className="wf-label">Approver</span>
            <ApproverSelect value={String(approval.approver_role ?? "")} palette={palette}
                            onChange={(v) => onPatch({ approvalMore: { approver_role: v } })} />
          </label>
          <label className="wf-field">
            <span className="wf-label">If no answer</span>
            <select className="wf-select" value={onTimeout} data-testid="wf-approval-timeout"
                    onChange={(e) => onPatch({ approvalMore: { on_timeout: e.target.value } })}>
              <option value="block">Wait</option>
              <option value="escalate">Escalate</option>
              <option value="approve" disabled={risk >= 2}
                      title={risk >= 2 ? `Risk ${risk}, always needs a person` : undefined}>Approve</option>
            </select>
          </label>
          <label className="wf-field">
            <span className="wf-label">Within</span>
            <span className="wf-row">
              <input className="wf-input wf-num" type="number" min={1} max={720}
                     value={sla === undefined || sla === null ? "" : String(sla)} placeholder="—"
                     aria-label="Within hours"
                     onChange={(e) => onPatch({ approvalMore: {
                       sla_hours: e.target.value === "" ? null : Number(e.target.value) } })} />
              <span className="wf-faint">h</span>
            </span>
          </label>
        </div>
      ) : null}
    </Section>
  );
}

type FleetHost = { id: string; name: string; address?: string; status?: string };
const ASK = "";

/**
 * A DEPLOY STEP ASKS ONLY WHERE: a machine from the Fleet, or "ask me at deploy time". No target,
 * manifest, port or URL, the app's first deploy creates its target on that machine (a free port,
 * a URL and health check from the host), and the deploy plan shows them with an Edit.
 */
export function DeployWhere({ step, projectId, onPatch }: {
  step: SpecStep; projectId: string; onPatch(p: StepPatch): void;
}) {
  const [hosts, setHosts] = useState<FleetHost[] | null>(null);
  useEffect(() => {
    let live = true;
    fetch(`/v1/deployment-hosts?project_id=${encodeURIComponent(projectId)}`, { cache: "no-store" })
      .then((r) => (r.ok ? r.json() : null))
      .then((body) => {
        if (!live) return;
        const rows = Array.isArray(body) ? body : Array.isArray(body?.hosts) ? body.hosts : [];
        setHosts(rows.filter((h: FleetHost) => h && h.id && h.status !== "retired"));
      })
      .catch(() => { if (live) setHosts([]); });
    return () => { live = false; };
  }, [projectId]);
  const src = sourceOf(step, "host_id");
  const chosen = src && "value" in src ? String(src.value ?? "") : ASK;
  const verified = (hosts ?? []).filter((h) => ["verified", "ready", "active"].includes(String(h.status)));
  const tip = "The app's first deploy creates its place on this machine: a free port, its URL and a health check. "
    + "The plan shows them before anything ships.";
  return (
    <Section label="Where" tip={tip} testId="wf-sec-deploy-where">
      <select className="wf-input" value={chosen} data-testid="wf-deploy-where" aria-label="Where it deploys"
              onChange={(e) => onPatch({ input: { name: "host_id", source: e.target.value === ASK ? null : { value: e.target.value } } })}>
        <option value={ASK}>{verified.length === 1 ? `${verified[0].name} (the only machine)` : "Ask me at deploy time"}</option>
        {verified.map((h) => <option key={h.id} value={h.id}>{h.name}{h.address ? `, ${h.address}` : ""}</option>)}
      </select>
      {hosts && !verified.length ? (
        <span className="wf-faint" data-testid="wf-deploy-where-none">
          No machine yet. <a href="/?area=infra&view=fleet&add=host" target="_blank" rel="noreferrer">Add one in Infra</a>; it can be added before this step runs.
        </span>
      ) : null}
    </Section>
  );
}

function InputsSection({ step, state, palette, view, problems, showMakes, onPatch, onAutoWire }: {
  step: SpecStep; state: EditorState; palette: Palette; view: StepView; problems: Problem[];
  showMakes: boolean; onPatch(p: StepPatch): void; onAutoWire(): number;
}) {
  const [showOptional, setShowOptional] = useState(false);
  const [note, setNote] = useState("");
  useEffect(() => { if (!note) return; const t = setTimeout(() => setNote(""), 2500); return () => clearTimeout(t); }, [note]);

  const inputs = declaredInputs(step, palette);
  const upstream: Upstream[] = useMemo(() => {
    const ids = upstreamOf(state.spec, step.id);
    return ids.map((id) => state.spec.steps.find((s) => s.id === id)).filter(Boolean).map((s) => ({
      id: s!.id, title: stepView(s!, palette).title, fields: producesOf(s!, palette),
    }));
  }, [state.spec, step.id, palette]);
  const formNames = formFields(state.spec).map((f) => f.name);
  // An optional input with a problem is drawn, not hidden behind "n optional": the problem
  // shows on it (StepInspector counts on that when it places problems).
  const shown = inputs.filter((i) => i.required || i.extra || sourceOf(step, i.name) !== null
    || problems.some((p) => p.field === i.name));
  const autoWired = state.wired?.[step.id] ?? [];
  const hidden = inputs.filter((i) => !shown.includes(i));
  const makes = producesOf(step, palette);

  return (
    <Section label="Inputs" tip="Where each value comes from at run time." testId="wf-sec-inputs"
             action={inputs.length ? (
               <button type="button" className="wf-link" data-testid="wf-autowire"
                       title="Connect each input to the nearest earlier step that makes it."
                       onClick={() => { if (!onAutoWire()) setNote("Nothing to wire"); }}>Auto-wire</button>
             ) : undefined}>
      {note ? <span className="wf-faint wf-note" role="status">{note}</span> : null}
      {[...shown, ...(showOptional ? hidden : [])].map((input) => (
        <React.Fragment key={input.name}>
          <InputSourceRow name={input.name} required={input.required}
                          source={sourceOf(step, input.name)} upstream={upstream} formNames={formNames}
                          auto={autoWired.includes(input.name)}
                          unwired={view.badges.unwired.includes(input.name)}
                          onChange={(source) => onPatch({ input: { name: input.name, source } })} />
          <FieldError problems={problems} field={input.name} />
        </React.Fragment>
      ))}
      {hidden.length && !showOptional ? (
        <button type="button" className="wf-link" data-testid="wf-inputs-optional"
                onClick={() => setShowOptional(true)}>{hidden.length} optional</button>
      ) : null}
      {showMakes ? (
        <div className="wf-makes">
          <div className="wf-sub-label">Makes</div>
          {isAgentTask(step) ? (
            <ChipInput value={makes} label="Makes" placeholder="field name" testId="wf-makes"
                       onChange={(produces) => onPatch({ produces })} />
          ) : (
            <div className="wf-chips">
              {makes.map((m) => <span key={m} className="wf-chip wf-chip-ro wf-mono">{m}</span>)}
            </div>
          )}
          <FieldError problems={problems} field="produces" />
        </div>
      ) : null}
    </Section>
  );
}
