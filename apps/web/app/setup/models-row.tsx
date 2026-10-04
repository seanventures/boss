"use client";

/**
 * ② Models: one row per model source — a subscription, a key, a local model, or one from the
 * install's settings file — each with its kind, the jobs it does now, and its last check.
 */
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
      <button type="button" className="su-btn su-btn-sm su-btn-primary" disabled={busy || !value.trim()}
              onClick={async () => {
                setBusy(true);
                try {
                  apply(await setupWrite<SetupView>(projectId, "source_model", {
                    source_id: entry.source_id, model: value.trim() }, { label: "Save" }));
                  onDone();
                } catch (error) {
                  toast((error as SetupFailure).refusal?.headline ?? "Check failed", "error");
                } finally { setBusy(false); }
              }}>Save</button>
    </div>
  );
}

function ModelLine({ entry, menuOpen, onReplace }: {
  entry: ModelEntry; menuOpen: boolean; onReplace: (provider: string) => void;
}) {
  const { projectId, apply, checking, setChecking } = useBoard();
  const key = `model:${entry.source_id}`;
  const busy = checking.has(key) || checking.has("all");
  const pill = busy ? { state: "checking", tone: "busy" as const, label: "Checking…",
                        tip: "Asking one tiny question. Budgeted." } : entry.pill;
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
        {entry.source === "install" ? (
          <Tip tip="Set in the server's settings file. Replace here to manage it in BOSS."
               className="su-chip">From install</Tip>
        ) : null}
        {entry.model ? <span className="su-faint su-mono" data-user-text>{entry.model}</span> : null}
        <Tip tip="Jobs this model does now, including Auto picks." className="su-used">
          {entry.used_for.length ? entry.used_for.join(" · ") : "—"}
        </Tip>
        {entry.checked_at ? (
          <Tip tip={`Last checked ${ageLabel(entry.checked_at)} ago.`} className="su-faint su-age">
            Checked {ageLabel(entry.checked_at)}
          </Tip>
        ) : null}
        <RowMenu label={`${entry.label} actions`} testid="su-model-menu" open={menuOpen} items={[
          { label: "Test", onSelect: test, testid: "su-menu-test" },
          { label: "Replace", onSelect: () => onReplace(entry.provider), testid: "su-menu-replace" },
          ...(canPick ? [{ label: "Default model…", onSelect: () => setPicking(true),
                           testid: "su-menu-default-model" }] : []),
          ...(entry.source === "connected" ? [{ label: "Remove", danger: true,
                                                onSelect: () => void remove(false),
                                                testid: "su-menu-remove" }] : []),
        ]} />
      </div>
      {red || pill.tone === "amber" ? (
        <div className="su-line su-fix">
          {entry.pill.action === "replace" ? (
            <button type="button" className="su-btn su-btn-sm" onClick={() => onReplace(entry.provider)}>
              Replace
            </button>
          ) : null}
          {entry.pill.action === "fix_link" && entry.pill.fix_url ? (
            <Out href={entry.pill.fix_url}>Fix</Out>
          ) : null}
          {entry.pill.action === "default_model" && canPick ? (
            <button type="button" className="su-btn su-btn-sm" onClick={() => setPicking(true)}>
              Default model…
            </button>
          ) : null}
          {entry.pill.action === "budget" ? (
            <a className="su-link" href="?area=setup&focus=budget">Budget</a>
          ) : null}
          {entry.pill.action === "test" || entry.pill.action === "fix_link" ? (
            <button type="button" className="su-btn su-btn-sm" onClick={test} disabled={busy}>Test</button>
          ) : null}
          {red ? <AskBoss row={entry.label} pillLabel={entry.pill.label} tip={entry.pill.tip} /> : null}
        </div>
      ) : null}
      {picking ? <DefaultModel entry={entry} onDone={() => setPicking(false)} /> : null}
      {confirm ? (
        <div className="su-confirm" role="alertdialog" data-testid="su-remove-confirm">
          <strong>Remove {entry.label}?</strong>
          <span>{confirm.map((job) => `${job} goes back to Auto.`).join(" ")}</span>
          <button type="button" className="su-btn su-btn-sm su-btn-danger"
                  data-testid="su-remove-confirm-yes" onClick={() => void remove(true)}>Remove</button>
          <button type="button" className="su-link" onClick={() => setConfirm(null)}>Keep</button>
        </div>
      ) : null}
    </div>
  );
}

export const ModelsRow = forwardRef<HTMLElement, {
  mode: string; step: number | null; focused: boolean; add: string | null; source: string | null;
  onAddHandled: () => void;
}>(function ModelsRow({ mode, step, focused, add, source, onAddHandled }, ref) {
  const { view } = useBoard();
  const [drawer, setDrawer] = useState<{ provider: string } | null>(null);
  const opening = drawer ?? (add ? { provider: add } : null);
  const close = () => { setDrawer(null); onAddHandled(); };
  const start = defaultAdd(view);
  const working = view.models.find((m) => m.pill.tone === "green") ?? view.models[0];

  let body: React.ReactNode;
  if (mode === "done" && working) {
    body = <span className="su-line"><span>{working.label}</span><Pill pill={working.pill} /></span>;
  } else if (mode === "waiting") {
    body = <span className="su-faint">Not connected</span>;
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
                     onReplace={(provider) => setDrawer({ provider })} />
        ))}
        <button type="button" className="su-link su-add" data-testid="su-add-model-open"
                onClick={() => setDrawer({ provider: start.provider })}>
          <Plus size={12} aria-hidden="true" />Add model
        </button>
      </div>
    );
  }
  const showDrawer = opening && !(mode === "current" && view.models.length === 0);
  return (
    <SetupRow ref={ref} row="models" label={mode === "open" ? "Models" : "Model"} step={step}
              mode={mode} done={mode === "done"} focused={focused}
              tip="What BOSS thinks with: subscriptions, keys and local models.">
      {body}
      {showDrawer ? (
        <Drawer title="Add a model" onClose={close} testid="su-model-drawer">
          <AddModelPanel key={opening!.provider} initialTile={tileOf(view, opening!.provider)}
                         initialProvider={opening!.provider} onDone={close} />
        </Drawer>
      ) : null}
    </SetupRow>
  );
});
