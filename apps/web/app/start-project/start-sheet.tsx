"use client";

/**
 * NEW PROJECT (BOSSAPP-810, screen 1): three doors and the presets.
 *
 *   Try a sample        → a "Hello World" project, the hello-world built-in applied as a new app
 *                          through the smallest planned workflow (POST /v1/templates/…/apply)
 *   Import a repo       → screen 2 (import-repo.tsx)
 *   New app from an idea → the name-and-description form (new-project-sheet.tsx, both project
 *                          types), then the project page with the description ready to Start
 *   a preset            → an app brief: a project of that name, its page with the brief ready;
 *                          a banking preset: a knowledge-graph project on its Documents tab
 *
 * Opened from the project menu's "+ New project" and from `/?new_project=sample|import|idea|1`.
 * Short labels; one primary action per screen.
 */
import React, { useEffect, useId, useState } from "react";
import { createPortal } from "react-dom";
import { Flask, FolderPlus, GitBranch, Lightbulb, X } from "@phosphor-icons/react";

import { GatedButton, useGate } from "../../components/gated";
import { useGuardedCall } from "../../components/identity/identity-provider";
import { IdentityError } from "../../lib/identity/api";
import { withConsent } from "../../lib/identity/consent";
import { templatesApi } from "../../lib/templates";
import {
  CHOICES, allPresets, createFresh, nameFromIdea, projectPageHref, shownPresets, type Preset, type StartStep,
} from "../../lib/start-project";
import { ReadinessPanel, asReadiness, unaccepted, type Readiness } from "../workflow-readiness-ui";
import { ImportRepo } from "./import-repo";
import { post } from "../workflows/delivery-api";
import "./start-project.css";

/** The sample, as the project page's view names it (services/start_project.py). */
export const SAMPLE = { template: "hello-world", name: "Hello World", stack: "node", workflow_key: "minor_feature" };

export function knowledgePresetHref(projectId: string, homeId: string, tab: "documents" | "setup" = "documents"): string {
  const p = new URLSearchParams(typeof window === "undefined" ? "" : window.location.search);
  for (const k of ["new_project", "task", "run", "build", "kg_node", "kg_scan", "kg_sub"]) p.delete(k);
  if (projectId === homeId) p.delete("project_id"); else p.set("project_id", projectId);
  p.set("area", "knowledge");
  p.set("kg_tab", tab);
  return `/?${p.toString()}`;
}

function refusalText(e: unknown): string {
  if (e instanceof IdentityError) {
    if (e.code === "step_up_cancelled") return "Not created: you cancelled the check.";
    const msg = typeof e.body.message === "string" ? e.body.message : null;
    return msg ?? e.message;
  }
  return e instanceof Error ? e.message : String(e);
}

export function StartProjectSheet({ step, onStep, onClose, projectId, homeId, go = (href) => window.location.assign(href) }: {
  step: Exclude<StartStep, "form">;
  onStep: (next: StartStep) => void;
  onClose: () => void;
  /** The project open now: the org registry is read through it. */
  projectId: string;
  homeId: string;
  /** Navigation (a test passes its own). */
  go?: (href: string) => void;
}) {
  const titleId = useId();
  const guarded = useGuardedCall();
  const canCreate = useGate("project:create");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [all, setAll] = useState(false);
  // The sample, refused for what this install lacks: the checklist, and Start anyway.
  const [sampleProject, setSampleProject] = useState<string | null>(null);
  const [refusal, setRefusal] = useState<Readiness | null>(null);
  const [startAnyway, setStartAnyway] = useState<string[]>([]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape" && !busy) onClose(); };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose, busy]);

  const presets = allPresets();
  const grid = all ? presets : shownPresets(presets);

  /** The sample: a Hello World project, then the template applied as its first app. */
  const runSample = async () => {
    if (!canCreate.allowed) { setError(canCreate.reason); return; }
    setBusy("sample"); setError(null);
    try {
      let pid = sampleProject;
      if (!pid) {
        const made = await guarded("Create the Hello World project.", () => createFresh(SAMPLE.name));
        pid = made.id;
        setSampleProject(pid);
      }
      const body = { project_id: pid, new_app: { name: SAMPLE.name }, stack: SAMPLE.stack, workflow_key: SAMPLE.workflow_key,
                     dedupe_key: `sample:${pid}`, ...(startAnyway.length ? { start_anyway: startAnyway } : {}) };
      await guarded("Start the sample.", () => withConsent(() => templatesApi.apply(SAMPLE.template, body)));
      go(projectPageHref(pid, homeId));
    } catch (e) {
      const b = e instanceof IdentityError ? e.body : {};
      const detail = (b.detail && typeof b.detail === "object" ? b.detail : {}) as Record<string, unknown>;
      const ready = asReadiness(b.readiness ?? detail.readiness);
      if (ready) { setRefusal(ready); setBusy(null); return; }
      setError(refusalText(e));
    } finally {
      setBusy((cur) => (cur === "sample" ? null : cur));
    }
  };

  /** A preset: its project, then its page (an app brief waiting for Start; a banking one's Documents). */
  const pickPreset = async (p: Preset) => {
    if (!canCreate.allowed) { setError(canCreate.reason); return; }
    setBusy(p.key); setError(null);
    try {
      const made = await guarded(`Create the ${p.title} project.`, () => createFresh(p.title, {
        type: p.kind === "knowledge_graph" ? "knowledge_graph" : "app" }));
      if (p.kind === "knowledge_graph") { go(knowledgePresetHref(made.id, homeId)); return; }
      // A preset's brief waits in the Command Center's "What should we build?" (project-start.tsx).
      const href = projectPageHref(made.id, homeId, "overview");
      go(`${href}&start_preset=${encodeURIComponent(p.key)}`);
    } catch (e) {
      setError(refusalText(e));
      setBusy(null);
    }
  };

  // THE IDEA: one sentence. The name comes from it and can be changed; Plan it makes the project
  // and starts its new-app workflow (project.new) with the sentence as the brief.
  const [idea, setIdea] = useState("");
  const [ideaName, setIdeaName] = useState("");
  const [ideaNamed, setIdeaNamed] = useState(false);
  const [ideaProject, setIdeaProject] = useState<string | null>(null);
  const shownName = ideaNamed ? ideaName : nameFromIdea(idea);
  const runIdea = async () => {
    if (!idea.trim() || !shownName.trim() || busy) return;
    if (!canCreate.allowed) { setError(canCreate.reason); return; }
    setBusy("idea"); setError(null);
    try {
      let pid = ideaProject;
      if (!pid) {
        const made = await guarded("Create the project.", () => createFresh(shownName.trim(), { description: idea.trim() }));
        pid = made.id;
        setIdeaProject(pid);
      }
      const r = await post("start", {
        project_id: pid, work_class: "project.new", input: { directive: idea.trim(), title: shownName.trim() },
        ...(startAnyway.length ? { start_anyway: startAnyway } : {}),
      });
      if (!r.ok) {
        if ("readiness" in r && r.readiness) { setRefusal(r.readiness); setBusy(null); return; }
        setError(typeof r.error === "string" ? r.error : "It did not start.");
        setBusy(null);
        return;
      }
      go(projectPageHref(pid, homeId));
    } catch (e) {
      setError(refusalText(e));
      setBusy(null);
    }
  };

  const open = unaccepted(refusal, startAnyway);
  const sheet = (
    <div className="bx-np-backdrop" data-testid="start-project-backdrop"
         onPointerDown={(e) => { if (e.target === e.currentTarget && !busy) onClose(); }}>
      <div className="bx-np-sheet spj-sheet" role="dialog" aria-modal="true" aria-labelledby={titleId}
           data-testid="start-project" data-step={step}>
        <div className="spj-head">
          {/* ADD A PROJECT (BOSSAPP-914 screen 3): New · from an idea | Existing · repo, as tabs, with
              Try a sample beside them. The choice screen (presets) stays one press away. */}
          {/* ONE HEAD ON EVERY STEP (screen 03, PX-39): Add a project · New | Existing · Try a sample.
              The sample and the presets are this sheet's third door, never a different title. */}
          <span className="spj-ico" aria-hidden><FolderPlus size={17} weight="duotone" /></span>
          <h2 id={titleId} className="bx-np-title" style={{ flex: "none" }}>Add a project</h2>
          <div className="spj-seg" role="tablist" aria-label="New or existing">
            <button type="button" role="tab" aria-selected={step === "idea"} data-testid="start-tab-idea" disabled={!!busy}
                    onClick={() => { setRefusal(null); setError(null); onStep("idea"); }}><Lightbulb size={14} aria-hidden /> New · from an idea</button>
            <button type="button" role="tab" aria-selected={step === "import"} data-testid="start-tab-import" disabled={!!busy}
                    onClick={() => { setRefusal(null); setError(null); onStep("import"); }}><GitBranch size={14} aria-hidden /> Existing · repo or folder</button>
          </div>
          <span style={{ flex: 1 }} />
          <button type="button" className="spj-more spj-sample-link" data-testid="start-tab-sample" disabled={!!busy}
                  aria-current={step === "sample" || step === "choose" ? "page" : undefined}
                  onClick={() => { setRefusal(null); setError(null); onStep("sample"); }}>
            <Flask size={13} aria-hidden /> Try a sample</button>
          <button type="button" className="spj-close" aria-label="Close" title="Close (Esc)" data-testid="start-close"
                  disabled={!!busy} onClick={onClose}><X size={16} weight="bold" aria-hidden /></button>
        </div>

        {step === "choose" ? (
          <>
            <div className="spj-choices" data-testid="start-choices">
              {CHOICES.map((c) => (
                <button key={c.key} type="button" className="spj-choice" data-highlight={c.highlight || undefined}
                        data-testid={`start-${c.key}`} disabled={!!busy}
                        onClick={() => onStep(c.key)}>
                  <span className="spj-tag">{c.time}</span>
                  <b>{c.title}</b>
                  <span className="spj-line">{c.line}</span>
                  <span className="spj-cta">{c.cta}</span>
                </button>
              ))}
            </div>
            <section aria-labelledby={`${titleId}-presets`}>
              <h3 id={`${titleId}-presets`} className="spj-sub">Or start from a preset</h3>
              <div className="spj-presets" data-testid="start-presets">
                {grid.map((p) => (
                  <GatedButton key={p.key} permission="project:create" className="spj-preset" data-testid={`preset-${p.key}`}
                               data-kind={p.kind} disabled={!!busy}
                               title={p.kind === "knowledge_graph" ? "A knowledge-graph project, opened on its sample" : "A project with this brief, ready to Start"}
                               onClick={() => void pickPreset(p)}>
                    <b>{busy === p.key ? "Creating…" : p.title}</b>
                    <span>{p.label}</span>
                  </GatedButton>
                ))}
                {!all ? (
                  <button type="button" className="spj-preset spj-preset-all" data-testid="start-presets-all"
                          onClick={() => setAll(true)}>
                    <b>See all {presets.length}</b>
                  </button>
                ) : null}
              </div>
            </section>
          </>
        ) : null}

        {step === "sample" ? (
          <div className="spj-sample" data-testid="start-sample-step">
            <ol className="spj-flow" aria-label="What happens">
              <li><b>Plan</b><span>You approve</span></li>
              <li><b>Build</b><span>BOSS</span></li>
              <li><b>Review</b><span>BOSS</span></li>
              <li><b>Pull request</b><span>You merge</span></li>
              <li><b>Preview</b><span>Live</span></li>
            </ol>
            <p className="spj-quiet">Hello World. One page and one test. About 8 minutes.
              {" "}<button type="button" className="spj-more" data-testid="start-tab-presets" disabled={!!busy} onClick={() => onStep("choose")}>More samples &amp; presets</button></p>
            {refusal ? (
              <div data-testid="start-sample-readiness">
                <ReadinessPanel readiness={refusal} accepted={startAnyway} onAccept={setStartAnyway} />
              </div>
            ) : null}
            <div className="bx-np-actions">
              <GatedButton permission="project:create" className="ag-btn ag-btn-primary" data-testid="start-sample-go"
                           disabled={!!busy || (!!refusal && open.length > 0)}
                           title="Create the Hello World project and start building it"
                           onClick={() => void runSample()}>
                {busy === "sample" ? "Starting…" : refusal ? "Start anyway" : "Start the sample"}
              </GatedButton>
            </div>
          </div>
        ) : null}

        {step === "idea" ? (
          <div className="spj-idea" data-testid="start-idea-step">
            <label className="bx-np-field">
              <span>Describe it in a sentence</span>
              <textarea className="input spj-ask-text" data-testid="idea-text" rows={3} maxLength={2000} autoFocus
                        value={idea} placeholder="e.g. A shift-swap board where staff offer shifts and a manager approves"
                        onChange={(e) => setIdea(e.target.value)} />
            </label>
            <label className="bx-np-field spj-idea-name">
              <span>Name</span>
              <input className="input" data-testid="idea-name" maxLength={120} value={shownName}
                     placeholder="From your sentence" onChange={(e) => { setIdeaName(e.target.value); setIdeaNamed(true); }} />
            </label>
            <p className="spj-quiet">BOSS plans it, you approve, it builds.</p>
            {refusal ? (
              <div data-testid="start-idea-readiness">
                <ReadinessPanel readiness={refusal} accepted={startAnyway} onAccept={setStartAnyway} />
              </div>
            ) : null}
            <div className="bx-np-actions spj-idea-actions">
              <button type="button" className="spj-more" data-testid="idea-form"
                      title="A knowledge graph for an existing codebase, or a project with no build yet"
                      disabled={!!busy} onClick={() => onStep("form")}>Knowledge graph or blank project</button>
              <GatedButton permission="project:create" className="ag-btn ag-btn-primary" data-testid="idea-go"
                           disabled={!!busy || !idea.trim() || !shownName.trim() || (!!refusal && open.length > 0)}
                           title="Create the project and start planning it"
                           onClick={() => void runIdea()}>
                {busy === "idea" ? "Starting…" : refusal ? "Start anyway" : "Plan it →"}
              </GatedButton>
            </div>
          </div>
        ) : null}

        {step === "import" ? (
          <ImportRepo projectId={projectId} onCreated={(id, type) => go(type === "knowledge_graph"
            ? knowledgePresetHref(id, homeId, "setup") : projectPageHref(id, homeId, "project", "onboarding"))} />
        ) : null}

        {error ? <p className="bx-np-error" role="alert" data-testid="start-error">{error}</p> : null}
      </div>
    </div>
  );
  return typeof document === "undefined" ? sheet : createPortal(sheet, document.body);
}
