"use client";

/**
 * ② Models: what BOSS thinks with. One header — what works, what needs you, and a clear
 * "+ Add a model" — then ONE list of sources (capacity-row.tsx): each a single line with its
 * name, its pill, one plain sentence and one primary action, the rest behind Details and ⋯.
 *
 * Every way into a model (Add, Connect, Sign in, Replace key, a picked catalogue source) opens
 * the one drawer owned here, so there is one surface per job. Closing it, or anything it
 * connects, re-reads the board; the list re-reads its sources whenever the board changes, so the
 * header, the rows and the drawer never disagree (2026-10-25: "Working" above, "Needs sign-in"
 * below, until a reload).
 */
import { GatedButton } from "../../components/gated";
import React, { createContext, forwardRef, useContext, useState } from "react";
import { Plus } from "@phosphor-icons/react";

import { fetchModelList, fetchSetup, SetupFailure, setupWrite } from "../../lib/setup-client";
import { defaultAdd, tileOf } from "../../lib/setup-stepper";
import type { ModelEntry, SetupView } from "../../lib/setup-types";
import { AddModelPanel } from "./add-model-drawer";
import { Drawer } from "./drawer";
import { providerRow } from "./model-status";
import { toast, useBoard } from "./parts";
import { Tip } from "./pill";
import { SetupRow } from "./setup-row";
import { SIGN_IN_PROVIDERS } from "./subscription-sign-in";

/** A subscription BOSS signs in itself: it signs in again, it is never Replaced (BOSSAPP-769). */
export const signsIn = (entry: ModelEntry) => entry.kind === "subscription" && SIGN_IN_PROVIDERS.has(entry.provider);

/** How the drawer opens: Add (provider only), Replace key (sourceId), Sign in (signIn: a fresh
 *  login), or Connect (connect: prove the CLI's current login — nothing is signed out). */
export type Opening = { provider: string; sourceId?: string; signIn?: boolean; connect?: boolean };

export const KIND = {
  subscription: { label: "Subscription", tip: "Your plan. No per-call charge; plan limits apply." },
  metered: { label: "Metered", tip: "Billed per call to your key. Counts toward the budget." },
  local: { label: "Local", tip: "Runs on your hardware. Free." },
} as const;

/** A catalogue source nobody uses yet, offered under the drawer's "More sources". */
export type MoreSource = { id: string; label: string };

type SectionCtx = {
  open: (opening: Opening) => void;
  /** The source the drawer is replacing or signing in right now (its old check is void). */
  editing: string | null;
  /** `?source=<id>`: that row's ⋯ menu opens. */
  menuSource: string | null;
  /** Catalogue sources the list proved unused; the drawer offers them. */
  setMore: (more: MoreSource[]) => void;
  /** The source picked from "More sources": the list shows it with its form open. */
  picked: string | null;
  clearPicked: () => void;
};
const Section = createContext<SectionCtx>({
  open: () => undefined, editing: null, menuSource: null, setMore: () => undefined,
  picked: null, clearPicked: () => undefined,
});
export const useModelsSection = () => useContext(Section);

/** Add a model opens on what is most likely next: a signed-in CLI not yet connected, else the
 *  board's default (never a subscription that already works). */
function addStart(view: SetupView): { provider: string } {
  const ready = view.detected.find((d) => d.kind === "subscription" && d.found === "signed_in"
                                          && providerRow(view, d.provider)?.pill.tone !== "green");
  if (ready) return { provider: ready.provider };
  const start = defaultAdd(view);
  if (providerRow(view, start.provider)?.pill.tone !== "green") return { provider: start.provider };
  const free = view.catalogue.find((c) => c.kind === "api_key" && !providerRow(view, c.provider));
  return { provider: free?.provider ?? start.provider };
}

export function DefaultModel({ entry, onDone }: { entry: ModelEntry; onDone: () => void }) {
  const { projectId, apply } = useBoard();
  const [value, setValue] = useState(entry.model ?? "");
  const [options, setOptions] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  React.useEffect(() => {
    fetchModelList(projectId, entry.provider, entry.source_id).then(setOptions).catch(() => setOptions([]));
  }, [projectId, entry.provider, entry.source_id]);
  return (
    <div className="su-line" data-testid="su-default-model">
      <label className="su-field su-field-inline">
        <span className="su-field-label">Model</span>
        <input list={`su-dm-${entry.source_id}`} value={value} aria-label="Model"
               onChange={(e) => setValue(e.target.value)} />
        <datalist id={`su-dm-${entry.source_id}`}>
          {options.map((m) => <option key={m} value={m} />)}
        </datalist>
      </label>
      <GatedButton permission="model_provider:manage" type="button" className="su-btn su-btn-sm su-btn-primary" disabled={busy || !value.trim()}
              onClick={async () => {
                setBusy(true);
                try {
                  apply(await setupWrite<SetupView>(projectId, "source_model", {
                    source_id: entry.source_id, model: value.trim() }, { label: "Save" }));
                  onDone();
                } catch (error) {
                  toast((error as SetupFailure).refusal?.headline ?? "Check failed", "error");
                } finally { setBusy(false); }
              }}>Save</GatedButton>
      <button type="button" className="su-link" onClick={onDone}>Cancel</button>
    </div>
  );
}

/** Catalogue sources the list proved unused (Cursor, Gemini, …): pick one, and its row appears
 *  in the list, its action focused. Was a small "Add source" disclosure among the
 *  rows that nobody found (2026-10-25). */
function MoreSources({ more, onPick }: { more: MoreSource[]; onPick: (id: string) => void }) {
  if (!more.length) return null;
  return (
    <label className="su-field su-more-sources" data-testid="setup-cap-add">
      <Tip tip="Catalogue sources nobody uses yet: pick one to add its key or sign-in steps."
           className="su-field-label">More sources</Tip>
      <select value="" aria-label="More sources" data-testid="setup-cap-chooser"
              onChange={(e) => { if (e.target.value) onPick(e.target.value); }}>
        <option value="">Choose a source</option>
        {more.map((m) => <option key={m.id} value={m.id}>{m.label}</option>)}
      </select>
    </label>
  );
}

/** "2 working · 1 needs you": the section in five words; each count's rows in its tooltip. */
function Summary({ models }: { models: ModelEntry[] }) {
  const working = models.filter((m) => m.pill.tone === "green");
  const needs = models.filter((m) => m.pill.tone === "red" || m.pill.tone === "amber");
  if (!models.length) return <span className="su-faint">None connected yet</span>;
  return (
    <span className="su-line su-models-summary" data-testid="su-models-summary">
      <Tip tip={working.length ? "Models that answered their last check." : "Nothing answers yet."}
           className={working.length ? "su-models-count su-count-ok" : "su-models-count"}>
        {working.length} working
      </Tip>
      {needs.length ? (
        <Tip tip="Rows below with a red or amber pill: each says its fix."
             className="su-models-count su-count-needs" testid="su-models-needs">
          {needs.length} {needs.length === 1 ? "needs" : "need"} you
        </Tip>
      ) : null}
    </span>
  );
}

export const ModelsRow = forwardRef<HTMLElement, {
  mode: string; step: number | null; focused: boolean; add: string | null; source: string | null;
  onAddHandled: () => void; children?: React.ReactNode;
  /** Repo and models' board (BOSSAPP-808): the one Models block — a heading, the summary and
   *  "+ Add model" over the full list — instead of a numbered row. Same drawer, same list. */
  block?: boolean;
}>(function ModelsRow({ mode, step, focused, add, source, onAddHandled, children, block = false }, ref) {
  const { view, projectId, apply } = useBoard();
  const [drawer, setDrawer] = useState<Opening | null>(null);
  const [more, setMore] = useState<MoreSource[]>([]);
  const [picked, setPicked] = useState<string | null>(null);
  const opening: Opening | null = drawer ?? (add ? { provider: add } : null);
  // CLOSING RE-READS THE BOARD: a sign-in that finished without Connect, or a Connect whose
  // answer was applied, leaves every row, chip and the header on the same, fresh view.
  const close = () => {
    setDrawer(null);
    onAddHandled();
    void fetchSetup(projectId).then(apply).catch(() => undefined);
  };
  const start = addStart(view);
  const firstRunInline = mode === "current" && view.models.length === 0;
  const ctx: SectionCtx = {
    open: setDrawer, editing: drawer && !drawer.connect ? drawer.sourceId ?? null : null,
    menuSource: source, setMore, picked, clearPicked: () => setPicked(null),
  };

  const addButton = (
    <GatedButton permission="model_provider:manage" type="button"
                 className="su-btn su-btn-primary su-add-model-btn" data-testid="su-add-model-open"
                 title="Connect a subscription, an API key or a local model."
                 data-tip="Connect a subscription, an API key or a local model."
                 onClick={() => setDrawer({ provider: start.provider })}>
      <Plus size={14} weight="bold" aria-hidden="true" />Add a model
    </GatedButton>
  );
  let body: React.ReactNode;
  if (mode === "waiting" && !view.models.length) {
    // An earlier step is open; adding a model stays possible (it never waited before either).
    body = (
      <div className="su-models-head" data-testid="su-models-head">
        <span className="su-faint su-model-empty">Not connected</span>
        <span className="su-spacer" />
        {addButton}
      </div>
    );
  } else if (firstRunInline) {
    body = (
      <div>
        <span className="su-row-title">Add a model</span>
        <AddModelPanel key={opening?.provider ?? "first"}
                       initialTile={opening ? tileOf(view, opening.provider) : defaultAdd(view).tile}
                       initialProvider={opening?.provider ?? defaultAdd(view).provider} onDone={close} />
        <MoreSources more={more} onPick={pick} />
      </div>
    );
  } else {
    body = (
      <div className="su-models-head" data-testid="su-models-head">
        <Summary models={view.models} />
        <span className="su-spacer" />
        {addButton}
      </div>
    );
  }
  const showDrawer = opening && !firstRunInline;
  function pick(id: string) {
    setPicked(id);
    setDrawer(null);
    onAddHandled();
  }
  const title = opening?.signIn ? "Sign in" : opening?.connect ? "Connect"
    : opening?.sourceId ? "Replace key" : "Add a model";
  const drawerNode = showDrawer ? (
    <Drawer title={title} onClose={close} testid="su-model-drawer">
      <AddModelPanel key={`${opening!.provider}:${drawer?.sourceId ?? ""}:${opening!.signIn ? "in" : ""}`}
                     initialTile={tileOf(view, opening!.provider)}
                     initialProvider={opening!.provider} onDone={close}
                     replaceSourceId={opening!.signIn || opening!.connect ? undefined : drawer?.sourceId}
                     startSignIn={!!opening!.signIn} />
      {!opening!.sourceId && !opening!.signIn && !opening!.connect ? (
        <MoreSources more={more} onPick={pick} />
      ) : null}
    </Drawer>
  ) : null;
  if (block) {
    return (
      <Section.Provider value={ctx}>
        <section ref={ref} className="su-block su-models-block" data-testid="su-models-block" id="su-models"
                 aria-label="Models" data-focused={focused || undefined}>
          <header className="su-block-head">
            <h2 className="su-block-title" title="What BOSS thinks with: subscriptions, API keys and local models."
                data-tip="What BOSS thinks with: subscriptions, API keys and local models.">Models</h2>
            {view.models.length ? <Summary models={view.models} /> : null}
            <span className="su-spacer" />
            {addButton}
          </header>
          {view.models.length ? null : <span className="su-faint" data-testid="su-models-empty">No model yet</span>}
          {children}
          {drawerNode}
        </section>
      </Section.Provider>
    );
  }
  return (
    <Section.Provider value={ctx}>
      <SetupRow ref={ref} row="models" label="Models" step={step}
                mode={mode} done={mode === "done"} focused={focused}
                tip="What BOSS thinks with: subscriptions, keys and local models.">
        {body}
        {drawerNode}
        {children}
      </SetupRow>
    </Section.Provider>
  );
});

/** A model that does every job lists six names (eight words): the row shows three and a count,
 *  and the tooltip names them all, so the line stays inside the copy budget. */
const USED_SHOWN = 3;
export function usedForLabel(jobs: string[]): string {
  if (!jobs.length) return "—";
  const shown = jobs.slice(0, USED_SHOWN).join(" · ");
  return jobs.length > USED_SHOWN ? `${shown} +${jobs.length - USED_SHOWN}` : shown;
}
export function usedForTip(jobs: string[]): string {
  return jobs.length > USED_SHOWN ? `Used for ${jobs.join(", ")}.`
                                  : "Jobs this model does now, including Auto picks.";
}
