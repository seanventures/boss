"use client";

/**
 * Who is on this app, on the screen where the work happens.
 *
 * A workflow showed eight steps and never said who would run them. The only role anywhere on
 * the page was the gate's `approver_role` — "release_manager signs this off" — and that is one
 * agent out of the several that build, review, test and ship the thing. The implicit assignment
 * lived in `STAGE_ROLES`, a map used for cost attribution at write time and never displayed.
 *
 * FIRST IN THE CHAIN, BUT NOT A CAPABILITY. This began as a panel above the rail and the
 * operator could not find it — "where when creating a new project do we have the option to
 * assign the agents?" A block over the chain is something you have to notice; a node IN the
 * chain is where somebody reading the steps already is. So it renders as step one.
 *
 * It is still not a workflow capability. A real step would mean editing all twelve catalogue
 * workflows, a new capability in the runtime, and something that can execute, fail and block a
 * run — and assignment is a property of the app, not a stage of its delivery. The node carries
 * no duration and no status badge, because it does not run: it says "auto-confirmed", which is
 * exactly what happened when start_run seeded it.
 *
 * The badge on each name is the part worth reading: `default` means BOSS seeded the agents that
 * would have done the work anyway, and nobody has looked. `assigned` means somebody chose.
 */

import { useCallback, useEffect, useState } from "react";

import { AgentChip } from "./agent-chip";
import { notifyChanged, onChanged } from "./changed";

type Assigned = { role_key: string; source: string; assigned_by?: string | null };

/** What each badge means, said in words rather than left as a status word.
 *
 *  `builtin` is the one that matters: nothing is recorded for this app, and BOSS runs the
 *  project's team on it anyway. Calling that "nobody is assigned" reported an empty table as a
 *  fact about the world, over a live run with seven agents on it. */
const SOURCE_LABEL: Record<string, string> = {
  builtin: "by default",
  default: "seeded",
  operator: "chosen",
};

export function AgentsOnThis({ projectId, appName, actor, roles, readOnly = false }: {
  projectId: string;
  /** The app, by the name the run carries. Assignment is keyed on its identity, not this text. */
  appName: string;
  actor: string;
  /** Every agent this project has, to choose from. */
  roles: Array<{ role_key: string; display_name?: string | null }>;
  /** A FINISHED RUN'S ASSIGNMENT IS HISTORY. Offering "+ Assign" on a completed workflow invites
   *  a change that cannot affect anything: the work is done and the gates it would have governed
   *  are already answered. Shown, never editable. */
  readOnly?: boolean;
}) {
  const [agents, setAgents] = useState<Assigned[] | null>(null);
  const [adding, setAdding] = useState(false);
  const [busy, setBusy] = useState("");
  const [failed, setFailed] = useState("");

  const load = useCallback(async () => {
    if (!appName.trim()) return;
    try {
      const r = await fetch(
        `/api/project-agents?project_id=${encodeURIComponent(projectId)}`
        + `&app=${encodeURIComponent(appName)}`, { cache: "no-store" });
      const body = await r.json().catch(() => ({}));
      if (!r.ok) { setFailed(String(body?.error ?? `HTTP ${r.status}`)); return; }
      setAgents((body?.agents ?? []) as Assigned[]);
      setFailed("");
    } catch (e) {
      setFailed(String((e as Error)?.message ?? e));
    }
  }, [projectId, appName]);

  useEffect(() => { void load(); }, [load]);
  // An assignment made on the agent's own page, or a run that seeded its defaults, changes what
  // this shows. Same signal the rest of the console already listens on.
  useEffect(() => onChanged(() => { void load(); }, "agents-on-this"), [load]);

  const act = useCallback(async (body: Record<string, unknown>, label: string) => {
    setBusy(label);
    setFailed("");
    try {
      const r = await fetch("/api/project-agents", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ project_id: projectId, app: appName, actor, ...body }),
      });
      const data = await r.json().catch(() => ({}));
      if (!r.ok) { setFailed(String(data?.error ?? `HTTP ${r.status}`)); return; }
      await load();
      // The agent detail page lists what it is assigned to; it must not go stale behind this.
      notifyChanged("agents");
    } catch (e) {
      setFailed(String((e as Error)?.message ?? e));
    } finally {
      setBusy("");
    }
  }, [projectId, appName, actor, load]);

  if (!appName.trim()) return null;

  const assigned = agents ?? [];
  // NOTHING RECORDED, but agents run it anyway — the project's team. The distinction the panel
  // must never lose: "no row" is not "no one".
  const builtin = assigned.length > 0 && assigned.every((a) => a.source === "builtin");
  const onIt = new Set(assigned.map((a) => a.role_key));
  const available = roles.filter((r) => !onIt.has(r.role_key));
  const nameOf = (key: string) =>
    roles.find((r) => r.role_key === key)?.display_name || key.replace(/_/g, " ");

  return (
    <section className="aot" data-testid="agents-on-this">
      <div className="aot-head">
        <span className="aot-label">Agents on this</span>
        {agents === null && !failed ? <span className="detail">Reading…</span> : null}
        {failed ? <span className="detail aot-bad">{failed}</span> : null}
      </div>

      {agents !== null && !assigned.length ? (
        // NOBODY ASSIGNED IS NOT THE SAME AS NOBODY WORKING. An app with no assignment lets any
        // agent act — enforcement stops work that contradicts a decision, not work nobody has
        // decided about — and saying so is the difference between "unconfigured" and "blocked".
        <p className="detail" data-testid="aot-empty">
          {/* Reached only when the project itself has no team AND nothing is recorded, which is
              a real state and a rare one. It still does not claim nobody works on it. */}
          {readOnly
            ? "No agents were recorded for this run."
            : "No agents are recorded for this app yet."}
        </p>
      ) : null}

      <div className="aot-list">
        {/* A FACE AND A NAME, the way the meeting room has always drawn a panel. These were
            bare text with a tiny uppercase source word after each — "Adversarial Reviewer BY
            DEFAULT  Chief Architect BY DEFAULT" — a run-on line where the label competed with
            the name and nothing separated one agent from the next. */}
        {assigned.map((a) => (
          <AgentChip key={a.role_key} roleKey={a.role_key} label={nameOf(a.role_key)}
                     note={SOURCE_LABEL[a.source] ?? a.source}
                     href={`?area=workforce&role=${encodeURIComponent(a.role_key)}`}>
            {readOnly ? null : (
              <button type="button" className="aot-x" aria-label={`Remove ${nameOf(a.role_key)}`}
                      disabled={!!busy}
                      onClick={() => void act({ action: "unassign", role_key: a.role_key },
                                              a.role_key)}>
                {busy === a.role_key ? "…" : "✕"}
              </button>
            )}
          </AgentChip>
        ))}

        {readOnly ? null : adding ? (
          <select className="mr-select aot-pick" defaultValue="" disabled={!!busy}
                  data-testid="aot-pick"
                  onChange={(e) => {
                    if (!e.target.value) return;
                    void act({ role_keys: [e.target.value] }, e.target.value);
                    setAdding(false);
                  }}>
            <option value="">Choose an agent…</option>
            {available.map((r) => (
              <option key={r.role_key} value={r.role_key}>
                {r.display_name || r.role_key}
              </option>
            ))}
          </select>
        ) : (
          <button type="button" className="aot-add" disabled={!!busy}
                  data-testid="aot-add" onClick={() => setAdding(true)}>
            + Assign
          </button>
        )}
      </div>

      {assigned.length ? (
        <p className="detail aot-note">
          {readOnly
            ? "This run is finished \u2014 who was on it is a record now, not a setting."
            : builtin
              ? "These run by default. Add or remove one and it becomes this app\u2019s own "
                + "list, and only those agents may sign off its gates."
              : "Only these agents may sign off this app\u2019s gates. Anyone else is refused, "
                + "and the refusal names who is on it."}
        </p>
      ) : null}
    </section>
  );
}
