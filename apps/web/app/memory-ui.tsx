"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { apiWrite } from "../lib/api-write";

/** What BOSS knows about this project, and the rules waiting to be allowed to bind.
 *
 *  THE STORE IS USELESS TO AN OPERATOR WHO CANNOT SEE IT.
 *
 *  A lesson is a rule every future session will follow. One that is wrong and invisible is
 *  worse than having no memory at all — which is why BOSS records lessons as `proposed` and
 *  binds nothing until a human says so, and why that queue is the first thing on this panel
 *  rather than a settings page nobody opens.
 *
 *  Phase F0.3 / F0.4 of docs/analysis/kiro-gap-features.md.
 */

type Memory = {
  id: string;
  kind: "lesson" | "project" | "preference";
  scope: string;
  scope_ref: string | null;
  body: string;
  status: "proposed" | "active" | "retired" | "rejected";
  use_count: number;
  last_used_at: string | null;
  created_at: string;
};

type Payload = {
  memories?: Memory[];
  awaiting_review?: number;
  counts?: Record<string, number>;
  error?: string;
};

const KIND_LABEL: Record<Memory["kind"], string> = {
  lesson: "Rule",
  project: "Context",
  preference: "Preference",
};

/** What each kind actually does to a turn, said once here so nobody has to infer it from a
 *  badge. The distinction is the whole reason a lesson needs review and the others do not. */
const KIND_HELP: Record<Memory["kind"], string> = {
  lesson: "Binding. Every session follows this without being asked again.",
  project: "Informs answers about this project.",
  preference: "Shapes how answers are written.",
};

export function MemoryHome({ projectId, actor }: { projectId: string; actor: string }) {
  const [data, setData] = useState<Payload | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (!projectId) return;
    try {
      const params = new URLSearchParams({ project_id: projectId, actor });
      const r = await fetch(`/api/memory?${params}`, { cache: "no-store" });
      const body = (await r.json()) as Payload;
      // SAY WHAT FAILED AND WHAT TO DO. This read "failed" — a word with no subject and no
      // next step (console walk, 2026-10-02).
      if (r.ok) { setData(body); setError(null); }
      else {
        setError(`Could not load what BOSS remembers: ${body?.error ?? `HTTP ${r.status}`}. `
                 + "Reload the page; if it stays like this, check the API in Repo and models.");
      }
    } catch (e) {
      setError(`Could not load what BOSS remembers: ${String((e as Error)?.message ?? e)}. `
               + "Reload the page; if it stays like this, check the API in Repo and models.");
    }
  }, [projectId, actor]);

  useEffect(() => { void load(); }, [load]);

  const act = useCallback(async (
    memory: Memory, action: "review" | "forget", decision?: "active" | "rejected",
  ) => {
    // Retiring a rule changes what every later session is told, and there is no undo here.
    if (action === "forget" && typeof window !== "undefined"
        && !window.confirm(`Retire this memory? Agents stop being told: "${memory.body.slice(0, 120)}"`)) {
      return;
    }
    setBusy(memory.id);
    try {
      const r = await apiWrite("/api/memory", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          action, memory_id: memory.id, project_id: projectId, actor, decision,
        }),
      });
      const body = await r.json().catch(() => ({}));
      if (!r.ok) {
        const verb = action === "forget" ? "retire" : decision === "rejected" ? "reject" : "accept";
        setError(`Could not ${verb} "${memory.body.slice(0, 80)}": ${body?.error ?? `HTTP ${r.status}`}. `
                 + "Nothing changed — press it again.");
      } else setError(null);
      // RELOAD RATHER THAN PATCH LOCAL STATE. Accepting a rule changes what every later
      // session is told; showing a stale list after that is how an operator ends up unsure
      // whether the thing they just pressed took effect.
      await load();
    } finally {
      setBusy(null);
    }
  }, [projectId, actor, load]);

  const memories = data?.memories ?? [];
  const proposed = useMemo(
    () => memories.filter((m) => m.status === "proposed"), [memories]);
  const active = useMemo(
    () => memories.filter((m) => m.status === "active"), [memories]);

  return (
    <details className="stack-card" data-testid="memory-home" open={proposed.length > 0}>
      {/* OPEN WHEN SOMETHING IS WAITING. A review queue behind a closed fold is a queue that
          never gets emptied; with nothing pending this collapses like every other section. */}
      <summary className="skill-form-summary">
        What BOSS knows{memories.length ? ` (${active.length})` : ""}
        {proposed.length > 0 ? (
          <span className="mem-badge" data-testid="memory-awaiting">
            {proposed.length} awaiting review
          </span>
        ) : null}
      </summary>

      <p className="detail mem-intro">
        Recorded in earlier sessions and read at the start of every new one — this is why you
        do not have to repeat yourself. A <strong>Rule</strong> is binding, so BOSS cannot make
        one on its own: it proposes, you decide.
      </p>

      {error ? <p className="mem-error" data-testid="memory-error">{error}</p> : null}

      {proposed.length > 0 ? (
        <section className="mem-queue" data-testid="memory-queue">
          <h3>Waiting for you</h3>
          <p className="detail">
            BOSS wants to follow these from now on. Nothing below is in force yet.
          </p>
          <ul className="mem-list">
            {proposed.map((m) => (
              <li key={m.id} className="mem-row" data-proposed data-testid={`memory-${m.id}`}>
                <div className="mem-row-main">
                  <span className="mem-kind" data-kind={m.kind}>{KIND_LABEL[m.kind]}</span>
                  <span className="mem-body">{m.body}</span>
                </div>
                <div className="mem-row-actions">
                  <button type="button" className="cap-btn primary" disabled={busy === m.id}
                          onClick={() => act(m, "review", "active")}
                          data-testid={`memory-accept-${m.id}`}>
                    {busy === m.id ? "…" : "Accept"}
                  </button>
                  <button type="button" className="linklike" disabled={busy === m.id}
                          onClick={() => act(m, "review", "rejected")}
                          data-testid={`memory-reject-${m.id}`}>
                    Reject
                  </button>
                </div>
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      {active.length > 0 ? (
        <section className="mem-known" data-testid="memory-active">
          <h3>In force</h3>
          <ul className="mem-list">
            {active.map((m) => (
              <li key={m.id} className="mem-row" data-testid={`memory-${m.id}`}>
                <div className="mem-row-main">
                  <span className="mem-kind" data-kind={m.kind} title={KIND_HELP[m.kind]}>
                    {KIND_LABEL[m.kind]}
                  </span>
                  <span className="mem-body">{m.body}</span>
                </div>
                <div className="mem-row-actions">
                  {/* USED-COUNT IS THE HONEST SIGNAL. Something recorded and never once
                      relevant is noise that costs prompt budget on every single turn, and
                      this is what tells an operator which entries those are. */}
                  <span className="detail mem-used">
                    {m.use_count > 0 ? `used ${m.use_count}×` : "never used"}
                  </span>
                  <button type="button" className="linklike" disabled={busy === m.id}
                          onClick={() => act(m, "forget")}
                          data-testid={`memory-forget-${m.id}`}>
                    Retire
                  </button>
                </div>
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      {memories.length === 0 ? (
        <p className="detail" data-testid="memory-empty">
          Nothing recorded yet. Correct BOSS in a conversation — &ldquo;always run the frontend
          checks before calling a change done&rdquo; — and it will propose that as a rule here.
        </p>
      ) : null}
    </details>
  );
}

/** Where a long conversation had got to — read from storage, not from the scrollback.
 *
 *  A long thread eventually has its early turns compacted away and the objective is usually
 *  the first thing to go. This is the same record the agent re-reads each turn, shown to the
 *  operator so both are looking at one thing.
 *
 *  Renders nothing when there is no open ledger, which is most conversations.
 */
export function LedgerStrip({ threadId }: { threadId: string | null }) {
  const [ledger, setLedger] = useState<
    { goal: string; phase?: string | null; next_step?: string | null;
      rejected?: string[] } | null>(null);

  useEffect(() => {
    if (!threadId) { setLedger(null); return; }
    let alive = true;
    void (async () => {
      try {
        const r = await fetch(
          `/api/agent-thread?thread_id=${encodeURIComponent(threadId)}`,
          { cache: "no-store" });
        const body = await r.json();
        if (alive && r.ok) setLedger(body?.ledger ?? null);
      } catch {
        /* the strip is an aid; failing to load one must not disturb the conversation */
      }
    })();
    return () => { alive = false; };
  }, [threadId]);

  if (!ledger) return null;
  return (
    <div className="ledger-strip" data-testid="ledger-strip">
      <span className="ledger-label">Goal</span>
      <span className="ledger-goal">{ledger.goal}</span>
      {ledger.next_step ? (
        <>
          <span className="ledger-label">Next</span>
          <span className="ledger-next">{ledger.next_step}</span>
        </>
      ) : null}
      {ledger.rejected?.length ? (
        <span className="detail ledger-tried" title={ledger.rejected.join("\n")}>
          {ledger.rejected.length} tried and rejected
        </span>
      ) : null}
    </div>
  );
}
