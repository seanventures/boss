"use client";

/**
 * What Agent Studio was for, where the agent already lives.
 *
 * Agent Studio was a third place to look at agents (after the roster and Team structure) and a
 * second place to edit workflows. Its workflow half is the Workflow editor's JSON view now; its
 * agent half is two things in the Agents area:
 *
 *   - AgentProfile, a section of the agent's own page: the Studio definition for that role —
 *     its version, model policy and validation — and a Test that compiles it and proves its
 *     authority envelope without running a model;
 *   - PanelsView, the Agents area's Panels view: each panel, its members, and a Run.
 *
 * The metric tiles and the Live Coordination (A2A) column are gone from the console; the API
 * behind them is kept.
 */
// React by name: the suite renders with renderToStaticMarkup (classic runtime).
import React, { useState, useTransition } from "react";
import { CheckCircle, Flask, Play, Warning } from "@phosphor-icons/react";

import type { AgentStudioFeed, StudioAgent, StudioPanel, StudioRun } from "../lib/agent-studio";
import { callAction } from "../lib/call-action";
import { AgentChip } from "./agent-chip";
import { ClientRelativeTime } from "./client-relative-time";
import { HelpTip } from "./help-tip";
import { runStudioPanel, testStudioAgent, type StudioActionResult } from "./agent-studio-actions";
import { StartRefusal, asReadiness, type PendingRefusal } from "./workflow-readiness-ui";
import type { Persona } from "../lib/agent-world-types";
import { PassportTest } from "./agents/passport-test";

const words = (value: unknown): string =>
  typeof value === "string" && value ? value.replaceAll("_", " ") : "—";

/** The Studio definitions that describe this role. Keyed by role_key, the thing a workflow
 *  step's `captain` names, so the agent page, the roster and the editor agree. */
export function profilesFor(feed: Pick<AgentStudioFeed, "agents">, roleKey: string): StudioAgent[] {
  return feed.agents.filter((a) =>
    a.status !== "retired" && (a.active_version?.role_key === roleKey || a.agent_key === roleKey));
}

/** Definitions no registered agent answers to: offered as templates by New agent, so none is
 *  unreachable once Studio is gone. */
export function orphanProfiles(feed: Pick<AgentStudioFeed, "agents">, roleKeys: Iterable<string>): StudioAgent[] {
  const known = new Set(roleKeys);
  return feed.agents.filter((a) => a.status !== "retired"
    && !known.has(a.active_version?.role_key ?? "") && !known.has(a.agent_key));
}

function Result({ result }: { result?: StudioActionResult }) {
  if (!result) return null;
  return (
    <p className="wf-ag-result" data-ok={result.ok} role="status" data-testid="wf-ag-result">
      {result.ok ? <CheckCircle size={14} aria-hidden /> : <Warning size={14} aria-hidden />}
      <span>{result.message}</span>
    </p>
  );
}

function RecentRuns({ runs }: { runs: StudioRun[] }) {
  if (!runs.length) return null;
  return (
    <ul className="wf-ag-runs" data-testid="wf-ag-runs">
      {runs.slice(0, 5).map((run) => {
        const output = run.output ?? {};
        const summary = typeof output.summary === "string" ? output.summary
          : typeof output.brief === "string" ? output.brief : "";
        return (
          <li key={run.id} title={run.blocked_reason ? `Stopped: ${run.blocked_reason}` : summary || undefined}>
            <span className="wf-tag" data-status={run.status}>{words(run.status)}</span>
            <ClientRelativeTime iso={run.completed_at ?? run.created_at} style="short" empty="" />
          </li>
        );
      })}
    </ul>
  );
}

/** The Profile section of an agent's page. Read-only; Test is the one action — the real one
 *  (a model call in its compiled voice, Agent World) above the Studio definition's compile check. */
export function AgentProfile({ feed, roleKey, actor, draftPersona }: {
  feed: AgentStudioFeed; roleKey: string; actor: string;
  /** An unsaved voice from the Persona editor, so Test hears it before Save. */
  draftPersona?: Persona | null;
}) {
  const profiles = profilesFor(feed, roleKey);
  return (
    <section className="ct-block" data-testid="agent-profile">
      <div className="ct-block-title">
        Profile <HelpTip label="Profile" text="Prompts and model settings for this agent" />
      </div>
      <PassportTest projectId={feed.projectId} actor={actor} roleKey={roleKey} persona={draftPersona} />
      {!feed.apiReachable ? (
        <p className="detail" title={feed.blockedReason}>Unavailable</p>
      ) : profiles.length === 0 ? (
        <p className="detail" data-testid="agent-profile-none">No profile</p>
      ) : profiles.map((agent) => (
        <ProfileRow key={agent.id} agent={agent} feed={feed} actor={actor} />
      ))}
    </section>
  );
}

function ProfileRow({ agent, feed, actor }: { agent: StudioAgent; feed: AgentStudioFeed; actor: string }) {
  const [result, setResult] = useState<StudioActionResult | undefined>();
  const [pending, start] = useTransition();
  const version = agent.active_version;
  const policy = version?.model_policy ?? {};
  const validation = version?.validation ?? {};
  const lastTest = feed.tests.find((t) => t.agent_name === agent.name);
  return (
    <div className="wf-ag-profile" data-testid={`agent-profile-${agent.agent_key}`}>
      <div className="wf-ag-profile-head">
        <strong title={agent.description || undefined}>{agent.name}</strong>
        <span className="wf-tag" title="Each publish is a new version">
          {version ? `v${version.version}` : "No version"}
        </span>
        <span className="wf-tag" data-status={agent.status}>{words(agent.status)}</span>
        <button type="button" className="wf-btn wf-btn-sm" disabled={pending || !version}
                data-testid={`agent-profile-test-${agent.agent_key}`}
                title="Compile it and check what it may do. Runs no model."
                onClick={() => {
                  if (!version) return;
                  setResult(undefined);
                  start(async () => {
                    setResult(await callAction(testStudioAgent)(feed.projectId, version.id, actor));
                  });
                }}>
          <Flask size={12} aria-hidden /> {pending ? "Testing…" : "Test"}
        </button>
      </div>
      {version ? (
        <dl className="wf-ag-facts">
          <div><dt>Model</dt><dd title={words(policy.selection_mode)}>
            {words(policy.pinned_model ?? policy.execution_mode ?? "deterministic_brief")}
          </dd></div>
          <div><dt>Skills</dt><dd title={(version.requested_skills ?? []).join(", ") || undefined}>
            {(version.requested_skills ?? []).length}
          </dd></div>
          <div><dt>Data</dt><dd>{words(version.scopes?.data_boundary)}</dd></div>
          <div><dt>Checks</dt><dd title={[...(validation.errors ?? []), ...(validation.warnings ?? [])].join(" ") || undefined}>
            {validation.valid ? "Passed" : "Pending"}
          </dd></div>
          {lastTest ? (
            <div><dt>Last test</dt><dd title={lastTest.result?.summary}>{words(lastTest.status)}</dd></div>
          ) : null}
        </dl>
      ) : null}
      <Result result={result} />
      <RecentRuns runs={feed.agentRuns.filter((r) => r.agent_key === agent.agent_key)} />
    </div>
  );
}

/** The Agents area's Panels view: who sits on each panel, and a Run. */
export function PanelsView({ feed, actor }: { feed: AgentStudioFeed; actor: string }) {
  return (
    <div className="wf-ag-panels" data-testid="agents-panels">
      {feed.panels.map((panel) => (
        <PanelCard key={panel.id} panel={panel} feed={feed} actor={actor} />
      ))}
    </div>
  );
}

function PanelCard({ panel, feed, actor }: { panel: StudioPanel; feed: AgentStudioFeed; actor: string }) {
  const [objective, setObjective] = useState("");
  const [result, setResult] = useState<StudioActionResult | undefined>();
  const [refusal, setRefusal] = useState<PendingRefusal | null>(null);
  const [pending, start] = useTransition();
  const version = panel.active_version;
  const members = version?.members ?? [];
  const run = () => {
    if (!version || !objective.trim()) return;
    setResult(undefined);
    setRefusal(null);
    start(async () => {
      const next = await callAction(runStudioPanel)(feed.projectId, version.id, objective.trim(), actor);
      // A Run refused for what the project lacks gets the same checklist as every other door.
      const readiness = !next.ok ? asReadiness(next.readiness) : null;
      if (readiness) {
        setRefusal({ label: "Run", message: next.message, readiness, retry: () => run() });
        return;
      }
      setResult(next);
    });
  };
  return (
    <article className="wf-ag-panel" data-testid={`agents-panel-${panel.panel_key}`}>
      <header className="wf-ag-profile-head">
        <strong title={panel.purpose || panel.description || undefined}>{panel.name}</strong>
        <span className="wf-tag">{words(version?.topology)}</span>
        {version ? <span className="wf-tag">v{version.version}</span> : null}
      </header>
      <div className="wf-ag-members">
        {members.map((m) => (
          <span key={m.member_key} title={m.perspective || (m.required ? "Required" : `Weight ${m.weight}`)}>
            <AgentChip roleKey={m.role_key} label={m.agent_name} size={24} />
          </span>
        ))}
        {members.length === 0 ? <span className="detail">No members</span> : null}
      </div>
      <form className="wf-ag-run" onSubmit={(e) => { e.preventDefault(); run(); }}>
        <input value={objective} placeholder="What should it decide?" aria-label="Question for the panel"
               onChange={(e) => setObjective(e.target.value)} />
        <button type="submit" className="wf-btn wf-btn-sm wf-btn-primary"
                disabled={pending || !version || !objective.trim()}>
          <Play size={12} aria-hidden /> {pending ? "Starting…" : "Run"}
        </button>
      </form>
      <Result result={result} />
      {refusal ? (
        <StartRefusal refusal={refusal} projectId={feed.projectId} busy={pending}
                      onDismiss={() => setRefusal(null)} />
      ) : null}
      <RecentRuns runs={feed.panelRuns.filter((r) => r.panel_key === panel.panel_key)} />
    </article>
  );
}
