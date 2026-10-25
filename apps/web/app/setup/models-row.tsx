"use client";

/**
 * ② Models: one row per model source — a subscription, a key, a local model, or one from the
 * install's settings file — each with its kind, the jobs it does now, and its last check.
 */
import { GatedButton } from "../../components/gated";
import React, { forwardRef, useState } from "react";
import { Plus } from "@phosphor-icons/react";

import { fetchModelList, SetupFailure, setupWrite } from "../../lib/setup-client";
import { ageLabel, defaultAdd, tileOf } from "../../lib/setup-stepper";
import type { ModelEntry, SetupView } from "../../lib/setup-types";
import { AddModelPanel } from "./add-model-drawer";
import { Drawer } from "./drawer";
import { AskBoss, RowMenu, toast, useBoard } from "./parts";
import { Out, Pill, Tip } from "./pill";
import { SetupRow } from "./setup-row";
import { SIGN_IN_PROVIDERS } from "./subscription-sign-in";

/** A subscription BOSS signs in itself: it signs in again, it is never Replaced (BOSSAPP-769). */
const signsIn = (entry: ModelEntry) => entry.kind === "subscription" && SIGN_IN_PROVIDERS.has(entry.provider);

type Opening = { provider: string; sourceId?: string; signIn?: boolean };

const KIND = {
  subscription: { label: "Subscription", tip: "Your plan. No per-call charge; plan limits apply." },
  metered: { label: "Metered", tip: "Billed per call to your key. Counts toward the budget." },
  local: { label: "Local", tip: "Runs on your hardware. Free." },
} as const;

function DefaultModel({ entry, onDone }: { entry: ModelEntry; onDone: () => void }) {
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
    </div>
  );
}

function ModelLine({ entry, menuOpen, onReplace, onSignIn, replacing }: {
  entry: ModelEntry; menuOpen: boolean; onReplace: (provider: string, sourceId: string) => void;
  onSignIn: (provider: string, sourceId: string) => void;
  /** The Add model drawer is open to paste this row a new key. Its last check no longer
   *  describes anything — a stale "Paused · budget" sat there while the operator typed a
   *  fresh key that had not been tested yet (boss-1 rc10.9, Setup > Add a model). */
  replacing: boolean;
}) {
  const again = signsIn(entry);
  const { projectId, apply, checking, setChecking } = useBoard();
  const key = `model:${entry.source_id}`;
  const busy = checking.has(key) || checking.has("all");
  const pill = busy ? { state: "checking", tone: "busy" as const, label: "Checking…",
                        tip: "Asking one tiny question. Budgeted." }
             : replacing && again ? { state: "editing", tone: "grey" as const, label: "Signing in…",
                                      tip: "Signing in again. The old check no longer applies." }
             : replacing ? { state: "editing", tone: "grey" as const, label: "Editing…",
                             tip: "Testing a new key. The old check no longer applies." }
             : entry.pill;
  const [picking, setPicking] = useState(false);
  const [confirm, setConfirm] = useState<string[] | null>(null);
  const test = async () => {
    setChecking(key, true);
    try {
      apply(await setupWrite<SetupView>(projectId, "checks", { target: key }, { label: "Test" }));
    } catch (error) {
      toast((error as SetupFailure).refusal?.headline ?? "Check failed", "error");
    } finally { setChecking(key, false); }
  };
  const remove = async (confirmed: boolean) => {
    try {
      apply(await setupWrite<SetupView>(projectId, "model_remove", {
        source_id: entry.source_id, confirm: confirmed ? "true" : undefined }, { label: "Remove" }));
      setConfirm(null);
    } catch (error) {
      const failure = error as SetupFailure;
      if (failure.status === 409 && failure.refusal?.jobs?.length) setConfirm(failure.refusal.jobs);
      else toast(failure.refusal?.headline ?? "Not removed", "error");
    }
  };
  const red = pill.tone === "red";
  const canPick = entry.kind !== "subscription" && entry.source === "connected";
  return (
    <div className="su-model" data-testid={`su-model-${entry.source_id}`}>
      <div className="su-line">
        <Pill pill={pill} testid="su-model-pill" />
        <span className="su-strong">{entry.label}</span>
        <Tip tip={KIND[entry.kind].tip} className="su-chip">{KIND[entry.kind].label}</Tip>
        {entry.scope === "org" ? (
          <Tip tip="The org's connection: set once, every project in this org uses it."
               className="su-chip" testid="su-model-scope">All projects</Tip>
        ) : null}
        {entry.source === "install" ? (
          <Tip tip="Set in the server's settings file. Replace here to manage it in BOSS."
               className="su-chip">From install</Tip>
        ) : null}
        {entry.model ? <span className="su-faint su-mono" data-user-text>{entry.model}</span> : null}
        <Tip tip={usedForTip(entry.used_for)} className="su-used" testid="su-model-used">
          {usedForLabel(entry.used_for)}
        </Tip>
        {entry.checked_at ? (
          <Tip tip={`Last checked ${ageLabel(entry.checked_at)} ago.`} className="su-faint su-age">
            Checked {ageLabel(entry.checked_at)}
          </Tip>
        ) : null}
        <RowMenu label={`${entry.label} actions`} testid="su-model-menu" open={menuOpen} items={[
          { label: "Test", onSelect: test, testid: "su-menu-test" },
          again ? { label: "Sign in", onSelect: () => onSignIn(entry.provider, entry.source_id),
                    testid: "su-menu-sign-in" }
                : { label: "Replace", onSelect: () => onReplace(entry.provider, entry.source_id),
                    testid: "su-menu-replace" },
          ...(canPick ? [{ label: "Default model…", onSelect: () => setPicking(true),
                           testid: "su-menu-default-model" }] : []),
          ...(entry.source === "connected" ? [{ label: "Remove", danger: true,
                                                onSelect: () => void remove(false),
                                                testid: "su-menu-remove" }] : []),
        ]} />
      </div>
      {red || pill.tone === "amber" ? (
        <div className="su-line su-fix">
          {entry.pill.action === "sign_in" || (entry.pill.action === "replace" && again) ? (
            <GatedButton permission="model_provider:manage" type="button" className="su-btn su-btn-sm"
                         data-testid="su-model-sign-in"
                         title="Runs the provider's sign-in on the BOSS server, then checks it."
                         data-tip="Runs the provider's sign-in on the BOSS server, then checks it."
                         onClick={() => onSignIn(entry.provider, entry.source_id)}>
              Sign in
            </GatedButton>
          ) : null}
          {entry.pill.action === "replace" && !again ? (
            <GatedButton permission="model_provider:manage" type="button" className="su-btn su-btn-sm"
                    onClick={() => onReplace(entry.provider, entry.source_id)}>
              Replace
            </GatedButton>
          ) : null}
          {entry.pill.action === "fix_link" && entry.pill.fix_url ? (
            <Out href={entry.pill.fix_url}>Fix</Out>
          ) : null}
          {entry.pill.action === "default_model" && canPick ? (
            <GatedButton permission="model_provider:manage" type="button" className="su-btn su-btn-sm" onClick={() => setPicking(true)}>
              Default model…
            </GatedButton>
          ) : null}
          {entry.pill.action === "budget" ? (
            <a className="su-link" href="?area=setup&focus=budget">Budget</a>
          ) : null}
          {entry.pill.action === "test" || entry.pill.action === "fix_link" ? (
            <GatedButton permission="setup:manage" type="button" className="su-btn su-btn-sm" onClick={test} disabled={busy}>Test</GatedButton>
          ) : null}
          {red ? <AskBoss row={entry.label} pillLabel={entry.pill.label} tip={entry.pill.tip} /> : null}
        </div>
      ) : null}
      {picking ? <DefaultModel entry={entry} onDone={() => setPicking(false)} /> : null}
      {confirm ? (
        <div className="su-confirm" role="alertdialog" data-testid="su-remove-confirm">
          <strong>Remove {entry.label}?</strong>
          <span>{confirm.map((job) => `${job} goes back to Auto.`).join(" ")}</span>
          <GatedButton permission="model_provider:manage" type="button" className="su-btn su-btn-sm su-btn-danger"
                  data-testid="su-remove-confirm-yes" onClick={() => void remove(true)}>Remove</GatedButton>
          <button type="button" className="su-link" onClick={() => setConfirm(null)}>Keep</button>
        </div>
      ) : null}
    </div>
  );
}

export const ModelsRow = forwardRef<HTMLElement, {
  mode: string; step: number | null; focused: boolean; add: string | null; source: string | null;
  onAddHandled: () => void; children?: React.ReactNode;
}>(function ModelsRow({ mode, step, focused, add, source, onAddHandled, children }, ref) {
  const { view } = useBoard();
  const [drawer, setDrawer] = useState<Opening | null>(null);
  const opening: Opening | null = drawer ?? (add ? { provider: add } : null);
  const close = () => { setDrawer(null); onAddHandled(); };
  const start = defaultAdd(view);
  const working = view.models.find((m) => m.pill.tone === "green") ?? view.models[0];

  let body: React.ReactNode;
  // A finished step, or a later one that already has a model (connected before the
  // repository broke): one line with the model and its pill, never "Not connected".
  if ((mode === "done" || mode === "waiting") && working) {
    body = (
      <span className="su-line">
        <span>{working.label}</span>
        <Pill pill={working.pill} testid="su-model-pill" />
        {working.source === "install" ? (
          <Tip tip="Set in the server's settings file. Replace here to manage it in BOSS."
               className="su-chip">From install</Tip>
        ) : null}
        <RowMenu label={`${working.label} actions`} testid="su-model-menu" items={[
          signsIn(working)
            ? { label: "Sign in", onSelect: () => setDrawer({ provider: working.provider,
                                                              sourceId: working.source_id, signIn: true }),
                testid: "su-menu-sign-in" }
            : { label: "Replace", onSelect: () => setDrawer({ provider: working.provider,
                                                              sourceId: working.source_id }),
                testid: "su-menu-replace" },
          { label: "Add model", onSelect: () => setDrawer({ provider: start.provider }),
            testid: "su-menu-add" },
        ]} />
      </span>
    );
  } else if (mode === "waiting") {
    body = <span className="su-faint su-model-empty">Not connected</span>;
  } else if (mode === "current" && view.models.length === 0) {
    body = (
      <div>
        <span className="su-row-title">Add a model</span>
        <AddModelPanel key={opening?.provider ?? "first"}
                       initialTile={opening ? tileOf(view, opening.provider) : start.tile}
                       initialProvider={opening?.provider ?? start.provider} onDone={close} />
      </div>
    );
  } else {
    body = (
      <div className="su-stack">
        {view.models.map((entry) => (
          <ModelLine key={entry.source_id} entry={entry} menuOpen={source === entry.source_id}
                     replacing={!!drawer?.sourceId && drawer.sourceId === entry.source_id}
                     onReplace={(provider, sourceId) => setDrawer({ provider, sourceId })}
                     onSignIn={(provider, sourceId) => setDrawer({ provider, sourceId, signIn: true })} />
        ))}
        <GatedButton permission="model_provider:manage" type="button" className="su-link su-add" data-testid="su-add-model-open"
                onClick={() => setDrawer({ provider: start.provider })}>
          <Plus size={12} aria-hidden="true" />Add model
        </GatedButton>
      </div>
    );
  }
  const showDrawer = opening && !(mode === "current" && view.models.length === 0);
  return (
    <SetupRow ref={ref} row="models" label="Models" step={step}
              mode={mode} done={mode === "done"} focused={focused}
              tip="What BOSS thinks with: subscriptions, keys and local models.">
      {body}
      {showDrawer ? (
        <Drawer title={opening!.signIn ? "Sign in" : "Add a model"} onClose={close} testid="su-model-drawer">
          <AddModelPanel key={`${opening!.provider}:${drawer?.sourceId ?? ""}:${opening!.signIn ? "in" : ""}`}
                         initialTile={tileOf(view, opening!.provider)}
                         initialProvider={opening!.provider} onDone={close}
                         replaceSourceId={opening!.signIn ? undefined : drawer?.sourceId}
                         startSignIn={!!opening!.signIn} />
        </Drawer>
      ) : null}
      {children}
    </SetupRow>
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
function usedForTip(jobs: string[]): string {
  return jobs.length > USED_SHOWN ? `Used for ${jobs.join(", ")}.`
                                  : "Jobs this model does now, including Auto picks.";
}
