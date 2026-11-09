"use client";

/**
 * THE ONE "ADD A PROJECT" SHEET, WHEREVER IT IS ASKED FOR (screen 03; PX-20; v2 one screen:
 * app/start-project/add-project.tsx, which also absorbs Workflows › New build's new app).
 *
 * `?new_project=import|idea|sample|1` opens it — the Projects page's `+ Add project`, the rail's
 * `+ Add project`, the first run's doors and the project menu all write that. It used to be
 * mounted only by the project menu in the top bar; when the top bar stopped drawing the menu on
 * the Projects page, `+ Add project` silently did nothing. Every surface that can ask for it
 * mounts this host, and exactly ONE mounted host draws the sheet (the first to mount), so the
 * project page — which also has the project menu — never shows two.
 *
 * It opens whatever is connected: with no GitHub account or model yet, the sheet still opens and
 * says so inside (contract §15: a read-only status linking to Repo and models, no picker).
 */
import React, { useEffect, useState, useSyncExternalStore } from "react";

import { EMPTY_DRAFT, NewProjectSheet, type NewProjectDraft } from "../new-project-sheet";
import { AddProjectSheet } from "./add-project";
import { NEW_PROJECT_PARAM, clearStepParam, projectPageHref, stepFromParam, type StartStep } from "../../lib/start-project";

// ── one owner among the mounted hosts ──────────────────────────────────────────────────────
const owners: number[] = [];
const listeners = new Set<() => void>();
let nextId = 1;
const emit = () => listeners.forEach((l) => l());
const subscribe = (l: () => void) => { listeners.add(l); return () => { listeners.delete(l); }; };

/** True for the first mounted caller only (the others stand by and take over if it unmounts). */
export function useIsStartOwner(): boolean {
  const [id] = useState(() => nextId++);
  useEffect(() => {
    owners.push(id);
    emit();
    return () => { const i = owners.indexOf(id); if (i >= 0) owners.splice(i, 1); emit(); };
  }, [id]);
  return useSyncExternalStore(subscribe, () => owners[0] === id, () => false);
}

/** Open the sheet at a step from anywhere (pushes `?new_project=` and tells the hosts). */
export function openStartSheet(step: StartStep = "choose"): void {
  try {
    const url = new URL(window.location.href);
    url.searchParams.set(NEW_PROJECT_PARAM, step === "choose" ? "1" : step === "form" ? "1" : step);
    window.history.pushState(null, "", url.toString());
    window.dispatchEvent(new PopStateEvent("popstate"));
  } catch { /* non-browser */ }
}

function knowledgeSetup(id: string, homeId: string): string {
  const p = new URLSearchParams(typeof window === "undefined" ? "" : window.location.search);
  for (const k of [NEW_PROJECT_PARAM, "task", "run", "build"]) p.delete(k);
  if (id === homeId) p.delete("project_id"); else p.set("project_id", id);
  p.set("area", "knowledge");
  p.set("kg_tab", "setup");
  return `/?${p.toString()}`;
}

export function StartProjectHost({ projectId, homeId, step: forced, onStep: forcedOnStep }: {
  projectId: string; homeId: string;
  /** A caller that keeps the step itself (the project menu); otherwise the URL is the state. */
  step?: StartStep | null; onStep?: (s: StartStep | null) => void;
}) {
  const owner = useIsStartOwner();
  const [own, setOwn] = useState<StartStep | null>(null);
  const step = forced !== undefined ? forced : own;
  const setStep = forcedOnStep ?? setOwn;
  const [draft, setDraft] = useState<NewProjectDraft>(EMPTY_DRAFT);
  useEffect(() => {
    const read = () => {
      try {
        const asked = stepFromParam(new URL(window.location.href).searchParams.get(NEW_PROJECT_PARAM));
        if (asked) setStep(asked);
      } catch { /* non-browser */ }
    };
    read();
    window.addEventListener("popstate", read);
    return () => window.removeEventListener("popstate", read);
  }, [setStep]);
  if (!owner || !step) return null;
  const close = () => { setStep(null); clearStepParam(); };
  // "Build it" from a dossier or a panel (lib/research-panels composerHref) and the Command
  // Center's ideas carry the brief: written into the one input, never started unseen; consumed.
  const prefill = (() => {
    try {
      const p = new URL(window.location.href).searchParams;
      const brief = p.get("brief");
      if (!brief) return "";
      const title = p.get("title");
      return title && !brief.startsWith(title) ? `${title}\n${brief}` : brief;
    } catch { return ""; }
  })();
  if (step === "form") {
    return (
      <NewProjectSheet draft={draft} onDraft={setDraft} onClose={close} onBack={() => setStep("idea")}
                       onCreated={(id, type) => {
                         setDraft(EMPTY_DRAFT);
                         clearStepParam();
                         window.location.assign(type === "knowledge_graph" ? knowledgeSetup(id, homeId) : projectPageHref(id, homeId, "project", "onboarding"));
                       }} />
    );
  }
  // ADD A PROJECT v2 (new-project-v2): ONE screen for every door — an idea, a repo, a folder, the
  // sample in one press. The old three-tab sheet (start-sheet.tsx) stays for one release behind it.
  // TWO DOORS (BOSSAPP-916): an idea opens Start a new app, import opens Bring existing code; the
  // plain link and the sample open the two doors (a handed-over brief is always a new app).
  const startKind = step === "import" ? "repo" : step === "sample" ? "sample" : step === "idea" || prefill ? "idea" : "choose";
  return <AddProjectSheet key={step} startKind={startKind} initialText={prefill} onClose={close} onForm={() => setStep("form")} projectId={projectId} homeId={homeId} />;
}
