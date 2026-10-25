"use client";

/**
 * KNOWLEDGE GRAPH › SETUP — a knowledge-graph project, set up in order (BOSSAPP-768 project flow).
 *
 * The operator: "There's no good sequencing — we need a proper UI flow to create the knowledge
 * graph and how it is associated with Agents for a particular project." This is that flow, in the
 * Setup stepper's pattern (setup-row.tsx, lib/setup-stepper.ts): numbered rows, the current step
 * open, finished steps one line with Change, later steps waiting with why.
 *
 *   ① Import code      the org's allowed repositories, added to the project (Factory ›
 *                      Repositories' own door), and their indexing with real counts
 *   ② Assign the team  BOSS agents from this project's real roster, with their Passports:
 *                      who scans, who changes code, who handles incidents
 *   ③ First scan       one area, a budget and the cost preview (Scan for rules here)
 *   ④ Review rules     the queue's count, and Rules review
 *   Done               the Knowledge Graph
 *
 * Every state comes from GET /v1/knowledge/setup, which derives it from the records each step's
 * own service writes: the setup resumes wherever it was left, from any tab or after an agent did
 * a step. Also here: the first-visit intro strip (the three layers, and that every node says
 * which agent added it and who approved it), the "Start a knowledge graph" state for a project
 * without one, and the "continue setup" strip over the graph.
 */
import React, { useCallback, useEffect, useState } from "react";
import { Check, CircleNotch, X } from "@phosphor-icons/react";

import { AgentAvatar } from "../agent-avatar";
import { SectionLabel } from "../section-label";
import { GatedButton, useGate } from "../../components/gated";
import { useGuardedCall } from "../../components/identity/identity-provider";
import { openAgent } from "../../lib/agent-world";
import { INDEX_NOW, indexNow } from "../../lib/kg-view";
import { SETUP_STEPS, setProjectTeam, stepsLeftWords, type KgSetupCandidate, type KgSetupResponse, type KgSetupStepKey } from "../../lib/kg-setup";
import { MULTIREPO_PLACES, multirepo, refusalMessage, type ProjectRepos } from "../../lib/multirepo";
import { areaHref } from "../factory/cockpit-util";
import { ScanForRules } from "./node-panel";
import { StageCard } from "./kg-states";

const CIRCLED = ["①", "②", "③", "④"];
const ARCHIVED_WHY = "This project is archived: nothing new runs on it. Restore it to change it.";

type Who = { projectId: string; actor: string };

// ── the intro strip ─────────────────────────────────────────────────────────────────────────

const INTRO_KEY = "boss.kg.intro.dismissed";

function introDismissed(): boolean {
  try { return window.localStorage.getItem(INTRO_KEY) === "1"; } catch { return false; }
}

/** What a knowledge graph IS, said once on a first visit and dismissible (per browser). */
export function KgIntro() {
  const [shown, setShown] = useState(false);
  useEffect(() => setShown(!introDismissed()), []);
  if (!shown) return null;
  const dismiss = () => {
    try { window.localStorage.setItem(INTRO_KEY, "1"); } catch { /* private window: hide for now */ }
    setShown(false);
  };
  return (
    <aside className="kg-intro" data-testid="kg-intro" aria-label="What the knowledge graph is">
      <div className="kg-intro-body">
        <strong className="kg-intro-title">A knowledge graph of this project&apos;s code, and of what its agents know about it.</strong>
        <ul className="kg-intro-layers">
          <li><span className="kg-dot" data-layer="code" aria-hidden /><b>Code</b> — areas, packages, classes and calls, indexed from the repository.</li>
          <li><span className="kg-dot" data-layer="rules" aria-hidden /><b>Rules</b> — business rules agents find in the code, trusted once a person approves them.</li>
          <li><span className="kg-dot" data-layer="learning" aria-hidden /><b>Learning</b> — what agents learn while they work: failures, causes, fixes, precedents.</li>
        </ul>
        <p className="kg-quiet">Every node says which agent added it and who approved it.</p>
      </div>
      <button type="button" className="kg-intro-close" aria-label="Dismiss the introduction" title="Got it" data-testid="kg-intro-dismiss" onClick={dismiss}>
        <X size={14} weight="bold" aria-hidden />
      </button>
    </aside>
  );
}

// ── a project without a knowledge graph ────────────────────────────────────────────────────

/** Never an empty canvas: the designed way in, which opens the setup. */
export function KgStart({ onStart, archived }: { onStart: () => void; archived?: boolean }) {
  return (
    <StageCard testId="kg-start" eyebrow="Knowledge Graph" layers
               title="Start a knowledge graph for this project"
               body="Import the code, choose the agents who work on it, and run a first scan for business rules. BOSS maps the code; agents propose rules for a person to approve, and record what they learn as they work.">
      <button type="button" className="ag-btn ag-btn-primary" data-testid="kg-start-setup" disabled={archived}
              title={archived ? ARCHIVED_WHY : "Four steps: import code, assign the team, a first scan, review"} onClick={onStart}>
        Set up the knowledge graph
      </button>
    </StageCard>
  );
}

/** Over the graph while steps are left: how far, and Continue. */
export function KgSetupStrip({ setup, onOpen }: { setup: KgSetupResponse; onOpen: () => void }) {
  const next = SETUP_STEPS.find((s) => s.key === setup.next);
  if (!next) return null;
  return (
    <p className="kg-banner kg-setup-strip" role="note" data-testid="kg-setup-strip">
      <span className="kg-banner-text">Setup · {stepsLeftWords(setup)}. Next: <b>{next.label}</b></span>
      <button type="button" className="ag-btn" data-testid="kg-setup-continue" onClick={onOpen}>Continue setup</button>
    </p>
  );
}

// ── the stepper ─────────────────────────────────────────────────────────────────────────────

const STATE_WORD: Record<string, string> = { done: "Done", working: "Working", current: "Next", waiting: "Waiting" };

export function KgSetup({ w, setup, error, onReload, onOpenTab, archived }: {
  w: Who; setup: KgSetupResponse | null; error: string | null; onReload: () => void;
  onOpenTab: (tab: "graph" | "rules") => void; archived?: boolean;
}) {
  const [open, setOpen] = useState<KgSetupStepKey | null>(null);
  if (!setup) {
    return error ? (
      <div className="kg-banner" data-tone="red" role="alert" data-testid="kg-setup-error">
        <span className="kg-banner-text">Couldn&apos;t read this project&apos;s setup: {error}</span>
        <button type="button" className="ag-btn" onClick={onReload}>Retry</button>
      </div>
    ) : <div className="kg-setup" data-testid="kg-setup" aria-busy="true"><div className="kg-shimmer kg-setup-loading" aria-hidden /></div>;
  }
  const allDone = setup.next === null;
  return (
    <section className="kg-setup" data-testid="kg-setup" aria-label="Knowledge graph setup" data-next={setup.next ?? "done"}>
      <header className="kg-setup-head">
        <span className="kg-empty-eyebrow">Knowledge Graph setup</span>
        <h3 className="kg-setup-title">{allDone ? "The knowledge graph is set up" : <>Set up the knowledge graph for <span data-user-text>{setup.name}</span></>}</h3>
        <span className="kg-setup-progress" data-testid="kg-setup-progress">
          <span className="kg-setup-pips" aria-hidden>
            {setup.steps.map((s) => <i key={s.key} data-state={s.state} />)}
          </span>
          {stepsLeftWords(setup)}
        </span>
      </header>
      {error ? <p className="kg-inline-err" role="alert">{error} <button type="button" className="inf-link" onClick={onReload}>Retry</button></p> : null}
      <ol className="kg-setup-steps">
        {SETUP_STEPS.map((def, i) => {
          const step = setup.steps.find((s) => s.key === def.key)!;
          const expanded = open === def.key || (open === null && step.state === "current")
            || (open === null && step.state === "working" && def.key === "import" && setup.next === "import");
          return (
            <li key={def.key} className="kg-setup-row" data-testid={`kg-step-${def.key}`} data-state={step.state} data-open={expanded || undefined}>
              <div className="kg-setup-label">
                <span className="kg-setup-num" aria-hidden>{step.done ? <Check size={14} weight="bold" /> : step.state === "working" ? <CircleNotch size={14} weight="bold" className="kg-spin" /> : CIRCLED[i]}</span>
                <SectionLabel as="span" tip={def.line}>{def.label}</SectionLabel>
                <span className="kg-setup-state" data-state={step.state}>{STATE_WORD[step.state]}</span>
              </div>
              <div className="kg-setup-body">
                <p className="kg-setup-summary" data-testid={`kg-step-${def.key}-summary`}>{step.summary}</p>
                {expanded ? (
                  <div className="kg-setup-work">
                    <p className="kg-quiet">{def.line}</p>
                    {def.key === "import" ? <ImportStep w={w} setup={setup} archived={archived} onChanged={onReload} /> : null}
                    {def.key === "team" ? <TeamStep w={w} setup={setup} archived={archived} onSaved={() => { setOpen(null); onReload(); }} /> : null}
                    {def.key === "scan" ? <ScanStep w={w} setup={setup} archived={archived} onStarted={onReload} /> : null}
                    {def.key === "review" ? <ReviewStep setup={setup} onOpen={() => onOpenTab("rules")} /> : null}
                  </div>
                ) : step.state === "waiting" ? (
                  <p className="kg-quiet" data-testid={`kg-step-${def.key}-why`}>{waitingWhy(def.key, setup)}</p>
                ) : null}
              </div>
              <div className="kg-setup-aside">
                {!expanded && step.state !== "waiting" ? (
                  <button type="button" className="inf-link" data-testid={`kg-step-${def.key}-open`} onClick={() => setOpen(def.key)}>
                    {step.done ? "Change" : "Open"}
                  </button>
                ) : expanded && open === def.key ? (
                  <button type="button" className="inf-link" onClick={() => setOpen(null)}>Close</button>
                ) : null}
              </div>
            </li>
          );
        })}
        <li className="kg-setup-row kg-setup-done" data-testid="kg-step-done" data-state={allDone ? "done" : "waiting"}>
          <div className="kg-setup-label">
            <span className="kg-setup-num" aria-hidden>{allDone ? <Check size={14} weight="bold" /> : "→"}</span>
            <SectionLabel as="span">Done</SectionLabel>
          </div>
          <div className="kg-setup-body">
            <p className="kg-setup-summary">{allDone
              ? `${setup.code_nodes.toLocaleString("en-US")} code nodes · ${setup.rules.approved} approved rules · ${setup.learnings} learnings. Agents on this project are given it as they work.`
              : "The Knowledge Graph opens with the code, the rules and what agents learned, on one canvas."}</p>
          </div>
          <div className="kg-setup-aside">
            <button type="button" className={allDone ? "ag-btn ag-btn-primary" : "ag-btn"} data-testid="kg-setup-open-graph"
                    disabled={!setup.has_graph} title={setup.has_graph ? "Open the Knowledge Graph" : "The graph opens once the code is indexed"}
                    onClick={() => onOpenTab("graph")}>Open the Knowledge Graph</button>
          </div>
        </li>
      </ol>
    </section>
  );
}

function waitingWhy(key: KgSetupStepKey, setup: KgSetupResponse): string {
  if (key === "scan") return setup.steps[0].state === "working" ? "Starts when the code is indexed. Indexing is running." : "Needs indexed code: import a repository first.";
  if (key === "review") return "Rules arrive here from the first scan.";
  if (key === "team") return "Choose the agents once the code is on its way in, or now.";
  return "";
}

// ── ① import code ────────────────────────────────────────────────────────────────────────────

function repoProgress(r: KgSetupResponse["repositories"][number]): string {
  if (r.status === "running" && r.files) return `Indexing ${r.parsed ? `${r.parsed.toLocaleString("en-US")} of ` : ""}${r.files.toLocaleString("en-US")} files`;
  if (r.status === "running") return `Indexing ${r.branch ?? "its branch"}`;
  if (r.status === "queued" || r.status === "pending") return "Queued: indexing starts within a minute";
  if (r.status === "failed" || r.status === "too_large") return `Not indexed: ${r.error ?? r.status.replace("_", " ")}`;
  if (r.status === "indexed") return r.summary ?? `${(r.graph?.nodes ?? 0).toLocaleString("en-US")} nodes`;
  return r.summary ?? r.status;
}

function ImportStep({ w, setup, archived, onChanged }: {
  w: Who; setup: KgSetupResponse; archived?: boolean; onChanged: () => void;
}) {
  const [view, setView] = useState<ProjectRepos | null>(null);
  const [pick, setPick] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const gate = useGate("repository:manage");
  const guarded = useGuardedCall();
  const load = useCallback(async () => {
    try { setView(await multirepo.projectRepos(w.projectId)); } catch (e) { setMsg({ ok: false, text: refusalMessage(e) }); }
  }, [w.projectId]);
  useEffect(() => { void load(); }, [load]);
  const add = async () => {
    if (!pick || !gate.allowed) return;
    const name = view?.addable.find((a) => a.id === pick)?.full_name ?? "it";
    setBusy("add"); setMsg(null);
    try {
      setView(await guarded("Add a repository to this project.", () => multirepo.putProjectRepo(w.projectId, pick, {})));
      setPick("");
      setMsg({ ok: true, text: `Added ${name}. Indexing starts within a minute; this list follows it.` });
      onChanged();
    } catch (e) {
      if ((e as { code?: string }).code !== "step_up_cancelled") setMsg({ ok: false, text: refusalMessage(e) });
    } finally { setBusy(null); }
  };
  const repos = setup.repositories;
  const idle = repos.length > 0 && !repos.some((r) => ["queued", "running", "pending"].includes(r.status)) && !setup.steps[0].done;
  const reindex = async () => {
    setBusy("index"); setMsg(null);
    const r = await indexNow(w, "");
    setBusy(null);
    setMsg(r.ok ? { ok: true, text: "Indexing started." } : { ok: false, text: r.error });
    onChanged();
  };
  const registry = areaHref(MULTIREPO_PLACES.registry);
  return (
    <div className="kg-setup-import" data-testid="kg-import">
      {repos.length ? (
        <ul className="kg-index-list" data-testid="kg-import-repos">
          {repos.map((r) => (
            <li key={r.repo} className="kg-index-row" data-status={r.status}>
              <span className="inf-dot" data-tone={r.status === "running" || r.status === "queued" || r.status === "pending" ? "accent" : r.status === "indexed" ? "green" : r.status === "failed" || r.status === "too_large" ? "red" : "grey"} aria-hidden />
              <span className="inf-mono" data-user-text>{r.repo}</span>
              <span className="kg-index-what">{repoProgress(r)}</span>
              {r.status === "running" && r.files && r.parsed ? (
                <span className="kg-bar" role="progressbar" aria-valuemin={0} aria-valuemax={r.files} aria-valuenow={r.parsed} aria-label={`${r.repo} indexing`}>
                  <i style={{ width: `${Math.min(100, Math.round((r.parsed / r.files) * 100))}%` }} />
                </span>
              ) : null}
            </li>
          ))}
        </ul>
      ) : null}
      <div className="kg-setup-actions">
        <select className="input kg-setup-select" value={pick} disabled={!view || !view.addable.length || !!busy || archived}
                aria-label="Add a repository" data-testid="kg-import-pick" onChange={(e) => setPick(e.target.value)}>
          <option value="">{!view ? "Reading the allowed repositories…" : view.addable.length ? "Choose a repository…" : "Nothing more to add"}</option>
          {(view?.addable ?? []).map((a) => <option key={a.id} value={a.id}>{a.full_name}{a.language ? ` · ${a.language}` : ""}</option>)}
        </select>
        <GatedButton permission="repository:manage" className="ag-btn ag-btn-primary" data-testid="kg-import-add"
                     disabled={!pick || !!busy || archived} title={archived ? ARCHIVED_WHY : "Add it to this project; indexing starts by itself"}
                     onClick={() => void add()}>{busy === "add" ? "Adding…" : "Add and index"}</GatedButton>
        {idle ? (
          <GatedButton permission={INDEX_NOW} className="ag-btn" data-testid="kg-import-index" disabled={!!busy || archived}
                       onClick={() => void reindex()}>{busy === "index" ? "Starting…" : "Index now"}</GatedButton>
        ) : null}
      </div>
      <p className="kg-quiet">
        Lists the repositories allowed for BOSS in <a className="inf-link" href={registry} data-testid="kg-import-registry">Repo and models</a>
        {view?.not_allowed_count ? ` · ${view.not_allowed_count} more there ${view.not_allowed_count === 1 ? "is" : "are"} not allowed yet` : ""}.
      </p>
      {view && view.registry_in_use === false && !repos.length ? (
        <p className="kg-quiet" data-testid="kg-import-no-registry">No repository is allowed for BOSS yet. Tick one in Repo and models › Repositories, then add it here.</p>
      ) : null}
      {msg ? <p className={msg.ok ? "kg-ok" : "kg-inline-err"} role={msg.ok ? "status" : "alert"} data-testid="kg-import-msg">{msg.text}</p> : null}
    </div>
  );
}

// ── ② assign the team ────────────────────────────────────────────────────────────────────────

const JOB_WORD: Record<string, string> = { scans: "Scans", changes: "Changes", incidents: "Incidents" };

function initialPick(setup: KgSetupResponse): Set<string> {
  if (setup.team.chosen) return new Set(setup.team.role_keys);
  return new Set(setup.team.candidates.filter((c) => c.recommended).map((c) => c.role_key));
}

function TeamStep({ w, setup, archived, onSaved }: { w: Who; setup: KgSetupResponse; archived?: boolean; onSaved: () => void }) {
  const [picked, setPicked] = useState<Set<string>>(() => initialPick(setup));
  const [all, setAll] = useState(false);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const recommended = setup.team.candidates.filter((c) => c.recommended);
  const others = setup.team.candidates.filter((c) => !c.recommended);
  const chosenOthers = others.filter((c) => picked.has(c.role_key));
  const shownOthers = all ? others : chosenOthers;
  const toggle = (key: string) => setPicked((cur) => { const n = new Set(cur); if (n.has(key)) n.delete(key); else n.add(key); return n; });
  const save = async () => {
    setBusy(true); setMsg(null);
    const r = await setProjectTeam(w, setup.team.candidates.map((c) => c.role_key).filter((k) => picked.has(k)));
    setBusy(false);
    if (r.ok) onSaved(); else setMsg(r.error);
  };
  return (
    <div className="kg-setup-team" data-testid="kg-team">
      <p className="kg-setup-sub">Recommended for a knowledge graph</p>
      <ul className="kg-team-list" data-testid="kg-team-recommended">
        {recommended.map((c) => <Candidate key={c.role_key} c={c} on={picked.has(c.role_key)} onToggle={() => toggle(c.role_key)} disabled={archived} />)}
      </ul>
      {shownOthers.length ? (
        <>
          <p className="kg-setup-sub">Other agents in this project</p>
          <ul className="kg-team-list" data-testid="kg-team-others">
            {shownOthers.map((c) => <Candidate key={c.role_key} c={c} on={picked.has(c.role_key)} onToggle={() => toggle(c.role_key)} disabled={archived} />)}
          </ul>
        </>
      ) : null}
      {others.length && !all ? (
        <button type="button" className="inf-link" data-testid="kg-team-all" onClick={() => setAll(true)}>Show all {setup.team.candidates.length} agents</button>
      ) : null}
      <div className="kg-setup-actions">
        <GatedButton permission="work_item:manage" className="ag-btn ag-btn-primary" data-testid="kg-team-save"
                     disabled={busy || picked.size === 0 || archived}
                     title={archived ? ARCHIVED_WHY : "These agents work on this project: every new piece of work starts with them, and only they sign off its gates"}
                     onClick={() => void save()}>{busy ? "Saving…" : `Save the team (${picked.size})`}</GatedButton>
        {picked.size === 0 ? <span className="kg-quiet">Choose at least one agent.</span> : null}
      </div>
      {msg ? <p className="kg-inline-err" role="alert" data-testid="kg-team-msg">{msg}</p> : null}
    </div>
  );
}

function Candidate({ c, on, onToggle, disabled }: { c: KgSetupCandidate; on: boolean; onToggle: () => void; disabled?: boolean }) {
  return (
    <li className="kg-team-row" data-testid={`kg-team-${c.role_key}`} data-on={on || undefined}>
      <label className="kg-team-pick">
        <input type="checkbox" checked={on} onChange={onToggle} disabled={disabled} aria-label={`${c.display_name} on the team`} />
        <AgentAvatar agent={c.role_key} size={28} />
        <span className="kg-team-who">
          <b>{c.display_name}</b>
          {c.job ? <span className="kg-team-job" data-job={c.job}>{JOB_WORD[c.job] ?? c.job}</span> : null}
          <small>{c.does ?? c.mission ?? ""}</small>
        </span>
      </label>
      <span className="kg-team-vitals">
        <span title="Trust score from its record">Trust {c.trust}</span>
        <span>{c.runs.toLocaleString("en-US")} runs</span>
        <button type="button" className="inf-link" data-testid={`kg-team-passport-${c.role_key}`}
                title={`${c.display_name}'s Passport: who it is, what it may do, and its record${c.passport ? ` (${c.passport})` : ""}`}
                onClick={() => openAgent(c.role_key)}>Passport</button>
      </span>
    </li>
  );
}

// ── ③ first scan ─────────────────────────────────────────────────────────────────────────────

function ScanStep({ w, setup, archived, onStarted }: { w: Who; setup: KgSetupResponse; archived?: boolean; onStarted: () => void }) {
  const [area, setArea] = useState<string>(() => setup.areas[0]?.key ?? "");
  const chosen = setup.areas.find((a) => a.key === area) ?? null;
  const scanner = setup.team.candidates.find((c) => c.job === "scans" && setup.team.role_keys.includes(c.role_key))
    ?? setup.team.candidates.find((c) => c.job === "scans");
  useEffect(() => {
    // A scan started here shows up in the setup's own count: re-read once it has had time to start.
    if (!setup.scans.running) return undefined;
    const t = setInterval(onStarted, 6000);
    return () => clearInterval(t);
  }, [setup.scans.running, onStarted]);
  return (
    <div className="kg-setup-scan" data-testid="kg-scan-step">
      {setup.scans.latest ? (
        <p className="kg-quiet" data-testid="kg-scan-latest">Last scan: {setup.scans.latest.status}{setup.scans.running ? " — proposed rules arrive in Rules review as it reads" : ""}.</p>
      ) : null}
      {setup.areas.length ? (
        <>
          <p className="kg-setup-sub">Choose an area{scanner ? <> · <span>{scanner.display_name} reads it</span></> : null}</p>
          <div className="kg-area-list" role="radiogroup" aria-label="The area to scan" data-testid="kg-scan-areas">
            {setup.areas.map((a) => (
              <label key={a.key} className="kg-area" data-on={a.key === area || undefined}>
                <input type="radio" name="kg-scan-area" value={a.key} checked={a.key === area} onChange={() => setArea(a.key)} />
                <span className="kg-dot" data-layer="code" aria-hidden />
                <span className="kg-area-name" data-user-text>{a.label}</span>
                <small>{a.classes.toLocaleString("en-US")} classes · {a.packages.toLocaleString("en-US")} packages</small>
              </label>
            ))}
          </div>
          {chosen ? (
            <ScanForRules w={w} node={{ id: chosen.key, key: chosen.key, label: chosen.label, kind: "area" }} archived={archived}
                          archivedWhy={ARCHIVED_WHY} compact onStarted={onStarted} />
          ) : null}
        </>
      ) : (
        <p className="kg-quiet" data-testid="kg-scan-no-areas">The graph&apos;s areas appear once the index is built. Pick any area or package in the Knowledge Graph&apos;s tree and press Scan for rules here.</p>
      )}
    </div>
  );
}

// ── ④ review rules ───────────────────────────────────────────────────────────────────────────

function ReviewStep({ setup, onOpen }: { setup: KgSetupResponse; onOpen: () => void }) {
  const { proposed, approved, rejected } = setup.rules;
  return (
    <div className="kg-setup-review" data-testid="kg-review-step">
      <dl className="kg-setup-counts">
        <div><dt>Waiting</dt><dd data-testid="kg-review-waiting">{proposed}</dd></div>
        <div><dt>Approved</dt><dd>{approved}</dd></div>
        <div><dt>Rejected</dt><dd>{rejected}</dd></div>
      </dl>
      <div className="kg-setup-actions">
        <button type="button" className="ag-btn ag-btn-primary" data-testid="kg-review-open" onClick={onOpen}>
          {proposed ? `Review ${proposed} rule${proposed === 1 ? "" : "s"}` : "Open Rules review"}
        </button>
        <span className="kg-quiet">Owners and admins approve. An agent never approves a rule.</span>
      </div>
    </div>
  );
}
