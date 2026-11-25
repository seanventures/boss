"use client";

/**
 * Who does what, as cards (BOSSAPP-1341): one card per job showing the job, its engine (icon),
 * model, how it is paid, who chose it and its health. Change opens the editor right beneath the
 * summary, in place; Advanced keeps the same controls (fold, never remove).
 */
import React, { useState } from "react";
import {
  Brain, ChatCircle, Code, Cpu, GitFork, Hammer, Lightning, MagnifyingGlass, Mountains,
  ShareNetwork, Sparkle, Wrench,
} from "@phosphor-icons/react";

import { GatedButton } from "../../components/gated";
import { PICK_LABEL, splitPick, payWay } from "../../lib/job-picks";
import type { Job, SetupView } from "../../lib/setup-types";
import { JobLine } from "./jobs-row";
import { Dot, Pill, Tip } from "./pill";

const JOB_ICON: Record<string, React.ComponentType<{ size?: number; weight?: "regular" | "bold" | "fill"; "aria-hidden"?: boolean }>> = {
  build: Hammer, review: MagnifyingGlass, chat: ChatCircle, decisions: GitFork,
  hard: Mountains, routine: Wrench,
};
const ENGINE_ICON: Record<string, typeof Cpu> = {
  claude_code: Sparkle, claude_agent_sdk: Sparkle, codex: Code, grok: Lightning,
  openrouter: ShareNetwork, native: Brain,
};
const ENGINE_NAME: Record<string, string> = {
  claude_code: "Claude Code", claude_agent_sdk: "Claude", codex: "Codex", grok: "Grok Build",
  openrouter: "OpenRouter", native: "API key",
};
const JOB_TIPS: Record<string, string> = {
  build: "Writes the code and its tests.",
  review: "Reviews each change. Best on a different model.",
  chat: "Answers in the console, agents and meetings.",
  decisions: "Picks a workflow branch from fixed options.",
  hard: "Escalations, repairs and security checks.",
  routine: "Small, well-specified changes.",
};

export function JobCard({ job }: { job: Job }) {
  const [editing, setEditing] = useState(false);
  const Icon = JOB_ICON[job.job] ?? Wrench;
  const r = job.resolved;
  const EngineIcon = (r && ENGINE_ICON[r.engine]) || Cpu;
  const model = r ? splitPick(r.model).bare : null;
  const engineName = r ? (ENGINE_NAME[r.engine] ?? r.label.split(" · ")[0]) : null;
  const auto = !job.choice;
  const way = payWay(job);
  return (
    <div className="su-card su-jobcard" data-testid={`su-who-card-${job.job}`} data-editing={editing || undefined}>
      <div className="su-jobcard-head">
        <Tip tip={JOB_TIPS[job.job] ?? job.label} className="su-jobcard-name">
          <Icon size={16} aria-hidden /> {job.label}
        </Tip>
        {r ? (r.pill ? <Pill pill={r.pill} testid={`su-who-pill-${job.job}`} />
          : <span className="su-pill su-tone-grey" title="No health check has reported for this job yet."
                  data-testid={`su-who-health-${job.job}`}><Dot tone="grey" />Not checked</span>)
          : <span className="su-pill su-tone-amber" title="Nothing can do this job yet."
                  data-testid={`su-who-health-${job.job}`}><Dot tone="amber" />Not set up</span>}
      </div>
      {r ? (
        <div className="su-jobcard-what" data-testid={`su-who-${job.job}`} title={r.why} data-tip={r.why}>
          <EngineIcon size={18} aria-hidden />
          <span className="su-jobcard-model">
            <strong>{engineName}</strong>{model ? <span className="su-faint"> · {model}</span> : null}
          </span>
        </div>
      ) : <span className="su-faint">Connect a model first</span>}
      <div className="su-jobcard-meta">
        {r ? (
          <Tip tip={way === "key" ? "Runs on an API key: billed per call." : "Runs on the subscription's included usage."}
               className={`su-chip ${way === "key" ? "su-chip-metered" : ""}`}
               testid={`su-who-paid-${job.job}`}>{PICK_LABEL[way]}</Tip>
        ) : null}
        <Tip tip={auto ? "BOSS picks: your choice, else a working subscription, else a key."
                       : job.scope === "project" ? "Pinned for this project only." : "Set once: every project in this org uses it."}
             className="su-chip">{auto ? "Auto" : job.scope === "project" ? "This project" : "All projects"}</Tip>
        <GatedButton permission="setup:manage" type="button" className="su-link su-jobcard-change"
                     aria-expanded={editing} data-testid={`su-who-change-${job.job}`}
                     onClick={() => setEditing((v) => !v)}>{editing ? "Done" : "Change"}</GatedButton>
      </div>
      {editing ? <div className="su-jobcard-edit" data-testid={`su-who-edit-${job.job}`}><JobLine job={job} /></div> : null}
    </div>
  );
}

/** "Who does what": one card per job. */
export function WhoDoesWhat({ view }: { view: SetupView; onChange?: (job?: string) => void }) {
  const jobs = view.jobs.filter((j) => j.resolved || !j.more);
  if (!jobs.length) return null;
  return (
    <div className="su-whocards" data-testid="su-who-does-what">
      <span className="su-faint su-who-title">Who does what</span>
      <div className="su-cards su-jobcards">
        {jobs.map((job) => <JobCard key={job.job} job={job} />)}
      </div>
    </div>
  );
}
