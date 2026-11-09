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
import React, { useRef, useState } from "react";
import {
  ArrowSquareOut, BellRinging, CaretDown, Check, CheckSquare, Envelope, FolderPlus, GitBranch, GithubLogo, Globe, HardDrives,
  LockKey, PencilSimple, Plus, PuzzlePiece, Square, Trash, X, CreditCard, PaypalLogo, Hand,
} from "@phosphor-icons/react";

import { AgentAvatar } from "../agent-avatar";
import { useBoxPop } from "../../components/box-pop";
import type { TemplateCard } from "../../lib/templates";
import type { StackAdapter } from "../../lib/templates";
import { workflowInputFields, type WorkflowInputField } from "../../lib/workflow-inputs";
import { WIZARD_COLLECTS } from "../workflow-readiness-ui";
import { hostPort, repoCheckFix, type Candidate, type Host, type Kind, type Pick, type Preview, type RepoCheck, type Where, type Workflow } from "../../lib/add-project";
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
  /** WHERE A NEW APP'S CODE LIVES (BOSSAPP-917/918): this machine, a new GitHub repo, or a
   *  folder in a repo you have (repoIds, `layout`). */
  where: Where;
  /** The new GitHub repo: owner (an account BOSS can reach), name ("" = from the project's
   *  name), private by default. */
  newRepo: { owner: string; name: string; private: boolean };
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

/** Helm's reason, in quotes once: a reason that already quotes the person's words
 *  (`"staff sign in with Google" needs …`) is shown as written, never double-quoted. */
export function quoted(why: string): string {
  const w = why.trim();
  return /["“”]/.test(w) ? w : `“${w}”`;
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

/** The box a click lands on, one open at a time; Esc closes it (useBoxPop, in Pop). */
function usePopover(): [BoxKey | null, (k: BoxKey | null) => void] {
  return useState<BoxKey | null>(null);
}

function Pop({ title, icon, anchor, children, onDone, onEscape, foot, testid, wide }: {
  title: string; icon: React.ReactNode; anchor: string; children: React.ReactNode; onDone: () => void;
  /** Esc: close without Done's save (default: Done). */
  onEscape?: () => void;
  foot?: React.ReactNode; testid: string; wide?: boolean;
}) {
  const ref = useRef<HTMLDivElement>(null);
  // Whole in view, focus in, Esc out, focus back to its box: one rule for every box popover.
  useBoxPop(ref, { onEscape: onEscape ?? onDone });
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

/** What the sheet does with a repository already in a project (operator, 2026-11-08: "what if we
 *  choose same repo again, will it create new or open the already imported one?"): its row opens
 *  that project; Add work goes to its New work with the words; a second project on the same code
 *  only through "Make a separate project anyway", asked once. The server refuses the same
 *  (repo_in_use) without the person's say-so. */
export type InUseActions = {
  open: (projectId: string) => void;
  addWork: (projectId: string) => void;
  /** Repository ids the person chose to share anyway. */
  shared: string[];
  share: (repoId: string) => void;
};

export function usedBy(r: { used_by?: { project_id: string; name: string }[] }): { project_id: string; name: string }[] {
  return [...new Map((r.used_by ?? []).map((u) => [u.project_id, u])).values()];
}

/** The row's tag: "in <Project> · Open ›". */
export function InUseTag({ used }: { used: { name: string }[] }) {
  return (
    <span className="npx-tag npx-inuse-tag" data-tone="amber" title={`Already in ${used.map((u) => u.name).join(", ")}: opens it`}>
      in {used[0].name}{used.length > 1 ? ` +${used.length - 1}` : ""} ›
    </span>
  );
}

/** Under an in-use row: every project it is in, Add work instead, Make a separate project anyway. */
export function InUseBlock({ repoId, used, act, testid }: {
  repoId: string; used: { project_id: string; name: string }[]; act: InUseActions; testid: string;
}) {
  const [asking, setAsking] = useState(false);
  if (act.shared.includes(repoId)) return <p className="npx-inuse npx-faint" data-testid={`${testid}-shared`}>A separate project on the same code as {used[0].name}.</p>;
  return (
    <div className="npx-inuse" data-testid={`${testid}-inuse`}>
      {used.length > 1 ? (
        <span className="npx-inuse-in">In {used.slice(0, 3).map((u, i) => (
          <React.Fragment key={u.project_id}>{i ? ", " : ""}<button type="button" className="npx-link" data-testid={`${testid}-open-${u.project_id}`}
            onClick={() => act.open(u.project_id)}>{u.name} ›</button></React.Fragment>))}
          {used.length > 3 ? <span className="npx-faint" title={used.slice(3).map((u) => u.name).join(", ")}> +{used.length - 3} more</span> : null}</span>
      ) : null}
      <button type="button" className="npx-link" data-testid={`${testid}-addwork`} onClick={() => act.addWork(used[0].project_id)}>
        Add work to {used[0].name} instead</button>
      {asking ? (
        <span className="npx-inuse-ask" role="group" aria-label="Make a separate project">
          <span>Two projects on one repo means two sets of agents opening PRs on the same code.</span>
          <button type="button" className="npx-btn" data-testid={`${testid}-separate-yes`} autoFocus onClick={() => { act.share(repoId); setAsking(false); }}>Make it separate</button>
          <button type="button" className="npx-link" data-testid={`${testid}-separate-no`} onClick={() => setAsking(false)}>Cancel</button>
        </span>
      ) : (
        <button type="button" className="npx-link npx-faint" data-testid={`${testid}-separate`} onClick={() => setAsking(true)}>Make a separate project anyway</button>
      )}
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

export function PlanBoxes({ kind, preview, plan, set, cards, adapters, helmPicks, workflow, registry, repoLabel, codeName = "", live, accountLogin, accounts = [], homeHref, inUse, newRepo, codeWhere = null }: {
  kind: Kind; preview: Preview | null; plan: PlanState; set: (patch: Partial<PlanState>) => void;
  cards: TemplateCard[] | null; adapters: StackAdapter[]; helmPicks: { stack: Pick | null; connectors: Pick[]; workflow: Pick | null };
  workflow: Workflow | null; registry: RegistryRepo[]; repoLabel: { v: string; s: string };
  /** The picked repository's name or the folder's path (the Code box). */
  codeName?: string;
  live: boolean; accountLogin: string;
  /** The GitHub accounts BOSS reaches (the new repo's owner picker). */
  accounts?: string[];
  homeHref: (p: Record<string, string>) => string;
  /** A repo already in a project (the Code box's list): open it, add work to it, or share it. */
  inUse: InUseActions;
  /** The new GitHub repo's name as it will be made, and the engine's answer to "can BOSS make it?". */
  newRepo?: { name: string; check: RepoCheck | null; checking: boolean };
  /** Existing code: where it lives once added (a repo, this machine, or read in place). */
  codeWhere?: { name: string; sub: string; local: boolean } | null;
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
              {pickedCards.slice(0, 2).map((c) => <span key={c.key} data-on title={because(c.key) ? quoted(because(c.key)!) : c.summary}>{cxIcon(c.key)}{shortName(c.name)}</span>)}
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
                      {cxIcon(c.key)}<span><b>{c.name}</b><small>{because(c.key) ? quoted(because(c.key)!) : c.summary}</small></span>
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
                  data-where={kind === "idea" ? plan.where : codeWhere?.local ? "local" : "repo"}
                  data-t={kind === "idea" && plan.where === "github_new" && newRepo?.check && !newRepo.check.ok ? "you" : undefined}
                  title="Where the code lives · this machine, a new GitHub repo, or a repo you have" onClick={() => toggle("code")}>
            <span className="npx-lbl">Code{kind === "idea" && plan.where === "local" ? <span className="npx-auto" data-tone="local">LOCAL</span> : null}<CaretDown size={11} aria-hidden /></span>
            <CodeLabel kind={kind} plan={plan} registry={registry} preview={preview} name={codeName} newRepo={newRepo} codeWhere={codeWhere} />
          </button>
          {open === "code" ? (
            <Pop title="Where the code lives" icon={<GitBranch size={14} />} anchor="Code" testid="code" onDone={() => setOpen(null)} wide={kind === "idea"}>
              {kind === "idea" ? (
                <>
                  <div className="npx-where" role="radiogroup" aria-label="Where the code lives" data-testid="ap-where">
                    <button type="button" role="radio" aria-checked={plan.where === "local"} className="npx-where-opt" data-testid="ap-where-local"
                            onClick={() => set({ where: "local" })}>
                      <HardDrives size={16} weight="duotone" aria-hidden /><span><b>On this machine</b><small>BOSS keeps the repo · publish to GitHub any time</small></span>
                    </button>
                    <button type="button" role="radio" aria-checked={plan.where === "github_new"} className="npx-where-opt" data-testid="ap-where-github"
                            onClick={() => set({ where: "github_new" })} title={accounts.length ? "A new repo on GitHub, made by BOSS when you press Create" : "Connect a GitHub account first (Repo and models)"}>
                      <GithubLogo size={16} weight="fill" aria-hidden /><span><b>New GitHub repo</b><small>made by BOSS when you press Create</small></span>
                    </button>
                    <button type="button" role="radio" aria-checked={plan.where === "repo"} className="npx-where-opt" data-testid="ap-where-repo"
                            onClick={() => set({ where: "repo" })}>
                      <FolderPlus size={16} weight="duotone" aria-hidden /><span><b>A folder in a repo you have</b><small>a monorepo · one PR</small></span>
                    </button>
                  </div>
                  {plan.where === "local" ? (
                    <p className="npx-faint npx-small" data-testid="ap-where-local-note">Works offline. Changes are reviewed and merged here; Publish to GitHub moves it whole, history included.</p>
                  ) : plan.where === "github_new" ? (
                    <NewRepoForm plan={plan} set={set} accounts={accounts} newRepo={newRepo} homeHref={homeHref} />
                  ) : null}
                  {plan.where === "repo" ? (<>
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
                      const used = usedBy(r);
                      // A repo already in a project opens that project, unless the person chose a separate one.
                      const opens = used.length > 0 && !inUse.shared.includes(r.id);
                      return (
                        <React.Fragment key={r.id}>
                        <button type="button" className="npx-opt" aria-selected={on} data-testid={`ap-code-repo-${r.id}`} data-in-use={used.length ? "" : undefined}
                                title={opens ? `Already in ${used[0].name}: opens it` : r.full_name}
                                onClick={() => opens ? inUse.open(used[0].project_id) : set({ repoIds: plan.layout === "one" ? [r.id] : on ? plan.repoIds.filter((x) => x !== r.id) : [...plan.repoIds, r.id].slice(0, 6) })}>
                          {plan.layout === "several" ? (on ? <CheckSquare size={13} aria-hidden /> : <Square size={13} aria-hidden />) : <GitBranch size={12} aria-hidden />}
                          <span className="npx-mono">{r.full_name}</span>{opens ? <InUseTag used={used} /> : used.length ? <span className="npx-tag">shared</span> : null}
                        </button>
                        {used.length ? <InUseBlock repoId={r.id} used={used} act={{ ...inUse, share: (id) => { inUse.share(id); set({ repoIds: plan.layout === "one" ? [id] : [...plan.repoIds.filter((x) => x !== id), id].slice(0, 6) }); } }} testid={`ap-code-repo-${r.id}`} /> : null}
                        </React.Fragment>
                      );
                    })}
                  </div>
                  {plan.layout === "several" ? <p className="npx-faint">Several repos: BOSS maps the app&apos;s parts to them; linked PRs, approved as a set.</p> : null}
                  </>) : null}
                </>
              ) : (
                <p className="npx-faint" data-testid="ap-code-existing">{codeWhere ? (codeWhere.local
                  ? <>On <b>this machine</b> ({codeWhere.sub}): builds become change sets you review and merge here. Publish to GitHub any time.</>
                  : <>The {kind === "folder" ? "folder" : "repo"} you picked, <span className="npx-mono">{codeWhere.name}</span>: {codeWhere.sub}.</>)
                  : codeName ? <>The {kind === "folder" ? "folder" : "repo"} you picked, <span className="npx-mono">{codeName}</span>.</> : "Pick a repo or a folder above."}</p>
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
            <Pop title="Who to tell" icon={<BellRinging size={14} />} anchor="Tell" testid="tell" onDone={() => { set({ tell: tellDraft.trim() }); setOpen(null); }} onEscape={() => setOpen(null)}>
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

/** The new GitHub repo: owner, name (from the brief, editable), private; the engine's answer to
 *  "can BOSS make it?" with its one fix; and the line that makes the person's decision explicit. */
function NewRepoForm({ plan, set, accounts, newRepo, homeHref }: {
  plan: PlanState; set: (p: Partial<PlanState>) => void; accounts: string[];
  newRepo?: { name: string; check: RepoCheck | null; checking: boolean }; homeHref: (p: Record<string, string>) => string;
}) {
  const check = newRepo?.check ?? null;
  const fix = repoCheckFix(check);
  const owner = plan.newRepo.owner;
  if (!accounts.length) {
    return <p className="npx-faint npx-small" data-testid="ap-newrepo-noaccount">No GitHub account yet · <a className="npx-link" href={homeHref({ area: "setup", focus: "repository" })}>Connect one in Repo and models ›</a></p>;
  }
  return (
    <div className="npx-newrepo" data-testid="ap-newrepo">
      <div className="npx-newrepo-row">
        <label className="npx-field npx-newrepo-owner"><span>Owner</span>
          <select className="npx-input" data-testid="ap-newrepo-owner" value={owner} onChange={(e) => set({ newRepo: { ...plan.newRepo, owner: e.target.value } })}>
            {accounts.map((a) => <option key={a} value={a}>{a}</option>)}
          </select></label>
        <span className="npx-newrepo-slash" aria-hidden>/</span>
        <label className="npx-field npx-newrepo-name"><span>Repo name</span>
          <input className="npx-input npx-mono" data-testid="ap-newrepo-name" maxLength={100} spellCheck={false} value={plan.newRepo.name || newRepo?.name || ""}
                 onChange={(e) => set({ newRepo: { ...plan.newRepo, name: e.target.value.replace(/\s+/g, "-") } })} /></label>
      </div>
      <label className="npx-keep" data-testid="ap-newrepo-private-row">
        <input type="checkbox" data-testid="ap-newrepo-private" checked={plan.newRepo.private} onChange={(e) => set({ newRepo: { ...plan.newRepo, private: e.target.checked } })} />
        <LockKey size={12} aria-hidden /> Private
      </label>
      <p className="npx-newrepo-check" data-testid="ap-newrepo-check" data-ok={newRepo?.checking ? "checking" : check ? String(check.ok) : "unknown"} role="status">
        {newRepo?.checking ? <span className="npx-faint">Asking GitHub…</span>
          : check ? <>{check.ok ? <Check size={12} weight="bold" className="npx-ok" aria-hidden /> : <Hand size={12} weight="fill" className="npx-amber" aria-hidden />}
            <span>{check.words}</span>
            {fix ? <> <a className="npx-link" data-testid="ap-newrepo-fix" href={fix.external ? fix.href : homeHref(Object.fromEntries(new URLSearchParams(fix.href.replace(/^\?/, ""))))}
                         {...(fix.external ? { target: "_blank", rel: "noreferrer" } : {})}>{fix.label} ›</a></> : null}</>
          : null}
      </p>
      <p className="npx-faint npx-small" data-testid="ap-newrepo-decision"><Hand size={11} aria-hidden /> Making a repo in <b>{owner || "GitHub"}</b> is your decision: it happens when you press Create.</p>
    </div>
  );
}

function CodeLabel({ kind, plan, registry, preview, name, newRepo, codeWhere }: {
  kind: Kind; plan: PlanState; registry: RegistryRepo[]; preview: Preview | null; name: string;
  newRepo?: { name: string; check: RepoCheck | null; checking: boolean }; codeWhere?: { name: string; sub: string; local: boolean } | null;
}) {
  if (kind !== "idea") {
    if (codeWhere) return <><span className="npx-v npx-mono">{codeWhere.local ? "This machine" : codeWhere.name.split("/").pop()}</span><span className="npx-s">{codeWhere.sub}</span></>;
    return <><span className="npx-v npx-mono">{name ? name.split("/").pop() : "—"}</span><span className="npx-s">{kind === "folder" ? "a folder" : "its branch · PRs"}</span></>;
  }
  if (plan.where === "local") return <><span className="npx-v">This machine</span><span className="npx-s">local · publish any time</span></>;
  if (plan.where === "github_new") {
    const full = `${plan.newRepo.owner || "?"}/${plan.newRepo.name || newRepo?.name || "…"}`;
    const refused = newRepo?.check && !newRepo.check.ok;
    return <><span className="npx-v npx-mono">{full}</span><span className="npx-s">{refused ? "GitHub can’t make it yet" : `new · ${plan.newRepo.private ? "private" : "public"} · made at Create`}</span></>;
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
  return editing ? <NameEdit plan={plan} set={set} done={() => setEditing(false)} /> : (
    <button type="button" className="npx-pchip" data-testid="ap-chip-name" title="Prefilled from the brief or the repo · edit inline" onClick={() => setEditing(true)}>
      <PencilSimple size={13} aria-hidden />Name <b>{plan.name || "—"}</b>
    </button>
  );
}

/** The Name chip, editing: in view like a popover, its own keys (↵ / Esc end the edit). */
function NameEdit({ plan, set, done }: { plan: PlanState; set: (p: Partial<PlanState>) => void; done: () => void }) {
  const ref = useRef<HTMLLabelElement>(null);
  useBoxPop(ref, { fit: false });
  return (
    <label className="npx-pchip" data-editing ref={ref}>
      <PencilSimple size={13} aria-hidden />Name
      <input autoFocus className="npx-inline" data-testid="ap-name-input" maxLength={120} value={plan.name}
             onChange={(e) => set({ name: e.target.value, named: true })} onBlur={done}
             onKeyDown={(e) => { if (e.key === "Enter" || e.key === "Escape") { e.preventDefault(); e.stopPropagation(); done(); } }} />
    </label>
  );
}

