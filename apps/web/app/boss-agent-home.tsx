"use client";

/**
 * The BOSS Agent — one surface, with the whole factory in front of it.
 *
 * THREE THINGS BECAME ONE. There were an agent chat area (a thread rail and a composer that
 * knew nothing about the system it was attached to), a Metatron Captain page (whose durable
 * session table does not exist in this database, so it was a shell), and a Task Shepherd
 * autopilot (switched off after one afternoon of repairing things nobody asked about). The
 * operator used one of the three and did not like it.
 *
 * What they DID like is the agent as it appears on Workflows, Infra, Factory and Monitoring —
 * and what makes it good there is not the model. It is that the screen hands it the facts
 * before the first word: the app that is down, named, with its error and its address. A
 * composer with no context is the same agent doing worse, because the operator has to type
 * what the page already knew.
 *
 * So this page is the briefing first and the conversation second. Every row names something
 * real, carries what the failure MEANS — the Shepherd's taxonomy, which was always the good
 * part — and offers the remedy as a button. Nothing runs until it is pressed; that is the one
 * thing the Shepherd got wrong and the reason it was turned off.
 */

import { useCallback, useEffect, useState } from "react";

import { AgentChat } from "./agent-ui";
import { onChanged } from "./changed";

type Action = { tool: string; args: Record<string, unknown>; label: string };
type Diagnosis = { kind?: string; remedies?: { id: string; describe: string;
                                               action?: string | null }[] };
type Item = {
  kind: string; id: string;
  title?: string; app?: string; step?: string; environment?: string; at?: string;
  status?: string; error?: string; note?: string; since?: string;
  phase?: string | null; quiet_minutes?: number; run_id?: string;
  /** "4 days ago" — a stopped row four days old is a different fact from one four minutes old. */
  age?: string;
  /** Whether the recorded reason still holds. Two rows asserted "no active deployment target"
   *  while the project had six. */
  blocker?: { cleared?: boolean; reason?: string } | null;
  diagnosis?: Diagnosis; actions?: Action[];
};
type Briefing = {
  items: Item[]; counts: Record<string, number>; total: number; quiet: boolean;
  /** Sections that could not be read. Never rendered as "nothing needs you". */
  errors?: Record<string, string>;
};

const GROUPS: Array<[string, string, string]> = [
  ["decision", "Waiting for you", "Nothing moves until these are answered."],
  ["workflow_step", "Stopped", "A step failed. What it means is below it."],
  ["unhealthy_app", "Not answering", "Deployed, and not serving."],
  ["quiet_build", "Quiet", "Running, but it has not said anything for a while."],
];

/** One line an operator can read at a glance, and the agent can act on verbatim. */
export function itemHeadline(item: Item): string {
  switch (item.kind) {
    case "decision":
      return item.title || item.id;
    case "workflow_step":
      return `${item.app ?? "an app"} — ${item.step} ${item.status}`;
    case "unhealthy_app":
      return `${item.app} (${item.environment}) at ${item.at}`;
    case "quiet_build":
      return `${item.app} — ${item.quiet_minutes}m quiet in ${item.phase ?? "its phase"}`;
    default:
      return item.id;
  }
}

/** The question this row would ask, with its facts already in it. */
export function itemQuestion(item: Item): string {
  switch (item.kind) {
    case "decision":
      return `Decision ${item.id} — "${item.title}" — is unanswered. What is it gating, what ` +
             "happens if I approve it, and is there any reason not to?";
    case "workflow_step":
      return `${item.app}: the '${item.step}' step is ${item.status} — ${item.error ?? ""}. ` +
             "Why is it in this state, and what is the fix? Check the app's code and the host " +
             "before answering.";
    case "unhealthy_app":
      return `${item.app} (${item.environment}) is deployed at ${item.at} and not answering` +
             `${item.error ? ` — ${item.error}` : ""}. What is actually wrong?`;
    case "quiet_build":
      return `${item.app} has been quiet for ${item.quiet_minutes} minutes in ` +
             `${item.phase ?? "its current phase"}. Read its activity and tell me whether it is ` +
             "working or stuck — do not cancel anything.";
    default:
      return `Tell me about ${item.id}.`;
  }
}

function openDock(question: string): void {
  try {
    const url = new URL(window.location.href);
    url.searchParams.set("agent", "general");
    url.searchParams.set("dock_ask", question);
    window.history.replaceState(null, "", url.toString());
    window.dispatchEvent(new PopStateEvent("popstate"));
  } catch { /* non-browser */ }
}

export function BossAgentHome({ projectId, actor, initialTaskId, roleName }: {
  projectId: string; actor: string; initialTaskId?: string; roleName?: string;
}) {
  const [briefing, setBriefing] = useState<Briefing | null>(null);
  const [failed, setFailed] = useState("");

  const load = useCallback(async () => {
    try {
      const r = await fetch(
        `/api/agent?view=briefing&project_id=${encodeURIComponent(projectId)}` +
        `&actor=${encodeURIComponent(actor)}`, { cache: "no-store" });
      if (!r.ok) { setFailed(`the briefing could not be read (HTTP ${r.status})`); return; }
      const d = await r.json();
      if (d && !("error" in d)) { setBriefing(d as Briefing); setFailed(""); }
      else setFailed(String(d?.error ?? "the briefing could not be read"));
    } catch (e) {
      setFailed(String((e as Error)?.message ?? e));
    }
  }, [projectId, actor]);

  useEffect(() => { void load(); }, [load]);
  // A decision approved on the Approvals screen, a step re-queued from Workflows, a target set
  // in Factory — all of them change what is on this list.
  useEffect(() => onChanged(() => { void load(); }, "agent-home"), [load]);

  const items = briefing?.items ?? [];
  return (
    <div style={{ display: "grid", gap: 18 }} data-testid="boss-agent-home">
      <section className="ct-block" data-testid="agent-briefing">
        <div className="ct-block-title">
          What wants you{briefing ? ` · ${briefing.total}` : ""}
        </div>
        {failed ? (
          // NOT an empty list. "Nothing needs you" and "I could not look" are opposite claims,
          // and rendering the second as the first is how a console says everything is fine
          // during an outage.
          <p className="detail" data-testid="agent-briefing-failed">
            The briefing could not be read — {failed}. This is not the same as “nothing needs
            you”; ask below and BOSS will look directly.
          </p>
        ) : null}
        {/* PART OF THE FACTORY WENT UNREAD. Reporting the rest as the whole picture is how a
            console says everything is fine during an outage — and it already happened once
            here: a query named a column the table does not have, and the page answered
            "nothing needs you" while six decisions sat unanswered. */}
        {Object.keys(briefing?.errors ?? {}).length ? (
          <p className="detail" data-testid="agent-briefing-partial">
            Could not read: {Object.entries(briefing!.errors!)
              .map(([k, v]) => `${k} (${v})`).join("; ")}. What is listed below is therefore
            incomplete — ask BOSS and it will look directly.
          </p>
        ) : null}
        {!failed && briefing?.quiet ? (
          <p className="detail" data-testid="agent-briefing-quiet">
            Nothing is blocked, failed, unanswered or unhealthy right now. Ask anything below —
            BOSS reads the live system, it does not answer from this list.
          </p>
        ) : null}

        {GROUPS.map(([kind, heading, blurb]) => {
          const group = items.filter((i) => i.kind === kind);
          if (!group.length) return null;
          return (
            <div key={kind} className="fb-group" data-testid={`agent-group-${kind}`}>
              <div className="fb-group-head">
                <strong>{heading}</strong> <span className="detail">{blurb}</span>
              </div>
              {group.map((item) => (
                <div className="fb-item" key={`${item.kind}-${item.id}`} data-testid="agent-item"
                     data-cleared={item.blocker?.cleared ? "true" : undefined}>
                  <div className="fb-item-head">
                    {itemHeadline(item)}
                    {/* WHEN. Every stopped row on the live board was one to four days old and
                        rendered with no date, under a heading that reads as "right now". */}
                    {item.age ? <span className="fb-age" data-testid="agent-item-age">
                      {item.age}
                    </span> : null}
                  </div>
                  {/* THE REASON HAS SINCE GONE FALSE. Not dropped — the step really did stop,
                      and hiding that would be its own lie — but said plainly, so an operator
                      does not go looking for a problem that has already been dealt with. */}
                  {item.blocker?.cleared ? (
                    <div className="fb-cleared" data-testid="agent-item-cleared">
                      No longer true: {item.blocker.reason}
                    </div>
                  ) : null}
                  {item.error ? (
                    <div className={item.blocker?.cleared ? "fb-item-error fb-stale"
                                                          : "fb-item-error"}>
                      {item.blocker?.cleared ? "Recorded then: " : ""}{item.error}
                    </div>
                  ) : null}
                  {item.note ? <div className="detail">{item.note}</div> : null}
                  {/* WHAT IT MEANS — the Shepherd's taxonomy, which was always the good part.
                      It is offered here, never run: that is the one thing the autopilot got
                      wrong and the reason it was turned off. */}
                  {item.diagnosis?.kind ? (
                    <div className="fb-diagnosis" data-testid="agent-diagnosis">
                      <span className="fb-kind">{item.diagnosis.kind}</span>
                      {(item.diagnosis.remedies ?? []).slice(0, 2).map((r) => (
                        <span className="detail" key={r.id}> · usually: {r.describe}</span>
                      ))}
                    </div>
                  ) : null}
                  <div className="dm-starters">
                    <button type="button" className="cap-btn" data-testid="agent-item-ask"
                            onClick={() => openDock(itemQuestion(item))}>
                      Ask BOSS about this
                    </button>
                  </div>
                </div>
              ))}
            </div>
          );
        })}
      </section>

      {/* The conversation, unchanged — same component the dock uses, so there is one chat in
          BOSS rather than two that drift. */}
      <AgentChat projectId={projectId} actor={actor}
                 initialTaskId={initialTaskId} roleName={roleName} />
    </div>
  );
}
