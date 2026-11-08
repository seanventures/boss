"use client";

/**
 * ADD A PROJECT › WHAT BOSS WILL DO (new-project-v2 a-add-project.html §4, b-adjust.html): six
 * boxes — Captain · Stack · Connectors · Workflow · Code · Staging — each pre-picked from real
 * data, each opening its popover in place (Esc or Done closes it, the box shows the new value).
 * Under them the workflow's step strip (approvals amber) and the chips: Name · Tell · More
 * detail · Map business rules. No popover is required to start.
 *
 * Every choice here is state the sheet owns (`PlanState`); this file draws it and edits it.
 */
import React, { useEffect, useRef, useState } from "react";
import {
  ArrowSquareOut, BellRinging, CaretDown, Check, CheckSquare, Envelope, GitBranch, Globe, HardDrives,
  LockKey, PencilSimple, Plus, PuzzlePiece, Square, Trash, X, CreditCard, PaypalLogo, Hand,
} from "@phosphor-icons/react";

import { AgentAvatar } from "../agent-avatar";
import type { TemplateCard } from "../../lib/templates";
import type { StackAdapter } from "../../lib/templates";
import { workflowInputFields, type WorkflowInputField } from "../../lib/workflow-inputs";
import { WIZARD_COLLECTS } from "../workflow-readiness-ui";
import { hostPort, type Candidate, type Host, type Kind, type Pick, type Preview, type Workflow } from "../../lib/add-project";
import type { RegistryRepo } from "../../lib/multirepo";

export type BoxKey = "captain" | "stack" | "connectors" | "workflow" | "code" | "staging" | "tell" | "more";

export type PlanState = {
  captain: string;            // role key
  crew: string[] | null;      // null = the default crew
  stack: string;              // adapter id, "" = Auto (BOSS picks from the brief)
  connectors: string[];       // template keys, in the order picked
  secrets: Record<string, string>;
  workflow: string;           // workflow key
  inputs: Record<string, string>;
  layout: "one" | "several";
  repoIds: string[];          // idea: an existing repo (or several); repo: the picked one
  staging: { host_id: string; port: number | null } | null;
  stagingTouched: boolean;
  name: string;
  named: boolean;
  tell: string;               // "" = you · email · in-app
  when: string[];
  audience: string;
  doneWhen: string[];
  readFirst: string[];
  talksTo: string[];
  mapRules: boolean;
  /** The hosts the connectors' app talks to, allowed out loud (the templates' consent). */
  egressOk: boolean;
};

export const WHEN = [
  { key: "needs_you", label: "Needs you" }, { key: "pr_ready", label: "PR ready" },
  { key: "deployed", label: "Deployed" }, { key: "each_step", label: "Each step" },
] as const;

const CX_ICON: Record<string, React.ComponentType<{ size?: number; "aria-hidden"?: boolean }>> = {
  "login-oauth": LockKey, "stripe-checkout": CreditCard, "paypal-checkout": PaypalLogo, "transactional-email": Envelope,
};

/** "+ 4" on the Connectors box: how many more fit the stack, beyond those picked. */
function cxIcon(key: string) {
  const I = CX_ICON[key] ?? PuzzlePiece;
  return <I size={12} aria-hidden />;
}

/** A connector's chip label: "Login (Google, GitHub or BOSS)" → "Login", "Email (SMTP or Resend)" → "Email". */
export function shortName(name: string): string {
  return name.replace(/^Login with /, "").split(/[,(]/)[0].trim() || name;
}

/** Every host the picked connectors' app talks to, once each. */
export function egressOf(cards: TemplateCard[]): string[] {
  return [...new Set(cards.flatMap((c) => c.egress))];
}

/** The workflow's own start form (b-adjust note 3): its declared fields, less what this screen
 *  already collects (title, directive, repository, criteria, links) and Who to tell. */
export function ownFields(w: Workflow | null | undefined): WorkflowInputField[] {
  return workflowInputFields(w?.input_schema).filter((f) => !WIZARD_COLLECTS.has(f.name) && f.name !== "notify_channel");
}

/** The box a click lands on, one open at a time; Esc closes it. */
function usePopover(): [BoxKey | null, (k: BoxKey | null) => void] {
  const [open, setOpen] = useState<BoxKey | null>(null);
  useEffect(() => {
    if (!open) return undefined;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") { e.stopPropagation(); setOpen(null); } };
    document.addEventListener("keydown", onKey, true);
    return () => document.removeEventListener("keydown", onKey, true);
  }, [open]);
  return [open, setOpen];
}

function Pop({ title, icon, anchor, children, onDone, foot, testid, wide }: {
  title: string; icon: React.ReactNode; anchor: string; children: React.ReactNode; onDone: () => void;
  foot?: React.ReactNode; testid: string; wide?: boolean;
}) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => { ref.current?.querySelector<HTMLElement>("button, input, select, textarea")?.focus(); }, []);
  return (
    <div className="npx-pop" role="dialog" aria-label={title} data-testid={`ap-pop-${testid}`} data-wide={wide || undefined} ref={ref}>
      <div className="npx-pop-h"><span className="npx-pop-ico" aria-hidden>{icon}</span><b>{title}</b>
        <span className="npx-pop-anchor">↑ {anchor}</span>
        <button type="button" className="npx-x" aria-label="Close" onClick={onDone}><X size={13} aria-hidden /></button></div>
      <div className="npx-pop-b">{children}</div>
      <div className="npx-pop-f">{foot}<span className="npx-spacer" />
        <button type="button" className="npx-btn" data-testid={`ap-pop-${testid}-done`} onClick={onDone}>Done</button></div>
    </div>
  );
}

/** The step strip: every step, numbered; where a person approves, ✋ in amber. */
export function StepStrip({ w, testid = "ap-strip" }: { w: Workflow | null; testid?: string }) {
  if (!w) return null;
  const you = w.step_list.filter((s) => s.you).map((s) => s.short || s.label.toLowerCase());
  return (
    <div className="npx-strip" data-testid={testid} title={`${w.name}: every step; amber = you approve`}>
      <span className="npx-lbl">Steps</span>
      <ol className="npx-st" aria-label={`${w.name} steps`}>
        {w.step_list.map((s, i) => (
          <li key={`${s.id}-${i}`} data-you={s.you || undefined} title={`${s.label}${s.you ? " · you approve" : s.agent?.name ? ` · ${s.agent.name}` : ""}`}>
            {s.you ? <Hand size={10} weight="fill" aria-label="you approve" /> : i + 1}
          </li>
        ))}
      </ol>
      <span className="npx-st-legend"><span><i aria-hidden />agents</span>
        {w.approvals ? <span><i data-you aria-hidden />you approve: {[...new Set(you)].slice(0, 4).join(" · ")}</span> : <span>no approvals</span>}</span>
    </div>
  );
}

export function PlanBoxes({ kind, preview, plan, set, cards, adapters, helmPicks, workflow, registry, repoLabel, codeName = "", live, accountLogin, homeHref }: {
  kind: Kind; preview: Preview | null; plan: PlanState; set: (patch: Partial<PlanState>) => void;
  cards: TemplateCard[] | null; adapters: StackAdapter[]; helmPicks: { stack: Pick | null; connectors: Pick[]; workflow: Pick | null };
  workflow: Workflow | null; registry: RegistryRepo[]; repoLabel: { v: string; s: string };
  /** The picked repository's name or the folder's path (the Code box). */
  codeName?: string;
  live: boolean; accountLogin: string; homeHref: (p: Record<string, string>) => string;
}) {
  const [open, setOpen] = usePopover();
  const toggle = (k: BoxKey) => setOpen(open === k ? null : k);
  const cap = preview?.captain;
  const captainOf = (rk: string): Candidate | undefined => cap?.candidates.find((c) => c.role_key === rk);
  const capName = captainOf(plan.captain)?.name ?? cap?.name ?? "Helm";
  const crew = plan.crew ?? (cap?.squad ?? []).map((m) => m.role_key).filter((r) => r !== plan.captain);
  const adapter = adapters.find((a) => a.id === plan.stack) ?? null;
  const autoStack = helmPicks.stack ? adapters.find((a) => a.id === helmPicks.stack?.key) ?? null : null;
  const stackShown = adapter ?? autoStack;
  const lang = stackShown?.primary ?? null;
  const offered = (cards ?? []).filter((c) => c.kind === "connector" && (!lang || c.stacks.includes(lang)));
  const pickedCards = plan.connectors.map((k) => (cards ?? []).find((c) => c.key === k)).filter((c): c is TemplateCard => !!c);
  const because = (key: string) => helmPicks.connectors.find((p) => p.key === key)?.because;
  const host = preview?.staging.find((h) => h.host_id === plan.staging?.host_id) ?? null;
  const flows = preview?.workflows ?? [];
  const paths = flows.filter((w) => w.path);
  const [allFlows, setAllFlows] = useState(false);
  const [moreSteps, setMoreSteps] = useState(false);
  const [tellDraft, setTellDraft] = useState(plan.tell);
  const [check, setCheck] = useState("");
  const [link, setLink] = useState("");
  const [talk, setTalk] = useState("");
  const [repoQ, setRepoQ] = useState("");
  const fields = ownFields(workflow);
  const secretsNeeded = pickedCards.flatMap((c) => c.secrets.filter((s) => s.testDefault === "none").map((s) => ({ card: c, s })));
  const toggleConnector = (key: string) => {
    const on = plan.connectors.includes(key);
    set({ connectors: on ? plan.connectors.filter((k) => k !== key) : plan.connectors.length >= 6 ? plan.connectors : [...plan.connectors, key] });
  };

  return (
    <>
      <div className="npx-flow" data-testid="ap-flow">
        {/* 1 CAPTAIN */}
        <div className="npx-node-wrap">
          <button type="button" className="npx-node" data-testid="ap-box-captain" aria-expanded={open === "captain"} data-open={open === "captain" || undefined}
                  title="Who takes the project over · change captain or crew" onClick={() => toggle("captain")}>
            <span className="npx-lbl">Captain<CaretDown size={11} aria-hidden /></span>
            <span className="npx-row"><AgentAvatar agent={plan.captain || "engineering_captain"} size={20} aiBadge={false} /><span className="npx-v">{capName}</span></span>
            <span className="npx-faces">{crew.slice(0, 5).map((r) => <AgentAvatar key={r} agent={r} size={18} aiBadge={false} tip={captainOf(r)?.name ?? r} />)}</span>
          </button>
          {open === "captain" ? (
            <Pop title="Captain & crew" icon={<AgentAvatar agent="engineering_captain" size={16} aiBadge={false} />} anchor="Captain" testid="captain" onDone={() => setOpen(null)}
                 foot={<button type="button" className="npx-link" data-testid="ap-crew-reset" onClick={() => set({ captain: "engineering_captain", crew: null })}>Put it back</button>}>
              <div className="npx-list npx-scroll" role="listbox" aria-label="Captain">
                {/* The captain roles first (the API's order), the default one recommended. */}
                {(cap?.candidates ?? []).slice(0, 12).map((c) => (
                  <button key={c.role_key} type="button" role="option" aria-selected={c.role_key === plan.captain} className="npx-opt" data-testid={`ap-captain-${c.role_key}`}
                          onClick={() => set({ captain: c.role_key })}>
                    <AgentAvatar agent={c.role_key} size={20} aiBadge={false} /><b>{c.name}</b><small>{c.role}{c.recommended ? " · default" : ""}</small>
                    {c.role_key === plan.captain ? <Check size={12} weight="bold" aria-hidden className="npx-ok" /> : null}
                  </button>
                ))}
              </div>
              <span className="npx-sub">Crew</span>
              <div className="npx-chips" data-testid="ap-crew">
                {crew.map((r) => (
                  <span key={r} className="npx-chip" data-on>
                    <AgentAvatar agent={r} size={16} aiBadge={false} />{captainOf(r)?.name ?? r}
                    <button type="button" aria-label={`Take ${captainOf(r)?.name ?? r} off`} onClick={() => set({ crew: crew.filter((x) => x !== r) })}><X size={10} aria-hidden /></button>
                  </span>
                ))}
                <label className="npx-chip" data-dashed>
                  <Plus size={10} aria-hidden />
                  <select aria-label="Add an agent" data-testid="ap-crew-add" value="" onChange={(e) => { if (e.target.value) set({ crew: [...crew, e.target.value] }); }}>
                    <option value="">agent</option>
                    {(cap?.candidates ?? []).filter((c) => !crew.includes(c.role_key) && c.role_key !== plan.captain).map((c) => <option key={c.role_key} value={c.role_key}>{c.name} · {c.role}</option>)}
                  </select>
                </label>
              </div>
            </Pop>
          ) : null}
        </div>

        {/* 2 STACK */}
        <div className="npx-node-wrap">
          <button type="button" className="npx-node" data-testid="ap-box-stack" data-t={kind !== "idea" ? "live" : undefined} aria-expanded={open === "stack"} data-open={open === "stack" || undefined}
                  title={kind === "idea" ? "BOSS picks the stack from the brief · change it" : "Read from the repo"} onClick={() => toggle("stack")}>
            <span className="npx-lbl">Stack{kind === "idea" ? (plan.stack ? null : <span className="npx-auto">AUTO</span>) : <span className="npx-auto">FOUND</span>}<CaretDown size={11} aria-hidden /></span>
            <span className="npx-v">{kind === "idea" ? (stackShown ? stackShown.label.split(",")[0] + (stackShown.label.includes(",") ? " …" : "") : "BOSS picks") : repoLabel.v || (live ? "Reading…" : "Pick the code")}</span>
            <span className="npx-s" title={helmPicks.stack?.because ? `Helm: ${helmPicks.stack.because}` : undefined}>
              {kind === "idea" ? (stackShown ? `${plan.stack ? "your pick" : "from the brief"} · ${stackShown.languages.map((l) => l[0].toUpperCase() + l.slice(1)).join(" · ")}` : "from the brief, at the plan") : repoLabel.s}</span>
          </button>
          {open === "stack" ? (
            <Pop title="Stack" icon={<GitBranch size={14} />} anchor="Stack" testid="stack" onDone={() => setOpen(null)}
                 foot={<span className="npx-faint">Warnings come from the stack check</span>}>
              <div className="npx-list" role="listbox" aria-label="Stack">
                <button type="button" role="option" aria-selected={!plan.stack} className="npx-opt" data-testid="ap-stack-auto" onClick={() => set({ stack: "" })}>
                  <b>Auto</b><small>BOSS picks from the brief{autoStack ? ` → ${autoStack.label}` : ""}</small>
                </button>
                {adapters.map((a) => (
                  <button key={a.id} type="button" role="option" aria-selected={plan.stack === a.id} className="npx-opt" data-testid={`ap-stack-${a.id}`}
                          onClick={() => set({ stack: a.id })} title={a.ready ? `${a.label} · ${a.level}` : "This install has no toolchain for it yet"}>
                    <b>{a.label}</b><span className="npx-tag" data-tone={a.ready ? "green" : "amber"}>{a.ready ? "ready" : "no toolchain"}</span>
                  </button>
                ))}
              </div>
            </Pop>
          ) : null}
        </div>

        {/* 3 CONNECTORS */}
        <div className="npx-node-wrap">
          <button type="button" className="npx-node" data-testid="ap-box-connectors" aria-expanded={open === "connectors"} data-open={open === "connectors" || undefined}
                  title="Connectors suggested from the brief (or found in the repo) · add or drop" onClick={() => toggle("connectors")}>
            <span className="npx-lbl">Connectors<CaretDown size={11} aria-hidden /></span>
            <span className="npx-cx">
              {pickedCards.slice(0, 2).map((c) => <span key={c.key} data-on title={because(c.key) ? `“${because(c.key)}”` : c.summary}>{cxIcon(c.key)}{shortName(c.name)}</span>)}
              {pickedCards.length > 2 ? <span data-on>+{pickedCards.length - 2}</span> : null}
              {offered.length - pickedCards.length > 0 ? <span data-add>{pickedCards.length ? `+ ${offered.length - pickedCards.length}` : "+ add"}</span> : null}
              {!offered.length && !pickedCards.length ? <span data-add>none fit</span> : null}
            </span>
          </button>
          {open === "connectors" ? (
            <Pop title="Connectors" icon={<PuzzlePiece size={14} />} anchor="Connectors" testid="connectors" onDone={() => setOpen(null)} wide
                 foot={<a className="npx-link" data-testid="ap-cx-reference" href={homeHref({ area: "skills", tab: "catalogue" })} target="_blank" rel="noreferrer">Build from its reference <ArrowSquareOut size={11} aria-hidden /></a>}>
              <p className="npx-faint">Fit {stackShown ? stackShown.label.split(",")[0] : "any stack"} · up to 6{helmPicks.connectors.length ? <> · <span className="npx-green">✓ suggested from your brief</span></> : null}</p>
              <div className="npx-cn-grid">
                {offered.map((c) => {
                  const on = plan.connectors.includes(c.key);
                  return (
                    <button key={c.key} type="button" className="npx-cn" data-on={on || undefined} aria-pressed={on} data-testid={`ap-cx-${c.key}`}
                            title={c.summary} onClick={() => toggleConnector(c.key)}>
                      {cxIcon(c.key)}<span><b>{c.name}</b><small>{because(c.key) ? `“${because(c.key)}”` : c.summary}</small></span>
                      {on ? <Check size={12} weight="bold" className="npx-ok" aria-hidden /> : null}
                    </button>
                  );
                })}
                {!offered.length ? <p className="npx-faint">No connector fits this stack yet.</p> : null}
              </div>
              {egressOf(pickedCards).length ? (
                <div className="npx-secrets" data-testid="ap-cx-egress">
                  <span className="npx-sub">Talks to</span>
                  <span className="npx-chips">{egressOf(pickedCards).map((h) => <span key={h} className="npx-chip npx-mono">{h}</span>)}</span>
                  <label className="npx-field" style={{ flexDirection: "row", alignItems: "center", gap: 6 }}>
                    <input type="checkbox" checked={plan.egressOk} data-testid="ap-egress-ok" onChange={(e) => set({ egressOk: e.target.checked })} />
                    Allow the app to talk to these hosts</label>
                </div>
              ) : null}
              {secretsNeeded.length ? (
                <div className="npx-secrets" data-testid="ap-cx-secrets">
                  <span className="npx-sub">Test keys</span>
                  {secretsNeeded.map(({ card, s }) => (
                    <label key={`${card.key}-${s.name}`} className="npx-field">
                      <span>{s.name} <small className="npx-faint">· {card.name} · {s.purpose}</small></span>
                      <input type="password" autoComplete="off" className="npx-input" data-testid={`ap-secret-${s.name}`} placeholder="test value (write-only)"
                             value={plan.secrets[s.name] ?? ""} onChange={(e) => set({ secrets: { ...plan.secrets, [s.name]: e.target.value } })} />
                    </label>
                  ))}
                </div>
              ) : null}
            </Pop>
          ) : null}
        </div>

        {/* 4 WORKFLOW */}
        <div className="npx-node-wrap">
          <button type="button" className="npx-node" data-t="you" data-testid="ap-box-workflow" aria-expanded={open === "workflow"} data-open={open === "workflow" || undefined}
                  title="The workflow it runs · change it" onClick={() => toggle("workflow")}>
            <span className="npx-lbl">Workflow{kind === "idea" && !plan.workflow ? <span className="npx-auto">AUTO</span> : null}<CaretDown size={11} aria-hidden /></span>
            {kind === "idea" ? (
              <>
                <span className="npx-v">{workflow?.short ?? "…"}</span>
                <span className="npx-s">{workflow ? <>{workflow.steps} steps · <b className="npx-amber">{workflow.approvals} you approve</b></> : "reading"}</span>
              </>
            ) : (
              <>
                <span className="npx-v">Onboard → {workflow?.short ?? "Small"}</span>
                <span className="npx-s">{workflow ? <>then {workflow.steps} steps · <b className="npx-amber">{workflow.approvals} you approve</b></> : "then the first work"}</span>
              </>
            )}
          </button>
          {open === "workflow" ? (
            <Pop title="Workflow" icon={<GitBranch size={14} />} anchor="Workflow" testid="workflow" onDone={() => setOpen(null)} wide
                 foot={<button type="button" className="npx-link" data-testid="ap-wf-all" aria-expanded={allFlows} onClick={() => setAllFlows((v) => !v)}>All installed workflows ▾</button>}>
              <div className="npx-wfp" role="radiogroup" aria-label="Workflow">
                {(allFlows ? flows : paths.length ? paths : flows).map((w) => (
                  <button key={w.key} type="button" role="radio" aria-checked={w.key === workflow?.key} className="npx-wfc" data-testid={`ap-wf-${w.key}`}
                          onClick={() => set({ workflow: w.key })} title={w.detail}>
                    <b>{w.short}{w.key === workflow?.key ? <span className="npx-tag" data-tone="blue">picked</span> : null}
                      {helmPicks.workflow?.key === w.key ? <span className="npx-tag" title={helmPicks.workflow.because}>Helm</span> : null}</b>
                    <span className="npx-mini" aria-hidden>{w.step_list.slice(0, 19).map((s, i) => <i key={i} data-y={s.you || undefined} />)}</span>
                    <small>{w.steps} steps · {w.approvals ? `${w.approvals} you approve` : "no approvals"}{w.approve_words.length ? `: ${w.approve_words.slice(0, 3).join(", ")}` : ""}</small>
                  </button>
                ))}
              </div>
              {workflow ? (
                <details className="npx-steps-l" open data-testid="ap-wf-steps">
                  <summary>Steps &amp; who does each</summary>
                  {(moreSteps ? workflow.step_list : workflow.step_list.slice(0, 6)).map((s, i) => (
                    <div key={`${s.id}-${i}`}><span className="npx-n" data-you={s.you || undefined}>{s.you ? "!" : i + 1}</span><span>{s.label}</span>
                      {s.you ? <span className="npx-you-pill">You approve</span> : s.agent?.role_key ? <AgentAvatar agent={s.agent.role_key} size={16} aiBadge={false} tip={s.agent.name ?? ""} /> : <span />}</div>
                  ))}
                  {workflow.step_list.length > 6 ? <button type="button" className="npx-link" onClick={() => setMoreSteps((v) => !v)}>{moreSteps ? "fewer" : `+ ${workflow.step_list.length - 6} more`}</button> : null}
                </details>
              ) : null}
              {fields.length ? (
                <div className="npx-own-form" data-testid="ap-wf-form">
                  <span className="npx-sub">{workflow?.name}&apos;s start form</span>
                  {fields.map((f) => (
                    <label key={f.name} className="npx-field">
                      <span>{f.label}{f.required ? <em className="npx-amber"> required</em> : null}</span>
                      {f.kind === "boolean" ? (
                        <input type="checkbox" checked={plan.inputs[f.name] === "true"} onChange={(e) => set({ inputs: { ...plan.inputs, [f.name]: e.target.checked ? "true" : "false" } })} />
                      ) : f.kind === "multiline" ? (
                        <textarea className="npx-input" rows={3} placeholder={f.example} value={plan.inputs[f.name] ?? f.default ?? ""} onChange={(e) => set({ inputs: { ...plan.inputs, [f.name]: e.target.value } })} />
                      ) : (
                        <input className="npx-input" placeholder={f.example} value={plan.inputs[f.name] ?? f.default ?? ""} type={f.kind === "number" ? "number" : f.kind === "url" ? "url" : "text"}
                               onChange={(e) => set({ inputs: { ...plan.inputs, [f.name]: e.target.value } })} />
                      )}
                    </label>
                  ))}
                </div>
              ) : null}
            </Pop>
          ) : null}
        </div>

        {/* 5 CODE */}
        <div className="npx-node-wrap">
          <button type="button" className="npx-node" data-testid="ap-box-code" aria-expanded={open === "code"} data-open={open === "code" || undefined}
                  title="Where the code lives · one repo or several" onClick={() => toggle("code")}>
            <span className="npx-lbl">Code<CaretDown size={11} aria-hidden /></span>
            <CodeLabel kind={kind} plan={plan} registry={registry} preview={preview} name={codeName} />
          </button>
          {open === "code" ? (
            <Pop title="Where the code lives" icon={<GitBranch size={14} />} anchor="Code" testid="code" onDone={() => setOpen(null)}>
              {kind === "idea" ? (
                <>
                  <div className="npx-seg" role="tablist" aria-label="Repos">
                    <button type="button" role="tab" aria-selected={plan.layout === "one"} data-testid="ap-code-one" onClick={() => set({ layout: "one", repoIds: plan.repoIds.slice(0, 1) })}>One repo</button>
                    <button type="button" role="tab" aria-selected={plan.layout === "several"} data-testid="ap-code-several" onClick={() => set({ layout: "several" })}>Several repos</button>
                  </div>
                  {plan.layout === "one" ? (
                    <button type="button" className="npx-opt" aria-selected={!plan.repoIds.length} data-testid="ap-code-new" onClick={() => set({ repoIds: [] })}>
                      <Plus size={12} aria-hidden /><b className="npx-mono">{preview?.default_repository ?? "No repo yet"}</b>
                      <small>{preview?.default_repository ? "the install's default · new app folder" : "connect one before the build step"}</small>
                    </button>
                  ) : null}
                  <input className="npx-input" placeholder="Use an existing repo…" value={repoQ} data-testid="ap-code-search" onChange={(e) => setRepoQ(e.target.value)} />
                  <div className="npx-list npx-scroll">
                    {registry.filter((r) => !repoQ || r.full_name.toLowerCase().includes(repoQ.toLowerCase())).slice(0, 8).map((r) => {
                      const on = plan.repoIds.includes(r.id);
                      return (
                        <button key={r.id} type="button" className="npx-opt" aria-selected={on} data-testid={`ap-code-repo-${r.id}`}
                                onClick={() => set({ repoIds: plan.layout === "one" ? [r.id] : on ? plan.repoIds.filter((x) => x !== r.id) : [...plan.repoIds, r.id].slice(0, 6) })}>
                          {plan.layout === "several" ? (on ? <CheckSquare size={13} aria-hidden /> : <Square size={13} aria-hidden />) : <GitBranch size={12} aria-hidden />}
                          <span className="npx-mono">{r.full_name}</span>{(r.used_by ?? []).length ? <span className="npx-tag">in use</span> : null}
                        </button>
                      );
                    })}
                  </div>
                  {plan.layout === "several" ? <p className="npx-faint">Several repos: BOSS maps the app&apos;s parts to them; linked PRs, approved as a set.</p> : null}
                </>
              ) : (
                <p className="npx-faint">{codeName ? <>The {kind === "folder" ? "folder" : "repo"} you picked, <span className="npx-mono">{codeName}</span>: builds land on its branch as pull requests.</> : "Pick a repo above."}</p>
              )}
              <p className="npx-faint">Account <b>{accountLogin || "—"}</b> is global — <a className="npx-link" href={homeHref({ area: "setup", focus: "repository" })}>Repo and models</a></p>
            </Pop>
          ) : null}
        </div>

        {/* 6 STAGING */}
        <div className="npx-node-wrap">
          <button type="button" className="npx-node" data-testid="ap-box-staging" aria-expanded={open === "staging"} data-open={open === "staging" || undefined}
                  title="Staging target suggested from your hosts · change it" onClick={() => toggle("staging")}>
            <span className="npx-lbl">Staging<CaretDown size={11} aria-hidden /></span>
            <span className="npx-v">{host ? hostPort(host.host, host.port) : "Preview only"}</span>
            <span className="npx-s">{host ? (host.suggested && !plan.stagingTouched ? "suggested · free port" : "your pick") : preview?.staging.length ? "no staging yet" : "no verified host yet"}</span>
          </button>
          {open === "staging" ? (
            <Pop title="Staging" icon={<HardDrives size={14} />} anchor="Staging" testid="staging" onDone={() => setOpen(null)}
                 foot={<a className="npx-link" href={homeHref({ area: "infra", add: "host" })}>+ host in Infra</a>}>
              <div className="npx-list" role="radiogroup" aria-label="Staging">
                {(preview?.staging ?? []).map((h: Host) => (
                  <button key={h.host_id} type="button" role="radio" aria-checked={plan.staging?.host_id === h.host_id} className="npx-opt" data-testid={`ap-staging-${h.host_id}`}
                          onClick={() => set({ staging: { host_id: h.host_id, port: h.port }, stagingTouched: true })}>
                    <HardDrives size={13} aria-hidden /><b>{h.host}</b><span className="npx-mono npx-faint">{h.port ? `:${h.port} free` : ""}</span>
                    {h.suggested ? <span className="npx-tag" data-tone="blue">suggested</span> : null}{h.shared ? <span className="npx-tag">shared</span> : null}
                  </button>
                ))}
                <button type="button" role="radio" aria-checked={!plan.staging} className="npx-opt" data-testid="ap-staging-none" onClick={() => set({ staging: null, stagingTouched: true })}>
                  <Globe size={13} aria-hidden /><b>Preview only</b><small>no staging yet</small>
                </button>
              </div>
              {kind === "idea" && plan.staging && !plan.repoIds.length ? <p className="npx-faint">Made once the app has its repo (Onboarding › Staging host).</p> : null}
              <p className="npx-faint"><LockKey size={11} aria-hidden /> Prod: a person picks it later (Deployments)</p>
            </Pop>
          ) : null}
        </div>
      </div>

      <StepStripOrOnboarding kind={kind} w={workflow} live={live} />

      <div className="npx-chips-row">
        <NameChip plan={plan} set={set} />
        <div className="npx-node-wrap">
          <button type="button" className="npx-pchip" data-testid="ap-chip-tell" aria-expanded={open === "tell"} title="Who hears when it needs you or finishes" onClick={() => { setTellDraft(plan.tell); toggle("tell"); }}>
            <BellRinging size={13} aria-hidden />Tell <b>{plan.tell ? plan.tell.replace(/^email:/, "") : "you · email"}</b>
          </button>
          {open === "tell" ? (
            <Pop title="Who to tell" icon={<BellRinging size={14} />} anchor="Tell" testid="tell" onDone={() => { set({ tell: tellDraft.trim() }); setOpen(null); }}>
              <button type="button" className="npx-opt" aria-selected={!tellDraft} onClick={() => setTellDraft("")}><b>You</b><small>email · in-app</small>{!tellDraft ? <Check size={12} weight="bold" className="npx-ok" aria-hidden /> : null}</button>
              <input className="npx-input" data-testid="ap-tell-input" placeholder="team@example.com or #slack-channel" value={tellDraft} onChange={(e) => setTellDraft(e.target.value)} />
              <span className="npx-sub">When</span>
              <div className="npx-chips">
                {WHEN.map((w) => {
                  const on = plan.when.includes(w.key);
                  return <button key={w.key} type="button" className="npx-chip" data-on={on || undefined} aria-pressed={on} data-testid={`ap-when-${w.key}`}
                                 onClick={() => set({ when: on ? plan.when.filter((x) => x !== w.key) : [...plan.when, w.key] })}>{w.label}</button>;
                })}
              </div>
            </Pop>
          ) : null}
        </div>
        <div className="npx-node-wrap">
          <button type="button" className="npx-pchip" data-dashed={!plan.audience && !plan.doneWhen.length && !plan.readFirst.length && !plan.talksTo.length || undefined}
                  data-testid="ap-chip-more" aria-expanded={open === "more"} title="Who it's for · done when · read first · talks to" onClick={() => toggle("more")}>
            <Plus size={12} aria-hidden />More detail{plan.doneWhen.length + plan.readFirst.length + plan.talksTo.length + (plan.audience ? 1 : 0) ? <b>· {plan.doneWhen.length + plan.readFirst.length + plan.talksTo.length + (plan.audience ? 1 : 0)}</b> : null}
          </button>
          {open === "more" ? (
            <Pop title="More detail" icon={<Plus size={14} />} anchor="More detail" testid="more" onDone={() => setOpen(null)}>
              <span className="npx-sub">Who is it for</span>
              <input className="npx-input" data-testid="ap-more-audience" placeholder="A support agent on a laptop, mid-queue" value={plan.audience} onChange={(e) => set({ audience: e.target.value })} />
              <span className="npx-sub">Done when</span>
              {plan.doneWhen.map((c, i) => (
                <div key={i} className="npx-check"><Check size={12} weight="bold" className="npx-ok" aria-hidden /><span>{c}</span>
                  <button type="button" aria-label="Remove" onClick={() => set({ doneWhen: plan.doneWhen.filter((_, j) => j !== i) })}><Trash size={11} aria-hidden /></button></div>
              ))}
              <input className="npx-input" data-testid="ap-more-check" placeholder="+ Add a check, then ↵" value={check} onChange={(e) => setCheck(e.target.value)}
                     onKeyDown={(e) => { if (e.key === "Enter" && check.trim()) { e.preventDefault(); set({ doneWhen: [...plan.doneWhen, check.trim()] }); setCheck(""); } }} />
              <span className="npx-sub">Read first</span>
              <div className="npx-chips">{plan.readFirst.map((l) => <span key={l} className="npx-chip"><Globe size={10} aria-hidden />{l.replace(/^https?:\/\//, "").slice(0, 32)}<button type="button" aria-label="Remove" onClick={() => set({ readFirst: plan.readFirst.filter((x) => x !== l) })}><X size={10} aria-hidden /></button></span>)}</div>
              <input className="npx-input" data-testid="ap-more-link" placeholder="+ link (https://…), then ↵" value={link} onChange={(e) => setLink(e.target.value)}
                     onKeyDown={(e) => { if (e.key === "Enter" && /^https?:\/\//i.test(link.trim())) { e.preventDefault(); set({ readFirst: [...plan.readFirst, link.trim()] }); setLink(""); } }} />
              <span className="npx-sub">Talks to</span>
              <div className="npx-chips">{plan.talksTo.map((l) => <span key={l} className="npx-chip"><PuzzlePiece size={10} aria-hidden />{l}<button type="button" aria-label="Remove" onClick={() => set({ talksTo: plan.talksTo.filter((x) => x !== l) })}><X size={10} aria-hidden /></button></span>)}</div>
              <input className="npx-input" data-testid="ap-more-talks" placeholder="+ app or API it calls, then ↵" value={talk} onChange={(e) => setTalk(e.target.value)}
                     onKeyDown={(e) => { if (e.key === "Enter" && talk.trim()) { e.preventDefault(); set({ talksTo: [...plan.talksTo, talk.trim()] }); setTalk(""); } }} />
            </Pop>
          ) : null}
        </div>
        {kind !== "idea" ? (
          <button type="button" className="npx-pchip" aria-pressed={plan.mapRules} data-on={plan.mapRules || undefined} data-testid="ap-chip-rules"
                  title="Also find the business rules in its code (a knowledge-graph project)" onClick={() => set({ mapRules: !plan.mapRules })}>
            {plan.mapRules ? <CheckSquare size={13} aria-hidden /> : <Square size={13} aria-hidden />}Map business rules
          </button>
        ) : null}
      </div>
    </>
  );
}

function CodeLabel({ kind, plan, registry, preview, name }: { kind: Kind; plan: PlanState; registry: RegistryRepo[]; preview: Preview | null; name: string }) {
  if (kind !== "idea") {
    return <><span className="npx-v npx-mono">{name ? name.split("/").pop() : "—"}</span><span className="npx-s">{kind === "folder" ? "a folder · read-only" : "its branch · PRs"}</span></>;
  }
  const picked = plan.repoIds.map((id) => registry.find((r) => r.id === id)?.full_name).filter(Boolean) as string[];
  if (picked.length > 1) return <><span className="npx-v npx-mono">{picked.length} repos</span><span className="npx-s">linked PRs</span></>;
  if (picked.length === 1) return <><span className="npx-v npx-mono">{picked[0]}</span><span className="npx-s">existing repo · 1 PR</span></>;
  return <><span className="npx-v npx-mono">{preview?.default_repository ?? "No repo yet"}</span><span className="npx-s">{preview?.default_repository ? "new app folder · 1 PR" : "connect before build"}</span></>;
}

/** A repo's THEN strip: the onboarding stages, ending at what you accept (the file count is the
 *  summary's, not a stage). */
function StepStripOrOnboarding({ kind, w, live }: { kind: Kind; w: Workflow | null; live: boolean }) {
  if (kind === "idea") return <StepStrip w={w} />;
  return (
    <div className="npx-strip" data-testid="ap-then" title="Onboarding, then the first work runs on the workflow above">
      <span className="npx-lbl">Then</span>
      <span className="npx-fl">
        <span className="npx-fl-n" data-t="run">{live ? <i className="npx-pulse" aria-hidden /> : null}Discover</span><span className="npx-fl-w" />
        <span className="npx-fl-n">Ready for work</span><span className="npx-fl-w" />
        <span className="npx-fl-n">Backlog</span><span className="npx-fl-w" />
        <span className="npx-fl-n" data-t="you">You accept</span>
      </span>
    </div>
  );
}

function NameChip({ plan, set }: { plan: PlanState; set: (p: Partial<PlanState>) => void }) {
  const [editing, setEditing] = useState(false);
  return editing ? (
    <label className="npx-pchip" data-editing>
      <PencilSimple size={13} aria-hidden />Name
      <input autoFocus className="npx-inline" data-testid="ap-name-input" maxLength={120} value={plan.name}
             onChange={(e) => set({ name: e.target.value, named: true })} onBlur={() => setEditing(false)}
             onKeyDown={(e) => { if (e.key === "Enter" || e.key === "Escape") { e.preventDefault(); e.stopPropagation(); setEditing(false); } }} />
    </label>
  ) : (
    <button type="button" className="npx-pchip" data-testid="ap-chip-name" title="Prefilled from the brief or the repo · edit inline" onClick={() => setEditing(true)}>
      <PencilSimple size={13} aria-hidden />Name <b>{plan.name || "—"}</b>
    </button>
  );
}

