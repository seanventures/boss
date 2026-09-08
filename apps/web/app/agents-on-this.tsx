"use client";

/**
 * Who is on this app, on the screen where the work happens.
 *
 * A workflow showed eight steps and never said who would run them. The only role anywhere on
 * the page was the gate's `approver_role` — "release_manager signs this off" — and that is one
 * agent out of the several that build, review, test and ship the thing. The implicit assignment
 * lived in `STAGE_ROLES`, a map used for cost attribution at write time and never displayed.
 *
 * WHY THIS IS A PANEL AND NOT A STEP. Making it a step would put it in the chain, where it
 * could execute, fail, and block a run — and it would have to be added to all twelve catalogue
 * workflows. Assignment is a property of the app, not a stage of its delivery. Panel.
 *
 * The badge on each name is the part worth reading: `default` means BOSS seeded the agents that
 * would have done the work anyway, and nobody has looked. `assigned` means somebody chose.
 */

import { useCallback, useEffect, useState } from "react";

import { notifyChanged, onChanged } from "./changed";

type Assigned = { role_key: string; source: string; assigned_by?: string | null };

export function AgentsOnThis({ projectId, appName, actor, roles }: {
  projectId: string;
  /** The app, by the name the run carries. Assignment is keyed on its identity, not this text. */
  appName: string;
  actor: string;
  /** Every agent this project has, to choose from. */
  roles: Array<{ role_key: string; display_name?: string | null }>;
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
          Nobody is assigned, so any agent may work on this. Assign someone to make that a
          decision rather than a default.
        </p>
      ) : null}

      <div className="aot-list">
        {assigned.map((a) => (
          <span className="aot-agent" key={a.role_key} data-source={a.source}
                data-testid={`aot-${a.role_key}`}>
            <a href={`?area=workforce&role=${encodeURIComponent(a.role_key)}`}
               className="aot-name">{nameOf(a.role_key)}</a>
            {/* SEEDED OR CHOSEN. A default is BOSS guessing from the stages that would have run;
                a person choosing is a decision. The gate refuses on both, so which one it is
                matters when the refusal arrives. */}
            <span className="aot-src">{a.source === "default" ? "default" : "assigned"}</span>
            <button type="button" className="aot-x" aria-label={`Remove ${nameOf(a.role_key)}`}
                    disabled={!!busy}
                    onClick={() => void act({ action: "unassign", role_key: a.role_key },
                                            a.role_key)}>
              {busy === a.role_key ? "…" : "✕"}
            </button>
          </span>
        ))}

        {adding ? (
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
          Only these agents may sign off this app&rsquo;s gates. Anyone else is refused, and the
          refusal names who is on it.
        </p>
      ) : null}
    </section>
  );
}
