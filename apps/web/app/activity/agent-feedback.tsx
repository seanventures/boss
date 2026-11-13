"use client";

/**
 * Activity › Feedback (BOSSAPP-889): what agents complained about or suggested (report_friction),
 * one card per target — the tool, skill, grant, brief or model it is about — most-open first. Each
 * report says who filed it, in which run, and what shows it. A person acknowledges, resolves or
 * dismisses it; agents never close their own. Nothing here asks a person for anything: a request
 * for help is an Ask card in Needs you.
 */
import React, { useCallback, useEffect, useState } from "react";

import { GatedButton } from "../../components/gated";
import {
  readFeedback, setFeedbackStatus, TARGET_WORDS, type FeedbackGroup, type FeedbackStatus,
} from "../../lib/agent-feedback";
import { ReflectionPanel } from "./reflection-panel";

type Show = "open" | "all";

export function AgentFeedback({ projectId }: { projectId: string }) {
  const [groups, setGroups] = useState<FeedbackGroup[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [show, setShow] = useState<Show>("open");
  const [said, setSaid] = useState<string | null>(null);

  const load = useCallback(async () => {
    const r = await readFeedback(projectId, show === "open" ? "open" : undefined);
    if (r.ok && "data" in r && r.data) { setGroups(r.data); setError(null); }
    else if (!r.ok) setError(r.error === "missing" ? "This install does not record agent feedback yet." : r.error);
  }, [projectId, show]);
  useEffect(() => { void load(); }, [load]);

  const mark = async (id: string, status: FeedbackStatus, words: string) => {
    const r = await setFeedbackStatus(projectId, id, status);
    setSaid(r.ok ? words : r.error ?? "Not saved.");
    if (r.ok) void load();
  };

  return (
    <div className="afb" data-testid="agent-feedback">
      {/* What the reflection agent proposes from these reports (BOSSAPP-888). */}
      <ReflectionPanel projectId={projectId} />
      <div className="tc-tabs" role="radiogroup" aria-label="Which feedback">
        {(["open", "all"] as const).map((s) => (
          <button key={s} type="button" role="radio" aria-checked={show === s} className="tc-tab"
                  data-testid={`agent-feedback-show-${s}`} onClick={() => setShow(s)}>
            {s === "open" ? "Open" : "All"}
          </button>
        ))}
      </div>
      {said ? <p role="status" data-testid="agent-feedback-said">{said}</p> : null}
      {error ? <p data-testid="agent-feedback-error">{error}</p> : null}
      {!groups && !error ? <p aria-busy="true">Loading…</p> : null}
      {groups && groups.length === 0 ? (
        <p data-testid="agent-feedback-empty">
          {show === "open" ? "No open feedback. Agents file it with report_friction when something gets in their way." : "No feedback yet."}
        </p>
      ) : null}
      {(groups ?? []).map((g) => (
        <section key={`${g.target_kind}:${g.target}`} className="afb-group"
                 data-testid={`agent-feedback-group-${g.target_kind}-${g.target}`}>
          <h4>
            <span className="afb-kind">{TARGET_WORDS[g.target_kind] ?? g.target_kind}</span>{" "}
            <span className="afb-target">{g.target}</span>
            <span className="afb-counts" title="Complaints · suggestions">
              {" "}{g.complaints} complaint{g.complaints === 1 ? "" : "s"} · {g.suggestions} suggestion{g.suggestions === 1 ? "" : "s"}
            </span>
          </h4>
          <ul>
            {g.items.map((f) => (
              <li key={f.id} data-testid={`agent-feedback-${f.id}`} data-status={f.status}>
                <span className={`afb-badge afb-${f.kind}`}>{f.kind === "complaint" ? "Complaint" : "Suggestion"}</span>{" "}
                <span>{f.body}</span>
                <span className="afb-meta">
                  {" — "}{f.role_key ?? "an agent"}{f.run_ref ? ` in ${f.run_ref}` : ""}
                  {f.status !== "open" ? ` · ${f.status}${f.resolution ? `: ${f.resolution}` : ""}` : ""}
                </span>
                {f.evidence.length ? (
                  <details><summary>What shows it</summary>
                    <ul>{f.evidence.map((e, i) => <li key={i}><code>{e}</code></li>)}</ul>
                  </details>
                ) : null}
                {f.status === "open" || f.status === "acknowledged" ? (
                  <span className="afb-actions">
                    {f.status === "open" ? (
                      <GatedButton permission="agent:update" projectId={projectId} className="kit-link"
                                   data-testid={`agent-feedback-ack-${f.id}`}
                                   onClick={() => void mark(f.id, "acknowledged", "Acknowledged.")}>Acknowledge</GatedButton>
                    ) : null}
                    <GatedButton permission="agent:update" projectId={projectId} className="kit-link"
                                 data-testid={`agent-feedback-resolve-${f.id}`}
                                 onClick={() => void mark(f.id, "resolved", "Resolved.")}>Resolve</GatedButton>
                    <GatedButton permission="agent:update" projectId={projectId} className="kit-link"
                                 data-testid={`agent-feedback-dismiss-${f.id}`}
                                 onClick={() => void mark(f.id, "dismissed", "Dismissed.")}>Dismiss</GatedButton>
                  </span>
                ) : null}
              </li>
            ))}
          </ul>
        </section>
      ))}
    </div>
  );
}
