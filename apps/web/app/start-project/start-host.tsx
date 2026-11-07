"use client";

/**
 * THE ONE "ADD A PROJECT" SHEET, WHEREVER IT IS ASKED FOR (screen 03; PX-20).
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
import { StartProjectSheet } from "./start-sheet";
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
  if (step === "form") {
    return (
      <NewProjectSheet draft={draft} onDraft={setDraft} onClose={close} onBack={() => setStep("idea")}
                       onCreated={(id, type) => {
                         setDraft(EMPTY_DRAFT);
                         clearStepParam();
                         window.location.assign(type === "knowledge_graph" ? knowledgeSetup(id, homeId) : projectPageHref(id, homeId));
                       }} />
    );
  }
  return <StartProjectSheet step={step} onStep={setStep} onClose={close} projectId={projectId} homeId={homeId} />;
}
