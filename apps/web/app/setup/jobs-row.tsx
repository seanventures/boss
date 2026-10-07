"use client";

/**
 * Who does what: four jobs, one picker each — `Auto · <source>` first, then every working
 * source × its models. Engine and model are saved together in one request; the cost chip says
 * whether the job spends money.
 */
import React, { forwardRef, useEffect, useState } from "react";
import { CaretRight } from "@phosphor-icons/react";

import { SetupFailure, setupWrite } from "../../lib/setup-client";
import type { Job, SetupView } from "../../lib/setup-types";
import { toast, useBoard } from "./parts";
import { Tip } from "./pill";
import { SetupRow } from "./setup-row";

const JOB_TIPS: Record<string, string> = {
  build: "Writes the code and its tests.",
  review: "Reviews each change. Best on a different model.",
  chat: "Answers in the console, agents and meetings.",
  decisions: "Picks a workflow branch from fixed options.",
  hard: "Escalations, repairs and security checks.",
  routine: "Small, well-specified changes.",
};
const AUTO_TIP = "BOSS picks: your choice, else a working subscription, else a key.";

const encode = (engine: string, model: string | null) => `${engine}::${model ?? ""}`;

function AddKey({ onDone }: { onDone: () => void }) {
  const { projectId, view, apply } = useBoard();
  const [key, setKey] = useState("");
  const [consent, setConsent] = useState(false);
  const [busy, setBusy] = useState(false);
  const needs = view.secrets.needs_consent;
  useEffect(() => {
    const escape = (event: KeyboardEvent) => { if (event.key === "Escape") onDone(); };
    document.addEventListener("keydown", escape);
    return () => document.removeEventListener("keydown", escape);
  }, [onDone]);
  return (
    <div className="su-popover" role="dialog" aria-label="Add key" data-testid="su-jev-key">
      <label className="su-field">
        <span className="su-field-label">API key</span>
        <input type="password" autoComplete="off" value={key} aria-label="API key" autoFocus
               title="Stored in BOSS's secret store. Never shown again."
               onChange={(e) => setKey(e.target.value)} />
      </label>
      {needs ? (
        <label className="su-consent" title="No secret store on this server. Encrypted in Postgres.">
          <input type="checkbox" checked={consent} onChange={(e) => setConsent(e.target.checked)} />
          Store in BOSS database
        </label>
      ) : null}
      <button type="button" className="su-btn su-btn-sm su-btn-primary"
              disabled={busy || !key.trim() || (needs && !consent)}
              onClick={async () => {
                setBusy(true);
                const sent = key.trim();
                setKey("");
                try {
                  apply(await setupWrite<SetupView>(projectId, "decision_key", {
                    api_key: sent, accept_database_fallback: needs ? consent : undefined },
                    { label: "Connect" }));
                  toast("Connected · Working");
                  onDone();
                } catch (error) {
                  toast((error as SetupFailure).message || "Key rejected", "error");
                } finally { setBusy(false); }
              }}>Connect</button>
    </div>
  );
}

function storedLabel(job: Job): string {
  const choice = job.choice!;
  const source = job.options.find((o) => o.engine === choice.engine)?.label.split(" · ")[0]
    ?? job.resolved?.label.split(" · ")[0] ?? choice.engine;
  return choice.model ? `${source} · ${choice.model}` : source;
}

export function JobLine({ job }: { job: Job }) {
  const { projectId, view, apply } = useBoard();
  const [busy, setBusy] = useState(false);
  const [adding, setAdding] = useState(false);
  const none = view.models.length === 0;
  const value = job.choice ? encode(job.choice.engine, job.choice.model) : "auto";
  // What Auto resolves to now, when Auto is what runs; "Auto" alone while a choice stands.
  const autoLabel = job.resolved && !job.choice ? job.resolved.label : "Auto";
  const save = async (raw: string) => {
    if (raw === "add-key") { setAdding(true); return; }
    const option = job.options.find((o) => encode(o.engine, o.model) === raw);
    setBusy(true);
    try {
      const next = await setupWrite<SetupView>(projectId, "job", {
        job: job.job, choice: raw === "auto" || !option ? null
          : { engine: option.engine, model: option.model },
      }, { label: job.label });
      apply(next);
      const saved = next.jobs.find((j) => j.job === job.job);
      toast(`Saved · ${job.label} → ${saved?.resolved?.label ?? "Auto"}`);
    } catch (error) {
      toast((error as SetupFailure).refusal?.headline ?? "Not saved", "error");
    } finally { setBusy(false); }
  };
  return (
    <div className="su-job" data-testid={`su-job-${job.job}`}>
      <Tip tip={JOB_TIPS[job.job]} className="su-job-name">{job.label}</Tip>
      {none ? (
        <span className="su-faint">Connect a model first</span>
      ) : (
        <select value={value} disabled={busy} aria-label={job.label} title={AUTO_TIP}
                data-testid={`su-job-select-${job.job}`} onChange={(e) => void save(e.target.value)}>
          <option value="auto">{autoLabel}</option>
          {job.choice && !job.options.some((o) => encode(o.engine, o.model) === value) ? (
            // THE STORED CHOICE IS ALWAYS SHOWN: a model no longer among the options (renamed,
            // its source re-listed) still reads as what is saved, never as "Auto".
            <option value={value}>{storedLabel(job)}</option>
          ) : null}
          {job.options.map((o) => (
            o.needs_key ? (
              <option key="add-key" value="add-key">{o.label} · Add key</option>
            ) : (
              <option key={encode(o.engine, o.model)} value={encode(o.engine, o.model)}>{o.label}</option>
            )
          ))}
        </select>
      )}
      {job.resolved ? (
        <Tip tip={job.metered ? "Metered: counts toward your budget." : "No per-call charge."}
             className={`su-chip ${job.metered ? "su-chip-metered" : ""}`}>
          {job.metered ? "Metered" : "Included"}
        </Tip>
      ) : !none && job.job === "decisions" ? (
        // NOTHING HERE CAN DECIDE: decisions run on Claude Code or TypeSafe Jev, and an API key
        // alone is neither. Said, with the one thing that fixes it.
        <button type="button" className="su-btn su-btn-sm" data-testid="su-decisions-add-key"
                title="Decisions run on Claude Code or TypeSafe Jev."
                data-tip="Decisions run on Claude Code or TypeSafe Jev."
                onClick={() => setAdding(true)}>Add key</button>
      ) : null}
      {adding ? <AddKey onDone={() => setAdding(false)} /> : null}
    </div>
  );
}

export const JobsRow = forwardRef<HTMLElement, { mode: string; focused: boolean }>(
  function JobsRow({ mode, focused }, ref) {
    const { view } = useBoard();
    const [open, setOpen] = useState(false);
    const [more, setMore] = useState(focused);
    useEffect(() => { if (focused) setMore(true); }, [focused]);
    const collapsed = mode === "collapsed" && !open && !focused;
    return (
      <SetupRow ref={ref} row="jobs" label="Who does what" mode={collapsed ? "collapsed" : "open"}
                focused={focused} tip="Which model does each job. Auto picks for you.">
        {collapsed ? (
          <div className="su-line">
            <span className="su-faint">Auto</span>
            <span className="su-chip">Defaults</span>
            <button type="button" className="su-link" data-testid="su-jobs-change"
                    onClick={() => setOpen(true)}>Change</button>
          </div>
        ) : (
          <div className="su-jobs">
            {view.jobs.filter((j) => !j.more || more).map((job) => <JobLine key={job.job} job={job} />)}
            <button type="button" className="su-link su-more" aria-expanded={more}
                    data-testid="su-more-jobs" onClick={() => setMore((v) => !v)}>
              More jobs<CaretRight size={12} className={more ? "su-rot" : undefined} aria-hidden="true" />
            </button>
          </div>
        )}
      </SetupRow>
    );
  });
